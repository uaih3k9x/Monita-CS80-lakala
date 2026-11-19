/* fw/link/linkwd.h 的主机单测: sh fw/link/test.sh */
#include <stdio.h>
#include "linkwd.h"

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)

static void t_no_hello_no_link(void){
    lw_t w; lw_init(&w, 0);
    CHECK(!w.up && !w.asleep);
    CHECK(lw_rx(&w, 100) == LW_NONE);                        /* 没 HELLO 过: 有帧也不算连上 */
    CHECK(!w.up);
    CHECK(lw_tick(&w, 100 + LW_LOST_MS + 1) == LW_NONE);
}

static void t_constants(void){
    /* 上位机正常时最长 ≈ 2 s 一帧 (Mac: SEG ≤ 1.5 s、PING 2 s; tick 200 ms); 5 s 留够余量又不至于让人等太久 */
    CHECK(LW_LOST_MS == 5000u);
    CHECK(LW_SLEEP_MS == 600000u);                           /* 10 min */
}

static void t_lost_after_silence(void){
    lw_t w; lw_init(&w, 0);
    lw_hello(&w, 0);
    CHECK(w.up);
    CHECK(lw_tick(&w, LW_LOST_MS) == LW_NONE);               /* 正好 5 s: 还算在 */
    lw_rx(&w, LW_LOST_MS);
    CHECK(lw_tick(&w, 2 * LW_LOST_MS) == LW_NONE);
    CHECK(lw_tick(&w, 2 * LW_LOST_MS + 1) == LW_LOST);
    CHECK(!w.up && !w.asleep);
    CHECK(lw_tick(&w, 3 * LW_LOST_MS) == LW_NONE);           /* 只报一次 */
}

static void t_resume_without_hello(void){
    /* 主机停了 > 5 s (Mac 睡眠、进程被挂起) 又接着发, 不一定重发 HELLO: 帧一来就恢复, 不能一直当它不在 */
    lw_t w; lw_init(&w, 0);
    lw_hello(&w, 0);
    CHECK(lw_tick(&w, LW_LOST_MS + 5) == LW_LOST);
    CHECK(lw_rx(&w, LW_LOST_MS + 100) == LW_RESUMED);
    CHECK(w.up);
    CHECK(lw_rx(&w, LW_LOST_MS + 200) == LW_NONE);           /* 已经在了: 不再报 */
}

static void t_idle_sleep(void){
    /* 主机走了 (或开机后一直没连) 满 10 min: 自动休眠, 只报一次; 之后来帧 = 醒 (+ 恢复) */
    lw_t w; lw_init(&w, 0);
    lw_hello(&w, 0);
    CHECK(lw_tick(&w, LW_LOST_MS + 1) == LW_LOST);
    CHECK(lw_tick(&w, LW_SLEEP_MS - 1) == LW_NONE);
    CHECK(lw_tick(&w, LW_SLEEP_MS) == LW_SLEEP);
    CHECK(w.asleep && !w.up);
    CHECK(lw_tick(&w, LW_SLEEP_MS + 5000) == LW_NONE);
    CHECK(lw_rx(&w, LW_SLEEP_MS + 6000) == (LW_WOKE | LW_RESUMED));
    CHECK(w.up && !w.asleep);

    lw_t b; lw_init(&b, 1000);                               /* 开机后没人连: 从开机算 10 min */
    CHECK(lw_tick(&b, 1000 + LW_SLEEP_MS - 1) == LW_NONE);
    CHECK(lw_tick(&b, 1000 + LW_SLEEP_MS) == LW_SLEEP);
    CHECK(lw_rx(&b, 1000 + LW_SLEEP_MS + 1) == LW_WOKE);     /* 没 HELLO 过: 醒, 但不算连上 */
    CHECK(!b.up && !b.asleep);
}

static void t_host_sleep(void){
    /* 主机发 SLEEP: 立刻睡, 主机算"走了"(不发 KEY), 不再触发 LOST / 自动休眠; 任何一帧叫醒并恢复 */
    lw_t w; lw_init(&w, 0);
    lw_hello(&w, 0);
    CHECK(lw_sleep(&w) == 1);                                /* 1 = 之前主机在 (调用方记下主机的灯) */
    CHECK(w.asleep && !w.up);
    CHECK(lw_tick(&w, LW_SLEEP_MS * 2) == LW_NONE);
    CHECK(lw_sleep(&w) == 0);                                /* 已经睡了 */
    CHECK(lw_rx(&w, LW_SLEEP_MS * 2 + 1) == (LW_WOKE | LW_RESUMED));
    CHECK(w.up && !w.asleep);
    CHECK(lw_tick(&w, LW_SLEEP_MS * 2 + 1 + LW_LOST_MS + 1) == LW_LOST);   /* 醒了之后照常计时 */
}

static void t_key_wakes(void){
    /* 按键叫醒 (没主机时也能看一眼): 不算主机回来, 10 min 重新计 */
    lw_t w; lw_init(&w, 0);
    CHECK(lw_key(&w, 10) == LW_NONE);                        /* 没睡: 什么都不做 */
    CHECK(lw_tick(&w, LW_SLEEP_MS) == LW_SLEEP);
    CHECK(lw_key(&w, LW_SLEEP_MS + 50) == LW_WOKE);
    CHECK(!w.asleep && !w.up);
    CHECK(lw_tick(&w, 2 * LW_SLEEP_MS + 49) == LW_NONE);
    CHECK(lw_tick(&w, 2 * LW_SLEEP_MS + 50) == LW_SLEEP);
}

static void t_unplug(void){
    /* USB 拔掉 (hotplug 回调): 不等 5 s, 马上当主机走了; 没连着时什么都不做 */
    lw_t w; lw_init(&w, 0);
    CHECK(lw_unplug(&w) == LW_NONE);
    lw_hello(&w, 0);
    CHECK(lw_unplug(&w) == LW_LOST);
    CHECK(!w.up);
    CHECK(lw_unplug(&w) == LW_NONE);
    CHECK(lw_tick(&w, LW_LOST_MS + 1) == LW_NONE);           /* 不重复报 */
}

static void t_wrap(void){
    lw_t w;
    uint32_t t = 0xFFFFFFFFu - 1000u;                        /* 跨 u32 回绕 */
    lw_init(&w, t);
    lw_hello(&w, t);
    CHECK(lw_tick(&w, t + LW_LOST_MS) == LW_NONE);
    CHECK(lw_tick(&w, t + LW_LOST_MS + 1) == LW_LOST);
    CHECK(lw_rx(&w, t + LW_LOST_MS + 2) == LW_RESUMED);
    CHECK(lw_tick(&w, t + LW_LOST_MS + 2 + LW_LOST_MS + 1) == LW_LOST);
    CHECK(lw_tick(&w, t + LW_LOST_MS + 2 + LW_SLEEP_MS) == LW_SLEEP);
}

int main(void){
    t_no_hello_no_link(); t_constants(); t_lost_after_silence(); t_resume_without_hello();
    t_idle_sleep(); t_host_sleep(); t_key_wakes(); t_unplug(); t_wrap();
    printf(fails ? "linkwd: %d FAIL\n" : "linkwd: all OK\n", fails);
    return fails ? 1 : 0;
}
