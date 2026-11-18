# R05 原厂基带上写原生 app：笔记

对象：拉卡拉 CS80 收银客显屏，通信模组 Quectel **EC600U-CN**（UIS8910DM，Cortex-A5，内部 NOR 8 MiB 映射在 `0x60000000`）。
基带：原厂 R05（`EC600UCNLBR05A01M08_OCPU_BETA1215`）。**本项目不降级基带**，只替换 app 槽。

标注：**【证实】** 反汇编、字节或实机直接看到；**【推测】** 推断，未上机验证。
地址都是 NOR 映射地址（文件偏移 = 地址 − `0x60000000`）。Thumb 函数指针 bit0 置 1。

> 这里只记录事实（地址、格式、引脚、时序）。原厂固件、整片备份、导出表以外的任何二进制都不在本仓库里。
> 动手前请先自己做一份整片 8 MiB 备份——它是唯一能完整回到出厂状态的东西。

---

## 1. 导入机制：core_stub 与名字哈希【证实】

app 不直接链接内核函数，而是在一张"桩表"里列出要用的导入，loader 加载时按哈希查表、把真实地址填进去。

每个桩 8 字节（Thumb）：

```
df f8 00 f0      ldr.w pc, [pc, #0]
xx xx xx xx      4 字节导入哈希 → 加载时被 loader 改写成真实 Thumb 地址
```

**哈希 = SHA-256(函数名 ASCII，不含 \0) 的前 4 字节。** R05 存的是反序：

```python
H_R05(name) = int.from_bytes(sha256(name.encode()).digest()[:4], "big")
# 汇编里写 .word H_R05(name)，落盘就是 sha256[:4] 的反序字节
```

- 例：`ql_pin_set_func` → sha256 前 4 字节 `14 07 64 5f` → `H_R05 = 0x1407645f`，桩里字节为 `5f 64 07 14`。
- 老的 R03（QuecPython 基带）用的是同一个哈希、相反字节序。
- 验证：R03 已知的 49 个 名字↔哈希 全中；R05 原厂 app 的 199 个桩在导出表里 199/199 全能查到。

`fw/sdk/gen_stubs.py` 就是按这个规则把 `*.imports` 生成桩表汇编的。

## 2. R05 导出表【证实】

- 位置 `0x60090710 .. 0x600920a0`，8 字节一条 `(u32 哈希, u32 Thumb 地址)`，按哈希升序（loader 二分查找）。
- 共 **818 条**。用 flash 里的字符串 + 公开 SDK 头文件里的名字做字典撞库，解出 280 个名字（`ql_*` 222 个，另有 lwip/mbedtls/osi/libc）。
- 整表在 [`fw/sdk/r05_exports.csv`](../fw/sdk/r05_exports.csv)（`hash,name`，只有哈希和名字、不含原厂地址；没解出的名字留空）。导入清单里写 `名字 = 0xHASH` 可以引用没解出名字的导出。

后来撞出、CSV 里还没有的几个（LCD/USB 相关）：

| 哈希 | 名字 | 地址 |
|---|---|---|
| 030f8f9b | ql_mipi_lcd_write_cmd | 0x60032735 |
| 381ee29f | ql_lcd_set_brightness | 0x60031ea1 |
| 15095c0d | ql_usb_get_enum_mode | 0x600430e9 |
| 131804aa | osiThreadCurrent | 0x60125dbf |

（`ql_lcd_*` 是 SPI LCD 那一套，原厂大屏没用。另有一个撞出来的 `ql_lcd_rx_status_mode` 指向不相干的代码区，大概率是碰撞，别用。）

够用的 API：线程/队列/信号量/定时器（`ql_rtos_*`、`ql_delay_us`）、GPIO（`ql_pin_set_func`、`ql_gpio_*`）、MIPI 大屏（`ql_mipi_lcd_init`、`ql_mipi_lcd_write_screen`）、USB 串口（`ql_uart_*`）、音频、文件、RTC 都在导出表里。

