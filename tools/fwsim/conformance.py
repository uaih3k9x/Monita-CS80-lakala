#!/usr/bin/env python3
"""上位机对照测试: 固件模拟器 (fwsim) × Python 参考实现 (tools/host-ref) × Swift (mac/, monita-cs80 --conformance)
× Linux 上位机 (linux/, Session; 只跑 HELLO + RECT_LZ4 那一段, 见 linux_session)。

每种模拟器配置起一个干净的 fwsim, 两个上位机各跑一遍同样的脚本 (HELLO、整帧 / COPY / FBCRC、SEG、LED、
按键注入、未知 type、DIAG、坏帧、沉浸模式 ...), 结果和 expected() (照 docs/protocol.md 写的) 逐项对比,
两个上位机的结果也要完全一样。一般用 tools/fwsim/run.sh 跑。

协议回包之外还看模拟器里固件的副作用 (stat): 脚本走到 "hello" / "seg" / "led" ... 这些点时 (Swift 打印 "@stat 名字"
等 harness 回一行) 取一次 stat, 归一成和时间无关的值放进结果的 "sim" (见 Probe)。HELLO 带的时间用每种配置固定的
时刻 + 时区 (CONFIGS 的 now / zone), 不依赖真实时间。

  python3 tools/fwsim/conformance.py --sim tools/fwsim/build/fwsim [--swift mac/.build/debug/monita-cs80]
"""
import argparse
import array
import datetime
import json
import os
import struct
import subprocess
import sys
import time
import zlib
import zoneinfo

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools", "host-ref"))
import monita_link as ml  # noqa: E402

W, H = ml.W, ml.H


# ---- 和 mac/Sources/monita-cs80/Conformance.swift 逐像素一致的图案 ----

def pat_a(x, y): return (x * 31 + y * 17 + (x ^ y)) & 0xFFFF
def pat_b(x, y): return ((x * 3 + y * 5) & 0xFFFF) ^ 0x5555
def pat_row(row, x): return (row * 7 + x * 3) & 0xFFFF
def pat_vis(row, x): return ((row * 11 + x) & 0xFFFF) ^ 0xA5A5


def le16(vals):
    a = array.array("H", vals)
    if sys.byteorder == "big":
        a.byteswap()
    return a.tobytes()


def frame_a():
    return bytearray(le16(pat_a(x, y) for y in range(H) for x in range(W)))


# ---- RECT_LZ4 (fw v11) 用的两种画面: 和 Conformance.swift 的 patUI / patNoise 逐像素一致 ----
# UI 画面每条都压得很小 (几十倍), 噪声一点都压不动: 不管 liblz4 还是 Apple 的编码器, "< 3/4 才发" 的判定都一样

