/*
 * 主机在不在 (fw v8 起): HELLO 之后 LW_LOST_MS 收不到任何字节 = 主机走了 (主机连着时至少每 2 s 一个 PING)。
 * 纯逻辑, 主机单测 test_linkwd.c。时间是 u32 毫秒 (固件里 1.024 ms 一个单位), 允许回绕。
 *
 * fw v9: 走了之后只要又收到字节 (HELLO 过的前提下) 就当回来了 (LW_RESUMED)。
 *   v8 只认下一条 HELLO: 主机停 > 15 s 又接着发 (Mac 睡眠唤醒但 USB 没断、进程被挂起) 时不会重发 HELLO,
 *   设备就一直不发 KEY、灯一直是"未连接"的蓝灯心跳。固件据此恢复 KEY 上报和主机设的灯。
 */
#ifndef MONITA_LINKWD_H
#define MONITA_LINKWD_H
#include <stdint.h>

#define LW_LOST_MS 15000u

#define LW_NONE    0
#define LW_LOST    1
#define LW_RESUMED 2

typedef struct { uint8_t hello, up; uint32_t last; } lw_t;

static inline void lw_init(lw_t *w){ w->hello = 0; w->up = 0; w->last = 0; }
static inline void lw_hello(lw_t *w, uint32_t now){ w->hello = 1; w->up = 1; w->last = now; }

/* 收到字节 */
static inline int lw_rx(lw_t *w, uint32_t now){
    w->last = now;
    if (w->hello && !w->up){ w->up = 1; return LW_RESUMED; }
    return LW_NONE;
}

/* 主循环每轮调 */
static inline int lw_tick(lw_t *w, uint32_t now){
    if (w->up && now - w->last > LW_LOST_MS){ w->up = 0; return LW_LOST; }
    return LW_NONE;
}

#endif
