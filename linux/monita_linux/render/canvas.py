"""Pillow 画布：坐标系同浏览器 canvas（左上原点、y 向下），好和 Swift 的 Canvas.swift / LyricsPage.swift 逐行对照。

文字：按字符分段选字体（CJK / 拉丁字体缺的字形 -> CJK 字体），每段在同一条基线上画。
折行照搬 MonitaCore/LyricsCore.swift 的 wrapText（CJK 逐字断，拉丁按空格断），是纯函数，测试不需要字体。
"""
import contextlib
import math
import re

from PIL import Image, ImageChops, ImageDraw, ImageFont

from .fonts import find_fonts

_CJK = re.compile("[⺀-⿟　-〿぀-ヿ㄀-ㄯ㐀-䶿一-鿿"
                  "가-힯豈-﫿︰-﹏＀-￯\U00020000-\U0002fa1f]")


def is_cjk(ch):
    return bool(_CJK.match(ch))


class Fonts:
    """(px, bold, mono) -> 拉丁字体 + CJK 字体；字形缺失检测带缓存"""

    def __init__(self, paths=None, **kw):
        self.paths = paths or find_fonts(**kw)
        self.cache = {}
        self.has = {}

    def get(self, role, bold, px):
        k = (role, bold, px)
        if k not in self.cache:
            path, idx = self.paths[(role, bold)]
            self.cache[k] = ImageFont.truetype(path, px, index=idx)
        return self.cache[k]

    def pair(self, spec):
        px, bold, mono = spec
        px = max(1, int(round(px)))
        return self.get("mono" if mono else "sans", bold, px), self.get("cjk", bold, px)

    def latin_has(self, font, ch):
        k = (id(font), ch)
        if k not in self.has:
            if ch.isspace():
                self.has[k] = True
            else:
                miss = font.getmask("\U0010fffd")                # 缺字形时画出来的 .notdef
                m = font.getmask(ch)
                self.has[k] = (m.size, bytes(m)) != (miss.size, bytes(miss))
        return self.has[k]

    def runs(self, s, spec):
        """把字符串切成 [(片段, 字体)]"""
        latin, cjk = self.pair(spec)
        out = []
        for ch in s:
            f = cjk if is_cjk(ch) or not self.latin_has(latin, ch) else latin
            if out and out[-1][1] is f:
                out[-1][0].append(ch)
            else:
                out.append(([ch], f))
        return [("".join(c), f) for c, f in out]


def F(px, weight=400, mono=False):
    """和 Swift 的 f(px, w, mono:) 一样：weight ≥ 700 算粗体"""
    return (px, weight >= 700, mono)


