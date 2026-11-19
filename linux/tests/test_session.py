"""Session：keepalive / 推帧判错 / 休眠 / caps 门控 / 断开重开（DESIGN.md §3.2）"""
import struct
import unittest

from util import FakeClock
from fakes import Factory
import monita_link as ml
from monita_linux.session import Session

FB = bytes(800 * 1280 * 2)


def make(stale=3.5, **kw):
    clk = FakeClock()
    fac = Factory()
    s = Session(fac.open_port, clock=lambda: 1784081587, mono=clk, stale_after=stale, link_factory=fac.link, **kw)
    return s, fac, clk


class Connect(unittest.TestCase):
    def test_connect_hello_and_generation(self):
        s, fac, clk = make()
        self.assertTrue(s.connect_if_needed())
        self.assertTrue(s.up)
        self.assertEqual(s.generation, 1)
        self.assertEqual(fac.links[0].types(), [ml.HELLO])
        self.assertEqual(fac.links[0].hello_times, [1784081587])     # HELLO 的时间走注入的 clock
        self.assertFalse(s.connect_if_needed())                       # 连着就不再 HELLO
        self.assertEqual(fac.opens, 1)

    def test_no_device_retries_every_2s(self):
        s, fac, clk = make()
        fac.none = True
        self.assertFalse(s.connect_if_needed())
        fac.none = False
        clk.advance(1.0)
        self.assertFalse(s.connect_if_needed())
        self.assertEqual(fac.opens, 0)
        clk.advance(1.1)
        self.assertTrue(s.connect_if_needed())

    def test_open_error_warned_once_per_text(self):
        """一直打不开（没装 udev 规则 EACCES）：同样的错只 warning 一次，换了错再 warning"""
        s, fac, clk = make()
        err = [PermissionError(13, "Access denied")]

        def boom():
            raise err[0]
        s.open_port = boom
        with self.assertLogs("monita.session", "DEBUG") as cm:
            for _ in range(5):
                s.connect_if_needed()
                clk.advance(2.1)
            err[0] = OSError(16, "busy")
            s.connect_if_needed()
        warns = [r for r in cm.records if r.levelname == "WARNING"]
        self.assertEqual(len(warns), 2)

    def test_bad_hello_ack_not_up_no_rects(self):
        for bad in (dict(w=480), dict(h=800), dict(pixfmt=2), dict(proto=1)):
            s, fac, clk = make()
            orig = fac.link

            def link(port, bad=bad):
                lk = orig(port)
                lk.info_reply.update(bad)
                return lk
            s.link_factory = link
            self.assertFalse(s.connect_if_needed(), bad)
            self.assertFalse(s.up)
            self.assertFalse(s.push(FB, ml.full_rects(), 1))
            self.assertNotIn(ml.RECT, fac.links[0].types())
            self.assertTrue(fac.links[0].closed)

    def test_io_error_reopens_port(self):
        s, fac, clk = make()
        s.connect_if_needed()
        fac.links[0].broken = True
        self.assertIsNone(s.ping())
        self.assertFalse(s.up)
        self.assertTrue(fac.links[0].closed)
        clk.advance(2.1)
        self.assertTrue(s.connect_if_needed())
        self.assertEqual(fac.opens, 2)                                # 重新调端口工厂，不在旧端口上重发 HELLO
        self.assertEqual(s.generation, 2)

    def test_timeout_is_disconnect(self):
        s, fac, clk = make()
        s.connect_if_needed()
        fac.links[0].fail.add(ml.PRESENT)
        self.assertFalse(s.push(FB, ml.full_rects(), 1))
        self.assertFalse(s.up)
        self.assertEqual(fac.links[0].last_timeout, 10)               # PRESENT 等 10 s，不是 Link.push 的 30 s


