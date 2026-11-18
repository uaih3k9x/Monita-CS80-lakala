/* fw/link/led.h 的主机单测: sh fw/link/test.sh */
#include <stdio.h>
#include "led.h"

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)

static uint32_t pl(uint8_t *b, uint8_t mask, uint8_t mode, uint32_t period, uint32_t on, uint32_t dur){
    b[0] = mask; b[1] = mode;
    b[2] = (uint8_t)period; b[3] = (uint8_t)(period >> 8);
    b[4] = (uint8_t)on;     b[5] = (uint8_t)(on >> 8);
    b[6] = (uint8_t)dur;    b[7] = (uint8_t)(dur >> 8);
    return LED_PL_LEN;
}

static void t_default_off(void){
    led_t l; led_init(&l);
    CHECK(led_step(&l, 0) == 0);
    CHECK(led_step(&l, 100000) == 0);
}

static void t_on_off(void){
    led_t l; led_init(&l); uint8_t b[8];
    CHECK(led_cmd(&l, b, pl(b, LED_G, LED_MODE_ON, 0, 0, 0), 10) == 0);
    CHECK(led_step(&l, 10) == LED_G);
    CHECK(led_step(&l, 999999) == LED_G);                       /* 一直亮 */
    CHECK(led_cmd(&l, b, pl(b, LED_G, LED_MODE_OFF, 0, 0, 0), 20) == 0);
    CHECK(led_step(&l, 20) == 0);
    /* 掩码 0 的常亮 = 灭 */
    CHECK(led_cmd(&l, b, pl(b, 0, LED_MODE_ON, 0, 0, 0), 30) == 0);
    CHECK(led_step(&l, 30) == 0);
}

static void t_blink_duty(void){
    /* 原厂心跳: 周期 3000, 亮 2000 */
    led_t l; led_init(&l);
    led_blink(&l, LED_B, 3000, 2000, 1000);
    CHECK(led_step(&l, 1000) == LED_B);
    CHECK(led_step(&l, 2999) == LED_B);
    CHECK(led_step(&l, 3000) == 0);
    CHECK(led_step(&l, 3999) == 0);
    CHECK(led_step(&l, 4000) == LED_B);
    CHECK(led_step(&l, 6000) == 0);
    CHECK(led_step(&l, 7000) == LED_B);
}

static void t_blink_default_half(void){
    led_t l; led_init(&l); uint8_t b[8];
    CHECK(led_cmd(&l, b, pl(b, LED_R | LED_G, LED_MODE_BLINK, 0, 0, 0), 0) == 0);   /* 周期 0 = 1000, 亮 0 = 一半 */
    CHECK(led_step(&l, 0) == (LED_R | LED_G));
    CHECK(led_step(&l, 499) == (LED_R | LED_G));
    CHECK(led_step(&l, 500) == 0);
    CHECK(led_step(&l, 1000) == (LED_R | LED_G));
}

static void t_blink_resync_after_gap(void){
    /* 主循环卡了很久 (远超一个周期) 才回来: 不追帧, 从亮重新开始 */
    led_t l; led_init(&l);
    led_blink(&l, LED_B, 1000, 500, 0);
    CHECK(led_step(&l, 0) == LED_B);
    CHECK(led_step(&l, 100000) == LED_B);
    CHECK(led_step(&l, 100499) == LED_B);
    CHECK(led_step(&l, 100500) == 0);
}

static void t_blink_wrap(void){
    led_t l; led_init(&l);
    uint32_t t0 = 0xFFFFFF00u;
    led_blink(&l, LED_G, 1000, 500, t0);
    CHECK(led_step(&l, t0 + 499) == LED_G);
    CHECK(led_step(&l, t0 + 500) == 0);                         /* 跨 u32 回绕 */
    CHECK(led_step(&l, t0 + 1000) == LED_G);
}

static void t_duration(void){
    led_t l; led_init(&l); uint8_t b[8];
    CHECK(led_cmd(&l, b, pl(b, LED_R, LED_MODE_ON, 0, 0, 2000), 100) == 0);
    CHECK(led_step(&l, 2099) == LED_R);
    CHECK(led_step(&l, 2100) == 0);                             /* 到时自动灭 */
    CHECK(led_step(&l, 5000) == 0);
    /* 闪烁 + 持续 */
    CHECK(led_cmd(&l, b, pl(b, LED_B, LED_MODE_BLINK, 400, 200, 1000), 0) == 0);
    CHECK(led_step(&l, 0) == LED_B);
    CHECK(led_step(&l, 850) == LED_B);                          /* 800..1000 是第三个亮段 */
    CHECK(led_step(&l, 1000) == 0);
    CHECK(led_step(&l, 1200) == 0);
}

static void t_bad_params(void){
    led_t l; led_init(&l); uint8_t b[8];
    led_blink(&l, LED_B, 1000, 500, 0);
    CHECK(led_cmd(&l, b, 7, 0) == LED_EPARAM);                              /* 长度不对 */
    CHECK(led_cmd(&l, b, pl(b, 0x08, LED_MODE_ON, 0, 0, 0), 0) == LED_EPARAM);  /* 只有 3 位 */
    CHECK(led_cmd(&l, b, pl(b, LED_G, 3, 0, 0, 0), 0) == LED_EPARAM);           /* 没有调光/呼吸 */
    CHECK(led_cmd(&l, b, pl(b, LED_G, LED_MODE_BLINK, 1000, 1000, 0), 0) == LED_EPARAM);   /* 亮 ≥ 周期 */
    CHECK(led_cmd(&l, b, pl(b, LED_G, LED_MODE_BLINK, 10, 0, 0), 0) == LED_EPARAM);        /* 周期太短 */
    CHECK(led_step(&l, 0) == LED_B && led_step(&l, 500) == 0);              /* 坏命令不改状态 */
}

int main(void){
    t_default_off(); t_on_off(); t_blink_duty(); t_blink_default_half(); t_blink_resync_after_gap(); t_blink_wrap();
    t_duration(); t_bad_params();
    printf(fails ? "led: %d FAIL\n" : "led: all OK\n", fails);
    return fails ? 1 : 0;
}
