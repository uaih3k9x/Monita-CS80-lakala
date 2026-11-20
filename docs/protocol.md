# 上位机 ↔ R05 固件协议 v0

> 状态：**v0，已实现并实机跑通**（固件 `fw/app.c` + `fw/link/proto.h`，参考上位机 `tools/host-ref/`、`mac/`）。
> fw v8 加的 KEY / LED、fw v9 加的沉浸模式 IMM_*、fw v10 加的 SLEEP / 主机走了回开机画面、fw v11 加的 RECT_LZ4（压缩的 RECT）
> 都是向后兼容的增量（`caps` 新位，`proto_ver` 不变：旧上位机照旧能用新固件，新上位机看 `caps` 降级）。fw v11 的 RECT_LZ4 / DIAG 33 已上真机实测（数字见 README "跑基准"）；v9 / v10 的功能还没在真机上逐项验证。
> 协议与平台无关：能打开 USB 串口、能画一张 800×1280 的图，就能当上位机。以后改动会升 `proto_ver`。

设计原则：**简单、流式、设备侧不需要大缓冲**。设备 app 只有 128 KiB RAM，一帧 payload 最多 32 KiB，
所以大图拆成很多条 RECT 连续发，最后一条 PRESENT 提交。

## 传输层

- 设备侧：R05 app `ql_uart_open(4)`，即模组的 USB modem 口。
- 主机侧：Windows 上是设备管理器里的 **"Quectel USB Modem"**（COMx，实测一台机器上是 COM5）；
  macOS 上**没有串口设备**（这些接口是厂商类 0xFF，系统不绑驱动），直接用 IOKit 打开 USB **接口 7**
  （VID 0x2c7c PID 0x0901；bulk IN 0x8A / OUT 0x07，另有一个中断 IN 0x89 不用，不需要 DTR），见 `mac/Sources/monita-cs80/UsbLink.swift`；Linux 上一般是 `/dev/ttyUSB*` / `/dev/ttyACM*`。**不用设波特率**（USB 虚拟串口，随便填）。
- 主机是主动方：设备只回应答，不主动发东西。唯一的例外是 fw v8 的按键事件 KEY（0x90），只在收到过 HELLO 之后发。
- USB 自带流控：主机尽管往里写，设备处理不过来时主机侧写会变慢，不丢数据（实测连续 16 MB 无丢失）。
  唯一见过的丢数据是**设备刚开机、USB 刚枚举**那一下，所以主机连上后先 HELLO、再推整帧。
- **一次写的长度别是最大包长的倍数**（或者补 ZLP）：bulk OUT 最后一包是满包时没有短包收尾，模组会把这段数据压在 USB 里，
  等下一次写来了才一起交给固件（Quectel 的 Linux 驱动指南也要求对 2c7c 的设备补零长包）。等应答的那条要是正好在这样一次写的末尾，
  就会一直等不到（fw v11 + Mac 上位机开 LZ4 时的"PRESENT 没有应答"：一批 COPY / RECT_LZ4 / PRESENT 一次写出去，批长约 1/512 的概率是 512 的倍数）。
  Mac 上位机每次写之前长度是 64 的倍数就在末尾补一个 0x00（`MonitaCore/Link.swift usbPadded`；`--no-usb-pad` 关掉做对照），
  落在两帧之间，拆帧器找同步字时直接跳过、不计错。Python / Linux 上位机的 PRESENT 是单独一次 12 字节的写，天然是短包收尾。

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

坏帧（CRC 错、长度超限）：设备丢弃、计数（见 ACK 统计），**不应答**。设备是流式拆帧、不留缓冲，所以重新找 `A5 5A` 的位置是：
- 长度超限（`len > max_payload`）：读完 4 字节头（type、seq、len）就重新找，后面的字节照常扫同步字；
- CRC 错：按 `len` 把整帧（payload + 4 字节 CRC）读完才重新找。所以 `len` 字段坏了会把后面最多 `len + 4` 字节一起吞掉，
  夹在里面的好帧不计数、不应答。主机表现为等应答超时（一般是等 PRESENT 的 ACK），重发整帧即可。

半截帧超过 0.5 s 没有后续字节也丢掉。主机侧拆设备回包时可以更宽松（参考实现都是坏帧前进 1 字节重新找同步）。

**应答规则：除了 RECT / RECT_LZ4 / COPY / IMM_RECT，每一条合法的主机帧都恰好有一条应答**（HELLO → HELLO_ACK，DIAG → DIAG_R，其余 → ACK）。
RECT / RECT_LZ4 / COPY / IMM_RECT 不应答（被拒也不应答，只计 `bad_param`），所以可以连续成批地写，不用等；之后任何一条有应答的消息
（PRESENT、IMM_SCROLL、PING）的 ACK 都是同步点。沉浸模式里被拒的 RECT / RECT_LZ4 / COPY 同样不应答。
（没有 RECT_LZ4 的旧固件把 0x23 当不认识的 type，回 ACK `status` = 3；所以上位机只在 `caps` bit8 置位时发它。）

