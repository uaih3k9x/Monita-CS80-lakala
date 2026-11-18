/* diag/segtest: 段码屏点亮验证。
 *  - 原厂开机时 pin 表把 GPIO10 复用到 pin95(func5, 输出高), 然后 fg00_hw_init 刷全亮帧,
 *    之后不调 onoff, GPIO10 保持高 => 段码屏常亮 888888。
 *  - 原厂 onoff(1) 会发 3D 并把 GPIO10 拉低, 实测是熄屏 (名字和效果相反, 早期版本因此"闪一下就灭")。
 * 本程序: 补 pin95 -> GPIO10 高, 跑 fg_init, 不调 fg_on; 每 3 s 在 888888 和 "123456" 之间切换,
 * 顺带验证 7 段位映射; USB modem 口同步打印当前图样。 */
#include "fg00.h"
#define W(s) ql_uart_write(4, (s), (uint32_t)(sizeof(s) - 1))
static void *g_task;
static void task_entry(void *arg){
    (void)arg; (void)fg_on;
    ql_pin_set_func(95, 5);                       /* = 原厂 pin 表第 18 项 */
    ql_gpio_init(10, GPIO_OUTPUT, 0xff, 1);
    fg_init();                                     /* 结束时 888888 已刷出, GPIO10 高 */
    ql_uart_open(4);
    for (;;){
        W("F:888888\r\n");
        ql_rtos_task_sleep_ms(3000);
        fg_fill(0);
        for (int k = 0; k < 6; k++) fg_digit(k, fg_hex[k + 1]);
        fg_flush();
        W("F:123456\r\n");
        ql_rtos_task_sleep_ms(3000);
        fg_fill(0xFF);
        fg_flush();
    }
}
int appimg_enter(void *arg){
    (void)arg;
    ql_rtos_task_create(&g_task, 8192, APP_PRIORITY_NORMAL, "monita-segtest", task_entry, 0, 1);
    return 0;
}
void appimg_exit(void){ }
