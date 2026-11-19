"""进度插值和帧键（照 MonitaCore/LyricsCore.swift positionAt / Schedule.swift lyricsFrameKey）。

主循环每个节拍算一次帧键，键没变就不画、不转 RGB565、不做差分（暂停 / 时钟页时只有整分钟才会变）。
"""
import bisect

LYRIC_LEAD_S = 0.15                 # 换行提前量：推帧 + 上屏的延迟


def position_at(play, now):
    """play = lyrics.snapshot 形状（pos 秒，at 毫秒）；now 秒。按 rate 外推，夹在 [0, duration]"""
    if not play or play.get("pos") is None:
        return 0.0
    rate = (play.get("rate") or 1.0) if play.get("state") == "playing" else 0.0
    at = play.get("at")
    p = play["pos"] + ((now * 1000 - at) / 1000.0 if at is not None else 0.0) * rate
    p = max(0.0, p)
    dur = play.get("duration")
    if dur and p > dur:
        p = dur
    return p


def line_index(times, pos):
    """最后一个 t <= pos 的下标；前奏返回 -1"""
    return bisect.bisect_right(times, pos) - 1


def progress_fill(pos, dur, width):
    return round(width * min(1.0, pos / dur))


def is_playing(d):
    return bool(d) and d.get("state") not in (None, "", "stopped") and not d.get("error")


def frame_key(page, d, now, progress_w, panel=(), cover=None):
    """画面真正依赖的量：页、数据（去掉时间戳）、显示的秒、当前歌词行、进度条像素、墙上分钟、Front Panel、封面有没有"""
    minute = int(now // 60)
    if page == "clock":
        return (page, minute, tuple(panel))
    if not is_playing(d):
        return (page, (d or {}).get("error"), minute, tuple(panel))
    pos = position_at(d, now)
    lines = (d.get("lyric") or {}).get("lines") or []
    line = line_index([l["t"] for l in lines], pos + LYRIC_LEAD_S)
    dur = d.get("duration") or 0
    fill = progress_fill(pos, dur, progress_w) if dur > 0 else 0
    stable = {k: v for k, v in d.items() if k not in ("pos", "at", "now", "cover")}
    return (page, repr(sorted(stable.items())), int(pos), line, fill, minute, tuple(panel), cover is not None)
