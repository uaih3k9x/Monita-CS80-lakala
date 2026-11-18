# 上位机 ↔ R05 固件协议 v0

> 状态：**v0，已实现并实机跑通**（固件 `fw/app.c` + `fw/link/proto.h`，参考上位机 `tools/host-ref/`、`mac/`）。
> 协议与平台无关：能打开 USB 串口、能画一张 800×1280 的图，就能当上位机。以后改动会升 `proto_ver`。

设计原则：**简单、流式、设备侧不需要大缓冲**。设备 app 只有 128 KiB RAM，一帧 payload 最多 32 KiB，
所以大图拆成很多条 RECT 连续发，最后一条 PRESENT 提交。

## 传输层

- 设备侧：R05 app `ql_uart_open(4)`，即模组的 USB modem 口。
- 主机侧：Windows 上是设备管理器里的 **"Quectel USB Modem"**（COMx，实测一台机器上是 COM5）；
  macOS 上**没有串口设备**（这些接口是厂商类 0xFF，系统不绑驱动），直接用 IOKit 打开 USB **接口 7**
  （VID 0x2c7c PID 0x0901；bulk IN 0x8A / OUT 0x07，另有一个中断 IN 0x89 不用，不需要 DTR），见 `mac/Sources/monita-cs80/UsbLink.swift`；Linux 上一般是 `/dev/ttyUSB*` / `/dev/ttyACM*`。**不用设波特率**（USB 虚拟串口，随便填）。
- 主机是主动方：设备只回应答，不主动发东西（以后的按键事件除外）。
- USB 自带流控：主机尽管往里写，设备处理不过来时主机侧写会变慢，不丢数据（实测连续 16 MB 无丢失）。
  唯一见过的丢数据是**设备刚开机、USB 刚枚举**那一下，所以主机连上后先 HELLO、再推整帧。

## 帧格式

全部小端。

```
u8  0xA5
u8  0x5A
u8  type
u8  seq          主机自增 (回绕)；应答带回同一个 seq
u16 len          payload 字节数，≤ max_payload (HELLO_ACK 给出，v0 固件 = 32768)
u8  payload[len]
u32 crc          CRC-32/IEEE，覆盖 type, seq, len, payload（不含 A5 5A 和 crc 本身）
```

CRC-32/IEEE 就是 zlib / PNG / Ethernet 用的那个（Python `zlib.crc32`，`"123456789"` → `0xCBF43926`）。

坏帧（CRC 错、长度超限）：设备丢弃、计数（见 ACK 统计），从下一个字节重新找 `A5 5A`，**不应答**。
半截帧超过 0.5 s 没有后续字节也丢掉。

**应答规则只有一条：除了 RECT，每一条合法的主机帧都恰好有一条应答**（HELLO → HELLO_ACK，DIAG → DIAG_R，其余 → ACK）。
RECT 不应答，所以可以连续成批地写，不用等。

## 消息

| type | 方向 | 名称 | payload | 应答 |
|---|---|---|---|---|
| 0x01 | → | HELLO | 空，或 `u32 unix_time` `i16 tz_min`（当地时区相对 UTC 的分钟数） | HELLO_ACK |
| 0x10 | → | SEG | 72 字节段码屏显存原样 | ACK |
| 0x20 | → | RECT | `u16 x, y, w, h` + `w×h` 个 RGB565（行优先，小端） | 无 |
| 0x21 | → | PRESENT | `u16 frame_id` | ACK（extra = 提交耗时 µs） |
| 0x22 | → | COPY | `u16 sx, sy, w, h, dx, dy`（fw v2，`caps` bit2） | 无 |
| 0x30 | → | BACKLIGHT | `u8 on`（0 灭，非 0 亮） | ACK |
| 0x40 | → | FBCRC | `u16 x, y, w, h` | ACK（extra = 这块像素的 CRC-32） |
| 0x50 | → | DIAG | `u8 op` + 参数（诊断用，不稳定，见 `fw/link/diag.h`，op 表见下文 "DIAG"） | DIAG_R (0xD0) |
| 0x7F | → | PING | 空 | ACK |
| 0x81 | ← | HELLO_ACK | 见下 | |
| 0xFE | ← | ACK | 见下 | |

