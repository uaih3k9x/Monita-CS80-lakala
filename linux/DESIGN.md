# Linux 上位机设计稿

状态：已实现（分支 `linux-host`，基于 `fw-v10-hostlost`）。评审改了哪些见文末"评审修订"，实现时和本稿的出入见最后"实现记录"。

目标是让 Linux 桌面用户也能用这块屏。功能对齐 Mac 版的直连部分：

- 读正在播放的曲目（MPRIS）；
- 取歌词；
- 画歌词页和时钟页；
- 推帧；
- 处理按键；
- 控制灯和段码屏；
- 跟着系统休眠。

视觉上和 Mac 版保持一致，不追求像素一致。

## 1. 原则

- **复用，不重写。** 协议层直接用 `tools/host-ref/monita_link.py`，它覆盖到 fw v10。歌词逻辑直接用 `tools/lyrics/lyrics.py`。Linux 版只补 Mac 版有、Python 还没有的东西：
  - 会话层：keepalive、PRESENT ACK 基线检查、SLEEP 和唤醒；
  - 段码屏帧；
  - 按键映射；
  - 渲染器；
  - Linux 系统接口。
- **系统相关的部分都放在可替换接口后面。** D-Bus（MPRIS、logind）、PipeWire/PulseAudio 和 sysfs 各定义一个小接口。单测用假实现。本机是 Mac，这几块只能靠假实现测，真机行为写进"待 Linux 实机验证"。
- **能在 Mac 上真跑的都要真跑**：渲染出 PNG、协议、fwsim 对拍、pyusb。真设备冒烟要等维护者确认可以临时停 Mac 上位机。
- **TDD**：每个模块先写测试。框架用标准库 `unittest`，和仓库现有测试一致。
- **单线程主循环。** 只有三类后台线程：logind 信号监听线程（用**自己的** D-Bus 连接，jeepney 的阻塞连接不能跨线程共用）；`lyrics.Lyrics.payload()` 每首新歌临时起的 `_resolve` 线程；取封面的线程（`Lyrics.cover()` 是同步 HTTP，超时 8 s，不能在主循环里调）。Mac 版每个功能一个轮询线程、靠锁串行化；Linux 版改成一个按时间表调度的循环，`Link` 不用加锁，也不会有收 KEY 和写帧抢串口的问题。
- **主循环不许被网络卡住。** 固件 ~5.1 s 收不到有效帧就回开机页。主循环里只允许三种阻塞：等设备应答、D-Bus 调用（超时 0.5 s）、音量子进程（超时 2 s）。网络请求一律在后台线程。
- **和显示服务器无关。** 渲染在内存里用 Pillow 画，不连 X11 / Wayland，也不截屏；只需要会话 D-Bus（MPRIS）和系统 D-Bus（logind）。所以 GNOME / KDE / sway / i3，甚至没有图形界面的机器都一样跑。

## 2. 目录结构

```
linux/
  DESIGN.md                 本文
  README.md                 安装、运行、排错（中文）
  monita-cs80-linux         启动脚本：python3 -m monita_linux "$@"
  monita_linux/
    __init__.py
    __main__.py             命令行入口（参数见 §5）
    _paths.py               把 tools/host-ref、tools/lyrics 加进 sys.path
    transport.py            open_device("usb" | "usb:N" | 串口路径)，UsbPort
    session.py              Session：Link 之上的 keepalive / 推帧判错 / 休眠 / 灯的代数
    segframe.py             段码屏 72 字节帧（照 MonitaCore/SegFrame.swift）
    controls.py             KeyMapper、led_for（照 MonitaCore/Controls.swift）
    schedule.py             帧键、下一次唤醒时刻、页面切换（照 Schedule.swift / ScreenMode.swift）
    render/
      fonts.py              找字体：参数 → fc-match → 候选列表；缺字体时报清楚的错
      canvas.py             Pillow 画布：Motif 斜面、凹陷框、文字锚点、折行（照 Canvas.swift / LyricsCore）
      palette.py            CDE Default.dp 色板
      lyrics_page.py        歌词页 + Front Panel
      clock_page.py         时钟页
    sources/
      mpris.py              MprisSource：MPRIS → mediaremote 形状的 raw 字典；make_lyrics() 接到 lyrics.Lyrics(np=…)
    system/
      dbus.py               对 jeepney 的极薄封装（Bus 接口：call / get_property / 订阅信号）
      power.py              Logind：delay inhibitor + PrepareForSleep
      volume.py             Wpctl / Pactl 音量
      stats.py              CPU（/proc/stat）、电池（/sys/class/power_supply）
    app.py                  主循环：把上面的东西拼起来
  tests/
    test_transport.py  test_session.py  test_segframe.py  test_controls.py
    test_schedule.py   test_render.py   test_mpris.py     test_power.py
    test_volume.py     test_stats.py    test_fwsim.py     test_app.py
  packaging/
    monita-cs80.service     systemd --user 单元示例
    70-monita-cs80.rules    udev 规则示例
```

对已有文件只做一处改动，不影响 Mac 版行为：

1. `tools/lyrics/lyrics.py`：`Lyrics.__init__(self, np=None)`，`np` 为 None 时还是 `NowPlaying()`。外加一条单测。

`tools/fwsim/conformance.py` 不改，测试只 import 它的 `Sim`、`region_crc`、`seg_text` 等工具（理由见文末"评审修订" R1）。

## 3. 模块和接口

### 3.1 transport：设备怎么打开

`open_device(spec)` 返回一个端口对象，供 `monita_link.Link(port)` 使用。端口要有 `read(n)`、`write(b)`、`in_waiting`、`reset_input_buffer()`、`close()` 这几个成员。`spec` 有三种写法：

| `--device` | 含义 |
|---|---|
| `usb` | pyusb 打开 2c7c:0901 的接口 7 |
| `usb:N` | 用接口 N |
| `/dev/ttyUSB3` 等路径 | 走串口，交给 `monita_link.open_port`（有 pyserial 就用，没有就用 PosixPort） |

