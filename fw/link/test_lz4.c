/* fw/link/lz4.h 的主机单测 (sh fw/link/test.sh 用 ASan + UBSan 编)。
 *   1. 手写的块: 纯字面量、重叠 match (offset 1/2/3)、长度扩展的边界 (14/15/16/18/19/270/271/…)。
 *   2. 往返: 测试里自己按 LZ4 块格式 (公开规范) 编随机序列, 解出来要逐字节一致。
 *   3. 恶意块: 偏移 0、偏移超出已解数据、各处截断、输出越界、长度扩展溢出、尾巴多字节、解出长度不对。
 *   4. 模糊测试: 合法块随机改字节 / 截断 / 插 0xFF 串 / 改偏移, 每次输入输出都按实际长度 malloc (越界一个字节 ASan 就报);
 *      和测试里逐字节的参考解码器 (照规范直写, 不做按字拷) 比: 成功 / 失败要一致, 成功时内容逐字节一致;
 *      输出起点轮流放在 malloc 地址 +0 / +1 / +2 / +3 (按字拷的各条对齐路径都走到)。
 *   5. RECT_LZ4 落到帧缓冲: 头校验、坏块不动帧缓冲、和同样像素的 RECT 结果一致。
 *   ./test_lz4 [模糊次数]   (默认 300000) */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#pragma GCC diagnostic ignored "-Wunused-function"   /* proto.h 里没用到的 */
#include "lz4.h"

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)

static uint32_t rng = 12345u;
static uint32_t rnd(void){ rng ^= rng << 13; rng ^= rng >> 17; rng ^= rng << 5; return rng; }
static uint32_t rndn(uint32_t n){ return n ? rnd() % n : 0; }

/* ---- 测试侧编码器: 按规范把 (字面量, offset, match 长度) 序列编成块 ---- */
typedef struct { uint8_t *b; uint32_t n, cap; } buf_t;
static void put(buf_t *o, uint8_t v){ if (o->n == o->cap){ o->cap = o->cap * 2 + 64; o->b = realloc(o->b, o->cap); } o->b[o->n++] = v; }
static void put_len(buf_t *o, uint32_t v){ while (v >= 255){ put(o, 255); v -= 255; } put(o, (uint8_t)v); }
/* 一个序列: lit 个字面量, 然后 (mlen >= 4 时) 一个 match; mlen = 0 表示最后一个序列 (只有字面量) */
static void emit(buf_t *blk, buf_t *out, const uint8_t *lit, uint32_t nlit, uint32_t off, uint32_t mlen){
    uint32_t ml = mlen ? mlen - 4 : 0;
    put(blk, (uint8_t)(((nlit < 15 ? nlit : 15) << 4) | (ml < 15 ? ml : 15)));
    if (nlit >= 15) put_len(blk, nlit - 15);
    for (uint32_t i = 0; i < nlit; i++){ put(blk, lit[i]); put(out, lit[i]); }
    if (!mlen) return;
    put(blk, (uint8_t)off); put(blk, (uint8_t)(off >> 8));
    if (ml >= 15) put_len(blk, ml - 15);
    for (uint32_t i = 0; i < mlen; i++) put(out, out->b[out->n - off]);   /* 逐字节向前: 重叠 match 的语义 */
}

/* 随机合法块, 正好解出 target 字节; 像 RGB565 UI 画面: 字面量多是 2 字节周期的"颜色", match 偏移偏向 1/2/3/1600 */
static void gen_block(buf_t *blk, buf_t *out, uint32_t target){
    blk->n = out->n = 0;
    uint8_t lit[600];
    for (;;){
        uint32_t left = target - out->n;
        uint32_t nl = rndn(4) == 0 ? rndn(300) : rndn(20);
        if (out->n == 0 && nl == 0) nl = 1;
        if (nl > left) nl = left;
        for (uint32_t i = 0; i < nl; i++) lit[i] = (uint8_t)(rndn(3) ? (i & 1 ? 0x84 : 0x21) : rnd());
        if (left - nl < 4){                            /* 剩下的放不下一个 match: 全当最后的字面量 */
            uint8_t *rest = malloc(left ? left : 1);
            for (uint32_t i = 0; i < left; i++) rest[i] = i < nl ? lit[i] : (uint8_t)rnd();
            emit(blk, out, rest, left, 0, 0);
            free(rest);
            return;
        }
        uint32_t have = out->n + nl, off;
        switch (rndn(6)){
        case 0: off = 1; break; case 1: off = 2; break; case 2: off = 3; break; case 3: off = 1600; break;
        default: off = 1 + rndn(have < 65535 ? have : 65535);
        }
        if (off > have) off = have;
        uint32_t ml = 4 + (rndn(5) == 0 ? rndn(2000) : rndn(30));
        if (ml > left - nl) ml = left - nl;
        emit(blk, out, lit, nl, off, ml);
    }
}

