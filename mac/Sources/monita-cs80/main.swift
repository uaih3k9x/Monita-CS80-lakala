// monita-cs80：取数据 → CoreGraphics 画 800×1280 → BGR565 → 32×16 tile 差分 → 推给 CS80
//
// 现阶段的推帧后端是「兼容旧链路」：第三方 host 的 HTTP 接口（/api/patch、/api/frame、/api/status 等，
// 只做接口互操作，不含它的任何代码）。下一步改成直接开 USB 口、走 docs/protocol.md 的自有协议，见 PLAN.md。
//   * 脏 tile ≤ 120 走 patch，否则走 /api/frame 整帧；像素没变不发
//   * 省电：帧键（MonitaCore/Schedule）没变就不画；播放中按 tick 醒，暂停/无播放睡到整分钟，
//     锁屏睡到时钟/留言下一次变化；数据、锁屏、屏保、设备状态一变立即唤醒（Shared.kick）。
//     锁屏期间歌词/布局/Agent/CPU/防睡眠轮询全部暂停（status/saver/heartbeat/lockpoll 照跑）
//   * 启动 / host session 变化 / 设备重新上线 / 最近 patch|frame 失败 → 整帧；60 分钟兜底整帧
//   * host 返回 ok:false（没入队）不推进 prev，下一轮差分会把漏掉的 tile 带上
//   * host 自己的视频屏保期间暂停；每 5 s POST /api/heartbeat
//
// 锁屏（DistributedNotification com.apple.screenIsLocked/Unlocked + 启动时 CGSession 判一次）
// → 整屏桌面壁纸 + 小时钟 + 「<名字> 暂时不在」留言。
//
// 歌词数据：默认从 --lyrics 指定的地址拉 /api/data/lyrics（tools/lyrics/lyrics.py serve 提供，默认同 --host）。
//
// 直连设备：--device 直接推到 R05 固件（docs/protocol.md），不再需要旧 host：
//   usb / usb:<接口号>：IOKit 直接开模组 USB 接口走 bulk（macOS，见 UsbLink.swift；不写接口号就发 HELLO 探测）
//   /dev/xxx：串口（Linux / 有串口驱动时）
//   像素换成 RGB565，脏 tile 合并成矩形 RECT + PRESENT；status/screensaver/heartbeat/layout/agents 轮询都不跑。
//   --bench：只测链路（HELLO/PING/测试图/FBCRC/带宽/SEG 时钟）然后退出，见 Bench.swift
//   --conformance：对着固件模拟器（tools/fwsim/run.sh）跑协议对照脚本，见 Conformance.swift
//
// 用法：monita-cs80 [--host URL | --device usb[:N]|/dev/xxx [--bench] [--no-scroll] [--flip-now]] [--lyrics URL] [--name 名字] [--tick 200]
//                   [--png out.png [--lock] [--sample lyrics.json]] [--dry-run [--lock]]
//   --png --sample：不连网络，用 JSON 样例画一张预览图（README 的图就是这么出的）
//   --dry-run --lock：假装一直锁屏（测锁屏耗电用，不推帧）
//   段码屏（直连时）：背面数码管显示播放进度 / 时长，没在播显示时钟日期，电池区 = Mac 电量，见 Seg.swift；--no-seg 不接管
//   --seg-png out.png：段码屏离线示意图；配 --sample 时画 播放中 / 暂停 / 没在播 三行
//   按键 / 状态灯（直连 + fw v8，见 Controls.swift）：+/− 系统音量，≡ 切 歌词页 ↔ 时钟页，查询 播放/暂停；
//   播放中绿灯常亮。--no-keys / --no-led 关掉；--png out.png --clock 出时钟页预览
//   沉浸模式（直连 + fw v9，见 Immersive.swift）：整屏歌词、滚动改扫描基址（零拷贝）。只有 --immersive 进（查询键长按不再切换）；
//   --png out.png --immersive 出预览
//   休眠（直连 + fw v10）：Mac 睡眠 / 关显示器时发 SLEEP（设备关背光 / 段码屏 / 灯）并停发；醒来重新 HELLO + 整帧。
//   设备 5 s 没收到帧会自己回开机画面，所以隔了 ≥ 3.5 s 才发时 DeviceLink 先重新 HELLO（见 SerialLink.swift）
import AppKit
import MonitaCore
import CoreGraphics
import Foundation
import IOKit.pwr_mgt

setvbuf(stdout, nil, _IOLBF, 0)