这三种写法和 Mac 版一致。Mac 版会遍历所有厂商类接口挨个发 HELLO 来探测，Linux 版不做探测（YAGNI）：接口不对就手动写 `usb:N`。

`open_device` 不是启动时调一次就完：设备没插、被拔、挂起后重新枚举，都表现为"打不开"或 IO 出错。Session 每次重连都重新调用 `open_device`（构造参数就是这个工厂），找不到设备时每 2 s 重试，**进程不退出**。`claim_interface` 报 EBUSY 时，日志里直接提示"接口被内核驱动或别的程序占用，试 `--device /dev/monita-cs80`"。

`UsbPort(dev, intf=7)` 的要点：

- 内核驱动（option / qcserial）已经绑定时，先 `detach_kernel_driver(intf)` 再 `claim_interface`，`close()` 时还给内核。
- `read(n)` 不管 n 是多少，一律按 16 KiB 读 bulk IN 0x8A，超时 50 ms。读到的数据先放进内部缓冲。`USBTimeoutError` 当作 `b''` 处理。
- `in_waiting` 返回内部缓冲的长度。缓冲为空时先做一次非阻塞读（超时 1 ms）。
- `write` 按 1 MiB 切块写 bulk OUT 0x07，超时 10 s，返回写入的总字节数。
- `reset_input_buffer()` 清空内部缓冲，并把端点里积压的数据读干净。
- `usb.core.USBError`（包括设备拔掉时的 ENODEV / EIO）不吞掉，原样抛给 Session 当断开处理；只有超时转成 `b''`。

测试：注入一个假的 usb 设备对象，至少覆盖以下几点：

- 不管调用方要多少字节，都按整块读；
- 超时返回空；
- 内核驱动已绑定时，先 detach 再 claim；
- 写入按块切分；
- 能被 `Link` 收发一条 HELLO（用假设备回放一条事先录好的 HELLO_ACK 字节流）。

### 3.2 session：Link 之上的会话语义

`monita_link.Link` 不处理下面这些，由 `Session` 补上，语义照 `MonitaCore/HostPower.swift` 和 `SerialLink.swift`：

```python
class Session:
    def __init__(self, open_port, clock=time.time, mono=time.monotonic, stale_after=3.5): ...
    up: bool                 # HELLO 成功且没在睡
    caps: int
    generation: int          # 每次 HELLO 成功加 1；灯、整帧都看它
    def connect_if_needed() -> bool   # 需要的话 HELLO，返回"刚(重)连上"（调用方推整帧）
    def push(fb, rects, frame_id, copies=()) -> bool  # False = 调用方下一帧推整帧
    def ping() / seg(fb72) / led(...) / poll_keys()
    def suspend()            # 发 SLEEP（超时 1 s），之后拒绝一切写
    def resume()             # 标记 forced，下一次 connect_if_needed 必定 HELLO
```

- **keepalive**：距离上一次发送 ≥ `stale_after` 秒时，下一次发送前先重新 HELLO，`generation` 加 1，调用方据此改推整帧（见下面"整帧以 generation 为准"）。`stale_after=None` 关掉 keepalive（相当于 Swift 的 `setStaleAfter(nil)`），只给单测用。
- **推帧判错**：PRESENT 的 ACK 满足 `status==0`，并且 `crc_err+len_err+bad_param` 没有比基线多，才算成功。每次 push 之后更新基线，HELLO 之后基线归零。等 ACK 超时或者 IO 出错，都算断开：`up=False`，下一轮重连。
- **休眠**：`suspend()` 之后 `can_send=False`。除了只读的 `poll_keys`，所有写操作直接返回、不碰端口，包括 ping、seg、led 和 keepalive 的 HELLO。
- **时间**：HELLO 的时间取 `clock()`。测试里由假时钟注入，tz 由 `monita_link.hello_payload` 按 `TZ` 计算。
- **HELLO_ACK 校验**：`monita_link.Link.hello` 对 proto_ver≠0 只发警告。Session 照 Mac 版收紧：proto≠0、尺寸不是 800×1280、pixfmt≠1，都当连接失败（记日志，2 s 后重试），不推帧。`max_payload` 取自 HELLO_ACK，传给 `full_rects` / `dirty_rects`。
- **caps 门控**：没有 bit5 LED 就不发 LED；没有 bit2 COPY 就不发 COPY；没有 bit7 SLEEP（v10 以前的固件）时 `suspend()` 只停发、不发 SLEEP。
- **哪些算断开**：只有等应答超时、IO 出错、HELLO_ACK 不合格算断开。SEG / LED 回 status≠0（例如 LED 回 3）只记日志，不断开。
- **断开就重开端口**：`up=False` 时关掉旧端口，下一轮重新 `open_device` + HELLO，而不是在旧端口上重发 HELLO（USB 重新枚举后旧句柄已经失效）。
- **整帧以 `generation` 为准**：keepalive 的重 HELLO 可能发生在 `seg()` / `ping()` 里，而不是 `push()` 里，单靠 `push` 返回 False 会漏掉。渲染侧记住上次推帧时的 `generation`，变了就推整帧；`push` 返回 False 只表示"这次推坏了"。
- **PRESENT 超时**：`Link.push` 写死等 30 s。Session 自己拼批次（`Link.send` 逐条写 COPY / RECT，最后 `call(PRESENT, timeout=10)`），和 Mac 版一致，免得设备掉线时主循环卡 30 s。
- **KEY 计数**：`n` 不连续只记日志；HELLO 之后 `n` 从 0 重新数，不算丢键。

测试：用一个假 Link 记录收到的调用，`mono` 用假时钟。至少覆盖：