/* 按实际长度 malloc 再解 (ASan 抓 1 字节越界); 输出起点 = malloc 地址 + al (0..3, 末尾仍紧贴分配的末尾) */
static int dec_al(const uint8_t *src, uint32_t n, uint32_t want, uint32_t al, uint8_t **outp){
    uint8_t *s = malloc(n ? n : 1), *raw = malloc(al + want ? al + want : 1);
    memcpy(s, src, n);
    int r = lz4_decode(s, n, raw + al, want);
    free(s);
    if (outp){ uint8_t *o = malloc(want ? want : 1); memcpy(o, raw + al, want); *outp = o; }
    free(raw);
    return r;
}
static int dec(const uint8_t *src, uint32_t n, uint32_t want, uint8_t **outp){ return dec_al(src, n, want, 0, outp); }

/* 参考解码器: 照规范逐字节直写, 接受规则和 lz4_decode 一样 (offset 0 / 超出已解 / 输出越界 / 以 match 结尾都算坏,
 * 输入正好用完在字面量之后且输出正好 want 才算成功)。size_t 记长度 (主机上不会回绕), 不做任何按字拷。0 = 成功 */
static int ref_decode(const uint8_t *s, size_t n, uint8_t *d, size_t want){
    size_t ip = 0, op = 0;
    for (;;){
        if (ip >= n) return -1;
        unsigned tok = s[ip++]; size_t len = tok >> 4;
        if (len == 15){ unsigned b; do { if (ip >= n) return -1; b = s[ip++]; len += b; } while (b == 255); }
        if (len > want - op || len > n - ip) return -1;
        for (size_t i = 0; i < len; i++) d[op + i] = s[ip + i];
        op += len; ip += len;
        if (ip == n) return op == want ? 0 : -1;
        if (n - ip < 2) return -1;
        size_t off = (size_t)s[ip] | ((size_t)s[ip + 1] << 8); ip += 2;
        if (off == 0 || off > op) return -1;
        len = tok & 15u;
        if (len == 15){ unsigned b; do { if (ip >= n) return -1; b = s[ip++]; len += b; } while (b == 255); }
        len += 4;
        if (len > want - op) return -1;
        for (size_t i = 0; i < len; i++, op++) d[op] = d[op - off];
    }
}

