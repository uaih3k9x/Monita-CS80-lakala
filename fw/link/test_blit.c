/* 主机单测: bk_cross_copy 和"先抄回待抄回行, 再原地 COPY (lk_copy_apply)"逐像素一致。sh fw/link/test.sh */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#pragma GCC diagnostic ignored "-Wunused-function"
#include "proto.h"
#include "blit.h"

#define W 64u
#define H 48u
static int fails;
#define CHECK(c) do{ if(!(c)){ printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } }while(0)

static uint32_t rng = 12345u;
static uint32_t rnd(uint32_t n){ rng = rng * 1103515245u + 12345u; return (rng >> 8) % n; }

static int calls, fail_next;
static int mv_count(uintptr_t d, uintptr_t s, uint32_t n, void *ctx){
    (void)ctx; calls++;
    if (((d | s | n) & 3u) != 0){ printf("FAIL mover 未对齐 %lx %lx %u\n", (unsigned long)d, (unsigned long)s, n); fails++; }
    if (fail_next){ fail_next = 0; return 1; }           /* 模拟 mover 失败: 应由 CPU 逐字重做 */
    memcpy((void *)d, (const void *)s, n);
    return 0;
}

static void one(int force_fail){
    static uint16_t f[W * H] __attribute__((aligned(4))), b[W * H] __attribute__((aligned(4))), ref[W * H] __attribute__((aligned(4)));
    for (uint32_t i = 0; i < W * H; i++){ f[i] = (uint16_t)rnd(65536); b[i] = f[i]; }
    uint32_t py0 = rnd(H), py1 = py0 + rnd(H - py0 + 1);       /* 可能为空 */
    for (uint32_t r = py0; r < py1; r++) for (uint32_t x = 0; x < W; x++) b[r * W + x] = (uint16_t)rnd(65536);
    uint8_t pl[12]; uint32_t w = 1 + rnd(W), h = 1 + rnd(H);
    uint32_t v[6] = { rnd(W - w + 1), rnd(H - h + 1), w, h, rnd(W - w + 1), rnd(H - h + 1) };
    for (int i = 0; i < 6; i++){ pl[2 * i] = (uint8_t)v[i]; pl[2 * i + 1] = (uint8_t)(v[i] >> 8); }
    /* 参考: B <- F (整块抄回), 再原地 COPY */
    memcpy(ref, f, sizeof ref);
    uint32_t ry0 = H, ry1 = 0;
    CHECK(lk_copy_apply(ref, W, H, pl, 12, &ry0, &ry1) == LK_OK);
    bk_copy_t c; CHECK(bk_copy_parse(pl, 12, W, H, &c));
    uint32_t y0 = H, y1 = 0;
    fail_next = force_fail;
    bk_cross_copy(b, f, W, &c, py0, py1, mv_count, 0, &y0, &y1);
    CHECK(memcmp(b, ref, sizeof ref) == 0);
    /* 脏行至少覆盖 COPY 目标行和待抄回行 */
    CHECK(y0 <= c.dy && y1 >= c.dy + c.h);
    if (py1 > py0) CHECK(y0 <= py0 && y1 >= py1);
}

int main(void){
    for (int i = 0; i < 3000; i++) one(i % 7 == 0);
    /* 参数校验 */
    uint8_t bad[12] = { 0 };                                      /* w = h = 0 */
    bk_copy_t c; CHECK(!bk_copy_parse(bad, 12, W, H, &c));
    uint8_t ok[12] = { 0, 0, 0, 0, W, 0, 1, 0, 0, 0, 1, 0 };      /* 整行, 下移 1 行 */
    CHECK(bk_copy_parse(ok, 12, W, H, &c) && c.w == W && c.dy == 1);
    CHECK(!bk_copy_parse(ok, 11, W, H, &c));
    /* 歌词滚动的典型形状: 一段整段搬 + 边距; mover 次数 = 1 段 + 待抄回行上下各最多 1 段 */
    printf(fails ? "blit: %d FAILED\n" : "blit tests passed\n", fails);
    return fails != 0;
}