- 隔 3.4 s 发送不会重新 HELLO，隔 3.6 s 会；
- 重连后 `push` 返回 False；
- ACK 计数增加时判失败；
- `suspend` 之后任何写都不落到端口；
- `resume` 之后第一次发送必定 HELLO，`generation` 加 1。
- 在 `seg()` 里触发的重 HELLO 也会让 `generation` 加 1；
- HELLO_ACK 尺寸不对时 `up` 保持 False，不发任何 RECT；
- caps 没有 LED / SLEEP 时，对应的帧不落到端口；
- IO 出错后下一次连接会重新调用端口工厂。

### 3.3 segframe：段码屏

照抄 `MonitaCore/SegFrame.swift` 的 SegBit 字节和掩码表。纯函数：

```python
seg_frame(state, now, battery=None) -> bytes(72)
# state: {"playing": bool, "paused": bool, "pos": s, "duration": s} 或 None
# battery: (percent, on_ac) 或 None
```

显示规则：

- 播放中：主区显示 mm:ss，满 1 小时显示 h:mm:ss；小时钟显示总时长；喇叭亮；声波按 `[2,4,5,3,5,4,2,3]` 每 250 ms 走一步。
- 暂停：主区按 1 Hz 闪烁，声波全灭。
- 没在播：主区显示 HH:MM，冒号以 500 ms 为半周期闪烁；小时钟显示 MM-DD。
- 电池：每 25% 一格，四舍五入，大于 0% 时至少亮 1 格。

测试：用 `tools/fwsim/conformance.py` 里现成的 `seg_render` / `seg_text` 把 72 字节解码回文字再断言，避免手写字节表。

### 3.4 controls：按键和灯

```python
class KeyMapper:            # feed(key, ev) -> 动作或 None
    # + / − 在 DOWN 时出 "vol+" / "vol-"
    # ≡ / 查询键 在 UP 时判短按（DOWN 之后、UP 之前出现过 LONG 就不算），出 "page" / "play_pause"
def led_for(playing: bool) -> (mask, mode, period, on)   # 播放中绿灯常亮，否则灭
```

动作的执行放在 app 层：

- `vol±` 调 `Volume.step(±6)`；`+` 顺带解除静音，`−` 不动静音。
- `play_pause` 调 MPRIS 的 `PlayPause()`，对象是**当前屏上显示的那个播放器**（MprisSource 选中的那个），不是随便哪个。已经有 D-Bus 连接，不必再依赖 playerctl。没有选中播放器时忽略。
- `page` 在歌词页和时钟页之间切换。

Linux 没有 agent 数据源，所以"有人在等"恒为 false，蓝灯闪烁那一档不做。

### 3.5 sources：正在播放和歌词

`MprisSource(bus, prefer=None)` 的 `get()` 返回 mediaremote 形状的字典，没有播放器时返回 None。字段映射：

| MPRIS | raw 字段 |
|---|---|
| `xesam:title` | `title` |
| `xesam:artist`（列表） | `artist`，用 `/` 连接 |
| `xesam:album` | `album` |
| `mpris:length`（µs） | `durationMicros`，缺失时不给 |
| `Position`（µs，每次 get 主动读） | `elapsedTimeMicros`，同时把 `timestampEpochMicros` 设为读取时刻 |
| `PlaybackStatus == "Playing"` | `playing` |
| `Rate` | `playbackRate`，缺失时为 1.0 |
| 总线名 `org.mpris.MediaPlayer2.<x>` | `bundleIdentifier` |
| `mpris:artUrl` | `artUrl`（封面补充，见下） |

选哪个播放器：

1. 每次 `get()` 用 `ListNames` 列出 `org.mpris.MediaPlayer2.*`，**去掉 `playerctld`**（它是转发代理，会让同一个播放器出现两次）。
2. `--player X` 按**前缀**匹配总线名后缀：浏览器和部分播放器会带实例号，比如 `chromium.instance12345`、`firefox.instance_1_23`、`vlc.instance777`。匹配到就只用它，匹配不到按下面规则选。
3. 否则选状态为 Playing 的；有多个时，留在上一次选中的那个（避免两个都在放时来回跳），否则按名字排序取第一个。
4. 都不在播放时，选最近一次处于 Playing 的那个；它已经退出了，就选任意一个 Paused 的；只剩 Stopped 的就返回 None。

只对选中的播放器做 `GetAll`，其余只读 `PlaybackStatus`。每次 D-Bus 调用超时 0.5 s；某个播放器卡住或报错就当它不存在，不影响别的，并且 30 s 内不再问它（否则每秒刷新都要在主循环里再卡 0.5 s，几个卡住的播放器叠起来能让固件以为主机走了）。

宽容处理残缺的元数据（浏览器、部分 Electron 应用常见）：

- 没有 `xesam:title`：返回 None（`snapshot()` 本来也会当 stopped）。
- 认歌用 `(播放器, mpris:trackid, 标题, 歌手)`：Chromium 的 trackid 是常量，规范里的 `NoTrack` 也是，只看 trackid 会把换歌当成同一首。
- `Position` 读不到、报 NotSupported，或者正在播放却连续 ≥ 2 s 都是 0：改用"这首歌处于 Playing 的累计时长"当进度（暂停的时间不算），并在 payload 上标 `estimated=True`（页面显示黄框"进度为估算"）。之后一旦读到非 0 的 Position（缓冲完、拖了进度）就改回真值。`Lyrics.payload()` 会写死 `estimated=False`，由 `make_lyrics()` 包一层覆盖回来。
- `PlaybackStatus` 是 Stopped 时返回 None，页面显示"未在播放"。

`make_lyrics()` 负责组装：`lyrics.Lyrics(np=MprisSource(...))`，`payload()` 原样复用。歌词搜索、打分匹配、缓存和 `cover(sid)` 都直接用 lyrics.py 里现成的。在进程内调用就够了，不起 HTTP 服务。

封面要注意两点：

