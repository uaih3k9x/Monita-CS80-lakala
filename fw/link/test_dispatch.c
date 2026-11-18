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
    CHECK(lk_rd16(rx_pl + 10) == 0x7F && lk_rd16(rx_pl + 12) == 32768);
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

/* 主机走了 (15 s 没字节) -> 蓝灯心跳; 又来字节 (没 HELLO) -> 恢复主机设的灯; 沉浸模式里来 HELLO -> 退回普通模式 */
static void test_link_lost_and_rehello(void){
    const uint8_t green[8] = { LED_G, LED_MODE_ON, 0, 0, 0, 0, 0, 0 };
    send(LK_LED, green, 8);
    CHECK(next_reply() && rx_pl[1] == LK_OK && g_led.mode == LED_MODE_ON && sim.led_mask == LED_G);
    tick_s(16);
    CHECK(!g_lw.up && g_led.mode == LED_MODE_BLINK && g_led.mask == LED_B);
    send(LK_PING, 0, 0);
    CHECK(next_reply() && g_lw.up && g_led.mode == LED_MODE_ON && g_led.mask == LED_G && sim.led_mask == LED_G);
    uint8_t y0[4] = { 0, 0, 0, 0 };
    send(LK_IMM_ENTER, y0, 4);
    CHECK(next_reply() && rx_pl[1] == LK_OK && g_imm);
    send(LK_HELLO, 0, 0);
    CHECK(next_reply() && rx_type == LK_HELLO_ACK && !g_imm && g_tb.n == 3);
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
    printf("dispatch: %s\n", fails ? "FAILED" : "all OK");
    return fails ? 1 : 0;
}
