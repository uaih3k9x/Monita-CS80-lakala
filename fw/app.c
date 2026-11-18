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
 *   未连接: 蓝灯 2 s 亮 1 s 灭 (同原厂常态心跳); HELLO 后熄灭, 交给主机 LED (0x31);
 *   HELLO 之后 15 s 收不到任何字节 = 主机走了 (link/linkwd.h), 回到蓝灯心跳, 也不再发 KEY;
 *   (v9) 之后又收到字节就当主机回来了: 恢复 KEY 上报, 灯恢复成主机最后设的样子 (主机睡醒后不一定重发 HELLO)。
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

#define FW_VER       0x0009u          /* 9: 主机不经 HELLO 回来时恢复 KEY/灯 (link/linkwd.h), PWRKEY 慢采样不丢轻点; 8: 按键 KEY + 状态灯 LED; 7: 开机版权页 (lcd/bootscreen.h); 6: 三缓冲 (link/tribuf.h), DMA 研究代码已移除 (DIAG 26–30); 4: 跨缓冲 COPY, 默认立即翻页 */
#define MAX_PAYLOAD  32768u          /* 800 x 20 行 RGB565 + 8 字节头 = 32008, 放得下一条整行带 */
#define RX_CHUNK     16384u
#define PORT         R05_PORT_USB_MODEM
#define SEG_HOST_HOLD_US 5000000u    /* 主机发 SEG 后这么久内固件不画自己的段码 */
#define KEY_SAMPLE_MS 8u             /* 按键最短采样间隔 (1.024 ms 单位); 主循环空闲时 10 ms 醒一次 */

static uint8_t  g_pl[MAX_PAYLOAD] __attribute__((aligned(4)));
static uint8_t  g_rx[RX_CHUNK] __attribute__((aligned(4)));
static uint8_t  g_tx[64];
static void    *g_sem;
static void    *g_task;
static lk_parser_t g_p;

static struct {
    uint32_t rx_bytes, rects, presents, bad_param;
} st;

static uint32_t now_us(void){ return (uint32_t)osiUpTimeUS(); }   /* 只用来做差, 71 分钟回绕无所谓 */
static uint32_t now_ms(void){ return (uint32_t)(osiUpTimeUS() >> 10); }   /* 1.024 ms 单位 (同 ACK.uptime), 按键/状态灯用 */

/* Cortex-A5 没有硬件除法, 不链接 libgcc: 自己做 32 位无符号除法 (只在每秒刷时钟时用) */
static uint32_t udiv(uint32_t n, uint32_t d, uint32_t *rem){
    uint32_t q = 0, r = 0;
    for (int i = 31; i >= 0; i--){
        r = (r << 1) | ((n >> i) & 1u);
        if (r >= d){ r -= d; q |= 1u << i; }
    }
    if (rem) *rem = r;
    return q;
}

/* ---- 段码屏: 本地时钟 ---- */
static int      clk_valid;
static uint32_t clk_sod;              /* 当地时间, 一天中的第几秒 */
static uint32_t clk_next_us;
static uint32_t seg_host_at; static int seg_host_seen;

static void seg_draw_local(void){
    fg_fill(0);
    for (int i = 0; i < 6; i++) fg_digit(FG_COL_MAIN(i), SEG_DASH);
    if (clk_valid){
        uint32_t s, m, h = udiv(clk_sod, 3600u, &s);
        m = udiv(s, 60u, &s);
        uint32_t v[3] = { h, m, s };
        for (int i = 0; i < 3; i++){
            uint32_t lo, hi = udiv(v[i], 10u, &lo);
            fg_digit(FG_COL_CLK(2 * i), fg_hex[hi]);
            fg_digit(FG_COL_CLK(2 * i + 1), fg_hex[lo]);
        }
        fg_seg(FG_SEG_CLK_COLON_A, 1); fg_seg(FG_SEG_CLK_COLON_B, 1);
    }
    fg_flush();
}

static int seg_host_active(uint32_t t){ return seg_host_seen && (t - seg_host_at) < SEG_HOST_HOLD_US; }

static void clk_set(uint32_t unix_s, int32_t tz_min){
    uint32_t sod;
    udiv((uint32_t)((int32_t)unix_s + tz_min * 60), 86400u, &sod);
    clk_sod = sod; clk_valid = 1; clk_next_us = now_us() + 1000000u;
}

