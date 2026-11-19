"""主循环（DESIGN.md §3.10）：单线程、按时间表调度，每轮最多睡 50 ms。

| 任务 | 周期 |
|---|---|
| 事件队列（logind、退出） | 每轮 |
| 收 KEY → 动作 | 50 ms（睡着也读，只是不写设备） |
| 连接 / PING | 2 s |
| 正在播放 / 歌词 / 本机数据 | 1 s |
| 段码屏 | 250 ms 算一次，变了才发，没变也每 1.5 s 重发 |
| 灯 | 0.5 s，灯语或 generation 变了才发 |
| 渲染 | tick（默认 0.2 s）算帧键，变了才画；差分推，失败 / generation 变了 / 60 min 兜底推整帧 |
"""
import io
import logging
import queue
import threading
import time
from urllib.parse import unquote, urlparse

import monita_link as ml

from .controls import KeyMapper, led_for
from .schedule import frame_key, is_playing
from .segframe import seg_frame

log = logging.getLogger("monita.app")
FULL_EVERY_S = 3600


class Covers:
    """封面在后台线程取（Lyrics.cover 是同步 HTTP，超时 8 s，不能卡主循环）；解码缩放成 220×220 缓存。取失败也缓存成 None"""

    def __init__(self, size=220, thread=threading.Thread):
        self.size, self.thread = size, thread
        self.cache = {}
        self.pending = set()
        self.lock = threading.Lock()

    def get(self, key, load):
        with self.lock:
            if key in self.cache:
                return self.cache[key]
            if key in self.pending:
                return None
            self.pending.add(key)

        def work():
            img = None
            try:
                data = load()
                if data:
                    from PIL import Image
                    img = Image.open(io.BytesIO(data)).convert("RGB").resize((self.size, self.size))
            except Exception as e:
                log.info("封面取不到：%s", e)
            with self.lock:
                self.cache[key] = img
                self.pending.discard(key)
        self.thread(target=work, daemon=True).start()
        return None


def _file_bytes(path):
    with open(path, "rb") as f:
        return f.read()


