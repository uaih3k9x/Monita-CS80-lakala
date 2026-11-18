/*
 * 沉浸模式 (fw v9): 整屏歌词, 滚动 = 改扫描基址 (零拷贝)。纯逻辑, 不碰硬件, 主机单测 test_imm.c。协议见 docs/protocol.md。
 *
 * 硬件前提 (实机, docs/r05-notes.md §4): 扫描基址 0x08807034 在帧头锁存 (写入立刻、下一帧整帧生效, 不撕裂);
 * 基址可以指到缓冲里任意一行; 基址 + 整帧 (1280 行) 读出缓冲之外就花屏。没有窗口/叠加层。
 *
 * 卷轴: 一块 R 行 (R >= H + IM_MIN_AHEAD) 的线性缓冲, 行跨同屏 (800 px)。
 *   主机用"逻辑行号" y (u32) 写行; 逻辑行 y 落在卷轴第 y - org 行 (org = 卷轴第 0 行对应的逻辑行)。
 *   屏上显示逻辑行 [Y, Y+H) = 卷轴第 [Y-org, Y-org+H) 行, 基址 = 卷轴 + (Y-org) 行。
 * 可见: 正在扫描的窗口 ys 和已写进基址、还没锁存的窗口 yp (两个都可能正在上屏)。写这些行要带 IM_F_VISIBLE
 *   (主机自己负责: 只拿来改颜色, 最多撕裂一帧); 不带就拒 (IM_EVIS)。
 * 走到卷轴末尾: 不在动画、已锁存时整体"换底" (rebase), 把 [窗口顶, 写到过的最高行) 搬回卷轴顶部, org = 窗口顶:
 *   - 直接: 搬的行数 <= 窗口顶所在行 (目标 [0,n) 和正在扫描的窗口不重叠): 搬完写一次基址, 等锁存;
 *   - 绕道: 否则先把窗口抄进保留缓冲 (原厂 fb, 平时不用), 切过去等锁存, 卷轴整个不可见了再搬, 再切回来等锁存。
 *   两种都是"屏上内容不变, 只换扫描地址", 不可见、不撕裂。耗时 1–2 帧 + 搬运 (整屏 ≈ 30 ms)。
 * 不变量 (单测逐步检查): 任何时候写进基址的卷轴行 r 满足 r + H <= R; 隐藏写不会碰正在扫描或待锁存的行。
 *
 * 不用 '/' '%' (A5 没有硬件除法, 不链 libgcc), 缓动除法用 im_udiv。时间是 u32 毫秒 (固件 1.024 ms 一单位), 允许回绕。
 */
#ifndef MONITA_IMM_H
#define MONITA_IMM_H
#include <stdint.h>

#define IM_MIN_AHEAD   256u      /* 卷轴至少比屏多这么多行, 否则不开沉浸模式 */
#define IM_MAX_STEP    32767     /* 一次滚动最多这么多行 (缓动 Q16 乘法不溢出) */
#define IM_REBASE_IDLE 200u      /* 动画停了这么久 (ms) 才主动换底, 给主机留出改色的时间 */
#define IM_F_VISIBLE   0x01u     /* IMM_RECT flags: 允许写可见行 */

#define IM_OK     0
#define IM_EVIS  -1              /* 碰到可见行 (没带 IM_F_VISIBLE) */
#define IM_ERANGE -2             /* 不在卷轴里 (换底也许能腾出来, 见 im_fits_after_rebase) */
#define IM_EPARAM -3

typedef struct {
    uint32_t R, H;               /* 卷轴行数, 屏高 */
    uint32_t org;                /* 卷轴第 0 行 = 逻辑行 org */
    uint32_t hiw;                /* 卷轴里 [0, hiw) 可能有内容 (换底时要搬的上界) */
    uint8_t  son;                /* 正在扫描卷轴 (否则扫描的是保留缓冲) */
    uint8_t  pend;               /* 卷轴基址已写、还没锁存 */
    uint8_t  show_req;           /* 还没上过屏, 等第一次 SCROLL 后的 tick 写基址 */
    uint8_t  anim;
    uint32_t ys, yp, ytgt;       /* 扫描中 / 待锁存 / 最终目标 的窗口顶 (逻辑行) */
    uint32_t a_y0, a_t0, a_dur;  /* 缓动: 起点、开始时刻、时长 */
    int32_t  a_d;                /* 缓动: 位移 (行, 带符号) */
    uint32_t idle_at;            /* 上一次动画结束 / 基址锁存的时刻 */
    uint32_t base_writes, rebases, hops;   /* 统计 */
} im_t;

