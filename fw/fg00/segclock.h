/*
 * 段码屏本地时钟 (纯逻辑, 只写 fg_fb 显存): fw/app.c 和 tools/fwsim/simdev.h 共用, 对拍脚本能看到 HELLO 带的时间
 * 落到设备上是什么 (clk_sod) 和段码屏上显示的是什么 (fg_fb)。
 * 主区 "------"; HELLO 带了时间后小时钟走 hh:mm:ss。主机发过 SEG 后 SEG_HOST_HOLD_US 内固件不画自己的段码。
 * 包含方先提供: now_us, fg_flush, SEG_HOST_HOLD_US, 以及 fgbuf.h / segmap.h (fg_fill / fg_digit / fg_seg / fg_hex)。
 */
#ifndef FG00_SEGCLOCK_H
#define FG00_SEGCLOCK_H
#include <stdint.h>

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

#endif
