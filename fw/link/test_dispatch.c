/* fw/link/dispatch.h 的主机单测 (sh fw/link/test.sh): 用 tools/fwsim/simdev.h 的假硬件, 直接喂字节、看回包。
 * 更完整的端到端对照 (Python / Swift 两个上位机连模拟器) 见 tools/fwsim/run.sh。 */
#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <stdint.h>
static void sim_out(const uint8_t *p, uint32_t n);
#include "../../tools/fwsim/simdev.h"

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)

/* 设备回的字节拆成帧 */
static uint8_t out_buf[1 << 16]; static uint32_t out_n;
static void sim_out(const uint8_t *p, uint32_t n){ if (out_n + n <= sizeof out_buf){ memcpy(out_buf + out_n, p, n); out_n += n; } }

static uint8_t rx_pl[256]; static uint8_t rx_type, rx_seq; static uint32_t rx_len;
static lk_parser_t rxp;
/* 取下一条回包; 返回 0 = 没有 */
static uint32_t out_pos;
static int next_reply(void){
    while (out_pos < out_n){
        int ev; uint32_t k = lk_feed(&rxp, out_buf + out_pos, out_n - out_pos, &ev);
        out_pos += k;
        if (ev){ rx_type = rxp.type; rx_seq = rxp.seq; rx_len = rxp.len; return 1; }
    }
    return 0;
}

static uint8_t seq;
static void send(uint8_t type, const uint8_t *pl, uint32_t n){
    static uint8_t f[LK_HDR + 40000 + LK_TRAIL];
    uint32_t len = lk_encode(f, type, ++seq, pl, n);
    sim_rx(f, len);
}

static void test_hello(void){
    send(LK_HELLO, 0, 0);
    CHECK(next_reply() && rx_type == LK_HELLO_ACK && rx_seq == seq && rx_len == 16);
    CHECK(lk_rd16(rx_pl + 0) == LK_PROTO_VER && lk_rd16(rx_pl + 2) == FW_VER);
    CHECK(lk_rd16(rx_pl + 4) == 800 && lk_rd16(rx_pl + 6) == 1280 && rx_pl[8] == 1 && rx_pl[9] == 72);
    CHECK(FW_VER == 11);
    CHECK(lk_rd16(rx_pl + 10) == 0x1FF && lk_rd16(rx_pl + 12) == 32768);      /* bit7 SLEEP (fw v10), bit8 LZ4 (fw v11) */
    CHECK(!next_reply());
}

/* 空 payload 的 DIAG 不该拿上一帧残留的 op 去判断要不要收拢多缓冲 */
static void test_empty_diag(void){
    uint8_t fill[6] = { 5, 0, 0, 0, 0, 0 };            /* DG_FILL: 会动缓冲 -> 先收拢 */
    uint32_t c0 = sim.collapse;
    send(LK_DIAG, fill, 6);
    CHECK(next_reply() && rx_type == LK_DIAG_R && rx_pl[0] == 5);
    CHECK(sim.collapse == c0 + 1);
    send(LK_DIAG, 0, 0);
    CHECK(next_reply() && rx_type == LK_DIAG_R && rx_seq == seq);
    CHECK(rx_pl[0] == 0 && rx_pl[1] == LK_EPARAM && lk_rd16(rx_pl + 2) == 0);
    CHECK(sim.collapse == c0 + 1);                     /* 没白跑一次 tri_collapse */
}

static void test_unknown_and_ping(void){
    send(0x55, 0, 0);
    CHECK(next_reply() && rx_type == LK_ACK && rx_pl[0] == 0x55 && rx_pl[1] == LK_EUNSUP);
    uint8_t op8 = 8;
    send(LK_DIAG, &op8, 1);
    CHECK(next_reply() && rx_type == LK_DIAG_R && rx_pl[0] == 8 && rx_pl[1] == LK_EUNSUP);
    send(LK_PING, 0, 0);
    CHECK(next_reply() && rx_type == LK_ACK && rx_pl[0] == LK_PING && rx_pl[1] == LK_OK);
}

