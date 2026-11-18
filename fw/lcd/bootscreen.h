/*
 * 开机版权页 (fw v7): 纯画图, 给定 800x1280 RGB565 fb 指针就能画, 主机可测 (fw/link/test_bootscreen.c 对照
 * tools/bootscreen/render_ref.py 逐像素比)。不碰硬件、不 clean cache、不管缓冲轮转, 这些由 app.c 做。
 *
 * 布局照设计稿 (render_ref.py): 5x7 像素 logo "MONITA" (逐行渐变 + 右下投影) + 下划线, 右上绿框徽章,
 * 其余文字用 EDK2 UEFI 8x19 字体 2 倍放大 (每字 16x38)。动态字段见 bs_info_t。
 * Link 行开机是黄色 WAIT, 收到 HELLO 后 bs_draw_link() 只重画这一行的状态。
 * 只用加减和移位 (A5 没有硬件除法, 固件不链接 libgcc)。
 */
#ifndef MONITA_BOOTSCREEN_H
#define MONITA_BOOTSCREEN_H
#include <stdint.h>
#include "font_uefi.h"

#define BS_W  800u
#define BS_H  1280u
#define BS_S  2u                                /* 字体放大倍数 */
#define BS_CW (FONT_UEFI_W * BS_S)              /* 16 */
#define BS_CH (FONT_UEFI_H * BS_S)              /* 38 */
#define BS_LH (BS_CH + 4u)                      /* 行距 42 */
#define BS_X0 44u
#define BS_TAIL_X (BS_X0 + 29u * BS_CW)         /* 状态列 (OK / WAIT ...) */
#define BS_STAT_Y 388u                          /* 第一行状态 (CPU) 的 y: 200 + 4 行 + 20 */
#define BS_LINK_Y (BS_STAT_Y + 6u * BS_LH)      /* Link 行 */

#define BS_RGB(r, g, b) ((uint16_t)((((r) >> 3) << 11) | (((g) >> 2) << 5) | ((b) >> 3)))
#define BS_WHITE BS_RGB(0xff, 0xff, 0xff)
#define BS_GRAY  BS_RGB(0xc8, 0xc8, 0xc8)
#define BS_DIM   BS_RGB(0x7a, 0x7a, 0x7a)
#define BS_OK    BS_RGB(0x5f, 0xd3, 0x5f)
#define BS_WAIT  BS_RGB(0xe8, 0xc7, 0x6a)
#define BS_FAIL  BS_RGB(0xff, 0x5f, 0x5f)
#define BS_FOOT  BS_RGB(0x44, 0x44, 0x44)

typedef struct {
    uint32_t fw_ver;
    const char *date;        /* "YYYY-MM-DD" (bs_date 从 __DATE__ 转) */
    uint32_t free_kib;       /* 内核堆剩余 KiB; 0xFFFFFFFF = 查不到 */
    uint32_t nbuf;           /* 缓冲块数 1..3 */
    int seg_ok;              /* 段码屏初始化成功 */
    int link_ok;             /* 收到过 HELLO */
} bs_info_t;

static void bs_rect(uint16_t *fb, uint32_t x, uint32_t y, uint32_t w, uint32_t h, uint16_t c){
    uint32_t ye = y + h > BS_H ? BS_H : y + h, xe = x + w > BS_W ? BS_W : x + w;
    for (uint32_t r = y; r < ye; r++)
        for (uint32_t k = x; k < xe; k++) fb[r * BS_W + k] = c;
}

static void bs_text(uint16_t *fb, uint32_t x, uint32_t y, const char *s, uint16_t c){
    for (; *s; s++, x += BS_CW){
        uint32_t ch = (uint8_t)*s;
        if (ch < FONT_UEFI_FIRST || ch > FONT_UEFI_LAST) ch = '?';
        const uint8_t *g = font_uefi[ch - FONT_UEFI_FIRST];
        for (uint32_t r = 0; r < FONT_UEFI_H; r++)
            for (uint32_t b = 0; b < 8u; b++)
                if (g[r] & (0x80u >> b)) bs_rect(fb, x + b * BS_S, y + r * BS_S, BS_S, BS_S, c);
    }
}

