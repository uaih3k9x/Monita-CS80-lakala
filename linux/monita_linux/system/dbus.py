"""对 jeepney 的极薄封装（DESIGN.md §4：纯 Python、阻塞 I/O、支持 fd 传递）。

Bus 接口只有四个方法 + 一个阻塞的信号监听，测试里用假实现；以后换库只改这个文件。
每次调用超时 0.5 s；超时 / 对方报错一律抛 BusError。jeepney 的阻塞连接不能跨线程共用，监听信号要自己开一条连接。
"""
DEFAULT_TIMEOUT = 0.5


class BusError(Exception):
    pass


def unvariant(sig, val):
    """jeepney 的 variant 是 (签名, 值)；a{sv} / v 递归拆开，其余原样"""
    if sig == "v":
        return unvariant(*val)
    if sig == "a{sv}":
        return {k: unvariant(*v) for k, v in val.items()}
    if sig == "av":
        return [unvariant(*v) for v in val]
    return val


class JeepneyBus:
    def __init__(self, bus="SESSION", timeout=DEFAULT_TIMEOUT, enable_fds=False):
        from jeepney.io.blocking import open_dbus_connection
        self.timeout = timeout
        self.conn = open_dbus_connection(bus=bus, enable_fds=enable_fds)

    def _send(self, msg):
        from jeepney import DBusErrorResponse
        from jeepney.wrappers import unwrap_msg
        try:
            return unwrap_msg(self.conn.send_and_get_reply(msg, timeout=self.timeout))
        except (DBusErrorResponse, TimeoutError, OSError) as e:
            raise BusError(str(e)) from e

    def call(self, name, path, iface, method, sig="", args=()):
        from jeepney import DBusAddress, new_method_call
        return self._send(new_method_call(DBusAddress(path, bus_name=name, interface=iface), method, sig, tuple(args)))

    def list_names(self):
        return list(self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ListNames")[0])

    def get(self, name, path, iface, prop):
        return unvariant(*self.call(name, path, "org.freedesktop.DBus.Properties", "Get", "ss", (iface, prop))[0])

    def get_all(self, name, path, iface):
        return unvariant("a{sv}", self.call(name, path, "org.freedesktop.DBus.Properties", "GetAll", "s", (iface,))[0])

    def listen(self, path, iface, member, cb):
        """阻塞：订阅信号，每来一条调 cb(*body)。在自己的线程里、用自己这条连接调"""
        from jeepney import MatchRule, message_bus
        from jeepney.io.blocking import Proxy
        rule = MatchRule(type="signal", path=path, interface=iface, member=member)
        with self.conn.filter(rule) as q:                  # 先挂过滤再 AddMatch，免得漏掉中间那条
            Proxy(message_bus, self.conn).AddMatch(rule)
            while True:
                cb(*self.conn.recv_until_filtered(q).body)

    def close(self):
        self.conn.close()
