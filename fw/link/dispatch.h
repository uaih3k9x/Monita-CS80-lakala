/*
 * 拆帧之后的协议分发 (on_frame) + KEY 上报 + "主机在不在"。协议见 docs/protocol.md。
 * 实机 (fw/app.c) 和主机模拟器 (tools/fwsim/simdev.h) 共用这一份: 哪条消息回什么、计数怎么记、沉浸模式拒什么都在这里,
 * 碰硬件的部分 (扫描基址、cache、多缓冲收拢、卷轴分配、段码屏 ...) 由包含方按下面的名字提供。
 *
 * 包含方先定义 (app.c 里就是原来的那些):
 *   状态   g_p (lk_parser_t), g_pl (payload 缓冲), g_lz (RECT_LZ4 暂存区, ≥ MAX_PAYLOAD 字节, 4 字节对齐), g_tx[64],
 *          st {rx_bytes, rects, presents, bad_param, lz4},
 *          g_tb (tb_t), g_imm, g_im (im_t), g_roll (卷轴首地址), g_boot_live, g_flip_now,
 *          g_lw (lw_t), g_key_n, g_key_at, g_keys, g_pwr, g_led, g_led_host, g_keys_ok, g_led_ok, g_lcd_ok, g_lz4_ok (fw v11; 0 = 装成旧固件, 模拟器用),
 *          fg_fb[72] (fg00/fgbuf.h), seg_host_at, seg_host_seen (fg00/segclock.h)
 *   常量   FW_VER, MAX_PAYLOAD, LCD_W, LCD_H, ROW_B, FB_ADDR, PORT, KEY_SAMPLE_MS
 *   函数   osiUpTimeUS, now_us, now_ms, reply, diag_reply, on_diag, dcache_clean, ql_uart_write, keys_hw_raw,
 *          draw_begin, draw_target, do_copy, present, flush_dirty, tb_addr,
 *          imm_enter, imm_exit, imm_make_room, tri_collapse, tri_uncollapse, diag_tri, diag_nbuf,
 *          clk_set, boot_link_ok, seg_host_active, seg_draw_local, fg_flush, lcd_backlight, led_poll,
 *          (fw v10) boot_redraw (退出沉浸模式、把开机版权页重画到屏上, Link 行 WAIT), panel_power (背光 + 段码屏 EN 开关),
 *          usb_unplugged (hotplug 回调报过"拔掉"就返回 1, 每次拔掉只报一次)
 * 地址一律按 uintptr_t 算 (实机 32 位, 模拟器 64 位)。
 *
 * 主机在不在 / 休眠 (fw v10, 状态机在 linkwd.h):
 *   主机走了 (5 s 没有效帧 / USB 拔掉) -> 开机版权页 + 段码屏本地时钟 + 蓝灯心跳, 不发 KEY;
 *   SLEEP 或没主机满 10 min -> 关背光 / 段码屏 / 灯; 任何一帧叫醒 (HELLO 过的话恢复主机的灯), 按键也能叫醒 (显示开机页)。
 */
#ifndef MONITA_LINK_DISPATCH_H
#define MONITA_LINK_DISPATCH_H
#include "proto.h"
#include "tribuf.h"
#include "imm.h"
#include "keys.h"
#include "led.h"
#include "linkwd.h"
#include "lz4.h"

/* ACK: u8 for_type u8 status u16 0 | u32 rx_bytes frames crc_err len_err rects presents bad_param uptime | u32 extra */
static void ack(uint8_t status, uint32_t extra){
    uint8_t *b = g_tx;
    b[0] = g_p.type; b[1] = status; b[2] = 0; b[3] = 0;
    uint32_t v[9] = { st.rx_bytes, g_p.frames, g_p.crc_err, g_p.len_err, st.rects, st.presents, st.bad_param,
                      (uint32_t)(osiUpTimeUS() >> 10), extra };     /* uptime 以 1.024 ms 为单位 (免 64 位除法) */
    for (int i = 0; i < 9; i++) lk_wr32(b + 4 + 4 * i, v[i]);
    reply(LK_ACK, b, 40);
}