- `payload()` 里的 `cover` 是给 HTTP 客户端的路径（`/api/lyrics/cover?id=…`），渲染器不能拿去用。渲染器看 `songId`，在后台线程调 `Lyrics.cover(sid)` 取字节、解码、缓存成 220×220 的图；没取到之前画文档图标。
- `snapshot()` 会丢掉 raw 里多余的字段，`artUrl` 传不过去。所以 `artUrl` 由 MprisSource 单独暴露（`source.art_url`）。网易云没给封面而 `artUrl` 是 `file://` 地址时，读本地文件作封面；`http(s)://` 的 artUrl 不取（YAGNI，也少一个外网请求）。

`lyrics.py` 在 import 时会计算几个 Mac 路径（`NETEASE_CACHE` 等）。在 Linux 上这些路径只是不存在，没有副作用。`_fallback()` 在 Linux 上永远返回 stopped，正好用作"没有播放器"的情况。

测试：用一个假 Bus，返回固定的属性字典，至少覆盖：

- 字段映射；
- artist 是列表时正确拼接；
- length 缺失；
- 多个播放器时的选择规则（含 playerctld 被排除、`--player` 前缀匹配实例号、两个都在播放时不来回跳）；
- Position 缺失时按 Playing 累计时长估算、暂停不计、`estimated=True`；读到真值后改回；trackid 不变但标题变了算换歌；
- 某个播放器的调用超时不影响选别的；
- 把输出喂给 `lyrics.snapshot()`，得到和 `mac/Samples/lyrics-playing.json` 同形状的字段。

### 3.6 歌词来源和许可

- **来源**：网易云音乐的非官方 Web 接口，`music.163.com` 下的 `/api/search/get`、`/api/song/lyric`、`/api/song/detail`，由 `tools/lyrics/lyrics.py` 调用。这些接口**没有公开文档，也没有明确许可，随时可能失效或被限流**。README 和 THIRD_PARTY.md 里要写明这一点。
- 歌词和封面的版权归原权利人。仓库只放调用代码，不提交任何歌词或封面内容。本地缓存放在 `~/.monita-cs80/lyrics/`（可以用 `MONITA_HOME` 改位置）。
- **取不到歌词时的降级**（`lyric.status` → 页面）：`loading` → "正在查找歌词…"；`nomatch` / `error` → "找不到歌词"；`pure` → "纯音乐，请欣赏"；`idle` → "未在播放"。除 `idle` 外，标题、歌手、进度条和段码屏照常显示。断网时 `_resolve` 失败，30 s 后自动重试（lyrics.py 现成逻辑），不需要额外处理。
- 备选的 LRCLIB（lrclib.net）这一版不接。原因有两个：它的使用条款还没有核实；多一个来源就多一套匹配逻辑，属于 YAGNI。接口已经预留好：`Lyrics` 的取词只依赖 `_find_id` / `_load_lyric`，以后要换来源，在 linux 包里写一个子类就行。

### 3.7 render：渲染

用 Pillow 画 800×1280 RGB，输出 `bytes`（RGB565 小端，R 在高位），转换用 `monita_link.rgb888_to_rgb565`。布局数字全部照"Mac 版渲染要点"抄，来源是 `LyricsPage.swift` 和 `ClockPage.swift`：

- **歌词页**：
  - 整屏 desk 底色；窗口框 (12,12,776,1012)，标题栏高 44；
  - 菜单栏 File / Edit / View / Options / Help；
  - 封面 220×220，没有封面时画文档图标；
  - 右侧信息区：标题 38（放不下改 30，最多 2 行）、Artist 26、专辑 20；
  - 状态行；
  - 歌词区 (win.minX+20, top+254, 720, 520)：当前句 50 bold，翻译 28，上面最多 3 句、下面最多 4 句，放不下就缩字号再加省略号；
  - 提示页：未在播放 / 纯音乐 / 正在查找 / 找不到歌词；
  - 进度条、底部消息栏；
  - Front Panel：(12,1040,776,228)。
- **时钟页**：凹陷框里 HH:mm 用 210 bold 等宽；下面是星期和 "NOV 19, 2026" 格式的日期；底栏显示 "≡  Lyrics"。
- **Front Panel 的数据格**：Mac 版的 Claude / Waiting / DSH 三格是作者私人数据，Linux 版换成 **CPU / BAT / Player / Load**，拿不到数据时显示 "–"。Lock 按钮固定显示 "Lock"，不做防睡眠断言。
- **字体**：Linux 默认用 Liberation Sans / Liberation Mono（分别和 Helvetica / Courier New 等宽）加 Noto Sans CJK SC。Pillow 没有字体回落，所以按字符选字体：CJK 字符用 CJK 字体，其余用拉丁字体。折行规则照搬 LyricsCore：CJK 逐字断，拉丁按空格断。字体路径可以用 `--font-sans/--font-mono/--font-cjk` 指定，不指定时用 `fc-match` 查找。代码里不写死任何 Mac 字体路径。
- **开机页和待机**：开机版权页由固件画。主机离开 5 s 后，固件也会自己重画开机页。主机侧不画开机页，只负责重连后先 HELLO 再推整帧。主机的"待机"就是歌词页的"未在播放"提示。锁屏页不做（见 §9）。
- **滚动动画**：第一版不做 450 ms 的 COPY 滚动，换句时直接按差分推。`Session.push` 支持 `copies` 参数，以后要加 `ScrollAnim` 时，主循环不用改。

测试，用 `mac/Samples/*.json` 作输入，机器上缺字体就 skip：

- 产物尺寸正确；
- 关键点取色，例如 desk、标题栏、歌词高亮块、进度条填充；
- 帧键相同时不重画；
- 折行切分规则，这部分是纯函数，不需要字体。

另外 `--png out.png --sample X.json [--clock]` 可以出图，和 `docs/img/*.png` 并排肉眼对照。

### 3.8 power：logind 休眠

```python
class Logind:
    def __init__(self, bus): ...
    def take_inhibitor()               # Inhibit("sleep", "monita-cs80", "关屏", "delay") → 持有 fd
    def on_prepare_for_sleep(cb)       # cb(True) 进入睡眠前，cb(False) 唤醒后
```