/* 段码屏第 k 列读回 7 段掩码 (fgbuf.h fg_digit 的逆) */
static uint8_t digit_at(int k){
    uint8_t c0 = fg_fb[k], c1 = fg_fb[18 + k], c2 = fg_fb[36 + k], c3 = fg_fb[54 + k];
    return (uint8_t)((c1 & 0x80 ? 0x01 : 0) | (c1 & 0x08 ? 0x02 : 0) | (c2 & 0x08 ? 0x04 : 0) | (c3 & 0x80 ? 0x08 : 0)
                   | (c2 & 0x80 ? 0x10 : 0) | (c0 & 0x80 ? 0x20 : 0) | (c0 & 0x08 ? 0x40 : 0));
}
static int clock_shows(const char *hms){                /* "hhmmss" */
    for (int i = 0; i < 6; i++) if (digit_at(FG_COL_CLK(i)) != fg_hex[hms[i] - '0']) return 0;
    for (int i = 0; i < 6; i++) if (digit_at(FG_COL_MAIN(i)) != SEG_DASH) return 0;
    return (fg_fb[FG_SEG_CLK_COLON_A >> 8] & (FG_SEG_CLK_COLON_A & 0xFF)) && (fg_fb[FG_SEG_CLK_COLON_B >> 8] & (FG_SEG_CLK_COLON_B & 0xFF));
}
static int clock_shows_dashes(void){                    /* 主区 "------" (本地画面) */
    for (int i = 0; i < 6; i++) if (digit_at(FG_COL_MAIN(i)) != SEG_DASH) return 0;
    return 1;
}
static void tick_s(uint32_t s){ for (uint32_t i = 0; i < 10 * s; i++){ sim_t_us += 100000u; sim_tick(); } }

/* HELLO 带时间: 设备时钟 = 当地时间 (unix + tz), 段码屏小时钟跟着走; 灯灭、KEY 计数清零 */
static void test_hello_clock(void){
    uint8_t pl[6];
    lk_wr16(pl, 1784081587u & 0xFFFF); lk_wr16(pl + 2, 1784081587u >> 16); lk_wr16(pl + 4, (uint16_t)-240);   /* 02:13:07Z, EDT */
    g_key_n = 7;
    send(LK_HELLO, pl, 6);
    CHECK(next_reply() && rx_type == LK_HELLO_ACK);
    CHECK(clk_valid && clk_sod == 22 * 3600 + 13 * 60 + 7);
    CHECK(clock_shows("221307"));
    CHECK(g_key_n == 0 && g_led.mode == LED_MODE_OFF && sim.led_mask == 0);
    tick_s(1);
    CHECK(clk_sod == 22 * 3600 + 13 * 60 + 8 && clock_shows("221308"));
    lk_wr16(pl + 4, 330);                                /* IST: 07:43:07, 东正 */
    send(LK_HELLO, pl, 6);
    CHECK(next_reply() && clk_sod == 7 * 3600 + 43 * 60 + 7 && clock_shows("074307"));
    uint32_t mid = 1784073599u;                          /* 2026-07-14 23:59:59Z */
    lk_wr16(pl, mid & 0xFFFF); lk_wr16(pl + 2, mid >> 16); lk_wr16(pl + 4, 0);
    send(LK_HELLO, pl, 6);
    CHECK(next_reply() && clock_shows("235959"));
    tick_s(1);
    CHECK(clk_sod == 0 && clock_shows("000000"));      /* 过零点回绕 */
    send(LK_HELLO, 0, 0);                                /* 不带时间的 HELLO: 时钟照走 */
    CHECK(next_reply() && clk_valid && clk_sod == 0);
}

/* 主机走了 (5 s 没有效帧) -> 蓝灯心跳; 又来字节 (没 HELLO) -> 恢复主机设的灯; 沉浸模式里来 HELLO -> 退回普通模式 */
static void test_link_lost_and_rehello(void){
    const uint8_t green[8] = { LED_G, LED_MODE_ON, 0, 0, 0, 0, 0, 0 };
    send(LK_LED, green, 8);
    CHECK(next_reply() && rx_pl[1] == LK_OK && g_led.mode == LED_MODE_ON && sim.led_mask == LED_G);
    tick_s(6);
    CHECK(!g_lw.up && g_led.mode == LED_MODE_BLINK && g_led.mask == LED_B);
    send(LK_PING, 0, 0);
    CHECK(next_reply() && g_lw.up && g_led.mode == LED_MODE_ON && g_led.mask == LED_G && sim.led_mask == LED_G);
    uint8_t y0[4] = { 0, 0, 0, 0 };
    send(LK_IMM_ENTER, y0, 4);
    CHECK(next_reply() && rx_pl[1] == LK_OK && g_imm);
    send(LK_HELLO, 0, 0);
    CHECK(next_reply() && rx_type == LK_HELLO_ACK && !g_imm && g_tb.n == 3);
}


static void ms_ticks(uint32_t ms){ for (uint32_t i = 0; i < ms / 10u; i++){ sim_t_us += 10240u; sim_tick(); } }   /* 10 个 1.024 ms 单位一拍 */

