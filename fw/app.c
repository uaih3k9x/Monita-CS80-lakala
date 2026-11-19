/*
 * Monita-CS80 R05 主 app: USB 通道 (port 4) + 大屏推帧 + 段码屏。协议见 docs/protocol.md, 纯逻辑在 link/proto.h。
 *
 * 线程模型:
 *   USB 回调 (内核线程上下文) 只 release 一个信号量, 不读不解析;
 *   "monita" 任务 (prio 12) 等信号量 (最多 20 ms) -> 循环 ql_uart_read 读空 -> 拆帧 -> 写 fb / 回 ACK。
 *   RECT 直接从帧缓冲区 (一帧 payload, ≤ 32 KiB) 拷进扫描缓冲; PRESENT 时统一 clean D-cache。
 *
 * 开机画面 (v7): 版权页 (lcd/bootscreen.h, EDK2 UEFI 字体), 画在前台缓冲; Link 行黄色 WAIT, 第一条 HELLO 改绿色 OK,
 *   之后主机推帧接管全屏。
 * 段码屏: 主区 "------"; HELLO 带了时间后小时钟走 hh:mm:ss。主机发过 SEG 后 5 s 内由主机接管整块显存。
 * 按键 (v8): 主循环每 ≈ 10 ms 采样 (io/keys_hw.h), 去抖/长按在 link/keys.h; HELLO 之后才发 KEY (0x90), 同一任务里写, 不和应答交错。
 * 状态灯 (v8): link/led.h 状态机, 主循环每轮算一次掩码, 变了才写 GPIO (不开任务、不忙等)。
 * 沉浸模式 (v9): IMM_ENTER 把第二、三块 fb 还给堆, 换一块尽量长的"卷轴" (link/imm.h); 整屏只显示卷轴里的一段,
 *   滚动 = 主循环每帧最多写一次扫描基址 (缓动在固件里), 原厂 fb 当保留缓冲 (换底绕道 / 退出时留住最后一帧)。
 *   IMM_EXIT 还掉卷轴、重新拿第二、三块 (拿不到就少几块, ACK 里给新的 caps)。沉浸模式下 RECT / COPY / PRESENT / 旧 DIAG 一律拒。
 *   未连接: 蓝灯 2 s 亮 1 s 灭 (同原厂常态心跳); HELLO 后熄灭, 交给主机 LED (0x31);
 *   (v9) 主机走了又收到帧就当回来了: 恢复 KEY 上报, 灯恢复成主机最后设的样子 (主机睡醒后不一定重发 HELLO)。
 * 主机走了 / 休眠 (v10, link/linkwd.h + dispatch.h):
 *   HELLO 之后 5 s 没有有效帧, 或 USB 拔掉 (ql_usb_bind_hotplug_cb 回调报"拔掉", 只在 VBUS 也断了时有) = 主机走了:
 *   大屏重画开机版权页 (Link 行 WAIT)、段码屏回本地时钟、蓝灯心跳, 不再发 KEY。下一条 HELLO 照旧把 Link 行改 OK, 主机推整帧。
 *   主机发 SLEEP (0x32, Mac 睡眠 / 关显示器时) 或没主机满 10 min: 关背光 (GPIO7)、段码屏 EN (GPIO10) 拉低、灭灯;
 *   任何一帧叫醒 (段码屏重新初始化), 按键也能叫醒 (显示开机页, 10 min 后再睡)。
 * RECT_LZ4 (v11, link/lz4.h + dispatch.h): LZ4 raw block 先整块解进 g_lz (bss, 32 KiB), 校验过了再走 RECT 的路径写帧缓冲;
 *   坏块计 bad_param 不动像素。caps bit8。
 *
 * 入口约定 (照原厂): enter 里只配看门狗 + 建任务就返回, 不得阻塞; 任务 prio 12, 7 参数, event_count = 1。
 */
#include "fg00.h"
#include "segmap.h"
#include "lcd.h"
#include "proto.h"
#include "blit.h"
#include "keys_hw.h"
#include "led_hw.h"
#include "linkwd.h"
#include "imm.h"

