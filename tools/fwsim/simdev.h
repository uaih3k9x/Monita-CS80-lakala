/*
 * 固件协议层的主机替身: 把 fw/link/dispatch.h (on_frame / ACK / KEY / 主机在不在) 和它用到的纯逻辑
 * (proto / blit / tribuf / imm / keys / led / linkwd) 原样编进来, 硬件换成假的:
 *   - 3 块 800×1280 RGB565 帧缓冲 (同实机: 原厂 fb + 开机 malloc 的第二、三块), 扫描基址是一个指针;
 *   - 帧事件"立即发生": 写完基址马上算锁存 (理想硬件, 等帧不花时间);
 *   - 卷轴按 sim_roll_rows 分配 (实机按堆里能拿到的, 典型 ≈ 2700 行);
 *   - 按键电平 / PWRKEY 计数由 sim_key_* 注入, USB 拔掉 (hotplug 回调) 由 sim_usb_unplug 注入; 灯、背光、段码屏 EN 只记下来给测试看;
 *   - 开机版权页是真固件代码 (fw/lcd/bootscreen.h) 画进屏上那块缓冲 (日期固定), 主机走了重画时屏上 CRC 应回到 sim_boot_crc;
 *   - 段码屏本地时钟是真固件代码 (fw/fg00/segclock.h + fgbuf.h + segmap.h), 只是 fg_flush 不刷 I2C, 显存 fg_fb 给测试看;
 *   - DIAG: 31 / 32 / 11 和"先收拢多缓冲"的分流走 dispatch.h, 具体的旧 op (diag.h, 碰寄存器) 换成桩:
 *     认识的 op 回 status 0、不带数据, 不认识的回 3 (同实机)。
 * 用的地方: tools/fwsim/fwsim.c (pty 上跑, 给上位机连) 和 fw/link/test_dispatch.c (单测)。
 * 包含方先定义 sim_out (设备 -> 主机的字节去哪)。
 */
#ifndef MONITA_FWSIM_SIMDEV_H
#define MONITA_FWSIM_SIMDEV_H
#pragma GCC diagnostic ignored "-Wunused-function"
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "proto.h"
#include "tribuf.h"
#include "imm.h"
#include "keys.h"
#include "led.h"
#include "linkwd.h"
#include "../../fw/fg00/fgbuf.h"
#include "../../fw/fg00/segmap.h"
#include "../../fw/lcd/bootscreen.h"

#define FW_VER        0x000Bu          /* 跟 fw/app.c 的 FW_VER (tools/fwsim/run.sh 会对一遍) */
#define LCD_W         800u
#define LCD_H         1280u
#define FB_SIZE       (LCD_W * LCD_H * 2u)
#define ROW_B         (LCD_W * 2u)
#define PORT          4u
#define KEY_SAMPLE_MS 8u
#define REPLY_MAX     68u
#define SIM_PL_CAP    65535u

/* ---- 可调 (fwsim 的命令行参数) ---- */
static uint32_t sim_max_payload = 32768u;   /* HELLO_ACK.max_payload 和拆帧上限 */
static uint32_t sim_nbuf_want   = 3u;       /* 开机拿到几块 fb (1..3) */
static uint32_t sim_roll_rows   = 2720u;    /* IMM_ENTER 时卷轴行数; < 1280 + 256 = 拿不到 (回 EUNSUP) */
static uint32_t g_keys_ok = 1, g_led_ok = 1, g_lcd_ok = 1, g_lz4_ok = 1;   /* g_lz4_ok = 0: 装成没有 RECT_LZ4 的旧固件 */
#define MAX_PAYLOAD sim_max_payload

/* ---- 时间: 默认真实单调时钟; 单测用 sim_virtual + sim_t_us ---- */
static int      sim_virtual;
static uint64_t sim_t_us;
static uint64_t osiUpTimeUS(void){
    if (sim_virtual) return sim_t_us;
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000u + (uint64_t)ts.tv_nsec / 1000u;
}
static uint32_t now_us(void){ return (uint32_t)osiUpTimeUS(); }
static uint32_t now_ms(void){ return (uint32_t)(osiUpTimeUS() >> 10); }   /* 1.024 ms 单位, 同实机 */

