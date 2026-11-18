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

int main(void){
    sim_virtual = 1; sim_t_us = 1000000u;
    rxp.buf = rx_pl;
    lk_init(&rxp, rx_pl, sizeof rx_pl);
    sim_init();
    test_hello();
    test_empty_diag();
    test_unknown_and_ping();
    printf("dispatch: %s\n", fails ? "FAILED" : "all OK");
    return fails ? 1 : 0;
}
