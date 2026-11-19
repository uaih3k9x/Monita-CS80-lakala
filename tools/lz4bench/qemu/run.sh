#!/bin/sh
# QEMU 指令计数 (tools/lz4bench/QEMU.md): 编插件 + 裸机小程序, qemu-system-arm 跑, 打印每段每遍的指令 / 访存和换算。
#   sh tools/lz4bench/qemu/run.sh
# 依赖: arm-none-eabi-gcc / binutils, qemu-system-arm (带 TCG 插件支持, Homebrew qemu 即可), cc, pkg-config + glib 头文件, python3。
set -eu
cd "$(dirname "$0")/../../.."
Q=tools/lz4bench/qemu
B=$Q/build
mkdir -p "$B"
QEMU=${QEMU:-qemu-system-arm}
QINC=${QEMU_PLUGIN_INC:-$(dirname "$(dirname "$(realpath "$(command -v "$QEMU")")")")/include}
# 1) 插件 (macOS: .dylib, 符号由 qemu 本体提供 -> -undefined dynamic_lookup; Linux 上改成 .so, 不要这个选项)
SO=$B/libcnt.dylib; LDX="-undefined dynamic_lookup"
[ "$(uname)" = Darwin ] || { SO=$B/libcnt.so; LDX=; }
cc -shared -fPIC -O2 -Wall -Wextra -I"$QINC" $(pkg-config --cflags glib-2.0) $LDX -o "$SO" $Q/cnt.c
# 2) 裸机小程序: 和 fw/build.sh 同一套 CFLAGS (只把 -I 换成 fw/link)
CFLAGS="-mcpu=cortex-a5 -mthumb -mfpu=neon-vfpv4 -mfloat-abi=hard -mlong-calls \
  -Os -ffreestanding -fno-builtin -fno-tree-loop-distribute-patterns -fno-tree-vectorize \
  -ffunction-sections -fdata-sections -Wall -Wextra -Werror -nostdlib -Ifw/link"
arm-none-eabi-gcc $CFLAGS -c $Q/bench.c -o $B/bench.o
arm-none-eabi-gcc -mcpu=cortex-a5 -mfpu=neon-vfpv4 -mfloat-abi=hard -c $Q/start.S -o $B/start.o
arm-none-eabi-ld -T $Q/link.ld --gc-sections --no-warn-rwx-segments -o $B/bench.elf $B/start.o $B/bench.o
arm-none-eabi-nm $B/bench.elf > $B/bench.nm
sym(){ awk -v s="$1" '$3 == s { print "0x" $1 }' $B/bench.nm; }
ARGS="e=$(sym m_end)"
for p in empty dec rect cpy8 ldm; do ARGS="$ARGS,b=$p@$(sym m_b_$p)"; done
# 3) 跑: cortex-a7 (QEMU 没有 cortex-a5; 两者都是 ARMv7-A + Thumb-2 + NEON-VFPv4, 指令集对这段代码一样)
rm -f $B/counts.txt
"$QEMU" -M virt -cpu cortex-a7 -m 128 -nographic -monitor none -serial none \
  -semihosting-config enable=on,target=native -kernel $B/bench.elf \
  -plugin "$SO,$ARGS" -d plugin -D $B/counts.txt
python3 -I $Q/report.py $B/counts.txt
