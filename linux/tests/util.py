"""测试公用：把 linux/ 加进 sys.path，再给几个假实现。"""
import os
import sys

LINUX = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if LINUX not in sys.path:
    sys.path.insert(0, LINUX)

import monita_linux  # noqa: E402,F401
from monita_linux._paths import REPO  # noqa: E402,F401


class FakeClock:
    """假时钟：t 手动拨；同时当 mono 用"""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt

import logging  # noqa: E402

logging.getLogger("monita").addHandler(logging.NullHandler())   # 单测里不刷屏；assertLogs 照样收得到
logging.getLogger("monita").propagate = False
