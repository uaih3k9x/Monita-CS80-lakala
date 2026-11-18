#!/usr/bin/env python3
"""上位机对照测试: 固件模拟器 (fwsim) × Python 参考实现 (tools/host-ref) × Swift (mac/, monita-cs80 --conformance)。

每种模拟器配置起一个干净的 fwsim, 两个上位机各跑一遍同样的脚本 (HELLO、整帧 / COPY / FBCRC、SEG、LED、
按键注入、未知 type、DIAG、坏帧、沉浸模式 ...), 结果和 expected() (照 docs/protocol.md 写的) 逐项对比,
两个上位机的结果也要完全一样。一般用 tools/fwsim/run.sh 跑。

  python3 tools/fwsim/conformance.py --sim tools/fwsim/build/fwsim [--swift mac/.build/debug/monita-cs80]
"""
import argparse
import array
import json
import os
import struct
import subprocess
import sys
import time
import zlib

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


def region_crc(fb, x, y, w, h, width=W):
    c = 0
    for r in range(y, y + h):
        c = zlib.crc32(fb[((r * width) + x) * 2:((r * width) + x + w) * 2], c)
    return c & 0xFFFFFFFF


def strips(h, max_payload):
    return len(ml.imm_strips(0, h, W, max_payload))


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


def python_session(sim):
    link = ml.Link(sim.pty)
    res = {}
    info = link.hello()
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

    res["seg"] = [link.call(ml.SEG, bytes(72))["status"], link.call(ml.SEG, bytes(71))["status"]]
    res["backlight"] = [link.call(ml.BACKLIGHT, b"\x01")["status"], link.call(ml.BACKLIGHT)["status"]]
    res["led"] = [link.led(ml.LED_G, ml.LED_BLINK, 500, 100)["status"],
                  link.call(ml.LED, bytes([8, 1, 0, 0, 0, 0, 0, 0]))["status"],
                  link.call(ml.LED, bytes([1, 2, 100, 0, 100, 0, 0, 0]))["status"]]

    keys = []
    for k, ms, n in ((3, 60, 2), (1, 1300, 5)):
        sim.cmd("tap %d %d" % (k, ms))
        end = time.monotonic() + 3
        while len(keys) < n and time.monotonic() < end:
            keys += [[e["key"], e["ev"], e["n"]] for e in link.poll_keys(0.05)]
    res["keys"] = keys

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
        res["imm"] = python_imm(link, push, fbcrc, a, base)

    link.wait(ml.HELLO_ACK, link.send(ml.HELLO))
    p2 = link.ping()
    res["rehello"] = dict(ok=True, frames=p2["frames"], rx_bytes=p2["rx_bytes"])
    link.close()
    return res


def python_imm(link, push, fbcrc, a, base):
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


# ---- Swift 上位机 ----

def swift_session(sim, exe):
    p = subprocess.Popen([exe, "--device", sim.pty, "--conformance"], stdout=subprocess.PIPE, text=True)
    res = None
    for line in p.stdout:
        if line.startswith("@key "):
            _, k, ms = line.split()
            sim.cmd("tap %s %s" % (k, ms))
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
    caps = ml.CAP_FBCRC | ml.CAP_COPY | ml.CAP_KEYS | ml.CAP_LED | ml.CAP_IMMERSIVE
    caps |= ml.CAP_DOUBLEBUF if nbuf > 1 else 0
    caps |= ml.CAP_TRIBUF if nbuf > 2 else 0
    strip_rows = (min(mp, 32768) - 8) // (W * 2)
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
    }
    if rows < 1280 + 256:
        e["imm"] = {"enter": [ml.EUNSUP, 0]}
    else:
        e["imm"] = dict(enter=[0, rows], present_status=ml.EPARAM, push_ok=False, plain_writes=[2, 0], flip_ok=False,
                        scroll0=[0, 0], crc0=True, scroll300=[0, 0], crc300=True, vis=[1, 1], anim=[0, 300], crc320=True,
                        far=[0, strips(rows + 200 - 1600, mp)], scroll1600=[0, 320], crc1600=True, out=ml.EPARAM, sub=True,
                        exit=[0, caps], crc_exit=True, caps_after=caps, after=[True, True])
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


CONFIGS = [
    dict(name="三缓冲", args=[], nbuf=3, max_payload=32768, imm_rows=2720),
    dict(name="单缓冲", args=["--nbuf", "1"], nbuf=1, max_payload=32768, imm_rows=2720),
    dict(name="双缓冲 + max_payload 16 KiB", args=["--nbuf", "2", "--max-payload", "16384"], nbuf=2, max_payload=16384, imm_rows=2720),
    dict(name="卷轴不够 (IMM_ENTER 被拒)", args=["--imm-rows", "1000"], nbuf=3, max_payload=32768, imm_rows=1000),
]


def fw_ver():
    for line in open(os.path.join(ROOT, "fw", "app.c"), encoding="utf-8"):
        if line.startswith("#define FW_VER"):
            return int(line.split()[2].rstrip("u"), 16)
    raise RuntimeError("fw/app.c 里没有 FW_VER")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim", default=os.path.join(ROOT, "tools", "fwsim", "build", "fwsim"))
    ap.add_argument("--swift", help="monita-cs80 (debug) 路径; 不给就只测 Python")
    args = ap.parse_args()
    fails = 0
    for cfg in CONFIGS:
        cfg["fw"] = fw_ver()
        want = expected(cfg)
        got = {}
        hosts = [("python", python_session)] + ([("swift", lambda s: swift_session(s, args.swift))] if args.swift else [])
        for host, run in hosts:
            sim = Sim(args.sim, cfg["args"])
            t0 = time.monotonic()
            try:
                got[host] = run(sim)
                st = sim.stat()
            finally:
                sim.close()
            d = diff(want, got[host])
            # 模拟器这边: 只有 op 8 那一条旧 DIAG 收拢过多缓冲 (空 DIAG 不算); 最后回到普通模式、块数复原
            for k, v in (("collapse", 1), ("imm", 0), ("nbuf", cfg["nbuf"])):
                if st[k] != v:
                    d.append("模拟器 %s: 应为 %r, 实际 %r" % (k, v, st[k]))
            print("%-4s %-7s %-28s %.1f s" % ("OK" if not d else "FAIL", host, cfg["name"], time.monotonic() - t0))
            for line in d:
                print("       " + line)
            fails += len(d)
        if len(got) == 2:
            d = diff(got["python"], got["swift"])
            print("%-4s python == swift (%s)" % ("OK" if not d else "FAIL", cfg["name"]))
            for line in d:
                print("       " + line)
            fails += len(d)
    print("conformance: %s" % ("all OK" if not fails else "%d 处不一致" % fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
