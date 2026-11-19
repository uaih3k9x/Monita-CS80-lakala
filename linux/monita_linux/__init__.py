"""Monita-CS80 的 Linux 上位机（见 linux/DESIGN.md）。协议层复用 tools/host-ref/monita_link.py，歌词复用 tools/lyrics/lyrics.py。"""
from . import _paths  # noqa: F401  把 tools/host-ref、tools/lyrics 加进 sys.path
