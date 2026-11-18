# tools/fwsim：固件模拟器 + 上位机对拍

`sh tools/fwsim/run.sh`：编模拟器 `fwsim`，Python 参考实现（`tools/host-ref`）和 Mac 渲染器（`mac/`，debug 构建，
`monita-cs80 --conformance`）对着它各跑一遍同一套脚本（`conformance.py` / `mac/Sources/monita-cs80/Conformance.swift`），
结果和 `expected()`（照 `docs/protocol.md` 写的）逐项比，两个上位机之间也要一样。`--python-only` 只测 Python。

## 模拟器里是什么

- 真固件代码：`fw/link/dispatch.h`（on_frame / ACK / KEY / 主机在不在）+ proto / blit / tribuf / imm / keys / led / linkwd，
  段码屏本地时钟 `fw/fg00/segclock.h`（`clk_set` / `clk_tick` / `seg_draw_local`）和显存 `fw/fg00/fgbuf.h`、`segmap.h`。
  这两个头是从 `fw/app.c` / `fg00.h` 原样搬出来的，实机固件编出来逐字节不变（`bash fw/build.sh all` 的 5 个 .bin sha256 和搬之前一样）。
- 假硬件（`simdev.h`）：3 块帧缓冲 + 卷轴、帧事件立即发生、按键电平注入；`fg_flush` / 背光 / 灯 GPIO 只记账。时间是真实单调时钟。

## stdin 命令

`tap K [ms]` / `down K` / `up K`（K：1 查询/PWRKEY，2 ≡，3 +，4 −），`stat`，`quit`。`stat` 回一行 JSON：

| 字段 | 含义 |
|---|---|
| `screen_crc` `nbuf` `imm` `roll_rows` `collapse` `uncollapse` `flip_now` | 屏上那一帧的 CRC、多缓冲块数、沉浸模式、卷轴行数、旧 DIAG 收拢次数 |
| `rx_bytes` `frames` `crc_err` `len_err` `rects` `presents` `bad_param` | 同 ACK 里的计数 |
| `link_up` `key_n` `boot_live` | 主机在不在（linkwd）、下一条 KEY 的序号、开机页还在（Link 行 WAIT） |
| `backlight` `bl_writes` `seg_writes` | 背光当前值 / 写了几次、段码屏刷了几次 |
| `fg` | 段码屏显存 72 字节（十六进制） |
| `clk_valid` `clk_sod` `seg_host_seen` | 设备时钟有没有设、当地时间一天中第几秒、主机发过 SEG |
| `led` | 此刻灯的输出掩码（bit0 R / bit1 G / bit2 B） |
| `led_mode` `led_mask` `led_lit` `led_on` `led_off` `led_next_in` `led_timed` `led_until_in` | 灯状态机（led.h）；`*_in` 是离现在多久（1.024 ms 单位，可负） |
| `im_ys` `im_ytgt` `im_anim` `im_y0` `im_d` `im_dur` `im_elapsed` `im_pos` | 滚动：扫描中 / 目标窗口顶、在不在缓动、起点、位移、时长、已过多久、此刻缓动位置 |

## 对拍查固件副作用（Probe）

协议回包之外，脚本走到下面这些点时 harness 取一次 `stat`（Swift 打印 `@stat 名字`，等 harness 回一行再继续），
归一成和时间无关的值放进结果的 `sim`，也参加 python == swift 比较。
HELLO 的时间由 harness 注入固定值（`CONFIGS` 的 `now` + `zone`；Python 用 `Link.clock` + `TZ`，Swift 用 `MONITA_FAKE_NOW` + `TZ`），
期望的当地时间用 `zoneinfo` 独立算。四种配置的时区：America/New_York 夏令时（-240，跨到前一天）、Asia/Kolkata（+330，跨到第二天）、
Europe/London 夏令时（+60）、Australia/Adelaide 南半球夏令时（+630）。

| 点 | 检查 |
|---|---|
| `boot`（连上之前） | 段码屏 `------`、时钟没设；蓝灯闪，亮 2000 / 灭 1000，相位对（输出和 lit 一致、下次翻转在本段内）；背光开；没连主机；开机页还在 |
| `hello` | 设备时钟 = 注入时刻的当地时间（差 0–2 s）；段码屏 = `------ hh:mm:ss` 且和 `clk_sod` 一致（逐位重画比对，多亮/少亮一段都算错）；灯灭；`key_n` 0；开机页已退 |
| `seg` | 显存 = 主机发的 72 字节图案（71 字节那条被拒、不动显存） |
| `keys`（SEG 后 1–4.8 s） | 本地时钟还没盖掉主机的 SEG |
| `backlight` | BACKLIGHT 0 生效（空的被拒），共写 2 次 |
| `led` | 绿灯闪 500 / 100 生效（mode / mask / on / off / 相位），两条被拒的不动状态 |
| `imm_anim`（IMM_SCROLL 300 → 320, 100 ms 刚 ACK） | 目标 320、起点 300、位移 20、时长 100、缓动位置在 [300, 320] |
| `rehello`（不带时间的 HELLO） | KEY 计数清零、灯灭、时钟还在 |
| `end`（会话结束，等到 SEG 后 6.2 s） | 时钟按秒走（和墙钟差 ≤ 1.5 s）；SEG 接管过期后回到本地时钟画面 |

## 变异测试（2026-11-18）

