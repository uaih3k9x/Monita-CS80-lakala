"""段码屏展示帧（照 mac/Sources/MonitaCore/SegFrame.swift；映射见 docs/segmap.md / fw/fg00/segmap.h）。

  播放中：主区 = 进度 mm:ss（≥1 h 为 h:mm:ss），小时钟 = 总时长，喇叭 + 声波每 250 ms 跳一步
  暂停：  同上，但主区 1 Hz 闪烁、声波全灭
  没在播：主区 = HH:MM（冒号 500 ms 半周期闪），小时钟 = MM-DD
  电池：  25% 一格四舍五入，>0% 至少 1 格；闪电 = 接着交流电；没有电池整块不亮
"""
import time

import monita_link as ml

from .schedule import position_at

# 单段：(显存字节号, 位掩码)，与 segmap.h 的 FG_SEG_* 一致
MAIN_COLON2, MAIN_COLON4 = (55, 0x08), (57, 0x08)
SPEAKER = (43, 0x08)
VOLUMES = [(61, 0x08), (61, 0x02), (43, 0x02), (7, 0x02), (25, 0x02)]
BAT_FRAME, CHARGE = (70, 0x80), (52, 0x80)
CELLS = [(34, 0x80), (16, 0x80), (16, 0x40), (70, 0x40)]     # 点亮顺序：从负极端（最右的 D）开始
COLON_COLS = {1, 3, 9, 11}                                   # 冒号在该列之后（槽位 fb[54+k] bit3）
WAVE = [2, 4, 5, 3, 5, 4, 2, 3]


def clock_text(s):
    t = max(0, int(s))
    h, m, ss = t // 3600, t % 3600 // 60, t % 60
    return "%d:%02d:%02d" % (h, m, ss) if h else "%02d:%02d" % (m, ss)


def battery_cells(percent):
    if percent <= 0:
        return 0
    return min(4, max(1, int(percent / 25 + 0.5)))


def _put(fb, cols, text):
    """在 cols 这几列里右对齐写 text（数字、'-'、' '）；':' 点亮前一位之后的冒号"""
    masks, colon_after = [], []
    for ch in text:
        if ch == ":":
            colon_after.append(len(masks) - 1)
            continue
        masks.append(ml.SEG7[int(ch)] if ch.isdigit() else (0x40 if ch == "-" else 0))
    base = cols.stop - len(masks)
    for i, m in enumerate(masks):
        if base + i in cols:
            ml.seg_digit(fb, base + i, m)
    for i in colon_after:
        if base + i in COLON_COLS and base + i in cols:
            fb[54 + base + i] |= 0x08


def _set(fb, bit):
    fb[bit[0]] |= bit[1]


def seg_frame(play, now, battery=None, local=None):
    """play：lyrics.snapshot 形状的 dict（state / pos / at / rate / duration），None = 没在播。
    battery：(percent, on_ac) 或 None。local：(时, 分, 月, 日)，默认取 now 的本地时间"""
    fb = bytearray(72)
    if battery is not None:
        _set(fb, BAT_FRAME)
        for c in CELLS[:battery_cells(battery[0])]:
            _set(fb, c)
        if battery[1]:
            _set(fb, CHARGE)
    half = int(now * 1000 // 500) % 2 == 0
    if play is None:
        if local is None:
            t = time.localtime(now)
            local = (t.tm_hour, t.tm_min, t.tm_mon, t.tm_mday)
        hh, mm, mo, dd = local
        _put(fb, range(0, 6), "%02d%s%02d" % (hh, ":" if half else "", mm))
        _put(fb, range(8, 14), "%02d-%02d" % (mo, dd))
        return bytes(fb)
    playing = play.get("state") == "playing"
    if playing or half:
        _put(fb, range(0, 6), clock_text(position_at(play, now)))
    if play.get("duration"):
        _put(fb, range(8, 14), clock_text(play["duration"]))
    _set(fb, SPEAKER)
    if playing:
        for v in VOLUMES[:WAVE[int(now * 1000 // 250) % len(WAVE)]]:
            _set(fb, v)
    return bytes(fb)
