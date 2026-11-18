/*
 * DIAG (0x50): 只给诊断用的命令, 不属于稳定协议。当前用于"双缓冲可不可行"的调查:
 *   内核堆还剩多少 / 最大能 malloc 多大 / 第二块 fb / 切换扫描基址 / 读 LCDC 寄存器 / 测帧事件周期与相位。
 * FILL/FLIP/SCAN 会让两块缓冲内容不一致, 诊断完主机应推一次整帧。
 * 请求: u8 op + 参数;  回包 DIAG_R (0xD0): u8 op, u8 status, u16 n, n × u32。
 * 依赖 app.c 里的 reply()/FB_ADDR/LCD_*, 只在 app.c 里 include。
 */
#ifndef MONITA_LINK_DIAG_H
#define MONITA_LINK_DIAG_H

#include "lcdc.h"

#define LK_DIAG   0x50u
#define LK_DIAG_R 0xD0u

enum {
    DG_MEMQ   = 0,   /* -> rc, ql_dev_memory_size_query 输出的前 6 个字 */
    DG_MAXBLK = 1,   /* -> 最大可 malloc 字节数 (4 KiB 粒度二分, 试完即 free) */
    DG_ALLOC2 = 2,   /* -> 第二块 fb 地址, malloc 原始指针, 当前 front, back (开机已分配; 失败则 status=1) */
    DG_SCAN   = 3,   /* u8 which (0 原 fb, 1 第二块) -> 切扫描基址: 0, 耗时 us, 0x08807034/38 回读 */
    DG_PEEK   = 4,   /* u32 addr, u8 n(≤16) -> n 个字 (只允许 0x08807000..0x0880703c; 实测读 0x08807040 起会让设备复位) */
    DG_FILL   = 5,   /* u8 which, u16 c_top, u16 c_bottom -> 上半/下半两色填满并 clean, 耗时 us */
    DG_FLIP   = 6,   /* u16 count, u32 interval_us, u8 mode(0 write_screen, 1 直接写 0x08807034, 2 先等帧事件 0x04 再 write_screen) -> 总耗时 us */
    DG_EVT    = 7,   /* u32 mask, u8 rounds(≤16) -> W1C 清 0x08807008 的 mask 位 (只允许 0x2c 内的位; 动 0x40/0x80 实测会把设备弄复位), 计时等它再置位: 每轮 us (超时 100 ms 记 0) */
    DG_DBUF   = 10,  /* u8 on -> 开/关双缓冲 (A/B 对比撕裂用); 返回 front, back */
    DG_PHASE  = 9,   /* u8 rounds(≤5) -> 每轮: 清 0x2c 三位, 等 0x04 置位为 t0, 再记 0x08 / 0x20 相对 t0 的置位时刻 (us) */
};


static void diag_reply(uint8_t op, uint8_t status, const uint32_t *w, uint32_t n){
    uint8_t b[4 + 4 * 16];
    b[0] = op; b[1] = status; lk_wr16(b + 2, n);
    for (uint32_t i = 0; i < n; i++) lk_wr32(b + 4 + 4 * i, w[i]);
    reply(LK_DIAG_R, b, 4 + 4 * n);
}

static uint32_t rd32le(const uint8_t *p){ return (uint32_t)lk_rd16(p) | ((uint32_t)lk_rd16(p + 2) << 16); }

#include "zc_diag.h"   /* DIAG 20..25: 零拷贝滚动实验 (fw v3) */

static void fill_two(uint32_t base, uint16_t top, uint16_t bot){
    volatile uint32_t *p = (volatile uint32_t *)base;
    uint32_t half = FB_SIZE / 8u, a = ((uint32_t)top << 16) | top, b = ((uint32_t)bot << 16) | bot;
    for (uint32_t i = 0; i < half; i++) p[i] = a;
    for (uint32_t i = half; i < FB_SIZE / 4u; i++) p[i] = b;
    dcache_clean(base, FB_SIZE);
}

