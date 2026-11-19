#!/usr/bin/env python3
"""RECT_LZ4 (fw v11) 基准: 真实渲染帧 (docs/img、linux/docs/img 的 800×1280 截图) 的压缩比、主机压缩耗时、
固件解码器 (fw/link/lz4.h, 主机上编) 的解码耗时, 外加和 liblz4 的差分模糊对拍。不连设备。

  python3 tools/lz4bench/bench.py [--fuzz N] [--no-apple]

要 python lz4 (pip install lz4)、Pillow、cc; Mac 上有 swiftc 时顺带测 Apple Compression (Mac 上位机的编码器)。
切法和上位机一样: 整行宽 20 行一条 (32000 B 原始), 每条压缩后 < 3/4 才发 RECT_LZ4。
"""
import argparse
import glob
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools", "host-ref"))
import monita_link as ml  # noqa: E402
import lz4.block  # noqa: E402
from PIL import Image  # noqa: E402

W, H, ROWS = 800, 1280, 20
STRIP = W * ROWS * 2
LINK_MBPS = 2049162 / 0.320 / 1e6          # 实测 (docs/protocol.md): 整帧 64 条 RECT 线上 2,049,162 B ≈ 320 ms -> 6.4 MB/s
HERE = os.path.dirname(os.path.abspath(__file__))


def frames():
    out = []
    for p in sorted(glob.glob(os.path.join(ROOT, "docs", "img", "*.png")) + glob.glob(os.path.join(ROOT, "linux", "docs", "img", "*.png"))):
        im = Image.open(p)
        if im.size == (W, H):
            out.append((os.path.relpath(p, ROOT), ml.rgb888_to_rgb565(im.convert("RGB").tobytes())))
    return out


def has_offset0(blk):
    """块里有没有 offset = 0 的 match (规范: 坏块; liblz4 1.9 的快路径不查, 固件解码器拒)"""
    i = 0
    try:
        while i < len(blk):
            t = blk[i]; i += 1
            n = t >> 4
            if n == 15:
                while True:
                    b = blk[i]; i += 1; n += b
                    if b != 255:
                        break
            i += n
            if i >= len(blk):
                return False
            if blk[i] | blk[i + 1] << 8 == 0:
                return True
            i += 2
            if t & 15 == 15:
                while blk[i] == 255:
                    i += 1
                i += 1
    except IndexError:
        pass
    return False


def seq_stats(blk):
    """数一个块里的序列: (序列数, 字面量字节, match 字节, offset ≤ 2 的 match 字节, 走按字快路径的 match 字节)
    快路径 = 固件 lz4.h 里按字写的那两种: offset 1 / 2 且长度 ≥ 8, 或 offset 是 4 的倍数 (两边地址同余)"""
    i, nseq, lit, mat, o12, fast = 0, 0, 0, 0, 0, 0
    while i < len(blk):
        t = blk[i]; i += 1
        n = t >> 4
        if n == 15:
            while True:
                b = blk[i]; i += 1; n += b
                if b != 255:
                    break
        lit += n; i += n; nseq += 1
        if i >= len(blk):
            break
        off = blk[i] | blk[i + 1] << 8; i += 2
        m = t & 15
        if m == 15:
            while True:
                b = blk[i]; i += 1; m += b
                if b != 255:
                    break
        m += 4; mat += m
        if off <= 2:
            o12 += m
        if (off <= 2 and m >= 8) or off % 4 == 0:
            fast += m
    return nseq, lit, mat, o12, fast


def build(tmp):
    exe = os.path.join(tmp, "lz4bench")
    subprocess.run(["cc", "-std=c99", "-O2", "-Wall", "-Wextra", "-Werror", "-I", os.path.join(ROOT, "fw", "link"),
                    "-o", exe, os.path.join(HERE, "lz4bench.c")], check=True)
    apple = None
    if shutil.which("swiftc"):
        apple = os.path.join(tmp, "apple_lz4")
        r = subprocess.run(["swiftc", "-O", "-o", apple, os.path.join(HERE, "apple_lz4.swift")], capture_output=True)
        if r.returncode:
            apple = None
    return exe, apple


