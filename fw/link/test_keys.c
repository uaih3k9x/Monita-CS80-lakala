/* fw/link/keys.h 的主机单测: sh fw/link/test.sh */
#include <stdio.h>
#include "keys.h"

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)

/* 以 10 ms 步长喂 dur 毫秒恒定的 raw (允许 u32 回绕), 收集事件; *t 停在第一个 >= dur 的拍 */
static int run(keys_t *k, uint32_t *t, uint32_t dur, uint8_t raw, uint8_t *ev, int max){
    int n = 0;
    for (uint32_t t0 = *t; *t - t0 < dur; *t += 10) n += keys_feed(k, *t, raw, ev + n, max - n);
    return n;
}

static void t_press_release(void){
    keys_t k; keys_init(&k); uint8_t ev[16]; uint32_t t = 0;
    CHECK(run(&k, &t, 100, 0, ev, 16) == 0);                 /* 空闲无事件 */
    int n = run(&k, &t, KEY_DEBOUNCE_MS + 10, KEY_BIT(2), ev, 16);
    CHECK(n == 1 && ev[0] == KEY_EV(2, KEY_DOWN));           /* 稳定满去抖时间才报 */
    n = run(&k, &t, 400, KEY_BIT(2), ev, 16);
    CHECK(n == 0);                                           /* 按住不足长按时间: 无事件 */
    n = run(&k, &t, 100, 0, ev, 16);
    CHECK(n == 1 && ev[0] == KEY_EV(2, KEY_UP));
}

static void t_quick_tap_passes(void){
    /* 实测 30 ms 去抖会滤掉轻点: 20 ms 的点按 (10 ms 采样两拍) 必须出 DOWN + UP */
    keys_t k; keys_init(&k); uint8_t ev[16]; uint32_t t = 0;
    CHECK(KEY_DEBOUNCE_MS >= 15 && KEY_DEBOUNCE_MS <= 20);
    int n = run(&k, &t, 30, KEY_BIT(3), ev, 16);
    n += run(&k, &t, 50, 0, ev + n, 16 - n);
    CHECK(n == 2 && ev[0] == KEY_EV(3, KEY_DOWN) && ev[1] == KEY_EV(3, KEY_UP));
}

static void t_bounce_rejected(void){
    keys_t k; keys_init(&k); uint8_t ev[16]; uint32_t t = 0;
    int n = 0;
    for (int i = 0; i < 20; i++){                            /* 10 ms 一翻的抖动, 不该出事件 */
        n += keys_feed(&k, t, (i & 1) ? KEY_BIT(1) : 0, ev + n, 16 - n); t += 10;
    }
    CHECK(n == 0);
    CHECK(keys_feed(&k, t, KEY_BIT(1), ev, 16) == 0);        /* 刚稳定: 计时从这一拍开始 */
}

static void t_long(void){
    keys_t k; keys_init(&k); uint8_t ev[16]; uint32_t t = 0;
    int n = run(&k, &t, KEY_DEBOUNCE_MS + KEY_LONG_MS + 100, KEY_BIT(3), ev, 16);
    CHECK(n == 2 && ev[0] == KEY_EV(3, KEY_DOWN) && ev[1] == KEY_EV(3, KEY_LONG));  /* LONG 只报一次 */
    n = run(&k, &t, 3000, KEY_BIT(3), ev, 16);
    CHECK(n == 0);
    n = run(&k, &t, 100, 0, ev, 16);
    CHECK(n == 1 && ev[0] == KEY_EV(3, KEY_UP));             /* 长按后照样有 UP */
}

static void t_multi_and_wrap(void){
    keys_t k; keys_init(&k); uint8_t ev[16];
    uint32_t t = 0xFFFFFF00u;                                /* 跨 u32 回绕 */
    int n = run(&k, &t, 200, KEY_BIT(1) | KEY_BIT(4), ev, 16);
    CHECK(n == 2 && ev[0] == KEY_EV(1, KEY_DOWN) && ev[1] == KEY_EV(4, KEY_DOWN));
    n = run(&k, &t, 200, KEY_BIT(4), ev, 16);
    CHECK(n == 1 && ev[0] == KEY_EV(1, KEY_UP));
}

static void t_max_respected(void){
    keys_t k; keys_init(&k); uint8_t ev[1]; uint32_t t = 0;
    int n = 0;
    for (; t < 200; t += 10) n += keys_feed(&k, t, KEY_ALL, ev, 0);   /* 没空间: 不写、不丢状态 */
    CHECK(n == 0);
    CHECK(keys_feed(&k, t, KEY_ALL, ev, 1) == 1 && ev[0] == KEY_EV(1, KEY_DOWN));
}