流程：

1. 启动时先拿 delay inhibitor。
2. 收到 `PrepareForSleep(true)`：把事件投进主循环队列。主循环停掉所有定时任务，执行 `session.suspend()`（发 SLEEP，超时 1 s），然后关闭 inhibitor 的 fd，系统才会真正挂起。
3. 收到 `PrepareForSleep(false)`：`session.resume()`，重新拿 inhibitor。下一轮循环会 HELLO、推整帧、重发灯。

监听线程阻塞在 jeepney 的接收上，只往队列里放事件，不碰 Link。

补充几点：

- logind 默认 `InhibitDelayMaxSec=5`：从收到信号到关 fd 必须在 5 s 内。SLEEP 超时 1 s，加上主循环最多 50 ms 的轮询间隔，够用；但主循环这时如果正卡在 2 s 的音量子进程里，也还在预算内。不要在这条路径上加别的阻塞。
- 没连上设备时收到 `PrepareForSleep(true)`：不发 SLEEP，直接关 fd。
- 拿不到 inhibitor（没有 logind 的发行版、容器里、系统总线不可用）：记一条警告，不处理休眠。后果只是设备在主机睡着后约 5 s 回开机页、10 min 后自己休眠，不会出错。
- 唤醒后 USB 可能被重新枚举，旧端口句柄失效：靠 §3.2 的"断开就重开端口"恢复，不在这里特殊处理。
- 睡眠期间收到 SIGTERM：直接关端口退出，**不发 LED off**（任何字节都会叫醒设备）。

测试：用假 Bus 手动触发信号。至少覆盖：

- SLEEP 发出之后才关闭 fd；
- 睡眠期间主循环不往端口写任何字节；
- 唤醒后第一帧是 HELLO，之后在下一次 PRESENT 之前出现整帧的全部 RECT，LED 也会重发（整帧和 LED 的先后不作要求，Mac 版也不保证）；
- 拿不到 inhibitor 时不抛异常；
- 没连上设备时收到 PrepareForSleep(true) 不写端口。

### 3.9 system：音量和统计

```python
class Volume:               # Wpctl 或 Pactl，启动时按 which 选择
    def step(self, pct: int) -> None   # 例如 wpctl set-volume -l 1.0 @DEFAULT_AUDIO_SINK@ 6%+ ；+ 时 set-mute 0
```

- 音量动作串行执行。主循环本来就是单线程，调用是同步的 subprocess，并带 2 s 超时。
- 具体命令：wpctl 是 `wpctl set-volume -l 1.0 @DEFAULT_AUDIO_SINK@ 6%+`（+ 时再 `wpctl set-mute @DEFAULT_AUDIO_SINK@ 0`）；pactl 是 `pactl set-sink-volume @DEFAULT_SINK@ +6%`（+ 时再 `pactl set-sink-mute @DEFAULT_SINK@ 0`）。pactl 没有上限参数，+ 之前先读当前音量，超过 100% 就不再加。
- 先找 wpctl 再找 pactl：PipeWire 系统通常两个都有（pipewire-pulse），wpctl 更直接；纯 PulseAudio 系统只有 pactl。
- `stats.py`：`cpu_fraction(prev, cur)` 照搬成纯函数；电池读 `/sys/class/power_supply/BAT*/capacity` 和 `AC*/online`（或 `ADP*`）。根目录可以注入，方便用临时目录做测试。

测试：注入一个假 `run`，断言生成的命令行。stats 在临时目录里伪造 sysfs 和 /proc 文件来测。

### 3.10 app：主循环

一个循环，每轮最多睡 50 ms：

| 任务 | 周期 | 说明 |
|---|---|---|
| 处理事件队列 | 每轮 | logind 事件、退出信号 |
| `poll_keys` → KeyMapper → 动作 | 50 ms | 睡眠期间照样读，不执行写操作 |
| 连接 / PING | 2 s | 没连上就重连（成功就标记推整帧），连着就 PING |
| 段码屏 | 250 ms 算一次 | 变了才发；没变也每 1.5 s 重发一次 |
| 灯 | 0.5 s | 灯语变了或 `generation` 变了才发 |
| 正在播放 / 歌词 | 1 s | `Lyrics.payload()` |
| 渲染 | 到了 `schedule.next_wake` | 帧键变了才画；差分推，失败就推整帧；另外 60 min 兜底推一次整帧 |

- 退出：收到 SIGINT / SIGTERM 时先发 LED off，再关闭端口。systemd stop 发的就是 SIGTERM。设备正在睡（`suspend()` 之后）时不发 LED off，见 §3.8。
- 主循环只拿到上面这些接口的实例。`app.build(args)` 负责装配真实实现，测试里装配假实现，再加一个"假端口 + 假时钟"的集成测试，跑完一整轮。

## 4. 依赖

| 依赖 | 用途 | 说明 |
|---|---|---|
| Python ≥ 3.9 | | 只用标准库 + 下面三个 |
| Pillow | 渲染 | 必需 |
| jeepney | D-Bus（MPRIS、logind） | 只在 Linux 上需要；Mac 开发和单测不需要 |
| pyusb + libusb-1.0 | `--device usb` | 可选：走串口 `/dev/ttyUSB*` 时不需要 |
| pyserial | 串口 | 可选：没有就用 monita_link 自带的 PosixPort |
| wpctl 或 pactl | 音量 | 命令行工具，二者有一个就行；wpctl 跑不通（老版本没有 `-l`、实际跑的是 PulseAudio）时改用 pactl；都没有时音量键只打日志 |

**D-Bus 库选 jeepney，不选 dbus-next，** 原因有四点：

