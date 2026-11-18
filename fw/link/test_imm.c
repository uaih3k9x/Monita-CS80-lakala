/* fw/link/imm.h 的主机单测: sh fw/link/test.sh
 * 模拟一块会"帧头锁存基址"的扫描控制器 + 卷轴 + 保留缓冲, 随机序列对照参考模型 (主机写过的逻辑行内容), 每帧检查:
 *   - 屏上每一行 == 主机最后写给这个逻辑行的内容 (主机写过的行);
 *   - 写进基址的卷轴行 r 满足 r + H <= R;
 *   - 隐藏写 / 换底搬运从不碰正在扫描或待锁存的行 (不撕裂)。 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "imm.h"

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; if (fails > 20) exit(1); } } while (0)

/* ---- 模拟硬件 ---- */
#define MAXR 4096
#define TRUTH (1u << 21)
typedef struct { int buf; uint32_t row; uint32_t log; } scan_t;
static uint32_t roll[MAXR], hold[MAXR], truth[TRUTH];   /* 每行一个值 (= 这一行像素的指纹) */
static uint32_t R, H;
static scan_t reg, cur;            /* reg: 写进基址的; cur: 正在扫描的 (帧头锁存) */
static int evt, cur_valid;
static uint32_t now_ms, frames, next_frame;
static im_t M;
static uint32_t pending_log;       /* 下一次 base() 对应的逻辑窗口顶 (由调用方在写基址前设好) */

static uint32_t *bufp(int b){ return b == IM_BUF_ROLL ? roll : hold; }
static uint32_t buflen(int b){ return b == IM_BUF_ROLL ? R : H; }

/* 这一行此刻会不会被扫到 (正在扫描的, 或写进基址还没锁存的) */
static int scanned(int b, uint32_t row){
    if (cur_valid && cur.buf == b && row >= cur.row && row < cur.row + H) return 1;
    if (reg.buf == b && row >= reg.row && row < reg.row + H) return 1;
    return 0;
}

static void check_display(void){
    if (!cur_valid || cur.buf != IM_BUF_ROLL) return;
    for (uint32_t i = 0; i < H; i++){
        uint32_t want = truth[(cur.log + i) & (TRUTH - 1)];
        if (want && roll[cur.row + i] != want){ CHECK(roll[cur.row + i] == want); return; }
    }
}

static void frame(void){ cur = reg; cur_valid = 1; evt = 1; frames++; check_display(); }

static void io_copy(void *ctx, int db, uint32_t dr, int sb, uint32_t sr, uint32_t n){
    (void)ctx;
    CHECK(dr + n <= buflen(db) && sr + n <= buflen(sb));
    uint32_t *d = bufp(db), *s = bufp(sb);
    for (uint32_t i = 0; i < n; i++){
        CHECK(!scanned(db, dr + i));                       /* 搬运不碰屏上的行 */
        d[dr + i] = s[sr + i];
    }
}
static void io_base(void *ctx, int b, uint32_t row){
    (void)ctx;
    if (b == IM_BUF_ROLL) CHECK(row + H <= R);             /* 不变量: 整帧不出卷轴 */
    else CHECK(row == 0);
    reg.buf = b; reg.row = row; reg.log = pending_log; evt = 0;
}
static void io_wait(void *ctx){ (void)ctx; now_ms += 27; next_frame = now_ms + 27; frame(); }
static const im_io_t IO = { io_copy, io_base, io_wait, 0 };

static void sim_reset(uint32_t r, uint32_t h, uint32_t y0){
    R = r; H = h;
    memset(roll, 0, sizeof roll); memset(hold, 0, sizeof hold); memset(truth, 0, sizeof truth);
    reg.buf = IM_BUF_HOLD; reg.row = 0; reg.log = 0; cur = reg; cur_valid = 1; evt = 0;
    now_ms = 1000; next_frame = now_ms + 27; frames = 0;
    CHECK(im_init(&M, R, H, y0) == 0);
}

