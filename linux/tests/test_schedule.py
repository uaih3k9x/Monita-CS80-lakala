"""进度插值、歌词行、帧键"""
import unittest

from util import FakeClock  # noqa: F401
from monita_linux import schedule as S


def d(**kw):
    base = dict(state="playing", title="t", artist="a", pos=10.0, at=0, rate=1.0, duration=100.0,
                lyric=dict(status="ok", lines=[dict(t=0, x="", tr=""), dict(t=12.0, x="a", tr=""), dict(t=20.0, x="b", tr="")]))
    base.update(kw)
    return base


class Position(unittest.TestCase):
    def test_extrapolate_and_clamp(self):
        self.assertAlmostEqual(S.position_at(d(), 5.0), 15.0)
        self.assertEqual(S.position_at(d(state="paused"), 5.0), 10.0)
        self.assertEqual(S.position_at(d(), 500.0), 100.0)
        self.assertEqual(S.position_at(None, 5.0), 0.0)
        self.assertAlmostEqual(S.position_at(d(rate=2.0), 1.0), 12.0)

    def test_line_index(self):
        self.assertEqual(S.line_index([0, 12, 20], -1), -1)
        self.assertEqual(S.line_index([0, 12, 20], 12), 1)
        self.assertEqual(S.line_index([0, 12, 20], 99), 2)


class FrameKey(unittest.TestCase):
    def test_same_second_same_key(self):
        a = S.frame_key("lyrics", d(), 0.2, 452)
        b = S.frame_key("lyrics", d(at=100, now=5), 0.3, 452)   # 时间戳变了、显示的秒没变
        self.assertEqual(a, b)

    def test_second_or_line_change(self):
        self.assertNotEqual(S.frame_key("lyrics", d(), 0.2, 452), S.frame_key("lyrics", d(), 1.2, 452))
        self.assertNotEqual(S.frame_key("lyrics", d(), 1.6, 452), S.frame_key("lyrics", d(), 1.9, 452))  # 11.75 + 0.15 -> 第 1 行

    def test_paused_only_minute(self):
        p = d(state="paused")
        self.assertEqual(S.frame_key("lyrics", p, 1.0, 452), S.frame_key("lyrics", p, 50.0, 452))

    def test_clock_minute_and_page(self):
        self.assertEqual(S.frame_key("clock", d(), 1.0, 452), S.frame_key("clock", d(title="x"), 59.0, 452))
        self.assertNotEqual(S.frame_key("clock", d(), 59.0, 452), S.frame_key("clock", d(), 61.0, 452))
        self.assertNotEqual(S.frame_key("clock", None, 1.0, 452), S.frame_key("lyrics", None, 1.0, 452))

    def test_idle_minute_only(self):
        idle = dict(state="stopped", lyric=dict(status="idle", lines=[]))
        self.assertEqual(S.frame_key("lyrics", idle, 1.0, 452), S.frame_key("lyrics", None, 30.0, 452))

    def test_panel_and_cover_in_key(self):
        self.assertNotEqual(S.frame_key("lyrics", d(), 0.2, 452, panel=(1,)), S.frame_key("lyrics", d(), 0.2, 452, panel=(2,)))
        self.assertNotEqual(S.frame_key("lyrics", d(), 0.2, 452), S.frame_key("lyrics", d(), 0.2, 452, cover=object()))

    def test_lyric_status_change(self):
        a = d(lyric=dict(status="loading", lines=[]))
        b = d(lyric=dict(status="ok", lines=[dict(t=0, x="x", tr="")]))
        self.assertNotEqual(S.frame_key("lyrics", a, 0.2, 452), S.frame_key("lyrics", b, 0.2, 452))


if __name__ == "__main__":
    unittest.main()