class Keepalive(unittest.TestCase):
    def test_3_4_no_rehello_3_6_rehello(self):
        s, fac, clk = make()
        s.connect_if_needed()
        clk.advance(3.4)
        s.ping()
        self.assertEqual(fac.links[0].types(), [ml.HELLO, ml.PING])
        clk.advance(3.6)
        s.ping()
        self.assertEqual(fac.links[0].types(), [ml.HELLO, ml.PING, ml.HELLO, ml.PING])
        self.assertEqual(s.generation, 2)

    def test_rehello_inside_seg_bumps_generation(self):
        s, fac, clk = make()
        s.connect_if_needed()
        clk.advance(4)
        s.seg(bytes(72))
        self.assertEqual(fac.links[0].types()[-2:], [ml.HELLO, ml.SEG])
        self.assertEqual(s.generation, 2)

    def test_push_after_rehello_returns_false(self):
        s, fac, clk = make()
        s.connect_if_needed()
        self.assertTrue(s.push(FB, [(0, 0, 32, 16)], 1))
        clk.advance(4)
        self.assertFalse(s.push(FB, [(0, 0, 32, 16)], 2))             # 设备已回开机页：这批差分作废
        self.assertEqual(s.generation, 2)
        self.assertTrue(s.up)

    def test_stale_none_disables(self):
        s, fac, clk = make(stale=None)
        s.connect_if_needed()
        clk.advance(100)
        s.ping()
        self.assertEqual(fac.links[0].types(), [ml.HELLO, ml.PING])


