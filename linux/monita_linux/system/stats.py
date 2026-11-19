"""Front Panel / 段码屏用的本机数据：CPU（/proc/stat）、电池（/sys/class/power_supply）、负载（/proc/loadavg）。
root 可注入（测试用临时目录）；读不到一律 None（页面显示 "–"）。"""
import glob
import os


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def cpu_times(root="/"):
    """-> (忙, 总) jiffies；idle 算上 iowait"""
    s = _read(os.path.join(root, "proc/stat"))
    if not s or not s.startswith("cpu "):
        return None
    v = [int(x) for x in s.split("\n", 1)[0].split()[1:9]]
    idle = v[3] + (v[4] if len(v) > 4 else 0)
    return (sum(v) - idle, sum(v))


def cpu_fraction(prev, cur):
    if not prev or not cur or cur[1] <= prev[1]:
        return None
    return max(0.0, min(1.0, (cur[0] - prev[0]) / (cur[1] - prev[1])))


def battery(root="/"):
    """-> (百分比, 接着交流电) 或 None（台式机）"""
    base = os.path.join(root, "sys/class/power_supply")
    caps = sorted(glob.glob(os.path.join(base, "BAT*", "capacity")))
    if not caps:
        return None
    try:
        pct = int((_read(caps[0]) or "").strip())
    except ValueError:
        return None
    ac = any((_read(p) or "").strip() == "1" for p in glob.glob(os.path.join(base, "AC*", "online")) + glob.glob(os.path.join(base, "ADP*", "online")))
    return (pct, ac)


def loadavg(root="/"):
    s = _read(os.path.join(root, "proc/loadavg"))
    try:
        return float(s.split()[0]) if s else None
    except (ValueError, IndexError):
        return None


class Stats:
    def __init__(self, root="/"):
        self.root = root
        self.prev = None
        self.cpu = None
        self.bat = None
        self.load = None

    def sample(self):
        cur = cpu_times(self.root)
        f = cpu_fraction(self.prev, cur)
        self.prev = cur
        self.cpu = None if f is None else int(round(f * 100))
        self.bat = battery(self.root)
        self.load = loadavg(self.root)

    def panel(self):
        return dict(cpu=self.cpu, bat=self.bat[0] if self.bat else None, load=self.load)
