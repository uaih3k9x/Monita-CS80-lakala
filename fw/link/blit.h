/*
 * 双缓冲下 COPY 的"跨缓冲"做法 (fw v4), 纯逻辑, 主机可测 (test_blit.c)。
 *
 * 背景: PRESENT 翻页后, 前台 F = 本帧画面 S_N, 后台 B = 上一帧 S_{N-1}, 两者只在"待抄回行" [py0,py1) 里不同。
 * 原做法: 先把待抄回行 F->B (约 770 KB), 再在 B 里原地 memmove COPY (又约 770 KB)。
 * 这里: COPY 直接从 F 读、写进 B (跨缓冲, 不重叠, 可以一整段交给 mover), 再补三样:
 *   1. COPY 目标行里矩形外的列 (左右边距) 从 F 抄 (S_N 的边距);
 *      (整段搬运为了 4 字节对齐多搬的首尾像素也落在边距里, 一并修好);
 *   2. 待抄回行里落在 COPY 目标行之外的部分, 整行 F->B。
 * 结果和"先抄回再原地 COPY"逐像素一致, 搬运量减半。
 *
 * mover(dst, src, n): 不重叠的整段拷贝, dst/src/n 都 4 字节对齐 (app.c 里是 CPU ldm/stm)。返回 0 成功; 非 0 时这里用 CPU 重做。
 */
#ifndef MONITA_LINK_BLIT_H
#define MONITA_LINK_BLIT_H
#include <stdint.h>

typedef struct { uint32_t sx, sy, w, h, dx, dy; } bk_copy_t;
typedef int (*bk_mover_t)(uintptr_t dst, uintptr_t src, uint32_t n, void *ctx);

/* COPY 载荷 (u16 sx,sy,w,h,dx,dy) 校验, 规则同 lk_copy_apply */
static inline int bk_copy_parse(const uint8_t *pl, uint32_t len, uint32_t w_fb, uint32_t h_fb, bk_copy_t *c){
    if (len != 12) return 0;
    const uint32_t v[6] = { (uint32_t)(pl[0] | pl[1] << 8), (uint32_t)(pl[2] | pl[3] << 8), (uint32_t)(pl[4] | pl[5] << 8),
                            (uint32_t)(pl[6] | pl[7] << 8), (uint32_t)(pl[8] | pl[9] << 8), (uint32_t)(pl[10] | pl[11] << 8) };
    c->sx = v[0]; c->sy = v[1]; c->w = v[2]; c->h = v[3]; c->dx = v[4]; c->dy = v[5];
    if (c->w == 0 || c->h == 0 || c->sx + c->w > w_fb || c->sy + c->h > h_fb || c->dx + c->w > w_fb || c->dy + c->h > h_fb) return 0;
    return 1;
}

/* CPU 回退: 逐字拷 (n、地址 4 对齐) */
static inline int bk_cpu_move(uintptr_t dst, uintptr_t src, uint32_t n, void *ctx){
    (void)ctx;
    uint32_t *d = (uint32_t *)dst; const uint32_t *s = (const uint32_t *)src;
    for (uint32_t i = 0; i < (n >> 2); i++) d[i] = s[i];
    return 0;
}

static inline void bk_move(uintptr_t dst, uintptr_t src, uint32_t n, bk_mover_t mv, void *ctx){
    if (!n) return;
    if (mv(dst, src, n, ctx) != 0) bk_cpu_move(dst, src, n, 0);
}

/* 半字逐个抄 (边距 / 首尾修补用, 量很小) */
static inline void bk_px(uint16_t *d, const uint16_t *s, uint32_t n){ for (uint32_t i = 0; i < n; i++) d[i] = s[i]; }

/* 执行跨缓冲 COPY。fb 尺寸 w_fb x h_fb (w_fb 偶数), 两块缓冲 4 字节对齐。
 * [py0, py1) 待抄回行 (py1 <= py0 表示没有)。返回后 B = COPY(S_N), 调用方把 [*y0,*y1) 记为脏行 (要 clean)。 */
static inline void bk_cross_copy(uint16_t *b, const uint16_t *f, uint32_t w_fb, const bk_copy_t *c,
                                 uint32_t py0, uint32_t py1, bk_mover_t mv, void *ctx, uint32_t *y0, uint32_t *y1){
    const uint32_t ds = c->dy * w_fb + c->dx, ss = c->sy * w_fb + c->sx;      /* 像素下标 */
    const uint32_t n = (c->h - 1u) * w_fb + c->w;                            /* 整段跨度 (像素) */
    if (((ds ^ ss) & 1u) == 0){
        /* 起点向下、终点向上对齐到 4 字节, 首尾最多多搬 1 个像素。w_fb 是偶数, 所以多搬的像素
         * (起点奇 <=> dx 奇 >= 1; 终点奇 <=> dx+w 奇 < w_fb) 一定落在本行的左右边距里, 由下面的边距修补覆盖 */
        uint32_t a = ds & ~1u, e = (ds + n + 1u) & ~1u, sa = ss - (ds - a);
        bk_move((uintptr_t)(b + a), (uintptr_t)(f + sa), (e - a) * 2u, mv, ctx);
    } else {
        /* 源/目标奇偶不同, 没法同时对齐: CPU 逐行 (罕见: sx 与 dx 奇偶不同) */
        for (uint32_t r = 0; r < c->h; r++) bk_px(b + ds + r * w_fb, f + ss + r * w_fb, c->w);
    }
    /* 目标行的左右边距 (整段搬运把行间的边距也带过来了, 这里恢复成 S_N) */
    for (uint32_t r = c->dy; r < c->dy + c->h; r++){
        bk_px(b + r * w_fb, f + r * w_fb, c->dx);
        bk_px(b + r * w_fb + c->dx + c->w, f + r * w_fb + c->dx + c->w, w_fb - c->dx - c->w);
    }
    /* 待抄回行中 COPY 目标行以外的部分 */
    uint32_t lo = c->dy, hi = c->dy + c->h;
    if (py1 > py0){
        uint32_t t = py1 < lo ? py1 : lo;
        if (t > py0) bk_move((uintptr_t)(b + py0 * w_fb), (uintptr_t)(f + py0 * w_fb), (t - py0) * w_fb * 2u, mv, ctx);
        t = py0 > hi ? py0 : hi;
        if (py1 > t) bk_move((uintptr_t)(b + t * w_fb), (uintptr_t)(f + t * w_fb), (py1 - t) * w_fb * 2u, mv, ctx);
        if (py0 < lo) lo = py0;
        if (py1 > hi) hi = py1;
    }
    if (lo < *y0) *y0 = lo;
    if (hi > *y1) *y1 = hi;
}

#endif
