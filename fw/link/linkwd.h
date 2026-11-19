/*
 * 主机在不在 + 休眠 (fw v8 起; v10 改): 纯逻辑, 主机单测 test_linkwd.c。时间是 u32 毫秒 (固件里 1.024 ms 一个单位), 允许回绕。
 *
 * v10:
 *   - 只认**有效帧** (CRC 对的整帧), 乱码不算主机还在。HELLO 之后 LW_LOST_MS 没有有效帧 = 主机走了 (LW_LOST):
 *     固件回到开机画面 + 未连接蓝灯心跳, 不再发 KEY。USB 拔掉 (hotplug 回调) 时 lw_unplug 立刻报 LW_LOST, 不用等。
 *     上位机正常时最长 ≈ 2 s 一帧 (Mac: SEG ≤ 1.5 s、PING 2 s), 5 s 不会误报。
 *   - 没有主机 (走了 / 开机后一直没连) 满 LW_SLEEP_MS: 自动休眠 (LW_SLEEP, 关背光 / 段码屏 / 灯), 避免拔了线一直亮着。
 *   - 主机发 SLEEP (0x32): lw_sleep 立刻睡, 主机算"走了" (不发 KEY, 不再报 LOST / 自动休眠)。
 *   - 睡着时任何一帧叫醒 (lw_rx 带 LW_WOKE); 按键也能叫醒 (lw_key, 不算主机回来, 10 min 重新计)。
 * v9: 走了之后只要又收到帧 (HELLO 过的前提下) 就当回来了 (LW_RESUMED): 主机睡醒但 USB 没断、进程被挂起后接着发时
 *     不一定重发 HELLO。固件据此恢复 KEY 上报和主机设的灯。
 */
#ifndef MONITA_LINKWD_H
#define MONITA_LINKWD_H
#include <stdint.h>

#define LW_LOST_MS  5000u           /* v8 / v9: 15 s, 而且算的是任何字节 */
#define LW_SLEEP_MS 600000u         /* 10 min */

/* 事件 (位掩码; lw_rx 可能同时报 WOKE | RESUMED) */
#define LW_NONE    0
#define LW_LOST    1
#define LW_RESUMED 2
#define LW_SLEEP   4
#define LW_WOKE    8

typedef struct { uint8_t hello, up, asleep; uint32_t last, lost_ms, sleep_ms; } lw_t;

/* now: 开机时刻 (没人连时自动休眠从这里算) */
static inline void lw_init(lw_t *w, uint32_t now){
    w->hello = 0; w->up = 0; w->asleep = 0; w->last = now;
    w->lost_ms = LW_LOST_MS; w->sleep_ms = LW_SLEEP_MS;
}

/* 收到 HELLO (先照常 lw_rx, 再调这个) */
static inline void lw_hello(lw_t *w, uint32_t now){ w->hello = 1; w->up = 1; w->last = now; }

/* 收到一条有效帧 */
static inline int lw_rx(lw_t *w, uint32_t now){
    int r = LW_NONE;
    w->last = now;
    if (w->asleep){ w->asleep = 0; r |= LW_WOKE; }
    if (w->hello && !w->up){ w->up = 1; r |= LW_RESUMED; }
    return r;
}

/* 主循环每轮调 */
static inline int lw_tick(lw_t *w, uint32_t now){
    uint32_t idle = now - w->last;
    if (w->up && idle > w->lost_ms){ w->up = 0; return LW_LOST; }
    if (!w->up && !w->asleep && idle >= w->sleep_ms){ w->asleep = 1; return LW_SLEEP; }
    return LW_NONE;
}

/* 主机发 SLEEP: 返回 1 = 之前主机在 (调用方记下主机设的灯, 醒来恢复) */
static inline int lw_sleep(lw_t *w){
    int was = w->up;
    w->up = 0; w->asleep = 1;
    return was;
}

/* 按键: 睡着就叫醒 (主机还是不在), 自动休眠从现在重新计 */
static inline int lw_key(lw_t *w, uint32_t now){
    if (!w->asleep) return LW_NONE;
    w->asleep = 0; w->last = now;
    return LW_WOKE;
}

/* USB 拔掉: 连着就马上当主机走了 */
static inline int lw_unplug(lw_t *w){
    if (!w->up) return LW_NONE;
    w->up = 0;
    return LW_LOST;
}

#endif
