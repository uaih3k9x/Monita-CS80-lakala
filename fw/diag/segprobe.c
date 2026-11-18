/* R05 段码屏映射探针: 每步 1.5 s 点亮一个目标, 主数字区显示序号, COM5 同步打印。供录像对照段位。
 * 循环: "------" 2 s -> 阶段 F: 30 个渲染器 flag(原厂渲染函数的 state[0x12..0x2f], 见 docs/segmap.md), 主区显示 "F  xx"(xx=state 偏移 hex)
 *       -> 阶段 b: 72 字节中未被数字/flag 覆盖的位(按字节整体点亮), 主区显示 "b  nn"(nn=字节号 十六进制)。
 * 显存位: (com,seg) -> fb[18*com + seg/8] bit (7 - seg%8) (同原厂 set_bit)。 */
#include "fg00.h"
static const char HX[] = "0123456789abcdef";
static void say(char tag, unsigned idx, unsigned byte, unsigned mask) /* 全十六进制 */{
    char b[24]; int i = 0;
    b[i++] = tag; b[i++] = ' ';
    b[i++] = HX[(idx >> 4) & 0xf]; b[i++] = HX[idx & 0xf];
    b[i++] = ' '; b[i++] = 'B';
    b[i++] = HX[(byte >> 4) & 0xf]; b[i++] = HX[byte & 0xf];
    b[i++] = ' '; b[i++] = 'm';
    b[i++] = HX[(mask >> 4) & 0xf]; b[i++] = HX[mask & 0xf];
    b[i++] = '\r'; b[i++] = '\n';
    ql_uart_write(4, b, (uint32_t)i);
}
typedef struct { uint8_t st, byte, mask; } flag_t;
static const flag_t FL[] = {
    {0x12, 56, 0x08},
    {0x13, 58, 0x08},
    {0x14, 55, 0x08},
    {0x15, 57, 0x08},
    {0x16, 63, 0x08},
    {0x17, 65, 0x08},
    {0x18, 59, 0x08},
    {0x19, 60, 0x08},
    {0x1a, 25, 0x80},
    {0x1b, 61, 0x80},
    {0x1c, 43, 0x80},
    {0x1d, 7, 0x80},
    {0x1e, 25, 0x20},
    {0x1f, 43, 0x20},
    {0x20, 61, 0x20},
    {0x21, 7, 0x20},
    {0x22, 7, 0x08},
    {0x23, 25, 0x08},
    {0x24, 43, 0x08},
    {0x25, 61, 0x08},
    {0x26, 61, 0x02},
    {0x27, 43, 0x02},
    {0x28, 7, 0x02},
    {0x29, 25, 0x02},
    {0x2a, 70, 0x80},
    {0x2b, 52, 0x80},
    {0x2c, 70, 0x40},
    {0x2d, 16, 0x40},
    {0x2e, 16, 0x80},
    {0x2f, 34, 0x80}
};
typedef struct { uint8_t byte, mask; } unc_t;
static const unc_t UN[] = {
    {0, 0x77},
    {1, 0x77},
    {2, 0x77},
    {3, 0x77},
    {4, 0x77},
    {5, 0x77},
    {6, 0x77},
    {7, 0x55},
    {8, 0x77},
    {9, 0x77},
    {10, 0x77},
    {11, 0x77},
    {12, 0x77},
    {13, 0x77},
    {14, 0x77},
    {15, 0x77},
    {16, 0x3f},
    {17, 0xff},
    {18, 0x77},
    {19, 0x77},
    {20, 0x77},
    {21, 0x77},
    {22, 0x77},
    {23, 0x77},
    {24, 0x77},
    {25, 0x55},
    {26, 0x77},
    {27, 0x77},
    {28, 0x77},
    {29, 0x77},
    {30, 0x77},
    {31, 0x77},
    {32, 0x77},
    {33, 0x77},
    {34, 0x7f},
    {35, 0xff},
    {36, 0x77},
    {37, 0x77},
    {38, 0x77},
    {39, 0x77},
    {40, 0x77},
    {41, 0x77},
    {42, 0x77},
    {43, 0x55},
    {44, 0x77},
    {45, 0x77},
    {46, 0x77},
    {47, 0x77},
    {48, 0x77},
    {49, 0x77},
    {50, 0x77},
    {51, 0x77},
    {52, 0x7f},
    {53, 0xff},
    {54, 0x7f},
    {55, 0x77},
    {56, 0x77},
    {57, 0x77},
    {58, 0x77},
    {59, 0x77},
    {60, 0x77},
    {61, 0x55},
    {62, 0x7f},
    {63, 0x77},
    {64, 0x7f},
    {65, 0x77},
    {66, 0x7f},
    {67, 0x7f},
    {68, 0x7f},
    {69, 0x7f},
    {70, 0x3f},
    {71, 0xff}
};
#define SEG_b 0x7c
#define SEG_F 0x71
static void label(uint8_t letter, unsigned d1, unsigned d0){
    fg_fill(0);
    fg_digit(0, letter);
    fg_digit(4, fg_hex[d1 & 0xf]);
    fg_digit(5, fg_hex[d0 & 0xf]);
}
static void *g_task;
static void task_entry(void *arg){
    (void)arg; (void)fg_on; (void)fg_off;
    fg_init();
    ql_uart_open(4);
    for (;;){
        fg_fill(0);
        for (int k = 0; k < 6; k++) fg_digit(k, SEG_DASH);
        fg_flush(); ql_uart_write(4, "------\r\n", 8);
        ql_rtos_task_sleep_ms(2000);
        for (unsigned n = 0; n < sizeof FL / sizeof FL[0]; n++){
            label(SEG_F, FL[n].st >> 4, FL[n].st & 0xf);
            fg_fb[FL[n].byte] |= FL[n].mask;
            fg_flush(); say('F', FL[n].st, FL[n].byte, FL[n].mask);
            ql_rtos_task_sleep_ms(1500);
        }
        for (unsigned n = 0; n < sizeof UN / sizeof UN[0]; n++){
            label(SEG_b, UN[n].byte >> 4, UN[n].byte & 0xf);
            fg_fb[UN[n].byte] |= UN[n].mask;
            fg_flush(); say('b', UN[n].byte, UN[n].byte, UN[n].mask);
            ql_rtos_task_sleep_ms(1500);
        }
    }
}
int appimg_enter(void *arg){
    (void)arg;
    ql_pin_set_func(95, 5);
    ql_gpio_init(10, GPIO_OUTPUT, 0xff, 1);
    ql_rtos_task_create(&g_task, 8192, APP_PRIORITY_NORMAL, "monita-segmap", task_entry, 0, 1);
    return 0;
}
void appimg_exit(void){ }
