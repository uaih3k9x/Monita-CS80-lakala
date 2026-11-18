#!/usr/bin/env python3
"""开机版权页的参考渲染 (800x1280), 固件 fw/lcd/bootscreen.h 的对照: 主机单测逐像素比 (RGB565 截断后)。

    python3 -I tools/bootscreen/render_ref.py LaffStd.c out.ppm|out.png [--ver 6] [--date 2026-11-18]
        [--free 4352|-1] [--nbuf 3] [--seg ok|fail] [--link wait|ok]

默认参数 = 维护者确认过的设计稿。.ppm 不需要任何第三方库; .png 需要 Pillow (只给人看)。
字体: EDK2 UEFI 8x19 窄字形 (BSD-2-Clause-Patent, 见 THIRD_PARTY.md)。
"""
import argparse
import re

ap = argparse.ArgumentParser()
ap.add_argument("src")
ap.add_argument("out")
ap.add_argument("--ver", type=int, default=6)
ap.add_argument("--date", default="2026-11-18")
ap.add_argument("--free", type=int, default=4352, help="堆剩余 KiB, -1 = 查不到")
ap.add_argument("--nbuf", type=int, default=3)
ap.add_argument("--seg", choices=["ok", "fail"], default="ok")
ap.add_argument("--link", choices=["wait", "ok"], default="wait")
a = ap.parse_args()

glyph = {}
for m in re.finditer(r"\{\s*0x([0-9A-Fa-f]{4}),\s*0x[0-9A-Fa-f]{2},\s*\{([^}]*)\}", open(a.src).read()):
    cp = int(m.group(1), 16)
    rows = [int(x, 16) for x in re.findall(r"0x([0-9A-Fa-f]{2})", m.group(2))]
    if len(rows) == 19:
        glyph[cp] = rows

W, H, S = 800, 1280, 2           # 2 倍放大：每字 16x38
CW, CH = 8 * S, 19 * S
px = [[(0, 0, 0)] * W for _ in range(H)]


def rect(x, y, w, h, c):
    for yy in range(y, min(H, y + h)):
        row = px[yy]
        for xx in range(x, min(W, x + w)):
            row[xx] = c


def text(x, y, s, c):
    for ch in s:
        g = glyph.get(ord(ch), glyph[ord("?")])
        for r, bits in enumerate(g):
            for b in range(8):
                if bits & (0x80 >> b):
                    rect(x + b * S, y + r * S, S, S, c)
        x += CW


# 5x7 像素 logo（和固件里一样是字模表）
F = {"M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
     "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
     "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
     "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
     "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
     "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"]}
grad = [(0xff, 0xd1, 0xe0), (0xf2, 0xa7, 0xc0), (0xe0, 0x87, 0xa6), (0xc9, 0x6e, 0x8f),
        (0xa5, 0x62, 0x7e), (0x8a, 0x4f, 0x69), (0x6e, 0x3d, 0x54)]
x0, y0, p = 44, 56, 13
for i, ch in enumerate("MONITA"):
    for r in range(7):
        for c in range(5):
            if F[ch][r][c] == "1":
                bx, by = x0 + i * 6 * p + c * p, y0 + r * p
                rect(bx + 6, by + 6, p - 1, p - 1, (0x2a, 0x1a, 0x22))
                rect(bx, by, p - 1, p - 1, grad[r])
rect(44, 156, 456, 4, (0xa5, 0x62, 0x7e))
rect(44, 162, 456, 2, (0x5d, 0x7f, 0x99))
# 右上徽章
G = (0x5f, 0xd3, 0x5f)
bx, by = 610, 56
for t in range(3):
    rect(bx + t, by + t, 146 - 2 * t, 1, G); rect(bx + t, by + 106 - t, 146 - 2 * t, 1, G)
    rect(bx + t, by + t, 1, 106 - 2 * t, G); rect(bx + 145 - t, by + t, 1, 106 - 2 * t, G)
text(bx + 41, by + 12, "CS80", G)
text(bx + 9, by + 50, "R05 NATV", G)

WHITE, GRAY, DIM, OK, WAIT = (0xff, 0xff, 0xff), (0xc8, 0xc8, 0xc8), (0x7a, 0x7a, 0x7a), G, (0xe8, 0xc7, 0x6a)
FAIL = (0xff, 0x5f, 0x5f)
y = 200


def line(s, c=GRAY, tail=None, tc=None):
    global y
    text(44, y, s, c)
    if tail:
        text(44 + len(s) * CW, y, tail, tc)
    y += CH + 4


free = "?" if a.free < 0 else str(a.free)
fbname = {3: "triple", 2: "double"}.get(a.nbuf, "single")
line("Monita-CS80 firmware v%d" % a.ver, WHITE)
line("Build " + a.date, GRAY)
line("Copyright (c) 2026 uaih3k9x", GRAY)
line("PolyForm Noncommercial 1.0.0", GRAY)
y += 20
line("CPU   Cortex-A5 UIS8910DM .. ", GRAY, "OK", OK)
line("Base  EC600U R05 OpenCPU ... ", GRAY, "OK", OK)
line(("PSRAM 16 MiB  free %s KiB" % free).ljust(29), GRAY, "OK", OK)
line("Panel MIPI-DSI 800x1280 .... ", GRAY, "OK", OK)
line("FB    %s buffer x%d ..... " % (fbname, a.nbuf), GRAY, *(("OK", OK) if a.nbuf == 3 else ("WARN", WAIT)))
line("Seg   FG00 segment LCD ..... ", GRAY, *(("OK", OK) if a.seg == "ok" else ("FAIL", FAIL)))
line("Link  USB port 4 ........... ", GRAY, *(("OK", OK) if a.link == "ok" else ("WAIT", WAIT)))
y += 20
line("Unofficial firmware for", DIM)
line("retired or owned devices.", DIM)
line("Not affiliated with Lakala", DIM)
line("or Quectel. No payment.", DIM)
y += 20
line("Waiting for host_", GRAY)
text(44, H - CH - 40, "UEFI font: EDK2 (BSD-2-Clause-Patent)", (0x44, 0x44, 0x44))

if a.out.endswith(".png"):
    from PIL import Image
    im = Image.new("RGB", (W, H))
    im.putdata([c for row in px for c in row])
    im.save(a.out)
else:
    with open(a.out, "wb") as f:
        f.write(b"P6\n%d %d\n255\n" % (W, H))
        f.write(bytes(v for row in px for c in row for v in c))