/* 整帧推一屏 (单色), 开机画面就不在了 */
static void push_solid(uint16_t c){
    static uint8_t pl[8 + 800 * 20 * 2];
    for (uint32_t y = 0; y < LCD_H; y += 20){
        lk_wr16(pl, 0); lk_wr16(pl + 2, (uint16_t)y); lk_wr16(pl + 4, 800); lk_wr16(pl + 6, 20);
        for (uint32_t i = 0; i < 800u * 20u; i++) lk_wr16(pl + 8 + 2 * i, c);
        send(LK_RECT, pl, sizeof pl);
    }
    uint8_t fid[2] = { 1, 0 };
    send(LK_PRESENT, fid, 2);
    while (next_reply()) {}
}

/* v10: HELLO 之后 5 s 没有有效帧 = 主机走了 -> 大屏回开机版权页 (Link 行 WAIT)、段码屏回本地时钟、蓝灯心跳; 乱码不算帧 */
static void test_lost_boot_screen(void){
    send(LK_HELLO, 0, 0);
    while (next_reply()) {}
    uint8_t seg[72] = { 0xAA };
    send(LK_SEG, seg, 72);
    CHECK(next_reply() && rx_pl[1] == LK_OK && seg_host_seen);
    push_solid(0x07E0);
    CHECK(!g_boot_live && sim_screen_crc() != sim_boot_crc);
    const uint8_t off = 0;
    send(LK_BACKLIGHT, &off, 1);                                 /* 主机关过背光: 回开机画面时要重新打开 */
    CHECK(next_reply() && sim.backlight == 0);
    uint32_t draws = sim.boot_draws;
    static const uint8_t junk[15] = { 0xA5, 0x00, 0x11, 0x22, 0x5A, 0xA5, 0x5A, 0x7F, 0, 0, 0, 1, 2, 3, 4 };   /* 乱码 + 一条 CRC 错的 PING */
    for (int i = 0; i < 4; i++){ ms_ticks(1200); sim_rx(junk, sizeof junk); }   /* 4.8 s 里只有乱码 */
    CHECK(g_lw.up);
    ms_ticks(300);
    CHECK(!g_lw.up);
    CHECK(g_boot_live && sim.boot_draws == draws + 1);
    CHECK(sim_screen_crc() == sim_boot_crc);                     /* 和开机时画的一样 (Link 行 WAIT) */
    CHECK(!seg_host_seen && clock_shows_dashes());
    CHECK(g_led.mode == LED_MODE_BLINK && g_led.mask == LED_B && g_led.on == 2000u);
    CHECK(sim.backlight == 1 && !g_lw.asleep);
    send(LK_HELLO, 0, 0);                                        /* 主机重连: Link 行 OK, 之后推整帧 */
    CHECK(next_reply() && rx_type == LK_HELLO_ACK && !g_boot_live && g_lw.up);
}

/* 沉浸模式里主机走了: 先退出沉浸模式 (还回多缓冲) 再画开机页 */
static void test_lost_in_immersive(void){
    uint8_t y0[4] = { 0, 0, 0, 0 };
    send(LK_IMM_ENTER, y0, 4);
    CHECK(next_reply() && rx_pl[1] == LK_OK && g_imm);
    ms_ticks(5200);
    CHECK(!g_lw.up && !g_imm && g_tb.n == 3 && g_boot_live && sim_screen_crc() == sim_boot_crc);
    send(LK_HELLO, 0, 0);
    while (next_reply()) {}
}