## 3. 2PPA 容器与 loader 规则【证实】

app 槽从 **`0x60270000`** 开始（R03 是 `0x60260000`，R05 的 AP 更大，把 app 往后顶了 64 KiB）。

头 0x80 字节：

```
@0x00 magic 0x41505032 "2PPA"      @0x04 total_len
@0x08 crc32                         @0x0c version (原厂 0x00010000)
@0x10 enter (Thumb)                 @0x14 exit (Thumb)
@0x18, @0x1c = 0
@0x20..0x80  6 个装载描述符槽，每个 16 字节 (kind, fileoff, size, addr)
   kind1 拷贝到 RAM (.data)   kind2 桩表拷到 RAM 并解析   kind3 RAM 清零 (.bss)
   kind4 XIP flash 段，fileoff == addr − 0x60270000（外加一条 size=0 的末尾标记）
```

loader（AP `0x60090420`，调用方 `0x60092844`）依次检查：

1. 4 字节对齐、magic 对、`0x80 < total_len`、`base + total_len <= 0x60370000`。
2. **版本**：高 16 位等于内核版本高 16 位，低 16 位不大于内核的。照抄 `0x00010000`。
3. enter 非 0；enter/exit/@0x18/@0x1c 非 0 时必须落在 `[base, base+total)` 或 `[0x80fe0000, 0x81000000)`。
4. **签名门**：读 efuse（块 0xb 的三个位，三取二）判断是否开了安全启动；开了才对 `total_len + 0x260` 验签（签名尾以 `RDASEdB2` 开头，原厂 app 末尾确实带着）。同一台机器在 R03 下能跑不带签名的第三方 app，同样的门在 R03 内核里也有，所以**推测这台没开安全启动，只校验 CRC**。【推测，第一次上机即可验证：`fw/diag/probe` 不出声就先怀疑这里】
5. **CRC**：等价于"把 @8 清零后对整文件做 zlib crc32"。
6. 描述符：`kind==0` 或 `size==0` 的槽跳过（不是终止符）。kind1/2/3 要求 `addr > 0x80fdffff && addr+size <= 0x81000000`；kind1/2/4 要求 `fileoff+size <= total`。
7. **kind2 桩表里只要有一个哈希查不到，整个 app 不加载。**
8. 装完同步 cache，**尾调用 `enter(NULL)`**，跑在系统启动线程里。

`fw/sdk/pack.py` 生成容器，`fw/sdk/validate.py` 逐条复查上面的规则（再加上：不许出现 VFP/NEON 指令）。

**入口约定**：原厂 enter 只做"全局构造 → 看门狗配置 → 引脚默认表 → 建任务 → 立即返回"。enter 里阻塞或死循环会卡死启动线程（AT 口、USB 都起不来）。所以 **enter 只建任务就返回，所有事放进任务里**。

**任务参数**：`ql_rtos_task_create(&t, stack, prio, name, fn, arg, event_count)` 是 **7 个参数**，第 7 个不能漏；优先级有上限（内核检查），原厂用 12 和 15，我们用 12。

**RAM**：app 窗口 `0x80fe0000..0x81000000`（128 KiB），放桩表、.data、.bss。内核堆是 `[0x80791280, 0x80de0000)`；`0x80de0000..0x80fe0000` 这 2 MiB 不在堆里，原厂拿它当大屏帧缓冲。

### 刷写位置

- 写到 NOR **`0x60270000`**，擦除粒度 64 KiB。小 app 只需擦写第一个扇区，loader 只看 `total_len`，后面的旧残体不影响。
- **上限 0xa09a0（657824 B，原厂 app 的大小）**，紧接着就是 AP 资源区，没有空隙，超了会砸资源。`validate.py` 会检查。
- 下载态 USB ID `0525:a4a7`，macOS 上不出串口设备，刷写目前在 Windows 上做。
- 回滚：整片备份原样写回。

## 4. 800×1280 MIPI 大屏