/* ---- 搬运 / 基址 / 等帧 的回调 (固件: ldm/stm + clean、ql_mipi_lcd_write_screen + 清事件位、轮询帧事件) ---- */
#define IM_BUF_ROLL 0            /* 卷轴 */
#define IM_BUF_HOLD 1            /* 保留缓冲 (H 行) */
typedef struct {
    /* 按行升序逐行搬 n 行 (同一块里 dst < src 时就是 memmove), 搬完 clean */
    void (*copy)(void *ctx, int dbuf, uint32_t drow, int sbuf, uint32_t srow, uint32_t n);
    /* 写扫描基址 = buf 的第 row 行 (并清掉帧事件位) */
    void (*base)(void *ctx, int buf, uint32_t row);
    /* 等帧事件 (= 刚写的基址已锁存); 超时也返回 */
    void (*wait)(void *ctx);
    void *ctx;
} im_io_t;

static inline uint32_t im_udiv(uint32_t n, uint32_t d){
    uint32_t q = 0, r = 0;
    for (int i = 31; i >= 0; i--){
        r = (r << 1) | ((n >> i) & 1u);
        if (r >= d){ r -= d; q |= 1u << i; }
    }
    return q;
}

/* ease-out cubic, t / dur ∈ [0,1] -> Q16 [0, 65536] */
static inline uint32_t im_ease_q16(uint32_t t, uint32_t dur){
    if (!dur || t >= dur) return 65536u;
    uint32_t u = im_udiv(t << 16, dur);                  /* t < dur <= 65535: t << 16 不溢出 */
    uint64_t v = 65536u - u;
    uint64_t v3 = (((v * v) >> 16) * v) >> 16;
    return 65536u - (uint32_t)v3;
}

/* R 行卷轴、屏高 H、起点逻辑行 y0。返回 0 = OK, -1 = 卷轴太小 */
static inline int im_init(im_t *m, uint32_t R, uint32_t H, uint32_t y0){
    uint8_t *z = (uint8_t *)m;
    for (unsigned i = 0; i < sizeof *m; i++) z[i] = 0;
    if (R < H + IM_MIN_AHEAD) return -1;
    m->R = R; m->H = H; m->org = y0; m->ys = m->yp = m->ytgt = y0;
    return 0;
}

/* 重新开始 (换歌 / 往回跳): 调用方先 im_hold (屏上留着最后一帧), 卷轴整个作废 */
static inline void im_restart(im_t *m, uint32_t y0){
    m->org = y0; m->hiw = 0; m->son = 0; m->pend = 0; m->show_req = 0; m->anim = 0;
    m->ys = m->yp = m->ytgt = y0;
}

/* 逻辑行 [y, y+h) 和窗口 [w, w+H) 重叠 */
static inline int im_overlap(const im_t *m, uint32_t w, uint32_t y, uint32_t h){
    return (uint32_t)(y - w) < m->H || (uint32_t)(w - y) < h;
}

/* 逻辑行 [y, y+h) 现在有没有可能在屏上 */
static inline int im_visible(const im_t *m, uint32_t y, uint32_t h){
    if (m->son && im_overlap(m, m->ys, y, h)) return 1;
    if (m->pend && im_overlap(m, m->yp, y, h)) return 1;
    return 0;
}

/* 写 [y, y+h) 之前检查; OK 时 *row = 卷轴行号 (调用方逐行写, 每行写完 clean) */
static inline int im_write(im_t *m, uint32_t y, uint32_t h, uint32_t flags, uint32_t *row){
    if (h == 0 || h > m->R) return IM_EPARAM;
    uint32_t off = y - m->org;
    if (off > m->R - h) return IM_ERANGE;
    if (!(flags & IM_F_VISIBLE) && im_visible(m, y, h)) return IM_EVIS;
    if (off + h > m->hiw) m->hiw = off + h;
    *row = off;
    return IM_OK;
}

/* 现在能不能换底 (不在动画、没有待锁存、已上屏且停在目标上) */
static inline int im_can_rebase(const im_t *m){
    return m->son && !m->pend && !m->anim && !m->show_req && m->ys == m->ytgt;
}

/* 换底 (org = 窗口顶) 之后 [y, y+h) 能不能放进卷轴 */
static inline int im_fits_after_rebase(const im_t *m, uint32_t y, uint32_t h){
    return h <= m->R && (uint32_t)(y - m->ys) <= m->R - h;
}

/* 当前缓动位置 */
static inline uint32_t im_pos(const im_t *m, uint32_t now){
    if (!m->anim) return m->ytgt;
    uint32_t t = now - m->a_t0;
    if (t >= m->a_dur) return m->ytgt;
    uint32_t e = im_ease_q16(t, m->a_dur);
    uint32_t mag = (uint32_t)(m->a_d < 0 ? -m->a_d : m->a_d);
    uint32_t s = (mag * e) >> 16;                        /* mag < 2^15, e <= 2^16: 不溢出 */
    return m->a_d < 0 ? m->a_y0 - s : m->a_y0 + s;
}

/* 滚到窗口顶 = y, dur 毫秒 (0 = 下一帧直接跳)。第一次 (还没上屏) 一律直接跳。
 * 返回 IM_OK / IM_ERANGE (目标窗口不在卷轴里) / IM_EPARAM (一次滚太远) */
