#!/usr/bin/env python3
"""从 EDK2 的 LaffStd.c (8x19 窄字形) 生成 fw/lcd/font_uefi.h, 只取 0x20..0x7E。

    python3 -I tools/gen_font.py [LaffStd.c] [font_uefi.h]

默认读 tools/edk2font/LaffStd.c, 写 fw/lcd/font_uefi.h。字体版权见 THIRD_PARTY.md。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIRST, LAST, ROWS = 0x20, 0x7E, 19

HEADER = """\
/*
 * EDK2 UEFI 8x19 窄字形 (GraphicsConsole 的 gUsStdNarrowGlyphData), 只取 0x20..0x7E。
 * 由 tools/gen_font.py 从 tools/edk2font/LaffStd.c 生成, 不要手改。
 *
 * 原文件版权与授权:
 *   Copyright (c) 2006 - 2008, Intel Corporation. All rights reserved.
 *   SPDX-License-Identifier: BSD-2-Clause-Patent
 * 授权全文见 THIRD_PARTY.md。
 */
#ifndef MONITA_FONT_UEFI_H
#define MONITA_FONT_UEFI_H
#include <stdint.h>

#define FONT_UEFI_W     8u
#define FONT_UEFI_H     19u
#define FONT_UEFI_FIRST 0x20u
#define FONT_UEFI_LAST  0x7Eu

/* font_uefi[c - 0x20][行], 每行 1 字节, bit7 = 最左像素 */
static const uint8_t font_uefi[95][19] = {
"""


def parse(text):
    glyph = {}
    for m in re.finditer(r"\{\s*0x([0-9A-Fa-f]{4}),\s*0x[0-9A-Fa-f]{2},\s*\{([^}]*)\}", text):
        cp = int(m.group(1), 16)
        rows = [int(x, 16) for x in re.findall(r"0x([0-9A-Fa-f]{2})", m.group(2))]
        if len(rows) == ROWS and FIRST <= cp <= LAST:
            if cp in glyph and glyph[cp] != rows:
                sys.exit("duplicate glyph 0x%04X" % cp)
            glyph[cp] = rows
    missing = [c for c in range(FIRST, LAST + 1) if c not in glyph]
    if missing:
        sys.exit("missing glyphs: " + " ".join("0x%02X" % c for c in missing))
    return glyph


def render(glyph):
    out = [HEADER]
    for c in range(FIRST, LAST + 1):
        rows = ", ".join("0x%02X" % b for b in glyph[c])
        name = "'\\''" if c == 0x27 else "'\\\\'" if c == 0x5C else "'%c'" % c
        out.append("    { %s },  /* %s */\n" % (rows, name))
    out.append("};\n\n#endif\n")
    return "".join(out)


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "tools/edk2font/LaffStd.c")
    dst = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "fw/lcd/font_uefi.h")
    with open(src, encoding="utf-8") as f:
        text = render(parse(f.read()))
    with open(dst, "w", encoding="utf-8") as f:
        f.write(text)
    print("wrote", dst)


if __name__ == "__main__":
    main()