上电顺序（原厂 `MipiLcdTaskInit`）【证实】，`fw/lcd/lcd.h` 按这个顺序实现：

```
脚 118 → func0 = GPIO7，输出低（背光灭）
ql_mipi_lcd_init(&cfg)          cfg = { u32 width = 800, u32 height = 1280 }（8 字节）
无参导出 0xac3ae871              名字没解出，形态像 LCD 防休眠锁，原厂紧接着调一次
清帧缓冲 0x80de0000（0x1f4000 B）
ql_mipi_lcd_write_screen(0x80de0000)   整个生命周期只调这一次
画首帧 → sleep 200 ms → GPIO7 = 1（背光亮）
```

- `ql_mipi_lcd_init`：开 LCD 电源轨（VIBR 2800 mV、LCD 1800 mV），面板时序、lane 数、像素格式都写死在内核的面板描述符里，app 传不进去也不用传。cfg 比面板大会被静默忽略。
- `ql_mipi_lcd_write_screen(fb)`：**不拷贝**，只把 LCDC 的层基址指向 fb，并写行跨。之后控制器以 **video 模式持续扫描** 这块内存，更新画面就是直接改 fb。
- 像素：每像素 2 字节，行跨 0x640（800×2），共 2,048,000 B。**RGB565 小端、R 在高位**【证实，实机：`0xF800` 显示为红，`lcd_test` 红→绿→蓝顺序正确】。
- cache：原厂没做 clean。我们写完做一遍 DCCMVAC（32 B 一行）+ `dsb`。帧缓冲在 PSRAM、走 write-back cache，不 clean 的话像素会卡在 cache 里。整屏 clean ≈ 1.3 ms【实机】。
- `ql_mipi_lcd_write_screen` 可以**反复调**：每次只是重写层基址（≈ 5–18 µs，返回 0），换一块内存就是翻页【证实，实机】。
- **背光是 GPIO，不是 PWM**：原厂只有开/关两档，没导入 PWM。要调亮度只能软件 PWM（GPIO7）。

### LCD 控制器寄存器（实机读到的）【证实】

| 地址 | 值 / 行为 |
|---|---|
| `0x08807008` | 事件位，写 1 清零（W1C）。**0x04、0x08、0x20 每帧各置位一次**，周期 **26.6 ms（≈ 37.5 Hz）**，抖动 < 2 µs；0x20 比 0x04/0x08 早 ≈ 378 µs（约 19 行，推测 0x20 = 有效区扫完、0x04 = vsync），0x04 和 0x08 同时。0x40 一直置位 |
| `0x08807024` | `0x500`（= 1280） |
| `0x0880702c` | `0x143` |
| `0x08807030` | `0x0200020a` |
| `0x08807034` | 层扫描基址（= write_screen 写进去的 fb 地址） |
| `0x08807038` | `0x06400001` = 行跨 0x640 << 16 \| 使能 |

坑（**已更正**）：早先记的"读 `0x08807040` 起复位""W1C 0x40/0x80 之后复位"，根因很可能是固件自己的缺陷，不是寄存器。
`reply()` 的栈缓冲只有 48 B 载荷，而那两次 DIAG 回包都有 16 个字（PEEK 16 字、EVT 1 + 15 轮）；
13 个字以上就会写穿保存的 lr，设备复位【证实是缺陷（反汇编 v3 的 `reply`，E0 的 15 字回包同样复位过）；"那两次也是它"属推断】。fw v4 已修。
R03 内核写 0x40、R05 内核读改写 0x50、QDisplay 在 R03 上读 0x40/0x44，所以这些地址本身大概率能访问。
不过 0x40 以后的寄存器还没单独测过，固件白名单仍只读 0x00–0x3c 和 0x50（`fw/link/zc_logic.h`）；事件寄存器仍只 W1C 0x2c 内的位。

