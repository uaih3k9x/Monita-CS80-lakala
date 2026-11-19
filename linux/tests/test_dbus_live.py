"""真 D-Bus：起一个私有 dbus-daemon，上面挂一个假 MPRIS 播放器和一个假 login1，用 JeepneyBus + MprisSource + Logind 走真线协议。

需要 jeepney 和 dbus-daemon（Linux 上都有；Mac 上 brew install dbus），没有就 skip。
覆盖的是假 Bus 测不到的部分：variant 拆包、Properties.Get / GetAll、方法调用、信号订阅、Inhibit 的 fd 传递。
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from util import FakeClock  # noqa: F401

if sys.platform == "darwin" and hasattr(socket, "SCM_CREDS"):
    # 只为在 Mac 上跑这条测试：jeepney 在 BSD 上发 SCM_CREDS，macOS 不认（EINVAL）；macOS 的 dbus-daemon 用 getpeereid 认人，
    # 不需要这条辅助数据。Linux 上没有 SCM_CREDS，走不到这里，产品代码也不受影响
    delattr(socket, "SCM_CREDS")

try:
    import jeepney  # noqa: F401
    from jeepney import DBusAddress, MessageType, message_bus, new_error, new_method_return, new_signal
    from jeepney.io.blocking import Proxy, open_dbus_connection
except ImportError:
    jeepney = None

PLAYER = "org.mpris.MediaPlayer2.Player"


def serve(addr, name, handler, ready, fds=False):
    """在自己的线程 + 连接上挂一个服务：handler(msg) -> (签名, body) 或 None（回错误）"""
    conn = open_dbus_connection(addr, **({"enable_fds": True} if fds else {}))   # jeepney 0.6 没有 enable_fds
    Proxy(message_bus, conn).RequestName(name)
    ready[name] = conn
    while True:
        try:
            msg = conn.receive()
        except Exception:
            return
        if msg.header.message_type != MessageType.method_call:
            continue
        r = handler(msg)
        conn.send_message(new_method_return(msg, *r) if r is not None else new_error(msg, "org.freedesktop.DBus.Error.NotSupported"))


@unittest.skipUnless(jeepney and shutil.which("dbus-daemon"), "没有 jeepney / dbus-daemon")
class LiveBus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        conf = os.path.join(cls.tmp, "bus.conf")
        with open(conf, "w") as f:
            f.write("""<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig><type>session</type><listen>unix:dir=%s</listen>
<auth>EXTERNAL</auth>
<policy context="default"><allow send_destination="*" eavesdrop="true"/><allow eavesdrop="true"/><allow own="*"/></policy>
</busconfig>""" % cls.tmp)
        cls.daemon = subprocess.Popen(["dbus-daemon", "--config-file", conf, "--print-address", "--nofork"],
                                      stdout=subprocess.PIPE, text=True)
        cls.addr = cls.daemon.stdout.readline().strip()
        cls.calls = []
        cls.status = "Playing"
        ready = {}

        def mpris(msg):
            f = msg.header.fields
            iface, member = f.get(jeepney.HeaderFields.interface), f.get(jeepney.HeaderFields.member)
            md = ("a{sv}", {"xesam:title": ("s", "真总线上的歌"), "xesam:artist": ("as", ["甲", "乙"]),
                            "mpris:length": ("x", 200_000_000), "mpris:trackid": ("o", "/t/1")})
            props = {"PlaybackStatus": ("s", cls.status), "Metadata": md, "Position": ("x", 12_000_000), "Rate": ("d", 1.0)}
            if member == "GetAll":
                return ("a{sv}", (props,))
            if member == "Get":
                return ("v", (props[msg.body[1]],)) if msg.body[1] in props else None
            if iface == PLAYER and member == "PlayPause":
                cls.calls.append("PlayPause")
                return ("", ())
            return None

        def login1(msg):
            member = msg.header.fields.get(jeepney.HeaderFields.member)
            if member == "Inhibit":
                cls.calls.append(("Inhibit",) + tuple(msg.body))
                r, w = os.pipe()
                cls.inhibit_w = w
                return ("h", (r,))
            return None

        for name, h, fds in (("org.mpris.MediaPlayer2.fakeplayer.instance7", mpris, False), ("org.freedesktop.login1", login1, True)):
            threading.Thread(target=serve, args=(cls.addr, name, h, ready, fds), daemon=True).start()
        end = time.time() + 5
        while len(ready) < 2 and time.time() < end:
            time.sleep(0.02)
        if "org.mpris.MediaPlayer2.fakeplayer.instance7" not in ready:
            cls.tearDownClass()                          # setUpClass 失败时 tearDownClass 不会被调，别把 dbus-daemon 留成孤儿
            raise unittest.SkipTest("私有 dbus-daemon 上挂不上假播放器")
        cls.login_conn = ready.get("org.freedesktop.login1")   # jeepney < 0.7 不能传 fd，挂不上假 login1

    @classmethod
    def tearDownClass(cls):
        cls.daemon.terminate()
        cls.daemon.wait(5)
        cls.daemon.stdout.close()

    def bus(self, **kw):
        from monita_linux.system.dbus import JeepneyBus
        return JeepneyBus(self.addr, **kw)

    def test_mpris_over_real_bus(self):
        from monita_linux.sources.mpris import MprisSource
        s = MprisSource(self.bus(), prefer="fakeplayer")
        raw = s.get()
        self.assertEqual((raw["title"], raw["artist"], raw["durationMicros"], raw["elapsedTimeMicros"], raw["bundleIdentifier"]),
                         ("真总线上的歌", "甲/乙", 200_000_000, 12_000_000, "fakeplayer.instance7"))
        s.play_pause()
        self.assertIn("PlayPause", self.calls)

    def test_get_missing_prop_is_bus_error(self):
        from monita_linux.system.dbus import BusError
        with self.assertRaises(BusError):
            self.bus().get("org.mpris.MediaPlayer2.fakeplayer.instance7", "/org/mpris/MediaPlayer2", PLAYER, "Nope")

    def test_logind_inhibit_fd_and_signal(self):
        from monita_linux.system.power import Logind
        if self.login_conn is None:
            self.skipTest("jeepney < 0.7：不能传 fd，logind 休眠不可用（README 写了要 ≥ 0.7）")
        lg = Logind(self.bus(enable_fds=True), listen_bus=self.bus)
        if not lg.take_inhibitor():
            self.skipTest("这台机器的 dbus-daemon 不支持传 fd")
        self.assertIsInstance(lg.fd, int)
        got = []
        lg.start(got.append)
        time.sleep(0.3)                                  # 等监听线程 AddMatch
        for v in (True, False):
            self.login_conn.send_message(new_signal(DBusAddress("/org/freedesktop/login1", interface="org.freedesktop.login1.Manager"),
                                                    "PrepareForSleep", "b", (v,)))
        end = time.time() + 3
        while len(got) < 2 and time.time() < end:
            time.sleep(0.02)
        self.assertEqual(got, [True, False])
        fd = lg.fd
        lg.release()
        self.assertIsNone(lg.fd)
        with self.assertRaises(OSError):
            os.fstat(fd)                                 # 关掉了：logind 据此放行挂起


if __name__ == "__main__":
    unittest.main()
