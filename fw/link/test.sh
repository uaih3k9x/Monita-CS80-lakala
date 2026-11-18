#!/bin/sh
# 主机上跑 fw/link/*.h 和 fw/lcd/bootscreen.h 的单测 (cc = 系统自带 clang/gcc 即可; 开机画面对照要 python3)
cd "$(dirname "$0")"
out=$(mktemp)
cc -std=c99 -Wall -Wextra -Werror -O1 -o "$out" test_proto.c
"$out"; rc=$?
for t in test_zc_logic.c test_blit.c test_tribuf.c test_keys.c test_led.c; do
  [ $rc -eq 0 ] && cc -std=c99 -Wall -Wextra -Werror -O1 -o "$out" "$t" && "$out"; rc=$?
done
# 开机版权页: 字体表是否和 LaffStd.c 同步; C 画的 vs tools/bootscreen/render_ref.py 逐像素
if [ $rc -eq 0 ]; then
  tmp=$(mktemp -d)
  F=../../tools/edk2font/LaffStd.c
  REF="python3 -I ../../tools/bootscreen/render_ref.py $F"
  python3 -I ../../tools/gen_font.py "$F" "$tmp/font_uefi.h" >/dev/null && cmp "$tmp/font_uefi.h" ../lcd/font_uefi.h \
  && $REF "$tmp/ref_design.ppm" \
  && $REF "$tmp/ref_hello.ppm" --ver 7 --date 2026-11-08 --free 312 --nbuf 2 --seg fail --link ok \
  && $REF "$tmp/ref_single.ppm" --ver 7 --free -1 --nbuf 1 \
  && cc -std=c99 -Wall -Wextra -Werror -O1 -I../lcd -o "$out" test_bootscreen.c && "$out" "$tmp"; rc=$?
  rm -rf "$tmp"
fi
rm -f "$out"; exit $rc