其余静态结论（R05 `ql_mipi_lcd_init` / `write_screen` / 视频配置 `0x6010ce30` 写了哪些寄存器，面板描述符 `0x601b3094` 里的时序：vtotal 1310 行、每行 ≈ 20.3 µs）
是维护者本地的研究记录，未收录进本仓库；要用到的结论都已写在本节。

| 实机结论（2026-11-18，fw v3） | |
|---|---|
| `0x08807024` | 恒为 0x500，是高度，不是扫描行号 |
| `0x08807034` | **帧头锁存**：帧中途写入，本帧整帧不变，下一帧整帧换（锁存点就在 0x04 事件那一刻）→ 立即翻页不撕裂；做不了光栅分屏 |
| `0x0880703c` / `0x08807040` | 0x3c 写基址、0x40 写 `0x06400001` → 整屏换成那块缓冲。是另一个整屏源，不是叠加层 |
| `0x08807044` / `0x08807048` | 写窗口值没有效果，不是层窗口 |
| 整屏偏移 | 基址指到缓冲内任意一行都行，精确到行（实测 top + 100 行生效）；基址 + 1280 行超出缓冲时，末尾读到缓冲外的内存，那几行花屏 → fw v9 沉浸模式（`fw/link/imm.h`）用它做零拷贝整屏滚动，任何时候都保证基址 + 整帧落在卷轴里 |

### 双缓冲：可行，v0 固件已经在用【证实，实机】

- 内核堆还很宽裕：`ql_dev_memory_size_query` 报 total 6.6 MB / avail 4.36 MB / 最大块 4.35 MB；app 里二分 `malloc` 实测最大可分配 0x426000（4.35 MB）。
  开机 `malloc(2048000 + 4096)` 拿第二块 fb（落在堆里，4 KiB 对齐）之后还剩最大块 2.3 MB。
  （以前"拿不出第二块 2 MB"的结论来自 R03 QuecPython 时代的内存账，不适用于 R05。）
- 翻页 = 对另一块调一次 `ql_mipi_lcd_write_screen`（或直接写 `0x08807034`）。来回切上百次设备稳定、寄存器回读正确、FBCRC 回读一致（原 fb 64 KiB 对齐，第二块 4 KiB 对齐）。第二块在屏上是否显示正常、有没有对齐要求，**待肉眼确认**。
- v0 固件的 PRESENT：clean 后台脏行 → 等 0x04 置位 → 切基址 → 再等一次 0x04（保证旧前台不再被扫）→ 脏行抄回新后台（ldm/stm，整帧 ≈ 25–35 ms）。
- **已确认帧头锁存**（见上表），fw v4 起默认立即翻页（DIAG 11 可切回）。每批只等一次帧事件。
- fw v4 跨缓冲 COPY（`fw/link/blit.h`）：歌词区滚动 55 ms/帧（v2 时 72–97 ms）。
- fw v6 三缓冲（`fw/link/tribuf.h`）：开机再拿第三块 2 MB（拿完堆里要剩 ≥ 128 KB 连续块，否则退回双缓冲），PRESENT 后下一批不用等旧前台过帧头。歌词区滚动 41 ms/帧（≈ 24 fps）【实机】。

### AXI DMA `0x090c0000`（fw v4/v5 研究过，结论：不比 CPU 快；研究代码已从固件移除）

R05 内核的用法（静态分析）：
- 12 个硬件通道，通道 n 的寄存器在 `0x090c0040 + n×0x40`；内核只管 0–5 号，通过内核自己的通道表和分配函数分出去（UART 也从这里拿）。
- 内核开机初始化时设了全局 `+0x20 |= 0x3f`，注册了 IRQ 0x3c 和一个 ISR（W1C 状态后调通道表项里登记的回调）。
- 通道寄存器：+0x00 ctrl（bit0 启动，bit4 中断），+0x04 请求选择（0x1f1f = 内存），+0x08 源，+0x0c 目的，+0x10 字节数，+0x14，+0x18 状态。
- **状态 bit0 是"忙"不是"完成"**：内核的停止函数等它变 0，UART 驱动把它当"还在忙"轮询。
  fw v4 把 bit0 当成"完成"，传输刚启动就被自己掐断，四个候选全部"数据不对"。fw v5 改了判据，并加了探测命令 DIAG 28–30。
