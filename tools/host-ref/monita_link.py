"""Monita link 协议 v0 的 Python 参考实现 (docs/protocol.md), 覆盖到 fw v11 (KEY / LED / 沉浸模式 IMM_* / SLEEP / RECT_LZ4)。

只用标准库; 打开串口优先用 pyserial (`pip install pyserial`), 没装时 POSIX 上退回 PosixPort (termios)。
RECT_LZ4 (fw v11) 要 python lz4 (`pip install lz4` / Debian `python3-lz4`, 可选): 没装就照发 RECT。
纯逻辑 (编帧、拆帧、RGB565、tile 差分打包、IMM 载荷) 不依赖串口, 见 test_monita_link.py。
不用设备测上位机: tools/fwsim/ (固件模拟器, 开一个伪终端当串口)。
"""
import struct
import time
import warnings
import zlib

try:
    import lz4.block as _lz4block               # 可选: fw v11 的 RECT_LZ4
except ImportError:
    _lz4block = None

SYNC = b"\xA5\x5A"
PROTO_VER = 0
HELLO, SEG, RECT, PRESENT, BACKLIGHT, FBCRC, DIAG, PING = 0x01, 0x10, 0x20, 0x21, 0x30, 0x40, 0x50, 0x7F
COPY = 0x22
RECT_LZ4 = 0x23                                 # fw v11: u16 x,y,w,h + u32 raw_len + LZ4 raw block, 同 RECT 不应答
LED = 0x31                                      # fw v8
SLEEP = 0x32                                    # fw v10: 空 -> ACK, 设备关背光 / 段码屏 / 灯, 下一帧叫醒
IMM_ENTER, IMM_EXIT, IMM_RECT, IMM_SCROLL = 0x60, 0x61, 0x62, 0x63   # fw v9
HELLO_ACK, KEY, DIAG_R, ACK = 0x81, 0x90, 0xD0, 0xFE
OK, EPARAM, EUNSUP = 0, 1, 3
W, H = 800, 1280
TILE_W, TILE_H = 32, 16
CAP_FBCRC, CAP_DOUBLEBUF, CAP_COPY, CAP_TRIBUF = 0x0001, 0x0002, 0x0004, 0x0008
CAP_KEYS, CAP_LED, CAP_IMMERSIVE = 0x0010, 0x0020, 0x0040
CAP_SLEEP = 0x0080                              # fw v10: SLEEP + 主机走了 (5 s 没帧) 回开机画面
CAP_LZ4 = 0x0100                                # fw v11: RECT_LZ4
KEY_DOWN, KEY_UP, KEY_LONG = 1, 2, 3
LED_R, LED_G, LED_B = 1, 2, 4
LED_OFF, LED_ON, LED_BLINK = 0, 1, 2
IMM_VISIBLE = 0x01                              # IMM_RECT flags bit0: 允许写屏上的行
# DIAG (0x50) 子命令: 只给诊断用, 不属于稳定协议 (fw/link/diag.h)
DG_MEMQ, DG_MAXBLK, DG_BUFS, DG_SCAN, DG_PEEK, DG_FILL, DG_FLIP, DG_EVT, DG_PHASE, DG_DBUF = 0, 1, 2, 3, 4, 5, 6, 7, 9, 10
DG_FLIPMODE = 11                                # fw v2: 翻页时机, v4 起默认 1 = 立即 (26..30 曾是 DMA 研究, 已移除)
DG_TRI, DG_NBUF = 31, 32                        # fw v6: 多缓冲状态 / 切块数
TRI_FIELDS = ("n", "nbuf", "note", "s", "p", "d", "gver", "waits", "xcopies", "syncs", "full_syncs", "buf0", "buf1", "buf2", "flip_wait_us")
# 零拷贝滚动实验 (fw v3, fw/link/zc_diag.h)
DG_ZC_ARM, DG_ZC_PEEK, DG_ZC_POKE, DG_ZC_LINE, DG_ZC_RULER, DG_ZC_SPLIT = 20, 21, 22, 23, 24, 25
LINE_US = 26600 / 1310          # 每行时长 (面板 vtotal 1310 行, 帧周期 26.6 ms)
VBLANK_LINES = 3 + 11           # vsync + vbp: 0x04 事件之后多少行才到有效区第 0 行 (推测 0x04 = vsync 起点)
# ACK 的 9 个 u32。注意 "uptime_ms" 实际单位是 1.024 ms (固件 osiUpTimeUS >> 10); 键名是公开接口, 不改
STAT_NAMES = ("rx_bytes", "frames", "crc_err", "len_err", "rects", "presents", "bad_param", "uptime_ms", "extra")


