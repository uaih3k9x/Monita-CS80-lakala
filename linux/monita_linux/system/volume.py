"""音量：wpctl（PipeWire）优先，没有就 pactl（PulseAudio / pipewire-pulse）；都没有只打日志（DESIGN.md §3.9）。
wpctl 在但跑不通（老版本没有 -l、实际跑的是 PulseAudio）：记一次日志，之后改用 pactl（装了的话）。
+ 顺带解除静音，− 不动静音；不超过 100%。子进程超时 2 s。"""
import logging
import re
import shutil
import subprocess

log = logging.getLogger("monita.volume")


class Volume:
    def __init__(self, kind, run=subprocess.run, fallback=None):
        self.kind, self.run, self.fallback = kind, run, fallback

    def _run(self, cmd):
        try:
            return self.run(cmd, capture_output=True, text=True, timeout=2, check=False)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("%s：%s", cmd[0], e)
            return None

    def step(self, pct):
        if self.kind == "wpctl":
            r = self._run(["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "%d%%%s" % (abs(pct), "+" if pct > 0 else "-")])
            if r is not None and r.returncode != 0:
                log.warning("wpctl 失败（%s）%s", (r.stderr or "").strip()[:200] or r.returncode,
                            "，改用 pactl" if self.fallback else "")
                if self.fallback:
                    self.kind, self.fallback = self.fallback, None
                    self.step(pct)
                return
            if pct > 0:
                self._run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"])
        elif self.kind == "pactl":
            if pct > 0:
                r = self._run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"])
                cur = max([int(v) for v in re.findall(r"(\d+)%", r.stdout or "")] or [0]) if r else 0
                if cur < 100:
                    self._run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", "+%d%%" % min(pct, 100 - cur)])
                self._run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"])
            else:
                self._run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", "-%d%%" % -pct])
        else:
            log.info("没有 wpctl / pactl，音量键 %+d%% 忽略", pct)


def detect(which=shutil.which, run=subprocess.run):
    have = [k for k in ("wpctl", "pactl") if which(k)]
    return Volume(have[0] if have else None, run=run, fallback="pactl" if have == ["wpctl", "pactl"] else None)
