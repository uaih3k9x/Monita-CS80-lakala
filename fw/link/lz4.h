/*
 * LZ4 块 (raw block, 不是 frame 格式) 解码 + RECT_LZ4 (0x23, fw v11) 落到帧缓冲。照公开的 LZ4 Block Format 规范自己写的, 不含第三方代码。
 * 主机单测 fw/link/test_lz4.c (ASan + UBSan, 模糊测试), 协议见 docs/protocol.md "RECT_LZ4"。
 *
 * 块 = 若干序列: token (高 4 位字面量长度, 低 4 位 match 长度 - 4; 15 = 后面跟扩展字节, 每个 255 继续加)
 *                + 字面量 + u16 小端 offset (1..已解出的字节数) + match 长度扩展。最后一个序列只有字面量 (没有 offset)。
 * 解码器只认"输入正好用完在一段字面量之后、输出正好 want 字节"为成功; 编码端的约定 (最后 5 字节是字面量、
 * 最后一个 match 离末尾 ≥ 12 字节) 不强制, 不影响安全。
 * 所有边界都在写之前检查 (减法形式, 不会回绕): 输入越界、输出越界、offset 为 0 或超出已解数据、长度扩展 (want ≤ LZ4_WANT_MAX 入口挡住)。
 *
 * 不用 memcpy (固件 -nostdlib -fno-builtin)。UI 画面解出来 99% 是 match (tools/lz4bench), 所以只给 match 做了按字拷:
 *   - offset ≥ 4 且两边地址模 4 同余 (offset = 4 的倍数, 比如上一行 1600): 先逐字节到对齐, 再按字 (读的 4 字节都已经写好);
 *   - offset 1 / 2 (单色像素, 最常见): 先逐字节写满 4 字节并对齐, 之后这 4 字节就是周期图案, 按字重复写;
 *   - 其余 (offset 3、地址不同余) 和字面量: 逐字节。
 * A5 上不对齐的字访问是否允许没确认过, 所以字访问一律先对齐。暂存区在 RAM 窗口里 (≤ 32 KiB)。耗时估算见 docs/protocol.md。
 */
#ifndef MONITA_LINK_LZ4_H
#define MONITA_LINK_LZ4_H
#include "proto.h"

#define LZ4_OK      0
#define LZ4_E_TRUNC 1    /* 输入截断 (token / 长度扩展 / 字面量 / offset 读到了末尾之外, 或以 match 结尾) */
#define LZ4_E_OVER  2    /* 输出越界 (字面量或 match 会写出 want 字节之外) */
#define LZ4_E_OFF   3    /* match offset 为 0 或超出已解出的字节 */
#define LZ4_E_SIZE  4    /* 输入用完了, 解出的字节数 != want */
#define LZ4_WANT_MAX 0xFFFFFF00u   /* want 上限: 长度扩展的加法才不会回绕 (lz4_ext); 实际 ≤ 32760 */

/* 按字拷用的 4 字节类型: may_alias = 允许和 uint8_t 缓冲互相别名 (不然 -O2/-Os 默认的严格别名规则下, 按字读刚按字节写的数据是 UB);
 * aligned(4) 保住对齐访问 (ldr/str, 调用方保证先对齐)。只影响别名分析, 固件里 lz4_decode 的生成代码和改之前逐条一致 */
typedef uint32_t __attribute__((may_alias, aligned(4))) lz4_u32a;

/* 长度扩展: 每个字节加上去, 255 继续。超过 lim 就停 (返回 LZ4_E_OVER)。
 * 不回绕的前提: lim ≤ LZ4_WANT_MAX (= 0xFFFFFF00)。进来时 *len = 15, 之后每次加之前 *len ≤ max(lim, 15) ≤ 0xFFFFFF00,
 * 加一个 ≤ 255 的字节到不了 2^32。lz4_decode 入口挡掉 want > LZ4_WANT_MAX (lim = want - op ≤ want) */
static int lz4_ext(const uint8_t *s, uint32_t n, uint32_t *ip, uint32_t *len, uint32_t lim){
    uint32_t b;
    do {
        if (*ip >= n) return LZ4_E_TRUNC;
        b = s[(*ip)++];
        *len += b;
        if (*len > lim) return LZ4_E_OVER;
    } while (b == 255);
    return LZ4_OK;
}

