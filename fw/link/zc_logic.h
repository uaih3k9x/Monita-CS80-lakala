/*
 * 零拷贝滚动实验的纯逻辑 (无寄存器访问, 主机可测: sh test.sh)。
 * 固件侧胶水见 zc_diag.h。实机结论见 docs/r05-notes.md §4。
 *
 * 约定: 地址都是 u32 物理地址; LCDC 偏移相对 0x08807000。
 * 不用 '/' '%' (设备上没有硬件除法也不链 libgcc)。
 */
#ifndef ZC_LOGIC_H
#define ZC_LOGIC_H
#include <stdint.h>

/* ---- 面板时序 (R05 原厂面板描述符 +0x40.., 证实; 见 docs/r05-notes.md §4) ---- */
#define ZC_W        800u
#define ZC_H        1280u
#define ZC_STRIDE   0x640u          /* 1600 B */
#define ZC_VSYNC    3u
#define ZC_VBP      11u
#define ZC_VFP      16u
#define ZC_VTOTAL   (ZC_H + ZC_VSYNC + ZC_VBP + ZC_VFP)   /* 1310 */

/* ---- 寄存器白名单 ----
 * 只允许写这几个偏移; 读只允许 0x00..0x3c 和 0x50 (内核自己 RMW 读过 0x50)。
 * 0x3c/0x40/0x44/0x48 是"第二层/第二平面"候选, 要 armed 才放行。 */
#define ZC_R_BASE0  0x34u
#define ZC_R_PITCH0 0x38u
#define ZC_R_BASE1  0x3cu
#define ZC_R_PITCH1 0x40u
#define ZC_R_X44    0x44u
#define ZC_R_X48    0x48u

/* [lo, hi) 允许被扫描的内存。固件传 PSRAM {0x80000000, 0x81000000}: 读 PSRAM 无副作用,
 * 分屏 ABS 语义下写进去的基址可以在滚动缓冲之前, 只要整帧跨度仍在 PSRAM 内。 */
typedef struct { uint32_t lo, hi; } zc_range_t;

static inline int zc_in_ranges(uint32_t a, uint32_t n, const zc_range_t *r, uint32_t nr){
    for (uint32_t i = 0; i < nr; i++)
        if (a >= r[i].lo && a <= r[i].hi && n <= r[i].hi - a) return 1;
    return 0;
}

/* 一个基址要让控制器读满 rows 行 (行跨 pitch) 都落在允许区里 */
static inline int zc_base_ok(uint32_t base, uint32_t rows, uint32_t pitch, const zc_range_t *r, uint32_t nr){
    if (base & 3u) return 0;
    return zc_in_ranges(base, rows * pitch, r, nr);
}

/* 0x38 / 0x40 的值: pitch<<16 | en。pitch 13 位 (内核掩码 0x1fff0000), 偶数字节 */
static inline uint32_t zc_pitch_val(uint32_t pitch, uint32_t en){ return ((pitch & 0x1fffu) << 16) | (en & 1u); }

/* 写寄存器前检查。返回 1 放行。armed=0 时只放行 0x34/0x38 (固件早就在写的两个)。 */
static inline int zc_reg_write_ok(uint32_t off, uint32_t val, int armed, const zc_range_t *r, uint32_t nr){
    switch (off){
    case ZC_R_BASE0:  return zc_base_ok(val, ZC_H, ZC_STRIDE, r, nr);  /* 整帧 1280 行都要落在允许区 */
    case ZC_R_PITCH0: return val == zc_pitch_val(ZC_STRIDE, 1u);           /* 不许改行跨/关层 */
    case ZC_R_BASE1:  return armed && (val == 0u || zc_in_ranges(val, 4u, r, nr));
    case ZC_R_PITCH1: {
        uint32_t p = (val >> 16) & 0x1fffu;
        return armed && (val & ~0x1fff0001u) == 0u && (p & 1u) == 0u;
    }
    case ZC_R_X44: case ZC_R_X48:                                       /* 候选窗口 (y<<16|x), 11 位 */
        return armed && (val & ~0x07ff07ffu) == 0u;
    default: return 0;
    }
}