设备主动发的只有 KEY（0x90，`seq` = 0），可能夹在任何两条应答之间。主机拆帧时按 type 分开：等应答时把 KEY 交给按键处理，不要丢；
不认识 KEY 的旧上位机按 type + seq 过滤应答，会自然忽略它。

## 消息

| type | 方向 | 名称 | payload | 应答 |
|---|---|---|---|---|
| 0x01 | → | HELLO | 空，或 `u32 unix_time` `i16 tz_min`（当地时区相对 UTC 的分钟数） | HELLO_ACK |
| 0x10 | → | SEG | 72 字节段码屏显存原样 | ACK |
| 0x20 | → | RECT | `u16 x, y, w, h` + `w×h` 个 RGB565（行优先，小端） | 无 |
| 0x21 | → | PRESENT | `u16 frame_id` | ACK（extra = 提交耗时 µs） |
| 0x22 | → | COPY | `u16 sx, sy, w, h, dx, dy`（fw v2，`caps` bit2） | 无 |
| 0x23 | → | RECT_LZ4 | `u16 x, y, w, h, u32 raw_len` + LZ4 raw block（fw v11，`caps` bit8，见下） | 无 |
| 0x30 | → | BACKLIGHT | `u8 on`（0 灭，非 0 亮） | ACK |
| 0x31 | → | LED | `u8 mask, u8 mode, u16 period_ms, u16 on_ms, u16 duration_ms`（fw v8，`caps` bit5，见下） | ACK |
| 0x32 | → | SLEEP | 空（fw v10，`caps` bit7，见"主机走了 / 休眠"） | ACK |
| 0x40 | → | FBCRC | `u16 x, y, w, h` | ACK（extra = 这块像素的 CRC-32） |
| 0x50 | → | DIAG | `u8 op` + 参数（诊断用，不稳定，见 `fw/link/diag.h`，op 表见下文 "DIAG"） | DIAG_R (0xD0) |
| 0x60 | → | IMM_ENTER | `u32 y0`（可省 = 0）（fw v9，`caps` bit6，见"沉浸模式"） | ACK（extra = 卷轴行数 R） |
| 0x61 | → | IMM_EXIT | 空 | ACK（extra = 恢复后的 caps） |
| 0x62 | → | IMM_RECT | `u32 y, u16 x, w, h, u8 flags, u8 0` + `w×h` 个 RGB565 | 无 |
| 0x63 | → | IMM_SCROLL | `u32 y, u16 dur_ms` | ACK（extra = 正在扫描的窗口顶） |
| 0x7F | → | PING | 空 | ACK |
| 0x81 | ← | HELLO_ACK | 见下 | |
| 0x90 | ← | KEY | `u8 key, u8 ev, u16 n, u32 uptime`（fw v8，`caps` bit4，见下） | 主机不应答 |
| 0xFE | ← | ACK | 见下 | |

### HELLO / HELLO_ACK

HELLO 会把设备侧的统计计数清零。带了时间的话，设备用它走段码屏上的小时钟。

fw v7 起开机大屏是版权页（固件版本、构建日期、堆剩余、缓冲块数、段码屏初始化结果），Link 行黄色 `WAIT`；
开机后第一条 HELLO（之前没收到过 RECT / COPY / DIAG）会把这一行改成绿色 `OK`，只重画这一行，之后由主机推帧接管全屏。
主机侧不需要任何改动：照旧 HELLO 后推一次整帧。版权页画在当前前台缓冲上，其余缓冲第一次被用时整屏同步。
fw v10：主机走了（见"主机走了 / 休眠"）时重画一遍版权页（Link 行回到 `WAIT`），下一条 HELLO 照样改 `OK`。

```
HELLO_ACK (16 B):
u16 proto_ver     0
u16 fw_ver
u16 width         800
u16 height        1280
u8  pixfmt        1 = RGB565 小端 (R 在高 5 位)
u8  seg_bytes     72
u16 caps          bit0 FBCRC；bit1 DOUBLEBUF（双缓冲，见 PRESENT）；bit2 COPY（fw v2）；bit3 TRIBUF（三缓冲，fw v6）；
                  bit4 KEYS（按键上报，fw v8）；bit5 LED（状态灯，fw v8）；bit6 IMMERSIVE（沉浸模式，fw v9）；
                  bit7 SLEEP（SLEEP + 主机走了回开机画面，fw v10）；bit8 LZ4（RECT_LZ4，fw v11）
u16 max_payload   32768
u16 0
```

- `proto_ver` 不是 0 的设备，本文档描述的上位机不要接着用（参考实现：Python 警告，Mac 断开）。
- `caps` 是 HELLO 那一刻的快照（HELLO 会先退出沉浸模式），运行中会变：沉浸模式里没有多缓冲（bit1 / bit3 暂时不成立）；
  IMM_EXIT 重新拿第二、三块时可能拿不全，最新的 caps 在 IMM_EXIT 的 ACK `extra` 里；DIAG 32 也会改生效块数。bit0 / bit2 一直置位。

### ACK（0xFE，40 B）