static void test_vectors(void){
    uint8_t *o;
    /* 纯字面量 "abc" */
    const uint8_t a[] = { 0x30, 'a', 'b', 'c' };
    CHECK(dec(a, 4, 3, &o) == LZ4_OK && !memcmp(o, "abc", 3)); free(o);
    CHECK(dec(a, 4, 4, 0) == LZ4_E_SIZE);              /* 解出 3 != 4 */
    CHECK(dec(a, 4, 2, 0) == LZ4_E_OVER);              /* 输出放不下 */
    CHECK(dec(a, 3, 3, 0) == LZ4_E_TRUNC);             /* 字面量截断 */
    CHECK(dec(a, 0, 0, 0) == LZ4_E_TRUNC);             /* 空输入不算合法块 */
    /* 空输出: 一个 0 字面量的结束序列 */
    const uint8_t z[] = { 0x00 };
    CHECK(dec(z, 1, 0, 0) == LZ4_OK);
    /* offset 1 重叠: "x" + match(1, 9) + "y" = "xxxxxxxxxxy" */
    const uint8_t r1[] = { 0x15, 'x', 1, 0, 0x10, 'y' };
    CHECK(dec(r1, 6, 11, &o) == LZ4_OK && !memcmp(o, "xxxxxxxxxxy", 11)); free(o);
    /* offset 2 / 3 重叠 */
    const uint8_t r2[] = { 0x22, 'a', 'b', 2, 0, 0x00 };
    CHECK(dec(r2, 6, 8, &o) == LZ4_OK && !memcmp(o, "abababab", 8)); free(o);
    const uint8_t r3[] = { 0x33, 'a', 'b', 'c', 3, 0, 0x00 };
    CHECK(dec(r3, 7, 10, &o) == LZ4_OK && !memcmp(o, "abcabcabca", 10)); free(o);
    /* 偏移 0 / 超出已解数据 (= 已解 + 1) */
    const uint8_t b0[] = { 0x10, 'a', 0, 0, 0x00 };
    CHECK(dec(b0, 5, 5, 0) == LZ4_E_OFF);
    const uint8_t b2[] = { 0x10, 'a', 2, 0, 0x00 };
    CHECK(dec(b2, 5, 5, 0) == LZ4_E_OFF);
    const uint8_t bm[] = { 0x00, 1, 0, 0x00 };          /* 一个字面量都没有就 match */
    CHECK(dec(bm, 4, 4, 0) == LZ4_E_OFF);
    /* 截断在 offset 中间 / match 长度扩展中间 / 结束序列缺失 */
    const uint8_t t1[] = { 0x10, 'a', 1 };
    CHECK(dec(t1, 3, 5, 0) == LZ4_E_TRUNC);
    const uint8_t t2[] = { 0x1F, 'a', 1, 0 };
    CHECK(dec(t2, 4, 40, 0) == LZ4_E_TRUNC);
    const uint8_t t3[] = { 0x1F, 'a', 1, 0, 255 };
    CHECK(dec(t3, 5, 400, 0) == LZ4_E_TRUNC);
    const uint8_t t4[] = { 0x10, 'a', 1, 0 };          /* 以 match 结尾 (没有最后的字面量序列) */
    CHECK(dec(t4, 4, 5, 0) == LZ4_E_TRUNC);
    const uint8_t t5[] = { 0xF0 };                     /* 字面量长度扩展缺失 */
    CHECK(dec(t5, 1, 15, 0) == LZ4_E_TRUNC);
    /* match 写出界 / 尾巴多一个字节 */
    CHECK(dec(r1, 6, 10, 0) == LZ4_E_OVER);
    const uint8_t tail[] = { 0x30, 'a', 'b', 'c', 0x00 };
    CHECK(dec(tail, 5, 3, 0) != LZ4_OK);
    /* 长度扩展: 一长串 255 不能把长度算回绕 (输出上限先挡住) */
    uint8_t big[1 + 20000];
    big[0] = 0xF0; memset(big + 1, 255, sizeof big - 1);
    CHECK(dec(big, sizeof big, 32000, 0) == LZ4_E_OVER);
    big[0] = 0x1F; big[1] = 'a'; big[2] = 1; big[3] = 0;
    CHECK(dec(big, sizeof big, 32000, 0) == LZ4_E_OVER);
    /* 长度扩展"不回绕"的前提: 输出上限 ≤ 0xFFFFFF00 (len ≤ lim 时再加一个字节 ≤ 255 不会过 2^32)。更大的 want 入口就拒 */
    CHECK(lz4_decode(z, 1, 0, 0xFFFFFF01u) == LZ4_E_OVER);
    CHECK(lz4_decode(z, 1, 0, 0xFFFFFFFFu) == LZ4_E_OVER);
    CHECK(lz4_decode(z, 1, 0, 0xFFFFFF00u) == LZ4_E_SIZE);     /* 上限本身照常解 (这里解出 0 字节 != want) */
}

