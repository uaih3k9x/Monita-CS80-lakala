/*
 * 零拷贝滚动上机实验用的 DIAG 子命令 20..25 (fw v3)。背景与实机结论见 docs/r05-notes.md §4。
 * 由 diag.h 在 diag_reply/rd32le 之后 include; on_diag() 的 default 分支先调 zc_on_diag()。
 * 依赖 app.c 里的: now_us, g_front, g_fb2, FB_ADDR, dcache_clean; lcdc.h 的 rd32/wr32/lcd_wait_frame。
 *
 * 安全规则 (理由见 docs/r05-notes.md §4):
 *   - 读: 只读 0x00..0x3c 和 0x50。0x40 起其余一律不读 (实测某个读会复位, 具体是哪个没定位)。
 *   - 写: 只写 0x34/0x38 (+ arm 之后 0x3c/0x40/0x44/0x48), 值过 zc_reg_write_ok。
 *         时序/格式/尺寸/控制 (0x2c 0x30 0x4c 0x50) 和 DSI (0x4xx 0x5xx) 不碰。事件寄存器 W1C 仍只用 0x2c 内的位。
 *   - 每条命令结束都把 0x34 恢复成 g_front; disarm 把 0x3c/0x40 恢复成 R03 内核初始化写的值 (0 / 0x06400000)。
 *   - 单次忙等不超过一帧 (≤ 30 ms); 分屏循环每帧喂狗, 帧数上限 300 (≈ 8 s)。
 */
#ifndef MONITA_ZC_DIAG_H
#define MONITA_ZC_DIAG_H
#include "zc_logic.h"

enum {
    DG_ZC_ARM   = 20,  /* u8 on -> 0; off 时恢复 0x3c=0, 0x40=0x06400000, 0x34=front */
    DG_ZC_PEEK  = 21,  /* u32 off -> 值 (zc_reg_read_ok 之外 EPARAM) */
    DG_ZC_POKE  = 22,  /* u32 off, u32 val, u16 hold_ms -> 写入耗时 us, 0x38 回读, 0x3c 回读; hold_ms>0 则保持后恢复 */
    DG_ZC_LINE  = 23,  /* u8 n(≤15) -> 等 0x04 置位后连续采样 0x24: 每个 (t_us<<16 | val&0xffff) */
    DG_ZC_RULER = 24,  /* u16 tint -> 在后台缓冲 (不在扫描的那块) 填尺子图案并 clean -> 耗时 us, 缓冲地址 */
    DG_ZC_SPLIT = 25,  /* u16 frames, u16 t1_us, u16 t2_us, u16 top -> 帧数, t1 最晚迟到 us, t2 最晚迟到 us, 超时帧数 */
};

#define ZC_LCDC(off) (LCDC_BASE + (off))
static int zc_armed;
static const zc_range_t zc_psram[1] = {{ 0x80000000u, 0x81000000u }};

/* 不在扫描的那块 (双缓冲时就是后台); 没有第二块时返回 0 */
static uint32_t zc_other(void){
    if (!g_fb2) return 0;
    return g_front == FB_ADDR ? g_fb2 : FB_ADDR;
}

static void zc_restore(void){
    if (zc_armed){
        wr32(ZC_LCDC(ZC_R_PITCH1), 0x06400000u);   /* R03 内核 LCD 初始化写的值: 行跨, 不带使能 */
        wr32(ZC_LCDC(ZC_R_BASE1), 0u);             /* R03 写 panel+0x2c, 面板描述符里是 0 */
    }
    wr32(ZC_LCDC(ZC_R_PITCH0), zc_pitch_val(ZC_STRIDE, 1u));
    wr32(ZC_LCDC(ZC_R_BASE0), g_front);
}

/* 忙等到 t0 + dt (us)。dt 大于 3 ms 时先睡掉大头, 只留最后 ~2 ms 自旋。返回迟到多少 us */
static uint32_t zc_until(uint32_t t0, uint32_t dt){
    uint32_t el = now_us() - t0;
    if (dt > el + 3000u) ql_rtos_task_sleep_ms((dt - el - 2000u) >> 10);
    while (now_us() - t0 < dt) {}
    return now_us() - t0 - dt;
}