// MARK: 参数
var hostURL = "http://127.0.0.1:8787"
var deviceSpec: DeviceSpec?
var bench = false
/// --conformance：对着固件模拟器跑协议对照脚本（tools/fwsim/run.sh 用，见 Conformance.swift），不要对真设备用
var conformance = false
var scrollFlag = true
var scrollSim = false
/// fw v2：翻页不等帧事件（基址帧头锁存时不撕裂，滚动帧率翻倍；待肉眼确认），连上时用 DIAG 11 打开
var flipNow = false
var lyricsURL: String?
var samplePath: String?
var samplePos: Double?
/// 锁屏留言里的名字（「<名字> 暂时不在」）
var awayName = ProcessInfo.processInfo.environment["MONITA_AWAY_NAME"] ?? "主人"
var tickMs = 200.0
var pngPath: String?
var dryRun = false
var pngLock = false
var segOn = true
var keysOn = true
var ledOn = true
var pngClock = false
var immersiveFlag = false
var segPngPath: String?
do {
    var args = CommandLine.arguments.dropFirst().makeIterator()
    while let a = args.next() {
        switch a {
        case "--host": hostURL = args.next() ?? hostURL
        case "--device":
            let v = args.next() ?? ""
            guard let d = parseDeviceSpec(v) else { print("--device 写法：usb、usb:<接口号> 或串口路径（收到 \(v)）"); exit(2) }
            deviceSpec = d
        case "--bench": bench = true
        case "--conformance": conformance = true
        case "--no-scroll": scrollFlag = false
        case "--scroll-sim": scrollSim = true
        case "--flip-now": flipNow = true
        case "--lyrics": lyricsURL = args.next()
        case "--name": awayName = args.next() ?? awayName
        case "--sample": samplePath = args.next()
        case "--at": samplePos = Double(args.next() ?? "")
        case "--tick": tickMs = Double(args.next() ?? "") ?? tickMs
        case "--png": pngPath = args.next()
        case "--dry-run": dryRun = true
        case "--lock": pngLock = true
        case "--no-seg": segOn = false
        case "--no-keys": keysOn = false
        case "--no-led": ledOn = false
        case "--clock": pngClock = true
        case "--immersive": immersiveFlag = true
        case "--seg-png": segPngPath = args.next()
        default:
            print("用法：monita-cs80 [--host URL | --device usb[:N]|/dev/xxx [--bench] [--no-scroll] [--flip-now]] [--lyrics URL] [--name 名字] [--tick ms] [--png out.png [--lock] [--sample lyrics.json [--at 秒]]] [--dry-run] [--no-seg] [--no-keys] [--no-led] [--seg-png out.png [--sample lyrics.json [--at 秒]]] [--png out.png --clock|--immersive] [--immersive]")
            exit(a == "-h" || a == "--help" ? 0 : 2)
        }
    }
}

func log(_ s: String) {
    let f = DateFormatter()
    f.dateFormat = "MM-dd HH:mm:ss"
    print("\(f.string(from: Date())) \(s)")
}

// MARK: HTTP（同步，带超时；localhost 不走代理）
let session: URLSession = {
    let c = URLSessionConfiguration.ephemeral
    c.connectionProxyDictionary = [:]
    c.httpMaximumConnectionsPerHost = 4
    return URLSession(configuration: c)
}()