/* 小字符串拼接 (不依赖 libc) */
static char *bs_cat(char *d, const char *s){ while (*s) *d++ = *s++; *d = 0; return d; }
static char *bs_u32(char *d, uint32_t v){          /* 十进制, 只用减法 */
    static const uint32_t p10[10] = { 1000000000u, 100000000u, 10000000u, 1000000u, 100000u, 10000u, 1000u, 100u, 10u, 1u };
    int started = 0;
    for (int i = 0; i < 10; i++){
        char c = '0';
        while (v >= p10[i]){ v -= p10[i]; c++; }
        if (c != '0' || started || i == 9){ *d++ = c; started = 1; }
    }
    *d = 0; return d;
}
static char *bs_pad(char *b, char *d, uint32_t n){ while ((uint32_t)(d - b) < n) *d++ = ' '; *d = 0; return d; }

/* __DATE__ ("Nov 18 2026" / "Nov  8 2026") -> "2026-11-18" (out 至少 11 字节) */
static void bs_date(const char *in, char *out){
    static const char mon[] = "JanFebMarAprMayJunJulAugSepOctNovDec";
    int m = 0;
    for (int i = 0; i < 12; i++)
        if (in[0] == mon[3 * i] && in[1] == mon[3 * i + 1] && in[2] == mon[3 * i + 2]) m = i + 1;
    out[0] = in[7]; out[1] = in[8]; out[2] = in[9]; out[3] = in[10]; out[4] = '-';
    out[5] = m >= 10 ? '1' : '0'; out[6] = (char)('0' + (m >= 10 ? m - 10 : m)); out[7] = '-';
    out[8] = in[4] == ' ' ? '0' : in[4]; out[9] = in[5]; out[10] = 0;
}

/* 5x7 logo 字模, 每行低 5 位, bit4 = 最左 */
static const uint8_t bs_logo[6][7] = {
    { 0x11, 0x1B, 0x15, 0x15, 0x11, 0x11, 0x11 },   /* M */
    { 0x0E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E },   /* O */
    { 0x11, 0x19, 0x15, 0x13, 0x11, 0x11, 0x11 },   /* N */
    { 0x1F, 0x04, 0x04, 0x04, 0x04, 0x04, 0x1F },   /* I */
    { 0x1F, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04 },   /* T */
    { 0x0E, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11 },   /* A */
};
static const uint16_t bs_grad[7] = {
    BS_RGB(0xff, 0xd1, 0xe0), BS_RGB(0xf2, 0xa7, 0xc0), BS_RGB(0xe0, 0x87, 0xa6), BS_RGB(0xc9, 0x6e, 0x8f),
    BS_RGB(0xa5, 0x62, 0x7e), BS_RGB(0x8a, 0x4f, 0x69), BS_RGB(0x6e, 0x3d, 0x54),
};

static void bs_logo_draw(uint16_t *fb){
    const uint32_t x0 = 44u, y0 = 56u, p = 13u;
    for (uint32_t i = 0; i < 6u; i++)
        for (uint32_t r = 0; r < 7u; r++)
            for (uint32_t c = 0; c < 5u; c++)
                if (bs_logo[i][r] & (0x10u >> c)){
                    uint32_t bx = x0 + i * 6u * p + c * p, by = y0 + r * p;
                    bs_rect(fb, bx + 6u, by + 6u, p - 1u, p - 1u, BS_RGB(0x2a, 0x1a, 0x22));   /* 投影 */
                    bs_rect(fb, bx, by, p - 1u, p - 1u, bs_grad[r]);
                }
    bs_rect(fb, 44u, 156u, 456u, 4u, BS_RGB(0xa5, 0x62, 0x7e));
    bs_rect(fb, 44u, 162u, 456u, 2u, BS_RGB(0x5d, 0x7f, 0x99));
}

static void bs_badge(uint16_t *fb){
    const uint32_t bx = 610u, by = 56u;
    for (uint32_t t = 0; t < 3u; t++){
        bs_rect(fb, bx + t, by + t, 146u - 2u * t, 1u, BS_OK); bs_rect(fb, bx + t, by + 106u - t, 146u - 2u * t, 1u, BS_OK);
        bs_rect(fb, bx + t, by + t, 1u, 106u - 2u * t, BS_OK); bs_rect(fb, bx + 145u - t, by + t, 1u, 106u - 2u * t, BS_OK);
    }
    bs_text(fb, bx + 41u, by + 12u, "CS80", BS_OK);
    bs_text(fb, bx + 9u, by + 50u, "R05 NATV", BS_OK);
}

static void bs_tail(uint16_t *fb, uint32_t y, const char *s, uint16_t c){ bs_text(fb, BS_TAIL_X, y, s, c); }

