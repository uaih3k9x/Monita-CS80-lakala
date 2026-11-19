"""段码屏 72 字节帧（照 MonitaCore/SegFrame.swift）；用 fwsim/conformance.py 的解码器读回文字再断言"""
import os
import sys
import unittest

from util import REPO
sys.path.insert(0, os.path.join(REPO, "tools", "fwsim"))
import conformance as C  # noqa: E402
from monita_linux import segframe as S  # noqa: E402


def text(fb, cols):
    return "".join(C.SEG_CHARS.get(C.fg_digit_mask(fb, k), "?") for k in cols)


def main(fb):
    return text(fb, range(6))


def clk(fb):
    return text(fb, range(8, 14))


def lit(fb, bit):
    return bool(fb[bit[0]] & bit[1])


LOCAL = (21, 7, 11, 19)          # 21:07, 11-19


def play(state="playing", pos=33.0, dur=214.0):
    return dict(state=state, pos=pos, at=0, rate=1.0, duration=dur)


class SegFrameTests(unittest.TestCase):
    def test_playing_mm_ss_and_duration(self):
        fb = S.seg_frame(play(), now=0.1, local=LOCAL)
        self.assertEqual(len(fb), 72)
        self.assertEqual(main(fb), "  0033")
        self.assertTrue(lit(fb, S.MAIN_COLON4))
        self.assertEqual(clk(fb), "  0334")       # 3:34
        self.assertTrue(lit(fb, S.SPEAKER))
        self.assertEqual(sum(lit(fb, v) for v in S.VOLUMES), 2)   # 声波序列第 0 步 = 2

    def test_progress_extrapolates(self):
        fb = S.seg_frame(play(pos=10.0), now=5.0, local=LOCAL)       # at=0 ms -> 5 s 后 15 s
        self.assertEqual(main(fb), "  0015")

    def test_wave_steps_every_250ms(self):
        n = [sum(lit(S.seg_frame(play(), now=i * 0.25, local=LOCAL), v) for v in S.VOLUMES) for i in range(8)]
        self.assertEqual(n, [2, 4, 5, 3, 5, 4, 2, 3])

    def test_hour_format(self):
        fb = S.seg_frame(play(pos=3723.0, dur=7200.0), now=0.0, local=LOCAL)
        self.assertEqual(main(fb), " 10203")
        self.assertTrue(lit(fb, S.MAIN_COLON2) and lit(fb, S.MAIN_COLON4))

    def test_paused_blinks_and_no_wave(self):
        on = S.seg_frame(play("paused"), now=0.2, local=LOCAL)
        off = S.seg_frame(play("paused"), now=0.7, local=LOCAL)
        self.assertEqual(main(on), "  0033")
        self.assertEqual(main(off), "      ")
        self.assertTrue(lit(on, S.SPEAKER))
        self.assertFalse(any(lit(on, v) for v in S.VOLUMES))

    def test_idle_clock_and_date(self):
        a = S.seg_frame(None, now=0.2, local=LOCAL)
        b = S.seg_frame(None, now=0.7, local=LOCAL)
        self.assertEqual(main(a), "  2107")
        self.assertTrue(lit(a, S.MAIN_COLON4))
        self.assertFalse(lit(b, S.MAIN_COLON4))   # 冒号 500 ms 半周期闪
        self.assertEqual(clk(a), " 11-19")
        self.assertFalse(lit(a, S.SPEAKER))

    def test_battery_cells(self):
        self.assertEqual([S.battery_cells(p) for p in (0, 1, 12, 13, 37, 38, 62, 63, 100)], [0, 1, 1, 1, 1, 2, 2, 3, 4])
        fb = S.seg_frame(None, now=0, local=LOCAL, battery=(50, True))
        self.assertTrue(lit(fb, S.BAT_FRAME) and lit(fb, S.CHARGE))
        self.assertEqual([lit(fb, c) for c in S.CELLS], [True, True, False, False])   # 从负极端 D 开始
        fb = S.seg_frame(None, now=0, local=LOCAL)
        self.assertFalse(lit(fb, S.BAT_FRAME))

    def test_matches_swift_bytes(self):
        # 和 Swift segFrame 同一输入算出的字节（MonitaCoreTests 的写法：主区 00:33 + 时长 3:34 + 喇叭 + 2 格声波）
        fb = S.seg_frame(play(), now=0.1, local=LOCAL)
        want = bytearray(72)
        import monita_link as ml
        for k, m in ((2, 0x3F), (3, 0x3F), (4, 0x4F), (5, 0x4F), (10, 0x3F), (11, 0x4F), (12, 0x4F), (13, 0x66)):
            ml.seg_digit(want, k, m)
        for b, m in (S.MAIN_COLON4, (54 + 11, 0x08), S.SPEAKER, S.VOLUMES[0], S.VOLUMES[1]):
            want[b] |= m
        self.assertEqual(fb, bytes(want))


if __name__ == "__main__":
    unittest.main()
