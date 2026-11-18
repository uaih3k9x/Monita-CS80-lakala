#!/bin/sh
# 编译 ungive/mediaremote-adapter（BSD-3），给歌词页拿「当前播放 + 进度」用。
# macOS 15.4+ 第三方进程直接调 MediaRemote 拿不到 Now Playing；这个项目借 /usr/bin/perl
# （系统自带、带权限）加载自编译的 framework。只装到用户目录，不动系统。
#
#   sh tools/lyrics/build-nowplaying.sh     # 产物：~/.monita-cs80/mediaremote-adapter/
#   MONITA_MRA_DIR=/别的路径 sh ...         # lyrics.py 读同一个环境变量
#
# 依赖：Xcode Command Line Tools（clang）+ cmake（brew install cmake）+ git。
set -eu
DIR="${MONITA_MRA_DIR:-$HOME/.monita-cs80/mediaremote-adapter}"
REPO=https://github.com/ungive/mediaremote-adapter.git
REV=14eed9389241d06e29f7326c19e9ebc6ec8a5f36   # 2026-11-11，macOS 15.6 实测可用

command -v cmake >/dev/null || { echo "缺 cmake：brew install cmake" >&2; exit 1; }
command -v clang >/dev/null || { echo "缺 clang：xcode-select --install" >&2; exit 1; }

if [ ! -d "$DIR/.git" ]; then
  mkdir -p "$(dirname "$DIR")"
  git clone "$REPO" "$DIR"
fi
git -C "$DIR" fetch --quiet origin "$REV" 2>/dev/null || true
git -C "$DIR" checkout --quiet "$REV"
cmake -S "$DIR" -B "$DIR/build" -DCMAKE_BUILD_TYPE=Release >/dev/null
cmake --build "$DIR/build" --config Release

FW="$DIR/build/MediaRemoteAdapter.framework"
if /usr/bin/perl "$DIR/bin/mediaremote-adapter.pl" "$FW" "$DIR/build/MediaRemoteAdapterTestClient" test; then
  echo "OK：adapter 可用。当前播放："
  /usr/bin/perl "$DIR/bin/mediaremote-adapter.pl" "$FW" get --no-artwork || true
  echo
else
  echo "adapter 自检失败（可能 Apple 又改了权限），歌词页会退回「进度为估算」模式" >&2
  exit 2
fi
