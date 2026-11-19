"""Link 之上的会话语义（DESIGN.md §3.2；照 mac/Sources/MonitaCore/HostPower.swift + monita-cs80/SerialLink.swift）。

- keepalive：隔了 ≥ stale_after 秒才发下一条时先重新 HELLO（固件 ~5.1 s 没有效帧就回开机页），generation + 1；
- HELLO_ACK 收紧：proto / 800×1280 / pixfmt 1 不符都算连接失败；
- caps 门控：LED（bit5）、COPY（bit2）、SLEEP（bit7）、RECT_LZ4（bit8，fw v11；还要装了 python3-lz4）；
- RECT_LZ4：每个矩形压缩后 < 3/4 才发（monita_link.rect_frame）；一批里有 RECT_LZ4、PRESENT status 0 而 bad_param 涨了
  （monita_link.lz4_batch_bad；CRC 错是链路毛刺、沉浸模式被拒是模式不对，都不算），这次连接里 fw 没变就一直只发 RECT：
  keepalive / 睡醒的 HELLO 不恢复，端口重新打开（设备重新枚举）或 fw 变了才再试；
- 只有等应答超时、IO 出错、HELLO_ACK 不合格算断开；断开就关端口，下一轮重新 open_port（USB 重新枚举后旧句柄失效）；
- suspend() 发 SLEEP 后一个字节都不写（任何一帧都会叫醒设备）；resume() 之后第一条之前必定 HELLO。
单线程用，不加锁。
"""
import logging
import struct
import time

import monita_link as ml

log = logging.getLogger("monita.session")
RETRY_S = 2.0