### HELLO / HELLO_ACK

HELLO 会把设备侧的统计计数清零。带了时间的话，设备用它走段码屏上的小时钟。

```
HELLO_ACK (16 B):
u16 proto_ver     0
u16 fw_ver
u16 width         800
u16 height        1280
u8  pixfmt        1 = RGB565 小端 (R 在高 5 位)
u8  seg_bytes     72
u16 caps          bit0 FBCRC；bit1 DOUBLEBUF（双缓冲，见 PRESENT）；bit2 COPY（fw v2）；bit3 TRIBUF（三缓冲，fw v6）
u16 max_payload   32768
u16 0
```

### ACK（0xFE，40 B）

```
u8  for_type      应答的是哪种消息
u8  status        0 OK，1 参数不对，3 不支持
u16 0
u32 rx_bytes      HELLO 以来收到的字节
u32 frames        合法帧数
u32 crc_err       CRC 错的帧
u32 len_err       长度超限的帧
u32 rects         成功写入的 RECT
u32 presents
u32 bad_param     被拒的 RECT (越界 / 长度不对)
u32 uptime        设备开机时间，单位 1.024 ms
u32 extra         依消息而定 (PRESENT: 耗时 µs；FBCRC: CRC)
```

主机可以拿 PRESENT 的 ACK 检查这一批有没有出错：`crc_err + len_err + bad_param` 比上一次多了，
就说明有 RECT 没写进去，下一次推整帧即可。

### RECT（0x20）

```
u16 x, y, w, h       矩形，必须完全在屏内 (x+w ≤ 800, y+h ≤ 1280)
u8  pixels[w*h*2]    RGB565 小端，行优先；payload 长度必须正好 8 + w*h*2
```

- 坐标、大小随意，不要求对齐。一个 RECT 的 payload ≤ max_payload：整行宽（800）的条带一次最多 20 行（32008 B），整帧 = 64 条。
- 上位机常见做法：32×16 tile 差分，把同一 tile 行里相邻的脏 tile 合并成一个矩形发（参考实现都这么做）。
- 设备收齐一帧、CRC 对了才写，坏帧不会把垃圾写进画面。

### COPY（0x22，fw v2）

把画布（双缓冲时 = 后台）里的矩形 `(sx, sy, w, h)` 搬到 `(dx, dy)`，memmove 语义，源和目标可以任意重叠（上下滚动都行）。
两个矩形都必须在屏内，payload 正好 12 字节，否则计 `bad_param`、不动像素。和 RECT 一样不应答、计入 `rects`。
同一批里 COPY 应该放在 RECT 前面：主机的脏块是对着"COPY 之后的画面"算的。
用途：歌词滚动时一帧只发 COPY + 新露出的条带，线上从 ~350 KB/帧降到 ~10–34 KB/帧。

### PRESENT（0x21）

一批 RECT 发完后发一条 PRESENT，**然后等它的 ACK 再发下一批**（这是唯一需要等的地方，天然限流）。

- **双缓冲**（`caps` bit1，v0 固件开机时能从内核堆拿到第二块 2 MB 就开）：RECT 写的是后台缓冲，PRESENT 之前**看不到**。
  PRESENT 时固件：clean 后台脏行 → 等帧事件（扫描到帧尾）→ 把扫描基址切到后台 → 再等一帧（旧前台确定不再被扫）
  → 把这批脏行抄回新的后台，两块重新一致。一批改动整体出现在同一次刷新里，不撕裂。
  代价：每次 PRESENT 约 30–55 ms（两次等帧，帧周期 26.6 ms）+ 回抄（整帧 ≈ 25–35 ms）。