/* SLEEP (0x32): ACK 后关背光、段码屏 EN 低、灭灯, 本地时钟不再刷段码屏; 任何一帧叫醒并恢复主机设的灯 */
static void test_host_sleep(void){
    const uint8_t green[8] = { LED_G, LED_MODE_ON, 0, 0, 0, 0, 0, 0 };
    send(LK_LED, green, 8);
    CHECK(next_reply() && rx_pl[1] == LK_OK);
    push_solid(0x001F);
    uint32_t crc = sim_screen_crc();
    send(LK_SLEEP, green, 1);                                    /* 带了载荷: 拒 */
    CHECK(next_reply() && rx_type == LK_ACK && rx_pl[0] == LK_SLEEP && rx_pl[1] == LK_EPARAM && !g_lw.asleep);
    send(LK_SLEEP, 0, 0);
    CHECK(next_reply() && rx_type == LK_ACK && rx_pl[0] == LK_SLEEP && rx_pl[1] == LK_OK);
    CHECK(g_lw.asleep && !g_lw.up);
    CHECK(sim.backlight == 0 && sim.seg_en == 0 && sim.led_mask == 0);
    uint32_t w0 = sim.seg_writes, draws = sim.boot_draws;
    ms_ticks(20000);                                             /* 睡着: 不报 LOST、不画开机页、段码屏不刷 */
    CHECK(sim.seg_writes == w0 && sim.boot_draws == draws && sim.backlight == 0 && sim.led_mask == 0);
    send(LK_PING, 0, 0);
    CHECK(next_reply() && rx_pl[0] == LK_PING && rx_pl[1] == LK_OK);
    CHECK(!g_lw.asleep && g_lw.up && sim.backlight == 1 && sim.seg_en == 1);
    CHECK(g_led.mode == LED_MODE_ON && g_led.mask == LED_G && sim.led_mask == LED_G);
    CHECK(sim_screen_crc() == crc && !g_boot_live);              /* 睡之前的画面还在 */
    CHECK(clock_shows_dashes() && sim.seg_writes > w0);         /* 段码屏重画 (本地) */
}

/* 主机走了满 10 min: 自动休眠; 按键叫醒 (开机页 + 蓝灯心跳), 不算主机回来; 来帧叫醒 */
static void test_idle_sleep_and_key_wake(void){
    ms_ticks(5200);
    CHECK(!g_lw.up && !g_lw.asleep && sim.backlight == 1);
    for (uint32_t i = 0; i < 600; i++) ms_ticks(1000);
    CHECK(g_lw.asleep && sim.backlight == 0 && sim.seg_en == 0 && sim.led_mask == 0);
    sim_key_set(3, 1); ms_ticks(100); sim_key_set(3, 0); ms_ticks(100);
    CHECK(!g_lw.asleep && !g_lw.up && sim.backlight == 1 && sim.seg_en == 1);
    CHECK(g_boot_live && sim_screen_crc() == sim_boot_crc);
    CHECK(g_led.mode == LED_MODE_BLINK && g_led.mask == LED_B);
    for (uint32_t i = 0; i < 601; i++) ms_ticks(1000);
    CHECK(g_lw.asleep);
    send(LK_PING, 0, 0);                                         /* 主机回来 (没 HELLO): 醒 + 恢复 */
    CHECK(next_reply() && !g_lw.asleep && g_lw.up && sim.backlight == 1);
}

/* USB 拔掉 (hotplug 回调): 不用等 5 s */
static void test_unplug(void){
    send(LK_HELLO, 0, 0);
    while (next_reply()) {}
    push_solid(0xF800);
    sim_usb_unplug();
    ms_ticks(20);
    CHECK(!g_lw.up && g_boot_live && sim_screen_crc() == sim_boot_crc);
    send(LK_HELLO, 0, 0);
    while (next_reply()) {}
}