- R03 Helios 用同一块 DMA 把 2 MB 从 PSRAM 搬到 PSRAM（ctrl 0x4217，+0x14 = 4，轮询 +0x18 bit0），说明 PSRAM→PSRAM 可行、用的是 CPU 地址。
- **v5 实机（2026-11-18）**：
  - ctrl 0x05 / 0x15（32 位宽度；0x15 多开了中断位）配任意 +0x14 都能正确搬。
  - 0x07 / 0x17（宽度位 = 3）在测试用的 4/8 字节偏移下数据错，推测要求 8 字节对齐。
  - **+0x14 是突发/分段大小**：= 4 时约 8.5 MB/s，0x10 时约 27 MB/s，0x40 时约 54 MB/s，≥ 0x100 时饱和在约 60–66 MB/s。固定开销约 10 µs（开中断时多约 10 µs 回调延迟）。
  - CPU ldm/stm 拷贝大约也是这个量级（整帧 2 MB 约 25–35 ms），扫描本身还要占 PSRAM 约 77 MB/s。**DMA 没有带宽优势，固件默认用 CPU 搬。**
  - v5 的启用逻辑有缺陷：自测用 +0x14 = 4 通过后，开机 2 MB 的 DMA 拷贝（约 8 MB/s）超时，DMA 被永久关掉，`fails` = 1。实际效果就是 CPU 搬。
- **v6 之后**：DMA 驱动和 DIAG 26–30 已从固件和参考上位机删除，COPY / 翻页抄回一律 CPU ldm/stm（三缓冲下歌词滚动 41 ms/帧）。

### 撕裂与"追光"写入（单缓冲时的退路）

只有一块扫描缓冲时，写入和扫描线之间没有同步：写入窗口跨过扫描线的那一次刷新，就是上半旧、下半新。
有了上面的帧事件位，单缓冲也能"追着写"：每个条带等扫描线刚扫过再写、在它下一圈回来前写完。双缓冲可用时不需要。

## 5. USB 串口：和 Mac 通信的通道【证实】

`ql_uart_open(port)` 的端口号（内核 `ql_uart_open` 里的表）：

| port | 设备 | 说明 |
|---|---|---|
| 0..2 | uart1..3 | 硬件 UART |
| 3 | usbat | USB AT 口 |
| **4** | **usbmodem** | **原厂和上位机通信用的就是它** |
| 5 | usbnmea | USB NMEA |

- 原厂用法：`ql_uart_open(4)`（USB 口不需要设波特率）→ `ql_uart_register_cb(4, cb)`，回调里循环 `ql_uart_read` 读完；发送用 `ql_uart_write(4, …)`。
- `ql_uart_read(4, buf, len)` 返回实际读到的字节数（原厂回调里循环读到 0）。回调 `cb(ind, port, size)` 在内核线程里跑；
  我们的固件回调里只 release 一个信号量，读和解析都在自己的任务里做，实测没问题。
- USB 复合设备 VID:PID `2c7c:0901`。按功能顺序推算 modem 口是 **MI_07**；Windows 上显示为 **"Quectel USB Modem"**（实测一台机器上是 COM5）；macOS 上应该是一个 `cu.usbmodem*`。【Windows 证实，macOS 待测】
- app 打开 port 4 之后，这个口上就不再有 AT/PPP（AT 口是另一个 COM，刷机工具靠它进下载模式，不受影响）。
- 吞吐【实机，Windows】：裸链路 ≈ 8–8.6 MB/s；USB 有流控，设备处理慢时主机写入会被压住，连续 16 MB 不丢。
  唯一见过的丢数据是设备刚开机那一下（HELLO 之后发的大块只到了一部分），所以上位机连上后应先 HELLO 再推整帧，并看 ACK 里的统计。