/* s[0..n) 解到 d[0..want)。成功返回 LZ4_OK; 失败返回 LZ4_E_*, d 里可能写了一部分 (调用方别用) */
static int lz4_decode(const uint8_t *s, uint32_t n, uint8_t *d, uint32_t want){
    uint32_t ip = 0, op = 0;
    if (want > LZ4_WANT_MAX) return LZ4_E_OVER;
    for (;;){
        if (ip >= n) return LZ4_E_TRUNC;
        uint32_t tok = s[ip++], len = tok >> 4;
        if (len == 15){ int r = lz4_ext(s, n, &ip, &len, want - op); if (r) return r; }
        if (len > want - op) return LZ4_E_OVER;
        if (len > n - ip) return LZ4_E_TRUNC;
        for (uint32_t i = 0; i < len; i++) d[op + i] = s[ip + i];
        op += len; ip += len;
        if (ip == n) return op == want ? LZ4_OK : LZ4_E_SIZE;   /* 最后一个序列: 只有字面量 */
        if (n - ip < 2) return LZ4_E_TRUNC;
        uint32_t off = (uint32_t)s[ip] | ((uint32_t)s[ip + 1] << 8);
        ip += 2;
        if (off == 0 || off > op) return LZ4_E_OFF;
        len = tok & 15u;
        if (len == 15){ int r = lz4_ext(s, n, &ip, &len, want - op); if (r) return r; }
        len += 4;
        if (len > want - op) return LZ4_E_OVER;
        uint8_t *q = d + op; const uint8_t *m = q - off;
        uint32_t i = 0;
        if (off >= 4 && (((uintptr_t)q ^ (uintptr_t)m) & 3u) == 0){
            for (; i < len && ((uintptr_t)(q + i) & 3u); i++) q[i] = m[i];
            for (; i + 4 <= len; i += 4) *(lz4_u32a *)(q + i) = *(const lz4_u32a *)(m + i);
        } else if (off <= 2 && len >= 8){
            for (; i < 4 || ((uintptr_t)(q + i) & 3u); i++) q[i] = m[i];     /* 至多 7 字节 < len */
            uint32_t w = *(const lz4_u32a *)(q + i - 4);                    /* 周期 1 / 2 整除 4: 这 4 字节一直重复 */
            for (; i + 4 <= len; i += 4) *(lz4_u32a *)(q + i) = w;
        }
        for (; i < len; i++) q[i] = m[i];                       /* 向前逐字节: offset < len 时重复前面的图案 */
        op += len;
    }
}

/* ---- RECT_LZ4 (0x23): u16 x, y, w, h + u32 raw_len (= w*h*2) + LZ4 块 ----
 * 先整块解进暂存区 scratch (cap 字节, 至少 8 + raw_len; 4 字节对齐), 前面补上 RECT 的 8 字节头, 再交给 lk_rect_apply:
 * 越界 / 长度 / 对齐都走 RECT 原来的路径, 块坏了 (任何一种 LZ4_E_*) 帧缓冲一个像素都不动。
 * 返回 LK_OK / LK_EPARAM (矩形越界、raw_len 不对、放不进暂存区、块坏了)。 */
__attribute__((unused)) static uint32_t lk_rect_lz4_apply(uint16_t *fb, uint32_t w_fb, uint32_t h_fb, const uint8_t *pl, uint32_t len,
                                                          uint8_t *scratch, uint32_t cap, uint32_t *y0, uint32_t *y1){
    if (len < 12 || cap < 8) return LK_EPARAM;
    uint32_t x = lk_rd16(pl), y = lk_rd16(pl + 2), w = lk_rd16(pl + 4), h = lk_rd16(pl + 6), raw = lk_rd32(pl + 8);
    if (w == 0 || h == 0 || x + w > w_fb || y + h > h_fb) return LK_EPARAM;   /* 先挡住, w*h*2 才不会溢出 */
    if (raw != w * h * 2u || raw > cap - 8u) return LK_EPARAM;
    if (lz4_decode(pl + 12, len - 12, scratch + 8, raw) != LZ4_OK) return LK_EPARAM;
    for (int i = 0; i < 8; i++) scratch[i] = pl[i];
    return lk_rect_apply(fb, w_fb, h_fb, scratch, 8 + raw, y0, y1);
}

#endif