static void t_ev_helpers(void){
    CHECK(KEY_EV_KEY(KEY_EV(4, KEY_LONG)) == 4);
    CHECK(KEY_EV_TYPE(KEY_EV(4, KEY_LONG)) == KEY_LONG);
}

/* ---- PWRKEY: 回调只数 press / release 次数, pwr_sample 把计数翻成电平 ---- */

static void t_pwr_press_release(void){
    pwr_t p; pwr_init(&p);
    CHECK(pwr_sample(&p, 0, 0, 0) == 0);
    CHECK(pwr_sample(&p, 1, 0, 10) == 1);                    /* 按下回调 */
    CHECK(pwr_sample(&p, 1, 0, 500) == 1);
    CHECK(pwr_sample(&p, 1, 1, 510) == 0);                   /* 抬起回调 */
    CHECK(pwr_sample(&p, 1, 1, 520) == 0);
}

static void t_pwr_tap_between_samples(void){
    /* 两拍之间按下又抬起 (计数同时变): 电平至少保持 KEY_PWR_MIN_MS, 让去抖看得见 */
    pwr_t p; pwr_init(&p); keys_t k; keys_init(&k); uint8_t ev[8]; int n = 0;
    for (uint32_t t = 0; t < 200; t += 10){
        uint32_t c = t >= 50 ? 1u : 0u;
        n += keys_feed(&k, t, pwr_sample(&p, c, c, t) ? KEY_BIT(1) : 0, ev + n, 8 - n);
    }
    CHECK(n == 2 && ev[0] == KEY_EV(1, KEY_DOWN) && ev[1] == KEY_EV(1, KEY_UP));
}

static void t_pwr_stuck_recovers(void){
    /* 实测: 长按后收不到抬起回调。KEY_PWR_STUCK_MS 后自己放开, 报一次 UP; 之后的按下照常 */
    pwr_t p; pwr_init(&p); keys_t k; keys_init(&k); uint8_t ev[8]; int n = 0;
    uint32_t t = 0;
    for (; t < KEY_PWR_STUCK_MS + 200; t += 10) n += keys_feed(&k, t, pwr_sample(&p, 1, 0, t) ? KEY_BIT(1) : 0, ev + n, 8 - n);
    CHECK(n == 3 && ev[0] == KEY_EV(1, KEY_DOWN) && ev[1] == KEY_EV(1, KEY_LONG) && ev[2] == KEY_EV(1, KEY_UP));
    CHECK(KEY_PWR_STUCK_MS > KEY_LONG_MS);
    /* 迟到的抬起回调: 不再产生事件 */
    n = 0;
    for (uint32_t end = t + 200; t < end; t += 10) n += keys_feed(&k, t, pwr_sample(&p, 1, 1, t) ? KEY_BIT(1) : 0, ev + n, 8 - n);
    CHECK(n == 0);
    /* 下一次按下 (计数 +1) 照常 */
    for (uint32_t end = t + 100; t < end; t += 10) n += keys_feed(&k, t, pwr_sample(&p, 2, 1, t) ? KEY_BIT(1) : 0, ev + n, 8 - n);
    CHECK(n == 1 && ev[0] == KEY_EV(1, KEY_DOWN));
}

static void t_pwr_new_press_while_stuck(void){
    /* 卡住期间 (没等到超时) 又来一次按下回调: 仍是按下, 计时重新开始 */
    pwr_t p; pwr_init(&p);
    CHECK(pwr_sample(&p, 1, 0, 0) == 1);
    CHECK(pwr_sample(&p, 2, 0, KEY_PWR_STUCK_MS - 10) == 1);
    CHECK(pwr_sample(&p, 2, 0, KEY_PWR_STUCK_MS + 10) == 1);
    CHECK(pwr_sample(&p, 2, 0, 2 * KEY_PWR_STUCK_MS) == 0);
}

static void t_payload(void){
    uint8_t b[KEY_PL_LEN];
    keys_payload(b, KEY_EV(3, KEY_LONG), 0x1234, 0xA1B2C3D4u);
    CHECK(b[0] == 3 && b[1] == KEY_LONG && b[2] == 0x34 && b[3] == 0x12);
    CHECK(b[4] == 0xD4 && b[5] == 0xC3 && b[6] == 0xB2 && b[7] == 0xA1);
}

int main(void){
    t_press_release(); t_quick_tap_passes(); t_bounce_rejected(); t_long(); t_multi_and_wrap(); t_max_respected(); t_ev_helpers();
    t_pwr_press_release(); t_pwr_tap_between_samples(); t_pwr_stuck_recovers(); t_pwr_new_press_while_stuck(); t_payload();
    printf(fails ? "keys: %d FAIL\n" : "keys: all OK\n", fails);
    return fails ? 1 : 0;
}
