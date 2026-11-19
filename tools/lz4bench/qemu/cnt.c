/*
 * 最小 QEMU TCG 插件: 按"标记函数"分段统计执行的指令数和内存访问 (tools/lz4bench/QEMU.md)。
 *   -plugin libcnt.dylib,b=NAME@0xADDR,b=...,e=0xADDR
 *   执行到 b 的地址 = 开始一段 NAME (记下当前计数), 执行到 e 的地址 = 这段结束, 差值累计进 NAME。
 *   退出时 (qemu_plugin_outs, 配 -d plugin -D 文件) 每段打印一行:
 *   NAME 次数 指令 访存指令 load指令 store指令 load次数 store次数 load字节 store字节 (都是全部次数的总和)
 * ldm/stm 一条指令算 1 条访存指令, 每个字各算 1 次访问。单 vCPU, 不加锁。
 */
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <qemu-plugin.h>

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;

typedef struct { uint64_t insn, minsn, ldi, sti, ld, st, ldb, stb; } cnt_t;
#define MAXP 16
static struct { char name[32]; uint64_t addr; uint64_t n; cnt_t sum; } ph[MAXP];
static int nph, cur = -1;
static uint64_t end_addr;
static cnt_t c, at;              /* 全局计数 / 这一段开始时的快照 */
static uint64_t seq, mem_seq;    /* 第几条指令 / 最近一次访存属于第几条 */

static void on_insn(unsigned int vcpu, void *ud){
    (void)vcpu;
    seq++; c.insn++;
    intptr_t k = (intptr_t)ud;       /* 0 = 普通; 1..MAXP = 第 k-1 段开始; -1 = 结束 */
    if (k > 0){ cur = (int)k - 1; at = c; at.insn--; }   /* 标记函数第一条指令算进这一段 */
    else if (k < 0 && cur >= 0){
        cnt_t *s = &ph[cur].sum;
        s->insn += c.insn - 1 - at.insn; s->minsn += c.minsn - at.minsn; s->ldi += c.ldi - at.ldi; s->sti += c.sti - at.sti;
        s->ld += c.ld - at.ld; s->st += c.st - at.st; s->ldb += c.ldb - at.ldb; s->stb += c.stb - at.stb;
        ph[cur].n++; cur = -1;
    }
}

static void on_mem(unsigned int vcpu, qemu_plugin_meminfo_t info, uint64_t va, void *ud){
    (void)vcpu; (void)va; (void)ud;
    int store = qemu_plugin_mem_is_store(info);
    unsigned b = 1u << qemu_plugin_mem_size_shift(info);
    if (mem_seq != seq){ mem_seq = seq; c.minsn++; if (store) c.sti++; else c.ldi++; }
    if (store){ c.st++; c.stb += b; } else { c.ld++; c.ldb += b; }
}

static void on_tb(qemu_plugin_id_t id, struct qemu_plugin_tb *tb){
    (void)id;
    size_t n = qemu_plugin_tb_n_insns(tb);
    for (size_t i = 0; i < n; i++){
        struct qemu_plugin_insn *in = qemu_plugin_tb_get_insn(tb, i);
        uint64_t a = qemu_plugin_insn_vaddr(in);
        intptr_t k = 0;
        if (a == end_addr) k = -1;
        for (int j = 0; j < nph; j++) if (ph[j].addr == a) k = j + 1;
        qemu_plugin_register_vcpu_insn_exec_cb(in, on_insn, QEMU_PLUGIN_CB_NO_REGS, (void *)k);
        qemu_plugin_register_vcpu_mem_cb(in, on_mem, QEMU_PLUGIN_CB_NO_REGS, QEMU_PLUGIN_MEM_RW, 0);
    }
}

static void on_exit_cb(qemu_plugin_id_t id, void *ud){
    (void)id; (void)ud;
    char line[256];
    qemu_plugin_outs("# name n insn mem_insn ld_insn st_insn loads stores ld_bytes st_bytes (sums)\n");
    for (int j = 0; j < nph; j++){
        cnt_t *s = &ph[j].sum;
        snprintf(line, sizeof line, "%s %" PRIu64 " %" PRIu64 " %" PRIu64 " %" PRIu64 " %" PRIu64 " %" PRIu64 " %" PRIu64 " %" PRIu64 " %" PRIu64 "\n",
                 ph[j].name, ph[j].n, s->insn, s->minsn, s->ldi, s->sti, s->ld, s->st, s->ldb, s->stb);
        qemu_plugin_outs(line);
    }
    snprintf(line, sizeof line, "# total insn %" PRIu64 "\n", c.insn);
    qemu_plugin_outs(line);
}

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id, const qemu_info_t *info, int argc, char **argv){
    (void)info;
    for (int i = 0; i < argc; i++){
        if (!strncmp(argv[i], "b=", 2) && nph < MAXP){
            const char *at_ = strchr(argv[i], '@');
            if (!at_) return -1;
            size_t l = (size_t)(at_ - argv[i] - 2); if (l > 31) l = 31;
            memcpy(ph[nph].name, argv[i] + 2, l); ph[nph].name[l] = 0;
            ph[nph].addr = strtoull(at_ + 1, 0, 0) & ~1ull;     /* Thumb 函数地址的 bit0 */
            nph++;
        } else if (!strncmp(argv[i], "e=", 2)) end_addr = strtoull(argv[i] + 2, 0, 0) & ~1ull;
        else return -1;
    }
    qemu_plugin_register_vcpu_tb_trans_cb(id, on_tb);
    qemu_plugin_register_atexit_cb(id, on_exit_cb, 0);
    return 0;
}
