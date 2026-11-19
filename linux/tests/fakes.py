"""Session / app 单测用的假 Link（记录收到的帧）"""
import monita_link as ml

GOOD_INFO = dict(proto=0, fw=0x10, w=800, h=1280, pixfmt=1, seg_bytes=72, caps=0xFF, max_payload=32768)


class FakeSer:
    def __init__(self, link):
        self.link = link
        self.in_waiting = 0

    def write(self, data):
        if self.link.broken:
            raise OSError(5, "EIO")
        for t, s, p in ml.Parser().feed(bytes(data)):
            self.link.frames.append((t, p))

    def close(self):
        self.link.closed = True


class FakeLink:
    """Link 的替身：HELLO 回 info；call 回 ACK（计数可调）；fail 里的 type 等应答超时；broken = 写就 EIO"""

    def __init__(self, port=None):
        self.port = port
        self.ser = FakeSer(self)
        self.seq = 0
        self.keys = []
        self.clock = None
        self.frames = []              # [(type, payload)]，按写出的顺序
        self.info_reply = dict(GOOD_INFO)
        self.errs = 0                 # 下一条 ACK 里的 crc_err
        self.status = {}              # type -> ACK status
        self.fail = set()
        self.broken = False
        self.closed = False
        self.hello_times = []

    def types(self):
        return [t for t, _ in self.frames]

    def hello(self, with_time=True):
        self.hello_times.append(self.clock() if self.clock else None)
        self.ser.write(ml.encode(ml.HELLO, 0))
        if ml.HELLO in self.fail:
            raise TimeoutError("hello")
        self.info = dict(self.info_reply)
        return self.info

    def call(self, ftype, payload=b"", timeout=None):
        self.ser.write(ml.encode(ftype, 0, payload))
        self.last_timeout = timeout
        if ftype in self.fail:
            raise TimeoutError("type %#x" % ftype)
        d = dict(zip(ml.STAT_NAMES, [0] * 9))
        d.update(for_type=ftype, status=self.status.get(ftype, 0), crc_err=self.errs)
        return d

    def ping(self):
        return self.call(ml.PING)

    def seg(self, fb72):
        return self.call(ml.SEG, bytes(fb72))

    def led(self, mask, mode, period_ms=0, on_ms=0, duration_ms=0):
        return self.call(ml.LED, ml.led_payload(mask, mode, period_ms, on_ms, duration_ms))

    def poll_keys(self, timeout=0.0):
        ks, self.keys = self.keys, []
        return ks

    def close(self):
        self.closed = True


class Factory:
    """端口工厂：每次 open 记一笔，返回新的 FakeLink（经 link_factory）"""

    def __init__(self):
        self.opens = 0
        self.links = []
        self.none = False

    def open_port(self):
        if self.none:
            return None
        self.opens += 1
        return "port%d" % self.opens

    def link(self, port):
        lk = FakeLink(port)
        self.links.append(lk)
        return lk