func http(_ path: String, method: String = "GET", body: [UInt8]? = nil, timeout: Double = 5, base: String? = nil) -> (Int, [UInt8])? {
    guard let url = URL(string: (base ?? hostURL) + path) else { return nil }
    var req = URLRequest(url: url, timeoutInterval: timeout)
    req.httpMethod = method
    if let body { req.httpBody = Data(body); req.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type") }
    let sem = DispatchSemaphore(value: 0)
    var out: (Int, [UInt8])?
    session.dataTask(with: req) { data, resp, _ in
        if let r = resp as? HTTPURLResponse { out = (r.statusCode, data.map { [UInt8]($0) } ?? []) }
        sem.signal()
    }.resume()
    _ = sem.wait(timeout: .now() + timeout + 1)
    return out
}

func json(_ b: [UInt8]) -> [String: Any]? { try? JSONSerialization.jsonObject(with: Data(b)) as? [String: Any] }

func nowMs() -> Double { Date().timeIntervalSince1970 * 1000 }

// MARK: 共享状态（各轮询线程写，渲染线程读）
final class Shared {
    private let lock = NSLock()
    private var _data: LyricsData?
    private var _cover: CGImage?
    private var _coverURL = ""
    private var _cfg = PageConfig()
    private var _panel = PanelInfo()
    private var _saver = false
    private var _device = ""
    private var _markFull = false
    private var _lastSidecar = ""
    private var _locked = false
    private var _lockedSince: Date?
    private var _page = Page.lyrics
    private var _immersive = false
    // 唤醒：渲染循环等 _kicked，暂停中的轮询线程等解锁；任何影响画面的状态变化都 broadcast
    private let cond = NSCondition()
    private var _kicked = false

    func with<T>(_ f: (Shared) -> T) -> T { lock.lock(); defer { lock.unlock() }; return f(self) }

    func kick() { cond.lock(); _kicked = true; cond.broadcast(); cond.unlock() }
    /// 渲染循环：睡到 deadline，或被 kick 提前叫醒
    func waitWake(until deadline: Date) {
        cond.lock()
        while !_kicked && cond.wait(until: deadline) {}
        _kicked = false
        cond.unlock()
    }
    /// 轮询线程：锁屏期间挂起，解锁时 kick 叫醒
    func waitUnlocked() {
        cond.lock()
        while locked { cond.wait() }
        cond.unlock()
    }
    /// 写入并在值真的变了时唤醒渲染循环
    private func set(_ f: (Shared) -> Bool) { if with(f) { kick() } }

    var data: LyricsData? { get { with { $0._data } } set { set { let c = $0._data != newValue; $0._data = newValue; return c } } }
    var cover: (String, CGImage?) {
        get { with { ($0._coverURL, $0._cover) } }
        set { set { let c = $0._coverURL != newValue.0 || $0._cover !== newValue.1; $0._coverURL = newValue.0; $0._cover = newValue.1; return c } }
    }
    var cfg: PageConfig { get { with { $0._cfg } } set { set { let c = $0._cfg != newValue; $0._cfg = newValue; return c } } }
    var panel: PanelInfo { get { with { $0._panel } } }
    func updatePanel(_ f: (inout PanelInfo) -> Void) { set { let old = $0._panel; f(&$0._panel); return old != $0._panel } }
    var saver: Bool { get { with { $0._saver } } set { set { let c = $0._saver != newValue; $0._saver = newValue; return c } } }
    var device: String { get { with { $0._device } } set { set { let c = $0._device != newValue; $0._device = newValue; return c } } }
    var lastSidecar: String { get { with { $0._lastSidecar } } set { with { $0._lastSidecar = newValue } } }
    var locked: Bool {
        get { with { $0._locked } }
        set {
            set {
                let c = $0._locked != newValue
                if newValue && !$0._locked { $0._lockedSince = Date() }
                $0._locked = newValue
                return c
            }
        }
    }
    var lockedSince: Date? { with { $0._lockedSince } }
    /// ≡ 键切换的页（锁屏 / 屏保优先于它）
    var page: Page { get { with { $0._page } } set { set { let c = $0._page != newValue; $0._page = newValue; return c } } }
    /// 沉浸模式开关（--immersive）；锁屏 / 屏保优先于它
    var immersive: Bool { get { with { $0._immersive } } set { set { let c = $0._immersive != newValue; $0._immersive = newValue; return c } } }
    func requestFull() { with { $0._markFull = true }; kick() }
    func takeFull() -> Bool { with { let v = $0._markFull; $0._markFull = false; return v } }
}
let shared = Shared()

/// 每个周期任务一个线程：简单、互不阻塞（host 某个接口慢不会拖住渲染）。
/// pauseWhenLocked：锁屏画面用不到的数据，锁屏期间整个线程挂起，解锁立刻补拉一次
func every(_ seconds: Double, _ name: String, pauseWhenLocked: Bool = false, _ body: @escaping () -> Void) {
    let t = Thread {
        while true {
            if pauseWhenLocked { shared.waitUnlocked() }
            autoreleasepool { body() }
            Thread.sleep(forTimeInterval: seconds)
        }
    }
    t.name = name
    t.start()
}

func fetchLyrics() {
    guard let (code, b) = http("/api/data/lyrics", timeout: 4, base: lyricsURL), code == 200, let d = LyricsData.parse(b) else { return }
    shared.data = d
    let url = d.cover
    if url != shared.cover.0 {
        var img: CGImage?
        if !url.isEmpty, let (c2, bytes) = http(url, timeout: 8, base: lyricsURL), c2 == 200 { img = decodeImage(bytes) }
        // 拿不到也记下 url，避免每秒重试；换歌（url 变化）自然会再拉
        shared.cover = (url, img)
    }
}

func fetchLayout() {
    guard let (code, b) = http("/api/layout", timeout: 4), code == 200, let l = json(b),
          let pages = l["pages"] as? [[String: Any]] else { return }
    guard let page = pages.first(where: { ($0["module"] as? String) == "pages/lyrics.js" }) else { return }
    let c = page["config"] as? [String: Any] ?? [:]
    var cfg = PageConfig()
    cfg.translation = (c["translation"] as? Bool) ?? true
    if let t = c["title"] as? String, !t.isEmpty { cfg.title = t }
    shared.cfg = cfg
    if let active = l["activePage"] as? String, active != page["id"] as? String {
        log("注意：host 当前页是 \(active)，本渲染器只画歌词页")
    }
}

var watch = StatusWatch()
func fetchStatus() {
    guard let (code, b) = http("/api/status", timeout: 4), code == 200, let st = json(b) else {
        shared.device = "host-down"
        return
    }
    let side = st["sidecar"] as? [String: Any] ?? [:]
    let device = st["device"] as? String
    shared.device = device ?? ""
    shared.lastSidecar = "\(side["event"] ?? "-") ok=\(side["ok"] ?? "-") tiles=\(side["tiles"] ?? "-") wire=\(side["wire"] ?? "-") ms=\(side["ms"] ?? "-")"
    if watch.update(device: device, session: (st["session"] as? NSNumber)?.intValue,
                    lastEvent: side["event"] as? String, lastOk: (side["ok"] as? NSNumber)?.intValue) {
        log("status 要求整帧（session=\(st["session"] ?? "-") device=\(device ?? "-") last=\(shared.lastSidecar)）")
        shared.requestFull()
    }
}

func fetchSaver() {
    guard let (code, b) = http("/api/screensaver", timeout: 4), code == 200, let st = json(b) else { return }
    let playing = ((st["saver"] as? [String: Any])?["playing"] as? Bool) ?? false
    if playing != shared.saver {
        shared.saver = playing
        log(playing ? "屏保开始，暂停推帧" : "屏保结束，整帧重推")
        if !playing { shared.requestFull() }
    }
}

var waitingFlag = Debounced(false, minInterval: 2000)
func fetchAgents() {
    guard let (c1, b1) = http("/api/data/agents", timeout: 8), c1 == 200 else { return }
    let st = http("/api/data/agent", timeout: 4).flatMap { $0.0 == 200 ? $0.1 : nil }
    guard let counts = AgentCounts.parse(b1, agentState: st) else { return }
    let lit = waitingFlag.update(counts.waiting > 0, nowMs: nowMs())
    shared.updatePanel { $0.agents = counts; $0.waitingLit = lit }
}

/// CPU：本地 host_statistics(HOST_CPU_LOAD_INFO) 差分，不依赖 host。
var cpuPrev: [UInt32] = []
func sampleCPU() {
    var info = host_cpu_load_info()
    var count = mach_msg_type_number_t(MemoryLayout<host_cpu_load_info>.size / MemoryLayout<integer_t>.size)
    let kr = withUnsafeMutablePointer(to: &info) {
        $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) { host_statistics(mach_host_self(), HOST_CPU_LOAD_INFO, $0, &count) }
    }
    guard kr == KERN_SUCCESS else { return }
    let t = info.cpu_ticks
    let cur = [t.0, t.1, t.2, t.3]   // user, system, idle, nice
    if let frac = cpuFraction(prev: cpuPrev, cur: cur) {
        shared.updatePanel { $0.cpu = Int((frac * 100).rounded()) }
    }
    cpuPrev = cur
}

