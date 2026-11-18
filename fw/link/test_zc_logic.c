/* 主机单测 zc_logic.h: sh fw/link/test.sh */
#include <stdio.h>
#include "zc_logic.h"
static int fails;
#define CHECK(c) do{ if(!(c)){ printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } }while(0)
int main(void){
    const zc_range_t ps[1] = {{0x80000000u, 0x81000000u}};
    /* 白名单 */
    CHECK(zc_reg_write_ok(0x34, 0x80de0000u, 0, ps, 1));
    CHECK(!zc_reg_write_ok(0x34, 0x80de0002u, 0, ps, 1));                 /* 不对齐 */
    CHECK(!zc_reg_write_ok(0x34, 0x81000000u - 0x1000u, 0, ps, 1));       /* 整帧越界 */
    CHECK(!zc_reg_write_ok(0x34, 0x60000000u, 0, ps, 1));                 /* flash */
    CHECK(zc_reg_write_ok(0x38, 0x06400001u, 0, ps, 1));
    CHECK(!zc_reg_write_ok(0x38, 0x06400000u, 0, ps, 1));                 /* 关层不许 */
    CHECK(!zc_reg_write_ok(0x3c, 0x80de0000u, 0, ps, 1));                 /* 未 arm */
    CHECK(zc_reg_write_ok(0x3c, 0x80de0000u, 1, ps, 1));
    CHECK(zc_reg_write_ok(0x3c, 0u, 1, ps, 1));
    CHECK(zc_reg_write_ok(0x40, 0x05dc0001u, 1, ps, 1));                  /* 750*2 = 0x5dc */
    CHECK(!zc_reg_write_ok(0x40, 0x05dd0001u, 1, ps, 1));                 /* 奇数行跨 */
    CHECK(!zc_reg_write_ok(0x40, 0x80000001u, 1, ps, 1));
    CHECK(zc_reg_write_ok(0x44, (337u << 16) | 43u, 1, ps, 1));
    CHECK(!zc_reg_write_ok(0x44, 0x08000000u, 1, ps, 1));
    CHECK(!zc_reg_write_ok(0x2c, 0x143u, 1, ps, 1));                      /* 时序/格式类一律不许 */
    CHECK(!zc_reg_write_ok(0x4c, 0x05000320u, 1, ps, 1));
    CHECK(!zc_reg_write_ok(0x50, 2u, 1, ps, 1));
    CHECK(!zc_reg_write_ok(0x08, 0x04u, 1, ps, 1));
    CHECK(zc_reg_read_ok(0x24) && zc_reg_read_ok(0x3c) && zc_reg_read_ok(0x50));
    CHECK(!zc_reg_read_ok(0x40) && !zc_reg_read_ok(0x44) && !zc_reg_read_ok(0x48) && !zc_reg_read_ok(0x54));
    /* 分屏基址 */
    uint32_t sb = 0x80a00000u;
    CHECK(zc_split_base(sb, 0, 337, ZC_STRIDE, ZC_SEM_REL) == sb);
    CHECK(zc_split_base(sb, 10, 337, ZC_STRIDE, ZC_SEM_REL) == sb + 10u * 1600u);
    CHECK(zc_split_base(sb, 10, 337, ZC_STRIDE, ZC_SEM_ABS) == sb + 10u * 1600u - 337u * 1600u);
    /* ABS: 控制器在第 337 行读到的地址 = base + 337*pitch = 滚动缓冲第 top 行 */
    CHECK(zc_split_base(sb, 10, 337, ZC_STRIDE, ZC_SEM_ABS) + 337u * 1600u == sb + 10u * 1600u);
    CHECK(zc_restore_base(0x80de0000u, 851, ZC_STRIDE, ZC_SEM_ABS) == 0x80de0000u);
    CHECK(zc_restore_base(0x80de0000u, 851, ZC_STRIDE, ZC_SEM_REL) == 0x80de0000u + 851u * 1600u);
    /* 行时刻: 26600 us / 1310 行 */
    uint32_t q = (uint32_t)((26600ull << 16) / ZC_VTOTAL);
    CHECK(ZC_VTOTAL == 1310u);
    uint32_t t = zc_line_deadline_us(337, 0, q);                          /* (14+337)*20.305 = 7127 */
    CHECK(t >= 7120u && t <= 7135u);
    CHECK(zc_line_deadline_us(0, 100, q) == 0u);
    CHECK(zc_line_deadline_us(851, 3, q) < zc_line_deadline_us(851, 0, q));
    /* 尺子 */
    CHECK(zc_ruler_px(0, 500, 0x07E0) == 0xF800u && zc_ruler_px(300, 5, 0x07E0) == 0xF800u);
    CHECK(zc_ruler_px(1, 10u * 32u + 1u, 0) == 0xFFFFu);                  /* bit0 在第 11 块 */
    CHECK(zc_ruler_px(1, 0, 0) == 0x0000u);
    CHECK(zc_ruler_px(1024 + 1, 1, 0) == 0xFFFFu);                        /* bit10 在第 1 块 */
    CHECK(zc_ruler_px(5, 30, 0xFFFF) == 0x0000u);                         /* 间隔 */
    /* 回卷 */
    CHECK(!zc_need_rebase(0, 200, 514, 1500));
    CHECK(zc_need_rebase(800, 200, 514, 1500));
    zc_rebase_t p = zc_rebase_plan(800, 514);
    CHECK(p.src_row == 800 && p.dst_row == 0 && p.rows == 514);
    printf(fails ? "%d FAILED\n" : "ok\n", fails);
    return fails != 0;
}
