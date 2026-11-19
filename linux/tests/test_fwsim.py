"""Linux 上位机 × 固件模拟器（tools/fwsim）对拍：Session + App 主循环 + segframe + 真渲染器拼起来对着真固件协议代码跑。

conformance.py 的四种模拟器配置（三缓冲 / 单缓冲 / 双缓冲 + 16 KiB / 卷轴不够）各起一个 fwsim，并行跑同一套场景，
用 stat 断言（DESIGN.md §7.2）：连上、整帧和差分后屏上 CRC = 本地帧、段码屏、灯、按键（音量 / ≡ 切页 / 长按不切）、
keepalive（停 3.6 s 不回开机页）、主机走了（停 ≥ 5.5 s 回开机页，恢复后重连）、SLEEP / 唤醒、拔线重开端口。
协议编码本身由现有对拍（sh tools/fwsim/run.sh --python-only）覆盖，这里测的是 Linux 版自己的代码。

没有 cc 就 skip。单独跑：python3 linux/tests/test_fwsim.py（打印每种配置的结果）。
"""
import concurrent.futures
import os
import shutil
import subprocess
import sys
import time
import unittest
import zlib

from util import REPO

sys.path.insert(0, os.path.join(REPO, "tools", "fwsim"))
import conformance as CF  # noqa: E402
import monita_link as ml  # noqa: E402
from monita_linux.app import App  # noqa: E402
from monita_linux.schedule import position_at  # noqa: E402
from monita_linux.segframe import clock_text  # noqa: E402
from monita_linux.session import Session  # noqa: E402

SIM = os.path.join(REPO, "tools", "fwsim", "build", "fwsim")


def build_sim():
    """和 run.sh 同样的编译命令；源文件比产物新就重编"""
    srcs = [os.path.join(REPO, "tools", "fwsim", f) for f in ("fwsim.c", "simdev.h")]
    if os.path.exists(SIM) and all(os.path.getmtime(SIM) >= os.path.getmtime(s) for s in srcs):
        return
    os.makedirs(os.path.dirname(SIM), exist_ok=True)
    subprocess.run(["cc", "-std=c99", "-Wall", "-Wextra", "-Werror", "-O2", "-I" + os.path.join(REPO, "fw", "link"),
                    "-o", SIM, srcs[0]], check=True)


def make_renderer():
    """有字体用真渲染器；没有就用一个按 (页, 秒) 变色的合成画面（照样测推帧 / CRC）"""
    try:
        from monita_linux.render import Fonts, Renderer
        return Renderer(Fonts()), True
    except Exception:
        class Synth:
            progress_w = 452.0

            def render(self, page, inp):
                v = (int(inp["now"]) * 37 + (101 if page == "clock" else 0)) & 0xFFFF
                return None, bytes([v & 0xFF, v >> 8]) * (800 * 1280)
        return Synth(), False


class Lyr:
    def __init__(self, data):
        self.data = data

    def payload(self):
        return self.data

    def cover(self, sid):
        return None


class Vol:
    def __init__(self):
        self.steps = []

    def step(self, pct):
        self.steps.append(pct)


class Power:
    """记下关 fd 那一刻设备睡没睡（SLEEP 的 ACK 回来之后才能关）"""

    def __init__(self, sim):
        self.sim = sim
        self.release_asleep = []
        self.takes = 0

    def take_inhibitor(self):
        self.takes += 1
        return True

    def release(self):
        self.release_asleep.append(self.sim.stat()["asleep"])


class Flaky:
    """真端口外面包一层：fail = True 时下一次读写抛 OSError（模拟拔线后旧句柄失效）"""

    def __init__(self, port):
        self.port = port
        self.fail = False

    def _check(self):
        if self.fail:
            self.fail = False
            raise OSError(19, "No such device")

    @property
    def in_waiting(self):
        self._check()
        return self.port.in_waiting

    def read(self, n):
        self._check()
        return self.port.read(n)

    def write(self, b):
        self._check()
        return self.port.write(b)

    def reset_input_buffer(self):
        self.port.reset_input_buffer()

    def close(self):
        self.port.close()


def playing(t0, state="playing"):
    return dict(state=state, title="副屏之歌（示例）", artist="Monita", album="样例数据", duration=214.0, pos=33.0, at=t0 * 1000,
                rate=1.0, app="spotify", source="mpris", estimated=False, songId=None,
                lyric=dict(status="ok", translated=False, lines=[dict(t=0, x="第一句", tr=""), dict(t=34.0, x="第二句", tr=""),
                                                                   dict(t=36.0, x="第三句", tr="")]))


