/* 主机单测: fw/lcd/bootscreen.h 画的开机版权页 == tools/bootscreen/render_ref.py (设计稿) 逐像素 (RGB565 截断后)。
 * 另查: Link 行重画只动 [y0,y1) 那几行; 画在前台 + tb_reset_to 之后, 其余缓冲首次使用前整屏同步。
 * 由 test.sh 调: ./a.out DIR, DIR 里放好 render_ref.py 的输出 (见 test.sh), 本程序也把自己画的写成 DIR/c_*.ppm。 */
#pragma GCC diagnostic ignored "-Wunused-function"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "bootscreen.h"
#include "tribuf.h"

static int fails;
#define CHECK(c) do{ if(!(c)){ printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } }while(0)
#define NPX (BS_W * BS_H)
static const char *dir;

static void path(char *p, const char *name){ snprintf(p, 512, "%s/%s", dir, name); }

static void save(const uint16_t *fb, const char *name){
    char p[512]; path(p, name);
    FILE *f = fopen(p, "wb"); if (!f){ CHECK(f); return; }
    fprintf(f, "P6\n%u %u\n255\n", BS_W, BS_H);
    for (uint32_t i = 0; i < NPX; i++){
        uint16_t c = fb[i];
        uint8_t px[3] = { (uint8_t)((c >> 11) << 3), (uint8_t)(((c >> 5) & 63u) << 2), (uint8_t)((c & 31u) << 3) };
        fwrite(px, 1, 3, f);
    }
    fclose(f);
}

/* 读 render_ref.py 的 P6, 截成 RGB565 后逐像素比 */
static void compare(const uint16_t *fb, const char *name){
    char p[512]; path(p, name);
    FILE *f = fopen(p, "rb"); if (!f){ printf("FAIL missing %s\n", p); fails++; return; }
    unsigned w, h, mx;
    if (fscanf(f, "P6 %u %u %u", &w, &h, &mx) != 3 || w != BS_W || h != BS_H || mx != 255){ printf("FAIL header %s\n", p); fails++; fclose(f); return; }
    fgetc(f);
    uint8_t *rgb = malloc(NPX * 3u);
    CHECK(fread(rgb, 1, NPX * 3u, f) == NPX * 3u);
    fclose(f);
    uint32_t bad = 0, first = 0;
    for (uint32_t i = 0; i < NPX; i++){
        uint16_t c = BS_RGB(rgb[3 * i], rgb[3 * i + 1], rgb[3 * i + 2]);
        if (c != fb[i]){ if (!bad) first = i; bad++; }
    }
    free(rgb);
    if (bad){ printf("FAIL %s: %u px differ, first at (%u,%u)\n", name, bad, first % BS_W, first / BS_W); fails++; }
    else printf("  %s: %u px identical\n", name, NPX);
}

static uint16_t fb[TB_MAX][NPX];

int main(int argc, char **argv){
    if (argc < 2){ printf("usage: %s DIR\n", argv[0]); return 2; }
    dir = argv[1];

    /* bs_date */
    char d[11];
    bs_date("Nov 18 2026", d); CHECK(strcmp(d, "2026-11-18") == 0);
    bs_date("Jan  8 2027", d); CHECK(strcmp(d, "2027-01-08") == 0);
    bs_date("Dec 31 2026", d); CHECK(strcmp(d, "2026-12-31") == 0);
    char nb[16]; bs_u32(nb, 0); CHECK(strcmp(nb, "0") == 0);
    bs_u32(nb, 4294967295u); CHECK(strcmp(nb, "4294967295") == 0);
    bs_u32(nb, 1000); CHECK(strcmp(nb, "1000") == 0);

    /* 1. 设计稿原样 (v6 / 2026-11-18 / 4352 KiB / x3 / Seg OK / Link WAIT) */
    bs_info_t a = { 6, "2026-11-18", 4352, 3, 1, 0 };
    bs_draw(fb[0], &a); save(fb[0], "c_design.ppm"); compare(fb[0], "ref_design.ppm");

    /* 2. 降级双缓冲 + 段码屏失败 + 收到 HELLO: Link 行只重画状态 */
    bs_date("Nov  8 2026", d);
    bs_info_t b = { 7, d, 312, 2, 0, 0 };
    bs_draw(fb[0], &b);
    memcpy(fb[1], fb[0], sizeof fb[0]);
    uint32_t y0 = 0, y1 = 0;
    bs_draw_link(fb[0], 1, &y0, &y1);
    CHECK(y0 == BS_LINK_Y && y1 == BS_LINK_Y + BS_CH);
    for (uint32_t y = 0; y < BS_H; y++)
        if (y < y0 || y >= y1) CHECK(memcmp(fb[0] + y * BS_W, fb[1] + y * BS_W, BS_W * 2u) == 0 || (printf("  row %u\n", y), 0));
    save(fb[0], "c_hello.ppm"); compare(fb[0], "ref_hello.ppm");

    /* 3. 堆查不到 + 单缓冲 */
    bs_info_t c = { 7, "2026-11-18", 0xFFFFFFFFu, 1, 1, 0 };
    bs_draw(fb[0], &c); compare(fb[0], "ref_single.ppm");

    /* 4. 和三缓冲的配合 (app.c 的顺序): tb_setup -> 画在前台 s -> tb_reset_to(s) -> HELLO 重画 -> tb_reset_to(s)
     *    -> 第一条 RECT 的 tb_begin 选到别的块, 必须先整屏同步成开机画面 */
    for (int k = 0; k < TB_MAX; k++) memset(fb[k], 0x5A, sizeof fb[k]);     /* 别的块里是垃圾 */
    uintptr_t bufs[TB_MAX] = { (uintptr_t)fb[0], (uintptr_t)fb[1], (uintptr_t)fb[2] };
    tb_t T; tb_init(&T, bufs, 3, BS_W, BS_H); tb_reset_to(&T, 0);
    int s = T.s;
    bs_draw(fb[s], &a); tb_reset_to(&T, s);
    bs_draw_link(fb[s], 1, &y0, &y1); tb_reset_to(&T, s);
    int dd = tb_begin(&T, 0, 0, bk_cpu_move, 0);
    CHECK(dd != s && dd >= 0);
    CHECK(memcmp(fb[dd], fb[s], sizeof fb[0]) == 0);
    CHECK(T.full_syncs == 1);
    fb[dd][0] = 0xFFFF; tb_mark(&T, 0, 1);
    CHECK(tb_present(&T) == dd);
    tb_latched(&T);                                     /* 新画面已扫描; 再下一块也要整屏同步, 而且同步源是最新的 dd */
    int d2 = tb_begin(&T, 0, 0, bk_cpu_move, 0);
    CHECK(d2 >= 0 && d2 != dd);
    CHECK(memcmp(fb[d2], fb[dd], sizeof fb[0]) == 0);

    printf(fails ? "bootscreen: %d FAIL\n" : "bootscreen: all ok\n", fails);
    return fails ? 1 : 0;
}
