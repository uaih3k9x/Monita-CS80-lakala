"""python3 -m unittest discover -s tools/host-ref"""
import os
import struct
import time
import unittest
import zlib

import monita_link as ml


class FrameTests(unittest.TestCase):
    def test_encode_layout_and_crc(self):
        f = ml.encode(ml.PING, 7, b"\x01\x02\x03")
        self.assertEqual(f[:6], bytes([0xA5, 0x5A, 0x7F, 7, 3, 0]))
        self.assertEqual(struct.unpack("<I", f[-4:])[0], zlib.crc32(f[2:-4]))

    def test_parser_garbage_split_badcrc(self):
        good = ml.encode(ml.ACK, 1, b"hi")
        bad = bytearray(ml.encode(ml.ACK, 2, b"xx")); bad[-1] ^= 0xFF
        stream = b"\x00\xA5\x11" + bytes(bad) + good + ml.encode(ml.HELLO_ACK, 3)
        p = ml.Parser()
        got = []
        for i in range(len(stream)):
            got += p.feed(stream[i:i + 1])
        self.assertEqual(got, [(ml.ACK, 1, b"hi"), (ml.HELLO_ACK, 3, b"")])
        self.assertEqual(p.bad, 1)

    def test_ack_and_hello_parse(self):
        a = ml.parse_ack(bytes([0x21, 0, 0, 0]) + struct.pack("<9I", *range(9)))
        self.assertEqual((a["for_type"], a["rects"], a["extra"]), (0x21, 4, 8))
        h = ml.parse_hello_ack(struct.pack("<HHHHBBHHH", 0, 1, 800, 1280, 1, 72, 1, 32768, 0))
        self.assertEqual((h["w"], h["h"], h["max_payload"]), (800, 1280, 32768))


class HelloTimeTests(unittest.TestCase):
    """HELLO 带的时间: u32 unix 秒 + i16 本地 - UTC 分钟 (东正西负), 夏令时按那个时刻算"""

    def setUp(self):
        self.tz = os.environ.get("TZ")

    def tearDown(self):
        if self.tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.tz
        time.tzset()

    def payload(self, zone, t):
        os.environ["TZ"] = zone
        time.tzset()
        return struct.unpack("<Ih", ml.hello_payload(t))

    def test_zones_and_dst(self):
        summer, winter = 1784081587, 1768950309                 # 2026-07-15 02:13:07Z, 2026-01-20 23:05:09Z
        self.assertEqual(self.payload("America/New_York", summer), (summer, -240))
        self.assertEqual(self.payload("America/New_York", winter), (winter, -300))
        self.assertEqual(self.payload("Asia/Kolkata", summer), (summer, 330))
        self.assertEqual(self.payload("Europe/London", summer), (summer, 60))
        self.assertEqual(self.payload("Australia/Adelaide", winter), (winter, 630))
        self.assertEqual(self.payload("UTC", 1782907200.9), (1782907200, 0))


