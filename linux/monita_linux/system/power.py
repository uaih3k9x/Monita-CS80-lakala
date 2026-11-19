"""logind 休眠联动（DESIGN.md §3.8）：启动时拿 delay inhibitor；PrepareForSleep(true) 时主循环发 SLEEP 后才关 fd，系统才真正挂起。

监听线程阻塞在自己的系统总线连接上，只把事件交给回调（主循环的队列），不碰 Link。
拿不到 inhibitor（没有 logind、容器里）只记一条警告：后果只是设备约 5 s 后回开机页、10 min 后自己休眠。
"""
import logging
import os
import threading

from .dbus import BusError

log = logging.getLogger("monita.power")
LOGIN1 = "org.freedesktop.login1"
PATH = "/org/freedesktop/login1"
MANAGER = "org.freedesktop.login1.Manager"


class Logind:
    def __init__(self, bus, close=os.close, listen_bus=None):
        self.bus, self.close, self.listen_bus = bus, close, listen_bus
        self.fd = None

    def take_inhibitor(self):
        if self.fd is not None:
            return True
        try:
            fd = self.bus.call(LOGIN1, PATH, MANAGER, "Inhibit", "ssss", ("sleep", "monita-cs80", "关屏", "delay"))[0]
        except (BusError, OSError) as e:
            log.warning("拿不到 logind 的 delay inhibitor（%s）：不处理休眠", e)
            return False
        self.fd = fd.to_raw_fd() if hasattr(fd, "to_raw_fd") else int(fd)
        return True

    def release(self):
        """关 fd = 告诉 logind 可以睡了"""
        if self.fd is not None:
            fd, self.fd = self.fd, None
            try:
                self.close(fd)
            except OSError:
                pass

    def start(self, cb):
        """后台线程监听 PrepareForSleep，cb(True) = 要睡了，cb(False) = 醒了"""
        def run():
            try:
                bus = self.listen_bus()
                bus.listen(PATH, MANAGER, "PrepareForSleep", lambda going, *_: cb(bool(going)))
            except Exception as e:
                log.warning("logind 信号监听停了：%s", e)
        t = threading.Thread(target=run, name="logind", daemon=True)
        t.start()
        return t
