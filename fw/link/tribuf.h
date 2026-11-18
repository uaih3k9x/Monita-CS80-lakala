/*
 * 多缓冲 (1..3 块) 的轮转状态机 + 脏区同步, 纯逻辑, 主机可测 (test_tribuf.c)。fw v6。
 *
 * 前提 (实测): 扫描基址 0x08807034 在帧头锁存, 帧事件 0x04 置位 = 刚过帧头。
 * 三个角色 (都是缓冲下标):
 *   s  正在扫描的
 *   p  已写进 0x34、还没被帧头锁存的 ("待锁存"); -1 = 没有
 *   d  正在画的 (下一批写这里); -1 = 还没选
 * 规矩: 只往 d 写, d 永远不等于 s 或 p。
 * PRESENT: 把 d 写进 0x34 -> p = d, d = -1, 立即应答。如果那时还有一个 p 没被锁存, 先等帧事件再写
 *          (不在帧头附近覆盖待锁存的基址, 避免"到底锁存了哪个"的竞态; 同时限速到 ≤ 37.5 fps)。
 * 下一批第一条写操作: 选一块既不是 s 也不是 p 的 (三缓冲时总有一块, 不用等; 双缓冲时要等 p 被锁存)。
 * 同步: 每块缓冲记着自己是第几版 (ver), 每次 PRESENT 记下那一版改了哪些行;
 *       新的 d 落后几版, 就把这几版改过的行 (并集, 按行区间) 从"最新已提交的那块" (p 或 s) 抄过来。
 *       第一条写操作是 COPY 时直接跨缓冲做 (blit.h), 抄回量减半。
 * 落后超过 TB_HIST 版时整屏抄。
 */
#ifndef MONITA_LINK_TRIBUF_H
#define MONITA_LINK_TRIBUF_H
#include <stdint.h>
#include "blit.h"

#define TB_MAX  3
#define TB_HIST 4u                     /* 记几版的脏行 (2 的幂) */

typedef struct {
    uint32_t n;                        /* 缓冲块数 1..3; 1 = 单缓冲 (直接画在 s 上) */
    uintptr_t buf[TB_MAX];
    int s, p, d;
    uint32_t ver[TB_MAX], gver;        /* 各块内容版本; 最新已提交版本 */
    uint32_t hlo[TB_HIST], hhi[TB_HIST];   /* 第 v 版改过的行 [lo,hi), 下标 v & (TB_HIST-1) */
    uint32_t w, h;                     /* 像素宽高 */
    uint32_t y0, y1;                   /* d 上写过还没 clean 的行 */
    uint32_t waits, xcopies, syncs, full_syncs;   /* 统计 */
} tb_t;

static inline void tb_init(tb_t *t, const uintptr_t *bufs, uint32_t n, uint32_t w, uint32_t h){
    t->n = n; t->s = 0; t->p = -1; t->d = n == 1 ? 0 : -1;
    for (uint32_t i = 0; i < TB_MAX; i++){ t->buf[i] = i < n ? bufs[i] : 0; t->ver[i] = 0; }
    for (uint32_t i = 0; i < TB_HIST; i++){ t->hlo[i] = 0; t->hhi[i] = 0; }
    t->gver = 0; t->w = w; t->h = h; t->y0 = h; t->y1 = 0;
    t->waits = t->xcopies = t->syncs = t->full_syncs = 0;
}

/* 最新已提交的那块 (同步的源; FBCRC 校验它) */
static inline int tb_latest(const tb_t *t){ return t->p >= 0 ? t->p : t->s; }

/* 观察到帧事件 (写 0x34 之后又置位): 待锁存的已生效 */
static inline void tb_latched(tb_t *t){ if (t->p >= 0){ t->s = t->p; t->p = -1; } }

/* 第 i 块要追上最新版需要抄的行 [*lo,*hi) (空 = lo >= hi) */
static inline void tb_lag(const tb_t *t, int i, uint32_t *lo, uint32_t *hi){
    uint32_t v = t->ver[i];
    *lo = t->h; *hi = 0;
    if (v >= t->gver) return;
    if (t->gver - v > TB_HIST - 1u){ *lo = 0; *hi = t->h; return; }
    for (uint32_t k = v + 1u; k <= t->gver; k++){
        uint32_t a = t->hlo[k & (TB_HIST - 1u)], b = t->hhi[k & (TB_HIST - 1u)];
        if (b <= a) continue;
        if (a < *lo) *lo = a;
        if (b > *hi) *hi = b;
    }
}

/* 可以画的块: 不是 s 也不是 p, 选版本最新的 (落后最少)。没有返回 -1 */
static inline int tb_pick(const tb_t *t){
    int best = -1;
    for (int i = 0; i < (int)t->n; i++){
        if (i == t->s || i == t->p) continue;
        if (best < 0 || t->ver[i] > t->ver[best]) best = i;
    }
    return best;
}

