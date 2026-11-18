/*
 * 正面 RGB 状态灯的硬件层 (R05)。状态机在 link/led.h (主机单测)。
 * 实机证实 (2026-11-18, ledprobe): 一颗 RGB 灯, 纯 GPIO 推挽, 高电平亮:
 *   R = GPIO29 (脚53 func2)   G = GPIO19 (脚33 func0)   B = GPIO17 (脚12 func4)
 * GPIO13 (脚54) 上没有灯, 不碰。没有 PWM, 只做亮/灭。只供 monita 任务调用。
 */
#ifndef MONITA_LED_HW_H
#define MONITA_LED_HW_H
#include "r05_api.h"
#include "led.h"

typedef struct { uint8_t pin, func, gpio; } led_pin_t;
static const led_pin_t led_pins[3] = { {53, 2, 29}, {33, 0, 19}, {12, 4, 17} };   /* bit0 R, bit1 G, bit2 B */
static uint8_t led_hw_cur;

/* 返回 0 = 三路都初始化成功 */
static int led_hw_init(void){
    int bad = 0;
    for (int i = 0; i < 3; i++){
        ql_pin_set_func(led_pins[i].pin, led_pins[i].func);
        ql_gpio_deinit(led_pins[i].gpio);
        bad += ql_gpio_init(led_pins[i].gpio, GPIO_OUTPUT, PULL_NONE, 0) != 0;
    }
    led_hw_cur = 0;
    return bad;
}

/* 只写变了的那几路 */
static void led_hw_apply(uint8_t mask){
    uint8_t d = (uint8_t)((mask ^ led_hw_cur) & LED_RGB);
    for (int i = 0; i < 3; i++) if (d & (1u << i)) ql_gpio_set_level(led_pins[i].gpio, (mask >> i) & 1u);
    led_hw_cur = mask & LED_RGB;
}

#endif
