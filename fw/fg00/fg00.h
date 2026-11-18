/*
 * FG00 段码屏驱动 —— 按 R05 原厂固件的行为重新实现 (时序/命令见 docs/r05-notes.md), 对应原厂函数:
 *   i2c_start / write_byte / i2c_stop / cmd / hal_init / flush / fg00_hw_init / onoff (地址见 docs/r05-notes.md §9)
 * 引脚 (ql 硬件 GPIO 号): SDA=GPIO15(脚56 func0) SCK=GPIO14(脚57 func0)
 *                         RST=GPIO30(脚52 func2) EN=GPIO10
 * EN 实际走 pin95(func5): 原厂 ql_pin_cfg_init 表第 18 项 {95,5,5,10,OUT,-,1}。只接 pin4 屏完全不亮。
 * EN 极性(实机 2026-11-17): GPIO10 高=亮, 低=灭。原厂开机 fg00_hw_init 后不调 onoff, GPIO10 保持高;
 *   原厂 onoff(1)=发 3D + GPIO10 低, 实测是灭屏(命名与效果相反)。
 * 只供单个任务调用 (内部 static, 不可重入)。
 */
#ifndef FG00_H
#define FG00_H
#include "r05_api.h"

#define FG_SDA 15u
#define FG_SCK 14u
#define FG_RST 30u
#define FG_EN  10u

#include "fgbuf.h"            /* fg_fb 显存 + fg_fill / fg_digit / fg_hex (纯内存, 模拟器也用) */

static void fg_sda(uint32_t v){ ql_gpio_set_level(FG_SDA, v); }
static void fg_sck(uint32_t v){ ql_gpio_set_level(FG_SCK, v); }
static void fg_dly(uint32_t u){ ql_delay_us(u); }

static void fg_start(void){
    ql_gpio_set_direction(FG_SDA, GPIO_OUTPUT);
    fg_sck(1); fg_dly(10); fg_sda(1); fg_dly(10); fg_sda(0); fg_dly(10);
}
static void fg_stop(void){
    ql_gpio_set_direction(FG_SDA, GPIO_OUTPUT);
    fg_sck(1); fg_dly(10); fg_sda(0); fg_dly(10); fg_sda(1); fg_dly(10);
}
/* 原厂: sck0 d1, 然后每位 {sda=bit, sck1 d1, sck0 d1}; 第 9 时钟 sda1 d1 sck1 d1 sck0 d1 */
static void fg_wr(uint8_t b){
    fg_sck(0); fg_dly(1);
    for (int i = 0; i < 8; i++){
        fg_sda((b & 0x80u) ? 1u : 0u);
        fg_sck(1); fg_dly(1); fg_sck(0); fg_dly(1);
        b = (uint8_t)(b << 1);
    }
    fg_sda(1); fg_dly(1); fg_sck(1); fg_dly(1); fg_sck(0); fg_dly(1);
}
static void fg_cmd(uint8_t c){ fg_wr(0x80); fg_wr(c); }

static void fg_flush(void){
    fg_start(); fg_wr(0x7E);
    fg_cmd(0x40); fg_cmd(0x50); fg_cmd(0x10); fg_cmd(0x00); fg_cmd(0xC0);
    fg_stop();
    for (int i = 0; i < 72; i++){ fg_start(); fg_wr(0x7E); fg_wr(0xC0); fg_wr(fg_fb[i]); fg_stop(); }
}

/* 返回 ql_gpio_init 失败 (非 0) 的个数。0 = 成功按 Quectel CSDK 惯例推测, R05 上未单独证实 */
static int fg_hal_init(void){
    int bad = 0;
    ql_pin_set_func(56, 0); ql_gpio_deinit(FG_SDA); bad += ql_gpio_init(FG_SDA, GPIO_OUTPUT, PULL_NONE, 1) != 0;
    ql_pin_set_func(57, 0); ql_gpio_deinit(FG_SCK); bad += ql_gpio_init(FG_SCK, GPIO_OUTPUT, PULL_NONE, 1) != 0;
    ql_pin_set_func(52, 2); ql_gpio_deinit(FG_RST); bad += ql_gpio_init(FG_RST, GPIO_OUTPUT, PULL_NONE, 0) != 0;
    ql_pin_set_func( 4, 0); ql_gpio_deinit(FG_EN);  bad += ql_gpio_init(FG_EN,  GPIO_OUTPUT, PULL_NONE, 1) != 0;
    ql_pin_set_func(95, 5);   /* EN 真正的引脚; 原厂由 pin 表设置, 我们跳过了 pin 表所以在这里补 */
    return bad;
}

/* = fg00_hw_init: 结束时 fb 全 0xFF 已刷出且 EN 高 => 屏已亮 888888, 无需再调 fg_on() */
/* 返回 0 = GPIO 都初始化成功 (段码屏是单向写, 没有应答可查, 这是唯一能拿到的初始化结果) */
static int fg_init(void){
    fg_fill(0);
    int bad = fg_hal_init();
    fg_sck(1); fg_sda(1); ql_rtos_task_sleep_ms(10); fg_start(); fg_stop();
    ql_gpio_set_level(FG_RST, 0); ql_rtos_task_sleep_ms(50); ql_rtos_task_sleep_ms(100);
    ql_gpio_set_level(FG_RST, 1); ql_rtos_task_sleep_ms(100);
    /* 原厂是一整帧: START 7E {80 xx}*21 00 3D STOP, 0x34 后睡 50 ms */
    static const uint8_t a[] = {0x2F,0xB1,0x40,0x34};
    static const uint8_t b[] = {0x68,0xA0,0x94,0xB2,0x4C,0x10,0x40,0x50,0x00,
                                0x10,0xC0,0x88,0x88,0x88,0x88,0x61,0xE3};
    fg_start(); fg_wr(0x7E);
    for (unsigned i = 0; i < sizeof a; i++) fg_cmd(a[i]);
    ql_rtos_task_sleep_ms(50);
    for (unsigned i = 0; i < sizeof b; i++) fg_cmd(b[i]);
    fg_wr(0x00); fg_wr(0x3D);
    fg_stop();
    fg_fill(0xFF);
    fg_flush();
    ql_rtos_task_sleep_ms(50);
    return bad;
}

/* EN 开关。不发 3C/3D 命令(语义未证实), 只控 GPIO10: 高=亮 低=灭 (实机验证)。 */
__attribute__((unused)) static void fg_on(void){  ql_gpio_set_level(FG_EN, 1); }
__attribute__((unused)) static void fg_off(void){ ql_gpio_set_level(FG_EN, 0); }

#endif
