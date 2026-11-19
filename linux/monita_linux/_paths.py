"""把仓库里现成的 tools/host-ref（协议）和 tools/lyrics（歌词）加进 sys.path。"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _d in (os.path.join(REPO, "tools", "host-ref"), os.path.join(REPO, "tools", "lyrics")):
    if _d not in sys.path:
        sys.path.insert(0, _d)