static void on_diag(const uint8_t *pl, uint32_t n){
    if (n < 1){ diag_reply(0, LK_EPARAM, 0, 0); return; }
    uint8_t op = pl[0]; uint32_t w[16]; uint32_t k = 0;
    switch (op){
    case DG_MEMQ: {
        uint32_t q[8] = {0};
        w[0] = (uint32_t)ql_dev_memory_size_query(q);
        for (int i = 0; i < 6; i++) w[1 + i] = q[i];
        k = 7; break;
    }
    case DG_MAXBLK: {
        uint32_t lo = 0, hi = 16u << 20;               /* PSRAM 总共 16 MiB, 上限不会更大 */
        while (hi - lo > 4096u){
            uint32_t mid = ((lo + hi) >> 1) & ~4095u;
            void *p = malloc(mid);
            if (p){ free(p); lo = mid; } else hi = mid;
            ql_dev_feed_wdt();
        }
        w[0] = lo; k = 1; break;
    }
    case DG_ALLOC2:                                    /* 第二块 fb 开机时已分配 (dbuf_init) */
        w[0] = g_fb2; w[1] = g_fb2_raw; w[2] = g_front; w[3] = g_back; k = 4;
        diag_reply(op, g_fb2 ? LK_OK : LK_EPARAM, w, k); return;
    case DG_SCAN: {
        uint32_t fb = (n >= 2 && pl[1]) ? g_fb2 : FB_ADDR;
        if (!fb){ diag_reply(op, LK_EPARAM, 0, 0); return; }
        uint32_t t0 = now_us();
        lcd_show(fb);
        w[0] = 0;
        w[1] = now_us() - t0; w[2] = rd32(LCDC_LAYER); w[3] = rd32(LCDC_STRIDE); k = 4; break;
    }
    case DG_PEEK: {
        if (n < 6){ diag_reply(op, LK_EPARAM, 0, 0); return; }
        uint32_t a = rd32le(pl + 1) & ~3u, c = pl[5] > 16 ? 16u : pl[5];
        if (a < LCDC_BASE || a + 4u * c > LCDC_BASE + 0x40u){ diag_reply(op, LK_EPARAM, 0, 0); return; }
        for (uint32_t i = 0; i < c; i++) w[i] = rd32(a + 4u * i);
        k = c; break;
    }
    case DG_FILL: {
        if (n < 6){ diag_reply(op, LK_EPARAM, 0, 0); return; }
        uint32_t fb = pl[1] ? g_fb2 : FB_ADDR;
        if (!fb){ diag_reply(op, LK_EPARAM, 0, 0); return; }
        uint32_t t0 = now_us();
        fill_two(fb, lk_rd16(pl + 2), lk_rd16(pl + 4));
        w[0] = now_us() - t0; k = 1; break;
    }
    case DG_FLIP: {
        if (n < 8 || !g_fb2){ diag_reply(op, LK_EPARAM, 0, 0); return; }
        uint32_t cnt = lk_rd16(pl + 1), iv = rd32le(pl + 3), mode = pl[7];
        uint32_t t0 = now_us(), worst = 0;
        for (uint32_t i = 0; i < cnt; i++){
            uint32_t fb = (i & 1u) ? FB_ADDR : g_fb2, t1 = now_us();
            if (mode == 2) lcd_wait_frame(LCDC_EVT_FRAME, 60000u);
            if (mode == 1) wr32(LCDC_LAYER, fb); else ql_mipi_lcd_write_screen((void *)fb);
            uint32_t d = now_us() - t1; if (d > worst) worst = d;
            if (iv >= 2000u) ql_rtos_task_sleep_ms(udiv(iv, 1000u, 0));
            else { uint32_t t2 = now_us(); while (now_us() - t2 < iv) {} }
            if ((i & 63u) == 0) ql_dev_feed_wdt();
        }
        lcd_show(cnt & 1u ? g_fb2 : FB_ADDR);          /* 让 front/back 记账和最后一次翻页一致 */
        w[0] = now_us() - t0; w[1] = worst; w[2] = rd32(LCDC_LAYER); k = 3; break;
    }
    case DG_EVT: {
        if (n < 6){ diag_reply(op, LK_EPARAM, 0, 0); return; }
        uint32_t m = rd32le(pl + 1) & LCDC_EVT_SAFE, r = pl[5] > 15 ? 15u : pl[5];
        w[0] = rd32(LCDC_EVT); k = 1;
        wr32(LCDC_EVT, m);                             /* W1C */
        for (uint32_t i = 0; i < r; i++){
            uint32_t t0 = now_us(), d = 0;
            while (now_us() - t0 < 100000u){ if (rd32(LCDC_EVT) & m){ d = now_us() - t0; break; } }
            wr32(LCDC_EVT, m);
            w[k++] = d;
        }
        break;
    }
    case DG_DBUF:
        if (n >= 2 && pl[1] && g_fb2){
            uint32_t other = g_front == FB_ADDR ? g_fb2 : FB_ADDR;
            copy_rows(other, g_front, 0, LCD_H);
            g_back = other;
        } else {
            g_back = 0;
        }
        w[0] = g_front; w[1] = g_back; k = 2; break;
    case DG_PHASE: {
        uint32_t r = (n >= 2 && pl[1] <= 5) ? pl[1] : 3u;
        for (uint32_t i = 0; i < r; i++){
            wr32(LCDC_EVT, LCDC_EVT_SAFE);
            uint32_t t0 = now_us(), ta = 0, tb = 0, tc = 0, got = 0;
            while (now_us() - t0 < 100000u && got != 7u){
                uint32_t e = rd32(LCDC_EVT), t = now_us();
                if (!(got & 1u) && (e & 0x04u)){ got |= 1u; ta = t; }
                if (!(got & 2u) && (e & 0x08u)){ got |= 2u; tb = t; }
                if (!(got & 4u) && (e & 0x20u)){ got |= 4u; tc = t; }
            }
            w[k++] = tb - ta; w[k++] = tc - ta; w[k++] = got;
        }
        break;
    }
    default:
        if (zc_on_diag(op, pl, n)) return;
        diag_reply(op, LK_EUNSUP, 0, 0); return;
    }
    diag_reply(op, LK_OK, w, k);
}

#endif
