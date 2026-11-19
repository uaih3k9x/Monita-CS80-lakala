"""CDE 风格歌词页 + Front Panel（逐段照 mac/Sources/monita-cs80/LyricsPage.swift，不追求像素一致）。

和 Mac 版的差别：
- Front Panel 中间四格换成 CPU / BAT / Player / Load（Mac 版那三格是作者私人数据）；拿不到显示 "–"；
- Lock 按钮固定 "Lock"（不做防睡眠断言）；
- 不做 450 ms 滚动动画（换句直接差分推）。
"""
import math
import time

from . import palette as C
from .canvas import Canvas, F, arc_points, wrap_text
from ..schedule import LYRIC_LEAD_S, is_playing, line_index, position_at, progress_fill

MONS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
WDS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]          # time.struct_time.tm_wday：周一 = 0
DASH = "–"


def fmt_time(s):
    if s is None:
        return "-:--"
    t = int(s)
    h, m, ss = t // 3600, t % 3600 // 60, t % 60
    return "%d:%02d:%02d" % (h, m, ss) if h else "%d:%02d" % (m, ss)


def panel_cells(panel):
    """Front Panel 中间四格：[(大字, 标签)]"""
    p = panel or {}
    cpu, bat, player, load = p.get("cpu"), p.get("bat"), p.get("player"), p.get("load")
    return [("%d%%" % cpu if cpu is not None else DASH, "CPU"),
            ("%d%%" % bat if bat is not None else DASH, "BAT"),
            (player or DASH, "Player"),
            ("%.2f" % load if load is not None else DASH, "Load")]


