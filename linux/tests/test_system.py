"""system：logind inhibitor、音量命令行、CPU / 电池 / 负载（假 Bus、假 run、临时目录里的假 sysfs）"""
import os
import subprocess
import tempfile
import threading
import unittest

from util import FakeClock  # noqa: F401
from monita_linux.system import power, stats, volume
from monita_linux.system.dbus import BusError, unvariant


class Unvariant(unittest.TestCase):
    def test_nested(self):
        md = ("a{sv}", {"xesam:title": ("s", "t"), "xesam:artist": ("as", ["a", "b"])})
        self.assertEqual(unvariant("v", md), {"xesam:title": "t", "xesam:artist": ["a", "b"]})
        self.assertEqual(unvariant("x", 5), 5)


class OldJeepney(unittest.TestCase):
    def test_session_bus_does_not_pass_enable_fds(self):
        # jeepney 0.6（Debian 11）的 open_dbus_connection 没有 enable_fds：会话总线不需要传 fd，就别传，MPRIS 照样能用
        import sys
        import types
        from monita_linux.system.dbus import JeepneyBus
        got = []
        mod = types.ModuleType("jeepney.io.blocking")
        mod.open_dbus_connection = lambda bus="SESSION": got.append(bus) or object()
        saved = {k: sys.modules.get(k) for k in ("jeepney", "jeepney.io", "jeepney.io.blocking")}
        sys.modules.update({"jeepney": types.ModuleType("jeepney"), "jeepney.io": types.ModuleType("jeepney.io"),
                            "jeepney.io.blocking": mod})
        try:
            JeepneyBus("SESSION")
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
        self.assertEqual(got, ["SESSION"])


class FakeFd:
    def __init__(self, n):
        self.n = n

    def to_raw_fd(self):
        return self.n


class SysBus:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []
        self.listening = threading.Event()
        self.cb = None

    def call(self, name, path, iface, method, sig="", args=()):
        self.calls.append((method, args))
        if self.fail:
            raise BusError("no logind")
        return (FakeFd(42),)

    def listen(self, path, iface, member, cb):
        self.member, self.cb = member, cb
        self.listening.set()
        threading.Event().wait()


class Logind(unittest.TestCase):
    def test_inhibitor_take_release(self):
        closed = []
        lg = power.Logind(SysBus(), close=closed.append)
        self.assertTrue(lg.take_inhibitor())
        self.assertEqual(lg.bus.calls[0], ("Inhibit", ("sleep", "monita-cs80", "关屏", "delay")))
        lg.release()
        self.assertEqual(closed, [42])
        lg.release()                                     # 第二次什么都不做
        self.assertEqual(closed, [42])

    def test_no_logind_no_exception(self):
        lg = power.Logind(SysBus(fail=True), close=lambda fd: None)
        with self.assertLogs("monita.power", "WARNING"):
            self.assertFalse(lg.take_inhibitor())
        lg.release()

    def test_listener_thread_uses_own_bus(self):
        made = []

        def factory():
            b = SysBus()
            made.append(b)
            return b
        lg = power.Logind(SysBus(), close=lambda fd: None, listen_bus=factory)
        got = []
        lg.start(got.append)
        made_ok = [b for b in made if b.listening.wait(2)]
        self.assertEqual(len(made_ok), 1)
        self.assertIsNot(made[0], lg.bus)
        self.assertEqual(made[0].member, "PrepareForSleep")
        made[0].cb(True)
        self.assertEqual(got, [True])


class Run:
    def __init__(self, out=""):
        self.cmds = []
        self.out = out

    def __call__(self, cmd, **kw):
        self.cmds.append(cmd)
        self.kw = kw
        return subprocess.CompletedProcess(cmd, 0, stdout=self.out, stderr="")


