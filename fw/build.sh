#!/bin/bash
# 编译 + 静态校验 R05 app。不连设备、不刷机。
#
#   bash fw/build.sh                 # 默认: app + lcd/lcd_test
#   bash fw/build.sh diag/probe      # 指定目标 (源文件路径去掉 .c, 相对 fw/)
#   bash fw/build.sh all             # 全部, 含 diag/
#
# 每个目标 X 需要 X.c 和 X.imports (导入清单, 一行一个名字; "名字 = 0xHASH" 用于名字未解出的导出)。
# 产物: fw/build/<目标名>.bin (2PPA 容器, 写到 NOR 0x60270000; 刷写流程见 docs/r05-notes.md)。
# 依赖: arm-none-eabi-gcc / binutils, python3。
set -euo pipefail
cd "$(dirname "$0")"
CC=arm-none-eabi-gcc
# -fno-tree-loop-distribute-patterns: 不许把循环改写成 memset/memcpy 调用 (没有 libc)
# -fno-tree-vectorize: 不生成 NEON (任务切换是否保存 VFP 现场未证实); validate.py 也会查
CFLAGS="-mcpu=cortex-a5 -mthumb -mfpu=neon-vfpv4 -mfloat-abi=hard -mlong-calls \
  -Os -ffreestanding -fno-builtin -fno-tree-loop-distribute-patterns -fno-tree-vectorize \
  -ffunction-sections -fdata-sections -Wall -Wextra -Werror -nostdlib -Isdk -Ifg00 -Ilcd -Ilink"

TARGETS=("$@")
[ ${#TARGETS[@]} -eq 0 ] && TARGETS=(app lcd/lcd_test)
[ "${TARGETS[0]}" = all ] && TARGETS=(app lcd/lcd_test diag/probe diag/segtest diag/segprobe)

mkdir -p build
for T in "${TARGETS[@]}"; do
  N=$(echo "$T" | tr / _)
  O=build/$N
  echo "=== build $T"
  python3 -I sdk/gen_stubs.py "$T.imports" "$O.corestub.S" "$O.hashes.txt"
  $CC $CFLAGS -c "$T.c" -o "$O.o"
  $CC $CFLAGS -c "$O.corestub.S" -o "$O.corestub.o"
  arm-none-eabi-ld -T sdk/app.ld --gc-sections -o "$O.elf" "$O.o" "$O.corestub.o"
  (cd build && python3 -I ../sdk/pack.py "$N.elf" "$N.bin")
  echo "=== static validation $T"
  python3 -I sdk/validate.py "$O.bin" "$O.elf"
done
(cd build && shasum -a 256 *.bin)