/* 固件胶水的同款 (fw/app.c): 卷轴不够时先试换底 */
static void fw_rebase(void){ pending_log = M.ys; im_rebase(&M, &IO); }
static int fw_write(uint32_t y, uint32_t h, uint32_t flags, uint32_t val){
    uint32_t row;
    int r = im_write(&M, y, h, flags, &row);
    if (r == IM_ERANGE && im_fits_after_rebase(&M, y, h)){
        im_settle(&M, &IO, now_ms);
        if (im_can_rebase(&M)){ fw_rebase(); r = im_write(&M, y, h, flags, &row); }
    }
    if (r != IM_OK) return r;
    for (uint32_t i = 0; i < h; i++){
        if (!(flags & IM_F_VISIBLE)) CHECK(!scanned(IM_BUF_ROLL, row + i));   /* 隐藏写不撕裂 */
        roll[row + i] = val + i;
        truth[(y + i) & (TRUTH - 1)] = val + i;
    }
    return IM_OK;
}
static int fw_scroll(uint32_t y, uint32_t dur){
    int r = im_scroll(&M, y, dur, now_ms);
    if (r == IM_ERANGE && im_fits_after_rebase(&M, y, H)){
        im_settle(&M, &IO, now_ms);
        if (im_can_rebase(&M)){ fw_rebase(); r = im_scroll(&M, y, dur, now_ms); }
    }
    return r;
}
/* 主循环一轮: 推进 dt ms (中间过帧头就锁存), 再 tick */
static void fw_poll(uint32_t dt){
    for (uint32_t k = 0; k < dt; k++){ now_ms++; if (now_ms == next_frame){ frame(); next_frame += 27; } }
    int32_t r = im_tick(&M, now_ms, evt);
    if (r >= 0){ pending_log = M.yp; io_base(0, IM_BUF_ROLL, (uint32_t)r); }
    else if (im_should_rebase(&M, now_ms)) fw_rebase();
    if (M.son) CHECK(M.ys - M.org <= R - H);
}

/* ---- 用例 ---- */
static void t_ease(void){
    CHECK(im_ease_q16(0, 300) == 0);
    CHECK(im_ease_q16(300, 300) == 65536u && im_ease_q16(999, 300) == 65536u && im_ease_q16(5, 0) == 65536u);
    uint32_t prev = 0;
    for (uint32_t t = 0; t <= 65535; t += 97){ uint32_t e = im_ease_q16(t, 65535); CHECK(e >= prev); prev = e; }
    CHECK(im_ease_q16(150, 300) > 32768u);                 /* ease-out: 一半时间走完一大半 */
    CHECK(im_udiv(1000000u, 1600u) == 625u && im_udiv(7u, 3u) == 2u);
}

static void t_init_and_bounds(void){
    im_t m;
    CHECK(im_init(&m, 1280 + IM_MIN_AHEAD - 1, 1280, 0) == -1);   /* 卷轴太小: 不开 */
    CHECK(im_init(&m, 1280 + IM_MIN_AHEAD, 1280, 0) == 0);
    uint32_t row;
    CHECK(im_write(&m, 0, 0, 0, &row) == IM_EPARAM);
    CHECK(im_write(&m, m.R - 10, 11, 0, &row) == IM_ERANGE);       /* 末尾越界 */
    CHECK(im_write(&m, 0xFFFFFFFFu, 2, 0, &row) == IM_ERANGE);     /* org 之前 */
    CHECK(im_write(&m, 0, 1280, 0, &row) == IM_OK && row == 0);    /* 没上屏: 都能写 */
    CHECK(im_scroll(&m, m.R - 1280 + 1, 0, 0) == IM_ERANGE);       /* 目标窗口出卷轴 */
    CHECK(im_scroll(&m, 0, 300, 0) == IM_OK);                      /* 第一次: 直接跳 */
    CHECK(im_tick(&m, 1, 0) == 0);
    CHECK(im_write(&m, 100, 4, 0, &row) == IM_EVIS);               /* 待锁存的窗口也算可见 */
    CHECK(im_write(&m, 100, 4, IM_F_VISIBLE, &row) == IM_OK && row == 100);
    CHECK(im_write(&m, 1280, 4, 0, &row) == IM_OK);                /* 窗口下面: 隐藏 */
    CHECK(im_tick(&m, 30, 1) == -1 && m.son && m.ys == 0);         /* 锁存了, 停在目标上 */
    CHECK(im_scroll(&m, 40000, 300, 30) != IM_OK);                 /* 出卷轴 / 一次太远 */
}

static void t_overlap(void){
    im_t m; im_init(&m, 2000, 100, 1000);
    CHECK(im_overlap(&m, 1000, 999, 1) == 0 && im_overlap(&m, 1000, 999, 2) == 1);
    CHECK(im_overlap(&m, 1000, 1099, 5) == 1 && im_overlap(&m, 1000, 1100, 5) == 0);
    CHECK(im_overlap(&m, 0xFFFFFFF0u, 5, 5) == 1);                 /* 回绕 */
}

/* 顺序场景 (像歌词): 每次先把下一段写在窗口下面 (隐藏), 再缓动滚过去, 偶尔改屏上的颜色 (可见写)。
 * 跑过很多次换底, 两种换底 (直接 / 绕道) 都要出现 */
