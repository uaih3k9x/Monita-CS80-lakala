"""transport：UsbPort 对着假的 pyusb 设备"""
import unittest

from util import FakeClock  # noqa: F401  (也负责 sys.path)
import monita_link as ml
from monita_linux import transport as T


class Timeout(Exception):
    pass


class UsbErr(OSError):
    pass


class FakeDev:
    """假 pyusb 设备：IN 端点回放 inbox 里的块，OUT 端点记录写入"""

    def __init__(self, inbox=(), kernel=False):
        self.inbox = list(inbox)
        self.kernel = kernel
        self.log = []
        self.writes = []
        self.reads = []
        self.reply = None           # 写入时回调：bytes -> 追加给 inbox 的应答
        self.fail = None

    def is_kernel_driver_active(self, i):
        return self.kernel

    def detach_kernel_driver(self, i):
        self.log.append(("detach", i)); self.kernel = False

    def attach_kernel_driver(self, i):
        self.log.append(("attach", i))

    def read(self, ep, size, timeout):
        self.reads.append((ep, size, timeout))
        if self.fail:
            raise self.fail
        if not self.inbox:
            raise Timeout()
        return bytearray(self.inbox.pop(0))

    def write(self, ep, data, timeout):
        if self.fail:
            raise self.fail
        self.writes.append((ep, bytes(data), timeout))
        if self.reply:
            r = self.reply(bytes(data))
            if r:
                self.inbox.append(r)
        return len(data)


class FakeUtil:
    def __init__(self, dev):
        self.dev = dev

    def claim_interface(self, dev, i):
        dev.log.append(("claim", i))

    def release_interface(self, dev, i):
        dev.log.append(("release", i))

    def dispose_resources(self, dev):
        dev.log.append(("dispose",))


def port(dev, **kw):
    return T.UsbPort(dev, intf=7, util=FakeUtil(dev), timeout_error=Timeout, **kw)


class UsbPortTests(unittest.TestCase):
    def test_reads_whole_block_whatever_n(self):
        d = FakeDev([b"abcdef"])
        p = port(d)
        self.assertEqual(p.read(1), b"a")
        self.assertEqual(d.reads[-1][:2], (0x8A, 16384))
        self.assertEqual(p.read(100), b"bcdef")
        self.assertEqual(len(d.reads), 1)          # 第二次从内部缓冲给

    def test_timeout_is_empty(self):
        self.assertEqual(port(FakeDev()).read(10), b"")

    def test_in_waiting_does_nonblocking_read(self):
        d = FakeDev([b"xyz"])
        p = port(d)
        self.assertEqual(p.in_waiting, 3)
        self.assertEqual(d.reads[-1][2], 1)        # 1 ms
        self.assertEqual(port(FakeDev()).in_waiting, 0)

    def test_kernel_driver_detached_then_claimed_and_returned(self):
        d = FakeDev(kernel=True)
        p = port(d)
        self.assertEqual(d.log, [("detach", 7), ("claim", 7)])
        p.close()
        self.assertEqual(d.log[2:], [("release", 7), ("attach", 7), ("dispose",)])

    def test_no_kernel_driver_no_detach(self):
        d = FakeDev()
        port(d).close()
        self.assertEqual(d.log, [("claim", 7), ("release", 7), ("dispose",)])

    def test_write_chunks(self):
        d = FakeDev()
        p = port(d, chunk=4)
        self.assertEqual(p.write(b"0123456789"), 10)
        self.assertEqual([w[1] for w in d.writes], [b"0123", b"4567", b"89"])
        self.assertTrue(all(w[0] == 0x07 for w in d.writes))

    def test_usb_error_propagates(self):
        d = FakeDev()
        d.fail = UsbErr("No such device")
        p = port(d)
        with self.assertRaises(OSError):
            p.read(1)
        with self.assertRaises(OSError):
            p.write(b"x")

    def test_reset_input_drains(self):
        d = FakeDev([b"old1", b"old2"])
        p = port(d)
        p.in_waiting
        p.reset_input_buffer()
        self.assertEqual(d.inbox, [])
        self.assertEqual(p.read(4), b"")

    def test_link_hello_roundtrip(self):
        # 事先录好的 HELLO_ACK：proto 0, fw 0x10, 800x1280, pixfmt 1, seg 72, caps 0xF7, max 32768
        ack_pl = bytes.fromhex("0000" "1000" "2003" "0005" "01" "48" "f700" "0080" "0000")

        def reply(data):
            for t, s, _ in ml.Parser().feed(data):
                if t == ml.HELLO:
                    return ml.encode(ml.HELLO_ACK, s, ack_pl)

        d = FakeDev()
        d.reply = reply
        link = ml.Link(port(d))
        info = link.hello()
        self.assertEqual((info["w"], info["h"], info["pixfmt"], info["caps"], info["max_payload"]), (800, 1280, 1, 0xF7, 32768))


class OpenDeviceTests(unittest.TestCase):
    def test_spec_parsing(self):
        self.assertEqual(T.parse_spec("usb"), ("usb", 7))
        self.assertEqual(T.parse_spec("usb:5"), ("usb", 5))
        self.assertEqual(T.parse_spec("/dev/ttyUSB3"), ("tty", "/dev/ttyUSB3"))
        with self.assertRaises(ValueError):
            T.parse_spec("usb:x")

    def test_usb_not_found_returns_none(self):
        self.assertIsNone(T.open_device("usb", find=lambda: None))

    def test_tty_goes_to_open_port(self):
        seen = []
        T.open_device("/dev/ttyUSB3", open_tty=lambda p: seen.append(p) or "port")
        self.assertEqual(seen, ["/dev/ttyUSB3"])


if __name__ == "__main__":
    unittest.main()