/* 长度扩展边界: 字面量 / match 长度各取 0..20、250..280、510..530, 两两组合往返 */
static void test_len_edges(void){
    static const uint32_t L[] = { 0, 1, 4, 13, 14, 15, 16, 17, 18, 19, 20, 268, 269, 270, 271, 272, 273, 274, 524, 525, 526, 1000 };
    uint32_t nl = sizeof L / sizeof L[0], cases = 0;
    buf_t blk = {0}, out = {0};
    uint8_t lit[1100];
    for (uint32_t i = 0; i < sizeof lit; i++) lit[i] = (uint8_t)(i * 7 + 1);
    for (uint32_t i = 0; i < nl; i++) for (uint32_t j = 0; j < nl; j++){
        uint32_t ml = L[j] + 4;
        blk.n = out.n = 0;
        uint32_t nl0 = L[i] + 1, off = 1 + L[j] % 3;          /* 至少 1 个字面量, 偏移 1..3 (重叠) */
        emit(&blk, &out, lit, nl0, off > nl0 ? nl0 : off, ml);
        emit(&blk, &out, lit, L[j], 0, 0);
        uint8_t *o;
        int r = dec(blk.b, blk.n, out.n, &o);
        CHECK(r == LZ4_OK && !memcmp(o, out.b, out.n));
        free(o);
        CHECK(dec(blk.b, blk.n - 1, out.n, 0) != LZ4_OK);       /* 少最后一个字节 */
        CHECK(dec(blk.b, blk.n, out.n + 1, 0) == LZ4_E_SIZE);
        CHECK(dec(blk.b, blk.n, out.n - 1, 0) == LZ4_E_OVER);
        cases++;
    }
    free(blk.b); free(out.b);
    printf("  len edges: %u cases\n", cases);
}

/* 按字拷的对齐路径: offset 1..9 / 12 / 16 / 17 / 31..33 / 1599..1601, match 长度 4..80, 前面 0..7 个多余字面量, 输出起点 +0..3 */
static void test_align_matrix(void){
    static const uint32_t offs[] = { 1, 2, 3, 4, 5, 6, 7, 8, 9, 12, 16, 17, 31, 32, 33, 1599, 1600, 1601 };
    buf_t blk = {0}, out = {0};
    static uint8_t lit[1700], ref[1700 + 80 + 3];
    for (uint32_t i = 0; i < sizeof lit; i++) lit[i] = (uint8_t)(i * 37 + 11);
    uint32_t cases = 0; int f0 = fails;
    for (uint32_t oi = 0; oi < sizeof offs / sizeof offs[0]; oi++)
    for (uint32_t pre = 0; pre < 8; pre++)
    for (uint32_t ml = 4; ml <= 80; ml++)
    for (uint32_t al = 0; al < 4; al++){
        blk.n = out.n = 0;
        emit(&blk, &out, lit, offs[oi] + pre, offs[oi], ml);
        emit(&blk, &out, lit, 3, 0, 0);
        CHECK(ref_decode(blk.b, blk.n, ref, out.n) == 0 && !memcmp(ref, out.b, out.n));
        uint8_t *o;
        CHECK(dec_al(blk.b, blk.n, out.n, al, &o) == LZ4_OK && !memcmp(o, out.b, out.n));
        free(o);
        cases++;
        if (fails != f0) goto done;
    }
done:
    free(blk.b); free(out.b);
    printf("  align matrix: %u cases\n", cases);
}

static void test_roundtrip(uint32_t n){
    buf_t blk = {0}, out = {0};
    for (uint32_t k = 0; k < n; k++){
        gen_block(&blk, &out, rndn(k % 10 == 0 ? 32000 : 3000));
        uint8_t *o;
        int r = dec_al(blk.b, blk.n, out.n, k & 3u, &o);
        CHECK(r == LZ4_OK && !memcmp(o, out.b, out.n));
        free(o);
        if (fails) break;
    }
    free(blk.b); free(out.b);
    printf("  roundtrip: %u random blocks\n", n);
}

