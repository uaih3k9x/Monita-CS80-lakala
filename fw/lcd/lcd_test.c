/*
 * 大屏测试: 段码屏 888888 + 800x1280 大屏整屏纯色 红/绿/蓝 每 2 s 轮换。
 *
 * 段码屏诊断码: 888888 = 全部成功; "E  1xx" = lcd_init 失败, "E  2xx" = write_screen 失败,
 * xx 为返回码低 8 位 (十六进制)。
 */
#include "fg00.h"
#include "lcd.h"

static void show_err(int code){
    unsigned stage = (unsigned)code >> 16, ret = (unsigned)code & 0xff;
    fg_fill(0);
    fg_digit(0, SEG_E);
    fg_digit(3, fg_hex[stage & 0xF]);
    fg_digit(4, fg_hex[(ret >> 4) & 0xF]);
    fg_digit(5, fg_hex[ret & 0xF]);
    fg_flush();
}

static void task_entry(void *arg){
    (void)arg;
    fg_init();                                 /* 888888 (EN 高 = 亮, 不调 onoff) */

    int r = lcd_power_on();
    if (r != 0){ show_err(r); for (;;) ql_rtos_task_sleep_ms(1000); }

    static const uint16_t colors[3] = { 0xF800, 0x07E0, 0x001F };  /* 红 绿 蓝 */
    fb_fill(colors[0]);
    ql_rtos_task_sleep_ms(200);
    lcd_backlight(1);

    for (unsigned i = 0;;){
        ql_rtos_task_sleep_ms(2000);
        if (++i == 3u) i = 0;
        fb_fill(colors[i]);
    }
}

static void *g_task;

int appimg_enter(void *arg){
    (void)arg;
    ql_rtos_task_create(&g_task, 8192, APP_PRIORITY_NORMAL, "monita-lcdtest", task_entry, 0, 1);
    return 0;
}

void appimg_exit(void){ }