- 看门狗：照原厂，enter 里 `ql_dev_cfg_wdt(1)`，任务循环里每秒 `ql_dev_feed_wdt()`。

## 6. 段码屏 FG00

不是数码管，是一块段码 LCD，由一颗 I²C 风格的控制器驱动（地址字节 `0x7E`，命令前缀 `0x80`，数据前缀 `0xC0`），用 GPIO 软件模拟总线，不读 ACK。芯片型号没认出来，`FG00` 是屏模组代号。

### 引脚（用 `ql_*` 的硬件 GPIO 编号）【证实】

| 信号 | 模组脚 | 复用 | ql GPIO |
|---|---|---|---|
| SDA | 56 | func0 | 15 |
| SCK | 57 | func0 | 14 |
| RST | 52 | **func2** | 30（低有效） |
| EN | **95** | **func5** | 10 |

> QuecPython 的公开 GPIO 编号是另一套（同一根线编号不同），写原生 C 时只认"模组脚 + 复用号 + ql GPIO 号"三元组。

### 坑：EN 在 pin95，不在 pin4；高电平才亮【证实，实机】

- 原厂驱动自己的 `hal_init` 只把 GPIO10 复用到 **脚 4**，但真正连到屏的是 **脚 95（func5）**——这一步藏在原厂 enter 里那张 53 项的引脚默认表里（第 18 项）。我们的 app 不跑那张表，所以要**自己补 `ql_pin_set_func(95, 5)`**，否则屏完全不亮。
- **GPIO10 高 = 亮，低 = 灭。** 原厂开机路径是 init 完刷一帧全亮，GPIO10 一直保持高，**并不调用 onoff**。
- 原厂的 `onoff(1)` 会发 `0x3D` 并把 GPIO10 **拉低**，实测效果是熄屏——名字和效果相反。早期我们在 init 后调它，结果"闪一下就灭"。
- 所以 `fw/fg00/fg00.h` 里：`fg_init()` 结束时屏已亮，`fg_on()/fg_off()` 只控 GPIO10，开机不调。

### 总线时序【证实】

```
START  dir(SDA)=OUT; SCK=1; 10µs; SDA=1; 10µs; SDA=0; 10µs
STOP   dir(SDA)=OUT; SCK=1; 10µs; SDA=0; 10µs; SDA=1; 10µs
BYTE   SCK=0 1µs; 8 位 MSB 先发 {SDA=bit; SCK=1 1µs; SCK=0 1µs}; 第 9 个时钟 SDA=1 不读 ACK
CMD(c) BYTE(0x80); BYTE(c)
```

### 初始化（一整帧）【证实】

```
清显存; 配引脚; SCK=1 SDA=1; 10 ms; START STOP
RST=0; 150 ms; RST=1; 100 ms
START 7E  {80 xx}: 2F B1 40 34  (此处睡 50 ms)
                   68 A0 94 B2 4C 10 40 50 00 10 C0 88 88 88 88 61 E3
      00 3D  STOP
显存全 0xFF → 刷新 → 50 ms                    (开机 888888，所有段全亮)
```

### 显存与刷新【证实】

- 显存 72 字节 = 4 个 COM × 18 字节（每个 COM 144 个 SEG）。
- 段 (com, seg) 对应 `fb[18*com + seg/8]` 的第 `7 - seg%8` 位。
- 刷新：`START 7E 80 40 80 50 80 10 80 00 80 C0 STOP`，然后 72 次 `START 7E C0 fb[i] STOP`。

7 段数字、各图标的映射见 [`segmap.md`](segmap.md)。

## 7. 按键与状态灯【证实，实机 2026-11-18】

静态分析原厂按键 / 状态灯模块，再用两个只读诊断 app（keydiag、ledprobe，不在本仓库）上机确认。

### 前面板 4 键

