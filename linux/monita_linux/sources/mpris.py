"""MPRIS（会话总线）→ mediaremote-adapter 形状的 raw dict，交给 tools/lyrics/lyrics.py 现成的 snapshot / 歌词逻辑（DESIGN.md §3.5）。

选播放器：去掉 playerctld（转发代理）；--player 按总线名后缀前缀匹配（浏览器 / VLC 带实例号）；
否则 Playing 的优先（多个都在放时留在上一次选中的，不来回跳）；都没在放时选最近在放的那个，它没了就任意一个 Paused 的。
Position 读不到 / 播放中一直是 0：改用"这首歌处于 Playing 的累计时长"当进度（暂停不计），并标 estimated；
之后一旦读到非 0 的 Position 就改回真值。认歌用 (播放器, trackid, 标题, 歌手)：Chromium 的 trackid 是常量，NoTrack 也是。
PlaybackStatus 都读不到（卡住 / 报错）的播放器 30 s 内不再问（每问一次要卡主循环 0.5 s）。
"""
import time

import lyrics as L

from ..system.dbus import BusError

PREFIX = "org.mpris.MediaPlayer2."
PATH = "/org/mpris/MediaPlayer2"
PLAYER = "org.mpris.MediaPlayer2.Player"
STUCK_ZERO_S = 2.0
SKIP_BROKEN_S = 30.0


class MprisSource:
    def __init__(self, bus, prefer=None, clock=time.time):
        self.bus, self.prefer, self.clock = bus, prefer, clock
        self.current = None             # 屏上显示的那个（PlayPause 发给它）
        self.last_playing = None
        self.estimated = False
        self.art_url = None
        self.track = None               # (播放器, trackid, 标题, 歌手)
        self.played = 0.0               # 这首歌 Playing 的累计秒数（估算模式的进度）
        self.tick = None                # 上一次 get() 的 (时刻, 是否 Playing, rate)
        self.zero_since = None          # Position 从什么时候起一直是 0
        self.broken = {}                # 总线名 -> 到这个时刻之前不再问

    def _players(self):
        try:
            names = self.bus.list_names()
        except BusError:
            return {}
        out = {}
        now = self.clock()
        for n in sorted(names):
            if not n.startswith(PREFIX) or n[len(PREFIX):].split(".")[0] == "playerctld":
                continue
            if now < self.broken.get(n, 0.0):
                continue
            try:
                out[n] = self.bus.get(n, PATH, PLAYER, "PlaybackStatus")
                self.broken.pop(n, None)
            except BusError:
                self.broken[n] = now + SKIP_BROKEN_S     # 卡住 / 报错的当它不存在，一阵子不再问
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
        artist = md.get("xesam:artist")
        artist = "/".join(artist) if isinstance(artist, (list, tuple)) else (artist or "")
        rate = props.get("Rate", 1.0)
        track = (name, md.get("mpris:trackid"), title, artist)
        if track != self.track:
            self.track, self.played, self.tick = track, 0.0, None
            self.estimated, self.zero_since = False, None
        elif self.tick and self.tick[1]:
            self.played += (now - self.tick[0]) * (self.tick[2] or 0)
        self.tick = (now, playing, rate)
        pos = props.get("Position")
        if pos is None:
            try:
                pos = self.bus.get(name, PATH, PLAYER, "Position")
            except BusError:
                pos = None
        if pos is None:
            self.estimated = True
        elif pos != 0:
            self.estimated, self.zero_since = False, None       # 读到真值了（缓冲完 / 拖了进度）：改回真值
        else:
            self.zero_since = now if self.zero_since is None else self.zero_since
            if playing and now - self.zero_since >= STUCK_ZERO_S:
                self.estimated = True                           # 播放中一直是 0：卡住了
        if not self.estimated:
            self.played = pos / 1e6
        raw = dict(title=title, artist=artist, album=md.get("xesam:album") or "", playing=playing, playbackRate=rate,
                   bundleIdentifier=name[len(PREFIX):])
        elapsed = self.played * 1e6 if self.estimated else pos
        raw.update(elapsedTimeMicros=int(elapsed), timestampEpochMicros=int(now * 1_000_000))
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
