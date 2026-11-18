/* fw/link/linkwd.h 的主机单测: sh fw/link/test.sh */
#include <stdio.h>
#include "linkwd.h"

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)

static void t_no_hello_no_link(void){
    lw_t w; lw_init(&w);
    CHECK(!w.up);
    CHECK(lw_rx(&w, 100) == LW_NONE);                        /* 没 HELLO 过: 有字节也不算连上 */
    CHECK(!w.up);
    CHECK(lw_tick(&w, 100 + LW_LOST_MS + 1) == LW_NONE);
}

static void t_lost_after_silence(void){
    lw_t w; lw_init(&w);
    lw_hello(&w, 0);
    CHECK(w.up);
    CHECK(lw_tick(&w, LW_LOST_MS) == LW_NONE);               /* 正好 15 s: 还算在 */
    lw_rx(&w, LW_LOST_MS);
    CHECK(lw_tick(&w, 2 * LW_LOST_MS) == LW_NONE);
    CHECK(lw_tick(&w, 2 * LW_LOST_MS + 1) == LW_LOST);
    CHECK(!w.up);
    CHECK(lw_tick(&w, 3 * LW_LOST_MS) == LW_NONE);           /* 只报一次 */
}

static void t_resume_without_hello(void){
    /* 主机停了 > 15 s (Mac 睡眠、进程被挂起) 又接着发, 不一定重发 HELLO: 字节一来就恢复, 不能一直当它不在 */
    lw_t w; lw_init(&w);
    lw_hello(&w, 0);
    CHECK(lw_tick(&w, LW_LOST_MS + 5) == LW_LOST);
    CHECK(lw_rx(&w, LW_LOST_MS + 100) == LW_RESUMED);
    CHECK(w.up);
    CHECK(lw_rx(&w, LW_LOST_MS + 200) == LW_NONE);           /* 已经在了: 不再报 */
}

static void t_wrap(void){
    lw_t w; lw_init(&w);
    uint32_t t = 0xFFFFFFFFu - 1000u;                        /* 跨 u32 回绕 */
    lw_hello(&w, t);
    CHECK(lw_tick(&w, t + LW_LOST_MS) == LW_NONE);
    CHECK(lw_tick(&w, t + LW_LOST_MS + 1) == LW_LOST);
    CHECK(lw_rx(&w, t + LW_LOST_MS + 2) == LW_RESUMED);
}

int main(void){
    t_no_hello_no_link(); t_lost_after_silence(); t_resume_without_hello(); t_wrap();
    printf(fails ? "linkwd: %d FAIL\n" : "linkwd: all OK\n", fails);
    return fails ? 1 : 0;
}