| 键号 | 位置 | 接法 | 有效电平 | 读法 |
|---|---|---|---|---|
| 1 | 左上 查询 | **PWRKEY**（模组开机键，内核 keypad 驱动，不是 GPIO） | — | `ql_pwrkey_press/release_cb_register` 回调 |
| 2 | 左下 ≡ | 脚 14 func4 → **GPIO21** | 低 | 读电平 |
| 3 | 右上 + | 脚 99 func5 → **GPIO8** | 低 | 读电平 |
| 4 | 右下 − | 脚 13 func4 → **GPIO23** | 低 | 读电平 |

- 原厂 R05 用边沿中断（开硬件去抖）+ 100 ms 轮询标志，按一下后睡 900 ms 当去抖，GPIO 键没有长按。我们改成主循环 ≈ 10 ms 读电平 + 软件去抖。
- 原厂开机引脚表把 GPIO8/21/23 的另一个可选脚（9/124/121）设成 func1，保证同一 GPIO 只从一个脚进来；我们不跑整张表，`fw/io/keys_hw.h` 照抄这三项。
- 原厂最终配 PULL_NONE（板上有上拉）；开内部上拉（`PULL_UP` = 2）实机也正常。
- **去抖**：30 ms 会把很快的轻点滤掉，用 16 ms（GPIO 键另有硬件去抖）。
- **PWRKEY**：照原厂 `ql_pwrkey_shutdown_time_set(3000)` + `ql_pwrkey_callback_register(cb)`，注册了这个回调后长按 3 s 不关机（实机证实）。
  但**长按之后收不到抬起回调**，固件靠超时（按下 4 s 未抬起就当松开）自恢复，见 `fw/link/keys.h`。
- 没有触摸芯片、I²C 或 ADC 按键。原厂里另有一套外接收款小键盘（TM1721 类，GPIO9/11/12/18），和前面板无关。
- QDisplay 的 R03 资料用的是 QuecPython 编号（脚 99/13/14 = GPIO47/46/44），和 R05 的 `ql` 编号不同，只有模组脚号能对上。

### 正面 RGB 状态灯（数码屏下方）

| 颜色 | GPIO | 模组脚 / func |
|---|---|---|
| 红 | **GPIO29** | 脚 53 func2 |
| 绿 | **GPIO19** | 脚 33 func0 |
| 蓝 | **GPIO17** | 脚 12 func4 |

- 一颗 RGB 灯，纯 GPIO 推挽，**高电平亮**；没有 PWM（原厂导入里没有 `ql_pwm_*`），原厂只有亮 / 灭和按秒闪烁。
- 原厂"红"组同时拉 GPIO13（脚 54）和 GPIO29：**GPIO13 上没有灯**，不要碰。原厂 Wi-Fi 模块代码把 GPIO29 当外接 Wi-Fi 的唤醒脚，是别的机型的代码，CS80 上 GPIO29 就是红灯。
- 软件 PWM 呼吸实测效果差，固件只做亮 / 灭 / 闪烁（`fw/link/led.h`）。
- 原厂灯语：开机红绿交替；常态 **蓝灯 2 s 亮 1 s 灭**（我们的"未连接"沿用它）；收款成功绿灯常亮约 2–3 s；OTA / 下载策略文件绿灯 1 Hz 闪。

## 8. 其它备查

- 喇叭功放使能：`ql_pin_set_func(15, 4)` + ql GPIO22（原厂音频初始化路径）。
- 原厂另有一路硬件 UART1（port 0，921600 8N1）连着板上某个外设，和上位机通信无关。

## 9. 原厂地址备查（从代码注释移来）

代码里不再写原厂函数/数据地址，静态分析时用到的记在这里（NOR 映射地址；Thumb 函数实际调用时 bit0 置 1）。