1. 纯 Python，没有 C 扩展，Debian / Fedora / Arch 都有包，又是 keyring / secretstorage 的依赖，很多桌面已经装了。
2. 支持阻塞 I/O，和单线程主循环加一个阻塞监听线程的结构正合适。dbus-next 以 asyncio 为主，用它就得把整个主循环改成 async，或者再嵌一个事件循环。
3. 支持 Unix fd 传递（`enable_fds=True`），logind 的 inhibitor 正好靠这个。
4. 用到的 API 很少：方法调用、Get/GetAll、AddMatch 订阅信号。全部收在 `system/dbus.py` 的一个小 Bus 接口里，以后真要换库也只改这一个文件。

## 5. 配置

只用命令行参数，不加配置文件（YAGNI）。systemd 单元直接在 `ExecStart` 里写参数。

```
--device usb|usb:N|/dev/ttyUSBn   默认 usb
--player NAME                     优先的 MPRIS 播放器（总线名后缀，如 spotify）
--page lyrics|clock               启动页
--tick 0.2                        渲染节拍（秒）
--font-sans/--font-mono/--font-cjk PATH
--png OUT --sample JSON [--clock] 离线出图，不连设备
-v                                日志详细一些
```

环境变量只认 `MONITA_HOME`（歌词缓存目录，lyrics.py 已经支持）。测试里的时间靠注入 `clock` / `mono`，不走环境变量。

## 6. systemd 和 udev（示例，放 `linux/packaging/`）

`~/.config/systemd/user/monita-cs80.service`：

```ini
[Unit]
Description=Monita-CS80 副屏上位机
PartOf=graphical-session.target
After=graphical-session.target

[Service]
ExecStart=%h/.local/share/monita-cs80/linux/monita-cs80-linux --device usb
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
```

为什么是 `default.target` 而不是 `graphical-session.target`：

- MPRIS 在会话总线上，音量要访问用户自己的 PipeWire，这两样 `systemd --user` 本身就有（用户管理器自带 `DBUS_SESSION_BUS_ADDRESS`），不依赖图形会话。
- `graphical-session.target` 只有 GNOME、KDE 这类和 systemd 集成的桌面才会启动；sway、i3 等窗口管理器默认不会，服务就永远不起。挂在 `default.target` 下各种桌面都能用。
- 没有任何会话（开了 linger、只 ssh 登录）时服务也能跑：没有播放器就显示时钟页 / "未在播放"，按键里只有 ≡ 有用。但这时 udev 的 `uaccess` **不生效**（它只给当前活动座席上的用户 ACL），要访问 USB 得走下面的 `plugdev` 组。
- 主机设备不在时进程不退出、自己每 2 s 重连（§3.1），所以 `Restart=on-failure` 就够了，不需要 udev 触发启动。

`/etc/udev/rules.d/70-monita-cs80.rules`：

```
# 让 ModemManager 别去探测这台"Quectel 模块"：它会往串口发 AT 命令，打乱协议帧
ATTRS{idVendor}=="2c7c", ATTRS{idProduct}=="0901", ENV{ID_MM_DEVICE_IGNORE}="1"
# pyusb 直连：给当前活动座席上的用户访问权限（uaccess）
SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="2c7c", ATTR{idProduct}=="0901", TAG+="uaccess"
# 没有 logind / 无会话（linger）时改用组权限，二选一：
# SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="2c7c", ATTR{idProduct}=="0901", GROUP="plugdev", MODE="0660"
# 串口路径：接口 7 若被内核驱动绑成 ttyUSB，给个固定名字
SUBSYSTEM=="tty", ATTRS{idVendor}=="2c7c", ATTRS{idProduct}=="0901", ENV{ID_USB_INTERFACE_NUM}=="07", SYMLINK+="monita-cs80", TAG+="uaccess"
```

注意：

- 文件号必须小于 73：`uaccess` 标签由 `73-seat-late.rules` 处理，排在它后面就不生效。
- ttyUSB 节点默认属于 `dialout` 组；`uaccess` 只覆盖有活动会话的情况，无会话时把用户加进 `dialout`。
- `ENV{ID_USB_INTERFACE_NUM}` 由更早的 `60-persistent-serial.rules` 填，所以 70 号文件里能用。
- 装好后 `sudo udevadm control --reload && sudo udevadm trigger`，再拔插一次设备。
- 有的发行版没有 `plugdev` 组（Fedora、Arch），README 里写"没有就建一个或换成你自己的组"。

内核的 option 驱动是否会认 PID 0x0901，目前**没有验证过**，文档里不下结论。两条路都写上：

- 出现了 `/dev/monita-cs80`，就用 `--device /dev/monita-cs80`；
- 否则用 `--device usb`，UsbPort 会自己 detach 内核驱动。

## 7. 测试计划

1. **单测**（Mac 和 Linux 都能跑，命令 `python3 -m unittest discover -s linux/tests`）：§3 里每个模块的测试都用假实现，不需要 D-Bus、USB 或设备。缺字体的渲染测试会 skip，并打印原因。
2. **fwsim 集成**（`test_fwsim.py`）：在 worktree 里用 `cc` 编出 `tools/fwsim/build/fwsim`，没有 cc 就 skip。直接 import `tools/fwsim/conformance.py` 的 `Sim`（起模拟器、拿 PTY、发 stat）和 `seg_text`，不再自己写一套。用串口路径，让 Session + app 主循环（假 MPRIS / 假 Volume / 假 logind，真渲染器）对着模拟器跑，再用 `stat` 断言：
   - HELLO 之后 `link_up==1`，`boot_screen==0`；
   - 推完整帧后 `screen_crc == zlib.crc32(本地 RGB565 整帧)`（模拟器的 screen_crc 就是整块扫描缓冲的 CRC-32/IEEE）；换一句歌词后差分推，CRC 仍然相等；
   - 段码屏：`seg_text(stat.fg)` 解出的文字等于 `segframe` 对同一状态的期望（播放中是 mm:ss）；
   - 灯：播放中 `led_mask==2`（绿）、`led_mode==1`；暂停后熄灭；
   - `tap 3` 会让假 Volume 收到 +6，`tap 2` 切到时钟页（screen_crc 变化且等于本地时钟页的 CRC）；`down 2` 后等 LONG 再 `up 2` 不切页；
   - keepalive：主循环暂停 3.6 s（假任务阻塞）后恢复，Session 先重 HELLO 再推整帧，`screen_crc` 正确；**全程没有出现 `boot_screen==1`**；
   - 主机走了：主循环暂停 ≥5.5 s（固件阈值 5000×1.024 ms≈5.12 s，留余量；5.2 s 太贴边）后 `boot_screen==1`、`link_up==0`；恢复后重新 HELLO，`screen_crc` 回到本地帧，`led` 恢复绿灯；
   - 休眠：假 logind 触发 `PrepareForSleep(true)` 后 `asleep==1`，之后 1.5 s 内 `rx_bytes` 不变（期间主循环照常轮询，但不能写）；inhibitor fd 在 SLEEP 的 ACK 之后才关；
   - 唤醒：触发 `PrepareForSleep(false)` 后 `asleep==0`，`screen_crc` 回到本地帧，`led` 恢复；
   - 拔线：`unplug` 后 Session 判断开、重开端口，不崩溃。
   每条断言都是"Linux 版自己的代码"才能过的：协议编码本身已由现有对拍覆盖，这里测的是 Session / app / segframe / render 的拼装。
