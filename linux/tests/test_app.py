"""主循环：假端口 + 假时钟跑整轮（推帧、帧键、按键、灯、段码屏、休眠 / 唤醒、退出）"""
import time
import unittest

from util import FakeClock
from fakes import Factory
import monita_link as ml
from monita_linux.app import App, Covers
from monita_linux.controls import MENU, MINUS, PLUS, QUERY
from monita_linux.session import Session

NOW = 1784081587.0


class FakeRenderer:
    """画面 = (页, 秒) 决定的纯色整帧；记录画了几次"""
    progress_w = 452.0

    def __init__(self):
        self.calls = []

    def render(self, page, inp):
        self.calls.append((page, inp))
        v = (len(self.calls) * 7 + (1 if page == "clock" else 0)) & 0xFF
        fb = bytearray(800 * 1280 * 2)
        fb[0:64] = bytes([v]) * 64                          # 只动左上角一块 tile：差分 = 1 个矩形
        return None, bytes(fb)


class FakeLyrics:
    def __init__(self, data=None):
        self.data = data

    def payload(self):
        return self.data

    def cover(self, sid):
        return None


class FakePlayer:
    def __init__(self):
        self.pp = 0
        self.art_url = None

    def play_pause(self):
        self.pp += 1


class FakeVolume:
    def __init__(self):
        self.steps = []

    def step(self, pct):
        self.steps.append(pct)


class FakePower:
    def __init__(self, app_ref):
        self.app_ref = app_ref
        self.log = []

    def take_inhibitor(self):
        self.log.append("take")
        return True

    def release(self):
        lk = self.app_ref().session.link
        self.log.append(("release", list(lk.types()) if lk else None))


def playing(pos=33.0, at=None):
    return dict(state="playing", title="t", artist="a", album="", duration=214.0, pos=pos, at=at if at is not None else NOW * 1000,
                rate=1.0, app="spotify", source="mpris", estimated=False, songId=None,
                lyric=dict(status="ok", lines=[dict(t=0, x="a", tr=""), dict(t=40.0, x="b", tr="")]))


class Harness:
    def __init__(self, data=None, connect=True):
        self.mono = FakeClock(100.0)
        self.wall = FakeClock(NOW)
        self.fac = Factory()
        self.sess = Session(self.fac.open_port, clock=self.wall, mono=self.mono, link_factory=self.fac.link)
        self.r = FakeRenderer()
        self.lyr = FakeLyrics(data)
        self.player, self.vol = FakePlayer(), FakeVolume()
        self.app = App(self.sess, self.lyr, self.r, player=self.player, volume=self.vol, clock=self.wall, mono=self.mono)
        self.power = FakePower(lambda: self.app)
        self.app.power = self.power
        if connect:
            self.app.step()

    @property
    def link(self):
        return self.fac.links[-1]

    def run(self, seconds, dt=0.05):
        for _ in range(int(round(seconds / dt))):
            self.mono.advance(dt)
            self.wall.advance(dt)
            self.app.step()


def rects_since(link, i):
    return [t for t in link.types()[i:] if t == ml.RECT]


class Basic(unittest.TestCase):
    def test_first_step_hello_seg_led_full_frame(self):
        h = Harness(playing())
        t = h.link.types()
        self.assertEqual(t[0], ml.HELLO)
        for want in (ml.SEG, ml.LED, ml.PRESENT):
            self.assertIn(want, t)
        self.assertEqual(t.count(ml.RECT), len(ml.full_rects()))
        led = [p for ty, p in h.link.frames if ty == ml.LED][0]
        self.assertEqual(led[:2], bytes([ml.LED_G, ml.LED_ON]))

    def test_same_key_no_redraw_then_diff(self):
        h = Harness(playing())
        n = len(h.r.calls)
        h.run(0.3)                          # 同一秒内：帧键不变
        self.assertEqual(len(h.r.calls), n)
        i = len(h.link.frames)
        h.run(1.0)                          # 秒变了：重画，差分只推一块
        self.assertGreater(len(h.r.calls), n)
        self.assertEqual(len(rects_since(h.link, i)), len(h.r.calls) - n)

    def test_idle_led_off(self):
        h = Harness(dict(state="stopped", lyric=dict(status="idle", lines=[])))
        led = [p for ty, p in h.link.frames if ty == ml.LED][0]
        self.assertEqual(led[1], ml.LED_OFF)

    def test_seg_resent_every_1_5s_even_unchanged(self):
        h = Harness(None)
        i = len(h.link.frames)
        h.run(3.1)
        self.assertGreaterEqual(h.link.types()[i:].count(ml.SEG), 2)

    def test_no_device_no_crash_retry(self):
        h = Harness(playing(), connect=False)
        h.fac.none = True
        h.run(5)
        self.assertEqual(h.fac.opens, 0)
        h.fac.none = False
        h.run(2.1)
        self.assertEqual(h.link.types()[0], ml.HELLO)

    def test_push_failure_next_full(self):
        h = Harness(playing())
        i = len(h.link.frames)
        h.link.errs = 1                     # 下一条 PRESENT 的 ACK 计数涨了：这次差分作废
        h.run(1.0)
        t = h.link.types()[i:]
        bad = t.index(ml.PRESENT)                                   # 推坏的那一批之后
        nxt = t.index(ml.PRESENT, bad + 1)
        self.assertEqual(t[bad + 1:nxt].count(ml.RECT), len(ml.full_rects()))     # 紧接着推整帧