class PixelTests(unittest.TestCase):
    def test_rgb565(self):
        self.assertEqual(ml.rgb888_to_rgb565(bytes([255, 0, 0, 0, 255, 0, 0, 0, 255])),
                         bytes([0x00, 0xF8, 0xE0, 0x07, 0x1F, 0x00]))

    def test_rect_payload(self):
        w, h = 4, 3
        fb = bytes(range(w * h * 2))
        p = ml.rect_payload(fb, 1, 1, 2, 2, width=w)
        self.assertEqual(p[:8], struct.pack("<HHHH", 1, 1, 2, 2))
        self.assertEqual(p[8:], fb[10:14] + fb[18:22])

    def test_dirty_rects_merges_runs(self):
        w, h = 128, 32
        a = bytes(w * h * 2)
        b = bytearray(a)
        for x in (0, 40, 70, 100):           # tile 0,1,2,3 of row 0
            b[(3 * w + x) * 2] = 1
        b[(20 * w + 64) * 2] = 1             # row 1, tile 2
        self.assertEqual(ml.dirty_rects(a, bytes(b), w, h), [(0, 0, 128, 16), (64, 16, 32, 16)])
        self.assertEqual(ml.dirty_rects(a, a, w, h), [])
        # payload 只够 2 个 tile 时拆开
        self.assertEqual(ml.dirty_rects(a, bytes(b), w, h, max_payload=8 + 2 * 1024),
                         [(0, 0, 64, 16), (64, 0, 64, 16), (64, 16, 32, 16)])

    def test_full_rects_cover_screen_within_payload(self):
        rs = ml.full_rects()
        self.assertEqual(sum(r[3] for r in rs), 1280)
        self.assertTrue(all(8 + r[2] * r[3] * 2 <= 32768 for r in rs))
        self.assertEqual(rs[0], (0, 0, 800, 20))

    def test_zc_args_and_line_math(self):
        self.assertEqual(ml.zc_split_args(200, 13000, 0, 5), struct.pack("<HHHH", 200, 13000, 0, 5))
        self.assertEqual(len(ml.zc_poke_args(0x40, 0x06400001, 3000)), 10)
        self.assertEqual(ml.zc_line_decode([(1500 << 16) | 0x500]), [(1500, 0x500)])
        self.assertEqual(ml.zc_expected_line(13000), 626)
        self.assertEqual(ml.zc_expected_line(ml.zc_time_for_line(337)), 337)
        self.assertLess(ml.zc_time_for_line(337, lead=3), ml.zc_time_for_line(337))

    def test_copy_mirror(self):
        w, h = 8, 6
        fb = bytearray(struct.pack("<%dH" % (w * h), *range(w * h)))
        self.assertEqual(ml.scroll_copy(1, 1, 5, 4, -2), (1, 3, 5, 2, 1, 1))
        self.assertEqual(ml.scroll_copy(1, 1, 5, 4, 2), (1, 1, 5, 2, 1, 3))
        self.assertIsNone(ml.scroll_copy(1, 1, 5, 4, 4))
        ref = list(range(w * h))
        ml.apply_copy(fb, 1, 1, 5, 3, 1, 2, width=w)           # 下移 1 行, 重叠
        for r in reversed(range(3)):
            for x in range(5):
                ref[(2 + r) * w + 1 + x] = ref[(1 + r) * w + 1 + x]
        self.assertEqual(list(struct.unpack("<%dH" % (w * h), fb)), ref)
        self.assertEqual(ml.copy_payload(1, 2, 3, 4, 5, 6), struct.pack("<6H", 1, 2, 3, 4, 5, 6))

    def test_dma_diag_removed(self):
        # DIAG 26..30 (DMA 研究) 已从固件移除, 参考实现也不再提供
        for name in ("DG_DMA", "DG_DMAUSE", "DG_DMAPROBE", "DG_DMARES", "DG_DMASEL", "DMA_FIELDS", "DMA_CAND",
                     "dma_res_decode", "dma_probe_args"):
            self.assertFalse(hasattr(ml, name), name)

    def test_seg_number(self):
        fb = ml.seg_number("-1")
        self.assertEqual(fb[4] & 0x08, 0x08)              # 第 4 列 g 段
        self.assertEqual(fb[18 + 5] & 0x08, 0x08)         # 第 5 列 b 段
        self.assertEqual(fb[36 + 5] & 0x08, 0x08)         # 第 5 列 c 段
        self.assertEqual(fb[0], 0)