class Session:
    def __init__(self, open_port, clock=time.time, mono=time.monotonic, stale_after=3.5, link_factory=ml.Link, lz4=True):
        self.open_port, self.clock, self.mono = open_port, clock, mono
        self.stale_after, self.link_factory = stale_after, link_factory
        self.link = None
        self.info = None
        self.up = False
        self.generation = 0
        self.suspended = False
        self.forced = False
        self.last_sent = None
        self.retry_at = 0.0
        self.base = 0                   # PRESENT ACK 的出错计数基线（crc_err + len_err + bad_param）
        self.base_bad = 0               # 其中 bad_param 的基线（判断要不要关 RECT_LZ4）
        self.key_n = 0
        self.open_err = None            # 上一次打不开设备的错（日志去重）
        self.lz4 = lz4                  # False：不发 RECT_LZ4（--no-lz4）
        self.lz4_off_fw = None          # 一批带 RECT_LZ4 的推坏了（ml.lz4_batch_bad）时的 fw：这次连接里 fw 没变就只发 RECT

    # ---- 状态 ----
    @property
    def caps(self):
        return self.info["caps"] if self.info else 0

    @property
    def can_send(self):
        return self.up and not self.suspended

    def _needs_hello(self):
        if self.forced:
            return True
        return self.stale_after is not None and self.last_sent is not None and self.mono() - self.last_sent >= self.stale_after

    def _drop(self, why):
        if self.link is not None:
            log.warning("设备断开：%s", why)
            try:
                self.link.close()
            except Exception:
                pass
        self.link, self.info, self.up = None, None, False
        self.lz4_off_fw = None                                   # 下次重新打开端口（设备重新枚举）RECT_LZ4 再试
        self.retry_at = self.mono() + RETRY_S

    def _hello(self):
        self.last_sent = self.mono()
        try:
            info = self.link.hello()
        except (OSError, TimeoutError) as e:
            self._drop("HELLO 没有应答（%s）" % e)
            return False
        if info["proto"] != ml.PROTO_VER or (info["w"], info["h"]) != (ml.W, ml.H) or info["pixfmt"] != 1:
            self._drop("协议版本 / 屏参数不符 %r" % info)
            return False
        self.info, self.up, self.forced = info, True, False
        self.generation += 1
        self.base = self.base_bad = 0
        self.key_n = 0
        if info["fw"] != self.lz4_off_fw:                        # 刷了别的固件才再试；keepalive / 睡醒的 HELLO 不恢复
            self.lz4_off_fw = None
        return True

    def _fresh(self):
        """发之前：睡着 / 没连上就不发；隔太久 / 刚睡醒就先 HELLO。返回能不能接着发"""
        if not self.can_send:
            return False
        return self._hello() if self._needs_hello() else True

    def _call(self, ftype, payload=b"", timeout=None):
        self.last_sent = self.mono()
        try:
            return self.link.call(ftype, payload, timeout)
        except (OSError, TimeoutError) as e:
            self._drop("%#04x：%s" % (ftype, e))
            return None

    # ---- 连接 ----
    def connect_if_needed(self):
        """没连上就打开 + HELLO；连着但隔太久 / 刚睡醒就重新 HELLO。返回 True = 刚(重新)连上（调用方推整帧）"""
        if self.suspended:
            return False
        if self.link is not None:
            return self._hello() if self._needs_hello() else False
        if self.mono() < self.retry_at:
            return False
        try:
            port = self.open_port()
        except Exception as e:
            msg = "%s: %s" % (type(e).__name__, e)
            (log.debug if msg == self.open_err else log.warning)("打不开设备：%s", msg)   # 同样的错每 2 s 一次，只 warning 一次
            self.open_err = msg
            port = None
        if port is None:
            self.retry_at = self.mono() + RETRY_S
            return False
        try:
            self.link = self.link_factory(port)
        except (OSError, TimeoutError) as e:
            try:
                port.close()
            except Exception:
                pass
            self._drop("初始化端口：%s" % e)
            return False
        self.link.clock = self.clock
        if not self._hello():
            return False
        self.open_err = None
        log.info("设备已连上：fw=%#x caps=%#x max_payload=%d", self.info["fw"], self.caps, self.info["max_payload"])
        if self.caps & ml.CAP_LZ4 and self.lz4 and not ml.lz4_available():
            log.info("固件支持 RECT_LZ4（fw v11），但没装 python3-lz4：照发普通 RECT（整帧会慢一些）")
        return True

    # ---- 发送 ----
    def ping(self):
        return self._call(ml.PING) if self._fresh() else None

    def seg(self, fb72):
        if not self._fresh():
            return None
        a = self._call(ml.SEG, bytes(fb72))
        if a and a["status"] != ml.OK:
            log.info("SEG status=%d", a["status"])
        return a

    def led(self, mask, mode, period_ms=0, on_ms=0, duration_ms=0):
        if not self.caps & ml.CAP_LED or not self._fresh():
            return None
        a = self._call(ml.LED, ml.led_payload(mask, mode, period_ms, on_ms, duration_ms))
        if a and a["status"] != ml.OK:
            log.info("LED status=%d", a["status"])
        return a

    def push(self, fb, rects, frame_id, copies=()):
        """COPY + RECT 一次写出，再等 PRESENT 的 ACK（10 s）。False = 这次推坏了 / 刚重新 HELLO（调用方推整帧）"""
        g = self.generation
        if not self._fresh() or self.generation != g:
            return False
        maxp = min(self.info["max_payload"], 32768)
        wmax = max(1, (maxp - 8) // 2)                      # max_payload 比一整行还小时按宽度也切
        out = bytearray()
        lk = self.link
        lz = self.lz4 and self.lz4_off_fw is None and bool(self.caps & ml.CAP_LZ4) and ml.lz4_available()
        n_lz = 0
        for c in (copies if self.caps & ml.CAP_COPY else ()):
            lk.seq = (lk.seq + 1) & 0xFF
            out += ml.encode(ml.COPY, lk.seq, ml.copy_payload(*c))
        for (x, y, w, h) in rects:
            for x0 in range(x, x + w, wmax):
                w0 = min(wmax, x + w - x0)
                n = max(1, (maxp - 8) // (w0 * 2))
                for y0 in range(y, y + h, n):
                    lk.seq = (lk.seq + 1) & 0xFF
                    t, pl = ml.rect_frame(fb, x0, y0, w0, min(n, y + h - y0), lz)
                    n_lz += t == ml.RECT_LZ4
                    out += ml.encode(t, lk.seq, pl)
        self.last_sent = self.mono()
        try:
            if out:
                lk.ser.write(bytes(out))
        except OSError as e:
            self._drop("写失败：%s" % e)
            return False
        a = self._call(ml.PRESENT, struct.pack("<H", frame_id & 0xFFFF), timeout=10)
        if a is None:
            return False
        errs = a["crc_err"] + a["len_err"] + a["bad_param"]
        ok = a["status"] == ml.OK and errs == self.base
        lz_bad = ml.lz4_batch_bad(n_lz, a["status"], a["bad_param"], self.base_bad)
        self.base, self.base_bad = errs, a["bad_param"]
        if not ok:
            log.info("PRESENT status=%d 出错计数 %d", a["status"], errs)
        if lz_bad:
            self.lz4_off_fw = self.info["fw"]
            log.warning("这批有 %d 条 RECT_LZ4、设备计了 bad_param：这次连接里只发普通 RECT（重新插拔 / 换固件后再试）", n_lz)
        return ok

    def poll_keys(self):
        """只读：睡着时照样收。KEY 计数不连续只记日志"""
        if self.link is None:
            return []
        try:
            ks = self.link.poll_keys(0) if self.link.ser.in_waiting else self.link.keys
            self.link.keys = []
        except OSError as e:
            self._drop("读失败：%s" % e)
            return []
        for k in ks:
            if k["n"] != self.key_n:
                log.info("KEY 计数跳了：应为 %d，收到 %d", self.key_n, k["n"])
            self.key_n = (k["n"] + 1) & 0xFFFF
        return ks

    # ---- 休眠 ----
    def suspend(self):
        """发 SLEEP（超时 1 s），之后拒绝一切写。返回 SLEEP 有没有被 ACK"""
        ok = False
        if self.can_send and self.caps & ml.CAP_SLEEP:
            a = self._call(ml.SLEEP, timeout=1)
            ok = bool(a) and a["status"] == ml.OK
        self.suspended = True
        return ok

    def resume(self):
        if self.suspended:
            self.suspended = False
            self.forced = True

    def close(self):
        """退出：醒着就先灭灯；睡着时一个字节都不发（会叫醒设备）"""
        if self.can_send:
            self.led(0, ml.LED_OFF)
        if self.link is not None:
            try:
                self.link.close()
            except Exception:
                pass
        self.link, self.up = None, False