/// 当前是否锁屏（启动时判一次，之后靠通知；5 s 轮询兜底，防止漏通知）
func sessionLocked() -> Bool {
    guard let d = CGSessionCopyCurrentDictionary() as? [String: Any] else { return false }
    return (d["CGSSessionScreenIsLocked"] as? Bool) ?? ((d["CGSSessionScreenIsLocked"] as? NSNumber)?.boolValue ?? false)
}

/// 防睡眠断言：IOKit 直接取，不 fork pmset
func readSleepGuard() {
    var u: Unmanaged<CFDictionary>?
    guard IOPMCopyAssertionsByProcess(&u) == kIOReturnSuccess, let dict = u?.takeRetainedValue() as? [AnyHashable: Any] else { return }
    var all: [[String: Any]] = []
    for (_, v) in dict { if let list = v as? [[String: Any]] { all += list } }
    let g = sleepGuard(all)
    shared.updatePanel { $0.sleep = g }
}

// MARK: 渲染 + 推帧
let canvas = Canvas(w: screenW, h: screenH)
let page = LyricsPage(canvas)
let wallpaper = Wallpaper()
var wallpaperCheckedAt = 0.0
/// 样例模式不读本机名和壁纸，预览图里不带任何个人信息
let hostName = samplePath != nil ? "monita.local" : (Host.current().localizedName ?? ProcessInfo.processInfo.hostName)