/* 等帧事件的回调: 阻塞到帧事件置位 (或超时), 返回 1 = 等到了。固件里是轮询 0x08807008 */
typedef int (*tb_wait_t)(void *ctx);

/* 确保有 d (还没同步)。双缓冲时可能要等一帧。返回 d */
static inline int tb_select(tb_t *t, tb_wait_t wait, void *ctx){
    if (t->d >= 0) return t->d;
    int i = tb_pick(t);
    if (i < 0){
        t->waits++;
        if (wait(ctx)) tb_latched(t);
        i = tb_pick(t);
        if (i < 0){ tb_latched(t); i = tb_pick(t); }     /* 超时兜底: 当作已锁存 (只在帧事件坏掉时发生) */
    }
    t->d = i;
    return i;
}

static inline void tb_mark(tb_t *t, uint32_t y0, uint32_t y1){
    if (y0 < t->y0) t->y0 = y0;
    if (y1 > t->y1) t->y1 = y1;
}

/* 选 d 并把落后的行抄齐 (RECT 等普通写操作之前调)。返回 d */
static inline int tb_begin(tb_t *t, tb_wait_t wait, void *ctx, bk_mover_t mv, void *mctx){
    int fresh = t->d < 0;
    int d = tb_select(t, wait, ctx);
    if (fresh && t->n > 1){
        uint32_t lo, hi; tb_lag(t, d, &lo, &hi);
        if (hi > lo){
            int L = tb_latest(t);
            bk_move(t->buf[d] + (uintptr_t)lo * t->w * 2u, t->buf[L] + (uintptr_t)lo * t->w * 2u, (hi - lo) * t->w * 2u, mv, mctx);
            tb_mark(t, lo, hi);
            t->syncs++; if (lo == 0 && hi == t->h) t->full_syncs++;
        }
        t->ver[d] = t->gver;
    }
    return d;
}

/* COPY: 如果这是 d 的第一条写操作且 d 落后, 跨缓冲做 (源 = 最新已提交的那块), 否则 tb_begin 后原地 memmove。
 * 返回 1 = 已用跨缓冲做完; 0 = 调用方在 buf[d] 上原地做 (tb_begin 已调过) */
static inline int tb_copy(tb_t *t, const bk_copy_t *c, tb_wait_t wait, void *ctx, bk_mover_t mv, void *mctx){
    if (t->n > 1 && t->d < 0){
        int d = tb_select(t, wait, ctx);
        uint32_t lo, hi; tb_lag(t, d, &lo, &hi);
        if (hi > lo){
            int L = tb_latest(t);
            uint32_t y0 = t->y0, y1 = t->y1;
            bk_cross_copy((uint16_t *)t->buf[d], (const uint16_t *)t->buf[L], t->w, c, lo, hi, mv, mctx, &y0, &y1);
            t->y0 = y0; t->y1 = y1;
            t->ver[d] = t->gver;
            t->xcopies++;
            return 1;
        }
        t->ver[d] = t->gver;
        return 0;
    }
    tb_begin(t, wait, ctx, mv, mctx);
    return 0;
}

/* PRESENT 第一步: 要不要先等 (还有待锁存的)。固件: 是就等帧事件, 等到了调 tb_latched */
static inline int tb_need_wait_before_present(const tb_t *t){ return t->n > 1 && t->p >= 0; }

/* PRESENT 第二步 (d 的脏行已 clean, 0x34 即将写 buf[返回值]): 记账。没有 d 或没改动返回 -1 (不用翻页) */
static inline int tb_present(tb_t *t){
    if (t->n == 1){ t->y0 = t->h; t->y1 = 0; return -1; }
    if (t->d < 0 || t->y1 <= t->y0) return -1;
    int d = t->d;
    t->gver++;
    t->hlo[t->gver & (TB_HIST - 1u)] = t->y0; t->hhi[t->gver & (TB_HIST - 1u)] = t->y1;
    t->ver[d] = t->gver;
    t->p = d; t->d = -1;
    t->y0 = t->h; t->y1 = 0;
    return d;
}

/* 外部 (DIAG 等) 直接摆弄过缓冲之后重建状态: 扫描的是第 s 块, 其余全部当作要整屏同步 */
static inline void tb_reset_to(tb_t *t, int s){
    t->s = s; t->p = -1; t->d = t->n == 1 ? s : -1;
    t->gver += TB_HIST;                               /* 让所有别的块都"落后太多" -> 整屏同步 */
    for (uint32_t i = 0; i < t->n; i++) t->ver[i] = (int)i == s ? t->gver : 0;
    t->y0 = t->h; t->y1 = 0;
}

#endif