/* ---- 给测试看的记录 ---- */
static struct {
    uint32_t collapse, uncollapse, seg_writes, backlight, bl_writes, seg_en, boot_draws;
    uint8_t  led_mask;
    uint32_t base_writes;
} sim;

/* ---- 设备 -> 主机 ---- */
static void sim_out(const uint8_t *p, uint32_t n);
static void ql_uart_write(uint32_t port, const uint8_t *p, uint32_t n){ (void)port; sim_out(p, n); }

/* ---- dispatch.h 要的状态 ---- */
static uint8_t  g_pl[SIM_PL_CAP] __attribute__((aligned(4)));
static uint8_t  g_lz[SIM_PL_CAP] __attribute__((aligned(4)));   /* RECT_LZ4 暂存区 (同 app.c) */
static uint8_t  g_tx[64];
static lk_parser_t g_p;
static struct { uint32_t rx_bytes, rects, presents, bad_param, lz4; } st;
static tb_t     g_tb;
static uint32_t g_nbuf = 1;
static uint32_t g_flip_now = 1;
static int      g_boot_live;
static int      g_imm;
static im_t     g_im;
static uintptr_t g_roll; static uint32_t g_roll_rows;
static lw_t     g_lw;
static keys_t   g_keys;
static pwr_t    g_pwr;
static uint32_t g_key_n, g_key_at;
static led_t    g_led, g_led_host;

/* 帧缓冲: 第 0 块 = "原厂 fb" (FB_ADDR), 沉浸模式里当保留缓冲 */
static uint16_t sim_fb0[LCD_W * LCD_H] __attribute__((aligned(256)));
static uint16_t *sim_fbx[2];                 /* 第二、三块 (dbuf_init 时分配) */
#define FB_ADDR ((uintptr_t)sim_fb0)
static uintptr_t g_fb2, g_fb3;
static uintptr_t sim_scan = (uintptr_t)sim_fb0;   /* 正在扫描的地址 (屏上那一帧从这里起) */

static void dcache_clean(uintptr_t a, uint32_t n){ (void)a; (void)n; }

/* ---- 回包 ---- */
static void reply(uint8_t type, const uint8_t *pl, uint32_t n){
    uint8_t out[LK_HDR + REPLY_MAX + LK_TRAIL];
    if (n > REPLY_MAX) n = REPLY_MAX;
    ql_uart_write(PORT, out, lk_encode(out, type, g_p.seq, pl, n));
}
static void diag_reply(uint8_t op, uint8_t status, const uint32_t *w, uint32_t n){
    uint8_t b[4 + 4 * 16];
    reply(LK_DIAG_R, b, lk_diag_payload(b, op, status, w, n));
}

/* ---- 段码屏: 真固件的本地时钟 (segclock.h), 刷屏只记账 ---- */
#define SEG_HOST_HOLD_US 5000000u                       /* 同 fw/app.c */
static void fg_flush(void){ sim.seg_writes++; }
#include "../../fw/fg00/segclock.h"

/* ---- 背光 / 灯 / 段码屏 EN: 只记账; 开机页 Link 行 OK 只改标记 (屏上像素不动) ---- */
static void boot_link_ok(void){ g_boot_live = 0; }
static void lcd_backlight(uint32_t on){ sim.backlight = on ? 1u : 0u; sim.bl_writes++; }
static void led_poll(void){ if (g_led_ok) sim.led_mask = led_step(&g_led, now_ms()); }
static void panel_power(uint32_t on){ lcd_backlight(on); sim.seg_en = on ? 1u : 0u; }   /* 同 app.c: 背光 + 段码屏 EN */

/* ---- USB 拔掉 (实机: ql_usb_bind_hotplug_cb 回调里计数) ---- */
static uint32_t sim_unplug_n, sim_unplug_seen;
static void sim_usb_unplug(void){ sim_unplug_n++; }
static int  usb_unplugged(void){ if (sim_unplug_seen == sim_unplug_n) return 0; sim_unplug_seen = sim_unplug_n; return 1; }

