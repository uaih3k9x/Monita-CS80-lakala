# -*- coding: utf-8 -*-
"""歌词数据源：当前播放（MediaRemote，经 mediaremote-adapter）+ 网易云歌词。

自带一个极小的 HTTP 服务给 Mac 端渲染器（mac/ 下的 monita-cs80 --lyrics URL）用：

    python3 tools/lyrics/lyrics.py serve [--port 8788]
    GET /api/data/lyrics      当前播放 + 歌词（JSON）
    GET /api/lyrics/cover?id= 封面 jpg

  当前播放  macOS 15.4+ 第三方进程直接调 MediaRemote 拿不到 Now Playing，借 /usr/bin/perl
           加载 ungive/mediaremote-adapter 的 framework（sh tools/lyrics/build-nowplaying.sh 编译，
           产物在 ~/.monita-cs80/mediaremote-adapter/）。常驻一个 `stream` 子进程，事件驱动更新。
           没装 adapter → 兜底：网易云播放缓存目录里最新的 songId + 切歌时刻起自己计时（estimated）。
  歌词     songId 优先用缓存目录里出现过的 id，否则按 歌名+歌手+时长 搜索打分；
           歌词原文按 songId 存 ~/.monita-cs80/lyrics/<id>.json（内存也留一份），封面存 <id>.jpg。

上半部分全是纯函数（tests/test_lyrics.py 覆盖），下半部分是 IO。只用标准库。
"""
import atexit
import bisect
import glob
import json
import os
import re
import subprocess
import threading
import time
import unicodedata
import urllib.parse
import urllib.request

# ---------------- 纯函数 ----------------
_TAG = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_OFFSET = re.compile(r"\[offset:\s*([+-]?\d+)\s*\]", re.I)


def parse_lrc(text):
    """LRC → [(秒, 文本)]，按时间排序。支持一行多个时间戳、[offset:]、网易云的 JSON 署名行。
    空文本行保留（表示间奏），无时间戳的行丢弃。"""
    if not text:
        return []
    m = _OFFSET.search(text)
    offset = int(m.group(1)) / 1000.0 if m else 0.0
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("{") and '"t"' in line:
            try:
                obj = json.loads(line)
                out.append((max(0.0, obj.get("t", 0) / 1000.0),
                            "".join(c.get("tx", "") for c in obj.get("c", [])).strip()))
            except (ValueError, AttributeError, TypeError):
                pass
            continue
        stamps = []
        pos = 0
        while True:
            m = _TAG.match(line, pos)
            if not m:
                break
            frac = m.group(3) or "0"
            sec = int(m.group(1)) * 60 + int(m.group(2)) + int(frac) / (10 ** len(frac))
            stamps.append(sec)
            pos = m.end()
        if not stamps:
            continue
        body = line[pos:].strip()
        for sec in stamps:
            out.append((round(max(0.0, sec - offset), 3), body))
    out.sort(key=lambda x: x[0])
    return out


def merge_translation(lines, tlines, tol=0.05):
    """原文 + 翻译按时间戳对齐（容差 tol 秒）→ [{t, x, tr}]。"""
    times = [t for t, _ in tlines]
    res = []
    for t, x in lines:
        tr = ""
        i = bisect.bisect_left(times, t - tol)
        if i < len(times) and abs(times[i] - t) <= tol:
            tr = tlines[i][1]
        if tr.strip() in ("//", "/"):
            tr = ""
        res.append({"t": t, "x": x, "tr": tr})
    return res


def current_index(lines, pos):
    """pos 秒时正在唱的行下标；还没到第一行返回 -1。lines 元素带 't'。"""
    lo, hi = 0, len(lines)
    while lo < hi:
        mid = (lo + hi) // 2
        if lines[mid]["t"] <= pos:
            lo = mid + 1
        else:
            hi = mid
    return lo - 1


def position(elapsed, at, rate, now, duration=None):
    """进度插值：elapsed（秒）是 at 时刻的进度，按 rate 外推到 now。单位：秒。"""
    if elapsed is None:
        return 0.0
    p = float(elapsed) + (now - at) * (rate or 0.0)
    p = max(0.0, p)
    if duration:
        p = min(p, float(duration))
    return p