class V9Tests(unittest.TestCase):
    """fw v8 / v9 的 KEY / LED / IMM_* 编解码 (docs/protocol.md)"""

    def test_constants_match_protocol(self):
        self.assertEqual((ml.KEY, ml.LED, ml.IMM_ENTER, ml.IMM_EXIT, ml.IMM_RECT, ml.IMM_SCROLL),
                         (0x90, 0x31, 0x60, 0x61, 0x62, 0x63))
        self.assertEqual((ml.CAP_KEYS, ml.CAP_LED, ml.CAP_IMMERSIVE), (0x10, 0x20, 0x40))
        self.assertEqual(ml.PROTO_VER, 0)

    def test_parse_key(self):
        k = ml.parse_key(bytes([3, 2, 0x34, 0x12]) + struct.pack("<I", 99))
        self.assertEqual(k, dict(key=3, ev=2, n=0x1234, uptime=99))
        self.assertIsNone(ml.parse_key(b"\x01"))

    def test_led_payload(self):
        self.assertEqual(ml.led_payload(ml.LED_G, ml.LED_BLINK, 500, 100, 3000), struct.pack("<BBHHH", 2, 2, 500, 100, 3000))
        self.assertEqual(ml.led_payload(0, ml.LED_OFF), bytes(8))

    def test_imm_payloads(self):
        self.assertEqual(ml.imm_enter_payload(7), struct.pack("<I", 7))
        self.assertEqual(ml.imm_scroll_payload(1300, 360), struct.pack("<IH", 1300, 360))
        px = bytes(range(16))                                # 2 行 × 4 px
        p = ml.imm_rect_payload(5000, 1, 4, 2, px, visible=True)
        self.assertEqual(p[:12], struct.pack("<IHHHBB", 5000, 1, 4, 2, 1, 0))
        self.assertEqual(p[12:], px)
        with self.assertRaises(ValueError):
            ml.imm_rect_payload(0, 0, 4, 2, px[:-1])

    def test_imm_strips_respect_max_payload(self):
        self.assertEqual(ml.imm_strips(0, 45), [(0, 20), (20, 20), (40, 5)])
        self.assertTrue(all(12 + 800 * 2 * h <= 32768 for _, h in ml.imm_strips(0, 1280)))
        small = ml.imm_strips(100, 30, max_payload=16384)
        self.assertEqual(small, [(100, 10), (110, 10), (120, 10)])

    def test_stat_names_unchanged(self):
        # 键名是公开接口: uptime_ms 实际单位是 1.024 ms (ACK.uptime), 但不改名
        self.assertEqual(ml.STAT_NAMES[7], "uptime_ms")


class LoopPort:
    """假串口: 写进来的帧原样记下, 每条回一个 ACK (status 0; bad_param = self.bad); HELLO 回 HELLO_ACK (caps = self.caps)"""

    def __init__(self, caps=0):
        self.sent, self.rx, self.in_waiting = [], bytearray(), 0
        self.caps, self.bad = caps, 0

    def write(self, data):
        for t, s, p in ml.Parser().feed(bytes(data)):
            self.sent.append((t, p))
            if t == ml.HELLO:
                self.rx += ml.encode(ml.HELLO_ACK, s, struct.pack("<HHHHBBHHH", 0, 11, 800, 1280, 1, 72, self.caps, 32768, 0))
                continue
            self.rx += ml.encode(ml.ACK, s, bytes([t, 0, 0, 0]) + struct.pack("<9I", 0, 0, 0, 0, 0, 0, self.bad, 0, 0))
        self.in_waiting = len(self.rx)

    def read(self, n):
        out, self.rx[:] = bytes(self.rx[:n]), self.rx[n:]
        self.in_waiting = len(self.rx)
        return out

    def reset_input_buffer(self):
        self.rx.clear()

    def close(self):
        pass


class V10Tests(unittest.TestCase):
    """fw v10: SLEEP (0x32), caps bit7"""

    def test_constants_match_protocol(self):
        self.assertEqual((ml.SLEEP, ml.CAP_SLEEP), (0x32, 0x80))

    def test_sleep_sends_empty_frame_and_returns_ack(self):
        port = LoopPort()
        link = ml.Link(port)
        a = link.sleep()
        self.assertEqual(port.sent, [(ml.SLEEP, b"")])
        self.assertEqual((a["for_type"], a["status"]), (ml.SLEEP, ml.OK))



def ui_frame():
    """像 UI 画面: 整屏底色 + 几块色块 (压缩比几十倍)"""
    fb = bytearray(struct.pack("<H", 0x18E3) * (ml.W * ml.H))
    for y in range(300, 700):
        fb[(y * ml.W + 100) * 2:(y * ml.W + 700) * 2] = struct.pack("<H", 0xFFFF) * 600
    return bytes(fb)


def noise(n, seed=1):
    out, x = bytearray(n), seed
    for i in range(n):
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        out[i] = x >> 16 & 0xFF
    return bytes(out)