3. **现有对拍照跑**：`sh tools/fwsim/run.sh --python-only`，确认 `monita_link.py`（Linux 版的协议层）在 v10 固件下仍然全过。Linux 版不新增 `--conformance` 模式（见"评审修订" R1）。
4. **出图对照**：`--png` 按三个样例和时钟页各出一张，和 `docs/img/` 并排肉眼检查，不放进自动测试。
5. **真设备冒烟**（可选，需要维护者确认）：在 Mac 上用 pyusb 跑 `--device usb`，看歌词页、按键、灯、段码屏。前提是设备已插上，并且维护者同意临时停掉 LaunchAgent `com.monita.cs80`。没有确认就不做。
6. **待 Linux 实机验证**（本机无法覆盖，写进 README）：
   - 真实的 MPRIS 播放器（Spotify、mpv、浏览器）；
   - logind 挂起和唤醒；
   - wpctl / pactl；
   - udev 规则（含 ModemManager 忽略、无会话时的组权限）；
   - 内核驱动绑定时的 detach 路径；
   - sway / i3 这类不启动 graphical-session.target 的环境下服务能自启；
   - 浏览器播放器（实例号、Position 缺失）的降级显示。

## 8. 公开仓库约束

- 只放我们自己写的代码。不写真实 IP、机器名、真名或私人路径。样例主机名用 `monita.local`，锁屏名字默认"主人"（不过这一版不做锁屏）。
- 不提交歌词、封面、原厂固件或原厂地址。
- 提交前另外单独跑一次脱敏检查（不和 commit 串在一起）：`python3 check_public_safe.py <worktree>`。新 worktree 里没有 `.public-safe.local`（它被 gitignore 了），要先把主工作区那份复制过来，否则检查会静默跳过本地词表。

## 9. 明确不做

- 沉浸模式（IMM_*）的页面。对拍脚本里的 imm 一段仍按协议跑。
- 锁屏页（壁纸或 CDE 锁屏框）、关屏联动（各合成器的 DPMS 不统一），以及防睡眠断言。
- 450 ms 滚动动画（接口已留好，见 §3.7）。
- 自动探测 USB 接口、LRCLIB 等第二个歌词来源、配置文件。
- 跟随关屏 / DPMS（不睡眠只关屏时设备保持亮着），多台设备同时接。
- `--conformance` 对拍模式（见"评审修订" R1）。
- 取 `http(s)://` 的 MPRIS 封面。
- Mac 版的旧 HTTP host 链路（BGR565、/api/patch 等）、agent 数据、`--bench` / `--flip-now` / `--scroll-sim`。
- Windows 上位机。
- 打包成 pip、deb 或 rpm。README 里只写"git clone + 系统包装依赖"。

## 10. 实施顺序（TDD，每步先红后绿）

1. `lyrics.py` 加 `np=` 参数和对应单测。
2. transport（UsbPort）→ session（keepalive、判错、休眠）→ 用 fwsim 测 session。
3. segframe → controls → schedule。
4. render：canvas、字体 → 时钟页 → 歌词页 → Front Panel，加 `--png` 出图。
5. sources/mpris、system/{dbus,power,volume,stats}，全部配假实现。
6. app 主循环，`test_fwsim.py` 的集成场景全过；现有对拍照跑一遍。
7. packaging 示例、README，跑脱敏检查（单独跑）。最后 commit，不 push。

## 评审修订

独立评审对照了 `docs/protocol.md`、`monita_link.py`、`lyrics.py`、`tools/fwsim/` 和 Mac 版 `HostPower.swift` / `Controls.swift`，正文已直接改好，这里只记改了什么、为什么。

**R1 删掉 `--conformance` 模式和对 `conformance.py` 的改动（过度设计）。** Linux 版的协议层就是 `monita_link.Link`，打开 fwsim 的 PTY 也走同一个 `open_port`。照 Swift 约定再写一套 `--conformance`，等于在 linux 包里把 `python_session` 抄一遍，对拍出来的仍然是 `monita_link` 自己，测不到任何 Linux 新代码，还要改 `conformance.py` 的 main。改成：现有对拍照跑；`test_fwsim.py` 复用 `conformance.Sim` 等工具，专测 Session / app / segframe / render 拼起来之后的行为（§7.2）。对已有文件的改动从两处减到一处。