- **fw v2**：PRESENT 翻页后立即应答，"再等一帧 + 回抄"推迟到空闲时或下一条写后台的消息之前（主机渲染/传输和等帧重叠）；
  DIAG `op=11, u8 mode` 切翻页时机：0 = 先等帧事件、1 = 立即写基址。
- **fw v4 起默认立即翻页**（mode 1）：扫描基址 `0x08807034` 在帧头锁存（2026-11-18 实机：帧中途写入，本帧整帧不变、下一帧整帧换），
  所以立即写也不撕裂，每批只等一次帧事件（上限 37.5 fps）。
- **fw v4 跨缓冲 COPY**：翻页后这一批的第一条 COPY 直接从前台读、写进后台，再补边距和其余待抄回行，
  搬运量减半（`fw/link/blit.h`）；语义不变。歌词区滚动实测（750×514，每帧上移 8 行 + 8 行新条带）：
  v2 ≈ 72–97 ms/帧 → v4 ≈ 55 ms/帧（18.2 fps，CPU 搬运，FBCRC 一致）。DMA 实测和 CPU 一样快（约 64 MB/s），所以只用 CPU 搬；DMA 研究代码（DIAG 26–30）已移除。
- **fw v6 三缓冲**（`caps` bit3）：开机再拿第三块 2 MB。条件是拿完后堆里还剩 ≥ 128 KB 的连续块；拿不到就退回双缓冲，原因见 DIAG 31。
  三块缓冲轮着用：一块正在扫描，一块已提交待锁存，一块正在画。PRESENT 把正在画的那块写进扫描基址，立即应答；
  下一批直接画第三块，**不用等旧前台过帧头**。下一批开头会把这块落后的行从最新提交的那块抄过来；第一条是 COPY 时走跨缓冲。
  只有上一次 PRESENT 还没被帧头锁存时，这一次 PRESENT 才会先等一帧，所以上限仍是 37.5 fps，不丢帧，也不覆盖待锁存的基址。
  状态机在 `fw/link/tribuf.h`，主机单测会对照单缓冲模型做随机逐像素比对，并检查不撕裂。
- **单缓冲**（拿不到第二块时退回）：RECT 直接写正在扫描的缓冲，PRESENT 只做 cache clean；主机不发 PRESENT 的话，
  固件在 50 ms 没有新数据后也会自己 clean。大面积更新会看到自上而下的刷屏。
- `frame_id` 只是给主机自己对账用，设备不解释。

### SEG（0x10）

72 字节显存原样（4 COM × 18 字节），段位映射见 [segmap.md](segmap.md) 和 `fw/fg00/segmap.h`，
`tools/host-ref/monita_link.py` 里有 7 段数字编码的参考实现。

- 主机发过 SEG 之后 **5 秒内**段码屏完全由主机控制；超过 5 秒没有新的 SEG，固件接回来自己画：
  主区 `------`，小时钟走 HELLO 给的时间（没给过时间就只有 `------`）。
- 一次 SEG 往返约 25 ms（GPIO 模拟总线），1 Hz 左右更新就够。

### DIAG（0x50，诊断用，不稳定）

回包 DIAG_R：`u8 op, u8 status, u16 n, n × u32`，**n ≤ 16**（载荷 ≤ 68 B）。

fw v3 之前回包缓冲只有 48 B：13 个字以上的回包（PEEK 16 字、EVT 15 轮、PHASE 5 轮）会写穿栈、让设备复位。
早先记的"读 `0x08807040` 起复位"和"W1C 0x40/0x80 复位"很可能都是这个缺陷，而不是寄存器本身的问题。fw v4 已修。