@unittest.skipUnless(ml.lz4_available(), "没装 python lz4 (pip install lz4)")
class V11Tests(unittest.TestCase):
    """fw v11: RECT_LZ4 (0x23), caps bit8; 压缩后 < 3/4 原始才发, 旧固件 / 没装 lz4 照发 RECT"""

    def test_constants_match_protocol(self):
        self.assertEqual((ml.RECT_LZ4, ml.CAP_LZ4), (0x23, 0x100))

    def test_rect_lz4_payload_is_raw_block(self):
        import lz4.block
        fb = ui_frame()
        t, p = ml.rect_frame(fb, 0, 300, 800, 20, lz4=True)
        self.assertEqual(t, ml.RECT_LZ4)
        x, y, w, h, raw = struct.unpack_from("<HHHHI", p)
        self.assertEqual((x, y, w, h, raw), (0, 300, 800, 20, 32000))
        self.assertEqual(lz4.block.decompress(p[12:], uncompressed_size=raw), ml.rect_payload(fb, 0, 300, 800, 20)[8:])
        self.assertLess(len(p), 1000)

    def test_incompressible_or_disabled_falls_back_to_rect(self):
        fb = noise(ml.W * 20 * 2)
        self.assertEqual(ml.rect_frame(fb, 0, 0, 800, 20, lz4=True), (ml.RECT, ml.rect_payload(fb, 0, 0, 800, 20)))
        ui = ui_frame()
        self.assertEqual(ml.rect_frame(ui, 0, 0, 800, 20, lz4=False)[0], ml.RECT)
        self.assertEqual(ml.rect_frame(ui, 0, 0, 1, 1, lz4=True)[0], ml.RECT)    # 2 字节压不小

    def test_ratio_threshold(self):
        """3/4 是严格小于: 压缩后正好 3/4 也发 RECT"""
        self.assertTrue(ml.lz4_worth(74, 100))
        self.assertFalse(ml.lz4_worth(75, 100))
        self.assertFalse(ml.lz4_worth(None, 100))

    def test_missing_module_falls_back(self):
        saved = ml._lz4block
        ml._lz4block = None
        try:
            self.assertFalse(ml.lz4_available())
            self.assertEqual(ml.rect_frame(ui_frame(), 0, 0, 800, 20, lz4=True)[0], ml.RECT)
            port = LoopPort(caps=ml.CAP_LZ4)
            link = ml.Link(port)
            link.hello()
            link.push(ui_frame(), ml.full_rects(), 1)
            self.assertNotIn(ml.RECT_LZ4, [t for t, _ in port.sent])
        finally:
            ml._lz4block = saved

    def push_types(self, link, port):
        port.sent.clear()
        link.push(ui_frame(), ml.full_rects(), 1)
        return [t for t, _ in port.sent if t in (ml.RECT, ml.RECT_LZ4)]

    def test_push_uses_lz4_only_with_cap(self):
        port = LoopPort(caps=ml.CAP_LZ4 | ml.CAP_COPY)
        link = ml.Link(port)
        link.hello()
        self.assertEqual(self.push_types(link, port), [ml.RECT_LZ4] * 64)
        self.assertEqual(link.last_push["lz4"], 64)
        old = LoopPort(caps=ml.CAP_COPY)                      # fw v10: 没有 bit8
        link2 = ml.Link(old)
        link2.hello()
        self.assertEqual(self.push_types(link2, old), [ml.RECT] * 64)
        link3 = ml.Link(LoopPort(caps=ml.CAP_LZ4), lz4=False)  # 调用方关掉 (A/B 对比)
        link3.hello()
        self.assertEqual(self.push_types(link3, link3.ser), [ml.RECT] * 64)

    def test_failed_lz4_batch_disables_until_hello(self):
        """一批里有 RECT_LZ4 而 PRESENT 的 ACK 出错计数涨了: 到下一次 HELLO 前都只发 RECT (防编解码不一致时整帧重试死循环)"""
        port = LoopPort(caps=ml.CAP_LZ4)
        link = ml.Link(port)
        link.hello()
        port.bad = 1
        self.push_types(link, port)
        self.assertTrue(link.lz4_off)
        self.assertEqual(self.push_types(link, port), [ml.RECT] * 64)
        link.hello()
        port.bad = 0
        self.assertEqual(self.push_types(link, port), [ml.RECT_LZ4] * 64)


if __name__ == "__main__":
    unittest.main()