_BRACKETS = re.compile(r"[（(\[【].*?[)）\]】]")


def norm(s):
    """比较用：全角转半角、小写、去括号注释（Live/伴奏/版本）、去空白标点。"""
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = _BRACKETS.sub("", s)
    return "".join(ch for ch in s if ch.isalnum())


def _artists(s):
    return {norm(a) for a in re.split(r"\s*(?:/|&|,|，|、|;| feat\.? | ft\.? | x )\s*", s or "", flags=re.I) if norm(a)}


def song_score(cand, title, artist, duration):
    """搜索结果打分；<=0 视为不匹配。"""
    name = norm(cand.get("name"))
    want = norm(title)
    if not name or not want:
        return 0
    score = 0
    if name == want:
        score += 100
    elif want in name or name in want:
        score += 40
    else:
        return 0
    have = {norm(a.get("name")) for a in cand.get("artists") or cand.get("ar") or []}
    wanta = _artists(artist)
    if wanta and have:
        if wanta & have:
            score += 50
        elif any(w in h or h in w for w in wanta for h in have):
            score += 25
        else:
            score -= 30
    if duration:
        dd = abs((cand.get("duration") or cand.get("dt") or 0) / 1000.0 - float(duration))
        score += 30 if dd < 2 else 15 if dd < 6 else 0 if dd < 20 else -20
    return score


def pick_song(results, title, artist, duration=None, hint_ids=None):
    """从网易云搜索结果挑 songId；hint_ids（播放缓存里出现过的 id）在可信候选里优先。"""
    best, best_score = None, 0
    for c in results or []:
        s = song_score(c, title, artist, duration)
        if s <= 0:
            continue
        if hint_ids and c.get("id") in hint_ids:
            s += 60
        if s > best_score:
            best, best_score = c.get("id"), s
    return best if best_score >= 60 else None


_PURE = ("纯音乐，请欣赏", "纯音乐,请欣赏", "此歌曲为没有填词的纯音乐")


def lyric_payload(raw):
    """网易云 /api/song/lyric 原始响应 → {status: ok|pure|none, lines, translated}。"""
    raw = raw or {}
    if raw.get("pureMusic"):
        return {"status": "pure", "lines": [], "translated": False}
    lrc = ((raw.get("lrc") or {}).get("lyric") or "")
    if any(p in lrc for p in _PURE):
        return {"status": "pure", "lines": [], "translated": False}
    lines = parse_lrc(lrc)
    if not [1 for _, x in lines if x]:
        return {"status": "none", "lines": [], "translated": False}
    tl = parse_lrc((raw.get("tlyric") or {}).get("lyric") or "")
    merged = merge_translation(lines, tl)
    return {"status": "ok", "lines": merged, "translated": any(l["tr"] for l in merged)}


def apply_stream_event(state, ev):
    """mediaremote-adapter `stream` 的一行 JSON 合并进当前状态（diff=None 值表示删除）。"""
    if not isinstance(ev, dict) or ev.get("type") != "data":
        return state
    payload = ev.get("payload") or {}
    new = dict(state) if ev.get("diff") else {}
    for k, v in payload.items():
        if v is None:
            new.pop(k, None)
        else:
            new[k] = v
    return new