在临时克隆里一次改一处、跑、还原（工作树不动）。“对拍”= `run.sh`（Swift 变异跑 Python + Swift，其余 `--python-only`）；
“单测”= 对应那层的单测：host-ref `unittest` / `swift run monita-cs80-tests` / `fw/link/test.sh`（含新加的 `test_dispatch.c` 时钟、掉线、沉浸模式里 HELLO 用例）。
43 个变异，对拍抓到 38 个，每个都至少被一层抓到。

| ID | 变异 | 对拍 | 单测 |
|---|---|---|---|
| P1 | Python HELLO 时区符号反 | ✓ `sim.hello.clk` | ✓ |
| P2 | Python HELLO 时间偏 1 小时 | ✓ | ✓ |
| P3 | Python HELLO 不发时间 | ✓ `clk_valid` 0 | — |
| P4 | Python 不管夏令时（总用标准时） | ✓ | ✓ |
| P5 | Python 夏令时按当前真实时刻判（改之前的写法） | ✓ | ✓ |
| P6 | Python IMM_SCROLL dur 固定 1 ms | ✓ `sim.imm_anim` | ✓ |
| P7 | Python LED period / on 对调 | ✓ | ✓ |
| S1 | Swift HELLO 时区符号反 | ✓ | ✓ |
| S2 | Swift HELLO 时间偏 1 小时 | ✓ | ✓ |
| S3 | Swift HELLO 不发时间 | ✓ | — |
| S4 | Swift 夏令时按当前真实时刻判 | ✓ | ✓ |
| S5 | Swift HELLO 用真实时间（不走注入的 clock） | ✓ | — |
| S6 | Swift IMM_SCROLL dur 固定 1 ms | ✓ | ✓ |
| F1 | 固件 clk_set 时区符号反 | ✓ | ✓ |
| F2 | clk_set 不管时区 | ✓ | ✓ |
| F3 | HELLO 不调 clk_set | ✓ | ✓ |
| F4 | HELLO 时区按无符号读（西半球错） | ✓ | ✓ |
| F5 | HELLO 时间高低半字对调 | ✓ | ✓ |
| F6 | 时钟不走 | ✓ `sim.end.tick` | ✓ |
| F7 | 时钟走了不重画 | ✓ `sim.end.seg_clock` | ✓ |
| F8 | 小时钟十位个位画反 | ✓ | ✓ |
| F9 | 小时钟冒号不亮 | ✓ | ✓ |
| F10 | 主机 SEG 接管不过期 | ✓ `sim.end.seg_clock` | — |
| F11 | 主机 SEG 接管不生效（时钟照画） | ✓ `sim.keys.seg_held` | — |
| F12 | SEG ACK 了但没写显存 | ✓ `sim.seg.fg` | — |
| F13 | HELLO 后不画时钟 | ✓ | ✓ |
| F14 | 固件 IMM_SCROLL dur 改成 1 ms | ✓ `sim.imm_anim`（顺带 crc300 / crc1600） | — |
| F15 | IMM_SCROLL 忽略 dur（直接跳） | ✓ `sim.imm_anim` | — |
| F16 | LED ACK 了但没生效 | ✓ `sim.led` | — |
| F17 | LED 闪烁 on / off 对调 | ✓ | ✓ |
| F18 | LED 闪烁从灭开始 | ✓ `sim.boot.led` 相位 | ✓ |
| F19 | LED duration 不生效 | ✗ | ✓ test_led |
| F20 | HELLO 后不熄灯 | ✓ `sim.hello.led` / `sim.rehello.led` | ✓ |
| F21 | 未连接心跳颜色错 | ✓ `sim.boot.led` | — |
| F22 | 未连接心跳占空比错 | ✓ | — |
| F23 | HELLO 不清 KEY 计数 | ✓ `sim.rehello.key_n` | ✓ |
| F24 | BACKLIGHT ACK 了但没生效 | ✓ `sim.backlight` | — |
| F25 | HELLO 不把开机页 Link 行改 OK | ✓ `sim.hello.boot_live` | — |
| F26 | 主机回来（没 HELLO）不恢复灯 | ✗ | ✓ test_dispatch |
| F27 | 主机走了不回蓝灯心跳 | ✗ | ✓ test_dispatch |
| F28 | 沉浸模式里来 HELLO 不退出 | ✗ | ✓ test_dispatch |
| F29 | clk_set 不排下一秒（立刻连跳） | ✓ | ✓ |
| F30 | 时钟过零点不回绕 | ✗ | ✓ test_dispatch |

对拍仍抓不到的（都由单测兜住）：

- F19 LED duration：脚本没发带 duration 的 LED（要等它到时，拖慢对拍）。
- F26 / F27 掉线 / 回来：要 15 s 没字节，对拍用真实时钟太慢；`test_dispatch.c` 用虚拟时间测。
- F28：两边脚本都是先 IMM_EXIT 再重新 HELLO，没有“沉浸模式里直接 HELLO”这一步。
- F30：注入的时刻都不在 23:59:59 附近，会话里不过零点。

模拟器本身也有盲区：帧事件立即发生（等帧 / 撕裂相关的时序看不到），段码屏 / 灯 / 背光只到“固件写了什么”，
不含 I2C / GPIO 驱动（`fg00.h` 的 `fg_flush`、`io/led_hw.h`）；`app.c` 里开机流程（`task_entry`）是模拟器照抄的，不是共用代码。
