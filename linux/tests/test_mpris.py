"""MPRIS → mediaremote 形状的 raw（假 D-Bus 数据）；选播放器的规则；喂给 lyrics.snapshot / Lyrics.payload"""
import json
import os
import unittest

from util import FakeClock, REPO
import lyrics as L
from monita_linux.sources import mpris as M
from monita_linux.system.dbus import BusError

P = "org.mpris.MediaPlayer2."


def player(status="Playing", title="副屏之歌", artist=("Monita", "某人"), album="样例", length=214_000_000, pos=33_000_000,
           rate=1.0, art=None, trackid="/t/1"):
    md = {"mpris:trackid": trackid}
    if title is not None:
        md["xesam:title"] = title
    if artist is not None:
        md["xesam:artist"] = list(artist)
    if album is not None:
        md["xesam:album"] = album
    if length is not None:
        md["mpris:length"] = length
    if art:
        md["mpris:artUrl"] = art
    props = {"PlaybackStatus": status, "Metadata": md, "Rate": rate}
    if pos is not None:
        props["Position"] = pos
    return props


class FakeBus:
    def __init__(self, players):
        self.players = players          # 总线名后缀 -> props
        self.broken = set()             # 这些调用就超时
        self.calls = []

    def list_names(self):
        return ["org.freedesktop.DBus", ":1.42"] + [P + n for n in self.players]

    def _p(self, name):
        n = name[len(P):]
        if n in self.broken:
            raise BusError("timeout")
        return self.players[n]

    def get(self, name, path, iface, prop):
        self.calls.append(("get", name, prop))
        p = self._p(name)
        if prop not in p:
            raise BusError("org.freedesktop.DBus.Error.NotSupported")
        return p[prop]

    def get_all(self, name, path, iface):
        self.calls.append(("get_all", name))
        return dict(self._p(name))

    def call(self, name, path, iface, method, sig="", args=()):
        self.calls.append(("call", name, method))


def src(players, prefer=None, t=1000.0):
    clk = FakeClock(t)
    return M.MprisSource(FakeBus(players), prefer=prefer, clock=clk), clk


class Mapping(unittest.TestCase):
    def test_fields(self):
        s, clk = src({"spotify": player()})
        raw = s.get()
        self.assertEqual(raw["title"], "副屏之歌")
        self.assertEqual(raw["artist"], "Monita/某人")
        self.assertEqual(raw["album"], "样例")
        self.assertEqual(raw["durationMicros"], 214_000_000)
        self.assertEqual(raw["elapsedTimeMicros"], 33_000_000)
        self.assertEqual(raw["timestampEpochMicros"], 1000 * 1_000_000)
        self.assertTrue(raw["playing"])
        self.assertEqual(raw["playbackRate"], 1.0)
        self.assertEqual(raw["bundleIdentifier"], "spotify")
        self.assertFalse(s.estimated)

    def test_length_missing_and_rate_default(self):
        p = player(length=None)
        del p["Rate"]
        raw = src({"mpv": p})[0].get()
        self.assertNotIn("durationMicros", raw)
        self.assertEqual(raw["playbackRate"], 1.0)

    def test_artist_string(self):
        p = player()
        p["Metadata"]["xesam:artist"] = "单个字符串"
        self.assertEqual(src({"mpv": p})[0].get()["artist"], "单个字符串")

    def test_no_title_or_stopped_is_none(self):
        self.assertIsNone(src({"x": player(title=None)})[0].get())
        self.assertIsNone(src({"x": player(status="Stopped")})[0].get())
        self.assertIsNone(src({})[0].get())

    def test_art_url_exposed(self):
        s, _ = src({"mpv": player(art="file:///tmp/c.jpg")})
        s.get()
        self.assertEqual(s.art_url, "file:///tmp/c.jpg")

    def test_snapshot_shape_matches_sample(self):
        s, clk = src({"spotify": player()})
        snap = L.snapshot(s.get(), now=1000.5)
        with open(os.path.join(REPO, "mac", "Samples", "lyrics-playing.json"), encoding="utf-8") as f:
            sample = json.load(f)
        for k in ("state", "title", "artist", "album", "duration", "pos", "at", "rate", "app"):
            self.assertIn(k, snap)
            self.assertIn(k, sample)
            self.assertIsInstance(snap[k], type(sample[k]) if sample[k] is not None else object, k)
        self.assertEqual((snap["state"], snap["duration"], snap["pos"], snap["at"]), ("playing", 214.0, 33.0, 1_000_000))


