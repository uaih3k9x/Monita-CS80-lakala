#!/usr/bin/env python3
"""tools/lz4bench/qemu/run.sh 的后半段: 读插件输出 (counts.txt), 减掉标记开销 (empty 段), 按每遍打印, 再按 CPI / 主频换算。粗估, 见 QEMU.md。"""
import sys

RAW, STRIPS = 32000, 64
rows = {}
for line in open(sys.argv[1]):
    f = line.split()
    if not f or f[0].startswith("#"):
        continue
    n = int(f[1])
    rows[f[0]] = [int(x) / n for x in f[2:]]
base = rows.pop("empty")
names = ("insn", "mem_insn", "ld_insn", "st_insn", "loads", "stores", "ld_B", "st_B")
print("每遍 (已减标记开销: 每遍 %s)" % " ".join("%s=%g" % (k, v) for k, v in zip(names, base)))
print("%-5s %9s %9s %8s %8s %8s %8s %8s %8s  %s" % (("段",) + names + ("指令/字节",)))
net = {}
for k, v in rows.items():
    net[k] = [a - b for a, b in zip(v, base)]
    print("%-5s " % k + " ".join("%9.0f" % x if i < 2 else "%8.0f" % x for i, x in enumerate(net[k])) + "  %.3f" % (net[k][0] / RAW))
print("\n换算 (时间 = 指令 × CPI / 主频; 只算指令, 不含 PSRAM / cache / XIP 等待, 粗估)")
print("%-5s %8s %8s | %s" % ("段", "CPI", "MHz", "us/条带  ms/帧(x64)"))
for k in ("dec", "rect", "ldm", "cpy8"):
    for cpi in (1.0, 1.5):
        for mhz in (400, 500):
            us = net[k][0] * cpi / mhz
            print("%-5s %8.1f %8d | %8.1f  %8.2f" % (k, cpi, mhz, us, us * STRIPS / 1000))
d = net["dec"][0] + net["rect"][0]
print("\ndec+rect 每条带 %.0f 条指令 -> 整帧 (x64) %.2f–%.2f ms (CPI 1.0 @500 MHz .. CPI 1.5 @400 MHz)"
      % (d, d * STRIPS / 500e3, d * 1.5 * STRIPS / 400e3))
