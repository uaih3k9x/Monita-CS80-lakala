# RECT_LZ4 解码的 QEMU 指令计数（粗估）

目的：在没上真机之前，用指令数给 `fw/link/lz4.h` 的解码耗时一个比 docs/protocol.md "A5 估计"（拍脑袋的周期数）更有依据的数量级。
**这是粗估**：只数指令，不含 PSRAM 等待、D-cache / I-cache 命中、XIP 读 Flash、流水线停顿；真机数字以 DIAG 33（固件内自测）为准。

## 怎么跑

```sh
sh tools/lz4bench/qemu/run.sh
```

一键：编插件 → 用和 `fw/build.sh` 同一套 CFLAGS 交叉编裸机小程序 → `qemu-system-arm` 跑 → 打印每段每遍的计数和换算。产物在 `tools/lz4bench/qemu/build/`（不提交）。

| 文件 | 作用 |
|---|---|
| `qemu/cnt.c` | 最小 TCG 插件：执行到 `b=NAME@ADDR` 开始一段、到 `e=ADDR` 结束，累计指令数、访存指令数（load / store 各几条）、访问次数和字节数（ldm/stm 一条指令、每个字算一次访问） |
| `qemu/bench.c` | 裸机小程序：同一份 `fw/link/lz4.h`、`proto.h`，测试条带 `fw/link/lz4_testdata.h`；每段 16 遍，标记函数 `m_b_*` / `m_end` 夹住被测代码，每遍校验 CRC-32（不计数） |
| `qemu/start.S`、`qemu/link.ld` | 入口（栈、开 VFP/NEON：CPACR cp10/cp11 + FPEXC.EN、清 bss）、半主机打印 / 退出；加载到 virt 的 RAM 0x40010000 |
| `qemu/report.py` | 读插件输出，减掉标记开销（`empty` 段），按 CPI / 主频换算 |
| `gen_testdata.py` | 生成测试条带（DIAG 33 也用同一份） |

环境（本次结果）：

- 工具链：arm-none-eabi-gcc 13.3.1 20240614（Arm GNU Toolchain 13.3.Rel1），binutils 同包
- QEMU：10.2.0（Homebrew），插件 API `QEMU_PLUGIN_VERSION` 5；插件用系统 cc 编成 `.dylib`：
  `cc -shared -fPIC -O2 -I<qemu>/include $(pkg-config --cflags glib-2.0) -undefined dynamic_lookup -o libcnt.dylib cnt.c`
  （符号由 qemu 本体提供，所以 macOS 上要 `-undefined dynamic_lookup`；Linux 上编 `.so`，run.sh 自动区分）
- 编译选项（同 `fw/build.sh`，只把 `-I` 换成 `fw/link`）：
  `-mcpu=cortex-a5 -mthumb -mfpu=neon-vfpv4 -mfloat-abi=hard -mlong-calls -Os -ffreestanding -fno-builtin -fno-tree-loop-distribute-patterns -fno-tree-vectorize -ffunction-sections -fdata-sections -Wall -Wextra -Werror -nostdlib`
- 运行：
  `qemu-system-arm -M virt -cpu cortex-a7 -m 128 -nographic -monitor none -serial none -semihosting-config enable=on,target=native -kernel build/bench.elf -plugin build/libcnt.dylib,e=<m_end>,b=empty@<m_b_empty>,b=dec@<m_b_dec>,... -d plugin -D build/counts.txt`
  （地址由 run.sh 从 `arm-none-eabi-nm` 取）

为什么不是 `qemu-arm` + `libinsn`：macOS 上 QEMU 没有 linux-user 模式（只有 `qemu-system-*`），Homebrew 包也不带 contrib 插件，
所以改成 system 模式跑裸机程序 + 自写插件（API 一样）。QEMU 没有 Cortex-A5 模型，用 cortex-a7：同为 ARMv7-A + Thumb-2 + NEON-VFPv4，
二进制是按 `-mcpu=cortex-a5` 编的，执行的就是固件里的那种指令序列；指令数跟 CPU 模型无关（TCG 不模拟时序）。

## 输入

`fw/link/lz4_testdata.h`：`docs/img/lyrics-playing.png` 第 62 条（y = 1240..1259，800×20 RGB565 = 32000 B），liblz4 1.9.4 压成 1319 B。
这一帧 64 条共 83930 B、平均 1311 B/条，这条压缩后大小最接近平均，所以 ×64 **近似**这一整帧（歌词页，表里偏重的 UI 画面；
各条的序列构成不同，×64 只是近似）。解码输入先拷进 RAM（同线上：块在 `g_pl` 里）。