class Choose(unittest.TestCase):
    def test_playerctld_excluded(self):
        s, _ = src({"playerctld": player(title="代理"), "mpv": player(status="Paused", title="真的")})
        self.assertEqual(s.get()["title"], "真的")

    def test_prefer_prefix_matches_instance(self):
        s, _ = src({"chromium.instance12345": player(status="Paused", title="浏览器"), "spotify": player(title="S")}, prefer="chromium")
        self.assertEqual(s.get()["title"], "浏览器")
        s, _ = src({"spotify": player(title="S")}, prefer="vlc")     # 匹配不到按规则选
        self.assertEqual(s.get()["title"], "S")

    def test_playing_wins_and_sticky(self):
        players = {"a": player(status="Paused", title="A"), "b": player(title="B")}
        s, _ = src(players)
        self.assertEqual(s.get()["title"], "B")
        players["a"]["PlaybackStatus"] = "Playing"                 # 两个都在放：留在 b，不来回跳
        self.assertEqual(s.get()["title"], "B")
        players["b"]["PlaybackStatus"] = "Paused"
        self.assertEqual(s.get()["title"], "A")

    def test_none_playing_last_playing_then_paused(self):
        players = {"a": player(title="A", status="Paused"), "b": player(title="B")}
        s, _ = src(players)
        s.get()
        players["b"]["PlaybackStatus"] = "Paused"
        self.assertEqual(s.get()["title"], "B")                    # 最近一次在放的
        del players["b"]
        self.assertEqual(s.get()["title"], "A")                    # 它退出了：任意一个 Paused 的
        players["a"]["PlaybackStatus"] = "Stopped"
        self.assertIsNone(s.get())

    def test_broken_player_skipped(self):
        s, _ = src({"a": player(title="A"), "b": player(title="B", status="Paused")})
        s.bus.broken.add("a")
        self.assertEqual(s.get()["title"], "B")

    def test_only_selected_get_all(self):
        s, _ = src({"a": player(title="A"), "b": player(title="B", status="Paused")})
        s.get()
        self.assertEqual([c for c in s.bus.calls if c[0] == "get_all"], [("get_all", P + "a")])

    def test_play_pause_goes_to_shown_player(self):
        s, _ = src({"a": player(title="A", status="Paused"), "b": player(title="B")})
        s.get()
        s.play_pause()
        self.assertEqual(s.bus.calls[-1], ("call", P + "b", "PlayPause"))
        s2, _ = src({})
        s2.get()
        s2.play_pause()                                            # 没有选中的：忽略
        self.assertEqual([c for c in s2.bus.calls if c[0] == "call"], [])


class Estimate(unittest.TestCase):
    def test_position_missing_estimates_from_first_seen(self):
        players = {"firefox.instance_1_23": player(pos=None)}
        s, clk = src(players)
        r1 = s.get()
        self.assertTrue(s.estimated)
        self.assertEqual((r1["elapsedTimeMicros"], r1["timestampEpochMicros"]), (0, 1000 * 1_000_000))
        clk.advance(5)
        r2 = s.get()
        self.assertEqual(r2["timestampEpochMicros"], 1000 * 1_000_000)    # 固定在第一次看到这首的时刻
        players["firefox.instance_1_23"]["Metadata"]["xesam:title"] = "下一首"
        players["firefox.instance_1_23"]["Metadata"]["mpris:trackid"] = "/t/2"
        r3 = s.get()
        self.assertEqual(r3["timestampEpochMicros"], 1005 * 1_000_000)

    def test_position_stuck_at_zero_while_playing(self):
        s, clk = src({"x": player(pos=0)})
        s.get()
        self.assertFalse(s.estimated)                               # 刚切歌是 0 很正常
        clk.advance(3)
        r = s.get()
        self.assertTrue(s.estimated)
        self.assertEqual(r["timestampEpochMicros"], 1000 * 1_000_000)


class LyricsGlue(unittest.TestCase):
    def test_payload_source_and_estimated(self):
        s, clk = src({"x": player(pos=None)})
        lyr = M.MprisLyrics(s)
        lyr._find_id = lambda *a: None                               # 不上网
        p = lyr.payload()
        self.assertEqual(p["source"], "mpris")
        self.assertTrue(p["estimated"])
        self.assertEqual(p["title"], "副屏之歌")
        self.assertIn(p["lyric"]["status"], ("loading", "nomatch"))

    def test_no_player_is_idle_without_netease_fallback(self):
        lyr = M.MprisLyrics(src({})[0])
        lyr._load_lyric = lambda sid: self.fail("不该去取网易云缓存里的歌")
        p = lyr.payload()
        self.assertEqual((p["state"], p["lyric"]["status"]), ("stopped", "idle"))


if __name__ == "__main__":
    unittest.main()
