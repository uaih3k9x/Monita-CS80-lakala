"""设备怎么打开：--device usb | usb:N | 串口路径（DESIGN.md §3.1）。

返回的端口对象给 monita_link.Link 用：read(n) / write(b) / in_waiting / reset_input_buffer() / close()。
USB 走 pyusb（libusb）：接口 7，bulk OUT 0x07 / IN 0x8A（docs/protocol.md「传输层」）。
"""
import logging

import monita_link as ml

VID, PID = 0x2C7C, 0x0901
EP_OUT, EP_IN = 0x07, 0x8A
READ_BLOCK = 16384
log = logging.getLogger("monita.transport")


class UsbPort:
    """pyusb 设备的一个接口当字节流用。超时读成 b''；USBError（拔掉 ENODEV / EIO）原样抛出，由 Session 当断开处理。"""

    def __init__(self, dev, intf=7, ep_out=EP_OUT, ep_in=EP_IN, util=None, timeout_error=None, chunk=1 << 20):
        if util is None or timeout_error is None:
            import usb.core
            import usb.util
            util = util or usb.util
            timeout_error = timeout_error or usb.core.USBTimeoutError
        self.dev, self.intf, self.ep_out, self.ep_in = dev, intf, ep_out, ep_in
        self.util, self.timeout_error, self.chunk = util, timeout_error, chunk
        self.buf = bytearray()
        self.detached = False
        if dev.is_kernel_driver_active(intf):           # option / qcserial 已经绑上了：借过来，close 时还回去
            dev.detach_kernel_driver(intf)
            self.detached = True
        util.claim_interface(dev, intf)

    def _fill(self, timeout_ms):
        try:
            self.buf += bytes(self.dev.read(self.ep_in, READ_BLOCK, timeout_ms))
        except self.timeout_error:
            pass

    @property
    def in_waiting(self):
        if not self.buf:
            self._fill(1)
        return len(self.buf)

    def read(self, n):
        if not self.buf:
            self._fill(50)
        out = bytes(self.buf[:max(1, n)])
        del self.buf[:len(out)]
        return out

    def write(self, data):
        mv = memoryview(bytes(data))
        n = 0
        while n < len(mv):
            n += self.dev.write(self.ep_out, mv[n:n + self.chunk], 10000)
        return n

    def reset_input_buffer(self):
        self.buf.clear()
        for _ in range(64):                             # 把端点里积压的读干净（最多 1 MiB）
            before = len(self.buf)
            self._fill(1)
            if len(self.buf) == before:
                break
            self.buf.clear()
        self.buf.clear()

    def close(self):
        for f in (lambda: self.util.release_interface(self.dev, self.intf),
                  lambda: self.detached and self.dev.attach_kernel_driver(self.intf),
                  lambda: self.util.dispose_resources(self.dev)):
            try:
                f()
            except Exception as e:                      # 拔掉之后释放会失败，不要紧
                log.debug("close: %s", e)


def parse_spec(spec):
    """'usb' -> ('usb', 7)；'usb:N' -> ('usb', N)；其余当串口路径"""
    if spec == "usb":
        return ("usb", 7)
    if spec.startswith("usb:"):
        return ("usb", int(spec[4:]))
    return ("tty", spec)


def _find_usb():
    import usb.core
    return usb.core.find(idVendor=VID, idProduct=PID)


def open_device(spec, find=_find_usb, open_tty=ml.open_port):
    """打开设备；找不到返回 None（调用方 2 s 后重试）。claim 失败（EBUSY 等）抛 OSError，带提示"""
    kind, arg = parse_spec(spec)
    if kind == "tty":
        return open_tty(arg)
    dev = find()
    if dev is None:
        return None
    try:
        return UsbPort(dev, intf=arg)
    except OSError as e:
        if getattr(e, "errno", None) == 16:
            raise OSError(16, "接口 %d 被内核驱动或别的程序占用，试 --device /dev/monita-cs80（%s）" % (arg, e)) from e
        raise