static void t_lyrics_like(uint32_t r, uint32_t h, int steps){
    sim_reset(r, h, 5000);
    uint32_t val = 1;
    CHECK(fw_write(5000, h, 0, val) == IM_OK); val += h;          /* 第一屏 */
    uint32_t bottom = 5000 + h;                                    /* 写到哪了 */
    CHECK(fw_scroll(5000, 300) == IM_OK);
    fw_poll(30);
    uint32_t y = 5000;
    srand(r * 31u + h);
    for (int s = 0; s < steps; s++){
        uint32_t step = 1 + (uint32_t)rand() % (h / 3 + 1);
        if (step > 200) step = 200;
        while (bottom < y + step + h){                             /* 补齐将要露出来的行 */
            uint32_t n = 1 + (uint32_t)rand() % 20;
            CHECK(fw_write(bottom, n, 0, val) == IM_OK); val += n; bottom += n;
        }
        if (rand() % 3 == 0){                                      /* 改一下屏上的颜色 */
            uint32_t a = y + (uint32_t)rand() % h, n = 1 + (uint32_t)rand() % 10;
            if (a + n > bottom) n = bottom - a;
            if (n) CHECK(fw_write(a, n, IM_F_VISIBLE, val) == IM_OK);
            val += 16;
        }
        y += step;
        CHECK(fw_scroll(y, (uint32_t)(rand() % 2 ? 0 : 100 + rand() % 400)) == IM_OK);
        uint32_t last = M.ys;
        for (int k = 0; k < 120; k++){
            fw_poll(1 + (uint32_t)rand() % 15);
            if (M.son) { CHECK((int32_t)(M.ys - last) >= 0); last = M.ys; }   /* 往下滚: 单调 */
        }
        CHECK(!M.anim && M.ys == y);                               /* 900 ms 内一定到位 */
        fw_poll(IM_REBASE_IDLE + 30);                              /* 停稳 -> 可能主动换底 */
    }
    CHECK(M.rebases > 3);
    printf("  R=%u H=%u steps=%d frames=%u rebases=%u (绕道 %u) base_writes=%u\n",
           R, H, steps, frames, M.rebases, M.hops, M.base_writes);
}

/* 随机序列: 任意写 (可见 / 隐藏 / 越界)、任意滚动 (含往回、跳)、偶尔重新开始 */
static void t_random(uint32_t r, uint32_t h, int ops, unsigned seed){
    srand(seed);
    sim_reset(r, h, 100000);
    uint32_t val = 1;
    fw_write(100000, h, 0, val); val += h;
    fw_scroll(100000, 0);
    int ok_w = 0, rej_w = 0, ok_s = 0;
    for (int i = 0; i < ops; i++){
        int op = rand() % 10;
        uint32_t base = M.ytgt;
        if (op < 5){
            uint32_t y = base - 100 + (uint32_t)rand() % (R + 200), n = 1 + (uint32_t)rand() % 30;
            uint32_t fl = rand() % 5 == 0 ? IM_F_VISIBLE : 0;
            if (fw_write(y, n, fl, val) == IM_OK) ok_w++; else rej_w++;
            val += n;
        } else if (op < 7){
            uint32_t y = base - h / 2 + (uint32_t)rand() % (2 * h);
            if (fw_scroll(y, rand() % 3 ? (uint32_t)(rand() % 500) : 0) == IM_OK) ok_s++;
        } else if (op < 9){
            fw_poll(1 + (uint32_t)rand() % 40);
        } else if (rand() % 20 == 0){
            uint32_t y0 = 100000 + (uint32_t)rand() % 50000;
            im_hold(&M, &IO, now_ms);
            CHECK(cur.buf == IM_BUF_HOLD || !M.son);
            im_restart(&M, y0);
            memset(truth, 0, sizeof truth);
            fw_write(y0, h, 0, val); val += h;
            fw_scroll(y0, 0);
        }
    }
    CHECK(ok_w > 0 && rej_w > 0 && ok_s > 0);
}

int main(void){
    t_ease(); t_init_and_bounds(); t_overlap();
    t_lyrics_like(1280 + 1440, 1280, 300);     /* ≈ 4.3 MB: 释放第二、三块后的典型大小 */
    t_lyrics_like(1280 + IM_MIN_AHEAD, 1280, 300);   /* 最小卷轴: 换底很勤, 全走绕道 */
    t_lyrics_like(64 + 700, 64, 400);          /* 小屏: 直接换底也会出现 */
    for (unsigned s = 1; s <= 40; s++) t_random(64 + IM_MIN_AHEAD + (s * 37u) % 600u, 64, 3000, s);
    t_random(1280 + 1440, 1280, 3000, 99);
    printf(fails ? "imm: %d FAIL\n" : "imm: all OK\n", fails);
    return fails ? 1 : 0;
}