static void clk_tick(uint32_t t){
    if (!clk_valid || (int32_t)(t - clk_next_us) < 0) return;
    while ((int32_t)(t - clk_next_us) >= 0){
        clk_next_us += 1000000u;
        if (++clk_sod == 86400u) clk_sod = 0;
    }
    if (!seg_host_active(t)) seg_draw_local();
}

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
/* 回包缓冲按最大的 DIAG_R 算: 4 + 16 个字 = 68 B。v3 之前只有 48 B, 13 个字以上的 DIAG 回包 (PEEK 16 字、
 * EVT 15 轮、PHASE 5 轮、ZC_LINE 15 点) 会写穿栈上的返回地址 -> 设备复位。这就是早先"读 0x08807040 起复位"
 * "W1C 0x40/0x80 复位"和 E0 复位的共同原因 (推断: 三次都是 ≥13 字的回包)。 */
#define REPLY_MAX 68u
static void reply(uint8_t type, const uint8_t *pl, uint32_t n){
    uint8_t out[LK_HDR + REPLY_MAX + LK_TRAIL];
    if (n > REPLY_MAX) n = REPLY_MAX;
    uint32_t len = lk_encode(out, type, g_p.seq, pl, n);
    ql_uart_write(PORT, out, len);
}

/* ACK: u8 for_type u8 status u16 0 | u32 rx_bytes frames crc_err len_err rects presents bad_param uptime_ms | u32 extra */
static void ack(uint8_t status, uint32_t extra){
    uint8_t *b = g_tx;
    b[0] = g_p.type; b[1] = status; b[2] = 0; b[3] = 0;
    uint32_t v[9] = { st.rx_bytes, g_p.frames, g_p.crc_err, g_p.len_err, st.rects, st.presents, st.bad_param,
                      (uint32_t)(osiUpTimeUS() >> 10), extra };     /* uptime 以 1.024 ms 为单位 (免 64 位除法) */
    for (int i = 0; i < 9; i++) lk_wr32(b + 4 + 4 * i, v[i]);
    reply(LK_ACK, b, 40);
}

/* ---- 按键 / 状态灯 (fw v8) ---- */
static lw_t     g_lw;                  /* 收到过 HELLO 且主机还在 (g_lw.up), link/linkwd.h */
#define g_link  (g_lw.up)
static uint32_t g_keys_ok, g_led_ok;   /* 硬件初始化成功 -> caps 位 */
static keys_t   g_keys;
static pwr_t    g_pwr;
static uint32_t g_key_n, g_key_at;
static led_t    g_led;
static led_t    g_led_host;            /* 主机走了那一刻主机设的灯, 回来时恢复 */

static void led_default(void){ led_blink(&g_led, LED_B, 3000u, 2000u, now_ms()); }   /* 未连接: 蓝灯 2 s 亮 1 s 灭 */

static void keys_poll(void){
    if (!g_keys_ok) return;
    uint32_t t = now_ms();
    if (t - g_key_at < KEY_SAMPLE_MS) return;
    g_key_at = t;
    uint8_t ev[KEY_N * 2];
    int n = keys_feed(&g_keys, t, keys_hw_raw(&g_pwr, t), ev, (int)sizeof ev);
    for (int i = 0; i < n && g_link; i++){                 /* 没连主机: 照样走状态机, 只是不发 */
        uint8_t pl[KEY_PL_LEN], out[LK_HDR + KEY_PL_LEN + LK_TRAIL];
        keys_payload(pl, ev[i], g_key_n++, t);
        ql_uart_write(PORT, out, lk_encode(out, LK_KEY, 0, pl, KEY_PL_LEN));
    }
}

static void led_poll(void){ if (g_led_ok) led_hw_apply(led_step(&g_led, now_ms())); }

