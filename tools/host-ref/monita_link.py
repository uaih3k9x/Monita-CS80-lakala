"""Monita link 协议 v0 的 Python 参考实现 (docs/protocol.md)。

只用标准库; 打开串口要 pyserial (`pip install pyserial`)。纯逻辑 (编帧、拆帧、RGB565、tile 差分打包)
不依赖 pyserial, 见 test_monita_link.py。
"""
import struct
import time
import zlib

SYNC = b"\xA5\x5A"
HELLO, SEG, RECT, PRESENT, BACKLIGHT, FBCRC, DIAG, PING = 0x01, 0x10, 0x20, 0x21, 0x30, 0x40, 0x50, 0x7F
COPY = 0x22
HELLO_ACK, DIAG_R, ACK = 0x81, 0xD0, 0xFE
OK, EPARAM, EUNSUP = 0, 1, 3
W, H = 800, 1280
TILE_W, TILE_H = 32, 16
CAP_FBCRC, CAP_DOUBLEBUF, CAP_COPY, CAP_TRIBUF = 0x0001, 0x0002, 0x0004, 0x0008
# DIAG (0x50) 子命令: 只给诊断用, 不属于稳定协议 (fw/link/diag.h)
DG_MEMQ, DG_MAXBLK, DG_BUFS, DG_SCAN, DG_PEEK, DG_FILL, DG_FLIP, DG_EVT, DG_PHASE, DG_DBUF = 0, 1, 2, 3, 4, 5, 6, 7, 9, 10
DG_FLIPMODE = 11                                # fw v4: 翻页时机 (26..30 曾是 DMA 研究, 已移除)
DG_TRI, DG_NBUF = 31, 32                        # fw v6: 多缓冲状态 / 切块数
TRI_FIELDS = ("n", "nbuf", "note", "s", "p", "d", "gver", "waits", "xcopies", "syncs", "full_syncs", "buf0", "buf1", "buf2", "flip_wait_us")
# 零拷贝滚动实验 (fw v3, fw/link/zc_diag.h)
DG_ZC_ARM, DG_ZC_PEEK, DG_ZC_POKE, DG_ZC_LINE, DG_ZC_RULER, DG_ZC_SPLIT = 20, 21, 22, 23, 24, 25
LINE_US = 26600 / 1310          # 每行时长 (面板 vtotal 1310 行, 帧周期 26.6 ms)
VBLANK_LINES = 3 + 11           # vsync + vbp: 0x04 事件之后多少行才到有效区第 0 行 (推测 0x04 = vsync 起点)
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

class Link:
    def __init__(self, port, timeout=2.0):
        import serial  # pyserial
        self.ser = serial.Serial(port, 115200, timeout=0.05, write_timeout=30)
        self.timeout = timeout
        self.seq = 0
        self.parser = Parser()
        self.info = None
        self.ser.reset_input_buffer()

    def close(self):
        self.ser.close()

    def send(self, ftype, payload=b""):
        self.seq = (self.seq + 1) & 0xFF
        self.ser.write(encode(ftype, self.seq, payload))
        return self.seq

    def wait(self, want_type, seq, timeout=None):
        end = time.monotonic() + (timeout or self.timeout)
        while time.monotonic() < end:
            for t, s, p in self.parser.feed(self.ser.read(self.ser.in_waiting or 1)):
                if t == want_type and s == seq:
                    return p
        raise TimeoutError("no reply type %#x seq %d" % (want_type, seq))

    def call(self, ftype, payload=b"", timeout=None):
        """发一条需要应答的命令, 返回 ACK dict"""
        return parse_ack(self.wait(ACK, self.send(ftype, payload), timeout))

    def hello(self, with_time=True):
        pl = b""
        if with_time:
            tz = -time.altzone if time.localtime().tm_isdst > 0 else -time.timezone
            pl = struct.pack("<Ih", int(time.time()), tz // 60)
        self.info = parse_hello_ack(self.wait(HELLO_ACK, self.send(HELLO, pl)))
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

    def push(self, fb, rects, frame_id=0, copies=()):
        """把 fb (整帧 RGB565 字节) 里这些矩形发出去, 再 PRESENT; 返回 PRESENT 的 ACK。
        RECT/COPY 不应答, 一次写进串口, 只等 PRESENT 的 ACK。copies = [(sx,sy,w,h,dx,dy)] 排在 RECT 前面。"""
        out = bytearray()
        for c in copies:
            self.seq = (self.seq + 1) & 0xFF
            out += encode(COPY, self.seq, copy_payload(*c))
        for (x, y, w, h) in rects:
            self.seq = (self.seq + 1) & 0xFF
            out += encode(RECT, self.seq, rect_payload(fb, x, y, w, h))
        self.ser.write(out)
        return self.call(PRESENT, struct.pack("<H", frame_id & 0xFFFF), timeout=30)


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
