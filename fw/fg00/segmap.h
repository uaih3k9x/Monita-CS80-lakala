/* CS80 R05 段码屏映射 (来源: 原厂渲染函数 + 字库表 + 各 setter, 见 docs/segmap.md)
 * 显存 fg_fb[72] = 4 COM x 18 字节; (com,seg) -> fg_fb[18*com + seg/8] 位 (7 - seg%8)。
 * 7 段数字: 任一列 k 用 fg_digit(k, 掩码) (a..g = bit0..6), 段位: f=fb[k]&0x80 g=fb[k]&0x08 a=fb[18+k]&0x80
 *   b=fb[18+k]&0x08 e=fb[36+k]&0x80 c=fb[36+k]&0x08 d=fb[54+k]&0x80 (原厂 15 个数字位全部一致, 证实)。
 * 全部段已实机确认 (2026-11-18)。电池四格 A(0x2c) 最左 → D(0x2f) 最右, 实机确认。实机 segprobe(2026-11-18): 30 个单段以外的位全部不亮, 没有固定图标。 */
#ifndef FG00_SEGMAP_H
#define FG00_SEGMAP_H
#include "fgbuf.h"

/* 数字列 (证实) */
#define FG_COL_MAIN(i) (i)          /* 主数字 6 位, i=0..5 (左->右) */
#define FG_COL_NET     6            /* "8G" 里的 8 (4G 模式显示 4) */
#define FG_COL_CLK(i)  (8 + (i))    /* 小时钟 88:88:88, i=0..5 */
#define FG_COL_YUAN(i) (14 + (i))   /* "元88" 的两位, i=0..1 */

/* 单段: 编码 = (字节号<<8) | 位掩码 */
#define FG_SEG(byte, mask) (((byte) << 8) | (mask))
#define FG_SEG_MAIN_DP4     FG_SEG(56, 0x08)   /* state[0x12] com3 seg 20  主数字区第4位后小数点 */
#define FG_SEG_MAIN_DP5     FG_SEG(58, 0x08)   /* state[0x13] com3 seg 36  主数字区第5位后小数点 */
#define FG_SEG_MAIN_COLON2  FG_SEG(55, 0x08)   /* state[0x14] com3 seg 12  主数字区第2位后冒号 */
#define FG_SEG_MAIN_COLON4  FG_SEG(57, 0x08)   /* state[0x15] com3 seg 28  主数字区第4位后冒号 */
#define FG_SEG_CLK_COLON_A  FG_SEG(63, 0x08)   /* state[0x16] com3 seg 76  小时钟冒号(之一) */
#define FG_SEG_CLK_COLON_B  FG_SEG(65, 0x08)   /* state[0x17] com3 seg 92  小时钟冒号(之二) */
#define FG_SEG_YUAN         FG_SEG(59, 0x08)   /* state[0x18] com3 seg 44  "元" (实机 F18) */
#define FG_SEG_NET_G        FG_SEG(60, 0x08)   /* state[0x19] com3 seg 52  4G 的 "G" */
#define FG_SEG_NET_X        FG_SEG(25, 0x80)   /* state[0x1a] com1 seg 56  "8G" 的 × (实机 F1A) */
#define FG_SEG_SIG1         FG_SEG(61, 0x80)   /* state[0x1b] com3 seg 56  信号第1格 */
#define FG_SEG_SIG2         FG_SEG(43, 0x80)   /* state[0x1c] com2 seg 56  信号第2格 */
#define FG_SEG_SIG3         FG_SEG( 7, 0x80)   /* state[0x1d] com0 seg 56  信号第3格 */
#define FG_SEG_SIG4         FG_SEG(25, 0x20)   /* state[0x1e] com1 seg 58  信号第4格 */
#define FG_SEG_WIFI_X       FG_SEG(43, 0x20)   /* state[0x1f] com2 seg 58  WiFi 的 × (实机 F1F) */
#define FG_SEG_WIFI0        FG_SEG(61, 0x20)   /* state[0x20] com3 seg 58  WiFi 第1级(等级0也亮) */
#define FG_SEG_WIFI1        FG_SEG( 7, 0x20)   /* state[0x21] com0 seg 58  WiFi 第2级 */
#define FG_SEG_WIFI2        FG_SEG( 7, 0x08)   /* state[0x22] com0 seg 60  WiFi 第3级 */
#define FG_SEG_WIFI3        FG_SEG(25, 0x08)   /* state[0x23] com1 seg 60  WiFi 第4级 */
#define FG_SEG_SPK          FG_SEG(43, 0x08)   /* state[0x24] com2 seg 60  喇叭本体 (实机 segprobe F24) */
#define FG_SEG_VOL1         FG_SEG(61, 0x08)   /* state[0x25] com3 seg 60  声波第1道 (实机 F25) */
#define FG_SEG_VOL2         FG_SEG(61, 0x02)   /* state[0x26] com3 seg 62  声波第2道 (实机 F26) */
#define FG_SEG_VOL3         FG_SEG(43, 0x02)   /* state[0x27] com2 seg 62  声波第3道 (实机 F27) */
#define FG_SEG_VOL4         FG_SEG( 7, 0x02)   /* state[0x28] com0 seg 62  声波第4道 (实机 F28) */
#define FG_SEG_VOL5         FG_SEG(25, 0x02)   /* state[0x29] com1 seg 62  声波第5道 (实机 F29) */
#define FG_SEG_BAT_FRAME    FG_SEG(70, 0x80)   /* state[0x2a] com3 seg128  电池外框 (实机 F2A) */
#define FG_SEG_CHARGE       FG_SEG(52, 0x80)   /* state[0x2b] com2 seg128  充电闪电 (实机 F2B) */
#define FG_SEG_BAT_A        FG_SEG(70, 0x40)   /* state[0x2c] com3 seg129  电池格最左 (实机 F2C) */
#define FG_SEG_BAT_B        FG_SEG(16, 0x40)   /* state[0x2d] com0 seg129  电池格 (实机 F2D) */
#define FG_SEG_BAT_C        FG_SEG(16, 0x80)   /* state[0x2e] com0 seg128  电池格 (实机 F2E) */
#define FG_SEG_BAT_D        FG_SEG(34, 0x80)   /* state[0x2f] com1 seg128  电池格 (实机 F2F) */