#define FW_VER       0x000Bu          /* 11: RECT_LZ4 (0x23, link/lz4.h, caps bit8); 10: 主机走了回开机画面 / SLEEP + 10 min 自动休眠 (link/linkwd.h), USB 拔掉即时检测; 9: 沉浸模式 (link/imm.h), 主机不经 HELLO 回来时恢复 KEY/灯 (link/linkwd.h), PWRKEY 慢采样不丢轻点; 8: 按键 KEY + 状态灯 LED; 7: 开机版权页 (lcd/bootscreen.h); 6: 三缓冲 (link/tribuf.h), DMA 研究代码已移除 (DIAG 26–30); 4: 跨缓冲 COPY, 默认立即翻页 */
#define MAX_PAYLOAD  32768u          /* 800 x 20 行 RGB565 + 8 字节头 = 32008, 放得下一条整行带 */
#define RX_CHUNK     16384u
#define PORT         R05_PORT_USB_MODEM
#define SEG_HOST_HOLD_US 5000000u    /* 主机发 SEG 后这么久内固件不画自己的段码 */
#define KEY_SAMPLE_MS 8u             /* 按键最短采样间隔 (1.024 ms 单位); 主循环空闲时 10 ms 醒一次 */

static uint8_t  g_pl[MAX_PAYLOAD] __attribute__((aligned(4)));
static uint8_t  g_rx[RX_CHUNK] __attribute__((aligned(4)));
/* RECT_LZ4 (v11) 暂存区: 整块解在这里 (前 8 字节补 RECT 头), 校验过了才走 lk_rect_apply 写帧缓冲, 坏块不动像素。
 * 放 bss (RAM 窗口) 而不是 malloc: 不跟多缓冲 / 卷轴抢堆, 开机就有; raw_len ≤ MAX_PAYLOAD - 8 正好放得下 */
static uint8_t  g_lz[MAX_PAYLOAD] __attribute__((aligned(4)));
static const uint32_t g_lz4_ok = 1;
static uint8_t  g_tx[64];
static void    *g_sem;
static void    *g_task;
static lk_parser_t g_p;

static struct {
    uint32_t rx_bytes, rects, presents, bad_param, lz4;
} st;

static uint32_t now_us(void){ return (uint32_t)osiUpTimeUS(); }   /* 只用来做差, 71 分钟回绕无所谓 */
static uint32_t now_ms(void){ return (uint32_t)(osiUpTimeUS() >> 10); }   /* 1.024 ms 单位 (同 ACK.uptime), 按键/状态灯用 */

#include "segclock.h"   /* udiv + 段码屏本地时钟 (clk_set / clk_tick / seg_draw_local), 和 tools/fwsim 共用 */

/* ---- 多缓冲 (fw v6: 三缓冲, 见 link/tribuf.h) ----
 * 开机从内核堆 malloc 第二块、第三块 fb。三块都拿到 (且之后堆里还剩 TRI_RESERVE): 三缓冲, PRESENT 翻页后下一批立刻画第三块,
 * 不用等旧前台过帧头; 只拿到两块: 双缓冲 (下一批要等一帧); 一块都没拿到: 单缓冲。
 * 扫描基址 0x34 帧头锁存 (实测), 所以 PRESENT 写完基址立即应答。 */
#include "lcdc.h"
#include "tribuf.h"
#define TRI_RESERVE (128u * 1024u)     /* 拿第三块后堆里至少还要留这么大的连续块给内核 */
static uint32_t g_fb2, g_fb2_raw;      /* 第二块 fb (256 B 对齐) 和 malloc 原始指针 */
static uint32_t g_fb3, g_fb3_raw;      /* 第三块; 0 = 没拿到 */
static uint32_t g_nbuf = 1;            /* 拿到的块数 */
static uint32_t g_tri_note;            /* 第三块的结果: 0 拿到, 1 malloc 失败, 2 拿到后余量不够 (已还), 3 没试 (第二块都没有) */
static tb_t g_tb;
static uint32_t g_flip_wait_us;        /* 最近一次 PRESENT 等帧事件的总时长 */
/* 旧 DIAG (diag.h / zc_diag.h) 用的"双缓冲视角": 只在 tri_collapse() 之后有效 */
static uint32_t g_front = FB_ADDR;     /* 正在扫描的 */
static uint32_t g_back;                /* 后台; 0 = 单缓冲 */

static uint32_t tb_addr(int i){ return (uint32_t)g_tb.buf[i]; }
static uint32_t draw_target(void){ return tb_addr(g_tb.d >= 0 ? g_tb.d : g_tb.s); }

/* 旧 DIAG 用: 直接翻到 fb (只在 collapse 状态下) */
static void lcd_show(uint32_t fb){
    ql_mipi_lcd_write_screen((void *)fb);
    if (g_back) g_back = (fb == FB_ADDR) ? g_fb2 : FB_ADDR;
    g_front = fb;
}

