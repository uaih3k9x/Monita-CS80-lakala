/* diag/probe: 不驱动任何屏。建一个任务, 每 500 ms 往 USB modem 口(port 4)写 "MONITALIVE\r\n"。
 * 用途: 区分 "app 根本没被 loader 加载(签名门/CRC)" 和 "加载了但屏驱动有问题"。
 * enter 只建任务就返回(不能阻塞, 见 docs/r05-notes.md 的入口约定)。 */
#include "r05_api.h"

static const char MARK[] = "MONITALIVE\r\n";
static void task_entry(void *arg){
    (void)arg;
    ql_uart_open(4);
    for (;;){
        ql_uart_write(4, MARK, (uint32_t)(sizeof(MARK) - 1));
        ql_rtos_task_sleep_ms(500);
    }
}
static void *g_task;
int appimg_enter(void *arg){
    (void)arg;
    ql_rtos_task_create(&g_task, 8192, APP_PRIORITY_NORMAL, "monita-probe", task_entry, 0, 1);
    return 0;
}
void appimg_exit(void){ }