```
u8  for_type      应答的是哪种消息
u8  status        0 OK，1 参数不对，3 不支持
u16 0
u32 rx_bytes      HELLO 以来收到的字节
u32 frames        合法帧数
u32 crc_err       CRC 错的帧
u32 len_err       长度超限的帧
u32 rects         成功写入的 RECT / RECT_LZ4 / COPY / IMM_RECT
u32 presents
u32 bad_param     被拒的 RECT / RECT_LZ4 / COPY / IMM_RECT (越界 / 长度不对 / 坏的 LZ4 块 / 沉浸模式里的 RECT、RECT_LZ4、COPY / 不带 bit0 写屏上的行)
u32 uptime        设备开机时间，单位 1.024 ms
u32 extra         依消息而定：PRESENT 提交耗时 µs；FBCRC 这块像素的 CRC；IMM_ENTER 卷轴行数 R；
                  IMM_EXIT 退出后的 caps；IMM_SCROLL 正在扫描的窗口顶（还没生效的跳转 / 动画开始前的值）；其余 0
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

### RECT_LZ4（0x23，fw v11，`caps` bit8）

和 RECT 一样的矩形，像素换成 LZ4 压缩。UI 画面大片纯色，整帧 2000 KB 压到 30–90 KB（下面"实测"），整帧（开机、唤醒、重连、切页、锁屏切换）
从受 USB 限制的 ≈ 320–390 ms 降下来。

```
u16 x, y, w, h       矩形, 规则同 RECT (完全在屏内, w、h ≥ 1)
u32 raw_len          解压后的字节数, 必须正好 = w*h*2, 且 ≤ max_payload - 8 (和同一块的 RECT 一样大)
u8  block[]          LZ4 raw block (LZ4 Block Format, 不是 frame 格式: 没有魔数 / 帧头 / 校验), 解出来是 w*h 个 RGB565 (行优先, 小端)
```

- 块格式是公开的 LZ4 Block Format（token + 字面量 + u16 小端 offset + 长度扩展；最后一个序列只有字面量）。
  liblz4 `LZ4_compress_default` / python `lz4.block.compress(data, store_size=False)` / Apple Compression `COMPRESSION_LZ4_RAW` 的输出都直接能用。
- 固件（`fw/link/lz4.h`，自己写的解码器）**先把整块解进 32 KiB 的暂存区**，所有边界都在写之前检查，
  再走 RECT 原来的路径落进帧缓冲。所以坏块不会把任何像素写进画面，计 `bad_param`、不应答（同 RECT）。坏块包括：
  解出来的长度 ≠ `raw_len`、`raw_len` ≠ `w*h*2` 或 > `max_payload - 8`、输入截断（token / 长度扩展 / 字面量 / offset 读出界，或以 match 结尾）、
  输出越界、match offset 为 0 或超出已解出的字节、块后面还有多余字节。编码端约定（最后 5 字节是字面量、最后 12 字节里不起 match）不强制。
- 不应答的理由同 RECT：整帧 64 条连着写，只在 PRESENT 等一次；出错从 PRESENT 的 ACK 看（`crc_err + len_err + bad_param` 变多了 → 推整帧）。
- 沉浸模式里同 RECT：计 `bad_param`。开机版权页同 RECT：第一条 RECT_LZ4 之后 HELLO 不再改 Link 行。
- **上位机规则**（三个参考实现一样）：
  - 只在 HELLO_ACK `caps` bit8 置位时发；矩形照 RECT 的规则切（payload ≤ max_payload，整行宽 20 行一条）；
  - 每个矩形压缩后**严格小于原始的 3/4** 才发 RECT_LZ4，否则照发 RECT（小块、噪声、照片压不动）；
  - 一批里有 RECT_LZ4、PRESENT 的 ACK status 0 而 `bad_param` 涨了：**这次连接里只发 RECT**——keepalive / 睡醒的 HELLO 不恢复，
    端口重新打开（拔插、设备重新枚举）或 HELLO_ACK 的 `fw_ver` 变了才再试。这样万一编码器 / 解码器真有不一致，"推整帧恢复"会走普通 RECT，
    也不会每次 HELLO 之后再坏一批。`crc_err` / `len_err` 是链路毛刺，照常推整帧恢复，不关 LZ4；沉浸模式里 PRESENT 回 status 1、RECT / RECT_LZ4
    一律计 `bad_param`，那是模式不对，也不关；这批一条 RECT_LZ4 都没有时 `bad_param` 涨了不归因到 LZ4。
  - 压缩库是可选依赖：Python（`tools/host-ref`、`linux/`）用 python `lz4`（`pip install lz4` / `python3-lz4`），没装就只发 RECT；
    Mac 用系统自带的 Compression 框架。三个上位机都可以 `--no-lz4` 关掉，用来 A/B 对比。

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

### KEY（0x90，设备 → 主机，fw v8）

前面板 4 个键，设备去抖后上报。**只在收到过 HELLO 之后发**；HELLO 把计数 `n` 清零；主机走了（fw v10：HELLO 之后 5 s 没有有效帧，
见"主机走了 / 休眠"；fw v8 / v9 是 15 s 没有任何字节）设备停止上报。fw v8 要等下一条 HELLO 才恢复；**fw v9 起只要又收到帧就恢复**
（主机睡醒、进程被挂起后接着发时不一定重发 HELLO），计数 `n` 接着走（不清零）。帧头 `seq` = 0，不对应任何主机帧，主机**不应答**。

```
u8  key       1 查询（左上，PWRKEY）  2 ≡（左下）  3 +（右上）  4 −（右下）
u8  ev        1 DOWN  2 UP  3 LONG（按住满 1 s，每次按下最多一次；之后松开照样有 UP）
u16 n         HELLO 以来第几个事件（回绕）；主机可以据此发现丢事件
u32 uptime    事件时刻，1.024 ms 单位，同 ACK.uptime
```

- 去抖 16 ms（实机：30 ms 会把很快的轻点滤掉；GPIO 键另有硬件去抖），固件主循环 ≈ 10 ms 采样一次。
- 查询键是模组的 PWRKEY：实机长按之后内核不再给"抬起"回调，固件在按下 4 s 后仍没抬起就自己补一个 UP（之后迟到的抬起被吞掉）。
  PWRKEY 是回调计数，不会因为采样慢丢掉轻点：fw v9 起保证"按下"至少被去抖确认一拍才放开（v8 在主循环被 PRESENT 拖到 ≥ 30 ms 一拍时会丢）。
  所以主机看到 `LONG` 之后的 `UP` 不一定是真的松手时刻；短按判定用"DOWN → UP 之间没有 LONG"。
- 没有自动重复（按住连加）；主机要的话可以拿 DOWN 到 UP 的时长自己做。
- 参考做法（`mac/`）：+ / − 在 DOWN 时调音量；≡、查询 在 UP 时判短按（切页 / 播放暂停）。

### LED（0x31，fw v8）

正面一颗 RGB 状态灯，只有亮 / 灭 / 闪烁（没有调光、呼吸）。

```
u8  mask          bit0 红  bit1 绿  bit2 蓝（可混 7 色），其余位必须为 0
u8  mode          0 灭  1 常亮  2 闪烁
u16 period_ms     闪烁周期，0 = 1000，最小 40
u16 on_ms         闪烁中亮的时长，0 = 周期的一半；必须 < period_ms
u16 duration_ms   0 = 一直保持；否则到时自动灭（mode 0 时忽略）
```

- payload 必须正好 8 字节、参数合法，否则 ACK `status` = 1，灯不变；固件没有灯（`caps` 没有 bit5）时回 3。
- 闪烁从"亮"开始；相位抖动等于固件主循环间隔（≈ 10 ms，PRESENT 等帧时最长 ≈ 55 ms），对状态灯无所谓。时间按 1.024 ms 计，长 2.4%。
- 设备自己的灯语：**未连接时蓝灯 2 s 亮 1 s 灭**（同原厂常态心跳）；HELLO 把灯灭掉，之后完全由主机控制；主机走了（见"主机走了 / 休眠"）回到蓝灯心跳，休眠时灭。
  所以主机每次重连（HELLO）都要把自己想要的灯重发一次。fw v9：主机走了又不经 HELLO 回来时，固件把灯恢复成主机走之前设的样子。
- 参考做法（`mac/`）：播放中绿灯常亮，暂停 / 没在放熄灭；退出时发 `mode = 0`。

### 主机走了 / 休眠（SLEEP 0x32，fw v10，`caps` bit7）

固件自己判断主机在不在，纯逻辑在 `fw/link/linkwd.h`（主机单测 `test_linkwd.c`），分发在 `fw/link/dispatch.h`（`test_dispatch.c` 用虚拟时间测）。

| 情况 | 设备做什么 |
|---|---|
| HELLO 之后 **5 s 没有有效帧**（CRC 对的整帧才算；乱码不算），或 **USB 拔掉**（内核 hotplug 回调报"拔掉"，只在 VBUS 真断了时有，不用等 5 s） | **主机走了**：大屏重画开机版权页（Link 行黄色 `WAIT`；在沉浸模式里先退出；主机关过背光也重新打开）、段码屏回本地时钟（`------` + 小时钟）、灯回蓝灯心跳、不再发 KEY |
| 收到 SLEEP（空载荷；带了载荷回 `status` = 1、什么都不做） | 先回 ACK，再**休眠**：关大屏背光（GPIO7）、段码屏 EN（GPIO10）拉低、灭灯；本地时钟只走时间不刷段码屏。大屏画面（帧缓冲）不动 |
| 没有主机（走了，或开机后一直没人连）满 **10 min** | 自动休眠（同上），避免拔了线一直亮着 |
| 睡着时收到**任何一条有效帧** | 醒：背光开、段码屏重新初始化（会闪一下 `888888`，约 0.4 s）后画本地画面、灯回心跳；HELLO 过的话照"主机回来了"恢复 KEY 和主机设的灯。然后照常处理这一帧 |
| 睡着时按任意键 | 醒来显示开机版权页 + 蓝灯心跳（主机不在，不发 KEY），10 min 后再睡 |

- 主机正常连着时最长 ≈ 2 s 一帧（参考做法：SEG ≤ 1.5 s、PING 2 s），5 s 不会误报。主机暂停发送超过 5 s 后再接着发时，
  屏上已经是开机画面了：**隔了 ≥ 3.5 s 才发下一条的话先重新 HELLO、再推整帧**（`mac/` 的 DeviceLink 这么做，`MonitaCore/HostPower.swift`）。
  不这么做也能用，只是第一批差分会画在开机画面上，直到下一次整帧。
- SLEEP 之后主机**一个字节都别再发**（PING 也会叫醒设备）。参考做法（`mac/`）：Mac `willSleep` / `screensDidSleep` 时发 SLEEP 并停掉所有循环，
  `didWake` / `screensDidWake` 时重新 HELLO + 推整帧。主机走了 / 休眠期间设备不主动发任何东西（KEY 也不发）。
- 时间都按 1.024 ms 计（同 LED），5 s 实际约 5.1 s，10 min 约 10.2 min。

### 沉浸模式（IMM_*，fw v9，`caps` bit6）

整屏只显示一条"长卷轴"里的一段，**滚动 = 改扫描基址**，像素一个都不搬（零拷贝）。用来做全屏歌词。
硬件前提（实机，见 [r05-notes.md](r05-notes.md) §4）：扫描基址在帧头锁存（写入立刻、下一帧整帧生效，不撕裂），
可以指到缓冲里任意一行；基址 + 整帧读出缓冲之外会花屏。没有窗口 / 叠加层，所以沉浸模式下没有固定的上下栏。
纯逻辑在 `fw/link/imm.h`（主机单测 `test_imm.c`：随机序列对照参考模型，逐帧检查屏上内容、基址边界、不撕裂）。

**内存**：IMM_ENTER 先把多缓冲收拢到原厂 fb（扫描它、内容是最新提交的那帧），把第二、三块（共 4 MB）还给内核堆，
再按堆里实际能拿到的最大块分配卷轴（最多 4096 行；拿完堆里还要剩 128 KB）。典型 R ≈ 2700 行（4.3 MB）；
不到 `1280 + 256` 行就不开（ACK `status` = 3，多缓冲原样恢复，主机留在普通页面）。原厂 fb 在沉浸模式里当"保留缓冲"。
IMM_EXIT（或任何一条 HELLO）把当前画面抄回原厂 fb 显示、还掉卷轴、重新拿第二、三块；拿不到就少几块，ACK 的 extra 是新的 caps。
不在沉浸模式时发 IMM_EXIT 什么都不做，照样回 `status` = 0，extra = 当前 caps。

**坐标**：主机用逻辑行号 `y`（u32）写卷轴、指定窗口。IMM_ENTER 的 `y0` 是起点（卷轴第 0 行 = 逻辑行 y0）。
屏幕显示逻辑行 `[Y, Y+1280)`，`Y` 是窗口顶。

| 消息 | 说明 |
|---|---|
| IMM_ENTER `y0` | 进入（或已在沉浸模式里 = **重新开始**：屏上留着最后一帧，卷轴作废、从 y0 重来。换歌、往回跳都用它）。进入后屏上先还是原来的画面，直到第一条 IMM_SCROLL |
| IMM_RECT | 把 `w×h` 像素写到逻辑行 `[y, y+h)` 的 `[x, x+w)`。只能写**不在屏上**的行；`flags` bit0 = 允许写屏上的行（主机自己负责，只用来改颜色之类，最多撕裂一帧）。不应答，成功计 `rects`，被拒计 `bad_param` |
| IMM_SCROLL `y, dur_ms` | 窗口滚到顶 = y。`dur_ms` = 0 下一帧直接跳；否则固件做 ease-out cubic 缓动，每帧（≤ 37.5 fps）写一次基址。第一次 SCROLL 一律直接跳（上屏）。带缓动（`dur_ms` > 0）时一次最多滚 32767 行，超过回 `status` = 1；`dur_ms` = 0 的直接跳没有这个限制（目标窗口在卷轴里就行） |

- **写在前、滚在后**：滚动要露出来的行必须在 IMM_SCROLL 之前写好。IMM_SCROLL 的 ACK 也是同步点（之前的 IMM_RECT 都处理完了）。
  动画进行中，窗口扫过的行都算"在屏上"；缓动的中间位置主机不知道，所以动画期间别写会被扫到的行。
- **能写的范围**：不在动画时，`[Y, Y+R)` 里的行都能写（窗口里的要带 bit0）；窗口以上的行（已经滚出去的）不保证（可能已经被换底丢掉），
  往回滚请用 IMM_ENTER 重新开始。越界的 RECT 被拒，计 `bad_param`；越界的 SCROLL 回 `status` = 1。
- **换底**（固件自动，主机无感）：卷轴是线性的，窗口走到末尾附近时，把 `[窗口顶, 写到过的最高行)` 搬回卷轴顶部再把基址指过去。
  搬的行不和屏上的重叠就直接搬、切一次基址；否则先把当前窗口抄进保留缓冲、切过去，卷轴整个不可见了再搬、再切回来。
  屏上内容自始至终不变，不可见、不撕裂。动画停稳 200 ms 后、前面剩的行不到一半时主动做；IMM_RECT / IMM_SCROLL 需要时也会就地做
  （耗时 1–2 帧 + 搬运，整屏 ≈ 30 ms）。逻辑坐标不变。
- 沉浸模式下 RECT / RECT_LZ4 / COPY 计 `bad_param`，PRESENT 回 `status` = 1，DIAG 回 `status` = 3；FBCRC 校验屏上那一段（坐标相对屏幕）；SEG / LED / BACKLIGHT / SLEEP / PING 照常。
- 时间按 1.024 ms 计（同 LED），`dur_ms` 实际长 2.4%。

参考做法（`mac/`，`--immersive` 开机就进；fw v9 时代的"长按查询键切换"已去掉，容易误触）：歌词按句排成一列"块"（头部歌名 + 每句一块 + 尾部留白），
当前句居中；离当前句越远颜色越淡（上下淡出）。切句时先写新露出来的块（隐藏写）和高亮换行的两块（可见写），
IMM_SCROLL 缓动 360 ms，停下后再补其余变了档的颜色。换歌 / 往回跳 / 一次跳太远 → IMM_ENTER 重新开始、整屏重写。

### DIAG（0x50，诊断用，不稳定）

回包 DIAG_R：`u8 op, u8 status, u16 n, n × u32`，**n ≤ 16**（载荷 ≤ 68 B）。

fw v3 及之前回包缓冲只有 48 B：13 个字以上的回包（PEEK 16 字、EVT 15 轮、PHASE 5 轮）会写穿栈、让设备复位。
早先记的"读 `0x08807040` 起复位"和"W1C 0x40/0x80 复位"很可能都是这个缺陷，而不是寄存器本身的问题。fw v4 已修（见 [r05-notes.md](r05-notes.md)）。

空 payload 的 DIAG 回 `op` = 0、`status` = 1。不认识的 op 回 `status` = 3。

| op | 参数 → 回包 | 版本 |
|---|---|---|
| 0–7, 9, 10 | 堆 / 最大块 / 第二块 fb / 切扫描基址 / 读 LCDC / 填色 / 翻页压测 / 帧事件计时 / 相位 / 双缓冲开关（见 `fw/link/diag.h`；没有 op 8） | v0+ |
| 11 | `u8 mode` 翻页时机 → mode（v4 起默认 1 = 立即） | v2+ |
| 20–25 | 零拷贝滚动实验：ARM / PEEK / POKE（白名单）/ LINE / RULER / SPLIT（`fw/link/zc_diag.h`） | v3+ |
| 26–30 | 已移除（DMA 研究结论：不比 CPU 快，见 [r05-notes.md](r05-notes.md)） | v4–v5 研究期 |
| 31 | → `n nbuf note s p d gver waits xcopies syncs full_syncs buf0 buf1 buf2 flip_wait_us`。note：0 = 三块都拿到，1 = 第三块 malloc 失败，2 = 拿到了但余量不够（已还），3 = 连第二块都没有 | v6 |
| 32 | `u8 n`（1–3；0 = 能用几块用几块）→ 生效块数，用来做三缓冲 / 双缓冲 / 单缓冲 A/B 对比 | v6 |
| 33 | `[u8 n]`（缺省 / 0 = 8）→ 16 个字：设备侧 LZ4 自测（下面 "DIAG 33"）；没有 RECT_LZ4 的固件回 3 | v11 |

#### DIAG 33：设备侧 LZ4 自测（fw v11，`caps` bit8 的固件才有）

固件里内嵌一条测试条带（`fw/link/lz4_testdata.h`，`tools/lz4bench/gen_testdata.py` 从 `docs/img/lyrics-playing.png` 生成：
800×20 RGB565 = 32000 B，liblz4 压成 1319 B，这帧 64 条里压缩后大小最接近平均的一条，×64 ≈ 解这一整帧），自己解 n 次并用 `osiUpTimeUS` 计时：

- A **解码，输入在 Flash**：测试块是 `.rodata`，实机 XIP 读 Flash，所以比线上略慢（线上块在 RAM 里的 payload 缓冲里）；
- B **解码，输入在 RAM**：先把 RECT_LZ4 payload（12 字节头 + 块）拷进 payload 缓冲（线上拆帧收进来的同一个地方），再从那里解，对照 A（A / B 每次轮换先后，两边前一步的 cache 状态一样）；
- C **暂存区 → 帧缓冲**：`lk_rect_apply`（RECT_LZ4 的第二段）写进**不在扫描的那块** + clean 这 32000 B；
- D **合计**：`lk_rect_lz4_apply`（线上同一个函数，输入在 RAM）+ clean。C / D 的条带按次数轮流落在 64 个不同的 y 上；
- 最后一次 CPU 整屏拷贝（扫描的那块 → 不在扫描的那块，`ldm/stm` + clean，同三缓冲整屏同步）作对照，顺手把那块恢复成和屏上一样。

执行前先收拢多缓冲（同旧 DIAG），屏上那块一个像素都不动；之后其余块下次用时整屏同步，**不用推整帧**。单缓冲时没有不在扫描的那块：只测 A / B，C / D / 拷贝全 0。
长循环里按 `wdt_due` 喂狗。沉浸模式里同其他 DIAG 回 3。

回包 16 个字：`n | flags << 16`（bit0 = 每次解码 / 落帧都成功且解出来的 CRC 对，bit1 = 有不在扫描的那块），`raw_len | comp_len << 16`，
A min avg max，B min avg max，C min avg max，D min avg max（us），整屏拷贝字节数，耗时 us。

上位机（打印同一份格式，`monita_link.format_lz4bench`；之前先停掉常驻的上位机，同一个设备同时只能一个进程开）：
`monita-cs80 --device usb --bench-device [N]`、`python3 tools/host-ref/cs80.py PORT --bench-device [N]`、`monita-cs80-linux --device usb --bench-device [N]`。
主机上的指令计数粗估见 [tools/lz4bench/QEMU.md](../tools/lz4bench/QEMU.md)。

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

整帧是少数情况（切页、重连），日常是小块差分，所以 v0 不做压缩；fw v11 加了 RECT_LZ4（见下）。

### RECT_LZ4 的基准（fw v11，主机上测；下面设备侧是事前估计）

> 真机实测（Mac `--bench` / `--bench-device 64`）：测试图整帧 381 ms → 80.5 ms；DIAG 33 折合整帧解码 13.8 ms + 落帧 12.4 ms ≈ 26 ms，
> CPU 整屏拷贝 2 MB ≈ 23 ms（≈ 89 MB/s，比下面按 64 MB/s 估的快）。详见 README "跑基准"。

`python3 tools/lz4bench/bench.py`：12 张真实渲染帧（`docs/img`、`linux/docs/img` 的 800×1280 截图），按上位机的切法（整行宽 20 行一条，32000 B 原始）压缩。

| 帧 | 整帧一块压 | 64 条带（liblz4，线上） | 64 条带（Apple Compression） | 倍数（liblz4 条带） |
|---|---|---|---|---|
| docs/img/lock.png | 26 KB | 29 KB | 37 KB | 68× |
| docs/img/clock.png | 46 KB | 54 KB | 66 KB | 37× |
| docs/img/lyrics-playing.png | 73 KB | 83 KB | 98 KB | 24× |
| docs/img/immersive.png | 87 KB | 94 KB | 111 KB | 21× |
| linux/docs/img/idle.png | 38 KB | 45 KB | 58 KB | 44× |
| linux/docs/img/lyrics-paused.png | 79 KB | 89 KB | 106 KB | 22× |
| 12 张平均 | | 65 KB | ≈ 80 KB | ≈ 31× |

- 照片不好压：用一张照片壁纸渲染的锁屏页，64 条里 63 条刚过 3/4 门槛，线上 ≈ 1250 KB（RECT 的 60%），所以锁屏那一下只快 ~40%；
  解锁回歌词页（UI 画面）才是上表的量级。docs/img/lock.png 是拿不到壁纸时的 CDE 锁屏框。
- 主机压缩一整帧：liblz4 0.2–0.8 ms、Apple 0.2–0.7 ms（M 系列 Mac）；64 条都过了 3/4 门槛。
- 线上 65 KB / 帧，按实测链路 6.4 MB/s 约 **10 ms**（RECT 整帧 2049 KB ≈ 320 ms）。
- 解出来 99% 是 match（每帧约 1.2 万个序列、字面量约 18 KB）；offset 1 / 2（单色）占 match 字节 16–71%，其余多是 offset 1600（上一行）。
  固件按字写的两条快路径（offset 1 / 2、offset 是 4 的倍数）覆盖 96% 的 match 字节。
- 固件解码器在主机上（同一份 `fw/link/lz4.h`，-O2）解一整帧 0.05–0.3 ms，解 + 拷进帧缓冲 0.07–0.3 ms。
- **A5 估计**（UIS8910DM 的 Cortex-A5，主频按 400–500 MHz 估；周期数是拍脑袋的数量级）：逐字节 ~5 周期 / 字节、按字 ~6 周期 / 4 字节、~40 周期 / 序列
  → 解码 8–10 ms / 帧（全逐字节的话 21–27 ms）。暂存区 → 帧缓冲的 2 MB 拷贝受 PSRAM 写带宽限制（实测 CPU / DMA 都是 ~64 MB/s）约 31 ms，
  直接解进帧缓冲也躲不开这 2 MB 写。传输约 10 ms，和解码部分交错（解码时 USB 照样往内核缓冲里收）。
  **合计估计整帧 40–50 ms（原来 320–390 ms），没上机，等真机验证**（真机用 DIAG 33 量，见上面 "DIAG 33"）。
  QEMU 指令计数（[tools/lz4bench/QEMU.md](../tools/lz4bench/QEMU.md)，同一份解码器、同一套编译选项）：解一条 32000 B 条带 78 k 条指令，
  按 CPI 1.0–1.5、400–500 MHz 换算整帧 10–19 ms，上面的 8–10 ms 偏乐观。最大的未知数：暂存区（bss，在 RAM 窗口里，物理上和帧缓冲同一块 PSRAM）
  如果 D-cache 兜不住，解码的输出也要写回 PSRAM，设备侧会再多 ~30 ms，按 40–80 ms 看。对比方法：Mac 上 `monita-cs80 --device usb --bench` 和加 `--no-lz4` 各跑一次（看"整帧"那行）；
  有串口的系统上也可以 `cs80.py PORT bench 10 docs/img/clock.png docs/img/lyrics-playing.png` 和加 `--no-lz4`。
- 三缓冲的老代价会露出来：整帧提交之后，**下一批**开头要把新画的那块从刚提交的那块整屏抄回（2 MB，≈ 25–35 ms，`tribuf.h`）。
  以前被 320 ms 的传输盖住，现在整帧后的第一帧差分会多这一下。
- 和 liblz4 1.9.4 差分模糊（`bench.py --fuzz 200000`，真实条带的变异块）：结论一致 99.9%，内容从不相异；
  不一致的全是"块里有 offset 0 的 match"——规范说是坏块，liblz4 的快路径没查、照解，固件拒。
帧周期 26.6 ms（≈37.5 Hz）。双缓冲下每批占两个帧周期，动画上限约 18–20 fps；fw v6 三缓冲上限 37.5 fps，Mac 实测歌词滚动 41 ms/帧（≈ 24 fps）。

## 参考实现

- Python：`tools/host-ref/monita_link.py`（协议，覆盖到 fw v11：KEY、LED、沉浸模式 IMM_*、SLEEP、RECT_LZ4）+ `cs80.py`（命令行：hello / ping / pattern / png / bench / diffdemo / seg / backlight / sleep / teartest / scrollbench / tri / e0 / e1 / zc）。
  串口优先用 pyserial，没装时 POSIX 上用 termios；推 PNG 要 Pillow。`python3 -m unittest discover -s tools/host-ref`。
- Swift：`mac/Sources/MonitaCore/Link.swift`（纯逻辑，有测试）+ `mac/Sources/monita-cs80/UsbLink.swift`（macOS IOKit，接口 7）/ `SerialLink.swift`（termios 串口，Linux 等），
  渲染器 `monita-cs80 --device usb`（或 `--device /dev/ttyUSB0`）。

## 不用设备测上位机：固件模拟器

`tools/fwsim/`：把固件的协议层（`fw/link/dispatch.h` 和它用到的 proto / blit / tribuf / imm / keys / led / linkwd）
和段码屏本地时钟（`fw/fg00/segclock.h` + `fgbuf.h`）原样编成主机程序，
硬件换成假的（3 块帧缓冲、卷轴、按键注入；帧事件立即发生），开一个伪终端当 USB 串口。

- `sh tools/fwsim/run.sh`：Python 参考实现和 Mac 渲染器（debug 构建）各跑一遍同一套脚本（HELLO、整帧 / COPY / FBCRC、RECT_LZ4、SEG、LED、
  按键、未知 type、DIAG、坏帧、沉浸模式、停发 5 s 回开机画面、SLEEP / 醒来 ...），Linux 上位机（`linux/`）跑其中 HELLO + RECT_LZ4 那一段；
  五种模拟器配置（三 / 单 / 双缓冲 + 16 KiB max_payload / 卷轴不够 / 没有 RECT_LZ4 的旧固件 `--no-lz4`），
  结果和本文档逐项对比，两个上位机之间也要一致；还对拍固件的副作用（设备时钟 / 段码屏显示 / 灯状态机 / 滚动动画 / KEY 计数），
  HELLO 的时间用固定时刻 + 时区注入。细节和变异测试结果见 `tools/fwsim/README.md`。
- 测自己的上位机：`tools/fwsim/build/fwsim` 第一行打印 `PTY /dev/ttysNNN`，把它当串口打开即可；stdin 发 `tap 3`（按一下 + 键）、
  `unplug`（USB 拔掉）、`stat`（屏上 CRC、是不是开机页、睡没睡、缓冲块数、段码屏显存、时钟、灯、滚动等，一行 JSON）。用的是 POSIX 伪终端（macOS / Linux；Windows 上可以在 WSL 里跑）。

## 以后

- SEG 的高层字段（播放进度由固件外推）。按键事件和状态灯已在 fw v8 加上（KEY / LED）。
- 沉浸模式的可见写（改颜色）可以"追着扫描线写"做到完全不撕裂：帧事件后按 20.3 µs/行算出扫描线位置，等它扫过矩形再写。需要实机标定相位，暂未做。
- ~~更快的整帧：新的 RECT 编码~~：fw v11 RECT_LZ4。还能做的：整行宽的条带校验过后直接解进帧缓冲（省掉暂存区那一遍，估计省 ~5 ms，要真机数据再说）。
- ~~双缓冲 PRESENT 的第二次等帧~~：已确认基址帧头锁存，fw v4 起默认立即翻页。