/* ---- 按键注入: 键 2..4 是电平, 键 1 (PWRKEY) 是按下 / 抬起回调计数 (同实机) ---- */
static uint8_t  sim_key_level;
static uint32_t sim_pwr_press, sim_pwr_release;
static void sim_key_set(int k, int down){
    if (k == 1){ if (down) sim_pwr_press++; else sim_pwr_release++; return; }
    if (k < 2 || k > 4) return;
    if (down) sim_key_level |= KEY_BIT(k); else sim_key_level &= (uint8_t)~KEY_BIT(k);
}
static uint8_t keys_hw_raw(pwr_t *pw, uint32_t now){
    uint8_t m = pwr_sample(pw, sim_pwr_press, sim_pwr_release, now) ? KEY_BIT(1) : 0;
    return (uint8_t)(m | (sim_key_level & 0x0Eu));
}

/* ---- 多缓冲 (同 app.c, 帧事件立即发生) ---- */
static uintptr_t tb_addr(int i){ return g_tb.buf[i]; }
static uintptr_t draw_target(void){ return tb_addr(g_tb.d >= 0 ? g_tb.d : g_tb.s); }
static int  sim_wait(void *ctx){ (void)ctx; return 1; }
static void tb_poll(void){ if (g_tb.p >= 0){ sim_scan = tb_addr(g_tb.p); tb_latched(&g_tb); } }
static int  sim_mover(uintptr_t d, uintptr_t s, uint32_t n, void *ctx){ (void)ctx; memmove((void *)d, (const void *)s, n); return 0; }

static void tb_setup(uint32_t n, uintptr_t front){
    uintptr_t b[3] = { front, 0, 0 }; uint32_t k = 1;
    const uintptr_t all[3] = { FB_ADDR, g_fb2, g_fb3 };
    for (uint32_t i = 0; i < 3 && k < n; i++) if (all[i] && all[i] != front) b[k++] = all[i];
    tb_init(&g_tb, b, k, LCD_W, LCD_H);
    tb_reset_to(&g_tb, 0);
}

static void dbuf_init(void){
    g_nbuf = sim_nbuf_want < 1 ? 1 : sim_nbuf_want > 3 ? 3 : sim_nbuf_want;
    for (uint32_t i = 0; i < 2; i++){
        if (i + 2 <= g_nbuf && !sim_fbx[i]) sim_fbx[i] = (uint16_t *)calloc(LCD_W * LCD_H, 2);
    }
    g_fb2 = g_nbuf >= 2 ? (uintptr_t)sim_fbx[0] : 0;
    g_fb3 = g_nbuf >= 3 ? (uintptr_t)sim_fbx[1] : 0;
    tb_setup(g_nbuf, FB_ADDR);
}

static void flush_dirty(void){
    if (g_tb.y1 <= g_tb.y0) return;
    if (g_tb.n == 1){ g_tb.y0 = LCD_H; g_tb.y1 = 0; }
}

static void draw_begin(void){ if (g_tb.n > 1){ tb_poll(); tb_begin(&g_tb, sim_wait, 0, sim_mover, 0); } }

static void present(void){
    if (g_tb.n == 1){ flush_dirty(); return; }
    if (g_tb.d < 0 || g_tb.y1 <= g_tb.y0) return;
    flush_dirty();
    tb_poll();
    if (tb_need_wait_before_present(&g_tb)){ sim_wait(0); tb_poll(); }
    int d = tb_present(&g_tb);
    if (d < 0) return;
    sim.base_writes++;
    tb_poll();                                         /* 理想硬件: 下一个帧头马上到 */
}

static int do_copy(const uint8_t *pl, uint32_t n){
    bk_copy_t c;
    if (!bk_copy_parse(pl, n, LCD_W, LCD_H, &c)) return LK_EPARAM;
    if (g_tb.n > 1){
        tb_poll();
        if (tb_copy(&g_tb, &c, sim_wait, 0, sim_mover, 0)) return LK_OK;
    }
    return (int)lk_copy_apply((uint16_t *)draw_target(), LCD_W, LCD_H, pl, n, &g_tb.y0, &g_tb.y1);
}