def records(blocks):
    out = bytearray()
    for k, b in enumerate(blocks):
        out += struct.pack("<HHHHII", 0, k * ROWS % H, W, ROWS, STRIP, len(b)) + b
    return bytes(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fuzz", type=int, default=20000, help="和 liblz4 差分对拍的变异块数")
    ap.add_argument("--no-apple", action="store_true")
    args = ap.parse_args()
    tmp = tempfile.mkdtemp()
    exe, apple = build(tmp)
    if args.no_apple:
        apple = None
    print("%-30s %9s %9s %9s %6s %8s %8s %9s %9s %s" % ("帧", "整帧压", "条带压", "Apple条带", "倍数", "压缩ms", "Apple ms", "主机解码", "解+拷", "offset≤2 占 match"))
    tot = []
    for name, fb in frames():
        whole = len(lz4.block.compress(fb, store_size=False))
        t0 = time.perf_counter()
        blocks = [lz4.block.compress(fb[k * STRIP:(k + 1) * STRIP], store_size=False) for k in range(H // ROWS)]
        t_comp = time.perf_counter() - t0
        sent = sum(len(b) + 12 if ml.lz4_worth(len(b), STRIP) else STRIP + 8 for b in blocks) + 10 * len(blocks)
        a_sent, a_ms = None, None
        if apple:
            raw = os.path.join(tmp, "raw"); outp = os.path.join(tmp, "apple")
            open(raw, "wb").write(fb)
            ns = int(subprocess.run([apple, raw, outp, str(STRIP)], capture_output=True, text=True, check=True).stdout)
            d, i, ab = open(outp, "rb").read(), 0, []
            while i < len(d):
                n = struct.unpack_from("<I", d, i)[0]; ab.append(d[i + 4:i + 4 + n]); i += 4 + n
            a_sent = sum(len(b) + 12 if ml.lz4_worth(len(b), STRIP) else STRIP + 8 for b in ab) + 10 * len(ab)
            a_ms = ns / 1e6
            # Apple 编的块也要被固件解码器解对
            p = os.path.join(tmp, "arec"); open(p, "wb").write(records(ab))
            crc = int(subprocess.run([exe, "bench", p, "1"], capture_output=True, text=True, check=True).stdout.split()[0])
            assert crc == zlib.crc32(fb) & 0xFFFFFFFF, name + ": 固件解码器解 Apple 的块不对"
        p = os.path.join(tmp, "rec"); open(p, "wb").write(records(blocks))
        crc, ns_all, ns_dec = subprocess.run([exe, "bench", p, "30"], capture_output=True, text=True, check=True).stdout.split()
        assert int(crc) == zlib.crc32(fb) & 0xFFFFFFFF, name + ": 固件解码器解 liblz4 的块不对"
        st = [sum(x) for x in zip(*(seq_stats(b) for b in blocks))]
        tot.append(dict(sent=sent, a_sent=a_sent, nseq=st[0], lit=st[1], mat=st[2], fast=st[4], ns_all=float(ns_all), ns_dec=float(ns_dec)))
        print("%-30s %7d KB %7d KB %7s KB %5.0fx %8.2f %8s %7.2f ms %7.2f ms %5.0f%%"
              % (name, whole // 1024, sent // 1024, a_sent // 1024 if a_sent else "-", (STRIP * 64 + 640) / sent, t_comp * 1000,
                 "%.2f" % a_ms if a_ms else "-", float(ns_dec) / 1e6, float(ns_all) / 1e6, 100 * st[3] / max(1, st[2])))
    n = len(tot)
    avg = lambda k: sum(t[k] for t in tot) / n
    print("\n平均: 线上 %.0f KB/帧 (RECT 整帧 2001 KB)；按实测链路 %.1f MB/s 传输 ≈ %.1f ms (RECT ≈ %.0f ms)"
          % (avg("sent") / 1024, LINK_MBPS, avg("sent") / LINK_MBPS / 1e3, 2049162 / LINK_MBPS / 1e3))
    print("每帧平均 %.0f 个序列, 字面量 %.0f B, match %.0f B (其中按字快路径 %.0f%%)"
          % (avg("nseq"), avg("lit"), avg("mat"), 100 * avg("fast") / avg("mat")))
    # A5 粗估 (没上机, 只是数量级): 逐字节 ~5 周期/字节, 按字 ~6 周期/4 字节, 每序列 ~40 周期; 再加暂存区 -> 帧缓冲 2 MB 的拷贝
    for mhz in (400, 500):
        dec = (5 * (avg("lit") + avg("mat") - avg("fast")) + 1.5 * avg("fast") + 40 * avg("nseq")) / mhz / 1e3
        byte = (5 * (avg("lit") + avg("mat")) + 40 * avg("nseq")) / mhz / 1e3
        print("A5 @ %d MHz 估计: 解码 ≈ %.0f ms (全逐字节则 %.0f ms) + 拷进帧缓冲 2 MB ≈ 31 ms (64 MB/s PSRAM 写) + 传输 ≈ %.0f ms -> 整帧 ≈ %.0f–%.0f ms"
              % (mhz, dec, byte, avg("sent") / LINK_MBPS / 1e3, dec + 31, dec + 31 + avg("sent") / LINK_MBPS / 1e3))

    # ---- 差分模糊: 固件解码器 vs liblz4 (LZ4_decompress_safe) ----
    rnd = random.Random(1)
    src = [b for _, fb in frames()[:3] for b in (lz4.block.compress(fb[k * STRIP:(k + 1) * STRIP], store_size=False) for k in range(0, 64, 7))]
    muts = []
    for _ in range(args.fuzz):
        b = bytearray(rnd.choice(src))
        k = rnd.randrange(4)
        if k == 0:
            b = b[:rnd.randrange(len(b) + 1)]
        elif k == 1:
            for _ in range(rnd.randint(1, 3)):
                b[rnd.randrange(len(b))] = rnd.randrange(256)
        elif k == 2:
            p = rnd.randrange(len(b)); b[p:p + 8] = b"\xff" * 8
        else:
            b[rnd.randrange(len(b))] ^= 1 << rnd.randrange(8)
        muts.append(bytes(b))
    p = os.path.join(tmp, "fuzz"); open(p, "wb").write(records(muts))
    mine = subprocess.run([exe, "check", p], capture_output=True, text=True, check=True).stdout.split("\n")
    agree = only_mine = only_lib = lib_off0 = differ = 0
    for b, line in zip(muts, mine):
        try:
            ref = zlib.crc32(lz4.block.decompress(b, uncompressed_size=STRIP)) & 0xFFFFFFFF
            ref = ref if len(lz4.block.decompress(b, uncompressed_size=STRIP)) == STRIP else None
        except lz4.block.LZ4BlockError:
            ref = None
        ok = line.startswith("ok ")
        if ok and ref is not None:
            if int(line.split()[1]) == ref:
                agree += 1
            else:
                differ += 1
        elif not ok and ref is None:
            agree += 1
        elif ok:
            only_mine += 1
        elif has_offset0(b):
            lib_off0 += 1
        else:
            only_lib += 1
    print("\n和 liblz4 %s 差分 (%d 个变异块): 结论一致 %d; 只有固件解码器接受 %d (liblz4 更严的编码端约定: 末尾 5 字节字面量等); "
          "liblz4 接受了 offset 0 的块、固件按规范拒 %d; 其余只有 liblz4 接受 %d; 都接受但内容不同 %d"
          % (lz4.library_version_string(), len(muts), agree, only_mine, lib_off0, only_lib, differ))
    shutil.rmtree(tmp)
    return 1 if only_lib or differ else 0


if __name__ == "__main__":
    sys.exit(main())