/* 模糊: 改合法块。不崩、不越界 (ASan); 成功 / 失败和参考解码器一致, 成功时内容一致; 输出起点轮流错开 0..3 字节 */
static void test_fuzz(uint32_t n){
    buf_t blk = {0}, out = {0};
    uint32_t ok = 0, err[5] = {0};
    uint8_t *m = 0, *ref = 0; uint32_t mcap = 0, rcap = 0;
    for (uint32_t k = 0; k < n; k++){
        if (k % 64 == 0) gen_block(&blk, &out, 1 + rndn(k % 640 == 0 ? 20000 : 1500));
        uint32_t len = blk.n;
        if (mcap < len + 64){ mcap = len + 64; m = realloc(m, mcap); }
        memcpy(m, blk.b, len);
        uint32_t want = out.n;
        switch (rndn(8)){
        case 0: len = rndn(len + 1); break;                                     /* 截断 */
        case 1: for (uint32_t i = 1 + rndn(4); i--;) m[rndn(len)] = (uint8_t)rnd(); break;   /* 改几个字节 */
        case 2: { uint32_t p = rndn(len), q = 1 + rndn(40);                       /* 插一串 0xFF (长度扩展) */
                  if (p + q > len) q = len - p; memset(m + p, 255, q); break; }
        case 3: { uint32_t p = rndn(len); m[p] ^= (uint8_t)(1u << rndn(8)); break; }   /* 翻一位 */
        case 4: want = rndn(want * 2 + 2); break;                                 /* 期望长度不对 */
        case 5: { uint32_t p = rndn(len); m[p] = 0; if (p + 1 < len) m[p + 1] = 0; break; }   /* 偏移清零 (若落在偏移上) */
        case 6: { uint32_t e = rndn(16); for (uint32_t i = 0; i < e; i++) m[len + i] = (uint8_t)rnd(); len += e; break; }   /* 尾巴多字节 */
        default: m[rndn(len)] = 0xFF; break;
        }
        uint8_t *o;
        int r = dec_al(m, len, want, k & 3u, &o);
        if (rcap < want + 1){ rcap = want + 1; ref = realloc(ref, rcap); }
        int rr = ref_decode(m, len, ref, want);
        CHECK((r == LZ4_OK) == (rr == 0));                 /* 和逐字节参考解码器判得一样 */
        if (r == LZ4_OK && rr == 0) CHECK(!memcmp(o, ref, want));   /* 成功时内容一致 */
        free(o);
        if (r == LZ4_OK) ok++; else if (r > 0 && r < 5) err[r]++; else { CHECK(0 && "未知返回值"); break; }
        if (fails > 20) break;
    }
    free(m); free(ref); free(blk.b); free(out.b);
    printf("  fuzz: %u mutated blocks: ok %u, trunc %u, over %u, off %u, size %u\n", n, ok, err[1], err[2], err[3], err[4]);
    CHECK(err[1] && err[2] && err[3] && err[4]);       /* 每类错误都被触发过 */
}

/* 纯随机字节 (不从合法块变来) 也跑一遍 */
static void test_random_bytes(uint32_t n){
    uint8_t buf[256];
    uint32_t ok = 0;
    for (uint32_t k = 0; k < n; k++){
        uint32_t len = rndn(sizeof buf);
        for (uint32_t i = 0; i < len; i++) buf[i] = (uint8_t)rnd();
        if (dec(buf, len, rndn(4096), 0) == LZ4_OK) ok++;
    }
    printf("  random bytes: %u blocks, %u happened to decode\n", n, ok);
}

/* ---- RECT_LZ4 落到帧缓冲 ---- */
#define FW 64u
#define FH 48u
static uint16_t fb[FW * FH], fb2[FW * FH];
static uint8_t scratch[8 + 4096] __attribute__((aligned(4)));

static uint32_t rect_lz4(uint16_t *f, uint32_t x, uint32_t y, uint32_t w, uint32_t h, uint32_t raw, const uint8_t *blk, uint32_t n){
    uint8_t *pl = malloc(12 + n);
    lk_wr16(pl, x); lk_wr16(pl + 2, y); lk_wr16(pl + 4, w); lk_wr16(pl + 6, h); lk_wr32(pl + 8, raw);
    memcpy(pl + 12, blk, n);
    uint32_t y0 = FH, y1 = 0;
    uint32_t r = lk_rect_lz4_apply(f, FW, FH, pl, 12 + n, scratch, sizeof scratch, &y0, &y1);
    if (r == LK_OK) CHECK(y0 == y && y1 == y + h);
    else CHECK(y0 == FH && y1 == 0);
    free(pl);
    return r;
}

