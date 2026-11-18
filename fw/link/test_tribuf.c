/* 主机单测: tribuf.h 的轮转状态机 + 脏区同步。随机操作序列 (RECT / COPY / PRESENT / 帧头) 对照单缓冲参考模型:
 *   - 每次 PRESENT 后, 刚提交的那块 == 参考画面 (逐像素);
 *   - 每个帧头, 被锁存扫描的那块 == 当时最新提交的画面;
 *   - 任何写入都不落在正在扫描 (s) 或待锁存 (p) 的块上, 且被扫描的块在扫描期间内容不变 (不撕裂)。
 * sh fw/link/test.sh */
#pragma GCC diagnostic ignored "-Wunused-function"
#include <stdio.h>
#include <string.h>
#include "proto.h"
#include "tribuf.h"

#define W 24u
#define H 20u
static int fails;
#define CHECK(c) do{ if(!(c)){ printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } }while(0)
static uint32_t rng = 7u;
static uint32_t rnd(uint32_t n){ rng = rng * 1103515245u + 12345u; return (rng >> 8) % n; }

static uint16_t bufs[TB_MAX][W * H] __attribute__((aligned(4)));
static uint16_t ref[W * H], scanned_snap[W * H];
static tb_t T;
static int hw_scan;            /* 硬件真正在扫的块 (模拟) */
static int hw_pending = -1;    /* 写进 0x34 还没锁存的 */

static void vsync(void){
    if (hw_pending >= 0){ hw_scan = hw_pending; hw_pending = -1; }
    memcpy(scanned_snap, bufs[hw_scan], sizeof scanned_snap);
}
static int wait_cb(void *ctx){ (void)ctx; vsync(); return 1; }   /* 等帧事件 = 让一个帧头发生 */

static void check_target(int d){
    CHECK(d >= 0 && d != hw_scan && d != hw_pending);
    CHECK(d != T.s && d != T.p);
}
static int mv(uintptr_t d, uintptr_t s, uint32_t n, void *ctx){
    (void)ctx;
    for (int i = 0; i < TB_MAX; i++){            /* 目的地必须是 d 那块 */
        uintptr_t b = (uintptr_t)bufs[i];
        if (d >= b && d < b + sizeof bufs[i]) CHECK(i == T.d || (T.n > 1 && T.d < 0));
    }
    memcpy((void *)d, (const void *)s, n); return 0;
}

static void run(uint32_t n, uint32_t steps){
    uintptr_t b[TB_MAX] = { (uintptr_t)bufs[0], (uintptr_t)bufs[1], (uintptr_t)bufs[2] };
    for (uint32_t i = 0; i < W * H; i++) ref[i] = (uint16_t)rnd(65536);
    for (int k = 0; k < TB_MAX; k++) memcpy(bufs[k], ref, sizeof ref);
    tb_init(&T, b, n, W, H);
    hw_scan = 0; hw_pending = -1; vsync();
    uint32_t presents = 0;
    for (uint32_t step = 0; step < steps; step++){
        uint32_t op = rnd(10);
        if (op < 3){                                        /* RECT: 随机矩形填随机色 */
            int d = tb_begin(&T, wait_cb, 0, mv, 0);
            if (n > 1) check_target(d);
            uint32_t x = rnd(W), y = rnd(H), w = 1 + rnd(W - x), h = 1 + rnd(H - y);
            uint16_t c = (uint16_t)rnd(65536);
            for (uint32_t r = y; r < y + h; r++) for (uint32_t q = x; q < x + w; q++){ bufs[d][r * W + q] = c; ref[r * W + q] = c; }
            tb_mark(&T, y, y + h);
        } else if (op < 6){                                 /* COPY */
            uint32_t w = 1 + rnd(W), h = 1 + rnd(H);
            uint32_t v[6] = { rnd(W - w + 1), rnd(H - h + 1), w, h, rnd(W - w + 1), rnd(H - h + 1) };
            uint8_t pl[12];
            for (int i = 0; i < 6; i++){ pl[2 * i] = (uint8_t)v[i]; pl[2 * i + 1] = (uint8_t)(v[i] >> 8); }
            bk_copy_t c; CHECK(bk_copy_parse(pl, 12, W, H, &c));
            uint32_t ry0 = H, ry1 = 0;
            lk_copy_apply(ref, W, H, pl, 12, &ry0, &ry1);
            if (!tb_copy(&T, &c, wait_cb, 0, mv, 0)){
                uint32_t y0 = H, y1 = 0;
                lk_copy_apply(bufs[T.d], W, H, pl, 12, &y0, &y1);
                tb_mark(&T, y0, y1);
            }
            if (n > 1) check_target(T.d);
        } else if (op < 8){                                 /* PRESENT */
            if (tb_need_wait_before_present(&T)){ wait_cb(0); tb_latched(&T); }
            int d = T.d;
            int r = tb_present(&T);
            if (r >= 0){
                CHECK(r == d);
                CHECK(memcmp(bufs[r], ref, sizeof ref) == 0);
                CHECK(hw_pending < 0);                      /* 不覆盖待锁存的基址 */
                hw_pending = r;
                presents++;
            } else if (n == 1){
                CHECK(memcmp(bufs[T.s], ref, sizeof ref) == 0);
            }
        } else {                                            /* 帧头 (固件里: 主循环看到帧事件) */
            vsync();
            tb_latched(&T);
        }
        /* 不撕裂: 正在扫描的块自上一个帧头以来没被改过 (单缓冲本来就会撕裂, 不查) */
        if (n > 1) CHECK(memcmp(bufs[hw_scan], scanned_snap, sizeof scanned_snap) == 0);
        /* 状态机对硬件的认识是保守的: 它认为的 s/p 覆盖了真实的扫描块 */
        if (n > 1) CHECK(hw_scan == T.s || hw_scan == T.p);
    }
    if (n > 1) CHECK(presents > 0);
}

int main(void){
    for (int k = 0; k < 200; k++){ run(3, 400); run(2, 400); run(1, 200); }
    /* 三缓冲: 连续两次 PRESENT 之间不需要等帧 (翻页后立刻可以画下一块) */
    uintptr_t b[TB_MAX] = { (uintptr_t)bufs[0], (uintptr_t)bufs[1], (uintptr_t)bufs[2] };
    tb_init(&T, b, 3, W, H);
    uint32_t waits0 = T.waits;
    tb_begin(&T, wait_cb, 0, mv, 0); tb_mark(&T, 0, 1); tb_present(&T);
    tb_begin(&T, wait_cb, 0, mv, 0);
    CHECK(T.waits == waits0 && T.d >= 0 && T.d != T.s && T.d != T.p);
    /* 双缓冲: 同样的序列要等一次 */
    tb_init(&T, b, 2, W, H);
    tb_begin(&T, wait_cb, 0, mv, 0); tb_mark(&T, 0, 1); tb_present(&T);
    tb_begin(&T, wait_cb, 0, mv, 0);
    CHECK(T.waits == 1);
    /* 重建状态后整屏同步 */
    tb_init(&T, b, 3, W, H); tb_reset_to(&T, 1);
    uint32_t lo, hi; tb_lag(&T, 0, &lo, &hi); CHECK(lo == 0 && hi == H);
    tb_lag(&T, 1, &lo, &hi); CHECK(hi <= lo);
    printf(fails ? "tribuf: %d FAILED\n" : "tribuf tests passed\n", fails);
    return fails != 0;
}