**R2 补上遗漏的协议行为（§3.2）。**
- HELLO_ACK 校验：`Link.hello` 只对 proto 发警告，Mac 版对 proto / 尺寸 / pixfmt 不符都断开，Linux 照做。
- caps 门控：LED（bit5）、COPY（bit2）、SLEEP（bit7）按 caps 发；SEG / LED 回非 0 状态不算断开。
- keepalive 的重 HELLO 可能发生在 `seg()` / `ping()` 里，原稿只靠 `push` 返回 False 会漏推整帧，改为以 `generation` 变化为准。
- `Link.push` 等 PRESENT 写死 30 s，Session 自己拼批次、用 10 s，和 Mac 一致。
- 断开后要重开端口（USB 重新枚举后旧句柄失效），`USBError` 不能吞成 `b''`。
- 睡眠中收到 SIGTERM 不能发 LED off，否则会把设备叫醒（§3.8、§3.10）。

**R3 主循环阻塞（§1、§3.5）。** 原稿说"`lyrics.Lyrics` 自带的取词线程"，实际 `payload()` 只给歌词搜索起临时线程，`cover()` 是同步 HTTP（8 s 超时），在主循环里调会让固件以为主机走了。改为封面在后台线程取；D-Bus 调用设 0.5 s 超时；logind 监听线程用独立连接。

**R4 MPRIS 生态的坑（§3.5）。** 排除 `playerctld` 代理；`--player` 改前缀匹配（浏览器 / VLC 带实例号）；多个都在播放时不来回跳；`PlayPause` 发给屏上显示的那个播放器；Position 缺失或恒为 0 时改为估算并标 `estimated`；`snapshot()` 会丢 `artUrl`，改由 MprisSource 单独暴露；`payload()` 的 `cover` 是 HTTP 路径，渲染器要改用 `songId` + `cover(sid)`。

**R5 无歌词降级（§3.6）。** 写明每个 `lyric.status` 对应哪个提示页，以及标题 / 进度 / 段码屏照常显示；断网靠 lyrics.py 现成的 30 s 重试。

**R6 Wayland / X 无关性（§1）。** 写明不依赖显示服务器，只依赖会话总线和系统总线。

**R7 systemd --user（§6）。** `graphical-session.target` 在 sway / i3 等环境不会被启动，服务就永远不起，改挂 `default.target`；写明无会话（linger）时 MPRIS 和音量仍可用，但 `uaccess` 不生效、要走组权限；设备不在时进程不退出。

**R8 udev（§6）。** 加 `ID_MM_DEVICE_IGNORE`：VID 2c7c 是 Quectel，ModemManager 会去探测并往串口发 AT 命令，打乱协议帧，这是 Linux 上最容易踩的坑。usb 规则加 `DEVTYPE=="usb_device"`；写明文件号必须小于 73（`73-seat-late` 处理 uaccess）、ttyUSB 默认 `dialout` 组、部分发行版没有 `plugdev`、改完要 reload + trigger。

**R9 测试计划能否真验证（§7）。**
- "停发 5.2 s 出现开机页"太贴边：固件阈值是 5000 × 1.024 ms ≈ 5.12 s，加上调度抖动容易偶发失败，改成 ≥5.5 s；另加"暂停 3.6 s 不出现开机页"，真正验证 keepalive。
- `screen_crc` 写明就是整块扫描缓冲的 CRC-32/IEEE（`fwsim.c` 的 `sim_screen_crc`），可以直接和 `zlib.crc32(本地整帧)` 比。
- 补上段码屏（`seg_text` 解码）、灯（`led_mask` / `led_mode`）、≡ 切页与长按不切、拔线重连、inhibitor 关 fd 的时机这几项。
- "唤醒后 HELLO → 整帧 → LED"的严格顺序 Mac 版也不保证，放宽为"HELLO 在先，整帧和 LED 都要有"。
- Session 单测补上 `seg()` 里触发重 HELLO、HELLO_ACK 不合格、caps 门控、IO 出错后重开端口。

**R10 音量（§3.9）。** 写全 wpctl / pactl 两套命令；pactl 没有上限参数，要先读再加，避免音量被推过 100%。

保留未改、但值得留意的：Front Panel 的四格（CPU / BAT / Player / Load）都是本地一行读，成本很低，保留；"待 Linux 实机验证"清单里要再加上 ModemManager 忽略规则是否生效、sway / i3 下服务是否自启、无会话时的组权限。

## 实现记录

照本稿实现，出入如下（都往简单的方向走）：

- **没做 `schedule.next_wake`。** 主循环每个节拍（默认 0.2 s）算一次帧键，帧键是纯 Python 小元组，算一次远不到 1 ms；键没变就不画、不转 RGB565、不做差分。省电效果和按唤醒时刻睡一样，少一个函数。
- **generation 变了不等节拍、立刻推整帧**（seg / ping 里触发的重 HELLO 也一样）；每一代只立刻试一次，推失败就回到按节拍重试，免得设备一直拒收时空转。
- **封面缓存**是 `app.py` 里的 `Covers`（后台线程取、缓存成 220×220，只留最近 32 张；取失败 60 s 内不重试，之后再试）。
- **RGB565 转换**用 Pillow 的通道运算（`render/canvas.py` 的 `to_rgb565`），和 `monita_link.rgb888_to_rgb565` 逐字节一致（有测试），一整帧几毫秒；纯 Python 版要一秒多。
- **`MprisLyrics`** 是 `lyrics.Lyrics` 的子类：`_fallback()` 改成直接"未在播放"（不去翻网易云 Mac 客户端的播放缓存），`payload()` 把 `source` / `estimated` 改成 MPRIS 的实际情况。
- **测试文件**：`test_volume` / `test_stats` / `test_power` 合成一个 `test_system.py`；另加 `test_packaging.py`（单元 / 规则的几条硬约束）和 `test_dbus_live.py`（有 jeepney + dbus-daemon 时起一个私有 dbus-daemon，走真 D-Bus 线协议测 MprisSource / Logind，包括 Inhibit 的 fd 传递）。
- **`test_fwsim.py`** 四种配置各起一个子进程并行跑（HELLO 的时区是进程级的，子进程里各用 `CONFIGS` 的 zone），约 30 s；做过变异检查：去掉 keepalive、先关 fd 再发 SLEEP、不按 generation 推整帧、睡着时写字节，各自都会被抓到。