class Canvas:
    def __init__(self, w, h, fonts):
        self.w, self.h, self.fonts = w, h, fonts
        self.img = Image.new("RGB", (w, h))
        self.d = ImageDraw.Draw(self.img)

    # ---- 文字 ----
    def measure(self, s, spec):
        return sum(f.getlength(t) for t, f in self.fonts.runs(s, spec))

    def text(self, s, x, y, spec, color, align="left", base="alphabetic"):
        if not s:
            return
        runs = self.fonts.runs(s, spec)
        width = sum(f.getlength(t) for t, f in runs)
        if align == "center":
            x -= width / 2
        elif align == "right":
            x -= width
        if base == "middle":                     # canvas 'middle' = em 框中线：基线 = y + (ascent - descent) / 2
            a, dsc = self.fonts.pair(spec)[0].getmetrics()
            y += (a - dsc) / 2
        for t, f in runs:
            self.d.text((x, y), t, font=f, fill=color, anchor="ls")
            x += f.getlength(t)

    def ellipsis(self, s, max_w, spec):
        if self.measure(s, spec) <= max_w:
            return s
        while len(s) > 1 and self.measure(s + "…", spec) > max_w:
            s = s[:-1]
        return s + "…"

    # ---- 形状 ----
    def fill_rect(self, x, y, w, h, c):
        x0, y0, x1, y1 = round(x), round(y), round(x + w), round(y + h)
        if x1 > x0 and y1 > y0:
            self.d.rectangle([x0, y0, x1 - 1, y1 - 1], fill=c)

    def stroke_rect(self, x, y, w, h, c, lw):
        self.d.rectangle([round(x - lw / 2), round(y - lw / 2), round(x + w + lw / 2) - 1, round(y + h + lw / 2) - 1],
                         outline=c, width=max(1, round(lw)))

    def line(self, x0, y0, x1, y1, c, lw, round_cap=True):
        self.d.line([(x0, y0), (x1, y1)], fill=c, width=max(1, round(lw)))
        if round_cap:
            r = lw / 2
            for (x, y) in ((x0, y0), (x1, y1)):
                self.d.ellipse([x - r, y - r, x + r, y + r], fill=c)

    def polyline(self, pts, c, lw):
        self.d.line(pts, fill=c, width=max(1, round(lw)), joint="curve")

    def circle(self, cx, cy, r, fill=None, stroke=None, lw=2):
        self.d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill, outline=stroke, width=max(1, round(lw)) if stroke else 0)

    def polygon(self, pts, fill=None, stroke=None, lw=1):
        w = max(1, round(lw)) if stroke else 0
        try:
            self.d.polygon(pts, fill=fill, outline=stroke, width=w)
        except TypeError:                               # Pillow < 9.0（Debian 11）：polygon 没有 width，描边改用闭合折线
            self.d.polygon(pts, fill=fill)
            if stroke:
                self.d.line(list(pts) + [pts[0]], fill=stroke, width=w, joint="curve")

    def image(self, im, x, y, w, h):
        self.img.paste(im.convert("RGB").resize((round(w), round(h))), (round(x), round(y)))

    @contextlib.contextmanager
    def clip(self, x, y, w, h):
        """块里画的东西只留 (x, y, w, h) 以内的"""
        saved = self.img.copy()
        yield
        mask = Image.new("L", self.img.size, 255)
        mask.paste(0, (round(x), round(y), round(x + w), round(y + h)))
        self.img.paste(saved, (0, 0), mask)


def arc_points(cx, cy, r, a0, a1):
    """canvas 语义的圆弧（角度增大 = 屏幕顺时针）折线近似"""
    steps = max(16, int(abs(a1 - a0) / (math.pi * 2) * 96))
    return [(cx + math.cos(a0 + (a1 - a0) * i / steps) * r, cy + math.sin(a0 + (a1 - a0) * i / steps) * r) for i in range(steps + 1)]


def to_rgb565(img):
    """RGB 图 -> RGB565 小端字节（R 在高位），和 monita_link.rgb888_to_rgb565 逐字节一致，但快两个数量级"""
    r, g, b = img.split()
    hi = ImageChops.add(r.point(lambda v: v & 0xF8), g.point(lambda v: v >> 5))
    lo = ImageChops.add(g.point(lambda v: (v & 0x1C) << 3), b.point(lambda v: v >> 3))
    return Image.merge("LA", (lo, hi)).tobytes()


# ---- 折行（LyricsCore.swift tokenize / wrapText）----
_WORD = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'’-.,!?")


def tokenize(s):
    out, i = [], 0
    while i < len(s):
        j = i
        if s[i] in _WORD:
            while j < len(s) and s[j] in _WORD:
                j += 1
            while j < len(s) and s[j].isspace():
                j += 1
        elif s[i].isspace():
            while j < len(s) and s[j].isspace():
                j += 1
        else:
            j = i + 1
        out.append(s[i:j])
        i = j
    return out


def wrap_text(measure, text, max_w, max_lines):
    lines, cur = [], ""
    for tok in tokenize(text):
        nxt = cur + tok
        if measure(nxt.rstrip()) <= max_w or not cur:
            cur = nxt
            while measure(cur.rstrip()) > max_w and len(cur) > 1:      # 单个 token 本身超宽：硬切
                k = len(cur) - 1
                while k > 1 and measure(cur[:k]) > max_w:
                    k -= 1
                lines.append(cur[:k])
                cur = cur[k:]
        else:
            lines.append(cur.rstrip())
            cur = tok.lstrip()
    lines.append(cur.rstrip())
    if len(lines) > max_lines:
        kept = lines[:max_lines]
        last = kept[-1]
        while last and measure(last + "…") > max_w:
            last = last[:-1]
        kept[-1] = last + "…"
        return kept
    return lines