class LyricsPage:
    W, H = 800, 1280

    def __init__(self, fonts, title="Lyrics — NetEase Cloud Music", translation=True):
        self.fonts, self.title, self.translation = fonts, title, translation
        self.progress_inner_w = 452.0
        self.cv = None

    # ---- Motif 原语 ----
    @staticmethod
    def shades(c):
        mix = lambda t, k: tuple(int(round(v + (t - v) * k)) for v in c)   # noqa: E731
        return mix(255, 0.55), mix(0, 0.45)

    def bevel(self, x, y, w, h, base, sunken=False, width=2):
        hi, lo = self.shades(base)
        tl, br = (lo, hi) if sunken else (hi, lo)
        cv = self.cv
        for d in range(width):
            cv.fill_rect(x + d, y + d, w - d * 2 - 1, 1, tl)
            cv.fill_rect(x + d, y + d, 1, h - d * 2 - 1, tl)
            cv.fill_rect(x + d, y + h - 1 - d, w - d * 2, 1, br)
            cv.fill_rect(x + w - 1 - d, y + d, 1, h - d * 2, br)

    def box(self, x, y, w, h, base, sunken=False, width=2):
        self.cv.fill_rect(x, y, w, h, base)
        self.bevel(x, y, w, h, base, sunken, width)

    def _menu_item(self, label, x, y):
        cv = self.cv
        cv.text(label, x, y, F(22), C.FG)
        cv.fill_rect(x, y + 3, cv.measure(label[0], F(22)), 2, C.FG)
        return x + cv.measure(label, F(22)) + 30

    # ---- 窗口框（dtwm）----
    def window_frame(self, x, y, w, h, title, paused):
        cv, B, TH = self.cv, 8.0, 44.0
        self.box(x, y, w, h, C.TITLE, False, 2)
        hi, lo = self.shades(C.TITLE)
        corner = 34.0
        marks = [(x + corner, y, False), (x + w - corner, y, False), (x + corner, y + h - B, False), (x + w - corner, y + h - B, False),
                 (x, y + corner, True), (x + w - B, y + corner, True), (x, y + h - corner, True), (x + w - B, y + h - corner, True)]
        for cx, cy, horiz in marks:
            if horiz:
                cv.fill_rect(cx + 2, cy, B - 4, 1, lo); cv.fill_rect(cx + 2, cy + 1, B - 4, 1, hi)
            else:
                cv.fill_rect(cx, cy + 2, 1, B - 4, lo); cv.fill_rect(cx + 1, cy + 2, 1, B - 4, hi)
        self.bevel(x + B - 2, y + B - 2, w - (B - 2) * 2, h - (B - 2) * 2, C.TITLE, True, 2)
        tx, ty, tw = x + B, y + B, w - B * 2
        self.box(tx, ty, TH, TH, C.TITLE)
        self.box(tx + 10, ty + TH / 2 - 4, TH - 20, 8, C.TITLE)
        max_x = tx + tw - TH
        min_x = max_x - TH
        self.box(min_x, ty, TH, TH, C.TITLE)
        self.box(min_x + TH / 2 - 4, ty + TH / 2 - 4, 8, 8, C.TITLE)
        self.box(max_x, ty, TH, TH, C.TITLE)
        self.box(max_x + 9, ty + 9, TH - 18, TH - 18, C.TITLE)
        self.box(tx + TH, ty, min_x - tx - TH, TH, C.TITLE)
        label = cv.ellipsis(title + ("  [Paused]" if paused else ""), min_x - tx - TH - 24, F(22, 700))
        cv.text(label, tx + TH + (min_x - tx - TH) / 2, ty + TH / 2 + 1, F(22, 700), C.TITLE_FG, "center", "middle")
        cx, cy, cw, ch = tx, ty + TH, tw, h - B * 2 - TH
        self.box(cx, cy, cw, ch, C.WIN, False, 1)
        return (cx, cy, cw, ch)

    # ---- 图标 ----
    def audio_icon(self, x, y, size):
        cv, k = self.cv, size / 100.0
        P = lambda pts: [(x + px * k, y + py * k) for px, py in pts]   # noqa: E731
        doc = P([(18, 6), (66, 6), (84, 24), (84, 94), (18, 94)])
        cv.polygon(doc, fill=(0xE6, 0xE6, 0xE6), stroke=C.FG, lw=2 * k)
        cv.polygon(P([(66, 6), (66, 24), (84, 24)]), fill=(0x9C, 0x9C, 0x9C), stroke=C.FG, lw=2 * k)
        for ex, ey in ((39.0, 74.0), (63.0, 68.0)):                     # 音符头：转 -0.4 rad 的椭圆
            pts = [(ex + 9 * math.cos(a) * math.cos(-0.4) - 7 * math.sin(a) * math.sin(-0.4),
                    ey + 9 * math.cos(a) * math.sin(-0.4) + 7 * math.sin(a) * math.cos(-0.4))
                   for a in (i / 24 * 2 * math.pi for i in range(24))]
            cv.polygon(P(pts), fill=C.ICON_BLUE)
        cv.polygon(P([(46, 34), (50, 34), (50, 74), (46, 74)]), fill=C.ICON_BLUE)
        cv.polygon(P([(70, 28), (74, 28), (74, 68), (70, 68)]), fill=C.ICON_BLUE)
        cv.polygon(P([(46, 34), (74, 28), (74, 38), (46, 44)]), fill=C.ICON_BLUE)

    def info_icon(self, x, y, r, glyph):
        self.cv.circle(x, y, r, fill=C.WHITE, stroke=C.FG, lw=2)
        self.cv.text(glyph, x, y + 2, F(r * 1.3, 700, mono=glyph == "i"), C.ICON_BLUE, "center", "middle")

    # ---- Front Panel ----
    def front_panel(self, x, y, w, h, inp):
        cv, pad = self.cv, 10.0
        self.box(x, y, w, h, C.PANEL, False, 2)

        def slot(sx, sw):
            self.box(sx, y + pad, sw, h - pad * 2, C.PANEL, False, 2)
            return (sx, y + pad, sw, h - pad * 2)

        t = inp["local"]
        hr, mi = t.tm_hour, t.tm_min
        # 时钟（分钟级）
        rx, ry, rw, rh = slot(x + pad, 140)
        cx, cy = rx + rw / 2, ry + rh / 2 - 8
        rad = min(rw, rh) / 2 - 22
        cv.circle(cx, cy, rad, fill=(0xE9, 0xE9, 0xEF), stroke=C.FG, lw=2)
        for i in range(12):
            a = i / 12 * math.pi * 2
            cv.fill_rect(cx + math.sin(a) * (rad - 7) - 1.5, cy - math.cos(a) * (rad - 7) - 1.5, 3, 3, C.FG)
        for ang, ln, wd in (((hr % 12 + mi / 60) / 12 * math.pi * 2, rad * 0.55, 4), (mi / 60 * math.pi * 2, rad * 0.85, 3)):
            cv.line(cx, cy, cx + math.sin(ang) * ln, cy - math.cos(ang) * ln, C.FG, wd)
        cv.text("%02d:%02d" % (hr, mi), cx, ry + rh - 10, F(18, 700, mono=True), C.FG, "center")

        # 日历
        rx, ry, rw, rh = slot(rx + rw + 6, 120)
        self.box(rx + 18, ry + 22, rw - 36, rh - 56, C.WHITE, True, 2)
        cv.fill_rect(rx + 20, ry + 24, rw - 40, 26, C.TITLE)
        cv.text(MONS[t.tm_mon - 1], rx + rw / 2, ry + 38, F(17, 700), C.WHITE, "center", "middle")
        cv.text(str(t.tm_mday), rx + rw / 2, ry + rh - 46, F(44, 700), C.FG, "center")
        cv.text(WDS[t.tm_wday], rx + rw / 2, ry + rh - 12, F(16), C.FG, "center")

        # 中间四格
        rx, ry, rw, rh = slot(rx + rw + 6, 250)
        bw, bh = (rw - 30) / 2, (rh - 30) / 2
        for i, (big, label) in enumerate(panel_cells(inp.get("panel"))):
            bx = rx + 10 + (i % 2) * (bw + 10)
            by = ry + 10 + (i // 2) * (bh + 10)
            self.box(bx, by, bw, bh, C.PANEL, False, 2)
            size = 34
            while size > 16 and cv.measure(big, F(size, 700)) > bw - 12:
                size -= 2
            cv.text(cv.ellipsis(big, bw - 12, F(size, 700)), bx + bw / 2, by + bh / 2 - 8, F(size, 700), C.FG, "center", "middle")
            cv.text(label, bx + bw / 2, by + bh - 14, F(15), C.FG, "center")

        # 右侧：Lock + Lyrics（当前页）
        rest = x + w - pad - (rx + rw + 6)
        rx, ry, rw, rh = slot(rx + rw + 6, rest)
        half = (rw - 30) / 2
        lx, ly, lh = rx + 10, ry + 10, rh - 20
        self.box(lx, ly, half, lh, C.PANEL, False, 2)
        self._lock(lx, ly, half, lh)
        ax = lx + half + 10
        self.box(ax, ly, half, lh, C.ON, True, 2)
        self.audio_icon(ax + half / 2 - 34, ly + 14, 68)
        cv.text("Lyrics", ax + half / 2, ly + rh - 34, F(16, 700), C.FG, "center")

    def _lock(self, lx, ly, half, lh):
        """挂锁（锁扣抬起）+ 固定 "Lock"：Linux 版不看防睡眠断言"""
        cv, mx = self.cv, lx + half / 2
        cv.polyline(arc_points(mx, ly + 34, 16, math.pi, 2 * math.pi) + [(mx + 16, ly + 38)], C.FG, 4)
        cv.line(mx - 16, ly + 34, mx - 16, ly + 46, C.FG, 4, round_cap=False)
        cv.fill_rect(mx - 24, ly + 46, 48, 38, C.LOCK_GOLD)
        cv.stroke_rect(mx - 24, ly + 46, 48, 38, C.FG, 2)
        cv.text("Lock", mx, ly + lh - 14, F(16), C.FG, "center")

    # ---- 歌词区 ----
    def _fit_line(self, s, x, y, max_w, px, color):
        cv, size = self.cv, px
        while size > 24 and cv.measure(s, F(size)) > max_w:
            size -= 1
        cv.text(cv.ellipsis(s, max_w, F(size)), x, y, F(size), color, "center")

    def _message(self, r, glyph, big, small):
        x, y, w, h = r
        cx = x + w / 2
        self.info_icon(cx, y + h / 2 - 90, 34, glyph)
        self.cv.text(big, cx, y + h / 2 + 10, F(36, 700), C.FG, "center", "middle")
        if small:
            self.cv.text(small, cx, y + h / 2 + 60, F(22), C.DIM, "center", "middle")

    def lyric_area(self, r, inp):
        cv = self.cv
        x, y, w, h = r
        self.box(x, y, w, h, C.WELL, True, 2)
        d = inp.get("data")
        if not is_playing(d):
            e = (d or {}).get("error")
            return self._message(r, "i", "未在播放", ("数据源：" + str(e)[:40]) if e else "No media is playing")
        lyr = d.get("lyric") or {}
        st, lines = lyr.get("status"), lyr.get("lines") or []
        if st == "pure":
            return self._message(r, "♪", "纯音乐，请欣赏", "Instrumental")
        if st == "loading":
            return self._message(r, "i", "正在查找歌词…", "Searching music.163.com")
        if st != "ok" or not lines:
            return self._message(r, "?", "找不到歌词", "网易云接口暂时连不上，稍后重试" if st == "error" else "No synced lyrics for this track")

        pos = position_at(d, inp["now"])
        idx = line_index([l["t"] for l in lines], pos + LYRIC_LEAD_S)
        at = lambda i: lines[i] if 0 <= i < len(lines) else None       # noqa: E731
        gap = lambda l: (l["x"] or "· · ·") if l else ""                # noqa: E731
        cur = at(idx)
        pad = 34.0
        max_w = w - pad * 2
        cx = x + w / 2
        cur_f = F(50, 700)
        cur_lines = wrap_text(lambda s: cv.measure(s, cur_f), gap(cur), max_w - 24, 2) if cur else ["♪" if idx < 0 else ""]
        tr = cur.get("tr") if cur and self.translation else ""
        tr_lines = wrap_text(lambda s: cv.measure(s, F(28)), tr, max_w - 24, 2) if tr else []
        block_h = 36 + len(cur_lines) * 62 + len(tr_lines) * 36 + (8 if tr_lines else 0)
        mid_y = y + round(h * 0.42)
        by = round(mid_y - block_h / 2)
        with cv.clip(x + 3, y + 3, w - 6, h - 6):
            for k in (1, 2, 3):                                         # 上面最多三句、下面四句，区域外的裁掉
                if at(idx - k):
                    self._fit_line(gap(at(idx - k)), cx, by - 30 - (k - 1) * 58, max_w, 32, C.FAINT)
            sel = C.SELECT if d.get("state") == "playing" else C.TROUGH
            cv.fill_rect(x + pad - 12, by, w - (pad - 12) * 2, block_h, sel)
            self.bevel(x + pad - 12, by, w - (pad - 12) * 2, block_h, sel, False, 2)
            ty = by + 18 + 50
            for s in cur_lines:
                cv.text(s, cx, ty, cur_f, C.SELECT_FG, "center")
                ty += 62
            ty -= 8
            for s in tr_lines:
                cv.text(s, cx, ty, F(28), C.TR_FG, "center")
                ty += 36
            ny = by + block_h + 56
            for k in (1, 2, 3, 4):
                if not at(idx + k):
                    break
                self._fit_line(gap(at(idx + k)), cx, ny, max_w, 32, C.DIM)
                ny += 58

    def progress(self, r, d, now):
        cv = self.cv
        x, y, w, h = r
        pos = position_at(d, now) if d else None
        dur = d.get("duration") if d else None
        tw = 130.0
        cv.text(fmt_time(pos), x, y + h / 2 + 1, F(24, 700, mono=True), C.FG, "left", "middle")
        cv.text(fmt_time(dur), x + w, y + h / 2 + 1, F(24, 700, mono=True), C.FG, "right", "middle")
        bx, bw = x + tw, w - tw * 2
        self.box(bx, y, bw, h, C.TROUGH, True, 2)
        self.progress_inner_w = bw - 8
        if pos is not None and dur:
            fw = progress_fill(pos, dur, bw - 8)
            if fw > 0:
                cv.fill_rect(bx + 4, y + 4, fw, h - 8, C.FILL)
                self.bevel(bx + 4, y + 4, max(fw, 4), h - 8, C.FILL, False, 2)

    # ---- 整页 ----
    def draw(self, inp):
        """inp: data（lyrics payload 或 None）、cover（PIL 图或 None）、panel、now（秒）、local（struct_time，可省）"""
        inp = dict(inp)
        inp.setdefault("local", time.localtime(inp["now"]))
        self.cv = cv = Canvas(self.W, self.H, self.fonts)
        w, h = self.W, self.H
        cv.fill_rect(0, 0, w, h, C.DESK)
        d = inp.get("data")
        playing = is_playing(d)
        wx, wy, ww, wh = self.window_frame(12, 12, w - 24, 1012, self.title, playing and d.get("state") == "paused")
        mx, my = wx + 20, wy + 30
        for m in ("File", "Edit", "View", "Options"):
            mx = self._menu_item(m, mx, my)
        self._menu_item("Help", wx + ww - 70, my)
        hi, lo = self.shades(C.WIN)
        cv.fill_rect(wx + 4, wy + 44, ww - 8, 1, lo)
        cv.fill_rect(wx + 4, wy + 45, ww - 8, 1, hi)

        # 封面 + 歌名歌手
        top, cs = wy + 62, 220.0
        self.box(wx + 20, top, cs + 12, cs + 12, C.WIN, True, 2)
        if playing and inp.get("cover") is not None:
            cv.image(inp["cover"], wx + 26, top + 6, cs, cs)
        else:
            cv.fill_rect(wx + 24, top + 4, cs + 4, cs + 4, C.WELL)
            self.audio_icon(wx + 26 + cs / 2 - 70, top + 6 + cs / 2 - 70, 140)
        ix = wx + 20 + cs + 12 + 26
        iw = wx + ww - 20 - ix
        field = lambda s, fy: cv.text(s, ix, fy, F(18, 700), C.DIM)   # noqa: E731
        field("Title:", top + 22)
        if playing:
            title, artist, album = d.get("title") or "", d.get("artist") or "", d.get("album") or ""
            one = cv.measure(title, F(38, 700)) <= iw
            tf = F(38, 700) if one else F(30, 700)
            tl = [title] if one else wrap_text(lambda s: cv.measure(s, tf), title, iw, 2)
            for i, s in enumerate(tl):
                cv.text(s, ix, top + (66 if one else 60) + i * 36, tf, C.FG)
            ay = top + (66 if one else 60 + 36) + 30
            field("Artist:", ay)
            cv.text(cv.ellipsis(artist or "—", iw, F(26)), ix, ay + 34, F(26), C.FG)
            if one and album and album != title:
                cv.text(cv.ellipsis(album, iw, F(20)), ix, ay + 64, F(20), C.DIM)
        else:
            cv.text("—", ix, top + 66, F(38, 700), C.DIM)
            field("Artist:", top + 126)
            cv.text("—", ix, top + 160, F(26), C.DIM)
        # 状态行
        sy = top + cs + 12 - 26
        is_play = playing and d.get("state") == "playing"
        self.box(ix, sy - 9, 18, 18, C.PLAY_GREEN if is_play else C.WIN, True, 2)
        cv.text(("Playing" if is_play else "Paused") if playing else "Idle", ix + 28, sy + 1, F(20), C.FG, "left", "middle")
        if playing and d.get("estimated"):
            self.box(ix + 150, sy - 16, 176, 32, C.WARN, False, 2)
            cv.text("进度为估算", ix + 238, sy + 1, F(19, 700), C.FG, "center", "middle")

        lr = (wx + 20, top + cs + 34, ww - 40, 520)
        self.lyric_area(lr, inp)
        self.progress((wx + 20, lr[1] + lr[3] + 24, ww - 40, 34), d if playing else None, inp["now"])

        # 底部消息栏
        fy = wy + wh - 50
        self.box(wx + 12, fy, ww - 24, 38, C.WIN, True, 1)
        parts = ["No player" if not playing else ("MPRIS" if d.get("source") == "mpris" else "estimate")]
        if playing:
            app = d.get("app") or ""
            if app:
                parts.append(app[4:] if app.startswith("com.") else app)
            if d.get("songId"):
                parts.append("id %s" % d["songId"])
            if (d.get("lyric") or {}).get("translated"):
                parts.append("tr")
        fm = F(17, mono=True)
        cv.text(cv.ellipsis("  ·  ".join(parts), ww - 60, fm), wx + 24, fy + 20, fm, C.DIM, "left", "middle")

        self.front_panel(12, 1040, w - 24, h - 1040 - 12, inp)
        return cv.img
