/* fw/link/proto.h 的主机单测: sh fw/link/test.sh */
#include <stdio.h>
#include <string.h>
#include "proto.h"

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)

static void test_crc(void){
    CHECK(lk_crc32((const uint8_t *)"123456789", 9) == 0xCBF43926u);   /* CRC-32/IEEE 标准校验值 */
    CHECK(lk_crc32((const uint8_t *)"", 0) == 0);
}

static uint8_t pbuf[64];

static void test_roundtrip_bytewise_and_garbage(void){
    uint8_t f[64], s[200]; uint8_t pl[3] = {1, 2, 3};
    uint32_t n = lk_encode(f, LK_PING, 7, pl, 3);
    CHECK(n == 6 + 3 + 4);
    CHECK(f[0] == 0xA5 && f[1] == 0x5A && f[2] == LK_PING && f[3] == 7 && f[4] == 3 && f[5] == 0);
    /* 前面塞垃圾 (含一个孤立 A5 和 A5 A5), 逐字节喂 */
    uint32_t m = 0; s[m++] = 0x00; s[m++] = 0xA5; s[m++] = 0x11; s[m++] = 0xA5;
    memcpy(s + m, f, n); m += n;
    lk_parser_t p; lk_init(&p, pbuf, sizeof pbuf);
    int got = 0;
    for (uint32_t i = 0; i < m; i++){
        int ev; uint32_t k = lk_feed(&p, s + i, 1, &ev);
        CHECK(k == 1);
        if (ev){ got++; CHECK(p.type == LK_PING && p.seq == 7 && p.len == 3 && memcmp(pbuf, pl, 3) == 0); }
    }
    CHECK(got == 1 && p.frames == 1 && p.crc_err == 0);
}

static void test_bad_crc_then_good_in_one_chunk(void){
    uint8_t s[100]; uint32_t m = 0;
    m += lk_encode(s + m, LK_SEG, 1, (const uint8_t *)"ab", 2);
    s[m - 1] ^= 0xFF;                                   /* 弄坏 crc */
    m += lk_encode(s + m, LK_PRESENT, 2, (const uint8_t *)"\x05\x00", 2);
    m += lk_encode(s + m, LK_PING, 3, 0, 0);            /* 空 payload */
    lk_parser_t p; lk_init(&p, pbuf, sizeof pbuf);
    const uint8_t *d = s; uint32_t n = m; int types[4], nt = 0;
    while (n){ int ev; uint32_t k = lk_feed(&p, d, n, &ev); d += k; n -= k; if (ev && nt < 4) types[nt++] = p.type; }
    CHECK(nt == 2 && types[0] == LK_PRESENT && types[1] == LK_PING);
    CHECK(p.crc_err == 1 && p.frames == 2);
}

static void test_len_over_cap(void){
    uint8_t s[200]; uint8_t big[100] = {0};
    uint32_t m = lk_encode(s, LK_RECT, 1, big, 100);
    m += lk_encode(s + m, LK_PING, 2, 0, 0);
    lk_parser_t p; lk_init(&p, pbuf, sizeof pbuf);      /* cap 64 < 100 */
    const uint8_t *d = s; uint32_t n = m; int pings = 0;
    while (n){ int ev; uint32_t k = lk_feed(&p, d, n, &ev); d += k; n -= k; if (ev && p.type == LK_PING) pings++; }
    CHECK(p.len_err == 1 && pings == 1);
}

static void test_rect_apply_and_fbcrc(void){
    static uint16_t fb[8 * 4];
    memset(fb, 0, sizeof fb);
    uint8_t pl[8 + 3 * 2 * 2]; uint32_t y0 = 99, y1 = 0;
    lk_wr16(pl, 1); lk_wr16(pl + 2, 1); lk_wr16(pl + 4, 3); lk_wr16(pl + 6, 2);   /* 奇数 x: 走 u16 路径 */
    for (int i = 0; i < 6; i++) lk_wr16(pl + 8 + 2 * i, 0x1000u + (unsigned)i);
    CHECK(lk_rect_apply(fb, 8, 4, pl, sizeof pl, &y0, &y1) == LK_OK);
    CHECK(fb[1 * 8 + 1] == 0x1000 && fb[1 * 8 + 3] == 0x1002 && fb[2 * 8 + 1] == 0x1003 && fb[2 * 8 + 3] == 0x1005);
    CHECK(fb[1 * 8 + 0] == 0 && fb[1 * 8 + 4] == 0 && fb[3 * 8 + 1] == 0);
    CHECK(y0 == 1 && y1 == 3);
    uint8_t q[8]; uint32_t c = 0;
    memcpy(q, pl, 8);
    CHECK(lk_fb_crc(fb, 8, 4, q, 8, &c) == LK_OK);
    CHECK(c == lk_crc32(pl + 8, 12));                    /* 回读 crc = 发送像素的 crc */
    /* 偶数 x 偶数 w: 走 u32 路径 */
    uint8_t pl2[8 + 4 * 1 * 2] __attribute__((aligned(4)));
    lk_wr16(pl2, 4); lk_wr16(pl2 + 2, 0); lk_wr16(pl2 + 4, 4); lk_wr16(pl2 + 6, 1);
    for (int i = 0; i < 4; i++) lk_wr16(pl2 + 8 + 2 * i, 0xA000u + (unsigned)i);
    CHECK(lk_rect_apply(fb, 8, 4, pl2, sizeof pl2, &y0, &y1) == LK_OK);
    CHECK(fb[4] == 0xA000 && fb[7] == 0xA003 && y0 == 0);
    /* 越界 / 长度不对: 拒绝且不写 */
    lk_wr16(pl2, 5);
    CHECK(lk_rect_apply(fb, 8, 4, pl2, sizeof pl2, &y0, &y1) == LK_EPARAM);
    lk_wr16(pl2, 4);
    CHECK(lk_rect_apply(fb, 8, 4, pl2, sizeof pl2 - 2, &y0, &y1) == LK_EPARAM);
    CHECK(lk_fb_crc(fb, 8, 4, q, 7, &c) == LK_EPARAM);
}

