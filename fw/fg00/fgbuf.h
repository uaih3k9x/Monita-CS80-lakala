/*
 * 段码屏显存 + 画数字 (纯内存, 不碰 GPIO): fg00.h (实机驱动) 和 tools/fwsim/simdev.h (模拟器) 共用。
 * 写进显存后由包含方的 fg_flush() 刷到屏 (实机 I2C, 模拟器只记账)。
 */
#ifndef FG00_FGBUF_H
#define FG00_FGBUF_H
#include <stdint.h>

static uint8_t fg_fb[72];   /* COM0[18] COM1[18] COM2[18] COM3[18] */

static void fg_fill(uint8_t v){ for (int i = 0; i < 72; i++) fg_fb[i] = v; }

/* 第 k 位 (0..5) 写 7 段掩码 (bit0..6 = a..g), 位规则见 docs/segmap.md */
__attribute__((unused)) static void fg_digit(int k, uint8_t m){
    uint8_t *c0 = &fg_fb[k], *c1 = &fg_fb[18+k], *c2 = &fg_fb[36+k], *c3 = &fg_fb[54+k];
    *c0 &= 0x77; *c1 &= 0x77; *c2 &= 0x77; *c3 &= 0x7F;
    if (m & 0x20) *c0 |= 0x80;  /* f */
    if (m & 0x40) *c0 |= 0x08;  /* g */
    if (m & 0x01) *c1 |= 0x80;  /* a */
    if (m & 0x02) *c1 |= 0x08;  /* b */
    if (m & 0x10) *c2 |= 0x80;  /* e */
    if (m & 0x04) *c2 |= 0x08;  /* c */
    if (m & 0x08) *c3 |= 0x80;  /* d */
}
#define SEG_E     0x79
#define SEG_DASH  0x40
__attribute__((unused)) static const uint8_t fg_hex[16] = {0x3f,0x06,0x5b,0x4f,0x66,0x6d,0x7d,0x07,
                                   0x7f,0x6f,0x77,0x7c,0x39,0x5e,0x79,0x71};

#endif