/* n 字节 (32 的倍数, 两边 32 B 对齐) 按地址升序 ldm/stm 搬; 同一块里 dst < src 的重叠也安全 (先读满 8 个字再写) */
static void move32(uint32_t d, uint32_t s, uint32_t n){
    uint32_t k = n / 32u;
    if (!k) return;
    __asm__ volatile(
        "1: ldmia %1!, {r4-r11}\n"
        "   stmia %0!, {r4-r11}\n"
        "   subs %2, %2, #1\n"
        "   bne 1b\n"
        : "+r"(d), "+r"(s), "+r"(k) :: "r4", "r5", "r6", "r7", "r8", "r9", "r10", "r11", "cc", "memory");
}

/* 行区间整块拷贝 (两块不同缓冲), CPU 搬:
 * 一行 1600 B = 50 × 32 B, 两边都 32 B 对齐 (fb 基址 256 B 对齐), 用 ldm/stm 每次 8 个字, 再 clean 目的。 */
static void copy_rows(uint32_t dst, uint32_t src, uint32_t y0, uint32_t y1){
    if (y1 <= y0) return;
    uint32_t d = dst + y0 * LCD_W * 2u, s = src + y0 * LCD_W * 2u, n = (y1 - y0) * LCD_W * 2u / 32u;
    __asm__ volatile(
        "1: ldmia %1!, {r4-r11}\n"
        "   stmia %0!, {r4-r11}\n"
        "   subs %2, %2, #1\n"
        "   bne 1b\n"
        : "+r"(d), "+r"(s), "+r"(n) :: "r4", "r5", "r6", "r7", "r8", "r9", "r10", "r11", "cc", "memory");
    dcache_clean(dst + y0 * LCD_W * 2u, (y1 - y0) * LCD_W * 2u);
}

/* tribuf.h 的搬运回调: 整行区间 (地址/长度都是 1600 B 的倍数) 走 copy_rows 的 ldm/stm; 其它 (跨缓冲 COPY 的整段) 逐字 */
static int tb_mover(uintptr_t dst, uintptr_t src, uint32_t n, void *ctx){
    (void)ctx;
    if ((n & 31u) == 0 && ((dst | src) & 31u) == 0){
        uint32_t d = (uint32_t)dst, s = (uint32_t)src, k = n / 32u;
        __asm__ volatile(
            "1: ldmia %1!, {r4-r11}\n"
            "   stmia %0!, {r4-r11}\n"
            "   subs %2, %2, #1\n"
            "   bne 1b\n"
            : "+r"(d), "+r"(s), "+r"(k) :: "r4", "r5", "r6", "r7", "r8", "r9", "r10", "r11", "cc", "memory");
        return 0;
    }
    return 1;                                          /* blit.h 用逐字 CPU 拷 */
}

/* 等帧事件 (W1C 位, 每次写基址后清过; 置位 = 写进去的基址已在帧头锁存)。60 ms 兜底。返回 1 = 等到 */
static int tb_wait_evt(void *ctx){
    (void)ctx;
    uint32_t t0 = now_us();
    while (!(rd32(LCDC_EVT) & LCDC_EVT_FRAME) && now_us() - t0 < 60000u) {}
    g_flip_wait_us += now_us() - t0;
    return (rd32(LCDC_EVT) & LCDC_EVT_FRAME) ? 1 : 0;
}

static void tb_poll(void){ if (g_tb.p >= 0 && (rd32(LCDC_EVT) & LCDC_EVT_FRAME)) tb_latched(&g_tb); }

static uint32_t fb_alloc(uint32_t *raw){
    void *p = malloc(FB_SIZE + 256u);
    if (!p) return 0;
    *raw = (uint32_t)p;
    return ((uint32_t)p + 255u) & ~255u;
}

static void tb_setup(uint32_t n, uint32_t front){
    uintptr_t b[3] = { front, 0, 0 }; uint32_t k = 1;
    const uint32_t all[3] = { FB_ADDR, g_fb2, g_fb3 };
    for (uint32_t i = 0; i < 3 && k < n; i++) if (all[i] && all[i] != front) b[k++] = all[i];
    tb_init(&g_tb, b, k, LCD_W, LCD_H);
    tb_reset_to(&g_tb, 0);                             /* 其余块内容未知: 第一次用时整屏同步 */
}