class Push(unittest.TestCase):
    def test_push_batch_and_ack_baseline(self):
        s, fac, clk = make()
        s.connect_if_needed()
        lk = fac.links[0]
        self.assertTrue(s.push(FB, ml.full_rects(), 1))
        t = lk.types()
        self.assertEqual(t.count(ml.RECT), len(ml.full_rects()))
        self.assertEqual(t[-1], ml.PRESENT)
        lk.errs = 1                                                   # ACK 计数涨了：这次推坏了
        self.assertFalse(s.push(FB, [(0, 0, 32, 16)], 2))
        self.assertTrue(s.up)                                         # 不算断开
        self.assertTrue(s.push(FB, [(0, 0, 32, 16)], 3))              # 基线已更新

    def test_status_nonzero_fails(self):
        s, fac, clk = make()
        s.connect_if_needed()
        fac.links[0].status[ml.PRESENT] = 1
        self.assertFalse(s.push(FB, [(0, 0, 32, 16)], 1))

    def test_big_rect_split_by_max_payload(self):
        s, fac, clk = make()
        s.connect_if_needed()
        s.push(FB, [(0, 0, 800, 100)], 1)
        sizes = [len(p) for t, p in fac.links[0].frames if t == ml.RECT]
        self.assertTrue(all(n <= 32768 for n in sizes))
        self.assertEqual(sum(n - 8 for n in sizes), 800 * 100 * 2)

    def test_small_max_payload_splits_width(self):
        """HELLO_ACK 的 max_payload 比一整行（8 + 800×2）还小：按宽度也要切"""
        for mp in (1000, 100):
            s, fac, clk = make()
            fac_link = fac.link

            def small(port, mp=mp):
                lk = fac_link(port)
                lk.info_reply["max_payload"] = mp
                return lk
            s.link_factory = small
            s.connect_if_needed()
            fb = bytes(range(256)) * (800 * 1280 * 2 // 256)
            self.assertTrue(s.push(fb, ml.full_rects(max_payload=mp), 1))
            got = bytearray(800 * 1280 * 2)
            for t, p in fac.links[0].frames:
                if t != ml.RECT:
                    continue
                self.assertLessEqual(len(p), mp)
                x, y, w, h = struct.unpack("<4H", p[:8])
                for r in range(h):
                    o = ((y + r) * 800 + x) * 2
                    got[o:o + w * 2] = p[8 + r * w * 2:8 + (r + 1) * w * 2]
            self.assertEqual(bytes(got), fb, mp)

    def test_copy_gated_by_caps(self):
        s, fac, clk = make()
        s.connect_if_needed()
        s.push(FB, [], 1, copies=[(0, 8, 800, 100, 0, 0)])
        self.assertIn(ml.COPY, fac.links[0].types())
        s2, fac2, _ = make()
        fac2_link = fac2.link

        def nocopy(port):
            lk = fac2_link(port)
            lk.info_reply["caps"] &= ~ml.CAP_COPY
            return lk
        s2.link_factory = nocopy
        s2.connect_if_needed()
        s2.push(FB, [], 1, copies=[(0, 8, 800, 100, 0, 0)])
        self.assertNotIn(ml.COPY, fac2.links[0].types())


def lz4_make(caps=0x1FF, **kw):
    s, fac, clk = make(**kw)
    fac_link = fac.link

    def withcaps(port):
        lk = fac_link(port)
        lk.info_reply["caps"] = caps
        return lk
    s.link_factory = withcaps
    s.connect_if_needed()
    return s, fac, clk


def rect_types(lk):
    return [t for t, _ in lk.frames if t in (ml.RECT, ml.RECT_LZ4)]


@unittest.skipUnless(ml.lz4_available(), "没装 python lz4")
class Lz4(unittest.TestCase):
    """fw v11 RECT_LZ4：caps bit8 才发；压缩后 < 3/4 才发；推坏了到下次 HELLO 前只发 RECT"""

    def test_full_frame_compressed_and_decodes_back(self):
        import lz4.block
        s, fac, clk = lz4_make()
        fb = bytes([1, 2, 3, 4]) * (800 * 1280 // 2)
        self.assertTrue(s.push(fb, ml.full_rects(), 1))
        lk = fac.links[0]
        self.assertEqual(rect_types(lk), [ml.RECT_LZ4] * 64)
        got = bytearray(len(fb))
        for t, p in lk.frames:
            if t != ml.RECT_LZ4:
                continue
            x, y, w, h, raw = struct.unpack_from("<4HI", p)
            self.assertEqual(raw, w * h * 2)
            px = lz4.block.decompress(p[12:], uncompressed_size=raw)
            for r in range(h):
                o = ((y + r) * 800 + x) * 2
                got[o:o + w * 2] = px[r * w * 2:(r + 1) * w * 2]
        self.assertEqual(bytes(got), fb)

    def test_old_firmware_or_disabled_sends_rect(self):
        s, fac, clk = lz4_make(caps=0xFF)
        s.push(FB, ml.full_rects(), 1)
        self.assertEqual(rect_types(fac.links[0]), [ml.RECT] * 64)
        s2, fac2, _ = lz4_make(lz4=False)
        s2.push(FB, ml.full_rects(), 1)
        self.assertEqual(rect_types(fac2.links[0]), [ml.RECT] * 64)

    def test_incompressible_rect_stays_rect(self):
        s, fac, clk = lz4_make()
        fb = bytearray(FB)
        x = 1
        for i in range(32 * 16 * 2):                                   # 第一个 tile 是噪声
            x = (x * 1103515245 + 12345) & 0x7FFFFFFF
            r, c = divmod(i, 64)
            fb[r * 1600 + c] = x >> 16 & 0xFF
        s.push(bytes(fb), [(0, 0, 32, 16), (32, 0, 32, 16)], 1)
        self.assertEqual(rect_types(fac.links[0]), [ml.RECT, ml.RECT_LZ4])

    def test_crc_errors_do_not_disable_lz4(self):
        """链路 CRC 错（USB 毛刺）和 LZ4 无关：这批算推坏了，但下一批照样压缩"""
        s, fac, clk = lz4_make()
        lk = fac.links[0]
        lk.errs = 1
        self.assertFalse(s.push(FB, ml.full_rects(), 1))
        lk.frames.clear()
        self.assertTrue(s.push(FB, ml.full_rects(), 2))
        self.assertEqual(rect_types(lk), [ml.RECT_LZ4] * 64)

    def test_failed_lz4_batch_disables_until_hello(self):
        s, fac, clk = lz4_make()
        lk = fac.links[0]
        lk.bad = 1
        self.assertFalse(s.push(FB, ml.full_rects(), 1))
        lk.frames.clear()
        self.assertTrue(s.push(FB, ml.full_rects(), 2))               # 恢复的整帧走普通 RECT
        self.assertEqual(rect_types(lk), [ml.RECT] * 64)
        s.suspend(); s.resume()                                        # 下一条之前重新 HELLO
        self.assertTrue(s.connect_if_needed())
        lk.frames.clear()
        s.push(FB, ml.full_rects(), 3)
        self.assertEqual(rect_types(lk), [ml.RECT_LZ4] * 64)


class Caps(unittest.TestCase):
    def _caps(self, caps):
        s, fac, clk = make()
        orig = fac.link

        def link(port):
            lk = orig(port)
            lk.info_reply["caps"] = caps
            return lk
        s.link_factory = link
        s.connect_if_needed()
        return s, fac.links[0]

    def test_led_needs_bit5(self):
        s, lk = self._caps(0xFF & ~ml.CAP_LED)
        self.assertIsNone(s.led(ml.LED_G, ml.LED_ON))
        self.assertNotIn(ml.LED, lk.types())

    def test_sleep_needs_bit7(self):
        s, lk = self._caps(0x7F)
        s.suspend()
        self.assertNotIn(ml.SLEEP, lk.types())
        self.assertFalse(s.can_send)

    def test_led_status_nonzero_not_disconnect(self):
        s, lk = self._caps(0xFF)
        lk.status[ml.LED] = 3
        s.led(ml.LED_G, ml.LED_ON)
        s.seg(bytes(72))
        self.assertTrue(s.up)


class SleepResume(unittest.TestCase):
    def test_suspend_blocks_all_writes(self):
        s, fac, clk = make()
        s.connect_if_needed()
        lk = fac.links[0]
        self.assertTrue(s.suspend())
        self.assertEqual(lk.types()[-1], ml.SLEEP)
        self.assertEqual(lk.last_timeout, 1)
        n = len(lk.frames)
        clk.advance(10)                                               # 连 keepalive 的 HELLO 也不能发
        s.ping(); s.seg(bytes(72)); s.led(ml.LED_G, ml.LED_ON)
        self.assertFalse(s.push(FB, [(0, 0, 32, 16)], 1))
        self.assertFalse(s.connect_if_needed())
        s.poll_keys()
        self.assertEqual(len(lk.frames), n)

    def test_resume_forces_hello(self):
        s, fac, clk = make()
        s.connect_if_needed()
        s.suspend()
        s.resume()
        g = s.generation
        self.assertTrue(s.connect_if_needed())
        self.assertEqual(fac.links[0].types()[-1], ml.HELLO)
        self.assertEqual(s.generation, g + 1)

    def test_resume_then_first_send_is_hello(self):
        s, fac, clk = make()
        s.connect_if_needed()
        s.suspend()
        s.resume()
        s.seg(bytes(72))
        self.assertEqual(fac.links[0].types()[-2:], [ml.HELLO, ml.SEG])

    def test_suspend_when_not_connected_writes_nothing(self):
        s, fac, clk = make()
        self.assertFalse(s.suspend())
        self.assertEqual(fac.opens, 0)

    def test_close_led_off_unless_asleep(self):
        s, fac, clk = make()
        s.connect_if_needed()
        s.close()
        self.assertEqual(fac.links[0].types()[-1], ml.LED)
        self.assertTrue(fac.links[0].closed)
        s, fac, clk = make()
        s.connect_if_needed()
        s.suspend()
        s.close()
        self.assertEqual(fac.links[0].types()[-1], ml.SLEEP)          # 睡着时退出：不发 LED off，免得叫醒设备


class Keys(unittest.TestCase):
    def test_poll_keys_passthrough_and_gap_logged(self):
        s, fac, clk = make()
        s.connect_if_needed()
        lk = fac.links[0]
        lk.keys = [dict(key=3, ev=1, n=0, uptime=1), dict(key=3, ev=2, n=2, uptime=2)]
        with self.assertLogs("monita.session", "INFO") as cm:
            ks = s.poll_keys()
        self.assertEqual(len(ks), 2)
        self.assertTrue(any("KEY" in m for m in cm.output))

    def test_no_link_no_keys(self):
        s, fac, clk = make()
        self.assertEqual(s.poll_keys(), [])


if __name__ == "__main__":
    unittest.main()
