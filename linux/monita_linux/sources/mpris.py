"""MPRIS（会话总线）→ mediaremote-adapter 形状的 raw dict，交给 tools/lyrics/lyrics.py 现成的 snapshot / 歌词逻辑（DESIGN.md §3.5）。

选播放器：去掉 playerctld（转发代理）；--player 按总线名后缀前缀匹配（浏览器 / VLC 带实例号）；
否则 Playing 的优先（多个都在放时留在上一次选中的，不来回跳）；都没在放时选最近在放的那个，它没了就任意一个 Paused 的。
Position 读不到 / 播放中一直是 0：从"第一次看到这首"起外推，并标 estimated。
"""
import time

import lyrics as L

from ..system.dbus import BusError

PREFIX = "org.mpris.MediaPlayer2."
PATH = "/org/mpris/MediaPlayer2"
PLAYER = "org.mpris.MediaPlayer2.Player"
STUCK_ZERO_S = 2.0


class MprisSource:
    def __init__(self, bus, prefer=None, clock=time.time):
        self.bus, self.prefer, self.clock = bus, prefer, clock
        self.current = None             # 屏上显示的那个（PlayPause 发给它）
        self.last_playing = None
        self.estimated = False
        self.art_url = None
        self.track = None               # (播放器, trackid / 标题)
        self.first_seen = 0.0
        self.est_track = None

    def _players(self):
        try:
            names = self.bus.list_names()
        except BusError:
            return {}
        out = {}
        for n in sorted(names):
            if not n.startswith(PREFIX) or n[len(PREFIX):].split(".")[0] == "playerctld":
                continue
            try:
                out[n] = self.bus.get(n, PATH, PLAYER, "PlaybackStatus")
            except BusError:
                continue                # 卡住 / 报错的当它不存在
        return out

    def _choose(self, st):
        if self.prefer:
            hit = [n for n in st if n[len(PREFIX):].startswith(self.prefer)]
            if hit:
                playing = [n for n in hit if st[n] == "Playing"]
                return playing[0] if playing else hit[0]
        playing = [n for n in st if st[n] == "Playing"]
        if playing:
            return self.current if self.current in playing else playing[0]
        if self.last_playing in st and st[self.last_playing] != "Stopped":
            return self.last_playing
        paused = [n for n in st if st[n] == "Paused"]
        return paused[0] if paused else None

    def get(self):
        st = self._players()
        name = self._choose(st)
        self.current = name
        if name is None:
            return None
        if st[name] == "Playing":
            self.last_playing = name
        try:
            props = self.bus.get_all(name, PATH, PLAYER)
        except BusError:
            return None
        md = props.get("Metadata") or {}
        title = md.get("xesam:title")
        if not title or props.get("PlaybackStatus", st[name]) == "Stopped":
            return None
        now = self.clock()
        playing = st[name] == "Playing"
        track = (name, md.get("mpris:trackid") or title)
        if track != self.track:
            self.track, self.first_seen = track, now
        pos = props.get("Position")
        if pos is None:
            try:
                pos = self.bus.get(name, PATH, PLAYER, "Position")
            except BusError:
                pos = None
        if pos is None or (playing and pos == 0 and now - self.first_seen >= STUCK_ZERO_S):
            self.est_track = track
        self.estimated = self.est_track == track
        artist = md.get("xesam:artist")
        raw = dict(title=title, artist="/".join(artist) if isinstance(artist, (list, tuple)) else (artist or ""),
                   album=md.get("xesam:album") or "", playing=playing, playbackRate=props.get("Rate", 1.0),
                   bundleIdentifier=name[len(PREFIX):])
        if self.estimated:
            raw.update(elapsedTimeMicros=0, timestampEpochMicros=int(self.first_seen * 1_000_000))
        else:
            raw.update(elapsedTimeMicros=int(pos), timestampEpochMicros=int(now * 1_000_000))
        if md.get("mpris:length"):
            raw["durationMicros"] = int(md["mpris:length"])
        self.art_url = md.get("mpris:artUrl")
        return raw

    def play_pause(self):
        if self.current:
            try:
                self.bus.call(self.current, PATH, PLAYER, "PlayPause")
            except BusError:
                pass


class MprisLyrics(L.Lyrics):
    """lyrics.Lyrics 接 MPRIS：payload() 原样复用，只改两处——
    没有播放器时不去翻网易云 Mac 客户端的缓存（_fallback）；source / estimated 按 MPRIS 的实际情况填"""

    def __init__(self, source):
        super().__init__(np=source)
        self.source = source

    def _fallback(self):
        return {"state": "stopped", "source": "none"}

    def payload(self):
        p = super().payload()
        if p.get("state") != "stopped":
            p["source"] = "mpris"
            p["estimated"] = self.source.estimated
        return p