/// 旧 host 要 BGR565，R05 固件要 RGB565
func canvasBGR565() -> [UInt16] {
    var buf = [UInt16](repeating: 0, count: screenW * screenH)
    if deviceSpec != nil { toRGB565(canvas.pixels, count: screenW * screenH, into: &buf) }
    else { toBGR565(canvas.pixels, count: screenW * screenH, into: &buf) }
    return buf
}

func renderLock(_ date: Date, since: Date?) -> [UInt16] {
    page.drawLock(wallpaper: wallpaper.image, date: date, host: hostName, since: since)
    return canvasBGR565()
}

/// 歌词页输入一次性取快照：帧键和实际绘制用同一份数据
func lyricsInput(_ date: Date) -> (PageInput, coverURL: String) {
    let (url, cover) = shared.cover
    var cfg = shared.cfg
    cfg.scroll = cfg.scroll && scrollFlag && (link?.info?.hasCopy ?? false)   // 只有固件支持 COPY 才滚动
    let inp = PageInput(data: shared.data, cover: cover, cfg: cfg, panel: shared.panel,
                        nowMs: date.timeIntervalSince1970 * 1000, date: date)
    return (inp, url)
}

func render(_ inp: PageInput) -> [UInt16] {
    page.draw(inp)
    return canvasBGR565()
}

func renderClock(_ inp: PageInput) -> [UInt16] {
    page.drawClock(inp)
    return canvasBGR565()
}

/// 画面依赖的全部输入；和上一帧相同就不画
enum FrameKey: Equatable {
    case lock(LockFrameKey)
    case lyrics(LyricsFrameKey, coverURL: String, hasCover: Bool, cfg: PageConfig, panel: PanelInfo, scroll: Int)
    case clock(minute: Int, panel: PanelInfo)
}

func posted(_ r: (Int, [UInt8])?) -> Bool {
    guard let (code, b) = r, code == 200, let j = json(b) else { return false }
    return (j["ok"] as? Bool) != false
}

if scrollSim {
    guard let sp = samplePath, let bytes = FileManager.default.contents(atPath: sp), let d = LyricsData.parse([UInt8](bytes)) else {
        print("--scroll-sim 要配 --sample lyrics.json"); exit(2)
    }
    exit(runScrollSim(d, render: render, page: page) ? 0 : 1)
}

if let out = segPngPath {
    var rows: [(String, [UInt8])] = []
    let now = Date()
    if let sp = samplePath {
        guard let bytes = FileManager.default.contents(atPath: sp), var d = LyricsData.parse([UInt8](bytes)) else {
            print("读不了样例 \(sp)"); exit(1)
        }
        d.play.at = now.timeIntervalSince1970 * 1000
        if let p = samplePos { d.play.pos = p }
        d.state = "playing"; d.play.state = "playing"; d.play.rate = 1
        rows.append(("播放中（电量 60%，没插电）", segFrame(segInput(d, now, battery: SegBattery(percent: 60, onAC: false)))))
        d.state = "paused"; d.play.state = "paused"; d.play.rate = 0
        rows.append(("暂停（主区闪烁的亮半周；插着电 100%）", segFrame(segInput(d, Date(timeIntervalSince1970: (now.timeIntervalSince1970 / 1).rounded(.down)), battery: SegBattery(percent: 100, onAC: true)))))
        rows.append(("没在播：时间 + 日期（插着电 100%）", segFrame(segInput(nil, Date(timeIntervalSince1970: (now.timeIntervalSince1970 / 1).rounded(.down)), battery: SegBattery(percent: 100, onAC: true)))))
    } else {
        fetchLyrics()
        rows.append(("当前", segFrame(segInput(shared.data, now, battery: macBattery()))))
    }
    let ok = writeSegPreview(rows, out)
    print(ok ? "wrote \(out)" : "write failed")
    exit(ok ? 0 : 1)
}