static inline int im_scroll(im_t *m, uint32_t y, uint32_t dur, uint32_t now){
    if ((uint32_t)(y - m->org) > m->R - m->H) return IM_ERANGE;
    if (!m->son && !m->pend){                            /* 还没上屏: 下一个 tick 直接显示 */
        m->ytgt = y; m->anim = 0; m->show_req = 1;
        return IM_OK;
    }
    uint32_t from = im_pos(m, now);
    int32_t d = (int32_t)(y - from);
    if (dur && (d > IM_MAX_STEP || d < -IM_MAX_STEP)) return IM_EPARAM;
    m->ytgt = y;
    m->anim = (dur && d) ? 1 : 0;
    if (m->anim){ m->a_y0 = from; m->a_d = d; m->a_t0 = now; m->a_dur = dur; }
    return IM_OK;
}

/* 主循环每轮调; evt = 帧事件位置位 (写基址之后)。返回要写进基址的卷轴行号, -1 = 不用写。
 * 一次只有一个待锁存的基址 (同 tribuf): 37.5 fps 上限, "可见"的判断才有确定答案 */
static inline int32_t im_tick(im_t *m, uint32_t now, int evt){
    if (m->pend && evt){ m->ys = m->yp; m->pend = 0; m->son = 1; m->idle_at = now; }
    if (m->pend) return -1;
    if (m->anim && now - m->a_t0 >= m->a_dur){ m->anim = 0; m->idle_at = now; }
    if (!m->son && !m->show_req) return -1;
    uint32_t want = im_pos(m, now);
    if (m->son && !m->show_req && want == m->ys) return -1;
    uint32_t row = want - m->org;
    if (row > m->R - m->H) return -1;                    /* 不会发生 (目标都校验过); 保底: 宁可不动也不花屏 */
    m->yp = want; m->pend = 1; m->show_req = 0; m->base_writes++;
    return (int32_t)row;
}

/* 等掉待锁存的 (同步) */
static inline void im_settle(im_t *m, const im_io_t *io, uint32_t now){
    if (!m->pend) return;
    io->wait(io->ctx);
    m->ys = m->yp; m->pend = 0; m->son = 1; m->idle_at = now;
}

/* 该不该主动换底: 停稳了一会儿, 前面剩的行不到一半 */
static inline int im_should_rebase(const im_t *m, uint32_t now){
    if (!im_can_rebase(m) || m->ys == m->org) return 0;
    if (now - m->idle_at < IM_REBASE_IDLE) return 0;
    uint32_t ahead = m->R - (m->ys - m->org) - m->H;
    return ahead < ((m->R - m->H) >> 1);
}

/* 换底。返回 0 = 没做 (条件不满足 / 已经在顶), 1 = 直接, 2 = 绕道保留缓冲 */
static inline int im_rebase(im_t *m, const im_io_t *io){
    if (!im_can_rebase(m)) return 0;
    uint32_t b = m->ys - m->org;
    if (b == 0) return 0;
    uint32_t top = m->hiw > b + m->H ? m->hiw : b + m->H;
    uint32_t n = top - b;                                /* [窗口顶, 写到过的最高行) */
    int how;
    if (n <= b){                                         /* 目标 [0,n) 和扫描中的 [b, b+H) 不重叠 */
        io->copy(io->ctx, IM_BUF_ROLL, 0, IM_BUF_ROLL, b, n);
        io->base(io->ctx, IM_BUF_ROLL, 0);
        io->wait(io->ctx);
        how = 1;
    } else {
        io->copy(io->ctx, IM_BUF_HOLD, 0, IM_BUF_ROLL, b, m->H);
        io->base(io->ctx, IM_BUF_HOLD, 0);
        io->wait(io->ctx);                               /* 现在扫的是保留缓冲, 卷轴整个不可见 */
        io->copy(io->ctx, IM_BUF_ROLL, 0, IM_BUF_ROLL, b, n);
        io->base(io->ctx, IM_BUF_ROLL, 0);
        io->wait(io->ctx);
        m->hops++;
        how = 2;
    }
    m->org = m->ys; m->hiw = n; m->rebases++; m->base_writes += (uint32_t)how;
    return how;
}

/* 屏上换成保留缓冲 (内容 = 当前窗口), 卷轴不再可见: 退出 / 重新开始前调 */
static inline void im_hold(im_t *m, const im_io_t *io, uint32_t now){
    im_settle(m, io, now);
    if (!m->son) return;
    io->copy(io->ctx, IM_BUF_HOLD, 0, IM_BUF_ROLL, m->ys - m->org, m->H);
    io->base(io->ctx, IM_BUF_HOLD, 0);
    io->wait(io->ctx);
    m->son = 0; m->anim = 0; m->show_req = 0;
}

/* 最新提交的窗口 (FBCRC 校验它): 1 = 卷轴第 *row 行起, 0 = 保留缓冲 */
static inline int im_latest(const im_t *m, uint32_t *row){
    if (m->pend){ *row = m->yp - m->org; return 1; }
    if (m->son){ *row = m->ys - m->org; return 1; }
    return 0;
}

#endif
