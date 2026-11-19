#!/usr/bin/env python3
"""生成 fw/link/lz4_testdata.h: DIAG 33 (LZ4 自测) 内嵌的一条测试条带, 也是 tools/lz4bench/QEMU.md 指令计数用的输入。

  python3 tools/lz4bench/gen_testdata.py [--img docs/img/lyrics-playing.png] [--out fw/link/lz4_testdata.h] [--bin FILE]

取法: 一张真实渲染截图 (默认 docs/img/lyrics-playing.png, 歌词页, 64 条带共 83 KB, 偏重的 UI 画面),
按上位机的切法 (整行宽 20 行一条, 32000 B RGB565) 切 64 条, 每条用 liblz4 (python lz4.block, 同上位机) 压,
选压缩后大小最接近这一帧 64 条平均的那条 —— 解它 64 遍 ≈ 解这一整帧。
--bin 另存一份 [u32 raw_len | u32 crc32 | LZ4 块] 给 QEMU 指令计数的小程序读。
要 python lz4、Pillow。生成结果提交进仓库 (固件构建不依赖 python lz4)。
"""
import argparse
import os
import struct
import sys
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools", "host-ref"))
import monita_link as ml  # noqa: E402
import lz4.block  # noqa: E402
from PIL import Image  # noqa: E402

W, H, ROWS = 800, 1280, 20
STRIP = W * ROWS * 2


def pick(img):
    im = Image.open(os.path.join(ROOT, img))
    if im.size != (W, H):
        sys.exit("%s 不是 %dx%d" % (img, W, H))
    fb = ml.rgb888_to_rgb565(im.convert("RGB").tobytes())
    strips = [fb[i * STRIP:(i + 1) * STRIP] for i in range(H // ROWS)]
    blks = [lz4.block.compress(s, store_size=False) for s in strips]
    mean = sum(map(len, blks)) / len(blks)
    k = min(range(len(blks)), key=lambda i: (abs(len(blks[i]) - mean), i))
    return k, strips[k], blks[k], mean, sum(map(len, blks))


def header(img, k, raw, blk, mean, total):
    crc = zlib.crc32(raw) & 0xFFFFFFFF
    out = ["/* 生成文件, 别手改: python3 tools/lz4bench/gen_testdata.py (见那里的说明)",
           " * 来源 %s 第 %d 条 (y = %d..%d, 800x20 RGB565 = %d B), liblz4 %s 压成 %d B;" % (img, k, k * ROWS, k * ROWS + ROWS - 1, len(raw), lz4.library_version_string(), len(blk)),
           " * 这一帧 64 条共 %d B, 平均 %.0f B/条, 这条最接近平均 -> 解 64 遍 ≈ 解这一整帧。DIAG 33 (dispatch.h lz4_bench) 用。 */" % (total, mean),
           "#ifndef MONITA_LINK_LZ4_TESTDATA_H", "#define MONITA_LINK_LZ4_TESTDATA_H",
           "#include <stdint.h>",
           "#define LZ4_TD_RAW  %du" % len(raw),
           "#define LZ4_TD_LEN  %du" % len(blk),
           "#define LZ4_TD_CRC  0x%08Xu   /* 解出来的 %d B 的 CRC-32 (= zlib.crc32) */" % (crc, len(raw)),
           "#define LZ4_TD_Y    %du       /* 原图里的起始行 */" % (k * ROWS),
           "/* const: 放 .rodata (实机 XIP 读 Flash) */",
           "static const uint8_t lz4_td_blk[LZ4_TD_LEN] __attribute__((aligned(4), unused)) = {"]
    for i in range(0, len(blk), 16):
        out.append("    " + ",".join("0x%02x" % b for b in blk[i:i + 16]) + ",")
    out += ["};", "#endif", ""]
    return "\n".join(out), crc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--img", default="docs/img/lyrics-playing.png")
    ap.add_argument("--out", default=os.path.join(ROOT, "fw", "link", "lz4_testdata.h"))
    ap.add_argument("--bin", help="另存 [u32 raw_len | u32 crc32 | 块] (QEMU 指令计数用)")
    a = ap.parse_args()
    k, raw, blk, mean, total = pick(a.img)
    text, crc = header(a.img, k, raw, blk, mean, total)
    with open(a.out, "w") as f:
        f.write(text)
    if a.bin:
        with open(a.bin, "wb") as f:
            f.write(struct.pack("<II", len(raw), crc) + blk)
    print("%s 第 %d 条: %d B -> %d B (帧平均 %.0f B/条), crc %08x -> %s" % (a.img, k, len(raw), len(blk), mean, crc, a.out))


if __name__ == "__main__":
    main()