if let png = pngPath {
    // 截图模式：拉一次数据（或读 --sample 样例）就画，不推帧
    if let sp = samplePath {
        guard let bytes = FileManager.default.contents(atPath: sp), var d = LyricsData.parse([UInt8](bytes)) else {
            print("读不了样例 \(sp)"); exit(1)
        }
        // 样例里的进度按「此刻」解释：at = 现在，pos 可用 --at 覆盖
        d.play.at = nowMs()
        if let p = samplePos { d.play.pos = p }
        shared.data = d
        // 面板数据也用固定值，预览图不带本机状态
        shared.updatePanel {
            $0.agents = AgentCounts(claude: 2, waiting: 1, dsh: 0); $0.waitingLit = true
            $0.cpu = 12; $0.sleep = SleepGuard(awake: true, holder: "caffeinate", others: 0)
        }
    } else {
        fetchLayout(); fetchLyrics(); fetchAgents(); readSleepGuard()
        sampleCPU(); Thread.sleep(forTimeInterval: 0.5); sampleCPU()
    }
    if pngLock { if samplePath == nil { wallpaper.refresh() }; _ = renderLock(Date(), since: samplePath == nil ? nil : Date(timeIntervalSinceNow: -25 * 60)) }
    else if pngClock { _ = renderClock(lyricsInput(Date()).0) }
    else if immersiveFlag {
        let inp = lyricsInput(Date()).0
        let c = page.immBuild(inp.data, cfg: inp.cfg)
        let pos = inp.data.map { positionAt($0.play, nowMs: inp.nowMs) } ?? 0
        let line = c.times.isEmpty ? -1 : lineIndex(c.times, pos + lyricLeadS)
        page.immDrawWindow(c, y: immTarget(c.blocks, line: line, screenH: screenH), line: line)
    }
    else { _ = render(lyricsInput(Date()).0) }
    let ok = canvas.writePNG(png)
    print(ok ? "wrote \(png)" : "write failed")
    exit(ok ? 0 : 1)
}

let link: DeviceLink? = deviceSpec.map { spec in
    switch spec {
    case .serial(let path): return DeviceLink(name: path) { SerialPort(path: path) }
    case .usb(let n): return DeviceLink(name: n.map { "usb:\($0)" } ?? "usb") { openUsb(interface: n) }
    }
}
if bench || conformance {
    guard let link else { print("--bench / --conformance 要配 --device"); exit(2) }
    exit((conformance ? runConformance(link) : runBench(link)) ? 0 : 1)
}
log("monita-cs80 启动：\(link.map { "device=\($0.name)" } ?? "host=\(hostURL)") tick=\(Int(tickMs))ms\(dryRun ? " (dry-run)" : "")")
let forceLock = dryRun && pngLock   // --dry-run --lock：模拟锁屏
shared.immersive = immersiveFlag && link != nil
let immDriver = link.map { ImmersiveDriver(link: $0, page: page) }
shared.locked = forceLock || sessionLocked()
wallpaper.refreshInBackground()                       // 先备好，第一次锁屏不用等解码
every(1.0, "lyrics", pauseWhenLocked: true, fetchLyrics)
if let link {
    // 直连：每 2 s 没连上就重连（连上立即整帧），连着就 PING 当心跳
    every(2.0, "link") {
        if link.connectIfNeeded() {
            if flipNow, link.info?.hasCopy == true {
                log(link.setFlipNow() ? "翻页模式：立即写基址（--flip-now）" : "固件不认 DIAG 11（或正在沉浸模式里）")
            }
            shared.device = "up"; shared.requestFull()
        }
        else if link.isUp { link.ping() }
        if !link.isUp { shared.device = "" }
    }
} else {
    every(2.0, "status", fetchStatus)
    every(2.5, "saver", fetchSaver)
    every(10.0, "layout", pauseWhenLocked: true, fetchLayout)
    every(5.0, "agents", pauseWhenLocked: true, fetchAgents)
}
if let link, segOn { startSegLoop(link) }
if let link, keysOn { startKeys(link) }
if let link, ledOn { startLed(link) }
installExitHandler(link, led: ledOn)
every(2.0, "cpu", pauseWhenLocked: true, sampleCPU)
every(5.0, "sleep", pauseWhenLocked: true, readSleepGuard)
every(5.0, "lockpoll") {
    let l = forceLock || sessionLocked()
    if l != shared.locked { log("锁屏状态（轮询）：\(l)"); shared.locked = l }
}
for (name, v) in [("com.apple.screenIsLocked", true), ("com.apple.screenIsUnlocked", false)] where !forceLock {
    DistributedNotificationCenter.default().addObserver(forName: Notification.Name(name), object: nil, queue: nil) { _ in
        log("锁屏通知：\(name)")
        shared.locked = v
    }
}
// fw v10：Mac 睡眠 / 关显示器 → SLEEP（设备关背光 / 段码屏 / 灯）并停发；醒来 → 重新 HELLO + 整帧（任何一帧都会叫醒设备）
if let link, !dryRun {
    let ws = NSWorkspace.shared.notificationCenter
    for name in [NSWorkspace.willSleepNotification, NSWorkspace.screensDidSleepNotification] {
        ws.addObserver(forName: name, object: nil, queue: nil) { _ in
            guard let ok = link.sleep() else { return }          // 没连着 / 已经睡了 / 固件不支持
            log("\(name.rawValue)：设备休眠\(ok ? "" : "（SLEEP 没有应答）")")
            shared.device = ""                                    // 渲染循环停推
        }
    }
    for name in [NSWorkspace.didWakeNotification, NSWorkspace.screensDidWakeNotification] {
        ws.addObserver(forName: name, object: nil, queue: nil) { _ in
            link.resume()
            DispatchQueue.global().async {
                if link.connectIfNeeded() { log("\(name.rawValue)：设备唤醒，推整帧"); shared.device = "up"; shared.requestFull() }
            }
        }
    }
}
// host 自己的「锁屏自动播视频屏保」开着的话，锁屏时它会抢屏：提示一下（本渲染器会让路）
if link == nil, let (c, b) = http("/api/screensaver", timeout: 4), c == 200, let j = json(b), (j["autoLock"] as? Bool) == true {
    log("提示：host 的「锁屏播放视频屏保」开着（autoLock=true），锁屏时会播视频而不是壁纸。关掉：curl --noproxy '*' -X POST -d '{\"autoLock\":false}' \(hostURL)/api/screensaver")
}
if !dryRun && link == nil { every(5.0, "heartbeat") { _ = http("/api/heartbeat", method: "POST", body: [], timeout: 3) } }