static inline int zc_reg_read_ok(uint32_t off){ return (off < 0x40u && (off & 3u) == 0) || off == 0x50u; }

/* ---- 光栅分屏 (方案 S) ----
 * 屏幕行 [y0, y1) 显示滚动缓冲的第 top 行起。两种可能的硬件语义 (实验 E1 判定):
 *   ZC_SEM_ABS : 每行地址 = base + line*pitch (base 随写随用)  -> 写 scroll + (top - y0)*pitch
 *   ZC_SEM_REL : 写 base 时行指针从 base 重新开始               -> 写 scroll + top*pitch
 * (第三种"帧头锁存"下分屏不可能, 只剩整屏偏移。) */
enum { ZC_SEM_ABS = 0, ZC_SEM_REL = 1 };

static inline uint32_t zc_split_base(uint32_t scroll, uint32_t top, uint32_t y0, uint32_t pitch, int sem){
    uint32_t a = scroll + top * pitch;
    return sem == ZC_SEM_ABS ? a - y0 * pitch : a;
}

/* 恢复写 (行 y1 处切回主 fb) 的值 */
static inline uint32_t zc_restore_base(uint32_t front, uint32_t y1, uint32_t pitch, int sem){
    return sem == ZC_SEM_ABS ? front : front + y1 * pitch;
}

/* 帧事件 0x04 (推测 = vsync 起点) 之后多少 us 写, 才能赶在控制器取第 line 行之前。
 * line_q16 = 每行时长 us 的 Q16 (标定: period_us<<16 / 1310, 固件用 udiv 算一次)。
 * lead 是提前量 (行; 控制器预取深度 + 写寄存器延迟, 实验 E1 标定)。 */
static inline uint32_t zc_line_deadline_us(uint32_t line, uint32_t lead, uint32_t line_q16){
    uint32_t l = ZC_VSYNC + ZC_VBP + line;
    l = l > lead ? l - lead : 0u;
    return (uint32_t)(((uint64_t)l * line_q16) >> 16);
}

/* ---- 尺子图案: 每行左侧 11 个 32px 方块 = 行号二进制 (高位在左, 白 1 黑 0),
 * 右侧按 (row>>4) 交替两种底色, 每 100 行一条整行红线。拍照即可读出分屏发生在哪一行、显示的是缓冲第几行。 */
static inline uint16_t zc_ruler_px(uint32_t row, uint32_t col, uint16_t tint){
    uint32_t r100 = row;                       /* row 是 100 的倍数? 不用 %: 逐次减 */
    while (r100 >= 100u) r100 -= 100u;
    if (r100 == 0u) return 0xF800u;
    if (col < 11u * 32u){
        uint32_t bit = 10u - (col >> 5);
        if ((col & 31u) >= 28u) return 0x0000u;               /* 方块间隔 */
        return ((row >> bit) & 1u) ? 0xFFFFu : 0x0000u;
    }
    return ((row >> 4) & 1u) ? tint : (uint16_t)(tint >> 1 & 0x7befu);
}

/* ---- 滚动缓冲环 (方案 S/L 共用) ----
 * 缓冲 rows_buf 行, 窗口 win 行。top 往下走到 rows_buf - win 后需要一次"回卷": 把窗口当前内容抄到缓冲顶部
 * (抄的是不可见区, 不撕裂), 然后 top 归零。返回需要回卷时的新 top (=0) 并给出拷贝区间; 否则返回 top 不变。 */
typedef struct { uint32_t src_row, dst_row, rows; } zc_rebase_t;

static inline int zc_need_rebase(uint32_t top, uint32_t ahead, uint32_t win, uint32_t rows_buf){
    return top + win + ahead > rows_buf;        /* ahead = 主机想在窗口下方预先放好的行数 */
}

static inline zc_rebase_t zc_rebase_plan(uint32_t top, uint32_t win){
    zc_rebase_t p = { top, 0u, win };
    return p;
}

#endif
