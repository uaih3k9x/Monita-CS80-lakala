#!/usr/bin/env python3
"""画一张段码屏「展示模式示意图」（SVG）：python3 tools/segpreview/seg_preview.py docs/img/segment-plan.svg

只是示意：段的位置是按功能分区摆的，不是玻璃的真实排布（真实映射见 docs/segmap.md）。
7 段掩码 bit0..6 = a..g，与固件 fw/fg00/fg00.h 的 fg_digit() 同一套。
"""
import sys

MASK = {"0": 0x3f, "1": 0x06, "2": 0x5b, "3": 0x4f, "4": 0x66, "5": 0x6d, "6": 0x7d, "7": 0x07,
        "8": 0x7f, "9": 0x6f, "-": 0x40, " ": 0x00}
ON, OFF, BG, INK = "#1d2a22", "#c9d3c4", "#dfe6d6", "#3b4a40"


def seg_paths(x, y, w, h, t):
    """7 个段的六边形（a..g），左上角 (x,y)，宽 w 高 h，笔画 t，略带斜体。"""
    sk = 0.10 * w
    g = t * 0.25                      # 段与段之间的细缝
    def P(*pts):
        return " ".join("%.1f,%.1f" % (px + sk * (1 - (py - y) / h), py) for px, py in pts)
    def hs(yy):                       # 横段：中心线 yy
        x0, x1 = x + t / 2 + g, x + w - t / 2 - g
        return P((x0, yy), (x0 + t / 2, yy - t / 2), (x1 - t / 2, yy - t / 2), (x1, yy), (x1 - t / 2, yy + t / 2), (x0 + t / 2, yy + t / 2))
    def vs(xx, y0, y1):               # 竖段：中心线 xx，从 y0 到 y1
        y0, y1 = y0 + g, y1 - g
        return P((xx, y0), (xx + t / 2, y0 + t / 2), (xx + t / 2, y1 - t / 2), (xx, y1), (xx - t / 2, y1 - t / 2), (xx - t / 2, y0 + t / 2))
    top, mid, bot = y + t / 2, y + h / 2, y + h - t / 2
    l, r = x + t / 2, x + w - t / 2
    return [hs(top), vs(r, top, mid), vs(r, mid, bot), hs(bot), vs(l, mid, bot), vs(l, top, mid), hs(mid)]


def digit(out, ch, x, y, w, h, t):
    m = MASK.get(ch, 0)
    for i, pts in enumerate(seg_paths(x, y, w, h, t)):
        out.append('<polygon points="%s" fill="%s"/>' % (pts, ON if m >> i & 1 else OFF))


def digits(out, s, x, y, w, h, t, gap, colons=()):
    for i, ch in enumerate(s):
        digit(out, ch, x + i * (w + gap), y, w, h, t)
    for k, lit in colons:  # 冒号在第 k 位之后
        cx = x + k * (w + gap) - gap / 2 + 2
        for cy in (y + h * 0.32, y + h * 0.68):
            out.append('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="%s"/>' % (cx, cy, t * 0.55, ON if lit else OFF))


def text(out, s, x, y, size=13, anchor="start", fill=INK, weight="normal"):
    out.append('<text x="%s" y="%s" font-size="%s" text-anchor="%s" fill="%s" font-weight="%s" '
               'font-family="PingFang SC, Noto Sans CJK SC, sans-serif">%s</text>' % (x, y, size, anchor, fill, weight, s))


def bars(out, x, y, n, lit, w=9, step=5):
    for i in range(n):
        hh = 8 + i * step
        out.append('<rect x="%d" y="%d" width="%d" height="%d" fill="%s"/>' % (x + i * (w + 4), y - hh, w, hh, ON if i < lit else OFF))


def main(path):
    W, H = 760, 300
    o = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">' % (W, H, W, H),
         '<rect x="0" y="0" width="%d" height="%d" rx="18" fill="#2b2f33"/>' % (W, H),
         '<rect x="16" y="16" width="%d" height="%d" rx="10" fill="%s"/>' % (W - 32, H - 32, BG)]
    # 顶行：暂不用的图标画成灭的（8G、信号、WiFi、×、元88）
    bars(o, 40, 64, 4, 0); digit(o, " ", 112, 36, 18, 30, 3.5); text(o, "G", 134, 64, 18, fill=OFF, weight="bold")
    text(o, "×", 150, 48, 16, fill=OFF, weight="bold")
    bars(o, 212, 64, 4, 0, w=7, step=4); text(o, "×", 252, 48, 16, fill=OFF, weight="bold")
    # 喇叭 + 五道声波：播放中随节奏起伏
    o.append('<polygon points="320,46 330,46 342,36 342,66 330,56 320,56" fill="%s"/>' % ON)
    for i in range(5):
        o.append('<path d="M %d %d q %d %d 0 %d" stroke="%s" stroke-width="3" fill="none"/>'
                 % (348 + i * 7, 42 - i * 2, 6 + i, 9 + i * 2, 18 + i * 4, ON if i < 3 else OFF))
    text(o, "喇叭 + 声波：播放中起伏", 320, 92, 11)
    # 电池框 + 四格 = 歌曲进度；闪电暂不用
    o.append('<rect x="560" y="38" width="112" height="30" rx="3" fill="none" stroke="%s" stroke-width="3"/>' % ON)
    o.append('<rect x="672" y="46" width="6" height="14" fill="%s"/>' % ON)
    for i in range(4):
        o.append('<rect x="%d" y="43" width="22" height="20" fill="%s"/>' % (566 + i * 26, ON if i < 1 else OFF))
    o.append('<polygon points="698,34 686,54 696,54 690,72 706,48 696,48 702,34" fill="%s"/>' % OFF)
    text(o, "电池格：歌曲进度", 560, 86, 11)
    # 主区：播放进度 mm:ss
    digits(o, "  0333", 40, 112, 46, 92, 10, 16, colons=[(2, False), (4, True)])
    text(o, "主区：播放进度 mm:ss（无歌时显示时钟）", 40, 236, 13)
    # 小时钟 88:88:88：当前时间
    digits(o, "214507", 470, 116, 26, 46, 5, 12, colons=[(2, True), (4, True)])
    text(o, "小时钟：当前时间", 470, 180, 12)
    text(o, "元", 470, 236, 26, fill=OFF, weight="bold")
    digits(o, "  ", 506, 198, 24, 42, 5, 10)
    text(o, "示意图：按功能分区摆放，非玻璃真实排布", W / 2, H - 26, 12, anchor="middle", fill="#6b776e")
    o.append("</svg>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(o) + "\n")
    print("wrote", path)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "segment-plan.svg")