/* fw v11 RECT_LZ4 (0x23): 和 RECT 一样不应答、计入 rects / bad_param; 坏块不动像素; 沉浸模式里拒; 没有 LZ4 (旧固件) 当不认识的 type */
static uint32_t ack_u32(int i){ return lk_rd32(rx_pl + 4 + 4 * (uint32_t)i); }   /* 0 rx_bytes 1 frames 2 crc 3 len 4 rects 5 presents 6 bad 7 up 8 extra */
static void ping_stats(uint32_t *rects, uint32_t *bad){
    send(LK_PING, 0, 0);
    CHECK(next_reply() && rx_type == LK_ACK && rx_pl[0] == LK_PING);
    *rects = ack_u32(4); *bad = ack_u32(6);
    CHECK(!next_reply());
}
/* 一整条 800×20 的单色: 一个字面量像素 + offset 2 的长 match, 最后 0 字面量 */
static uint32_t solid_lz4(uint8_t *pl, uint32_t y, uint16_t c){
    lk_wr16(pl, 0); lk_wr16(pl + 2, (uint16_t)y); lk_wr16(pl + 4, 800); lk_wr16(pl + 6, 20); lk_wr32(pl + 8, 32000);
    uint8_t *b = pl + 12; uint32_t n = 0, ml = 32000 - 2 - 4 - 15;
    b[n++] = 0x2F; b[n++] = (uint8_t)c; b[n++] = (uint8_t)(c >> 8); b[n++] = 2; b[n++] = 0;
    while (ml >= 255){ b[n++] = 255; ml -= 255; }
    b[n++] = (uint8_t)ml; b[n++] = 0x00;
    return 12 + n;
}
static void test_rect_lz4(void){
    send(LK_HELLO, 0, 0);
    CHECK(next_reply() && rx_type == LK_HELLO_ACK && (lk_rd16(rx_pl + 10) & LK_CAP_LZ4));
    uint32_t r0, b0, r1, b1;
    push_solid(0x1234);
    uint32_t crc_rect = sim_screen_crc();
    push_solid(0);
    ping_stats(&r0, &b0);
    static uint8_t pl[12 + 400];
    for (uint32_t y = 0; y < LCD_H; y += 20) send(LK_RECT_LZ4, pl, solid_lz4(pl, y, 0x1234));
    CHECK(!next_reply());                              /* 不应答 */
    uint8_t fid[2] = { 2, 0 };
    send(LK_PRESENT, fid, 2);
    CHECK(next_reply() && rx_pl[0] == LK_PRESENT && rx_pl[1] == LK_OK);
    ping_stats(&r1, &b1);
    CHECK(r1 == r0 + 64 && b1 == b0 && st.lz4 >= 64);
    CHECK(sim_screen_crc() == crc_rect);               /* 和同样像素的 RECT 一模一样 */
    /* 坏块: offset 0 / 截断 / raw_len 不对 / 解出来超过 max_payload - 8 -> bad_param, 像素不动 */
    uint32_t n = solid_lz4(pl, 0, 0xFFFF);
    pl[12 + 3] = 0; pl[12 + 4] = 0;
    send(LK_RECT_LZ4, pl, n);
    n = solid_lz4(pl, 20, 0xFFFF);
    send(LK_RECT_LZ4, pl, n - 1);
    n = solid_lz4(pl, 40, 0xFFFF); lk_wr32(pl + 8, 31998);
    send(LK_RECT_LZ4, pl, n);
    n = solid_lz4(pl, 60, 0xFFFF); lk_wr16(pl + 6, 21); lk_wr32(pl + 8, 33600);
    send(LK_RECT_LZ4, pl, n);
    send(LK_RECT_LZ4, pl, 11);                         /* 头都不够 */
    fid[0] = 3;
    send(LK_PRESENT, fid, 2);
    CHECK(next_reply() && rx_pl[0] == LK_PRESENT);
    ping_stats(&r0, &b0);
    CHECK(r0 == r1 && b0 == b1 + 5);
    CHECK(sim_screen_crc() == crc_rect);
    /* 沉浸模式里: 同 RECT, 计 bad_param 不应答 */
    uint8_t y0[4] = { 0 };
    send(LK_IMM_ENTER, y0, 4);
    CHECK(next_reply() && rx_pl[1] == LK_OK && g_imm);
    send(LK_RECT_LZ4, pl, solid_lz4(pl, 0, 1));
    ping_stats(&r1, &b1);
    CHECK(r1 == r0 && b1 == b0 + 1);
    send(LK_IMM_EXIT, 0, 0);
    CHECK(next_reply() && !g_imm && (lk_rd32(rx_pl + 36) & LK_CAP_LZ4));
    /* 旧固件 (没有 LZ4): caps 没有 bit8, 0x23 回 ACK EUNSUP (同 v10 的 default) */
    g_lz4_ok = 0;
    send(LK_HELLO, 0, 0);
    CHECK(next_reply() && rx_type == LK_HELLO_ACK && !(lk_rd16(rx_pl + 10) & LK_CAP_LZ4));
    send(LK_RECT_LZ4, pl, solid_lz4(pl, 0, 1));
    CHECK(next_reply() && rx_type == LK_ACK && rx_pl[0] == LK_RECT_LZ4 && rx_pl[1] == LK_EUNSUP);
    CHECK(ack_u32(4) == 0 && ack_u32(6) == 0);
    g_lz4_ok = 1;
    send(LK_HELLO, 0, 0);
    CHECK(next_reply() && (lk_rd16(rx_pl + 10) & LK_CAP_LZ4));
}

int main(void){
    sim_virtual = 1; sim_t_us = 1000000u;
    rxp.buf = rx_pl;
    lk_init(&rxp, rx_pl, sizeof rx_pl);
    sim_init();
    test_hello();
    test_empty_diag();
    test_unknown_and_ping();
    test_hello_clock();
    test_link_lost_and_rehello();
    test_lost_boot_screen();
    test_lost_in_immersive();
    test_host_sleep();
    test_idle_sleep_and_key_wake();
    test_unplug();
    test_rect_lz4();
    printf("dispatch: %s\n", fails ? "FAILED" : "all OK");
    return fails ? 1 : 0;
}