| 地址 | 是什么 | 相关代码 |
|---|---|---|
| `0x6009053c` | AP loader 里算 2PPA CRC 的位置（loader 入口见 §3） | `fw/sdk/validate.py` |
| `0x60032795` | 未解名导出 `r05_lcd_pm_lock`（hash `ac3ae871`）的实现 | `fw/sdk/r05_api.h` |
| `0x8074df94` | 上面这个 PM 锁操作的对象 | 同上 |
| `*0x8074e270` / `*0x8074e274` | `ql_rtos_task_create` 检查的 prio / stack 上限 | `fw/sdk/r05_api.h` |
| `0x600fb242` | R03 内核 LCD 初始化里写 `0x40 = 0x06400000`（行跨，不带使能）的位置 | `fw/link/zc_diag.h` |
| `0x60308df0` | 原厂段码屏渲染函数（state[0x12..0x2f] 是 30 个图标 flag） | `fw/fg00/segmap.h`、`fw/diag/segprobe.c` |
| `0x60308c0c` | 原厂段码 set_bit | `fw/diag/segprobe.c` |
| `0x603080c0` | 原厂音量图标 setter | `fw/fg00/segmap.h` |
| `0x603086d0` / `0x60308708` / `0x603088ac` | FG00 i2c_start / write_byte / i2c_stop | `fw/fg00/fg00.h` |
| `0x60308898` / `0x60308a84` / `0x60308ba0` | FG00 cmd / hal_init / flush | 同上 |
| `0x60308cec` / `0x60308c84` | FG00 fg00_hw_init / onoff | 同上 |
| `0x6028bcf0` | 原厂按键表（3 项 × 16 B：id, pin, func, gpio…） | `fw/io/keys_hw.h` |
| `0x6028b8dc` / `0x6028ac5c` / `0x6028b9d4` | 原厂按键 init / 中断回调 / 100 ms 轮询任务 | 同上 |
| `0x6028aef8` | 原厂 PWRKEY 注册（shutdown_time 3000 + 4 个回调） | 同上 |
| `0x60279664` | 原厂开机引脚默认表（套表函数 `0x602712b8`） | 同上（9/124/121 设 func1） |
| `0x6002f694` / `0x6002f294` | 内核 `ql_int_register` / `ql_gpio_set_pull`（0 无、1 下拉、其它上拉） | `fw/sdk/r05_api.h` |
| `0x601a9dc0` / `0x601a9f68` | 内核 keypad 驱动 PWRKEY 按下 / 长按到时（有 callback 就不关机） | `fw/link/keys.h` |
| `0x60282674` | 原厂厂测：开机读 GPIO8 / GPIO23 电平 | — |
| `0x6028aa90` | 原厂状态灯 init（脚 33/12/54/53 → GPIO19/17/13/29 输出 0） | `fw/io/led_hw.h` |
| `0x6028ab18` / `ab20` / `ab28` / `ab30` / `ab46` | 原厂灯：绿开 / 绿关 / 蓝开 / 红开（拉 GPIO13+29）/ 全灭 | 同上 |
| `0x6027a264` / `0x6027a1c4` / `0x6027a468` | 原厂 `led_task` / 灯语状态分发 / `DevLedSendEvent` | `fw/link/led.h`（灯语参考） |
| `0x60282538` / `0x60282578` | 原厂厂测：三色轮流 / 只翻蓝灯（"Blue flash Start!"） | — |
| `0x6028d5e0` / `0x6028d48c` | 原厂外接 Wi-Fi 初始化（脚 53 → GPIO29 当唤醒脚）/ 唤醒 | — |

---

## 参考与致谢

- [StevenLang233/lakala-display-re](https://github.com/StevenLang233/lakala-display-re)（QDisplay，CC BY-NC-SA）：R03 侧的硬件与引脚资料（按键、背光、段码屏候选引脚）。本仓库**只引用事实**，没有使用其任何代码。
- [xbsheng/lakala-cs80](https://github.com/xbsheng/lakala-cs80)：Mac 端旧链路 host。本仓库只通过其 HTTP 接口互操作，没有包含其代码。
- 其余均为维护者对自有设备原厂固件的静态分析与实机测试。
