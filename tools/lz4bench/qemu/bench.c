/*
 * QEMU 指令计数用的裸机小程序 (tools/lz4bench/QEMU.md): 和固件同一份 fw/link/lz4.h / proto.h, 同一套 fw/build.sh 编译选项。
 * 每段跑 N 遍, 每遍前后各调一次标记函数 (m_b_* / m_end), 插件 cnt.c 只数标记之间执行的指令和访存。
 *   empty : 两个标记之间什么都不做 (标记本身的开销, 结果里减掉)
 *   dec   : lz4_decode 测试条带 (fw/link/lz4_testdata.h, 输入先拷进 RAM, 同线上 g_pl) -> 暂存区 +8
 *   rect  : lk_rect_apply 暂存区 (8 字节 RECT 头 + 32000 B) -> 800×1280 帧缓冲 (线上第二段)
 *   cpy8  : 逐字节 C 循环拷 32000 B (对照: 最慢的拷法)
 *   ldm   : 和 fw/app.c copy_rows 一样的 ldmia/stmia r4-r11 循环拷 32000 B (固件整行拷贝的真实写法)
 * 每遍解完 / 拷完校验 CRC-32 (不在标记之间, 不计数), 不对就打印 BAD 并以 1 退出。
 */
#pragma GCC diagnostic ignored "-Wunused-function"
#include "lz4.h"
#include "lz4_testdata.h"

#define N 16
#define W 800u
#define H 1280u
void sh_write0(const char *s);
void sh_exit(int code);

static uint8_t  in_ram[LZ4_TD_LEN] __attribute__((aligned(4)));
static uint8_t  scratch[32768] __attribute__((aligned(4)));
static uint8_t  dst[32000] __attribute__((aligned(32)));
static uint16_t fb[W * H] __attribute__((aligned(256)));
volatile uint32_t g_phase;

/* 标记函数: 各写一个不同的值, 免得 -Os 的 ICF 把它们合成一个 */
#define MARK(nm, v) __attribute__((noinline)) void nm(void){ g_phase = v; }
MARK(m_b_empty, 1) MARK(m_b_dec, 2) MARK(m_b_rect, 3) MARK(m_b_cpy8, 4) MARK(m_b_ldm, 5) MARK(m_end, 0)

static __attribute__((noinline)) void cpy8(uint8_t *d, const uint8_t *s, uint32_t n){
    for (uint32_t i = 0; i < n; i++) d[i] = s[i];
}
static __attribute__((noinline)) void cpy_ldm(uint32_t d, uint32_t s, uint32_t n){   /* = fw/app.c copy_rows 的循环 (不含 dcache_clean) */
    uint32_t k = n / 32u;
    __asm__ volatile(
        "1: ldmia %1!, {r4-r11}\n"
        "   stmia %0!, {r4-r11}\n"
        "   subs %2, %2, #1\n"
        "   bne 1b\n"
        : "+r"(d), "+r"(s), "+r"(k) :: "r4", "r5", "r6", "r7", "r8", "r9", "r10", "r11", "cc", "memory");
}

static void check(int ok){ if (!ok){ sh_write0("BAD\n"); sh_exit(1); } }

int main(void){
    for (uint32_t i = 0; i < LZ4_TD_LEN; i++) in_ram[i] = lz4_td_blk[i];
    for (int i = 0; i < N; i++){ m_b_empty(); m_end(); }
    for (int i = 0; i < N; i++){
        for (uint32_t j = 0; j < 8 + LZ4_TD_RAW; j++) scratch[j] = 0;
        m_b_dec();
        int r = lz4_decode(in_ram, LZ4_TD_LEN, scratch + 8, LZ4_TD_RAW);
        m_end();
        check(r == LZ4_OK && lk_crc32(scratch + 8, LZ4_TD_RAW) == LZ4_TD_CRC);
    }
    lk_wr16(scratch, 0); lk_wr16(scratch + 2, LZ4_TD_Y); lk_wr16(scratch + 4, W); lk_wr16(scratch + 6, 20);
    for (int i = 0; i < N; i++){
        uint32_t y0 = H, y1 = 0;
        m_b_rect();
        uint32_t r = lk_rect_apply(fb, W, H, scratch, 8 + LZ4_TD_RAW, &y0, &y1);
        m_end();
        check(r == LK_OK && lk_crc32((const uint8_t *)(fb + LZ4_TD_Y * W), LZ4_TD_RAW) == LZ4_TD_CRC);
    }
    for (int i = 0; i < N; i++){
        m_b_cpy8(); cpy8(dst, scratch + 8, LZ4_TD_RAW); m_end();
        check(lk_crc32(dst, LZ4_TD_RAW) == LZ4_TD_CRC);
    }
    for (int i = 0; i < N; i++){
        m_b_ldm(); cpy_ldm((uint32_t)dst, (uint32_t)(fb + LZ4_TD_Y * W), LZ4_TD_RAW); m_end();
        check(lk_crc32(dst, LZ4_TD_RAW) == LZ4_TD_CRC);
    }
    sh_write0("ok: decode / rect / cpy8 / ldm CRC 全对\n");
    sh_exit(0);
    return 0;
}