def crc32(b):
    return zlib.crc32(b) & 0xFFFFFFFF


def encode(ftype, seq, payload=b""):
    body = struct.pack("<BBH", ftype, seq & 0xFF, len(payload)) + bytes(payload)
    return SYNC + body + struct.pack("<I", crc32(body))


class Parser:
    """流式拆帧: feed(bytes) 返回收齐的 [(type, seq, payload)]; 坏帧丢弃并重新找同步字。"""

    def __init__(self, max_payload=65535):
        self.buf = bytearray()
        self.max = max_payload
        self.bad = 0

    def feed(self, data):
        self.buf += data
        out = []
        while True:
            i = self.buf.find(SYNC)
            if i < 0:
                del self.buf[:max(0, len(self.buf) - 1)]
                return out
            del self.buf[:i]
            if len(self.buf) < 6:
                return out
            t, s, n = struct.unpack_from("<BBH", self.buf, 2)
            if n > self.max:
                self.bad += 1
                del self.buf[:1]
                continue
            if len(self.buf) < 6 + n + 4:
                return out
            body = bytes(self.buf[2:6 + n])
            (c,) = struct.unpack_from("<I", self.buf, 6 + n)
            if c != crc32(body):
                self.bad += 1
                del self.buf[:1]
                continue
            out.append((t, s, body[4:]))
            del self.buf[:6 + n + 4]


def parse_ack(p):
    for_type, status = p[0], p[1]
    vals = struct.unpack_from("<9I", p, 4)
    d = dict(zip(STAT_NAMES, vals))
    d.update(for_type=for_type, status=status)
    return d


def parse_hello_ack(p):
    ver, fw, w, h, fmt, seg, caps, maxp, _ = struct.unpack("<HHHHBBHHH", p[:16])
    return dict(proto=ver, fw=fw, w=w, h=h, pixfmt=fmt, seg_bytes=seg, caps=caps, max_payload=maxp)


def parse_key(p):
    """KEY (0x90, 设备主动发, seq = 0): u8 key, u8 ev, u16 n, u32 uptime (1.024 ms 单位)。长度不对返回 None"""
    if len(p) < 8:
        return None
    key, ev, n, up = struct.unpack_from("<BBHI", p)
    return dict(key=key, ev=ev, n=n, uptime=up)


def led_payload(mask, mode, period_ms=0, on_ms=0, duration_ms=0):
    """LED (0x31, fw v8): mode 0 灭 / 1 常亮 / 2 闪烁; period 0 = 1000, on 0 = 一半, duration 0 = 一直 (mode 0 时忽略)"""
    return struct.pack("<BBHHH", mask, mode, period_ms, on_ms, duration_ms)


# ---- 像素 ----