static void on_frame(void){
    const uint8_t *pl = g_pl; uint32_t n = g_p.len;
    if (g_p.type == LK_RECT || g_p.type == LK_COPY || g_p.type == LK_DIAG) g_boot_live = 0;   /* 主机开始画了 / 诊断可能动缓冲 */
    switch (g_p.type){
    case LK_RECT:
        draw_begin();
        if (lk_rect_apply((uint16_t *)draw_target(), LCD_W, LCD_H, pl, n, &g_tb.y0, &g_tb.y1) == LK_OK) st.rects++;
        else st.bad_param++;
        return;                                        /* RECT 不应答, 保持流式 */
    case LK_COPY:
        if (do_copy(pl, n) == LK_OK) st.rects++;
        else st.bad_param++;
        return;                                        /* 同 RECT: 不应答, 计入 rects / bad_param */
    case LK_PRESENT: {
        uint32_t t0 = now_us();
        present();
        st.presents++;
        ack(LK_OK, now_us() - t0);
        return;
    }
    case LK_HELLO: {
        st.rx_bytes = 0; st.rects = 0; st.presents = 0; st.bad_param = 0;
        g_p.frames = 1; g_p.crc_err = 0; g_p.len_err = 0;      /* 计数从这条 HELLO 重新算 */
        if (n >= 6) clk_set((uint32_t)lk_rd16(pl) | ((uint32_t)lk_rd16(pl + 2) << 16), (int16_t)lk_rd16(pl + 4));
        boot_link_ok();                                /* 开机画面还在: Link 行 WAIT -> OK */
        lw_hello(&g_lw, now_ms()); g_key_n = 0;        /* KEY 计数从这条 HELLO 重新算 */
        led_off(&g_led); led_poll();                   /* 灯交给主机 */
        uint8_t *b = g_tx;
        lk_wr16(b + 0, LK_PROTO_VER); lk_wr16(b + 2, FW_VER);
        lk_wr16(b + 4, LCD_W); lk_wr16(b + 6, LCD_H);
        b[8] = LK_PIXFMT_RGB565; b[9] = 72;
        lk_wr16(b + 10, LK_CAP_FBCRC | LK_CAP_COPY | (g_tb.n > 1 ? LK_CAP_DOUBLEBUF : 0u) | (g_tb.n > 2 ? LK_CAP_TRIBUF : 0u)
                         | (g_keys_ok ? LK_CAP_KEYS : 0u) | (g_led_ok ? LK_CAP_LED : 0u)); lk_wr16(b + 12, MAX_PAYLOAD); lk_wr16(b + 14, 0);
        reply(LK_HELLO_ACK, b, 16);
        if (!seg_host_active(now_us())) seg_draw_local();
        return;
    }
    case LK_SEG:
        if (n != 72){ ack(LK_EPARAM, 0); return; }
        for (int i = 0; i < 72; i++) fg_fb[i] = pl[i];
        fg_flush();
        seg_host_at = now_us(); seg_host_seen = 1;
        ack(LK_OK, 0);
        return;
    case LK_BACKLIGHT:
        if (n != 1){ ack(LK_EPARAM, 0); return; }
        lcd_backlight(pl[0]);
        ack(LK_OK, 0);
        return;
    case LK_LED:
        if (!g_led_ok){ ack(LK_EUNSUP, 0); return; }
        ack(led_cmd(&g_led, pl, n, now_ms()) ? LK_EPARAM : LK_OK, 0);
        led_poll();
        return;
    case LK_FBCRC: {
        uint32_t c = 0;                                /* 校验的是最新提交的那块 (已显示, 或下一个帧头显示) */
        if (g_tb.n == 1) flush_dirty();
        uint32_t s = lk_fb_crc((const uint16_t *)tb_addr(tb_latest(&g_tb)), LCD_W, LCD_H, pl, n, &c);
        ack((uint8_t)s, c);
        return;
    }
    case LK_DIAG:
        if (n >= 1 && pl[0] == 31){                    /* DG_TRI: -> n nbuf note s p d gver waits xcopies syncs full_syncs buf0 buf1 buf2 flip_wait_us */
            uint32_t w[15] = { g_tb.n, g_nbuf, g_tri_note, (uint32_t)g_tb.s, (uint32_t)g_tb.p, (uint32_t)g_tb.d, g_tb.gver,
                               g_tb.waits, g_tb.xcopies, g_tb.syncs, g_tb.full_syncs, tb_addr(0), tb_addr(1), tb_addr(2), g_flip_wait_us };
            diag_reply(31, 0, w, 15);
            return;
        }
        if (n == 2 && pl[0] == 32){                    /* DG_NBUF: u8 n (1..3, 0 = 能用几块用几块) -> 生效块数。A/B 对比三缓冲/双缓冲 */
            tri_collapse();
            g_nactive = pl[1] > 3 ? 3u : pl[1];
            if (g_nactive == 1) g_back = 0;
            else if (!g_back) g_back = g_front == FB_ADDR ? g_fb2 : FB_ADDR;
            if (g_back) copy_rows(g_back, g_front, 0, LCD_H);
            tri_uncollapse();
            uint32_t w = g_tb.n;
            diag_reply(32, 0, &w, 1);
            return;
        }
        if (n == 2 && pl[0] == 11){                    /* DG_FLIPMODE: u8 mode -> 当前 mode */
            g_flip_now = pl[1] ? 1u : 0u;
            uint32_t w = g_flip_now;
            diag_reply(11, 0, &w, 1);
            return;
        }
        {   /* 旧 DIAG: 只读的几个 (堆 / 最大块 / PEEK / 帧事件计时 / 相位) 直接跑, 其余先退回双缓冲视角 */
            uint8_t op = pl[0];
            int ro = op == 0 || op == 1 || op == 4 || op == 7 || op == 9 || op == 21 || op == 23;
            if (!ro) tri_collapse();
            on_diag(pl, n);
            if (!ro) tri_uncollapse();
        }
        return;
    case LK_PING:
        ack(LK_OK, 0);
        return;
    default:
        ack(LK_EUNSUP, 0);
        return;
    }
}

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
    int lcd_err = lcd_power_on();
    if (lcd_err == 0){
        dbuf_init();
        boot_draw(seg_err == 0);
        ql_rtos_task_sleep_ms(200);
        lcd_backlight(1);
        seg_draw_local();
    } else {
        show_err(lcd_err);                             /* 大屏起不来也照样开 USB, 方便查 */
    }

    g_led_ok = led_hw_init() == 0;
    led_init(&g_led); led_default(); led_poll();
    keys_init(&g_keys); pwr_init(&g_pwr); lw_init(&g_lw);

    lk_init(&g_p, g_pl, MAX_PAYLOAD);
    ql_rtos_semaphore_create(&g_sem, 0);
    ql_uart_open(PORT);
    ql_uart_register_cb(PORT, uart_cb);
    g_keys_ok = (keys_hw_init() & 7u) == 0;            /* PWRKEY 注册失败只少键 1, 其余照用 */

    uint32_t last_rx = now_us(), last_wdt = last_rx;
    for (;;){
        ql_rtos_semaphore_wait(g_sem, 10);            /* 10 ms: 按键采样节拍 (v7 是 20) */
        for (;;){
            int r = ql_uart_read(PORT, g_rx, RX_CHUNK);
            if (r <= 0) break;
            uint32_t n = (uint32_t)r; const uint8_t *d = g_rx;
            st.rx_bytes += n;
            if (lw_rx(&g_lw, now_ms()) == LW_RESUMED){ g_led = g_led_host; led_poll(); }   /* 主机回来了 (没重发 HELLO) */
            while (n){
                int ev; uint32_t k = lk_feed(&g_p, d, n, &ev);
                d += k; n -= k;
                if (ev) on_frame();
            }
            last_rx = now_us();
            keys_poll();                               /* 整帧连续推 ≈ 300 ms 时也照常采样 */
        }
        uint32_t t = now_us();
        tb_poll();                                     /* 待锁存的过了帧头 -> 记成正在扫描 */
        if (g_tb.n == 1 && t - last_rx > 50000u) flush_dirty();               /* 主机没发 PRESENT 也别让像素一直卡在 cache 里 */
        if (!lk_idle(&g_p) && t - last_rx > 500000u) lk_reset(&g_p);   /* 半截帧: 丢掉重新找同步字 */
        if (t - last_wdt > 1000000u){ ql_dev_feed_wdt(); last_wdt = t; }
        if (lcd_err == 0) clk_tick(t);
        if (lw_tick(&g_lw, now_ms()) == LW_LOST){ g_led_host = g_led; led_default(); }   /* 主机走了: 回到未连接心跳 */
        keys_poll();
        led_poll();
    }
}

int appimg_enter(void *arg){
    (void)arg;
    ql_dev_cfg_wdt(1);                                 /* 同原厂 enter; 任务里每秒喂一次 */
    ql_rtos_task_create(&g_task, 8192, APP_PRIORITY_NORMAL, "monita", task_entry, 0, 1);
    return 0;
}

void appimg_exit(void){ }