static void dbuf_init(void){
    g_tri_note = 3;
    g_fb2 = fb_alloc(&g_fb2_raw);
    if (g_fb2){
        g_nbuf = 2;
        g_fb3 = fb_alloc(&g_fb3_raw);
        if (!g_fb3) g_tri_note = 1;
        else {
            void *probe = malloc(TRI_RESERVE);
            if (probe){ free(probe); g_nbuf = 3; g_tri_note = 0; }
            else { free((void *)g_fb3_raw); g_fb3 = g_fb3_raw = 0; g_tri_note = 2; }
        }
    }
    tb_setup(g_nbuf, FB_ADDR);
}

/* d 上写过的行 clean 掉 (PRESENT 前; 单缓冲时主循环兜底也调) */
static void flush_dirty(void){
    if (g_tb.y1 <= g_tb.y0) return;
    dcache_clean(draw_target() + g_tb.y0 * LCD_W * 2u, (g_tb.y1 - g_tb.y0) * LCD_W * 2u);
    if (g_tb.n == 1){ g_tb.y0 = LCD_H; g_tb.y1 = 0; }  /* 多缓冲时 y0/y1 留给 tb_present 记账 */
}

/* 翻页时机。1 (默认, v4 起): 不额外等帧事件就写基址 (帧头锁存, 不撕裂)。0: 先等一次帧事件再写 (v1..v3 的老行为)。
 * 运行时用 DIAG op 11 切换。三缓冲下只要还有待锁存的就一定先等 (tribuf.h), 所以上限 37.5 fps。 */
static uint32_t g_flip_now = 1;

/* 写操作之前: 选好要画的块并抄齐 (多缓冲); 单缓冲什么都不用做 */
static void draw_begin(void){ if (g_tb.n > 1){ tb_poll(); tb_begin(&g_tb, tb_wait_evt, 0, tb_mover, 0); } }

static void present(void){
    if (g_tb.n == 1){ flush_dirty(); return; }
    if (g_tb.d < 0 || g_tb.y1 <= g_tb.y0) return;
    flush_dirty();
    g_flip_wait_us = 0;
    tb_poll();
    if (tb_need_wait_before_present(&g_tb)){ tb_wait_evt(0); tb_latched(&g_tb); }   /* 超时也当已锁存 (只在帧事件坏掉时) */
    else if (!g_flip_now) lcd_wait_frame(LCDC_EVT_FRAME, 60000u);
    int d = tb_present(&g_tb);
    if (d < 0) return;
    ql_mipi_lcd_write_screen((void *)tb_addr(d));
    wr32(LCDC_EVT, LCDC_EVT_FRAME);                    /* 之后再置位 = 新基址已经过了一个帧头 */
}

/* COPY: 翻页后这一批的第一条写操作是 COPY 时跨缓冲做 (源 = 最新已提交的那块, link/tribuf.h + blit.h); 否则原地 memmove */
static int do_copy(const uint8_t *pl, uint32_t n){
    bk_copy_t c;
    if (!bk_copy_parse(pl, n, LCD_W, LCD_H, &c)) return LK_EPARAM;
    if (g_tb.n > 1){
        tb_poll();
        if (tb_copy(&g_tb, &c, tb_wait_evt, 0, tb_mover, 0)) return LK_OK;
    }
    return (int)lk_copy_apply((uint16_t *)draw_target(), LCD_W, LCD_H, pl, n, &g_tb.y0, &g_tb.y1);
}

/* 旧 DIAG 之前: 退回"双缓冲视角" —— 没有待锁存、扫描的是 FB_ADDR 或 g_fb2、另一块和它内容一致。只在诊断时用, 慢点没关系 */
static void tri_collapse(void){
    if (g_tb.n == 1){ flush_dirty(); g_front = tb_addr(g_tb.s); g_back = 0; return; }
    if (g_tb.d >= 0){ g_tb.ver[g_tb.d] = 0; g_tb.d = -1; g_tb.y0 = LCD_H; g_tb.y1 = 0; }   /* 没提交的改动作废 */
    tb_poll();
    if (g_tb.p >= 0){ tb_wait_evt(0); tb_latched(&g_tb); }
    uint32_t s = tb_addr(g_tb.s);
    if (s != FB_ADDR && s != g_fb2){                   /* 扫描的是第三块: 抄到 FB_ADDR 再翻过去 */
        copy_rows(FB_ADDR, s, 0, LCD_H);
        ql_mipi_lcd_write_screen((void *)FB_ADDR);
        wr32(LCDC_EVT, LCDC_EVT_FRAME);
        tb_wait_evt(0);
        s = FB_ADDR;
    }
    g_front = s;
    g_back = s == FB_ADDR ? g_fb2 : FB_ADDR;
    copy_rows(g_back, g_front, 0, LCD_H);
}