class KeepaliveAndGeneration(unittest.TestCase):
    def test_loop_stall_3_6s_rehello_full_frame(self):
        h = Harness(playing())
        h.run(1.0)
        i = len(h.link.frames)
        h.mono.advance(3.6)
        h.wall.advance(3.6)
        h.app.step()
        t = h.link.types()[i:]
        self.assertEqual(t[0], ml.HELLO)
        self.assertEqual(t.count(ml.RECT), len(ml.full_rects()))
        self.assertIn(ml.LED, t)            # HELLO 把灯灭了，重发

    def test_rehello_in_seg_triggers_full_same_step(self):
        h = Harness(None)
        h.run(1.0)
        i = len(h.link.frames)
        h.sess.last_sent -= 4               # 模拟：上一帧发出后过了 4 s（seg 先触发重 HELLO）
        h.app.seg_at -= 4
        h.app.due = {k: v for k, v in h.app.due.items() if k != "seg"}
        h.app.step()
        t = h.link.types()[i:]
        self.assertEqual(t[0], ml.HELLO)
        self.assertEqual(t.count(ml.RECT), len(ml.full_rects()))


class Keys(unittest.TestCase):
    def key(self, h, k, *evs):
        h.link.keys += [dict(key=k, ev=e, n=h.sess.key_n + j, uptime=0) for j, e in enumerate(evs)]
        h.run(0.05)

    def test_volume(self):
        h = Harness(playing())
        self.key(h, PLUS, ml.KEY_DOWN, ml.KEY_UP)
        self.key(h, MINUS, ml.KEY_DOWN)
        self.assertEqual(h.vol.steps, [6, -6])

    def test_page_toggle_and_long_press(self):
        h = Harness(playing())
        self.key(h, MENU, ml.KEY_DOWN, ml.KEY_UP)
        h.run(0.2)
        self.assertEqual(h.app.page, "clock")
        self.assertEqual(h.r.calls[-1][0], "clock")
        self.key(h, MENU, ml.KEY_DOWN, ml.KEY_LONG, ml.KEY_UP)
        self.assertEqual(h.app.page, "clock")

    def test_query_play_pause(self):
        h = Harness(playing())
        self.key(h, QUERY, ml.KEY_DOWN, ml.KEY_UP)
        self.assertEqual(h.player.pp, 1)


class Power(unittest.TestCase):
    def test_sleep_then_release_then_silence(self):
        h = Harness(playing())
        h.app.post(("sleep", True))
        h.run(0.05)
        rel = [x for x in h.power.log if x != "take"][0]
        self.assertEqual(rel[1][-1], ml.SLEEP)           # SLEEP 发出之后才关 fd
        n = len(h.link.frames)
        h.run(10)
        self.assertEqual(len(h.link.frames), n)          # 睡着时一个字节都不写

    def test_wake_hello_first_then_full_and_led(self):
        h = Harness(playing())
        h.app.post(("sleep", True))
        h.run(1)
        i = len(h.link.frames)
        h.app.post(("sleep", False))
        h.run(0.05)
        t = h.link.types()[i:]
        self.assertEqual(t[0], ml.HELLO)
        first_present = t.index(ml.PRESENT)
        self.assertEqual(t[:first_present].count(ml.RECT), len(ml.full_rects()))
        self.assertIn(ml.LED, t)
        self.assertIn("take", h.power.log)

    def test_sleep_when_not_connected(self):
        h = Harness(playing(), connect=False)
        h.fac.none = True
        h.app.post(("sleep", True))
        h.run(0.05)
        self.assertEqual(h.fac.opens, 0)
        self.assertEqual(h.power.log, [("release", None)])

    def test_quit_led_off_unless_asleep(self):
        h = Harness(playing())
        h.app.post(("quit",))
        h.run(0.05)
        self.assertTrue(h.app.done)
        self.assertEqual(h.link.types()[-1], ml.LED)
        self.assertEqual(h.link.frames[-1][1][1], ml.LED_OFF)
        h = Harness(playing())
        h.app.post(("sleep", True))
        h.app.post(("quit",))
        h.run(0.05)
        self.assertEqual(h.link.types()[-1], ml.SLEEP)


class CoversTests(unittest.TestCase):
    def test_background_fetch_then_cached(self):
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (300, 300), (9, 8, 7)).save(buf, "PNG")
        started = []

        class T:
            def __init__(self, target, daemon=True):
                started.append(target)

            def start(self):
                pass
        c = Covers(thread=T)
        self.assertIsNone(c.get("k", lambda: buf.getvalue()))
        self.assertIsNone(c.get("k", lambda: buf.getvalue()))
        self.assertEqual(len(started), 1)                # 不重复起线程
        started[0]()
        img = c.get("k", lambda: None)
        self.assertEqual(img.size, (220, 220))

    def test_fetch_failure_cached_as_none(self):
        started = []

        class T:
            def __init__(self, target, daemon=True):
                started.append(target)

            def start(self):
                pass
        c = Covers(thread=T)
        c.get("k", lambda: (_ for _ in ()).throw(OSError("net")))
        started[0]()
        self.assertIsNone(c.get("k", lambda: b""))
        self.assertEqual(len(started), 1)


if __name__ == "__main__":
    unittest.main()