## 结果（每遍，已减标记开销 5 条指令 / 2 次访问）

| 段 | 说明 | 指令 | 访存指令 | load 次数 | store 次数 | load 字节 | store 字节 | 指令 / 输出字节 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| dec | `lz4_decode` 1319 B → 32000 B 暂存区 | 78,231 | 18,655 | 9,164 | 9,491 | 28,190 | 32,000 | 2.44 |
| rect | `lk_rect_apply` 暂存区 → 800 宽帧缓冲（线上第二段） | 40,268 | 16,027 | 8,026 | 8,001 | 32,096 | 32,004 | 1.26 |
| ldm | `fw/app.c` copy_rows 的 ldmia/stmia r4-r11 循环拷 32000 B（不含 dcache_clean） | 4,008 | 2,003 | 8,010 | 8,009 | 32,040 | 32,036 | 0.125 |
| cpy8 | 逐字节 C 循环拷 32000 B（对照） | 128,008 | 64,005 | 32,005 | 32,000 | 32,020 | 32,000 | 4.0 |

16 遍的数字完全一样（TCG 指令计数是确定的）；每遍解完 / 拷完 CRC 都对。

- 解码：store 9491 次写 32000 B（平均 3.4 B/次）→ 绝大部分输出走了按字写的快路径；load 9164 次读 28190 B，里面多数是 match 从已解输出按字读。
  约 2.4 条指令 / 输出字节。
- `lk_rect_apply` 每个字约 5 条指令（`lk_row_move` 的逐字循环），是 ldm/stm 写法的 10 倍；但这一段在真机上受 PSRAM 写带宽限制（见下），指令不是瓶颈。

## 换算（时间 = 指令 × CPI / 主频；粗估）

| 段 | CPI 1.0 @ 500 MHz | CPI 1.0 @ 400 MHz | CPI 1.5 @ 500 MHz | CPI 1.5 @ 400 MHz |
|---|---|---|---|---|
| dec，每条带 | 156 us | 196 us | 235 us | 293 us |
| dec，整帧 ×64 | **10.0 ms** | 12.5 ms | 15.0 ms | **18.8 ms** |
| rect，整帧 ×64（只算指令） | 5.2 ms | 6.4 ms | 7.7 ms | 9.7 ms |
| ldm 拷 2 MB（×64，只算指令） | 0.5 ms | 0.6 ms | 0.8 ms | 1.0 ms |
| cpy8 拷 2 MB（×64） | 16.4 ms | 20.5 ms | 24.6 ms | 30.7 ms |
| dec + rect，整帧 | 15.2 ms | 19.0 ms | 22.7 ms | 28.4 ms |

和 docs/protocol.md 的 "A5 估计" 对照：

- 文档估 **解码 8–10 ms / 帧**（全逐字节 21–27 ms）。按指令数，CPI 1.0 @ 500 MHz 正好 10 ms，CPI 1.5 @ 400 MHz 到 19 ms：
  **文档的估计落在乐观端**，按 10–19 ms 看更稳。Cortex-A5 是单发射顺序核，有 load-use 停顿和分支（解码循环里分支很多），CPI 1.0 偏乐观。
- 暂存区 → 帧缓冲：指令只要 5–10 ms / 帧，而实测 PSRAM 写带宽约 64 MB/s → 2 MB 要约 31 ms（文档值）。ldm 循环的指令时间只有 0.5–1 ms，
  说明那 31 ms 全是存储器等待；`lk_rect_apply` 换成 ldm 写法省指令但省不了这 31 ms。
- 合计设备侧（不算传输）：解码 10–19 ms + 拷贝 ≈ 31 ms（带宽）≈ **40–50 ms / 帧**，和文档 "整帧 40–50 ms（40–80 ms 看 D-cache）" 一致；
  解码的输出若也要写回 PSRAM（暂存区 32 KB 和 D-cache 同量级），再加的那部分不在这里。

## 局限

- 只数指令：PSRAM 延迟、cache 命中、XIP（DIAG 33 的"输入在 Flash"变体）、写缓冲、分支预测都不在内。CPI 1.0 / 1.5 是假设，不是测量。
- 标记开销已减掉（每遍 5 条指令），调用 / 返回的那几条还在被测段里，对 4 万–8 万条的段可以忽略。
- 测试条带 ×64 ≈ 一整帧只是近似；轻的画面（锁屏 29 KB、时钟 54 KB）会更快。
- 小程序里 `lz4_decode` 内联进了 `main`，固件里内联进 `on_frame` 一带；两边同一编译选项（-Os），生成的循环形态应一致，但寄存器分配可能略有差别。