/* 旧 DIAG 之后: 按它留下的 g_front / g_back 重建 (g_back = 0 表示它把双缓冲关了 -> 单缓冲) */
static uint32_t g_nactive = 0;                         /* DIAG 32 指定的块数; 0 = 能用几块用几块 */
static void tri_uncollapse(void){
    uint32_t n = g_back ? (g_nactive ? g_nactive : g_nbuf) : 1u;
    if (n > g_nbuf) n = g_nbuf;
    tb_setup(n, g_front);
}

/* ---- 开机版权页 (fw v7, lcd/bootscreen.h) ----
 * 画在正在扫描的那块 (s) 上, clean 后 tb_reset_to(s): 其余块版本作废, 第一次被选来画时从 s 整屏同步。
 * g_boot_live: 开机画面还原样在屏上 (没收到过 RECT / COPY / DIAG)。只有这时 HELLO 才去改 Link 行。 */
#include "bootscreen.h"
static int g_boot_live;
static int g_seg_ok;                   /* 开机时段码屏 GPIO 初始化结果 (开机页上显示; 主机走了重画时照用) */

static void boot_draw(int seg_ok){
    uint32_t q[8] = {0};
    char date[11];
    bs_date(__DATE__, date);
    bs_info_t in = { FW_VER, date, 0xFFFFFFFFu, g_tb.n, seg_ok, 0 };
    if (ql_dev_memory_size_query(q) == 0) in.free_kib = q[1] >> 10;     /* {total, avail, max_block}, 见 docs/r05-notes.md */
    uint32_t fb = tb_addr(g_tb.s);
    bs_draw((uint16_t *)fb, &in);
    dcache_clean(fb, FB_SIZE);
    tb_reset_to(&g_tb, g_tb.s);
    g_boot_live = 1;
}

static void boot_link_ok(void){
    if (!g_boot_live) return;
    g_boot_live = 0;
    uint32_t fb = tb_addr(g_tb.s), y0, y1;
    bs_draw_link((uint16_t *)fb, 1, &y0, &y1);
    dcache_clean(fb + y0 * LCD_W * 2u, (y1 - y0) * LCD_W * 2u);
    tb_reset_to(&g_tb, g_tb.s);
}

static void reply(uint8_t type, const uint8_t *pl, uint32_t n);
#include "diag.h"

/* ---- 回包 ---- */
/* 回包缓冲按最大的 DIAG_R 算: 4 + 16 个字 = 68 B。v3 及之前只有 48 B (v4 修), 13 个字以上的 DIAG 回包 (PEEK 16 字、
 * EVT 15 轮、PHASE 5 轮、ZC_LINE 15 点) 会写穿栈上的返回地址 -> 设备复位。这就是早先"读 0x08807040 起复位"
 * "W1C 0x40/0x80 复位"和 E0 复位的共同原因 (推断: 三次都是 ≥13 字的回包)。 */
#define REPLY_MAX 68u
static void reply(uint8_t type, const uint8_t *pl, uint32_t n){
    uint8_t out[LK_HDR + REPLY_MAX + LK_TRAIL];
    if (n > REPLY_MAX) n = REPLY_MAX;
    uint32_t len = lk_encode(out, type, g_p.seq, pl, n);
    ql_uart_write(PORT, out, len);
}

/* ---- 按键 / 状态灯 (fw v8) ---- */
static lw_t     g_lw;                  /* 收到过 HELLO 且主机还在 (g_lw.up), link/linkwd.h */
static uint32_t g_keys_ok, g_led_ok;   /* 硬件初始化成功 -> caps 位 */
static keys_t   g_keys;
static pwr_t    g_pwr;
static uint32_t g_key_n, g_key_at;
static led_t    g_led;
static led_t    g_led_host;            /* 主机走了那一刻主机设的灯, 回来时恢复 */

static void led_poll(void){ if (g_led_ok) led_hw_apply(led_step(&g_led, now_ms())); }

