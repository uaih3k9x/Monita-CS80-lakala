/*
 * 正面 RGB 状态灯: LED 消息解析 + 亮/灭/闪烁状态机 (纯逻辑, 主机单测 test_led.c)。硬件层 ../io/led_hw.h。
 * 不做调光/呼吸 (实机软件 PWM 效果差, 维护者决定只做开关): 三位掩码 bit0 R, bit1 G, bit2 B, 可混 7 色。
 *
 * 用法: 主循环每轮 led_step(now) 得到此刻该亮的掩码, 变了才写 GPIO。没有定时器、不忙等;
 *       闪烁相位的抖动 = 主循环间隔 (≈ 10 ms, PRESENT 等帧时最长 ≈ 55 ms), 对状态灯无所谓。
 * 时间是 u32 (固件里 1.024 ms 一个单位), 允许回绕; 不用除法 (A5 没有硬件除法)。
 *
 * LED (0x31) 载荷 8 B: u8 mask, u8 mode, u16 period_ms, u16 on_ms, u16 duration_ms
 *   mode 0 灭 / 1 常亮 / 2 闪烁; period 0 = 1000; on 0 = period 的一半 (必须 < period);
 *   duration 0 = 一直保持, 否则到时自动灭。
 */
#ifndef MONITA_LED_H
#define MONITA_LED_H
#include <stdint.h>

#define LED_R 1u
#define LED_G 2u
#define LED_B 4u
#define LED_RGB 7u

#define LED_MODE_OFF   0u
#define LED_MODE_ON    1u
#define LED_MODE_BLINK 2u

#define LED_PL_LEN        8u
#define LED_DEF_PERIOD    1000u
#define LED_MIN_PERIOD    40u
#define LED_EPARAM        1       /* = LK_EPARAM */

typedef struct {
    uint8_t  mask, mode, lit, timed;
    uint32_t on, off;              /* 闪烁: 亮 / 灭的时长 */
    uint32_t next;                 /* 闪烁: 下一次翻转的时刻 */
    uint32_t until;                /* timed: 到这个时刻自动灭 */
} led_t;

static inline void led_init(led_t *l){
    l->mask = 0; l->mode = LED_MODE_OFF; l->lit = 0; l->timed = 0;
    l->on = l->off = l->next = l->until = 0;
}

static inline void led_solid(led_t *l, uint8_t mask){ l->mask = mask & LED_RGB; l->mode = LED_MODE_ON; l->timed = 0; }
static inline void led_off(led_t *l){ l->mode = LED_MODE_OFF; l->timed = 0; }

/* 闪烁, 从"亮"开始 */
static inline void led_blink(led_t *l, uint8_t mask, uint32_t period, uint32_t on, uint32_t now){
    l->mask = mask & LED_RGB; l->mode = LED_MODE_BLINK; l->timed = 0;
    l->on = on; l->off = period - on; l->lit = 1; l->next = now + on;
}

/* 解析 LED 消息并生效; 返回 0 = OK, LED_EPARAM = 参数不对 (状态不变) */
static inline int led_cmd(led_t *l, const uint8_t *p, uint32_t n, uint32_t now){
    if (n != LED_PL_LEN) return LED_EPARAM;
    uint8_t mask = p[0], mode = p[1];
    uint32_t period = (uint32_t)(p[2] | (p[3] << 8)), on = (uint32_t)(p[4] | (p[5] << 8)), dur = (uint32_t)(p[6] | (p[7] << 8));
    if (mask & ~LED_RGB) return LED_EPARAM;
    if (mode > LED_MODE_BLINK) return LED_EPARAM;
    if (mode == LED_MODE_BLINK){
        if (!period) period = LED_DEF_PERIOD;
        if (!on) on = period >> 1;
        if (period < LED_MIN_PERIOD || on >= period) return LED_EPARAM;
        led_blink(l, mask, period, on, now);
    } else if (mode == LED_MODE_ON) led_solid(l, mask);
    else led_off(l);
    if (dur && mode != LED_MODE_OFF){ l->timed = 1; l->until = now + dur; }
    return 0;
}

/* 此刻应该亮的掩码 */
static inline uint8_t led_step(led_t *l, uint32_t now){
    if (l->timed && (int32_t)(now - l->until) >= 0) led_off(l);
    if (l->mode == LED_MODE_ON) return l->mask;
    if (l->mode != LED_MODE_BLINK) return 0;
    if ((int32_t)(now - l->next) >= 0){
        if (now - l->next >= l->on + l->off){ l->lit = 1; l->next = now + l->on; }   /* 落后一整个周期以上: 不追, 从亮重来 */
        else { l->lit ^= 1u; l->next += l->lit ? l->on : l->off; }
    }
    return l->lit ? l->mask : 0;
}

#endif