/* COPY: 同一块 fb 里把矩形搬到另一位置, 允许重叠 (上下左右) */
static void fill_seq(uint16_t *fb, uint32_t n){ for (uint32_t i = 0; i < n; i++) fb[i] = (uint16_t)i; }
static void copy_pl(uint8_t *pl, uint32_t sx, uint32_t sy, uint32_t w, uint32_t h, uint32_t dx, uint32_t dy){
    lk_wr16(pl, sx); lk_wr16(pl + 2, sy); lk_wr16(pl + 4, w); lk_wr16(pl + 6, h); lk_wr16(pl + 8, dx); lk_wr16(pl + 10, dy);
}
static void test_copy_apply(void){
    static uint16_t fb[8 * 6];
    uint8_t pl[12]; uint32_t y0, y1;
    /* 向上滚 2 行 (重叠): 行 2..5 -> 行 0..3 */
    fill_seq(fb, 48); y0 = 99; y1 = 0;
    copy_pl(pl, 0, 2, 8, 4, 0, 0);
    CHECK(lk_copy_apply(fb, 8, 6, pl, 12, &y0, &y1) == LK_OK);
    CHECK(fb[0] == 16 && fb[7] == 23 && fb[3 * 8 + 5] == 5 * 8 + 5 && fb[4 * 8] == 32);
    CHECK(y0 == 0 && y1 == 4);
    /* 向下滚 2 行 (重叠): 行 0..3 -> 行 2..5, 必须从下往上拷 */
    fill_seq(fb, 48); y0 = 99; y1 = 0;
    copy_pl(pl, 1, 0, 6, 4, 1, 2);
    CHECK(lk_copy_apply(fb, 8, 6, pl, 12, &y0, &y1) == LK_OK);
    CHECK(fb[2 * 8 + 1] == 1 && fb[5 * 8 + 6] == 3 * 8 + 6 && fb[2 * 8 + 0] == 16 && fb[5 * 8 + 7] == 47);
    CHECK(y0 == 2 && y1 == 6);
    /* 同一行内右移 1 (重叠), 奇数宽度 */
    fill_seq(fb, 48);
    copy_pl(pl, 0, 1, 5, 1, 1, 1);
    CHECK(lk_copy_apply(fb, 8, 6, pl, 12, &y0, &y1) == LK_OK);
    CHECK(fb[8] == 8 && fb[9] == 8 && fb[13] == 12 && fb[14] == 14);
    /* 同一行内左移 2, 偶数对齐 (u32 路径) */
    fill_seq(fb, 48);
    copy_pl(pl, 2, 0, 6, 1, 0, 0);
    CHECK(lk_copy_apply(fb, 8, 6, pl, 12, &y0, &y1) == LK_OK);
    CHECK(fb[0] == 2 && fb[5] == 7 && fb[6] == 6 && fb[7] == 7);
    /* 越界 / 长度不对 / 空 */
    copy_pl(pl, 0, 3, 8, 4, 0, 0);
    CHECK(lk_copy_apply(fb, 8, 6, pl, 12, &y0, &y1) == LK_EPARAM);
    copy_pl(pl, 0, 0, 8, 2, 1, 0);
    CHECK(lk_copy_apply(fb, 8, 6, pl, 12, &y0, &y1) == LK_EPARAM);
    copy_pl(pl, 0, 0, 0, 2, 0, 0);
    CHECK(lk_copy_apply(fb, 8, 6, pl, 12, &y0, &y1) == LK_EPARAM);
    copy_pl(pl, 0, 0, 2, 2, 0, 0);
    CHECK(lk_copy_apply(fb, 8, 6, pl, 11, &y0, &y1) == LK_EPARAM);
}

int main(void){
    test_crc(); test_roundtrip_bytewise_and_garbage(); test_bad_crc_then_good_in_one_chunk();
    test_len_over_cap(); test_rect_apply_and_fbcrc(); test_copy_apply();
    printf(fails ? "%d FAILED\n" : "all link proto tests passed\n", fails);
    return fails != 0;
}