/* ---- 沉浸模式 (fw v9, link/imm.h) ---- */
#define IMM_MAX_ROWS 4096u                 /* 卷轴最多这么多行 (6.5 MB); 实际按堆里能拿到的 */
#define ROW_B (LCD_W * 2u)
static int      g_lcd_ok;
static int      g_imm;                     /* 在沉浸模式里 */
static im_t     g_im;
static uint32_t g_roll, g_roll_raw, g_roll_rows;

static uint32_t im_addr(int buf, uint32_t row){ return (buf == IM_BUF_ROLL ? g_roll : FB_ADDR) + row * ROW_B; }
static void im_io_copy(void *c, int db, uint32_t dr, int sb, uint32_t sr, uint32_t n){
    (void)c;
    uint32_t d = im_addr(db, dr);
    move32(d, im_addr(sb, sr), n * ROW_B);             /* 1600 B/行 = 50 × 32 B, 基址 256 B 对齐 */
    dcache_clean(d, n * ROW_B);
}
static void im_io_base(void *c, int buf, uint32_t row){
    (void)c;
    if (buf == IM_BUF_ROLL ? row + LCD_H > g_roll_rows : row != 0) return;   /* 保底: 整帧出了缓冲就花屏, 宁可不动 */
    ql_mipi_lcd_write_screen((void *)im_addr(buf, row));
    wr32(LCDC_EVT, LCDC_EVT_FRAME);                    /* 之后再置位 = 新基址已锁存 */
}
static void im_io_wait(void *c){ (void)c; tb_wait_evt(0); }
static const im_io_t g_imio = { im_io_copy, im_io_base, im_io_wait, 0 };

/* 主循环每轮: 锁存 / 下一帧的基址 / 停稳了就换底 */
static void imm_poll(void){
    if (!g_imm) return;
    uint32_t t = now_ms();
    int32_t r = im_tick(&g_im, t, (rd32(LCDC_EVT) & LCDC_EVT_FRAME) != 0);
    if (r >= 0) im_io_base(0, IM_BUF_ROLL, (uint32_t)r);
    else if (im_should_rebase(&g_im, t)) im_rebase(&g_im, &g_imio);
}

/* 多缓冲收拢到原厂 fb 上: 没有待锁存的、扫描的就是 FB_ADDR、内容是最新提交的那帧; 然后还掉第二、三块 */
static void imm_release_fbs(void){
    if (g_tb.n > 1){
        if (g_tb.d >= 0){ g_tb.ver[g_tb.d] = 0; g_tb.d = -1; g_tb.y0 = LCD_H; g_tb.y1 = 0; }   /* 没提交的改动作废 */
        tb_poll();
        if (g_tb.p >= 0){ tb_wait_evt(0); tb_latched(&g_tb); }
        uint32_t s = tb_addr(g_tb.s);
        if (s != FB_ADDR){
            copy_rows(FB_ADDR, s, 0, LCD_H);
            ql_mipi_lcd_write_screen((void *)FB_ADDR);
            wr32(LCDC_EVT, LCDC_EVT_FRAME);
            tb_wait_evt(0);
        }
    } else flush_dirty();
    if (g_fb3_raw) free((void *)g_fb3_raw);
    if (g_fb2_raw) free((void *)g_fb2_raw);
    g_fb2 = g_fb2_raw = g_fb3 = g_fb3_raw = 0;
    g_nbuf = 1;
    tb_setup(1, FB_ADDR);
}

/* 卷轴: 先二分出最大可 malloc 的块, 再从它往下试, 拿到后堆里还要剩 TRI_RESERVE。返回行数, 0 = 拿不到够用的 */
static uint32_t imm_alloc(void){
    uint32_t lo = 0, hi = 8u << 20;
    while (hi - lo > 65536u){
        uint32_t mid = lo + ((hi - lo) >> 1);
        void *p = malloc(mid);
        if (p){ free(p); lo = mid; } else hi = mid;
    }
    uint32_t rows = lo > 256u ? udiv(lo - 256u, ROW_B, 0) : 0;
    if (rows > IMM_MAX_ROWS) rows = IMM_MAX_ROWS;
    for (; rows >= LCD_H + IM_MIN_AHEAD; rows -= 80u){               /* 80 行 = 125 KiB 一档 */
        void *p = malloc(rows * ROW_B + 256u);
        if (!p) continue;
        void *probe = malloc(TRI_RESERVE);
        if (probe){
            free(probe);
            g_roll_raw = (uint32_t)p; g_roll = ((uint32_t)p + 255u) & ~255u; g_roll_rows = rows;
            return rows;
        }
        free(p);
    }
    return 0;
}