/* 整屏 (先清黑) */
static void bs_draw(uint16_t *fb, const bs_info_t *in){
    char b[48], *e;
    bs_rect(fb, 0, 0, BS_W, BS_H, 0);
    bs_logo_draw(fb);
    bs_badge(fb);

    uint32_t y = 200u;
    e = bs_cat(b, "Monita-CS80 firmware v"); bs_u32(e, in->fw_ver);
    bs_text(fb, BS_X0, y, b, BS_WHITE); y += BS_LH;
    e = bs_cat(b, "Build "); bs_cat(e, in->date);
    bs_text(fb, BS_X0, y, b, BS_GRAY); y += BS_LH;
    bs_text(fb, BS_X0, y, "Copyright (c) 2026 uaih3k9x", BS_GRAY); y += BS_LH;
    bs_text(fb, BS_X0, y, "PolyForm Noncommercial 1.0.0", BS_GRAY); y += BS_LH;
    y += 20u;

    bs_text(fb, BS_X0, y, "CPU   Cortex-A5 UIS8910DM .. ", BS_GRAY); bs_tail(fb, y, "OK", BS_OK); y += BS_LH;
    bs_text(fb, BS_X0, y, "Base  EC600U R05 OpenCPU ... ", BS_GRAY); bs_tail(fb, y, "OK", BS_OK); y += BS_LH;
    e = bs_cat(b, "PSRAM 16 MiB  free ");
    e = in->free_kib == 0xFFFFFFFFu ? bs_cat(e, "?") : bs_u32(e, in->free_kib);
    e = bs_cat(e, " KiB"); bs_pad(b, e, 29u);
    bs_text(fb, BS_X0, y, b, BS_GRAY); bs_tail(fb, y, "OK", BS_OK); y += BS_LH;
    bs_text(fb, BS_X0, y, "Panel MIPI-DSI 800x1280 .... ", BS_GRAY); bs_tail(fb, y, "OK", BS_OK); y += BS_LH;
    e = bs_cat(b, "FB    ");
    e = bs_cat(e, in->nbuf == 3u ? "triple" : in->nbuf == 2u ? "double" : "single");
    e = bs_cat(e, " buffer x"); e = bs_u32(e, in->nbuf); bs_cat(e, " ..... ");
    bs_text(fb, BS_X0, y, b, BS_GRAY);
    if (in->nbuf == 3u) bs_tail(fb, y, "OK", BS_OK); else bs_tail(fb, y, "WARN", BS_WAIT);
    y += BS_LH;
    bs_text(fb, BS_X0, y, "Seg   FG00 segment LCD ..... ", BS_GRAY);
    if (in->seg_ok) bs_tail(fb, y, "OK", BS_OK); else bs_tail(fb, y, "FAIL", BS_FAIL);
    y += BS_LH;
    bs_text(fb, BS_X0, y, "Link  USB port 4 ........... ", BS_GRAY);
    if (in->link_ok) bs_tail(fb, y, "OK", BS_OK); else bs_tail(fb, y, "WAIT", BS_WAIT);
    y += BS_LH + 20u;

    bs_text(fb, BS_X0, y, "Unofficial firmware for", BS_DIM); y += BS_LH;
    bs_text(fb, BS_X0, y, "retired or owned devices.", BS_DIM); y += BS_LH;
    bs_text(fb, BS_X0, y, "Not affiliated with Lakala", BS_DIM); y += BS_LH;
    bs_text(fb, BS_X0, y, "or Quectel. No payment.", BS_DIM); y += BS_LH + 20u;
    bs_text(fb, BS_X0, y, "Waiting for host_", BS_GRAY);
    bs_text(fb, BS_X0, BS_H - BS_CH - 40u, "UEFI font: EDK2 (BSD-2-Clause-Patent)", BS_FOOT);
}

/* 只重画 Link 行的状态 (清掉 4 个字宽再写)。返回的 y0, y1 = 改过的行区间 [y0, y1), 给调用方 clean cache */
static void bs_draw_link(uint16_t *fb, int ok, uint32_t *y0, uint32_t *y1){
    bs_rect(fb, BS_TAIL_X, BS_LINK_Y, 4u * BS_CW, BS_CH, 0);
    if (ok) bs_tail(fb, BS_LINK_Y, "OK", BS_OK); else bs_tail(fb, BS_LINK_Y, "WAIT", BS_WAIT);
    *y0 = BS_LINK_Y; *y1 = BS_LINK_Y + BS_CH;
}

#endif
