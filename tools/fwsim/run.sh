#!/bin/sh
# 不用设备测上位机: 编固件模拟器 (fwsim), Python 参考实现和 Mac 渲染器 (Swift, debug 构建) 各跑一遍对照脚本,
# 结果要和 docs/protocol.md 一致、两边也一致。见 tools/fwsim/conformance.py。
#   sh tools/fwsim/run.sh                # Python + Swift (没有 swift 就只测 Python)
#   sh tools/fwsim/run.sh --python-only
set -eu
cd "$(dirname "$0")/../.."
mkdir -p tools/fwsim/build
cc -std=c99 -Wall -Wextra -Werror -O2 -Ifw/link -o tools/fwsim/build/fwsim tools/fwsim/fwsim.c
SWIFT=
if [ "${1:-}" != "--python-only" ] && command -v swift >/dev/null 2>&1; then
  echo "=== swift build (debug) monita-cs80"
  if ! out=$(cd mac && swift build --product monita-cs80 2>&1); then echo "$out"; exit 1; fi
  SWIFT=mac/.build/debug/monita-cs80
fi
exec python3 -I tools/fwsim/conformance.py --sim tools/fwsim/build/fwsim ${SWIFT:+--swift "$SWIFT"}