/* 电平控制单段 (改完要 fg_flush) */
__attribute__((unused)) static void fg_seg(unsigned code, int on){
    uint8_t *p = &fg_fb[code >> 8]; uint8_t m = (uint8_t)code;
    if (on) *p |= m; else *p = (uint8_t)(*p & ~m);
}

/* 原厂等级函数的复刻 (证实): 信号 0..4 格, WiFi 等级 0..3 (0 也亮第1级), 音量 0..5 (不含喇叭本体) */
__attribute__((unused)) static void fg_signal(unsigned n){
    fg_seg(FG_SEG_SIG1, n >= 1); fg_seg(FG_SEG_SIG2, n >= 2); fg_seg(FG_SEG_SIG3, n >= 3); fg_seg(FG_SEG_SIG4, n >= 4);
}
__attribute__((unused)) static void fg_wifi(unsigned n){
    fg_seg(FG_SEG_WIFI0, 1); fg_seg(FG_SEG_WIFI1, n >= 1); fg_seg(FG_SEG_WIFI2, n >= 2); fg_seg(FG_SEG_WIFI3, n >= 3);
}
/* 同原厂音量 setter: n>=1 亮 0x25, n>=2..5 依次加 0x26..0x29; 喇叭本体 0x24 由别处单独控制 */
__attribute__((unused)) static void fg_volume(unsigned n){
    fg_seg(FG_SEG_VOL1, n >= 1); fg_seg(FG_SEG_VOL2, n >= 2); fg_seg(FG_SEG_VOL3, n >= 3);
    fg_seg(FG_SEG_VOL4, n >= 4); fg_seg(FG_SEG_VOL5, n >= 5);
}
/* 电池: 外框 + n 格 (0..4)。BAT_A..D 从左到右, 实机确认 */
__attribute__((unused)) static void fg_battery(unsigned n, int charging){
    fg_seg(FG_SEG_BAT_FRAME, 1);
    fg_seg(FG_SEG_BAT_A, n >= 1); fg_seg(FG_SEG_BAT_B, n >= 2); fg_seg(FG_SEG_BAT_C, n >= 3); fg_seg(FG_SEG_BAT_D, n >= 4);
    fg_seg(FG_SEG_CHARGE, charging);
}

#endif