/* HELLO_ACK 的 caps; 运行中会变 (沉浸模式里没有多缓冲, IMM_EXIT / DIAG 32 之后块数可能不同) */
static uint32_t caps(void){
    return LK_CAP_FBCRC | LK_CAP_COPY | (g_tb.n > 1 ? LK_CAP_DOUBLEBUF : 0u) | (g_tb.n > 2 ? LK_CAP_TRIBUF : 0u)
         | (g_keys_ok ? LK_CAP_KEYS : 0u) | (g_led_ok ? LK_CAP_LED : 0u) | (g_lcd_ok ? LK_CAP_IMMERSIVE : 0u) | LK_CAP_SLEEP
         | (g_lz4_ok ? LK_CAP_LZ4 : 0u);
}

static void led_default(void){ led_blink(&g_led, LED_B, 3000u, 2000u, now_ms()); }   /* 未连接: 蓝灯 2 s 亮 1 s 灭 */

/* ---- 主机走了 / 休眠 (fw v10) ---- */
/* 未连接的样子: 开机版权页 (Link 行 WAIT, 主机关过背光也重新打开) + 段码屏本地时钟 + 蓝灯心跳 */
static void show_unlinked(void){
    boot_redraw();
    if (g_lcd_ok) lcd_backlight(1);
    seg_host_seen = 0; seg_draw_local();
    led_default(); led_poll();
}
static void dev_sleep(void){
    led_off(&g_led); led_poll();
    seg_off = 1;
    panel_power(0);
}
/* 醒来: 背光 / 段码屏开, 段码屏重画本地画面 (主机的 SEG 会接着来), 灯先回心跳 (主机回来的话调用方再恢复) */
static void dev_wake(void){
    panel_power(1);
    seg_off = 0; seg_host_seen = 0; seg_draw_local();
    led_default(); led_poll();
}

/* 按键采样 + KEY 上报 (HELLO 之后、主机还在时才发) */
static void keys_poll(void){
    if (!g_keys_ok) return;
    uint32_t t = now_ms();
    if (t - g_key_at < KEY_SAMPLE_MS) return;
    g_key_at = t;
    uint8_t ev[KEY_N * 2];
    int n = keys_feed(&g_keys, t, keys_hw_raw(&g_pwr, t), ev, (int)sizeof ev);
    for (int i = 0; i < n; i++)                             /* 睡着时按任意键: 醒来显示开机页 (主机不在, 不发 KEY) */
        if (KEY_EV_TYPE(ev[i]) == KEY_DOWN && lw_key(&g_lw, t) == LW_WOKE){ dev_wake(); boot_redraw(); }
    for (int i = 0; i < n && g_lw.up; i++){                 /* 没连主机: 照样走状态机, 只是不发 */
        uint8_t pl[KEY_PL_LEN], out[LK_HDR + KEY_PL_LEN + LK_TRAIL];
        keys_payload(pl, ev[i], g_key_n++, t);
        ql_uart_write(PORT, out, lk_encode(out, LK_KEY, 0, pl, KEY_PL_LEN));
    }
}

/* IMM_RECT: u32 y, u16 x, w, h, u8 flags, u8 0, w*h 个 RGB565 */
static int imm_rect(const uint8_t *pl, uint32_t n){
    if (!g_imm || n < 12) return 0;
    uint32_t y = lk_rd32(pl), x = lk_rd16(pl + 4), w = lk_rd16(pl + 6), h = lk_rd16(pl + 8), fl = pl[10], row;
    if (w == 0 || h == 0 || x + w > LCD_W || n != 12u + w * h * 2u) return 0;
    int r = im_write(&g_im, y, h, fl, &row);
    if (r == IM_ERANGE && im_fits_after_rebase(&g_im, y, h)){ imm_make_room(); r = im_write(&g_im, y, h, fl, &row); }
    if (r != IM_OK) return 0;
    const uint8_t *src = pl + 12;
    for (uint32_t i = 0; i < h; i++)
        lk_row_move((uint16_t *)(g_roll + (row + i) * ROW_B) + x, (const uint16_t *)(src + i * w * 2u), w, 0);
    dcache_clean(g_roll + row * ROW_B, h * ROW_B);
    return 1;
}