def rgb888_to_rgb565(rgb):
    """RGB888 字节串 -> RGB565 小端字节串 (R 在高位)。"""
    out = bytearray(len(rgb) // 3 * 2)
    j = 0
    for i in range(0, len(rgb) - 2, 3):
        v = ((rgb[i] & 0xF8) << 8) | ((rgb[i + 1] & 0xFC) << 3) | (rgb[i + 2] >> 3)
        out[j] = v & 0xFF
        out[j + 1] = v >> 8
        j += 2
    return bytes(out)


def rect_payload(fb, x, y, w, h, width=W):
    """从整帧 RGB565 字节 fb 里截一个矩形, 编成 RECT payload。"""
    rows = [fb[((y + r) * width + x) * 2:((y + r) * width + x + w) * 2] for r in range(h)]
    return struct.pack("<HHHH", x, y, w, h) + b"".join(rows)


# ---- RECT_LZ4 (0x23, fw v11) ----

def lz4_available():
    return _lz4block is not None


def lz4_worth(clen, raw_len):
    """压缩后严格小于原始的 3/4 才值得发 RECT_LZ4 (三个上位机同一条规则)"""
    return clen is not None and clen * 4 < raw_len * 3


def rect_frame(fb, x, y, w, h, lz4=False, width=W):
    """一个矩形编成 (type, payload)。lz4 = True 且压得够小时是 RECT_LZ4 (矩形头 + u32 raw_len + LZ4 raw block,
    不带 frame 头), 否则 RECT。payload 不会比同一块的 RECT 长 (调用方按 RECT 切的矩形照样放得进 max_payload)"""
    pl = rect_payload(fb, x, y, w, h, width)
    if lz4 and _lz4block is not None:
        raw = pl[8:]
        c = _lz4block.compress(raw, store_size=False)
        if lz4_worth(len(c), len(raw)) and 12 + len(c) <= len(pl):
            return RECT_LZ4, pl[:8] + struct.pack("<I", len(raw)) + c
    return RECT, pl


def dirty_rects(prev, cur, width=W, height=H, max_payload=32768):
    """32×16 tile 差分, 同一 tile 行里相邻的脏 tile 合并成一个矩形 (受 max_payload 限制)。
    prev 为 None 时返回整帧条带。返回 [(x, y, w, h)]。"""
    if prev is None:
        return full_rects(width, height, max_payload)
    max_tiles = max(1, (max_payload - 8) // (TILE_W * TILE_H * 2))
    rects = []
    for ty in range(0, height, TILE_H):
        th = min(TILE_H, height - ty)
        run = None
        for tx in range(0, width, TILE_W):
            tw = min(TILE_W, width - tx)
            dirty = any(prev[((ty + r) * width + tx) * 2:((ty + r) * width + tx + tw) * 2]
                        != cur[((ty + r) * width + tx) * 2:((ty + r) * width + tx + tw) * 2] for r in range(th))
            if dirty and run and run[2] // TILE_W < max_tiles:
                run[2] += tw
            elif dirty:
                if run:
                    rects.append(tuple(run))
                run = [tx, ty, tw, th]
            elif run:
                rects.append(tuple(run))
                run = None
        if run:
            rects.append(tuple(run))
    return rects


def full_rects(width=W, height=H, max_payload=32768):
    rows = max(1, (max_payload - 8) // (width * 2))
    return [(0, y, width, min(rows, height - y)) for y in range(0, height, rows)]


# ---- 串口 ----

class PosixPort:
    """没有 pyserial 时的串口 (Linux / macOS 的 tty、fwsim 的伪终端): 只实现 Link 用到的那几个方法"""

    def __init__(self, path, read_timeout=0.05):
        import os
        import termios
        import tty
        self._os, self._termios = os, termios
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
        tty.setraw(self.fd)
        self.read_timeout = read_timeout

    @property
    def in_waiting(self):
        import fcntl
        b = fcntl.ioctl(self.fd, self._termios.FIONREAD, b"\0\0\0\0")
        return struct.unpack("i", b)[0]

    def read(self, n):
        import select
        r, _, _ = select.select([self.fd], [], [], self.read_timeout)
        return self._os.read(self.fd, max(1, n)) if r else b""

    def write(self, data):
        mv = memoryview(bytes(data))
        while mv:
            mv = mv[self._os.write(self.fd, mv):]

    def reset_input_buffer(self):
        self._termios.tcflush(self.fd, self._termios.TCIFLUSH)

    def close(self):
        self._os.close(self.fd)


def open_port(port):
    try:
        import serial  # pyserial
    except ImportError:
        return PosixPort(port)
    return serial.Serial(port, 115200, timeout=0.05, write_timeout=30)


def hello_payload(t):
    """HELLO 的 6 字节: u32 unix 秒, i16 本地时间 - UTC (分钟, 东正西负; 夏令时按 t 那一刻)"""
    tz = -time.altzone if time.localtime(t).tm_isdst > 0 else -time.timezone
    return struct.pack("<Ih", int(t), tz // 60)


class Link:
    """port: 串口名 (COMx / /dev/tty*), 或者已经打开的对象 (要有 read / write / in_waiting / reset_input_buffer / close)"""

    def __init__(self, port, timeout=2.0, lz4=True):
        self.ser = open_port(port) if isinstance(port, str) else port
        self.timeout = timeout
        self.lz4 = lz4                  # False: 不发 RECT_LZ4 (A/B 对比); 固件 caps 有 bit8 且装了 python lz4 才真发
        self.lz4_off = False            # 一批带 RECT_LZ4 的推坏了: 到下一次 HELLO 前只发 RECT
        self.last_bad = 0               # 最近一条 ACK 的 bad_param (RECT_LZ4 坏块计在这里; CRC 错是链路问题, 不算)
        self.last_push = None           # 上一次 push: dict(bytes, rects, lz4)
        self.seq = 0
        self.parser = Parser()
        self.info = None
        self.keys = []                  # 收到的 KEY (parse_key 的 dict), 调用方自己取走
        self.clock = time.time          # HELLO 带的时间从这里取 (对拍脚本换成固定值)
        self.ser.reset_input_buffer()

    def close(self):
        self.ser.close()

    def send(self, ftype, payload=b""):
        self.seq = (self.seq + 1) & 0xFF
        self.ser.write(encode(ftype, self.seq, payload))
        return self.seq

    def _frames(self):
        """读一次串口, 拆出的帧里 KEY 收进 self.keys, 其余返回"""
        out = []
        for t, s, p in self.parser.feed(self.ser.read(self.ser.in_waiting or 1)):
            if t == KEY:
                k = parse_key(p)
                if k:
                    self.keys.append(k)
            else:
                out.append((t, s, p))
        return out

    def wait(self, want_type, seq, timeout=None):
        end = time.monotonic() + (timeout or self.timeout)
        while time.monotonic() < end:
            for t, s, p in self._frames():
                if t == want_type and s == seq:
                    return p
        raise TimeoutError("no reply type %#x seq %d" % (want_type, seq))

    def poll_keys(self, timeout=0.0):
        """不等应答, 只收 KEY: 等到有事件或超时, 返回并清空 self.keys"""
        end = time.monotonic() + timeout
        while True:
            self._frames()
            if self.keys or time.monotonic() >= end:
                break
        ks, self.keys = self.keys, []
        return ks

    def call(self, ftype, payload=b"", timeout=None):
        """发一条需要应答的命令, 返回 ACK dict"""
        a = parse_ack(self.wait(ACK, self.send(ftype, payload), timeout))
        self.last_bad = a["bad_param"]
        return a

    def hello(self, with_time=True):
        pl = hello_payload(self.clock()) if with_time else b""
        self.info = parse_hello_ack(self.wait(HELLO_ACK, self.send(HELLO, pl)))
        self.lz4_off, self.last_bad = False, 0           # HELLO 清了设备的计数
        if self.info["proto"] != PROTO_VER:
            warnings.warn("设备 proto_ver = %d, 这份参考实现只认 %d" % (self.info["proto"], PROTO_VER))
        return self.info

    def ping(self):
        return self.call(PING)

    def backlight(self, on):
        return self.call(BACKLIGHT, bytes([1 if on else 0]))

    def seg(self, fb72):
        assert len(fb72) == 72
        return self.call(SEG, bytes(fb72))

    def fbcrc(self, x, y, w, h):
        a = self.call(FBCRC, struct.pack("<HHHH", x, y, w, h), timeout=5)
        return a["extra"] if a["status"] == OK else None

    def diag(self, op, args=b"", timeout=10):
        p = self.wait(DIAG_R, self.send(DIAG, bytes([op]) + args), timeout)
        n = struct.unpack_from("<H", p, 2)[0]
        return p[1], list(struct.unpack_from("<%dI" % n, p, 4))

    def led(self, mask, mode, period_ms=0, on_ms=0, duration_ms=0):
        return self.call(LED, led_payload(mask, mode, period_ms, on_ms, duration_ms))

    def sleep(self):
        """SLEEP (fw v10): 设备回 ACK 后关背光、段码屏、灯; 之后发任何一帧都会叫醒它 (所以睡着时别发 PING)"""
        return self.call(SLEEP)

    # ---- 沉浸模式 (fw v9, caps bit6) ----

    def max_rect_payload(self):
        return min(self.info["max_payload"] if self.info else 32768, 32768)

    def imm_enter(self, y0=0):
        """返回 ACK; status 0 时 extra = 卷轴行数 R, 3 = 设备拿不到够大的卷轴 (留在普通页面)"""
        return self.call(IMM_ENTER, imm_enter_payload(y0))

    def imm_exit(self):
        """返回 ACK; extra = 退出后的 caps (多缓冲可能拿不全), 同步进 self.info"""
        a = self.call(IMM_EXIT)
        if self.info is not None and a["status"] == OK:
            self.info["caps"] = a["extra"]
        return a

    def imm_write(self, y, rows, x=0, w=W, visible=False):
        """把 rows (h 行 × w 像素的 RGB565 字节) 写到逻辑行 y 起, 按 max_payload 切条带, 不等应答 (同 RECT)。
        下一条 IMM_SCROLL / PING 的 ACK 是同步点, 看它的 bad_param"""
        h = len(rows) // (w * 2)
        out = bytearray()
        for y0, n in imm_strips(0, h, w, self.max_rect_payload()):
            self.seq = (self.seq + 1) & 0xFF
            out += encode(IMM_RECT, self.seq, imm_rect_payload(y + y0, x, w, n, rows[y0 * w * 2:(y0 + n) * w * 2], visible))
        self.ser.write(out)

    def imm_scroll(self, y, dur_ms=0):
        """窗口滚到顶 = y; 返回 ACK (extra = 正在扫描的窗口顶)"""
        return self.call(IMM_SCROLL, imm_scroll_payload(y, dur_ms))

    def use_lz4(self):
        """这一批能不能发 RECT_LZ4: 调用方没关、固件有 caps bit8、装了 python lz4、没因为推坏而暂停"""
        return (self.lz4 and not self.lz4_off and lz4_available()
                and bool(self.info and self.info["caps"] & CAP_LZ4))

    def push(self, fb, rects, frame_id=0, copies=()):
        """把 fb (整帧 RGB565 字节) 里这些矩形发出去, 再 PRESENT; 返回 PRESENT 的 ACK。
        RECT/COPY 不应答, 一次写进串口, 只等 PRESENT 的 ACK。copies = [(sx,sy,w,h,dx,dy)] 排在 RECT 前面。
        fw v11: 每个矩形压缩后 < 3/4 就发 RECT_LZ4 (rect_frame)。这一批有 RECT_LZ4 而 ACK 的 bad_param 涨了 (或 status ≠ 0),
        到下一次 HELLO 前只发 RECT (编解码真不一致时, 调用方的"推整帧恢复"不会一直失败)。CRC 错是链路毛刺, 不暂停。"""
        out = bytearray()
        lz, n_lz = self.use_lz4(), 0
        before = self.last_bad
        for c in copies:
            self.seq = (self.seq + 1) & 0xFF
            out += encode(COPY, self.seq, copy_payload(*c))
        for (x, y, w, h) in rects:
            self.seq = (self.seq + 1) & 0xFF
            t, pl = rect_frame(fb, x, y, w, h, lz)
            n_lz += t == RECT_LZ4
            out += encode(t, self.seq, pl)
        self.ser.write(out)
        self.last_push = dict(bytes=len(out), rects=len(rects), lz4=n_lz)
        a = self.call(PRESENT, struct.pack("<H", frame_id & 0xFFFF), timeout=30)
        if n_lz and (a["status"] != OK or self.last_bad != before):
            self.lz4_off = True
        return a


# ---- COPY (0x22, fw v2) ----

def copy_payload(sx, sy, w, h, dx, dy):
    return struct.pack("<6H", sx, sy, w, h, dx, dy)


def apply_copy(fb, sx, sy, w, h, dx, dy, width=W):
    """主机侧镜像设备的 COPY (memmove 语义, 和固件 lk_copy_apply 一致); fb 是 bytearray"""
    rows = range(h - 1, -1, -1) if dy > sy else range(h)
    for r in rows:
        s = ((sy + r) * width + sx) * 2
        d = ((dy + r) * width + dx) * 2
        fb[d:d + w * 2] = fb[s:s + w * 2]       # 右边先整段取出再赋值, 同行重叠也对


def scroll_copy(x, y, w, h, dy):
    """区域 (x,y,w,h) 内容整体平移 dy 行 (负 = 上移) 对应的 COPY; 同 Scroll.swift scrollCopy"""
    if dy == 0 or abs(dy) >= h:
        return None
    return (x, y - dy, w, h + dy, x, y) if dy < 0 else (x, y, w, h - dy, x, y + dy)


# ---- 沉浸模式 (IMM_*, fw v9) 载荷 ----

def imm_enter_payload(y0=0):
    return struct.pack("<I", y0 & 0xFFFFFFFF)


def imm_scroll_payload(y, dur_ms=0):
    return struct.pack("<IH", y & 0xFFFFFFFF, max(0, min(dur_ms, 0xFFFF)))


def imm_rect_payload(y, x, w, h, pixels, visible=False):
    """IMM_RECT: u32 y, u16 x, w, h, u8 flags, u8 0 + w*h 个 RGB565 (行优先)"""
    if len(pixels) != w * h * 2:
        raise ValueError("IMM_RECT 像素长度 %d != %d" % (len(pixels), w * h * 2))
    return struct.pack("<IHHHBB", y & 0xFFFFFFFF, x, w, h, IMM_VISIBLE if visible else 0, 0) + bytes(pixels)


def imm_strips(y, h, width=W, max_payload=32768):
    """行 [y, y+h) 切成单条 IMM_RECT 放得下的条带 [(y, 行数)] (整行宽 800 / 32 KiB 时 20 行一条)"""
    n = max(1, (min(max_payload, 32768) - 12) // (width * 2))
    return [(r, min(n, y + h - r)) for r in range(y, y + h, n)]


# ---- 零拷贝滚动实验 (DIAG 20..25) 的参数打包/解码 ----

def zc_split_args(frames, t1_us, t2_us, top):
    return struct.pack("<HHHH", frames, t1_us, t2_us, top)


def zc_poke_args(off, val, hold_ms=0):
    return struct.pack("<IIH", off, val, hold_ms)


def zc_line_decode(words):
    """DG_ZC_LINE 回包 -> [(距 0x04 置位的 us, 0x24 低 16 位)]"""
    return [(w >> 16, w & 0xFFFF) for w in words]


def zc_expected_line(t_us, lead=0):
    """帧事件后 t_us 写基址, 按'立即生效'推算分屏线落在第几行屏幕行 (lead = 预取提前量, 行)"""
    return int(round(t_us / LINE_US)) - VBLANK_LINES + lead


def zc_time_for_line(line, lead=0):
    """要让分屏线落在屏幕第 line 行, 帧事件后多少 us 写"""
    return int(round((line + VBLANK_LINES - lead) * LINE_US))


# ---- 段码屏 (映射见 fw/fg00/segmap.h) ----

SEG7 = [0x3F, 0x06, 0x5B, 0x4F, 0x66, 0x6D, 0x7D, 0x07, 0x7F, 0x6F]


def seg_digit(fb, k, m):
    """第 k 列 7 段 (a..g = bit0..6), 与固件 fg_digit 一致"""
    fb[k] &= 0x77; fb[18 + k] &= 0x77; fb[36 + k] &= 0x77; fb[54 + k] &= 0x7F
    if m & 0x20: fb[k] |= 0x80
    if m & 0x40: fb[k] |= 0x08
    if m & 0x01: fb[18 + k] |= 0x80
    if m & 0x02: fb[18 + k] |= 0x08
    if m & 0x10: fb[36 + k] |= 0x80
    if m & 0x04: fb[36 + k] |= 0x08
    if m & 0x08: fb[54 + k] |= 0x80


def seg_number(text):
    """主数字区 6 位显示数字串 (空格 = 灭, '-' = g 段)"""
    fb = bytearray(72)
    for k, ch in enumerate(text[:6].rjust(6)):
        m = SEG7[int(ch)] if ch.isdigit() else (0x40 if ch == "-" else 0)
        seg_digit(fb, k, m)
    return bytes(fb)
