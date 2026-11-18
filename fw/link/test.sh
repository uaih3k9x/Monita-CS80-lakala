#!/bin/sh
# 主机上跑 fw/link/proto.h 的单测 (cc = 系统自带 clang/gcc 即可)
cd "$(dirname "$0")"
out=$(mktemp)
cc -std=c99 -Wall -Wextra -Werror -O1 -o "$out" test_proto.c
"$out"; rc=$?
for t in test_zc_logic.c test_blit.c test_tribuf.c; do
  [ $rc -eq 0 ] && cc -std=c99 -Wall -Wextra -Werror -O1 -o "$out" "$t" && "$out"; rc=$?
done
rm -f "$out"; exit $rc