| op | 参数 → 回包 | 版本 |
|---|---|---|
| 0–10 | 堆 / 最大块 / 第二块 fb / 切扫描基址 / 读 LCDC / 填色 / 翻页压测 / 帧事件计时 / 相位 / 双缓冲开关（见 `fw/link/diag.h`） | v0+ |
| 11 | `u8 mode` 翻页时机 → mode（v4 起默认 1 = 立即） | v2+ |
| 20–25 | 零拷贝滚动实验：ARM / PEEK / POKE（白名单）/ LINE / RULER / SPLIT（`fw/link/zc_diag.h`） | v3+ |
| 26–30 | 已移除（DMA 研究结论：不比 CPU 快，见 [r05-notes.md](r05-notes.md)） | v4–v5 研究期 |
| 31 | → `n nbuf note s p d gver waits xcopies syncs full_syncs buf0 buf1 buf2 flip_wait_us`。note：0 = 三块都拿到，1 = 第三块 malloc 失败，2 = 拿到了但余量不够（已还），3 = 连第二块都没有 | v6 |
| 32 | `u8 n`（1–3；0 = 能用几块用几块）→ 生效块数，用来做三缓冲 / 双缓冲 / 单缓冲 A/B 对比 | v6 |

fw v6 起，会动缓冲的旧 DIAG（2/3/5/6/10、20/22/24/25 等）执行前，固件先退回"双缓冲视角"：等待锁存完成，扫描落在 FB_ADDR 或第二块上，两块内容一致。执行完再重建三缓冲状态。诊断之后主机照旧推一次整帧。

### FBCRC（0x40）

对**最新提交**的那块缓冲（已显示，或在下一个帧头显示；单缓冲时就是正在扫描的那块）里的矩形算 CRC-32（按行优先、RGB565 小端字节，和主机对同一块像素数据算 `zlib.crc32` 一致）。
用来自检推帧是否完整：整帧推完后 `FBCRC 0,0,800,1280` 应该等于整帧字节的 CRC。整屏一次约 42 ms。

## 实测（Windows，USB 2.0，参考上位机 Python）

| 项 | 结果 |
|---|---|
| 链路裸吞吐（2 MB 无效字节，设备只扫同步字） | ≈ 8–8.6 MB/s |
| 整帧（64 条 RECT + PRESENT，线上 2,049,162 B） | 单缓冲 ≈ 320 ms（6.4 MB/s）；双缓冲 ≈ 390 ms（含 PRESENT ≈ 77 ms） |
| 小块差分（100×100 方块移动，平均 12.4 个矩形） | 单缓冲 ≈ 5 ms / 次；双缓冲 ≈ 52 ms / 次（基本是等帧） |
| PING 往返 | ≈ 0.4 ms |
| 丢包 / CRC 错 | 0（连续 16 MB、数百帧）；整帧回读 CRC 一致 |

整帧是少数情况（切页、重连），日常是小块差分，所以 v0 不做压缩。
帧周期 26.6 ms（≈37.5 Hz）。双缓冲下每批占两个帧周期，动画上限约 18–20 fps；fw v6 三缓冲上限 37.5 fps，Mac 实测歌词滚动 41 ms/帧（≈ 24 fps）。

## 参考实现

- Python：`tools/host-ref/monita_link.py`（协议）+ `cs80.py`（命令行：hello / ping / pattern / png / bench / diffdemo / seg / teartest / scrollbench / tri / e0 / e1 / zc）。
  只要 pyserial；推 PNG 要 Pillow。`python3 -m unittest discover -s tools/host-ref`。
- Swift：`mac/Sources/MonitaCore/Link.swift`（纯逻辑，有测试）+ `mac/Sources/monita-cs80/UsbLink.swift`（macOS IOKit，接口 7）/ `SerialLink.swift`（termios 串口，Linux 等），
  渲染器 `monita-cs80 --device usb`（或 `--device /dev/ttyUSB0`）。

## 以后

- 按键事件（设备 → 主机）、SEG 的高层字段（播放进度由固件外推）、段码屏断开后独立时钟。
- 如果需要更快的整帧：简单 RLE 作为新的 RECT 编码（新 type，`caps` 新位）。
- ~~双缓冲 PRESENT 的第二次等帧~~：已确认基址帧头锁存，fw v4 起默认立即翻页。
