"""纯时钟页（≡ 短按在歌词页 / 时钟页之间切换；照 mac/Sources/monita-cs80/ClockPage.swift）。整分钟才变。"""
import time

from . import palette as C
from .canvas import Canvas, F
from .lyrics_page import MONS

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def draw_clock(page, inp):
    """page：LyricsPage（借它的窗口框 / Front Panel）；inp 同 LyricsPage.draw"""
    inp = dict(inp)
    t = inp.setdefault("local", time.localtime(inp["now"]))
    page.cv = cv = Canvas(page.W, page.H, page.fonts)
    w, h = page.W, page.H
    cv.fill_rect(0, 0, w, h, C.DESK)
    wx, wy, ww, wh = page.window_frame(12, 12, w - 24, 1012, "Clock", False)
    mid = wx + ww / 2
    fx, fw, fy, fh = wx + 40, ww - 80, wy + 150, 420.0
    page.box(fx, fy, fw, fh, C.WELL, True, 2)
    cv.text("%02d:%02d" % (t.tm_hour, t.tm_min), mid, fy + fh / 2 + 6, F(210, 700, mono=True), C.FG, "center", "middle")
    cv.text(WEEKDAYS[t.tm_wday], mid, fy + fh + 90, F(48, 700), C.FG, "center", "middle")
    cv.text("%s %d, %d" % (MONS[t.tm_mon - 1], t.tm_mday, t.tm_year), mid, fy + fh + 160, F(36), C.DIM, "center", "middle")
    by = wy + wh - 50
    page.box(wx + 12, by, ww - 24, 38, C.WIN, True, 1)
    cv.text("≡  Lyrics", wx + 24, by + 20, F(17, mono=True), C.DIM, "left", "middle")
    page.front_panel(12, 1040, w - 24, h - 1040 - 12, inp)
    return cv.img
