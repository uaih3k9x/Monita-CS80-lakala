/*
 * fwsim: Monita-CS80 固件的主机模拟器, 不用设备就能测上位机。协议层是真固件代码 (fw/link/dispatch.h 等, 见 simdev.h)。
 *
 *   cc -std=c99 -O2 -I fw/link -o build/fwsim tools/fwsim/fwsim.c      (tools/fwsim/run.sh 会编)
 *   ./fwsim [--nbuf 1..3] [--imm-rows N] [--max-payload N] [--no-keys] [--no-led] [--no-lcd]
 *
 * 开一个伪终端当"USB 串口", 第一行在 stdout 打印 "PTY /dev/ttysNNN"; 上位机把它当串口打开即可
 * (Python: monita_link.Link("/dev/ttysNNN"), Mac: monita-cs80 --device /dev/ttysNNN)。
 * stdin 一行一条控制命令, 每条回一行:
 *   tap K [ms]    按一下键 K (1 查询/PWRKEY, 2 ≡, 3 +, 4 −), 按住 ms (默认 60)  -> ok
 *   down K / up K 按下 / 松开                                                       -> ok
 *   unplug        USB 拔掉 (hotplug 回调报 0, fw v10)                                 -> ok
 *   stat          一行 JSON: 屏上 CRC、缓冲块数、沉浸模式、收拢次数、段码屏显存 + 本地时钟、灯状态机 (三色 / 闪烁相位)、
 *                 滚动动画 (起点 / 目标 / 时长 / 进度)、KEY 计数 ... (字段见 tools/fwsim/README.md)
 *   quit          退出 (stdin 关掉也退出)
 */
#define _DARWIN_C_SOURCE
#define _DEFAULT_SOURCE
#define _XOPEN_SOURCE 600
#include <stdio.h>
#include <stdint.h>
#include <fcntl.h>
#include <poll.h>
#include <termios.h>
#include <unistd.h>
#include <errno.h>
#include <signal.h>

static void sim_out(const uint8_t *p, uint32_t n);
#include "simdev.h"

/* 设备 -> 主机: 先进队列, 主循环可写时再写 (主机正忙着写一大批时不互相卡死) */
static uint8_t *txq; static size_t txq_n, txq_cap;
static void sim_out(const uint8_t *p, uint32_t n){
    if (txq_n + n > txq_cap){
        txq_cap = (txq_n + n) * 2 + 4096;
        txq = (uint8_t *)realloc(txq, txq_cap);
        if (!txq){ perror("realloc"); exit(1); }
    }
    memcpy(txq + txq_n, p, n); txq_n += n;
}

/* 定时松键 */
static struct { int k; uint32_t at; } pend[16];
static int npend;

static void cmd(char *line){
    char op[16] = {0}; int k = 0, ms = 60;
    int n = sscanf(line, "%15s %d %d", op, &k, &ms);
    if (n < 1) return;
    if (!strcmp(op, "quit")) exit(0);
    if (!strcmp(op, "stat")){
        uint32_t t = now_ms();
        char fg[2 * 72 + 1];
        for (int i = 0; i < 72; i++) snprintf(fg + 2 * i, 3, "%02x", fg_fb[i]);
        printf("{\"screen_crc\":%u,\"nbuf\":%u,\"imm\":%d,\"roll_rows\":%u,\"led\":%u,\"backlight\":%u,\"bl_writes\":%u,"
               "\"seg_writes\":%u,\"collapse\":%u,\"uncollapse\":%u,"
               "\"link_up\":%u,\"rx_bytes\":%u,\"frames\":%u,\"crc_err\":%u,\"len_err\":%u,\"rects\":%u,\"presents\":%u,"
               "\"bad_param\":%u,\"flip_now\":%u,\"key_n\":%u,\"boot_live\":%d,",
               sim_screen_crc(), g_tb.n, g_imm, g_roll_rows, sim.led_mask, sim.backlight, sim.bl_writes,
               sim.seg_writes, sim.collapse, sim.uncollapse,
               g_lw.up, st.rx_bytes, g_p.frames, g_p.crc_err, g_p.len_err, st.rects, st.presents,
               st.bad_param, g_flip_now, g_key_n, g_boot_live);
        /* 主机走了 / 休眠 (fw v10): 屏上是不是开机版权页 (CRC 和开机时画的一样)、重画了几次、睡着没、段码屏 EN */
        printf("\"boot_screen\":%d,\"boot_draws\":%u,\"asleep\":%u,\"seg_en\":%u,\"seg_off\":%d,",
               sim_screen_crc() == sim_boot_crc, sim.boot_draws, g_lw.asleep, sim.seg_en, seg_off);
        /* 段码屏显存 + 本地时钟 (segclock.h) */
        printf("\"fg\":\"%s\",\"clk_valid\":%d,\"clk_sod\":%u,\"seg_host_seen\":%d,", fg, clk_valid, clk_sod, seg_host_seen);
        /* 状态灯状态机 (led.h): 时间都是相对现在的 1.024 ms 单位 */
        printf("\"led_mode\":%u,\"led_mask\":%u,\"led_lit\":%u,\"led_on\":%u,\"led_off\":%u,\"led_next_in\":%d,"
               "\"led_timed\":%u,\"led_until_in\":%d,",
               g_led.mode, g_led.mask, g_led.lit, g_led.on, g_led.off, (int32_t)(g_led.next - t),
               g_led.timed, (int32_t)(g_led.until - t));
        /* 沉浸模式滚动 (imm.h) */
        printf("\"im_ys\":%u,\"im_ytgt\":%u,\"im_anim\":%u,\"im_y0\":%u,\"im_d\":%d,\"im_dur\":%u,\"im_elapsed\":%u,\"im_pos\":%u}\n",
               g_im.ys, g_im.ytgt, g_im.anim, g_im.a_y0, g_im.a_d, g_im.a_dur, t - g_im.a_t0, im_pos(&g_im, t));
        fflush(stdout);
        return;
    }
    if (!strcmp(op, "tap") || !strcmp(op, "down")){
        sim_key_set(k, 1);
        if (op[0] == 't' && npend < 16){ pend[npend].k = k; pend[npend].at = now_ms() + (uint32_t)ms; npend++; }
    } else if (!strcmp(op, "up")) sim_key_set(k, 0);
    else if (!strcmp(op, "unplug")) sim_usb_unplug();
    else { printf("err unknown command\n"); fflush(stdout); return; }
    printf("ok\n"); fflush(stdout);
}