static uint8_t imm_scroll(const uint8_t *pl, uint32_t n){
    if (!g_imm || n != 6) return LK_EPARAM;
    uint32_t y = lk_rd32(pl), dur = lk_rd16(pl + 4);
    int r = im_scroll(&g_im, y, dur, now_ms());
    if (r == IM_ERANGE && im_fits_after_rebase(&g_im, y, LCD_H)){ imm_make_room(); r = im_scroll(&g_im, y, dur, now_ms()); }
    return r == IM_OK ? LK_OK : LK_EPARAM;
}

/* 一帧收齐 (g_p.type / g_p.seq / g_p.len, payload 在 g_pl) */
static void on_frame(void){
    const uint8_t *pl = g_pl; uint32_t n = g_p.len;
    int ev = lw_rx(&g_lw, now_ms());                   /* 有效帧才算主机还在 (fw v10) */
    if (ev & LW_WOKE) dev_wake();
    if (ev & LW_RESUMED){ g_led = g_led_host; led_poll(); }   /* 主机回来了 (没重发 HELLO): 恢复它设的灯 */
    int lz = g_p.type == LK_RECT_LZ4 && g_lz4_ok;     /* 没有 LZ4 时 0x23 走 default (同 v10: 不认识的 type) */
    if (g_p.type == LK_RECT || g_p.type == LK_COPY || g_p.type == LK_DIAG || lz) g_boot_live = 0;   /* 主机开始画了 / 诊断可能动缓冲 */
    if (g_imm){                                        /* 沉浸模式: 普通画面的写操作和会动缓冲的诊断一律拒 */
        if (g_p.type == LK_RECT || g_p.type == LK_COPY || lz){ st.bad_param++; return; }
        if (g_p.type == LK_PRESENT){ ack(LK_EPARAM, 0); return; }
        if (g_p.type == LK_DIAG){ diag_reply(n ? pl[0] : 0, LK_EUNSUP, 0, 0); return; }
    }
    switch (g_p.type){
    case LK_IMM_ENTER: {
        uint32_t rows;
        uint8_t stt = imm_enter(n >= 4 ? lk_rd32(pl) : 0u, &rows);
        ack(stt, rows);
        return;
    }
    case LK_IMM_EXIT:
        imm_exit();
        ack(LK_OK, caps());
        return;
    case LK_IMM_RECT:
        if (imm_rect(pl, n)) st.rects++; else st.bad_param++;
        return;                                        /* 同 RECT: 不应答 */
    case LK_IMM_SCROLL:
        ack(imm_scroll(pl, n), g_imm ? g_im.ys : 0u);
        return;
    case LK_RECT:
        draw_begin();
        if (lk_rect_apply((uint16_t *)draw_target(), LCD_W, LCD_H, pl, n, &g_tb.y0, &g_tb.y1) == LK_OK) st.rects++;
        else st.bad_param++;
        return;                                        /* RECT 不应答, 保持流式 */
    case LK_RECT_LZ4:                                  /* fw v11: 先整块解进 g_lz 再走 RECT 的路径; 坏块不动像素 */
        if (!lz) break;
        draw_begin();
        if (lk_rect_lz4_apply((uint16_t *)draw_target(), LCD_W, LCD_H, pl, n, g_lz, MAX_PAYLOAD, &g_tb.y0, &g_tb.y1) == LK_OK){
            st.rects++; st.lz4++;
        } else st.bad_param++;
        return;                                        /* 同 RECT: 不应答 */
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
        st.rx_bytes = 0; st.rects = 0; st.presents = 0; st.bad_param = 0; st.lz4 = 0;
        g_p.frames = 1; g_p.crc_err = 0; g_p.len_err = 0;      /* 计数从这条 HELLO 重新算 */
        if (n >= 6) clk_set((uint32_t)lk_rd16(pl) | ((uint32_t)lk_rd16(pl + 2) << 16), (int16_t)lk_rd16(pl + 4));
        boot_link_ok();                                /* 开机画面还在: Link 行 WAIT -> OK */
        lw_hello(&g_lw, now_ms()); g_key_n = 0;        /* KEY 计数从这条 HELLO 重新算 */
        led_off(&g_led); led_poll();                   /* 灯交给主机 */
        imm_exit();                                    /* 主机重连 = 它不记得沉浸模式了: 回普通画面 */
        uint8_t *b = g_tx;
        lk_wr16(b + 0, LK_PROTO_VER); lk_wr16(b + 2, FW_VER);
        lk_wr16(b + 4, LCD_W); lk_wr16(b + 6, LCD_H);
        b[8] = LK_PIXFMT_RGB565; b[9] = 72;
        lk_wr16(b + 10, caps()); lk_wr16(b + 12, MAX_PAYLOAD); lk_wr16(b + 14, 0);
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
    case LK_SLEEP:
        if (n != 0){ ack(LK_EPARAM, 0); return; }
        ack(LK_OK, 0);
        if (lw_sleep(&g_lw)) g_led_host = g_led;       /* 醒来 (主机回来) 时恢复 */
        dev_sleep();
        return;
    case LK_LED:
        if (!g_led_ok){ ack(LK_EUNSUP, 0); return; }
        ack(led_cmd(&g_led, pl, n, now_ms()) ? LK_EPARAM : LK_OK, 0);
        led_poll();
        return;
    case LK_FBCRC: {
        uint32_t c = 0, row;                           /* 校验的是最新提交的那块 (已显示, 或下一个帧头显示) */
        if (g_tb.n == 1) flush_dirty();
        uintptr_t fb = tb_addr(tb_latest(&g_tb));
        if (g_imm) fb = im_latest(&g_im, &row) ? g_roll + row * ROW_B : FB_ADDR;   /* 沉浸模式: 屏上那一段卷轴 */
        uint32_t s = lk_fb_crc((const uint16_t *)fb, LCD_W, LCD_H, pl, n, &c);
        ack((uint8_t)s, c);
        return;
    }
    case LK_DIAG:
        if (n < 1){ diag_reply(0, LK_EPARAM, 0, 0); return; }     /* 空 payload: g_pl[0] 是上一帧留下的, 别拿它当 op */
        if (pl[0] == 31){ diag_tri(); return; }          /* DG_TRI: 多缓冲状态 */
        if (n == 2 && pl[0] == 32){ diag_nbuf(pl[1]); return; }    /* DG_NBUF: 切块数 */
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
        break;
    }
    ack(LK_EUNSUP, 0);                                 /* 不认识的 type (含没有 LZ4 时的 0x23) */
}

/* 主循环: 收到一段字节 */
static void link_rx(const uint8_t *d, uint32_t n){
    st.rx_bytes += n;
    while (n){
        int ev; uint32_t k = lk_feed(&g_p, d, n, &ev);
        d += k; n -= k;
        if (ev) on_frame();
    }
}

/* 主循环每轮: 主机走了 (5 s 没帧 / USB 拔掉) -> 开机画面 + 蓝灯心跳 (记下主机设的灯, 回来时恢复); 没主机满 10 min -> 休眠 */
static void link_lost_poll(void){
    int ev = lw_tick(&g_lw, now_ms());
    if (usb_unplugged()) ev |= lw_unplug(&g_lw);
    if (ev & LW_LOST){ g_led_host = g_led; show_unlinked(); }
    if (ev & LW_SLEEP) dev_sleep();
}

#endif