def scenario(cfg, renderer):
    """跑一种配置，返回 (结果 dict, 不符合预期的地方 [str])"""
    sim = CF.Sim(SIM, cfg["args"])
    fails, res = [], {}
    opens = []

    def check(name, ok, detail=""):
        res[name] = bool(ok)
        if not ok:
            fails.append("%s %s" % (name, detail))

    def open_port():
        p = Flaky(ml.open_port(sim.pty))
        opens.append(p)
        return p

    lyr, vol = Lyr(playing(time.time())), Vol()
    power = Power(sim)
    sess = Session(open_port, clock=lambda: cfg["now"])            # HELLO 带的时间固定（同 conformance 的注入）
    app = App(sess, lyr, renderer, volume=vol, power=power)

    def run(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.step()
            time.sleep(0.01)

    def screen_ok():
        st = sim.stat()
        return st["screen_crc"] == zlib.crc32(app.prev_fb or b"") & 0xFFFFFFFF, st

    def stall(seconds):
        """主循环不动 seconds 秒；期间每 0.1 s 看一眼 stat，返回见过的 (link_up, boot_screen)"""
        seen = set()
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            st = sim.stat()
            seen.add((st["link_up"], st["boot_screen"]))
            time.sleep(0.1)
        return seen

    try:
        run(1.2)
        ok, st = screen_ok()
        check("hello", st["link_up"] == 1 and st["boot_screen"] == 0 and st["clk_valid"] == 1, st)
        d = (st["clk_sod"] - CF.local_sod(cfg["now"], cfg["zone"])) % 86400
        check("hello_time", d <= 3, "设备时钟差 %d s" % d)
        check("full_crc", ok, "screen_crc %#x" % st["screen_crc"])
        fg = bytes.fromhex(st["fg"])
        check("seg_sent", fg == app.last_seg, CF.seg_text(fg))
        main = "".join(CF.SEG_CHARS.get(CF.fg_digit_mask(fg, k), "?") for k in range(6)).strip()
        pos = position_at(lyr.data, time.time())
        check("seg_text", main in {clock_text(pos - k).replace(":", "") for k in (0, 1, 2)}, (main, pos))
        check("led_green", (st["led_mask"], st["led_mode"]) == (ml.LED_G, ml.LED_ON), st["led_mode"])

        g0 = sess.generation
        run(2.5)                                                    # 歌词换句（34 s、36 s）：差分推
        ok, st = screen_ok()
        check("diff_crc", ok and app.frame_id >= 3 and sess.generation == g0, (app.frame_id, sess.generation))

        lyr.data = playing(time.time() - 1, state="paused")
        run(1.0)
        st = sim.stat()
        check("led_off_paused", st["led_mode"] == ml.LED_OFF, st["led_mode"])
        lyr.data = playing(time.time())
        run(0.8)

        sim.cmd("tap 3")
        run(0.4)
        check("key_vol", vol.steps == [6], vol.steps)
        sim.cmd("tap 2")
        run(0.6)
        ok, st = screen_ok()
        check("key_page", app.page == "clock" and ok, app.page)
        sim.cmd("down 2")
        run(1.4)                                                    # 满 1 s 报 LONG
        sim.cmd("up 2")
        run(0.4)
        check("long_no_toggle", app.page == "clock", app.page)
        sim.cmd("tap 2")
        run(0.6)
        ok, _ = screen_ok()
        check("back_to_lyrics", app.page == "lyrics" and ok, app.page)

        g0 = sess.generation
        seen = stall(3.6)                                           # keepalive：停 3.6 s，固件不该回开机页
        run(0.8)
        ok, st = screen_ok()
        check("keepalive_no_boot", seen == {(1, 0)}, seen)
        check("keepalive_rehello", sess.generation == g0 + 1 and ok, (sess.generation, g0))

        seen = stall(5.6)                                           # 主机走了：≥ 5.5 s
        st = sim.stat()
        check("lost_boot_screen", (st["link_up"], st["boot_screen"]) == (0, 1), st)
        run(1.2)
        ok, st = screen_ok()
        check("lost_recover", st["link_up"] == 1 and st["boot_screen"] == 0 and ok, st)
        check("lost_led_back", (st["led_mask"], st["led_mode"]) == (ml.LED_G, ml.LED_ON), st)

        app.post(("sleep", True))
        run(0.1)
        st = sim.stat()
        check("sleep_asleep", st["asleep"] == 1 and st["backlight"] == 0, st)
        check("sleep_fd_after_ack", power.release_asleep == [1], power.release_asleep)
        rx = st["rx_bytes"]
        run(1.5)                                                    # 主循环照常转，但一个字节都不写
        st = sim.stat()
        check("sleep_quiet", st["rx_bytes"] == rx and st["asleep"] == 1, (rx, st["rx_bytes"]))

        app.post(("sleep", False))
        run(1.2)
        ok, st = screen_ok()
        check("wake", st["asleep"] == 0 and st["link_up"] == 1 and st["backlight"] == 1 and ok, st)
        check("wake_led", (st["led_mask"], st["led_mode"]) == (ml.LED_G, ml.LED_ON) and power.takes == 1, st)

        n = len(opens)
        sim.cmd("unplug")
        opens[-1].fail = True
        run(3.5)                                                    # 断开 -> 2 s 后重开端口 -> HELLO -> 整帧
        ok, st = screen_ok()
        check("unplug_reopen", len(opens) == n + 1 and st["link_up"] == 1 and st["boot_screen"] == 0 and ok, (len(opens), st))

        app.post(("quit",))
        run(0.1)
        st = sim.stat()
        check("quit_led_off", app.done and st["led_mode"] == ml.LED_OFF, st["led_mode"])
    finally:
        app.shutdown()
        sim.close()
    return res, fails


class FwsimTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("cc"):
            raise unittest.SkipTest("没有 cc，跳过 fwsim 对拍")
        build_sim()

    def test_all_configs(self):
        results = run_all()
        for cfg, (res, fails) in results:
            with self.subTest(cfg=cfg["name"]):
                self.assertEqual(fails, [])
                self.assertEqual(len(res), 24)


class SmallMaxPayload(unittest.TestCase):
    """max_payload 比一整行（8 + 800×2 = 1608）还小：Session 按宽度切，整帧照样上屏、一个长度错都没有"""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("cc"):
            raise unittest.SkipTest("没有 cc，跳过 fwsim 对拍")
        build_sim()

    def test_full_frame_at_1000(self):
        sim = CF.Sim(SIM, ["--max-payload", "1000"])
        sess = Session(lambda: ml.open_port(sim.pty), clock=lambda: 1784081587)
        try:
            self.assertTrue(sess.connect_if_needed())
            self.assertEqual(sess.info["max_payload"], 1000)
            fb = bytes(range(256)) * (800 * 1280 * 2 // 256)
            self.assertTrue(sess.push(fb, ml.full_rects(max_payload=1000), 1))
            st = sim.stat()
            self.assertEqual(st["len_err"], 0)
            self.assertEqual(st["screen_crc"], zlib.crc32(fb) & 0xFFFFFFFF)
        finally:
            sess.close()
            sim.close()


def _one(i):
    """子进程里跑第 i 种配置：HELLO 的 tz 是进程级的，各配置的时区（conformance.CONFIGS 的 zone）互不干扰"""
    cfg = CF.CONFIGS[i]
    os.environ["TZ"] = cfg["zone"]
    time.tzset()
    r, real = make_renderer()
    return cfg, scenario(cfg, r), real


def run_all():
    """四种配置并行跑，各自一个 fwsim、一个主循环、一个进程"""
    with concurrent.futures.ProcessPoolExecutor(len(CF.CONFIGS)) as ex:
        out = list(ex.map(_one, range(len(CF.CONFIGS))))
    if not all(real for _, _, real in out):
        print("  （缺字体：fwsim 对拍用的是合成画面）")
    return [(cfg, r) for cfg, r, _ in out]


if __name__ == "__main__":
    build_sim()
    t0 = time.monotonic()
    total = 0
    for cfg, (res, fails) in run_all():
        print("%-4s %-28s %d 项" % ("OK" if not fails else "FAIL", cfg["name"], len(res)))
        for f in fails:
            print("       " + f)
        total += len(fails)
    print("linux × fwsim: %s（%.1f s）" % ("all OK" if not total else "%d 处不一致" % total, time.monotonic() - t0))
    sys.exit(1 if total else 0)