var prev: [UInt16]?
var mode = ScreenMode.lyrics
var fullNeeded = true
var lastFull = 0.0
var lastKey: FrameKey?
var retry = false                    // 上一次推送失败：按 tick 快速重试
let tickS = tickMs / 1000
struct Stats { var wakes = 0, ticks = 0, renderMs = 0.0, patches = 0, tiles = 0, bytes = 0, fulls = 0, fails = 0, copies = 0 }
var stats = Stats()
var statsAt = nowMs()

/// 一次唤醒：判断要不要画、画了推什么；返回下一次醒来的时刻（秒）
func tick() -> Double {
    stats.wakes += 1
    if shared.takeFull() { fullNeeded = true }
    let m = screenMode(locked: shared.locked, saverPlaying: shared.saver, page: shared.page, immersive: shared.immersive)
    if m != mode {
        log("画面切换：\(mode) → \(m)")
        if mode == .immersive { immDriver?.exit() }      // 先还原设备上的多缓冲，再推整帧
        mode = m
        fullNeeded = true            // 进/出锁屏都一次整帧，之后只有小块差分
        wallpaperCheckedAt = 0
    }
    let now = Date()
    let since = shared.lockedSince
    let d = shared.data
    let playing = d.map { $0.isPlaying && $0.state == "playing" } ?? false
    let active = m != .yieldToSaver && shared.device == "up"
    func wake() -> Double {
        // 滚动动画中：推完一帧马上画下一帧（push 会等 PRESENT 的 ACK，天然按设备速度限流）
        if m == .lyrics && page.scrolling(nowMs: nowMs()) && active { return Date().timeIntervalSince1970 + 0.002 }
        return nextWake(mode: m, active: active, playing: playing, retry: retry, nowS: Date().timeIntervalSince1970,
                 sinceS: since?.timeIntervalSince1970, tickS: tickS)
    }
    if !active { fullNeeded = true; return wake() }
    if m == .immersive, let immDriver, let link {
        fullNeeded = true                              // 出沉浸模式时整帧
        if link.info?.hasImmersive != true {
            log("沉浸模式：固件不支持（要 fw v9），留在普通页面"); shared.immersive = false; return wake()
        }
        if !immDriver.active && !immDriver.enter() { shared.immersive = false; return wake() }
        let inp = lyricsInput(now).0
        return immDriver.tick(inp.data, cfg: inp.cfg, nowMs: inp.nowMs)
    }
    if nowMs() - lastFull > 3_600_000 { fullNeeded = true }  // 60 分钟兜底
    if m == .lockWallpaper && nowMs() - wallpaperCheckedAt > 60_000 {
        wallpaperCheckedAt = nowMs()
        wallpaper.refreshInBackground()               // 换了会 requestFull；渲染线程从不等它（以前在这儿同步问主线程，解锁后卡过 31 s）
    }

    let key: FrameKey
    var inp: PageInput?
    var clockInp: PageInput?
    if m == .lockWallpaper {
        key = .lock(lockFrameKey(nowS: now.timeIntervalSince1970, sinceS: since?.timeIntervalSince1970))
    } else if m == .clock {
        clockInp = lyricsInput(now).0
        key = .clock(minute: Int((now.timeIntervalSince1970 / 60).rounded(.down)), panel: clockInp!.panel)
    } else {
        let (i, url) = lyricsInput(now)
        inp = i
        key = .lyrics(lyricsFrameKey(i.data, nowMs: i.nowMs, progressW: page.progressInnerW),
                      coverURL: url, hasCover: i.cover != nil, cfg: i.cfg, panel: i.panel,
                      scroll: i.cfg.scroll ? page.scrollOffset(nowMs: i.nowMs) : 0)
    }
    if !fullNeeded && !retry && key == lastKey { return wake() }

    let t0 = nowMs()
    let buf = inp.map(render) ?? clockInp.map(renderClock) ?? renderLock(now, since: since)
    stats.ticks += 1
    stats.renderMs += nowMs() - t0
    let dirty = prev.map { diffTiles($0, buf) } ?? []
    var ok = true
    switch decidePush(fullNeeded: fullNeeded, hasPrev: prev != nil, dirty: dirty.count, maxPatch: link == nil ? maxPatchTiles : .max) {
    case .none:
        break
    case .patch:
        if let link {
            // 歌词区这一帧是纯平移：让设备 COPY，只发露出的条带和别处真变了的 tile
            var plan = ScrollPlan(copy: nil, dirty: dirty)
            if inp != nil, let dy = page.scrollDelta, dy != 0, let prev, link.info?.hasCopy == true {
                plan = planScroll(prev: prev, next: buf, region: page.lyricClip, dy: dy)
            }
            let rects = mergeTiles(plan.dirty)
            if !dryRun { ok = link.push(rects, buf, copies: plan.copy.map { [$0] } ?? [], frameID: stats.ticks) }
            let rectBytes = rects.reduce(0) { (acc: Int, r: LinkRect) -> Int in acc + 18 + r.w * r.h * 2 }
            stats.bytes += rectBytes + (plan.copy == nil ? 0 : 22)
            if plan.copy != nil { stats.copies += 1 }
        } else {
            let body = encodePatch(dirty, buf)
            if !dryRun { ok = posted(http("/api/patch", method: "POST", body: body, timeout: 5)) }
            stats.bytes += body.count
        }
        stats.patches += 1; stats.tiles += dirty.count
    case .full:
        if !dryRun {
            if let link { ok = link.push(fullRects(), buf, frameID: stats.ticks) }
            else { ok = posted(http("/api/frame", method: "POST", body: toRGB(canvas.pixels, count: screenW * screenH), timeout: 30)) }
        }
        if ok { fullNeeded = false; lastFull = nowMs() }
        stats.fulls += 1
        log("整帧 \(ok ? (link == nil ? "已入队" : "已推送") : "失败") (\(dirty.count) dirty)")
    }
    if ok { prev = buf; lastKey = key; retry = false } else {
        stats.fails += 1; retry = true
        if link != nil { fullNeeded = true }   // 直连时失败 = 设备上的内容不确定，下一次整帧
    }
    return wake()
}

let renderThread = Thread {
    var next = Date(timeIntervalSinceNow: 0.3)
    while true {
        shared.waitWake(until: next)
        autoreleasepool {
            next = Date(timeIntervalSince1970: tick())
            if nowMs() - statsAt >= 60_000 {
                let n = max(stats.ticks, 1)
                log(String(format: "60s: wakes=%d ticks=%d render=%.1fms patches=%d copies=%d tiles=%d bytes=%d full=%d fail=%d | sidecar %@",
                           stats.wakes, stats.ticks, stats.renderMs / Double(n), stats.patches, stats.copies, stats.tiles, stats.bytes,
                           stats.fulls, stats.fails, shared.lastSidecar))
                stats = Stats(); statsAt = nowMs()
            }
        }
    }
}
renderThread.name = "render"
renderThread.stackSize = 4 << 20
renderThread.start()

signal(SIGPIPE, SIG_IGN)
RunLoop.main.run()   // 主 run loop：收 DistributedNotification、NSWorkspace 调用