static void imm_exit(void){
    if (!g_imm) return;
    im_hold(&g_im, &g_imio, now_ms());                 /* 屏上留着最后一帧 (原厂 fb), 卷轴不再被扫 */
    free((void *)g_roll_raw);
    g_roll = g_roll_raw = g_roll_rows = 0;
    g_imm = 0;
    dbuf_init();                                       /* 重新拿第二、三块; 拿不到就少几块 */
}

/* IMM_ENTER: 返回 ACK status, *rows = 卷轴行数 */
static uint8_t imm_enter(uint32_t y0, uint32_t *rows){
    *rows = 0;
    if (!g_lcd_ok) return LK_EUNSUP;
    if (g_imm){                                        /* 重新开始 (换歌 / 往回跳) */
        im_hold(&g_im, &g_imio, now_ms());
        im_restart(&g_im, y0);
        *rows = g_roll_rows;
        return LK_OK;
    }
    g_boot_live = 0;
    imm_release_fbs();
    if (!imm_alloc() || im_init(&g_im, g_roll_rows, LCD_H, y0)){
        if (g_roll_raw) free((void *)g_roll_raw);
        g_roll = g_roll_raw = g_roll_rows = 0;
        dbuf_init();                                   /* 不够: 原样恢复多缓冲, 主机留在普通页面 */
        return LK_EUNSUP;
    }
    g_imm = 1;
    *rows = g_roll_rows;
    return LK_OK;
}

/* 卷轴不够时: 等掉待锁存的, 能换底就换 (IMM_RECT / IMM_SCROLL 共用) */
static void imm_make_room(void){
    im_settle(&g_im, &g_imio, now_ms());
    if (im_can_rebase(&g_im)) im_rebase(&g_im, &g_imio);
}

/* DIAG 31 (DG_TRI): -> n nbuf note s p d gver waits xcopies syncs full_syncs buf0 buf1 buf2 flip_wait_us */
static void diag_tri(void){
    uint32_t w[15] = { g_tb.n, g_nbuf, g_tri_note, (uint32_t)g_tb.s, (uint32_t)g_tb.p, (uint32_t)g_tb.d, g_tb.gver,
                       g_tb.waits, g_tb.xcopies, g_tb.syncs, g_tb.full_syncs, tb_addr(0), tb_addr(1), tb_addr(2), g_flip_wait_us };
    diag_reply(31, 0, w, 15);
}

/* DIAG 32 (DG_NBUF): u8 n (1..3, 0 = 能用几块用几块) -> 生效块数。A/B 对比三缓冲/双缓冲 */
static void diag_nbuf(uint8_t v){
    tri_collapse();
    g_nactive = v > 3 ? 3u : v;
    if (g_nactive == 1) g_back = 0;
    else if (!g_back) g_back = g_front == FB_ADDR ? g_fb2 : FB_ADDR;
    if (g_back) copy_rows(g_back, g_front, 0, LCD_H);
    tri_uncollapse();
    uint32_t w = g_tb.n;
    diag_reply(32, 0, &w, 1);
}

/* ---- 主机走了 / 休眠 (fw v10), dispatch.h 用 ---- */
/* 开机版权页重画到屏上: 先退出沉浸模式 (还回多缓冲), 没提交的改动作废、等待锁存的落定, 再画在正在扫描的那块上 */
static void boot_redraw(void){
    if (!g_lcd_ok) return;
    imm_exit();
    if (g_tb.n > 1){
        if (g_tb.d >= 0){ g_tb.ver[g_tb.d] = 0; g_tb.d = -1; g_tb.y0 = LCD_H; g_tb.y1 = 0; }
        tb_poll();
        if (g_tb.p >= 0){ tb_wait_evt(0); tb_latched(&g_tb); }
    } else flush_dirty();
    boot_draw(g_seg_ok);
}

/* 背光 (GPIO7) + 段码屏 EN (GPIO10, 高 = 亮)。醒来时段码屏整个重新初始化 (EN 拉低期间控制器状态是否保持没证实; 照开机走一遍最稳,
 * 约 0.4 s, 会闪一下 888888), 之后调用方重画。大屏面板本身不动 (LCDC 照常扫描), 只关背光。 */
static void panel_power(uint32_t on){
    if (!on){ if (g_lcd_ok) lcd_backlight(0); fg_off(); return; }
    fg_init();
    if (g_lcd_ok) lcd_backlight(1);
}