class Volume(unittest.TestCase):
    def test_wpctl(self):
        r = Run()
        v = volume.Volume("wpctl", run=r)
        v.step(6)
        v.step(-6)
        self.assertEqual(r.cmds, [["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "6%+"],
                                  ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"],
                                  ["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "6%-"]])
        self.assertEqual(r.kw["timeout"], 2)

    def test_pactl_caps_at_100(self):
        r = Run("Volume: front-left: 62259 /  97% / -0.79 dB,   front-right: 62259 /  97% / -0.79 dB\n")
        volume.Volume("pactl", run=r).step(6)
        self.assertEqual(r.cmds, [["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
                                  ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "+3%"],
                                  ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"]])
        r = Run("Volume: front-left: 65536 / 100% / 0.00 dB\n")
        volume.Volume("pactl", run=r).step(6)
        self.assertNotIn("set-sink-volume", [c[1] for c in r.cmds])

    def test_pactl_minus(self):
        r = Run()
        volume.Volume("pactl", run=r).step(-6)
        self.assertEqual(r.cmds, [["pactl", "set-sink-volume", "@DEFAULT_SINK@", "-6%"]])

    def test_detect_order(self):
        self.assertEqual(volume.detect(which=lambda n: "/usr/bin/" + n).kind, "wpctl")
        self.assertEqual(volume.detect(which=lambda n: "/usr/bin/pactl" if n == "pactl" else None).kind, "pactl")
        v = volume.detect(which=lambda n: None)
        self.assertIsNone(v.kind)
        with self.assertLogs("monita.volume", "INFO"):
            v.step(6)

    def test_wpctl_fails_falls_back_to_pactl(self):
        """wpctl 在但不能用（老版本没有 -l / 跑的是 PulseAudio）：改用 pactl，只记一次日志"""
        cmds = []

        def run(cmd, **kw):
            cmds.append(cmd)
            rc = 1 if cmd[0] == "wpctl" else 0
            return subprocess.CompletedProcess(cmd, rc, stdout="Volume: front-left: 50%\n" if cmd[0] == "pactl" else "", stderr="x")
        v = volume.detect(which=lambda n: "/usr/bin/" + n, run=run)
        with self.assertLogs("monita.volume", "WARNING") as cm:
            v.step(6)
        self.assertEqual([c[0] for c in cmds], ["wpctl", "pactl", "pactl", "pactl"])   # 失败的 set-volume 之后不再 set-mute
        self.assertEqual(len(cm.output), 1)
        cmds.clear()
        v.step(-6)
        self.assertEqual(cmds, [["pactl", "set-sink-volume", "@DEFAULT_SINK@", "-6%"]])

    def test_wpctl_fails_no_pactl_keeps_wpctl(self):
        fail = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="x")   # noqa: E731
        v = volume.detect(which=lambda n: "/usr/bin/wpctl" if n == "wpctl" else None, run=fail)
        with self.assertLogs("monita.volume", "WARNING"):
            v.step(6)
        self.assertEqual(v.kind, "wpctl")

    def test_timeout_does_not_raise(self):
        def boom(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 2)
        volume.Volume("wpctl", run=boom).step(6)


class Stats(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def put(self, rel, text):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def test_cpu_fraction(self):
        self.put("proc/stat", "cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 1 2 3 4\n")
        a = stats.cpu_times(self.root)
        self.put("proc/stat", "cpu  150 0 150 900 0 0 0 0 0 0\n")
        b = stats.cpu_times(self.root)
        self.assertAlmostEqual(stats.cpu_fraction(a, b), 0.5)
        self.assertIsNone(stats.cpu_fraction(a, a))
        self.assertIsNone(stats.cpu_times("/nonexistent"))

    def test_battery(self):
        self.assertIsNone(stats.battery(self.root))
        self.put("sys/class/power_supply/BAT0/capacity", "87\n")
        self.put("sys/class/power_supply/AC/online", "1\n")
        self.assertEqual(stats.battery(self.root), (87, True))
        self.put("sys/class/power_supply/AC/online", "0\n")
        self.assertEqual(stats.battery(self.root), (87, False))

    def test_battery_adp(self):
        self.put("sys/class/power_supply/BAT1/capacity", "40")
        self.put("sys/class/power_supply/ADP1/online", "1")
        self.assertEqual(stats.battery(self.root), (40, True))

    def test_loadavg(self):
        self.put("proc/loadavg", "0.42 0.30 0.20 1/234 5678\n")
        self.assertEqual(stats.loadavg(self.root), 0.42)
        self.assertIsNone(stats.loadavg("/nonexistent"))

    def test_sampler(self):
        self.put("proc/stat", "cpu  100 0 100 800 0 0 0 0\n")
        self.put("proc/loadavg", "1.5 1 1 1/1 1\n")
        s = stats.Stats(self.root)
        s.sample()
        self.put("proc/stat", "cpu  200 0 100 900 0 0 0 0\n")
        s.sample()
        self.assertEqual(s.panel(), dict(cpu=50, bat=None, load=1.5))


if __name__ == "__main__":
    unittest.main()
