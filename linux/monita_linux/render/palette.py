"""CDE Default.dp 色板（和 mac/Sources/monita-cs80/LyricsPage.swift 的 C 一致）"""


def rgb(h):
    n = int(h.lstrip("#"), 16)
    return (n >> 16 & 255, n >> 8 & 255, n & 255)


DESK, WIN, TITLE, TITLE_FG = rgb("#5d7f99"), rgb("#aeb2c3"), rgb("#a5627e"), rgb("#ffffff")
WELL, FG, DIM, FAINT = rgb("#c6cad8"), rgb("#000000"), rgb("#4b4f63"), rgb("#6f7389")
SELECT, SELECT_FG, TROUGH, FILL = rgb("#5b6c8f"), rgb("#ffffff"), rgb("#8f93a6"), rgb("#a5627e")
PANEL, WARN, ON = rgb("#9aa3ba"), rgb("#e8c76a"), rgb("#8c8fb8")
WHITE, TR_FG = rgb("#ffffff"), rgb("#dfe3f2")
PLAY_GREEN, ICON_BLUE, LOCK_GOLD = rgb("#57b05a"), rgb("#2f4f9e"), rgb("#d6b44a")