class App:
    def __init__(self, session, lyrics, renderer, player=None, volume=None, power=None, stats=None, covers=None,
                 page="lyrics", tick=0.2, clock=time.time, mono=time.monotonic):
        self.session, self.lyrics, self.renderer = session, lyrics, renderer
        self.player, self.volume, self.power, self.stats = player, volume, power, stats
        self.covers = covers or Covers()
        self.page, self.tick, self.clock, self.mono = page, tick, clock, mono
        self.events = queue.Queue()
        self.keys = KeyMapper()
        self.due = {}
        self.data = None
        self.asleep = False
        self.done = False
        self.last_seg, self.seg_at = None, 0.0
        self.last_led = None
        self.prev_fb, self.cur, self.pushed_gen = None, None, None
        self.last_key, self.last_full, self.frame_id = None, 0.0, 0
        self.tried_gen = None

    def post(self, ev):
        """任何线程都可以调（logind 监听线程、信号处理）"""
        self.events.put(ev)

    def _every(self, name, period):
        now = self.mono()
        if now >= self.due.get(name, 0.0):
            self.due[name] = now + period
            return True
        return False

    # ---- 事件 ----
    def _event(self, ev):
        if ev[0] == "sleep" and ev[1] and not self.asleep:
            self.asleep = True
            ok = self.session.suspend()
            log.info("系统要睡了：%s", "已发 SLEEP" if ok else "没发 SLEEP（没连着 / 固件不支持）")
            if self.power:
                self.power.release()
        elif ev[0] == "sleep" and not ev[1] and self.asleep:
            self.asleep = False
            self.session.resume()
            if self.power:
                self.power.take_inhibitor()
            self.due.clear()                    # 立刻重连、重推
            self.last_seg = self.last_led = self.prev_fb = None
            log.info("系统醒了：重新 HELLO + 整帧")
        elif ev[0] == "quit":
            self.shutdown()

    def shutdown(self):
        if not self.done:
            self.done = True
            self.session.close()                # 睡着时不发 LED off（会叫醒设备）
            if self.power:
                self.power.release()

    # ---- 动作 ----
    def _act(self, a):
        log.info("按键：%s", a)
        if a in ("vol+", "vol-") and self.volume:
            self.volume.step(6 if a == "vol+" else -6)
        elif a == "play_pause" and self.player:
            self.player.play_pause()
        elif a == "page":
            self.page = "clock" if self.page == "lyrics" else "lyrics"

    # ---- 数据 ----
    def _refresh(self):
        try:
            self.data = self.lyrics.payload()
        except Exception as e:
            log.warning("取正在播放失败：%s", e)
            self.data = None
        if self.stats and self._every("stats", 2.0):
            self.stats.sample()

    def _cover(self):
        d = self.data
        if not is_playing(d):
            return None
        if d.get("songId"):
            sid = d["songId"]
            return self.covers.get(("sid", sid), lambda: self.lyrics.cover(sid))
        art = getattr(self.player, "art_url", None) or ""
        if art.startswith("file://"):
            path = unquote(urlparse(art).path)
            return self.covers.get(("file", path), lambda: _file_bytes(path))
        return None

    def _panel(self):
        p = dict(self.stats.panel()) if self.stats else {}
        app = (self.data or {}).get("app") if is_playing(self.data) else None
        p["player"] = app.split(".")[0] if app else None
        return p

    # ---- 设备 ----
    def _seg(self):
        bat = self.stats.bat if self.stats else None
        fb = seg_frame(self.data if is_playing(self.data) else None, self.clock(), bat)
        if fb != self.last_seg or self.mono() - self.seg_at >= 1.5:
            a = self.session.seg(fb)
            ok = bool(a) and a["status"] == ml.OK
            self.last_seg, self.seg_at = (fb, self.mono()) if ok else (None, self.seg_at)

    def _led(self):
        if not self.session.caps & ml.CAP_LED:
            return
        cmd = led_for(is_playing(self.data) and self.data.get("state") == "playing")
        if (cmd, self.session.generation) != self.last_led:
            if self.session.led(*cmd) is not None:
                self.last_led = (cmd, self.session.generation)

    def _render(self):
        s = self.session
        self.tried_gen = s.generation
        now, mono = self.clock(), self.mono()
        panel = self._panel()
        cover = self._cover()
        key = frame_key(self.page, self.data, now, self.renderer.progress_w, sorted(panel.items()), cover)
        full = self.prev_fb is None or s.generation != self.pushed_gen or mono - self.last_full >= FULL_EVERY_S
        if key == self.last_key and not full:
            return
        if key != self.last_key or self.cur is None:
            _, self.cur = self.renderer.render(self.page, dict(data=self.data, cover=cover, panel=panel, now=now))
        maxp = min(s.info["max_payload"], 32768)
        rects = ml.full_rects(max_payload=maxp) if full else ml.dirty_rects(self.prev_fb, self.cur, max_payload=maxp)
        if not rects:
            self.last_key = key
            return
        self.frame_id += 1
        if s.push(self.cur, rects, self.frame_id):
            self.prev_fb, self.pushed_gen, self.last_key = self.cur, s.generation, key
            if full:
                self.last_full = mono
        else:
            self.prev_fb = None                 # 下一个节拍推整帧

    # ---- 一轮 ----
    def step(self):
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                break
            self._event(ev)
            if self.done:
                return
        if self._every("keys", 0.05):
            for k in self.session.poll_keys():
                a = self.keys.feed(k)
                if a:
                    self._act(a)
        if self.asleep:
            return
        if self._every("conn", 2.0):
            if not self.session.up:
                self.session.connect_if_needed()
            else:
                self.session.ping()
        if self._every("data", 1.0):
            self._refresh()
        if not self.session.up:
            return
        if self._every("seg", 0.25):
            self._seg()
        if self.session.up and self._every("led", 0.5):
            self._led()
        if self.session.up and (self.session.generation != self.tried_gen or self._every("render", self.tick)):
            self._render()                      # 重新 HELLO 过（可能发生在 seg / ping 里）：不等节拍，马上推整帧

    def run(self, sleep=time.sleep):
        while not self.done:
            self.step()
            if not self.done:
                nxt = min(self.due.values() or [self.mono()])
                sleep(max(0.0, min(0.05, nxt - self.mono())))