def snapshot(raw, now=None):
    """adapter 状态（--micros）→ 页面用的字段；时间：pos/duration 秒，at 毫秒（epoch）。"""
    if not raw or not raw.get("title"):
        return {"state": "stopped"}
    now = time.time() if now is None else now
    playing = bool(raw.get("playing"))
    rate = float(raw.get("playbackRate") or 0) if playing else 0
    ts = raw.get("timestampEpochMicros")
    # 切歌缓冲期 playing 会来回跳：进度 0 且 3 秒内 → 显示成播放中（rate 0，不外推），别闪「Paused」
    if not playing and not raw.get("elapsedTimeMicros") and ts and now - ts / 1e6 < 3:
        playing = True
    return {
        "state": "playing" if playing else "paused",
        "title": raw.get("title") or "",
        "artist": raw.get("artist") or "",
        "album": raw.get("album") or "",
        "duration": round((raw.get("durationMicros") or 0) / 1e6, 3) or None,
        "pos": round((raw.get("elapsedTimeMicros") or 0) / 1e6, 3),
        "at": int(ts // 1000) if ts else int(time.time() * 1000),
        "rate": rate,
        "app": raw.get("bundleIdentifier") or "",
    }


class EstimateClock:
    """兜底计时：只知道是哪首歌时，以「第一次看到这首」为 0 秒起点。"""

    def __init__(self):
        self.key = None
        self.start = 0.0

    def update(self, key, now=None):
        now = time.time() if now is None else now
        if key != self.key:
            self.key, self.start = key, now
        return {"pos": 0.0, "at": int(self.start * 1000), "rate": 1.0}


# ---------------- IO ----------------
CONF_DIR = os.environ.get("MONITA_HOME") or os.path.join(os.path.expanduser("~"), ".monita-cs80")
CACHE_DIR = os.path.join(CONF_DIR, "lyrics")


def mra_candidates(env, home):
    """mediaremote-adapter 候选目录，按顺序探测（同 mac/Sources/MonitaCore/Controls.swift 的 mraCandidates）：
    设了 MONITA_MRA_DIR 就只认它；否则 $MONITA_HOME → ~/.monita-cs80 → ~/.cs80_panel（旧链路 host 的目录）。"""
    if env.get("MONITA_MRA_DIR"):
        return [env["MONITA_MRA_DIR"]]
    out = [os.path.join(env["MONITA_HOME"], "mediaremote-adapter")] if env.get("MONITA_HOME") else []
    for d in (os.path.join(home, ".monita-cs80", "mediaremote-adapter"), os.path.join(home, ".cs80_panel", "mediaremote-adapter")):
        if d not in out:
            out.append(d)
    return out


def pick_mra_dir(candidates, installed):
    """第一个装好了的；都没装返回第一个（日志里报它）"""
    return next((d for d in candidates if installed(d)), candidates[0])


def mra_installed(d):
    return os.path.exists(os.path.join(d, "bin", "mediaremote-adapter.pl")) and \
        os.path.isdir(os.path.join(d, "build", "MediaRemoteAdapter.framework"))


MRA_DIR = pick_mra_dir(mra_candidates(os.environ, os.path.expanduser("~")), mra_installed)
NETEASE_CACHE = os.path.join(os.path.expanduser("~"), "Library", "Application Support",
                             "com.netease.163music", "Caches", "online_play_cache")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"


def _ne_get(path, timeout=6, raw=False):
    req = urllib.request.Request("https://music.163.com" + path,
                                 headers={"Referer": "https://music.163.com", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return body if raw else json.loads(body.decode("utf-8", "replace"))


def recent_cache_ids(window=1800, root=NETEASE_CACHE):
    """网易云 online_play_cache 里最近 window 秒内动过的 songId（文件名 <id>-_-_<码率>...），新→旧。"""
    now = time.time()
    seen = {}
    for p in glob.glob(os.path.join(root, "*-_-_*")):
        m = re.match(r"(\d+)-_-_", os.path.basename(p))
        if not m:
            continue
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        if now - mt <= window:
            sid = int(m.group(1))
            seen[sid] = max(seen.get(sid, 0), mt)
    return [k for k, _ in sorted(seen.items(), key=lambda kv: -kv[1])]


class NowPlaying:
    """常驻 `perl mediaremote-adapter.pl FRAMEWORK stream`，后台线程维护最新状态。"""

    def __init__(self, root=MRA_DIR):
        self.script = os.path.join(root, "bin", "mediaremote-adapter.pl")
        self.framework = os.path.join(root, "build", "MediaRemoteAdapter.framework")
        self.raw = {}
        self.ok = False          # stream 至少吐过一行 data
        self.lock = threading.Lock()
        self.thread = None
        self.proc = None
        atexit.register(self.close)

    def close(self):
        """进程退出时带走 perl 子进程（被 kill -9 时它会在下一次写管道时 SIGPIPE 自己退）。"""
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def available(self):
        return os.path.exists(self.script) and os.path.isdir(self.framework) and os.path.exists("/usr/bin/perl")

    def _run(self):
        backoff = 2
        while True:
            try:
                proc = subprocess.Popen(
                    ["/usr/bin/perl", self.script, self.framework, "stream", "--micros", "--no-artwork",
                     "--no-diff", "--debounce=100"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                self.proc = proc
                for line in proc.stdout:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    with self.lock:
                        self.raw = apply_stream_event(self.raw, ev)
                        self.ok = True
                    backoff = 2
                proc.wait()
            except Exception:
                pass
            with self.lock:
                self.raw, self.ok = {}, False
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)

    def get(self):
        """None = adapter 不可用；否则返回原始状态 dict。"""
        if not self.available():
            return None
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
            for _ in range(15):  # 首次启动等一下第一行
                if self.ok:
                    break
                time.sleep(0.1)
        with self.lock:
            return dict(self.raw) if self.ok else None


class Lyrics:
    def __init__(self):
        self.np = NowPlaying()
        self.clock = EstimateClock()
        self.lock = threading.Lock()
        self.ids = {}        # 曲目 key → songId / None（没匹配上）
        self.lrc = {}        # songId → lyric_payload
        self.covers = {}     # songId → 封面 URL
        self.pending = set()
        self.retry = {}      # 曲目 key → 失败后下次可重试的时间
        self.details = {}    # 兜底：songId → (title, artist, duration)

    # ---- 歌词解析 ----
    def _resolve(self, key, title, artist, duration):
        try:
            sid = self._find_id(title, artist, duration)
            if sid:
                self._load_lyric(sid)
            with self.lock:
                self.ids[key] = sid
        except Exception:
            with self.lock:
                self.retry[key] = time.time() + 30
        finally:
            with self.lock:
                self.pending.discard(key)

    def _find_id(self, title, artist, duration):
        hints = set(recent_cache_ids())
        q = urllib.parse.quote(("%s %s" % (title, artist)).strip())
        res = _ne_get("/api/search/get?s=%s&type=1&limit=10" % q)
        songs = (res.get("result") or {}).get("songs") or []
        sid = pick_song(songs, title, artist, duration, hints)
        if sid is None and artist:  # 歌手名写法不一样时只按歌名再搜一次
            q = urllib.parse.quote(title)
            res = _ne_get("/api/search/get?s=%s&type=1&limit=10" % q)
            sid = pick_song((res.get("result") or {}).get("songs") or [], title, artist, duration, hints)
        return sid

    def _load_lyric(self, sid):
        with self.lock:
            if sid in self.lrc:
                return self.lrc[sid]
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = os.path.join(CACHE_DIR, "%d.json" % sid)
        raw = None
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    raw = json.load(f)
            except ValueError:
                raw = None
        if raw is None:
            raw = _ne_get("/api/song/lyric?id=%d&lv=1&tv=1" % sid)
            if raw.get("code") != 200:
                raise RuntimeError("lyric code %s" % raw.get("code"))
            try:
                det = _ne_get("/api/song/detail/?ids=%s" % urllib.parse.quote("[%d]" % sid))
                s = (det.get("songs") or [{}])[0]
                raw["_cover"] = (s.get("album") or {}).get("picUrl")
                raw["_detail"] = [s.get("name"), "/".join(a.get("name", "") for a in s.get("artists") or []),
                                  (s.get("duration") or 0) / 1000.0]
            except Exception:
                pass
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False)
            os.replace(tmp, path)
        p = lyric_payload(raw)
        with self.lock:
            self.lrc[sid] = p
            if raw.get("_cover"):
                self.covers[sid] = raw["_cover"]
            if raw.get("_detail"):
                self.details[sid] = tuple(raw["_detail"])
        return p

    def cover(self, sid):
        """封面 jpg 字节（本服务转发，客户端只连一个地址）。"""
        sid = int(sid)
        path = os.path.join(CACHE_DIR, "%d.jpg" % sid)
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()
        url = self.covers.get(sid)
        if not url:
            self._load_lyric(sid)
            url = self.covers.get(sid)
        if not url:
            return None
        req = urllib.request.Request(url + "?param=300y300", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = resp.read()
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return data

    # ---- 当前播放 ----
    def _fallback(self):
        """没有 adapter：网易云播放缓存里最新的 songId，进度从第一次看到它开始估算。"""
        ids = recent_cache_ids(window=900)
        if not ids:
            return {"state": "stopped", "source": "none"}
        sid = ids[0]
        try:
            self._load_lyric(sid)
        except Exception:
            pass
        title, artist, duration = self.details.get(sid, ("", "", None))
        if not title:
            return {"state": "stopped", "source": "none"}
        est = self.clock.update(sid)
        return dict(est, state="playing", title=title, artist=artist, album="", duration=duration,
                    app="com.netease.163music", source="estimate", estimated=True, songId=sid)

    def payload(self):
        raw = self.np.get()
        if raw is None:
            np = self._fallback()
        else:
            np = dict(snapshot(raw), source="mediaremote", estimated=False)
        if np.get("state") == "stopped":
            return dict(np, lyric={"status": "idle", "lines": []})
        key = np.get("songId") or (norm(np["title"]), norm(np["artist"]))
        if np.get("songId"):
            with self.lock:
                self.ids[key] = np["songId"]
        with self.lock:
            known = key in self.ids
            sid = self.ids.get(key)
            busy = key in self.pending
            can_retry = time.time() >= self.retry.get(key, 0)
            if not known and not busy and can_retry:
                self.pending.add(key)
                threading.Thread(target=self._resolve, daemon=True,
                                 args=(key, np["title"], np["artist"], np.get("duration"))).start()
                busy = True
            lyric = self.lrc.get(sid) if sid else None
        if lyric is None:
            status = "loading" if busy else ("nomatch" if known else "error")
            lyric = {"status": status, "lines": []}
        np["songId"] = sid
        np["cover"] = ("/api/lyrics/cover?id=%d" % sid) if sid and self.covers.get(sid) else None
        np["lyric"] = lyric
        np["now"] = int(time.time() * 1000)
        return np


_LYRICS = None


def lyrics():
    """懒加载单例：import 本模块（比如跑测试）不会起后台线程。"""
    global _LYRICS
    if _LYRICS is None:
        _LYRICS = Lyrics()
    return _LYRICS


def serve(port=8788, bind="127.0.0.1"):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    cache = {"at": 0.0, "body": b""}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path == "/api/data/lyrics":
                with lock:  # 0.8 s 内的重复请求直接给缓存
                    if time.time() - cache["at"] > 0.8:
                        cache["body"] = json.dumps(lyrics().payload(), ensure_ascii=False).encode("utf-8")
                        cache["at"] = time.time()
                    body = cache["body"]
                return self._send(200, body)
            if u.path == "/api/lyrics/cover":
                try:
                    img = lyrics().cover((urllib.parse.parse_qs(u.query).get("id") or ["0"])[0])
                except Exception:
                    img = None
                if not img:
                    return self._send(404, b'{"error":"no cover"}')
                return self._send(200, img, "image/jpeg")
            return self._send(404, b'{"error":"not found"}')

    srv = ThreadingHTTPServer((bind, port), Handler)
    print("lyrics: http://%s:%d/api/data/lyrics" % (bind, port), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="当前播放 + 网易云歌词")
    sub = ap.add_subparsers(dest="cmd")
    sp = sub.add_parser("serve", help="起 HTTP 服务")
    sp.add_argument("--port", type=int, default=8788)
    sp.add_argument("--bind", default="127.0.0.1")
    sub.add_parser("once", help="打印一次当前播放 + 歌词（调试用）")
    a = ap.parse_args()
    if a.cmd == "serve":
        serve(a.port, a.bind)
    elif a.cmd == "once":
        print(json.dumps(lyrics().payload(), ensure_ascii=False, indent=1))
    else:
        ap.print_help()