def pat_ui(x, y):
    if 100 <= x < 700 and 300 <= y < 700:
        return 0xFFFF
    return 0x18E3 if y < 900 else (y // 20 * 0x0841) & 0xFFFF


def pat_noise(x, y):
    h = (x * 0x9E3779B1 ^ y * 0x85EBCA77) & 0xFFFFFFFF
    h ^= h >> 15
    h = (h * 0x2C1B3C6D) & 0xFFFFFFFF
    h ^= h >> 12
    return h & 0xFFFF


def frame_ui():
    return bytes(le16(pat_ui(x, y) for y in range(H) for x in range(W)))


def frame_mixed():
    """上半屏 (y < 640) 噪声 + 下半屏 UI: 640 行在 20 行 / 10 行两种条带下都对齐, 正好一半条带压缩"""
    return bytes(le16(pat_noise(x, y) if y < 640 else pat_ui(x, y) for y in range(H) for x in range(W)))


# 坏的 RECT_LZ4: 一条 800×20, 块里第一个 match 的 offset 是 0
BAD_LZ4 = struct.pack("<HHHHI", 0, 0, 800, 20, 32000) + bytes([0x10, 0xAB, 0x00, 0x00, 0x00])


def lz4_section(push, fbcrc_full, send_bad, rehello, probe):
    """RECT_LZ4 那一段, 三个上位机一样的步骤 (Swift 版在 Conformance.swift lz4Session)。这段推坏过一批, 之后这次连接里
    上位机只发 RECT (后面几段的像素和计数不受影响)。先 HELLO 清计数, 之后每步看
    固件里成功的 RECT_LZ4 条数和 bad_param (Probe "lz4_*")。push(fb, full) 返回推成没有; fbcrc_full() = 屏上整帧 CRC"""
    ui, mixed = frame_ui(), frame_mixed()
    c_ui, c_mixed = zlib.crc32(ui) & 0xFFFFFFFF, zlib.crc32(mixed) & 0xFFFFFFFF
    r = {}
    rehello()
    probe("lz4_hello")
    r["full"] = [push(ui, True), fbcrc_full() == c_ui]                 # 整帧都压缩
    probe("lz4_full")
    r["mixed"] = [push(mixed, True), fbcrc_full() == c_mixed]          # 一半压缩一半 RECT
    probe("lz4_mixed")
    send_bad()                                                         # 坏块: 计 bad_param, 屏上不动
    r["bad"] = [push(mixed, False), fbcrc_full() == c_mixed]
    probe("lz4_bad")
    send_bad()                                                         # 坏块 + 一批带 RECT_LZ4 的整帧: 这批算推坏了
    r["fail"] = push(ui, True)
    probe("lz4_fail")
    r["recover"] = [push(mixed, True), fbcrc_full() == c_mixed]        # 上位机推整帧恢复: 只发 RECT
    probe("lz4_recover")
    rehello()
    probe("lz4_hello2")
    r["again"] = [push(ui, True), fbcrc_full() == c_ui]                # 同一次连接、fw 没变: HELLO 之后照样只发 RECT
    probe("lz4_again")
    return r


def region_crc(fb, x, y, w, h, width=W):
    c = 0
    for r in range(y, y + h):
        c = zlib.crc32(fb[((r * width) + x) * 2:((r * width) + x + w) * 2], c)
    return c & 0xFFFFFFFF


def strips(h, max_payload):
    return len(ml.imm_strips(0, h, W, max_payload))


SEG_PAT = bytes((i * 37 + 11) & 0xFF for i in range(72))     # 和 Conformance.swift 的 segPattern 一致


# ---- 段码屏显存 (fw/fg00/fgbuf.h, segmap.h) ----

FG_HEX = [0x3f, 0x06, 0x5b, 0x4f, 0x66, 0x6d, 0x7d, 0x07, 0x7f, 0x6f, 0x77, 0x7c, 0x39, 0x5e, 0x79, 0x71]
SEG_CHARS = {**{m: "0123456789abcdef"[i] for i, m in enumerate(FG_HEX)}, 0x40: "-", 0: " "}
SEG_MASKS = {c: m for m, c in SEG_CHARS.items()}
CLK_COLONS = ((63, 0x08), (65, 0x08))                       # FG_SEG_CLK_COLON_A / _B


def fg_digit_mask(fg, k):
    c0, c1, c2, c3 = fg[k], fg[18 + k], fg[36 + k], fg[54 + k]
    bits = ((c1 & 0x80, 0x01), (c1 & 0x08, 0x02), (c2 & 0x08, 0x04), (c3 & 0x80, 0x08),
            (c2 & 0x80, 0x10), (c0 & 0x80, 0x20), (c0 & 0x08, 0x40))
    return sum(m for on, m in bits if on)


def seg_render(main, clk):
    """固件 seg_draw_local 应该画出的 72 字节: 主区 6 位 + 小时钟 "hh:mm:ss" (或空)"""
    fg = bytearray(72)
    digits = list(enumerate(main)) + [(8 + i, c) for i, c in enumerate(clk.replace(":", ""))]
    for k, ch in digits:
        m = SEG_MASKS[ch]
        for on, (i, b) in ((m & 0x20, (k, 0x80)), (m & 0x40, (k, 0x08)), (m & 0x01, (18 + k, 0x80)), (m & 0x02, (18 + k, 0x08)),
                           (m & 0x10, (36 + k, 0x80)), (m & 0x04, (36 + k, 0x08)), (m & 0x08, (54 + k, 0x80))):
            if on:
                fg[i] |= b
    if clk:
        for i, b in CLK_COLONS:
            fg[i] |= b
    return bytes(fg)


def seg_text(fg):
    """段码屏读成 "主区 小时钟"; 有 seg_render 画不出来的位 (多亮 / 少亮的段) 就原样给十六进制"""
    main = "".join(SEG_CHARS.get(fg_digit_mask(fg, k), "?") for k in range(6))
    clk = "".join(SEG_CHARS.get(fg_digit_mask(fg, 8 + k), "?") for k in range(6))
    clk = "" if clk == " " * 6 else "%s:%s:%s" % (clk[0:2], clk[2:4], clk[4:6])
    try:
        if seg_render(main, clk) == bytes(fg):
            return (main + " " + clk).rstrip()
    except KeyError:
        pass
    return "raw:" + bytes(fg).hex()


def hms(sod):
    return "%02d:%02d:%02d" % (sod // 3600, sod // 60 % 60, sod % 60)


def local_sod(now, zone):
    t = datetime.datetime.fromtimestamp(now, zoneinfo.ZoneInfo(zone))
    return t.hour * 3600 + t.minute * 60 + t.second


def led_state(st):
    """灯状态机 (led.h) 归一: 灭 [0, 输出灭了]; 常亮 [1, mask, timed, 输出=mask];
    闪烁 [2, mask, on, off, timed, 相位对 (输出 = lit ? mask : 0, 下次翻转在这一段时长以内)]"""
    mode, mask, out = st["led_mode"], st["led_mask"], st["led"]
    if mode == 0:
        return [0, out == 0]
    if mode == 1:
        return [1, mask, st["led_timed"], out == mask]
    span = st["led_on"] if st["led_lit"] else st["led_off"]
    phase = out == (mask if st["led_lit"] else 0) and 0 < st["led_next_in"] <= span
    return [mode, mask, st["led_on"], st["led_off"], st["led_timed"], phase]


class Probe:
    """脚本走到某一步时看一眼模拟器里固件的状态, 归一成和时间无关的值 (self.out[名字])"""

    def __init__(self, sim, cfg):
        self.sim, self.cfg, self.out = sim, cfg, {}
        self.want_sod = local_sod(cfg["now"], cfg["zone"])
        self.hello_at = self.seg_at = self.quiet_at = None

    def __call__(self, label):
        st = self.sim.stat()
        t = time.monotonic()
        fg = bytes.fromhex(st["fg"])
        clock = st["clk_valid"] == 1 and seg_text(fg) == "------ " + hms(st["clk_sod"])
        if label == "boot":                                  # 上位机连上之前
            v = dict(seg=seg_text(fg), clk_valid=st["clk_valid"], led=led_state(st), backlight=[st["backlight"], st["bl_writes"]],
                     link_up=st["link_up"], boot_live=st["boot_live"], boot_screen=st["boot_screen"], asleep=st["asleep"],
                     seg_en=st["seg_en"])
        elif label == "hello":                               # 第一条 HELLO (带时间) 之后
            d = (st["clk_sod"] - self.want_sod) % 86400       # HELLO 之后过了不到 1 s
            self.hello_at = (t, st["clk_sod"])
            v = dict(clk=True if st["clk_valid"] and d <= 2 else [st["clk_valid"], hms(st["clk_sod"]), hms(self.want_sod)],
                     seg_clock=clock or seg_text(fg), led=led_state(st), key_n=st["key_n"], link_up=st["link_up"],
                     boot_live=st["boot_live"])
        elif label == "seg":                                 # SEG (图案) + SEG (71 B, 被拒) 之后
            self.seg_at = t
            v = dict(fg=True if fg == SEG_PAT else fg.hex(), seg_host_seen=st["seg_host_seen"])
        elif label == "keys":                                # 按键注入完 (SEG 之后 1.5 s 多): 主机的 SEG 还没被本地时钟盖掉
            dt = t - self.seg_at
            v = dict(seg_held=(fg == SEG_PAT) if 1.05 < dt < 4.8 else "SEG 之后 %.2f s, 不在 (1.05, 4.8)" % dt)
        elif label == "backlight":                           # BACKLIGHT 0 + 空 BACKLIGHT (被拒)
            v = [st["backlight"], st["bl_writes"]]
        elif label == "led":                                 # 绿灯闪 500/100 + 两条被拒的
            v = led_state(st)
        elif label == "imm_anim":                            # IMM_SCROLL 300 -> 320, 100 ms 刚 ACK
            v = [st["im_ytgt"], st["im_y0"], st["im_d"], st["im_dur"], 300 <= st["im_pos"] <= 320]
        elif label == "rehello":                             # 不带时间的 HELLO: KEY 计数清零、灯交还主机、时钟照走
            self.quiet_at = t                                # 上位机从这里起不发了 (fw v10: 5 s 后算走了)
            v = dict(key_n=st["key_n"], led=led_state(st), clk_valid=st["clk_valid"], link_up=st["link_up"])
        elif label == "lost":                                # fw v10: 5 s 没有效帧 -> 开机版权页、段码屏本地时钟、蓝灯心跳
            end = self.quiet_at + 9
            while st["link_up"] and time.monotonic() < end:
                time.sleep(0.05)
                st = self.sim.stat()
            dt = time.monotonic() - self.quiet_at
            fg = bytes.fromhex(st["fg"])
            clock = st["clk_valid"] == 1 and seg_text(fg) == "------ " + hms(st["clk_sod"])
            v = dict(after=True if 4.6 <= dt <= 6.2 else round(dt, 2), link_up=st["link_up"], boot_screen=st["boot_screen"],
                     boot_live=st["boot_live"], seg_clock=clock or seg_text(fg), led=led_state(st), backlight=st["backlight"],
                     asleep=st["asleep"])
        elif label == "sleep":                               # SLEEP: 背光 / 段码屏 EN / 灯全关, 本地时钟不刷段码屏
            w0 = st["seg_writes"]
            time.sleep(1.2)
            st2 = self.sim.stat()
            v = dict(asleep=st["asleep"], backlight=st["backlight"], seg_en=st["seg_en"], seg_off=st["seg_off"], led=led_state(st),
                     link_up=st["link_up"], seg_quiet=st2["seg_writes"] == w0 and st2["asleep"] == 1)
        elif label == "wake":                                # 上位机醒来重新 HELLO (带时间): 全开, Link 行 OK, 时钟重新对过
            d = (st["clk_sod"] - self.want_sod) % 86400
            self.hello_at = (t, st["clk_sod"])
            v = dict(asleep=st["asleep"], backlight=st["backlight"], seg_en=st["seg_en"], seg_off=st["seg_off"], link_up=st["link_up"],
                     boot_live=st["boot_live"], led=led_state(st), clk=True if d <= 2 else hms(st["clk_sod"]),
                     seg_clock=clock or seg_text(fg))
        elif label.startswith("lz4_"):                       # RECT_LZ4 段: [成功的 RECT_LZ4 条数, bad_param] (HELLO 起算)
            if label.startswith("lz4_hello"):                # 段里的 HELLO 带固定时刻: 时钟从这里重新算
                self.hello_at = (t, st["clk_sod"])
            v = [st["lz4_rects"], st["bad_param"]]
        elif label == "end":                                 # 会话结束: 时钟按秒走; SEG 5 s 没续就回本地时钟
            if self.seg_at is not None:
                time.sleep(max(0.0, self.seg_at + 6.2 - time.monotonic()))
                st = self.sim.stat()
                fg, t = bytes.fromhex(st["fg"]), time.monotonic()
                clock = st["clk_valid"] == 1 and seg_text(fg) == "------ " + hms(st["clk_sod"])
            if self.hello_at is None:
                self.out[label] = "没有 hello 那一步"
                return
            t0, s0 = self.hello_at
            d = (st["clk_sod"] - s0) % 86400
            v = dict(tick=True if abs(d - (t - t0)) <= 1.5 else [d, round(t - t0, 2)], seg_clock=clock or seg_text(fg))
        else:
            raise ValueError(label)
        self.out[label] = v


# ---- 模拟器 ----

class Sim:
    def __init__(self, exe, args):
        self.p = subprocess.Popen([exe] + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        line = self.p.stdout.readline().split()
        if len(line) != 2 or line[0] != "PTY":
            raise RuntimeError("fwsim 没有给出伪终端: %r" % line)
        self.pty = line[1]

    def cmd(self, s):
        self.p.stdin.write(s + "\n")
        self.p.stdin.flush()
        return self.p.stdout.readline().strip()

    def stat(self):
        return json.loads(self.cmd("stat"))

    def close(self):
        try:
            self.p.stdin.write("quit\n")
            self.p.stdin.flush()
        except BrokenPipeError:
            pass
        self.p.wait(timeout=5)


# ---- Python 上位机跑的脚本 (和 Conformance.swift 一步一步对应) ----

def errs(a):
    return a["crc_err"] + a["len_err"] + a["bad_param"]


def python_session(sim, probe, now):
    link = ml.Link(sim.pty)
    link.clock = lambda: now
    res = {}
    info = link.hello()
    probe("hello")
    res["hello"] = {k: info[k] for k in ("proto", "fw", "w", "h", "pixfmt", "seg_bytes", "caps", "max_payload")}
    base = [0]

    def push(fb, rects, fid, copies=()):
        a = link.push(fb, rects, fid, copies)
        ok = a["status"] == ml.OK and errs(a) == base[0]
        base[0] = errs(a)
        return ok

    def fbcrc(x, y, w, h):
        return link.call(ml.FBCRC, struct.pack("<HHHH", x, y, w, h), timeout=5)

    def diag(pl):
        p = link.wait(ml.DIAG_R, link.send(ml.DIAG, bytes(pl)), 10)
        return [p[0], p[1], p[2] | p[3] << 8]

    def delta(junk, sleep=0):
        a = link.ping()
        link.ser.write(bytes(junk))
        if sleep:
            time.sleep(sleep)
        b = link.ping()
        return [b["frames"] - a["frames"], b["crc_err"] - a["crc_err"], b["len_err"] - a["len_err"], b["rx_bytes"] - a["rx_bytes"]]

    p0 = link.ping()
    res["ping"] = {k: p0[k] for k in ("for_type", "status", "frames", "rx_bytes", "crc_err", "len_err")}

    a = frame_a()
    full = push(a, ml.full_rects(max_payload=link.max_rect_payload()), 1)
    pf = link.ping()
    res["full"] = dict(ok=full, rects=pf["rects"], presents=pf["presents"],
                       fbcrc_ok=fbcrc(0, 0, W, H)["extra"] == region_crc(a, 0, 0, W, H))

    b = bytearray(a)
    copy = (0, 108, 800, 592, 0, 100)
    ml.apply_copy(b, *copy)
    rects = [(0, 692, 800, 8), (41, 40, 33, 17)]
    for (x0, y0, w, h) in rects:
        for y in range(y0, y0 + h):
            b[(y * W + x0) * 2:(y * W + x0 + w) * 2] = le16(pat_b(x, y) for x in range(x0, x0 + w))
    diff_ok = push(b, rects, 2, copies=[copy])
    res["diff"] = dict(ok=diff_ok, rects=link.ping()["rects"] - pf["rects"],
                       fbcrc_ok=fbcrc(0, 0, W, H)["extra"] == region_crc(b, 0, 0, W, H),
                       sub_fbcrc_ok=fbcrc(41, 40, 33, 17)["extra"] == region_crc(b, 41, 40, 33, 17))
    res["fbcrc_bad"] = fbcrc(799, 0, 2, 1)["status"]

    def rehello():
        link.hello()
        base[0] = 0                                           # HELLO 清了设备的计数
    res["lz4"] = lz4_section(
        lambda fb, full: push(fb, ml.full_rects(max_payload=link.max_rect_payload()) if full else [], 20),
        lambda: fbcrc(0, 0, W, H)["extra"], lambda: link.send(ml.RECT_LZ4, BAD_LZ4), rehello, probe)

    res["seg"] = [link.call(ml.SEG, SEG_PAT)["status"], link.call(ml.SEG, bytes(71))["status"]]
    probe("seg")
    res["backlight"] = [link.call(ml.BACKLIGHT, b"\x00")["status"], link.call(ml.BACKLIGHT)["status"]]
    probe("backlight")
    res["led"] = [link.led(ml.LED_G, ml.LED_BLINK, 500, 100)["status"],
                  link.call(ml.LED, bytes([8, 1, 0, 0, 0, 0, 0, 0]))["status"],
                  link.call(ml.LED, bytes([1, 2, 100, 0, 100, 0, 0, 0]))["status"]]
    probe("led")

    keys = []
    for k, ms, n in ((3, 60, 2), (1, 1300, 5)):
        sim.cmd("tap %d %d" % (k, ms))
        end = time.monotonic() + 3
        while len(keys) < n and time.monotonic() < end:
            keys += [[e["key"], e["ev"], e["n"]] for e in link.poll_keys(0.05)]
    res["keys"] = keys
    probe("keys")

    u = link.call(0x55)
    res["unknown"] = dict(for_type=u["for_type"], status=u["status"])
    res["diag"] = dict(flip=diag([11, 1]), op8=diag([8]), empty=diag([]), tri=diag([31]))
    res["diag"]["set_flip_now"] = link.diag(ml.DG_FLIPMODE, b"\x01")[0] == ml.OK

    bad = bytearray(ml.encode(ml.PING, 0x33)); bad[-1] ^= 0xFF
    mp = info["max_payload"] + 1
    swallow = bytes([0xA5, 0x5A, ml.PING, 0x34, 20, 0]) + ml.encode(ml.PING, 0x35) + bytes(14)
    res["errors"] = dict(bad_crc=delta(bad), oversize=delta([0xA5, 0x5A, ml.RECT, 0x36, mp & 0xFF, mp >> 8]),
                         swallow=delta(swallow), garbage=delta([0x00, 0x11, 0xA5, 0x00, 0x5A]),
                         half=delta([0xA5, 0x5A, ml.PING, 0x37, 4, 0, 1, 2], sleep=0.7))

    if info["caps"] & ml.CAP_IMMERSIVE:
        res["imm"] = python_imm(link, push, fbcrc, a, base, probe)

    link.wait(ml.HELLO_ACK, link.send(ml.HELLO))
    p2 = link.ping()
    res["rehello"] = dict(ok=True, frames=p2["frames"], rx_bytes=p2["rx_bytes"])
    probe("rehello")

    if info["caps"] & ml.CAP_SLEEP:                           # fw v10: 不发了 -> 走了; SLEEP; 醒来重新 HELLO
        probe("lost")
        res["sleep"] = [link.call(ml.SLEEP, b"\x00")["status"], link.sleep()["status"]]
        probe("sleep")
        link.hello()
        p3 = link.ping()
        res["wake"] = dict(frames=p3["frames"], rx_bytes=p3["rx_bytes"])
        probe("wake")
    link.close()
    return res


def python_imm(link, push, fbcrc, a, base, probe):
    r = {}
    e = link.imm_enter(0)
    r["enter"] = [e["status"], e["extra"]]
    if e["status"] != ml.OK:
        return r
    R = e["extra"]
    rows = {}
    zero = bytes(W * 2)

    def write(y, h, visible, pat, record=True):
        buf = le16(pat(y + i, x) for i in range(h) for x in range(W))
        link.imm_write(y, buf, visible=visible)
        if record:
            for i in range(h):
                rows[y + i] = buf[i * W * 2:(i + 1) * W * 2]

    def window(y):
        return b"".join(rows.get(y + i, zero) for i in range(H))

    def crc_is(y):
        return fbcrc(0, 0, W, H)["extra"] == zlib.crc32(window(y)) & 0xFFFFFFFF

    def scroll(y, dur):
        s = link.imm_scroll(y, dur)
        return [s["status"], s["extra"]]

    p0 = link.ping()
    base[0] = errs(p0)                                        # 基线对齐: push 只能靠 status 发现被拒
    r["present_status"] = link.call(ml.PRESENT, b"\0\0")["status"]
    r["push_ok"] = push(a, [], 9)
    link.send(ml.RECT, ml.rect_payload(a, 0, 0, 1, 1))
    link.send(ml.COPY, ml.copy_payload(0, 0, 1, 1, 1, 1))
    p1 = link.ping()
    r["plain_writes"] = [p1["bad_param"] - p0["bad_param"], p1["rects"] - p0["rects"]]
    r["flip_ok"] = link.diag(ml.DG_FLIPMODE, b"\x01")[0] == ml.OK

    write(0, 1600, False, pat_row)
    r["scroll0"] = scroll(0, 0); r["crc0"] = crc_is(0)
    r["scroll300"] = scroll(300, 0); r["crc300"] = crc_is(300)
    p2 = link.ping()
    write(400, 10, False, pat_vis, record=False)
    write(400, 10, True, pat_vis)
    p3 = link.ping()
    r["vis"] = [p3["bad_param"] - p2["bad_param"], p3["rects"] - p2["rects"]]
    r["anim"] = scroll(320, 100)
    probe("imm_anim")
    time.sleep(0.3)
    r["crc320"] = crc_is(320)
    write(1600, R + 200 - 1600, False, pat_row)
    p4 = link.ping()
    r["far"] = [p4["bad_param"] - p3["bad_param"], p4["rects"] - p3["rects"]]
    r["scroll1600"] = scroll(1600, 0); r["crc1600"] = crc_is(1600)
    r["out"] = scroll(R + 2000, 0)[0]
    r["sub"] = fbcrc(5, 7, 33, 17)["extra"] == region_crc(window(1600), 5, 7, 33, 17)
    x = link.imm_exit()
    r["exit"] = [x["status"], x["extra"]]
    r["crc_exit"] = crc_is(1600)
    r["caps_after"] = link.info["caps"]
    base[0] = errs(link.ping())
    ok = push(a, ml.full_rects(max_payload=link.max_rect_payload()), 10)
    r["after"] = [ok, fbcrc(0, 0, W, H)["extra"] == region_crc(a, 0, 0, W, H)]
    return r


# ---- Linux 上位机 (linux/monita_linux/session.py): 走它自己的 Session.push (切条带 / caps 门控 / 推坏了关 LZ4) ----

def linux_session(sim, probe, now):
    sys.path.insert(0, os.path.join(ROOT, "linux"))
    import logging
    logging.getLogger("monita").addHandler(logging.NullHandler())   # "推坏了关 LZ4" 是脚本故意造的, 别刷屏
    logging.getLogger("monita").propagate = False
    from monita_linux.session import Session
    s = Session(lambda: ml.open_port(sim.pty), clock=lambda: now, stale_after=None)
    if not s.connect_if_needed():
        return {"error": "HELLO 失败"}
    probe("hello")
    res = {"hello": {k: s.info[k] for k in ("proto", "fw", "w", "h", "pixfmt", "seg_bytes", "caps", "max_payload")}}
    maxp = lambda: min(s.info["max_payload"], 32768)

    def rehello():
        s.forced = True
        s.connect_if_needed()
    res["lz4"] = lz4_section(
        lambda fb, full: s.push(fb, ml.full_rects(max_payload=maxp()) if full else [], 20),
        lambda: s.link.call(ml.FBCRC, struct.pack("<HHHH", 0, 0, W, H), timeout=5)["extra"],
        lambda: s.link.send(ml.RECT_LZ4, BAD_LZ4), rehello, probe)
    s.link.close()
    return res


# ---- Swift 上位机 ----

def swift_session(sim, probe, now, zone, exe):
    env = dict(os.environ, TZ=zone, MONITA_FAKE_NOW=str(now))
    p = subprocess.Popen([exe, "--device", sim.pty, "--conformance"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         text=True, env=env)
    res = None
    for line in p.stdout:
        if line.startswith("@key "):
            _, k, ms = line.split()
            sim.cmd("tap %s %s" % (k, ms))
        elif line.startswith("@stat "):
            probe(line.split()[1])
            p.stdin.write("\n")
            p.stdin.flush()
        elif line.startswith("@result "):
            res = json.loads(line[len("@result "):])
        else:
            sys.stdout.write("    swift| " + line)
    p.wait(timeout=30)
    if res is None:
        raise RuntimeError("Swift 没有给出结果 (exit %d)" % p.returncode)
    return res


# ---- 照 docs/protocol.md 应该得到的 ----

def expected(cfg):
    nbuf, mp, rows = cfg["nbuf"], cfg["max_payload"], cfg["imm_rows"]
    caps = ml.CAP_FBCRC | ml.CAP_COPY | ml.CAP_KEYS | ml.CAP_LED | ml.CAP_IMMERSIVE | ml.CAP_SLEEP
    caps |= ml.CAP_DOUBLEBUF if nbuf > 1 else 0
    caps |= ml.CAP_TRIBUF if nbuf > 2 else 0
    lz = cfg.get("lz4", True)
    caps |= ml.CAP_LZ4 if lz else 0
    strip_rows = (min(mp, 32768) - 8) // (W * 2)
    n = -(-H // strip_rows)                                   # 整帧条数; mixed 里正好一半压缩
    e = {
        "hello": dict(proto=0, fw=cfg["fw"], w=800, h=1280, pixfmt=1, seg_bytes=72, caps=caps, max_payload=mp),
        # HELLO 清计数: 之后第一条 PING 时 frames = 2 (HELLO + PING), rx_bytes = 这条 PING 的 10 字节
        "ping": dict(for_type=ml.PING, status=0, frames=2, rx_bytes=10, crc_err=0, len_err=0),
        "full": dict(ok=True, rects=-(-H // strip_rows), presents=1, fbcrc_ok=True),
        "diff": dict(ok=True, rects=3, fbcrc_ok=True, sub_fbcrc_ok=True),   # COPY 也计入 rects
        "fbcrc_bad": ml.EPARAM,
        "seg": [0, 1], "backlight": [0, 1], "led": [0, 1, 1],
        # KEY: HELLO 后 n 从 0 数; 长按满 1 s 报 LONG, 之后照样 UP
        "keys": [[3, 1, 0], [3, 2, 1], [1, 1, 2], [1, 3, 3], [1, 2, 4]],
        "unknown": dict(for_type=0x55, status=ml.EUNSUP),
        # DIAG: op 8 没有 (EUNSUP); 空 payload -> op 0 EPARAM; 31 回 15 个字
        "diag": dict(flip=[11, 0, 1], op8=[8, 3, 0], empty=[0, 1, 0], tri=[31, 0, 15], set_flip_now=True),
        # [frames, crc_err, len_err, rx_bytes] 的增量 (后一条 PING 自己算 1 帧 10 字节)
        "errors": dict(bad_crc=[1, 1, 0, 20],
                       oversize=[1, 0, 1, 16],    # 长度超限: 读完 4 字节头就重新找同步
                       swallow=[1, 1, 0, 40],     # CRC 错的帧按 len 读完才重新找同步: 夹在里面的 PING 被吞掉
                       garbage=[1, 0, 0, 15],
                       half=[1, 0, 0, 18]),       # 半截帧 0.5 s 没后续就丢
        "rehello": dict(ok=True, frames=2, rx_bytes=10),
        # fw v11 RECT_LZ4 (lz4_section): 坏块计 bad_param、屏上不动, 这一批推坏了 (fail) -> 上位机这次连接里只发 RECT
        # (再 HELLO 也不恢复, lz4_again 里 0 条 RECT_LZ4; 重新打开端口 / fw 变了才再试, 见各上位机单测)。
        # 旧固件 (--no-lz4): 一条 RECT_LZ4 都不发; 脚本自己塞的坏块被当成不认识的 type (回 EUNSUP, 不计错), 每批都推成
        "lz4": dict(full=[True, True], mixed=[True, True], bad=[not lz, True], fail=not lz, recover=[True, True],
                    again=[True, True]),
        # fw v10: 带载荷的 SLEEP 被拒, 空的 OK; 醒来 HELLO 清计数
        "sleep": [ml.EPARAM, ml.OK], "wake": dict(frames=2, rx_bytes=10),
        # 模拟器里固件的副作用 (Probe): 开机蓝灯 2 s 亮 1 s 灭 (1.024 ms 单位 2000/1000)、段码屏 "------";
        # HELLO 后小时钟 = 固定时刻在 zone 的当地时间, 灯灭; SEG 原样进显存; LED 生效、被拒的不动它;
        # 再 HELLO: KEY 计数清零、灯灭; 时钟按秒走, SEG 5 s 后回本地时钟
        "sim": dict(lz4_hello=[0, 0], lz4_full=[n if lz else 0, 0], lz4_mixed=[n * 3 // 2 if lz else 0, 0],
                    lz4_bad=[n * 3 // 2, 1] if lz else [0, 0], lz4_fail=[n * 5 // 2, 2] if lz else [0, 0],
                    lz4_recover=[n * 5 // 2, 2] if lz else [0, 0], lz4_hello2=[0, 0], lz4_again=[0, 0],
                    boot=dict(seg="------", clk_valid=0, led=[2, ml.LED_B, 2000, 1000, 0, True], backlight=[1, 1],
                              link_up=0, boot_live=1, boot_screen=1, asleep=0, seg_en=1),
                    hello=dict(clk=True, seg_clock=True, led=[0, True], key_n=0, link_up=1, boot_live=0),
                    seg=dict(fg=True, seg_host_seen=1), keys=dict(seg_held=True), backlight=[0, 2], led=[2, ml.LED_G, 100, 400, 0, True],
                    rehello=dict(key_n=0, led=[0, True], clk_valid=1, link_up=1), end=dict(tick=True, seg_clock=True),
                    # fw v10: 5 s 没帧 -> 开机版权页 (屏上 CRC = 开机时那一屏)、段码屏本地时钟、蓝灯心跳, 背光照开
                    lost=dict(after=True, link_up=0, boot_screen=1, boot_live=1, seg_clock=True,
                              led=[2, ml.LED_B, 2000, 1000, 0, True], backlight=1, asleep=0),
                    sleep=dict(asleep=1, backlight=0, seg_en=0, seg_off=1, led=[0, True], link_up=0, seg_quiet=True),
                    wake=dict(asleep=0, backlight=1, seg_en=1, seg_off=0, link_up=1, boot_live=0, led=[0, True], clk=True,
                              seg_clock=True)),
    }
    if rows < 1280 + 256:
        e["imm"] = {"enter": [ml.EUNSUP, 0]}
    else:
        e["imm"] = dict(enter=[0, rows], present_status=ml.EPARAM, push_ok=False, plain_writes=[2, 0], flip_ok=False,
                        scroll0=[0, 0], crc0=True, scroll300=[0, 0], crc300=True, vis=[1, 1], anim=[0, 300], crc320=True,
                        far=[0, strips(rows + 200 - 1600, mp)], scroll1600=[0, 320], crc1600=True, out=ml.EPARAM, sub=True,
                        exit=[0, caps], crc_exit=True, caps_after=caps, after=[True, True])
        e["sim"]["imm_anim"] = [320, 300, 20, 100, True]
    return e


def diff(want, got, path=""):
    out = []
    if isinstance(want, dict) and isinstance(got, dict):
        for k in sorted(set(want) | set(got)):
            if k not in got:
                out.append("%s.%s: 缺" % (path, k))
            elif k not in want:
                out.append("%s.%s: 多了 %r" % (path, k, got[k]))
            else:
                out += diff(want[k], got[k], "%s.%s" % (path, k))
    elif want != got:
        out.append("%s: 应为 %r, 实际 %r" % (path, want, got))
    return out


# now / zone: HELLO 带的时间 (固定, 不用真实时间)。西半球夏令时跨日、半小时时区、东半球夏令时 (南半球一月)、+1 h
CONFIGS = [
    dict(name="三缓冲", args=[], nbuf=3, max_payload=32768, imm_rows=2720,
         now=1784081587, zone="America/New_York"),            # 2026-07-15 02:13:07Z = 前一天 22:13:07 EDT (-240)
    dict(name="单缓冲", args=["--nbuf", "1"], nbuf=1, max_payload=32768, imm_rows=2720,
         now=1784141251, zone="Asia/Kolkata"),                # 18:47:31Z = 次日 00:17:31 IST (+330)
    dict(name="双缓冲 + max_payload 16 KiB", args=["--nbuf", "2", "--max-payload", "16384"], nbuf=2, max_payload=16384,
         imm_rows=2720, now=1784141251, zone="Europe/London"), # 19:47:31 BST (+60)
    dict(name="卷轴不够 (IMM_ENTER 被拒)", args=["--imm-rows", "1000"], nbuf=3, max_payload=32768, imm_rows=1000,
         now=1768950309, zone="Australia/Adelaide"),          # 2026-01-20 23:05:09Z = 次日 09:35:09 ACDT (+630)
    dict(name="旧固件 (没有 RECT_LZ4)", args=["--no-lz4"], nbuf=3, max_payload=32768, imm_rows=2720, lz4=False,
         now=1784081587, zone="Asia/Tokyo"),                  # 11:13:07 JST (+540)
]

LINUX_KEYS = ("hello", "lz4")                                 # Linux 上位机只跑这几段 (+ sim 里的 boot / hello / lz4_* / end)


def linux_subset(e):
    out = {k: e[k] for k in LINUX_KEYS}
    out["sim"] = {k: v for k, v in e["sim"].items() if k in ("boot", "hello", "end") or k.startswith("lz4_")}
    return out


def fw_ver():
    for line in open(os.path.join(ROOT, "fw", "app.c"), encoding="utf-8"):
        if line.startswith("#define FW_VER"):
            return int(line.split()[2].rstrip("u"), 16)
    raise RuntimeError("fw/app.c 里没有 FW_VER")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim", default=os.path.join(ROOT, "tools", "fwsim", "build", "fwsim"))
    ap.add_argument("--swift", help="monita-cs80 (debug) 路径; 不给就只测 Python (+ Linux)")
    ap.add_argument("--no-linux", action="store_true", help="不跑 Linux 上位机 (linux/) 那一段")
    args = ap.parse_args()
    if not ml.lz4_available():                                # 上位机里是可选依赖, 对拍的期望值按"装了"写
        print("对拍要 python lz4 (RECT_LZ4 段): %s -m pip install lz4 (run.sh 用 python3 -I, 只认系统 / venv 的 site-packages)"
              % sys.executable)
        return 2
    fails = 0
    for cfg in CONFIGS:
        cfg["fw"] = fw_ver()
        want = expected(cfg)
        got = {}
        os.environ["TZ"] = cfg["zone"]                        # Python 上位机按这个时区算 HELLO 的 tz
        time.tzset()
        hosts = [("python", lambda s, p: python_session(s, p, cfg["now"]))]
        if args.swift:
            hosts.append(("swift", lambda s, p: swift_session(s, p, cfg["now"], cfg["zone"], args.swift)))
        if not args.no_linux:
            hosts.append(("linux", lambda s, p: linux_session(s, p, cfg["now"])))
        for host, run in hosts:
            sim = Sim(args.sim, cfg["args"])
            t0 = time.monotonic()
            try:
                probe = Probe(sim, cfg)
                probe("boot")
                got[host] = run(sim, probe)
                probe("end")
                got[host]["sim"] = probe.out
                st = sim.stat()
            finally:
                sim.close()
            d = diff(linux_subset(want) if host == "linux" else want, got[host])
            # 模拟器这边: 只有 op 8 那一条旧 DIAG 收拢过多缓冲 (空 DIAG 不算); 最后回到普通模式、块数复原
            for k, v in (("collapse", 1), ("imm", 0), ("nbuf", cfg["nbuf"])):
                if host == "linux" and k == "collapse":           # Linux 段里没有旧 DIAG
                    v = 0
                if st[k] != v:
                    d.append("模拟器 %s: 应为 %r, 实际 %r" % (k, v, st[k]))
            print("%-4s %-7s %-28s %.1f s" % ("OK" if not d else "FAIL", host, cfg["name"], time.monotonic() - t0))
            for line in d:
                print("       " + line)
            fails += len(d)
        if "swift" in got:
            d = diff(got["python"], got["swift"])
            print("%-4s python == swift (%s)" % ("OK" if not d else "FAIL", cfg["name"]))
            for line in d:
                print("       " + line)
            fails += len(d)
        if "linux" in got:                                    # Linux 跑的那几段和 Python (和 Swift) 逐项一样
            for other in [h for h in ("python", "swift") if h in got]:
                d = diff(linux_subset(got[other]), got["linux"])
                print("%-4s %s == linux (%s)" % ("OK" if not d else "FAIL", other, cfg["name"]))
                for line in d:
                    print("       " + line)
                fails += len(d)
    print("conformance: %s" % ("all OK" if not fails else "%d 处不一致" % fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