/* USB 插拔回调 (内核 VBUS / 充电器通知里调, 线程未证实): 只计数。state 0 = 拔掉。原厂 app 也注册了一个 (只打日志) */
static volatile uint32_t g_usb_out_n;
static uint32_t g_usb_out_seen;
static int usb_hotplug_cb(uint32_t state, void *ctx){ (void)ctx; if (state == R05_USB_HOTPLUG_OUT) g_usb_out_n++; return 0; }
static int usb_unplugged(void){
    uint32_t n = g_usb_out_n;
    if (n == g_usb_out_seen) return 0;
    g_usb_out_seen = n;
    return 1;
}

#include "dispatch.h"   /* on_frame / ack / KEY 上报 / 主机在不在 (和 tools/fwsim 共用) */

static void uart_cb(uint32_t ind, uint32_t port, uint32_t size){
    (void)size;
    if (port == PORT && (ind == R05_UART_IND_RX || ind == R05_UART_IND_RX2)) ql_rtos_semaphore_release(g_sem);
}

static void show_err(int code){
    unsigned stage = (unsigned)code >> 16, ret = (unsigned)code & 0xff;
    fg_fill(0);
    fg_digit(0, SEG_E); fg_digit(3, fg_hex[stage & 0xF]);
    fg_digit(4, fg_hex[(ret >> 4) & 0xF]); fg_digit(5, fg_hex[ret & 0xF]);
    fg_flush();
}

static void task_entry(void *arg){
    (void)arg;
    int seg_err = fg_init();                           /* 888888 */
    g_seg_ok = seg_err == 0;
    int lcd_err = lcd_power_on();
    if (lcd_err == 0){
        g_lcd_ok = 1;
        dbuf_init();
        boot_draw(g_seg_ok);
        ql_rtos_task_sleep_ms(200);
        lcd_backlight(1);
        seg_draw_local();
    } else {
        show_err(lcd_err);                             /* 大屏起不来也照样开 USB, 方便查 */
    }

    g_led_ok = led_hw_init() == 0;
    led_init(&g_led); led_default(); led_poll();
    keys_init(&g_keys); pwr_init(&g_pwr); lw_init(&g_lw, now_ms());

    lk_init(&g_p, g_pl, MAX_PAYLOAD);
    ql_rtos_semaphore_create(&g_sem, 0);
    ql_uart_open(PORT);
    ql_uart_register_cb(PORT, uart_cb);
    ql_usb_bind_hotplug_cb(usb_hotplug_cb);            /* 失败也无妨: 照样靠 5 s 超时 */
    g_keys_ok = (keys_hw_init() & 7u) == 0;            /* PWRKEY 注册失败只少键 1, 其余照用 */

    uint32_t last_rx = now_us(), last_wdt = last_rx;
    for (;;){
        ql_rtos_semaphore_wait(g_sem, 10);            /* 10 ms: 按键采样节拍 (v7 是 20) */
        for (;;){
            int r = ql_uart_read(PORT, g_rx, RX_CHUNK);
            if (r <= 0) break;
            uint32_t n = (uint32_t)r; const uint8_t *d = g_rx;
            link_rx(d, n);
            last_rx = now_us();
            keys_poll();                               /* 整帧连续推 ≈ 300 ms 时也照常采样 */
            imm_poll();                                /* 主机连续写卷轴时滚动也照走 */
        }
        uint32_t t = now_us();
        tb_poll();                                     /* 待锁存的过了帧头 -> 记成正在扫描 */
        if (g_tb.n == 1 && t - last_rx > 50000u) flush_dirty();               /* 主机没发 PRESENT 也别让像素一直卡在 cache 里 */
        if (!lk_idle(&g_p) && t - last_rx > 500000u) lk_reset(&g_p);   /* 半截帧: 丢掉重新找同步字 */
        if (t - last_wdt > 1000000u){ ql_dev_feed_wdt(); last_wdt = t; }
        if (lcd_err == 0) clk_tick(t);
        link_lost_poll();                              /* 主机走了: 开机画面 + 心跳; 太久没主机: 休眠 */
        keys_poll();
        led_poll();
        imm_poll();
    }
}

int appimg_enter(void *arg){
    (void)arg;
    ql_dev_cfg_wdt(1);                                 /* 同原厂 enter; 任务里每秒喂一次 */
    ql_rtos_task_create(&g_task, 8192, APP_PRIORITY_NORMAL, "monita", task_entry, 0, 1);
    return 0;
}

void appimg_exit(void){ }