/* 旧 DIAG 前后 (实机: 退回双缓冲视角再重建; 这里只保留对状态机的效果: 没提交的作废, 其余块整屏同步) */
static void tri_collapse(void){
    sim.collapse++;
    if (g_tb.n > 1 && g_tb.d >= 0){ g_tb.ver[g_tb.d] = 0; g_tb.d = -1; g_tb.y0 = LCD_H; g_tb.y1 = 0; }
    tb_poll();
}
static void tri_uncollapse(void){ sim.uncollapse++; tb_reset_to(&g_tb, g_tb.s); }

static void diag_tri(void){
    uint32_t w[15] = { g_tb.n, g_nbuf, g_nbuf == 3 ? 0u : g_nbuf == 2 ? 1u : 3u, (uint32_t)g_tb.s, (uint32_t)g_tb.p,
                       (uint32_t)g_tb.d, g_tb.gver, g_tb.waits, g_tb.xcopies, g_tb.syncs, g_tb.full_syncs,
                       (uint32_t)tb_addr(0), (uint32_t)tb_addr(1), (uint32_t)tb_addr(2), 0 };
    diag_reply(31, 0, w, 15);
}
static void diag_nbuf(uint8_t v){
    tri_collapse();
    uint32_t n = v == 0 || v > g_nbuf ? g_nbuf : v;
    uintptr_t s = tb_addr(g_tb.s);
    for (uint32_t i = 0; i < 3; i++){                  /* 其余块抄成和扫描的一样 (实机 copy_rows) */
        const uintptr_t b = i == 0 ? FB_ADDR : i == 1 ? g_fb2 : g_fb3;
        if (b && b != s) memcpy((void *)b, (const void *)s, FB_SIZE);
    }
    tb_setup(n, s);
    sim.uncollapse++;
    uint32_t w = g_tb.n;
    diag_reply(32, 0, &w, 1);
}

/* diag.h 的旧 op 换成桩 */
static void on_diag(const uint8_t *pl, uint32_t n){
    if (n < 1){ diag_reply(0, LK_EPARAM, 0, 0); return; }
    uint8_t op = pl[0];
    int known = op <= 7 || op == 9 || op == 10 || (op >= 20 && op <= 25);
    diag_reply(op, known ? LK_OK : LK_EUNSUP, 0, 0);
}

/* ---- 沉浸模式 (同 app.c, 卷轴按 sim_roll_rows) ---- */
static uint16_t *sim_rollbuf;
static uintptr_t im_addr(int buf, uint32_t row){ return (buf == IM_BUF_ROLL ? g_roll : FB_ADDR) + row * ROW_B; }
static void im_io_copy(void *c, int db, uint32_t dr, int sb, uint32_t sr, uint32_t n){
    (void)c; memmove((void *)im_addr(db, dr), (const void *)im_addr(sb, sr), n * ROW_B);
}
static void im_io_base(void *c, int buf, uint32_t row){
    (void)c;
    if (buf == IM_BUF_ROLL ? row + LCD_H > g_roll_rows : row != 0) return;
    sim_scan = im_addr(buf, row); sim.base_writes++;
}
static void im_io_wait(void *c){ (void)c; }
static const im_io_t g_imio = { im_io_copy, im_io_base, im_io_wait, 0 };

static void imm_poll(void){
    if (!g_imm) return;
    uint32_t t = now_ms();
    int32_t r = im_tick(&g_im, t, 1);
    if (r >= 0) im_io_base(0, IM_BUF_ROLL, (uint32_t)r);
    else if (im_should_rebase(&g_im, t)) im_rebase(&g_im, &g_imio);
}

static void imm_release_fbs(void){
    if (g_tb.n > 1){
        if (g_tb.d >= 0){ g_tb.ver[g_tb.d] = 0; g_tb.d = -1; g_tb.y0 = LCD_H; g_tb.y1 = 0; }
        tb_poll();
        uintptr_t s = tb_addr(g_tb.s);
        if (s != FB_ADDR){ memcpy((void *)FB_ADDR, (const void *)s, FB_SIZE); sim_scan = FB_ADDR; }
    } else flush_dirty();
    g_fb2 = g_fb3 = 0; g_nbuf = 1;                     /* 第二、三块"还给堆" (内存留着, 下次 dbuf_init 再用) */
    tb_setup(1, FB_ADDR);
}

