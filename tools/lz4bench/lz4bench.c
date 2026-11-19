/*
 * 固件 RECT_LZ4 解码 (fw/link/lz4.h, 和固件同一份代码) 在主机上的基准 / 对拍, 由 tools/lz4bench/bench.py 调。
 *   lz4bench bench FILE N   FILE = 一串记录 [u16 x,y,w,h | u32 raw | u32 n | n 字节 LZ4 块], 一整帧;
 *                           解 N 遍 (lk_rect_lz4_apply 进 800×1280 帧缓冲), 打印 "crc ns_per_frame ns_decode_only"
 *   lz4bench check FILE     每条记录一行: "ok CRC" (解出的 raw 字节的 CRC-32) 或 "err CODE" (LZ4_E_*)
 */
#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#pragma GCC diagnostic ignored "-Wunused-function"
#include "lz4.h"

#define W 800u
#define H 1280u
static uint16_t fb[W * H];
static uint8_t scratch[8 + W * H * 2] __attribute__((aligned(4)));

typedef struct { uint8_t *pl; uint32_t len, raw; } rec_t;

static rec_t *load(const char *path, uint32_t *nrec){
    FILE *f = fopen(path, "rb");
    if (!f){ perror(path); exit(1); }
    rec_t *r = 0; uint32_t n = 0, cap = 0;
    uint8_t hdr[16];
    while (fread(hdr, 1, 16, f) == 16){
        uint32_t len = lk_rd32(hdr + 12);
        if (n == cap){ cap = cap * 2 + 64; r = realloc(r, cap * sizeof *r); }
        r[n].pl = malloc(12 + len); r[n].len = 12 + len; r[n].raw = lk_rd32(hdr + 8);
        memcpy(r[n].pl, hdr, 12);
        if (fread(r[n].pl + 12, 1, len, f) != len){ fprintf(stderr, "truncated record\n"); exit(1); }
        n++;
    }
    fclose(f);
    *nrec = n;
    return r;
}

static double now_ns(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec * 1e9 + t.tv_nsec; }

int main(int argc, char **argv){
    if (argc < 3){ fprintf(stderr, "usage: lz4bench bench FILE N | check FILE\n"); return 2; }
    uint32_t n; rec_t *r = load(argv[2], &n);
    if (!strcmp(argv[1], "check")){
        for (uint32_t i = 0; i < n; i++){
            uint32_t raw = r[i].raw;
            uint8_t *d = malloc(raw ? raw : 1);
            int e = lz4_decode(r[i].pl + 12, r[i].len - 12, d, raw);
            if (e) printf("err %d\n", e); else printf("ok %u\n", lk_crc32(d, raw));
            free(d);
        }
        return 0;
    }
    int iters = argc > 3 ? atoi(argv[3]) : 20;
    double best = 1e30, best_dec = 1e30;
    for (int it = 0; it < iters; it++){
        uint32_t y0 = H, y1 = 0;
        double t0 = now_ns();
        for (uint32_t i = 0; i < n; i++)
            if (lk_rect_lz4_apply(fb, W, H, r[i].pl, r[i].len, scratch, sizeof scratch, &y0, &y1) != LK_OK){
                fprintf(stderr, "record %u rejected\n", i); return 1;
            }
        double t1 = now_ns();
        for (uint32_t i = 0; i < n; i++)                 /* 只解码 (不拷进帧缓冲) */
            if (lz4_decode(r[i].pl + 12, r[i].len - 12, scratch + 8, r[i].raw) != LZ4_OK) return 1;
        double t2 = now_ns();
        if (t1 - t0 < best) best = t1 - t0;
        if (t2 - t1 < best_dec) best_dec = t2 - t1;
    }
    printf("%u %.0f %.0f\n", lk_crc32((const uint8_t *)fb, sizeof fb), best, best_dec);
    return 0;
}