/* 返回 1 = 已处理 (已回包) */
static int zc_on_diag(uint8_t op, const uint8_t *pl, uint32_t n){
    uint32_t w[16]; uint32_t k = 0;
    switch (op){
    case DG_ZC_ARM:
        if (n < 2){ diag_reply(op, LK_EPARAM, 0, 0); return 1; }
        if (!pl[1]) zc_restore();
        zc_armed = pl[1] != 0;
        if (!zc_armed) wr32(ZC_LCDC(ZC_R_BASE0), g_front);
        w[k++] = (uint32_t)zc_armed; break;
    case DG_ZC_PEEK: {
        if (n < 5){ diag_reply(op, LK_EPARAM, 0, 0); return 1; }
        uint32_t off = rd32le(pl + 1);
        if (!zc_reg_read_ok(off)){ diag_reply(op, LK_EPARAM, 0, 0); return 1; }
        w[k++] = rd32(ZC_LCDC(off)); break;
    }
    case DG_ZC_POKE: {
        if (n < 11){ diag_reply(op, LK_EPARAM, 0, 0); return 1; }
        uint32_t off = rd32le(pl + 1), val = rd32le(pl + 5), hold = lk_rd16(pl + 9);
        if (!zc_reg_write_ok(off, val, zc_armed, zc_psram, 1u)){ diag_reply(op, LK_EPARAM, 0, 0); return 1; }
        uint32_t t0 = now_us();
        wr32(ZC_LCDC(off), val);
        w[k++] = now_us() - t0;
        w[k++] = rd32(ZC_LCDC(ZC_R_PITCH0));
        w[k++] = rd32(ZC_LCDC(ZC_R_BASE1));
        if (hold){
            for (uint32_t i = 0; i < hold; i += 100u){ ql_rtos_task_sleep_ms(hold - i < 100u ? hold - i : 100u); ql_dev_feed_wdt(); }
            zc_restore();
        }
        break;
    }
    case DG_ZC_LINE: {
        uint32_t m = (n >= 2 && pl[1] && pl[1] <= 15u) ? pl[1] : 15u;
        uint32_t t0 = lcd_wait_frame(LCDC_EVT_FRAME, 30000u) ? now_us() : 0u;
        if (!t0){ diag_reply(op, LK_EPARAM, 0, 0); return 1; }
        for (uint32_t i = 0; i < m; i++){
            uint32_t v = rd32(ZC_LCDC(0x24u)), t = now_us() - t0;
            w[k++] = (t << 16) | (v & 0xffffu);
            uint32_t t1 = now_us(); while (now_us() - t1 < 1500u) {}   /* 每 1.5 ms 一个点, 共 ≤ 22 ms */
        }
        break;
    }
    case DG_ZC_RULER: {
        uint32_t fb = zc_other();                         /* 只画后台, 不撕当前画面 */
        if (n < 3 || !fb){ diag_reply(op, LK_EPARAM, 0, 0); return 1; }
        uint16_t tint = lk_rd16(pl + 1);
        uint32_t t0 = now_us();
        volatile uint16_t *p = (volatile uint16_t *)fb;
        for (uint32_t r = 0; r < ZC_H; r++){
            for (uint32_t c = 0; c < ZC_W; c++) p[r * ZC_W + c] = zc_ruler_px(r, c, tint);
            if ((r & 127u) == 0) ql_dev_feed_wdt();
        }
        dcache_clean(fb, ZC_H * ZC_STRIDE);
        w[k++] = now_us() - t0; w[k++] = fb; break;
    }
    case DG_ZC_SPLIT: {
        /* 实验 E1/E3: 每帧 [0x04 置位] -> t1 写分屏基址 (以 fb2 当滚动缓冲) -> t2 写回 front。
         * t1/t2 相对 0x04 置位时刻 (us); t2 = 0 表示不写回 (下一帧开头由 0x04 处统一写回)。 */
        uint32_t sb = zc_other();
        if (n < 9 || !sb){ diag_reply(op, LK_EPARAM, 0, 0); return 1; }
        uint32_t frames = lk_rd16(pl + 1), t1 = lk_rd16(pl + 3), t2 = lk_rd16(pl + 5), top = lk_rd16(pl + 7);
        if (frames > 300u) frames = 300u;
        if (t1 >= 26000u || (t2 && (t2 <= t1 || t2 >= 26000u))){ diag_reply(op, LK_EPARAM, 0, 0); return 1; }
        /* 写进去的就是后台缓冲第 top 行的地址; 照片上分屏线下方显示的是尺子第几行 -> 判定 ABS / REL / 锁存 */
        uint32_t b1 = sb + top * ZC_STRIDE;
        if (!zc_base_ok(b1, ZC_H, ZC_STRIDE, zc_psram, 1u)){ diag_reply(op, LK_EPARAM, 0, 0); return 1; }
        uint32_t late1 = 0, late2 = 0, miss = 0, f;
        for (f = 0; f < frames; f++){
            wr32(LCDC_EVT, LCDC_EVT_FRAME);
            uint32_t ts = now_us();
            while (!(rd32(LCDC_EVT) & LCDC_EVT_FRAME) && now_us() - ts < 30000u) {}
            if (!(rd32(LCDC_EVT) & LCDC_EVT_FRAME)){ miss++; continue; }
            uint32_t t0 = now_us();
            if (!t2) wr32(ZC_LCDC(ZC_R_BASE0), g_front);
            uint32_t l = zc_until(t0, t1); wr32(ZC_LCDC(ZC_R_BASE0), b1); if (l > late1) late1 = l;
            if (t2){ l = zc_until(t0, t2); wr32(ZC_LCDC(ZC_R_BASE0), g_front); if (l > late2) late2 = l; }
            ql_dev_feed_wdt();
        }
        zc_restore();
        w[k++] = f; w[k++] = late1; w[k++] = late2; w[k++] = miss; w[k++] = b1; break;
    }
    default:
        return 0;
    }
    diag_reply(op, LK_OK, w, k);
    return 1;
}

#endif