static int open_pty(char *name, size_t cap, int *slave_keep){
    int m = posix_openpt(O_RDWR | O_NOCTTY);
    if (m < 0 || grantpt(m) || unlockpt(m)) return -1;
    const char *s = ptsname(m);
    if (!s) return -1;
    snprintf(name, cap, "%s", s);
    int sl = open(name, O_RDWR | O_NOCTTY);           /* 自己留一个从端: 主机关了重开时主端不会 EIO */
    if (sl < 0) return -1;
    struct termios t;
    if (tcgetattr(sl, &t) == 0){ cfmakeraw(&t); tcsetattr(sl, TCSANOW, &t); }
    *slave_keep = sl;
    fcntl(m, F_SETFL, fcntl(m, F_GETFL) | O_NONBLOCK);
    return m;
}

static void usage(void){
    fprintf(stderr, "usage: fwsim [--nbuf 1..3] [--imm-rows N] [--max-payload N] [--no-keys] [--no-led] [--no-lcd]\n");
    exit(2);
}

int main(int argc, char **argv){
    for (int i = 1; i < argc; i++){
        const char *a = argv[i];
        if (!strcmp(a, "--nbuf") && i + 1 < argc) sim_nbuf_want = (uint32_t)atoi(argv[++i]);
        else if (!strcmp(a, "--imm-rows") && i + 1 < argc) sim_roll_rows = (uint32_t)atoi(argv[++i]);
        else if (!strcmp(a, "--max-payload") && i + 1 < argc) sim_max_payload = (uint32_t)atoi(argv[++i]);
        else if (!strcmp(a, "--no-keys")) g_keys_ok = 0;
        else if (!strcmp(a, "--no-led")) g_led_ok = 0;
        else if (!strcmp(a, "--no-lcd")) g_lcd_ok = 0;
        else usage();
    }
    if (sim_max_payload < 16 || sim_max_payload > SIM_PL_CAP) usage();
    signal(SIGPIPE, SIG_IGN);
    char name[128]; int keep;
    int m = open_pty(name, sizeof name, &keep);
    if (m < 0){ perror("pty"); return 1; }
    sim_init();
    printf("PTY %s\n", name); fflush(stdout);

    static uint8_t rx[16384];
    char line[256]; size_t ln = 0;
    for (;;){
        struct pollfd pf[2] = { { m, POLLIN | (txq_n ? POLLOUT : 0), 0 }, { 0, POLLIN, 0 } };
        int r = poll(pf, 2, 1);
        if (r < 0 && errno != EINTR){ perror("poll"); return 1; }
        if (pf[0].revents & POLLIN){
            for (;;){
                ssize_t k = read(m, rx, sizeof rx);
                if (k <= 0) break;
                sim_rx(rx, (uint32_t)k);
            }
        }
        if (txq_n){
            ssize_t k = write(m, txq, txq_n);
            if (k > 0){ memmove(txq, txq + k, txq_n - (size_t)k); txq_n -= (size_t)k; }
        }
        if (pf[1].revents & (POLLIN | POLLHUP)){
            char c; ssize_t k = read(0, &c, 1);
            if (k <= 0) return 0;
            if (c == '\n'){ line[ln] = 0; cmd(line); ln = 0; }
            else if (ln + 1 < sizeof line) line[ln++] = c;
        }
        uint32_t t = now_ms();
        for (int i = 0; i < npend;){
            if ((int32_t)(t - pend[i].at) >= 0){ sim_key_set(pend[i].k, 0); pend[i] = pend[--npend]; }
            else i++;
        }
        sim_tick();
    }
}