static void test_rect(void){
    buf_t blk = {0}, out = {0};
    for (uint32_t k = 0; k < 2000; k++){
        uint32_t w = 1 + rndn(FW), h = 1 + rndn(FH), x = rndn(FW - w + 1), y = rndn(FH - h + 1);
        if (k % 4 == 0){ x = 0; w = FW; }
        uint32_t raw = w * h * 2;
        if (8 + raw > sizeof scratch) continue;
        gen_block(&blk, &out, raw);
        for (uint32_t i = 0; i < FW * FH; i++) fb[i] = fb2[i] = (uint16_t)rnd();
        /* 对照: 同样的像素用 RECT */
        uint8_t *rp = malloc(8 + raw);
        lk_wr16(rp, x); lk_wr16(rp + 2, y); lk_wr16(rp + 4, w); lk_wr16(rp + 6, h); memcpy(rp + 8, out.b, raw);
        uint32_t a0 = FH, a1 = 0;
        CHECK(lk_rect_apply(fb2, FW, FH, rp, 8 + raw, &a0, &a1) == LK_OK);
        free(rp);
        CHECK(rect_lz4(fb, x, y, w, h, raw, blk.b, blk.n) == LK_OK);
        CHECK(!memcmp(fb, fb2, sizeof fb));
        /* 坏块: 帧缓冲一个像素都不动 */
        memcpy(fb2, fb, sizeof fb);
        CHECK(rect_lz4(fb, x, y, w, h, raw, blk.b, blk.n - 1) == LK_EPARAM);
        CHECK(rect_lz4(fb, x, y, w, h, raw + 2, blk.b, blk.n) == LK_EPARAM);   /* raw_len != w*h*2 */
        CHECK(!memcmp(fb, fb2, sizeof fb));
        if (fails) break;
    }
    /* 头: 越界 / 0 宽高 / 太短 / raw 放不进暂存区 */
    const uint8_t one[] = { 0x20, 1, 2 };              /* 2 字节字面量 = 1 像素 */
    CHECK(rect_lz4(fb, 0, 0, 1, 1, 2, one, 3) == LK_OK && fb[0] == 0x0201);
    CHECK(rect_lz4(fb, FW - 1, FH - 1, 1, 1, 2, one, 3) == LK_OK && fb[FW * FH - 1] == 0x0201);
    CHECK(rect_lz4(fb, FW, 0, 1, 1, 2, one, 3) == LK_EPARAM);
    CHECK(rect_lz4(fb, 0, FH, 1, 1, 2, one, 3) == LK_EPARAM);
    CHECK(rect_lz4(fb, 0, 0, 0, 1, 0, one, 3) == LK_EPARAM);
    CHECK(rect_lz4(fb, 0, 0, 1, 0, 0, one, 3) == LK_EPARAM);
    CHECK(rect_lz4(fb, 0, 0, 0xFFFF, 0xFFFF, 2, one, 3) == LK_EPARAM);   /* w*h*2 溢出 32 位之前就被屏幕边界挡住 */
    uint32_t y0 = FH, y1 = 0;
    CHECK(lk_rect_lz4_apply(fb, FW, FH, one, 3, scratch, sizeof scratch, &y0, &y1) == LK_EPARAM);   /* 头都不够 */
    {   /* raw 刚好 = 暂存区 - 8 可以, 再大一点被拒 (FW = 64: 32 行 = 4096 B) */
        buf_t b2 = {0}, o2 = {0};
        uint8_t lit[4] = { 1, 2, 3, 4 };
        emit(&b2, &o2, lit, 4, 4, 4096 - 4 - 4);
        emit(&b2, &o2, lit, 4, 0, 0);
        CHECK(o2.n == 4096);
        CHECK(rect_lz4(fb, 0, 0, FW, 32, 4096, b2.b, b2.n) == LK_OK);
        CHECK(lk_rect_lz4_apply(fb, FW, FH, (const uint8_t[]){0}, 0, scratch, sizeof scratch, &y0, &y1) == LK_EPARAM);
        /* 暂存区小一个字节 */
        uint8_t *pl = malloc(12 + b2.n);
        lk_wr16(pl, 0); lk_wr16(pl + 2, 0); lk_wr16(pl + 4, FW); lk_wr16(pl + 6, 32); lk_wr32(pl + 8, 4096);
        memcpy(pl + 12, b2.b, b2.n);
        y0 = FH; y1 = 0;
        CHECK(lk_rect_lz4_apply(fb, FW, FH, pl, 12 + b2.n, scratch, sizeof scratch - 1, &y0, &y1) == LK_EPARAM);
        free(pl); free(b2.b); free(o2.b);
    }
    free(blk.b); free(out.b);
}

int main(int argc, char **argv){
    uint32_t n = argc > 1 ? (uint32_t)strtoul(argv[1], 0, 10) : 300000u;
    test_vectors();
    test_len_edges();
    test_align_matrix();
    test_roundtrip(20000);
    test_fuzz(n);
    test_random_bytes(n / 3);
    test_rect();
    printf("lz4: %s\n", fails ? "FAILED" : "all OK");
    return fails ? 1 : 0;
}