static void imm_exit(void){
    if (!g_imm) return;
    im_hold(&g_im, &g_imio, now_ms());
    free(sim_rollbuf); sim_rollbuf = 0;
    g_roll = 0; g_roll_rows = 0;
    g_imm = 0;
    dbuf_init();
}

static uint8_t imm_enter(uint32_t y0, uint32_t *rows){
    *rows = 0;
    if (!g_lcd_ok) return LK_EUNSUP;
    if (g_imm){
        im_hold(&g_im, &g_imio, now_ms());
        im_restart(&g_im, y0);
        *rows = g_roll_rows;
        return LK_OK;
    }
    g_boot_live = 0;
    imm_release_fbs();
    uint32_t r = sim_roll_rows > 4096u ? 4096u : sim_roll_rows;
    if (r < LCD_H + IM_MIN_AHEAD || im_init(&g_im, r, LCD_H, y0)){
        dbuf_init();
        return LK_EUNSUP;
    }
    sim_rollbuf = (uint16_t *)calloc((size_t)r * LCD_W, 2);
    g_roll = (uintptr_t)sim_rollbuf; g_roll_rows = r;
    g_imm = 1;
    *rows = g_roll_rows;
    return LK_OK;
}

static void imm_make_room(void){
    im_settle(&g_im, &g_imio, now_ms());
    if (im_can_rebase(&g_im)) im_rebase(&g_im, &g_imio);
}

/* ---- 开机版权页 (真固件画图代码, 画在屏上那块; 同 app.c boot_draw) ---- */
static uint32_t sim_boot_crc;                          /* 开机时画的那一屏 */
static void sim_boot_draw(void){
    bs_info_t in = { FW_VER, "2026-01-01", 0xFFFFFFFFu, g_tb.n, 1, 0 };
    tb_poll();
    bs_draw((uint16_t *)tb_addr(g_tb.s), &in);
    tb_reset_to(&g_tb, g_tb.s);
    g_boot_live = 1; sim.boot_draws++;
}
/* 主机走了 (同 app.c): 退出沉浸模式, 没提交的改动作废, 画到正在扫描的那块上 */
static void boot_redraw(void){
    if (!g_lcd_ok) return;
    imm_exit();
    if (g_tb.n > 1 && g_tb.d >= 0){ g_tb.ver[g_tb.d] = 0; g_tb.d = -1; g_tb.y0 = LCD_H; g_tb.y1 = 0; }
    sim_boot_draw();
}

#include "dispatch.h"

/* 屏上那一帧 (扫描基址起 1280 行) 的 CRC-32, 主机可以拿它对照自己画的 */
static uint32_t sim_screen_crc(void){ return lk_crc32((const uint8_t *)sim_scan, FB_SIZE); }

/* ---- 主循环 (同 app.c task_entry, 去掉看门狗 / cache) ---- */
static uint32_t sim_last_rx;
static void sim_init(void){
    dbuf_init();
    if (g_lcd_ok){ sim_boot_draw(); sim_boot_crc = sim_screen_crc(); lcd_backlight(1); seg_draw_local(); }   /* 同 app.c: 开机页、亮背光, 段码屏 "------" */
    sim.seg_en = 1;
    led_init(&g_led); led_default(); led_poll();
    keys_init(&g_keys); pwr_init(&g_pwr); lw_init(&g_lw, now_ms());
    lk_init(&g_p, g_pl, sim_max_payload > SIM_PL_CAP ? SIM_PL_CAP : sim_max_payload);
    sim_last_rx = now_us();
}
static void sim_rx(const uint8_t *d, uint32_t n){
    link_rx(d, n);
    sim_last_rx = now_us();
    keys_poll();
    imm_poll();
}
static void sim_tick(void){
    uint32_t t = now_us();
    tb_poll();
    if (g_tb.n == 1 && t - sim_last_rx > 50000u) flush_dirty();
    if (!lk_idle(&g_p) && t - sim_last_rx > 500000u) lk_reset(&g_p);   /* 半截帧: 丢掉重新找同步字 */
    if (g_lcd_ok) clk_tick(t);
    link_lost_poll();
    keys_poll();
    led_poll();
    imm_poll();
}


#endif
