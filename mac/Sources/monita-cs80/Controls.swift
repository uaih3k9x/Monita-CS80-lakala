// 前面板按键和状态灯（fw v8；纯逻辑在 MonitaCore/Controls.swift，协议见 docs/protocol.md）。
//   按键：+ / − 调系统音量（osascript，每下 6/100）；≡ 短按切 歌词页 ↔ 时钟页；查询 短按 播放/暂停，长按 TODO。
//   状态灯：播放中绿灯常亮，暂停/没在放熄灭；有会话在等人时蓝灯闪（数据源 PanelInfo.waitingLit，直连模式下暂时没有）。
//           每次连上重发一次（设备 HELLO 会灭灯）；渲染器收到 SIGINT/SIGTERM 时先发灭灯再退出。
//   --no-keys / --no-led 分别关掉。
import Foundation
import MonitaCore

private let controlQueue = DispatchQueue(label: "monita.controls")

/// 跑一个外部命令；失败只打日志。都在 controlQueue（串行）上调，不碰渲染线程。
/// wait = true 时等它跑完：音量是"读当前值 + delta 再写回"，连按几下并发跑会互相覆盖、少加几格，所以排队一个个来
private func spawn(_ path: String, _ args: [String], _ what: String, wait: Bool = false) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: path)
    p.arguments = args
    p.standardOutput = FileHandle.nullDevice
    p.terminationHandler = { if $0.terminationStatus != 0 { log("\(what) 失败（exit \($0.terminationStatus)）") } }
    do { try p.run() } catch { log("\(what) 启动失败：\(error.localizedDescription)"); return }
    if wait { p.waitUntilExit() }
}

/// mediaremote-adapter 的目录：MONITA_MRA_DIR；否则 $MONITA_HOME → ~/.monita-cs80 → ~/.cs80_panel 里第一个装好了的
/// （MonitaCore.mraCandidates，tools/lyrics/lyrics.py 同一顺序）
func mraInstalled(_ d: String) -> Bool {
    FileManager.default.fileExists(atPath: d + "/bin/mediaremote-adapter.pl")
        && FileManager.default.fileExists(atPath: d + "/build/MediaRemoteAdapter.framework")
}

func mraDir() -> String {
    let c = mraCandidates(env: ProcessInfo.processInfo.environment, home: NSHomeDirectory())
    return pickMraDir(c, installed: mraInstalled) ?? (NSHomeDirectory() + "/.monita-cs80/mediaremote-adapter")
}

/// 播放/暂停：首选 mediaremote-adapter 的 `send 2`（kMRATogglePlayPause；macOS 15.4+ 借 /usr/bin/perl 的权限），
/// 没装 adapter 时退回进程内直接调 MediaRemote 的 MRMediaRemoteSendCommand（新系统上可能没反应）
private typealias MRSend = @convention(c) (UInt32, CFDictionary?) -> Bool
private let mrSend: MRSend? = {
    guard let h = dlopen("/System/Library/PrivateFrameworks/MediaRemote.framework/MediaRemote", RTLD_LAZY),
          let f = dlsym(h, "MRMediaRemoteSendCommand") else { return nil }
    return unsafeBitCast(f, to: MRSend.self)
}()

func togglePlayPause() {
    let d = mraDir(), script = d + "/bin/mediaremote-adapter.pl", fw = d + "/build/MediaRemoteAdapter.framework"
    if mraInstalled(d) {
        spawn("/usr/bin/perl", [script, fw, "send", "2"], "播放/暂停（adapter）")
    } else if let mrSend {
        if !mrSend(2, nil) { log("播放/暂停：MRMediaRemoteSendCommand 返回失败") }
    } else {
        log("播放/暂停：没有 mediaremote-adapter（\(d)），也加载不了 MediaRemote")
    }
}

func perform(_ a: KeyAction) {
    switch a {
    case .volumeUp: spawn("/usr/bin/osascript", ["-e", volumeScript(delta: 6)], "音量 +", wait: true)
    case .volumeDown: spawn("/usr/bin/osascript", ["-e", volumeScript(delta: -6)], "音量 −", wait: true)
    case .togglePage:
        let p = shared.page.toggled
        log("按键：切到\(p == .clock ? "时钟页" : "歌词页")")
        shared.page = p
    case .playPause: togglePlayPause()
    case .queryLong: log("按键：查询键长按（还没分配功能）")   // TODO: 长按用途待定
    }
}

func startKeys(_ link: DeviceLink) {
    var mapper = KeyMapper()   // 只在 controlQueue 上用
    var lastCount: Int?
    link.onKey = { e in
        controlQueue.async {
            if let c = lastCount, e.count != (c + 1) & 0xFFFF, e.count != 0 { log("按键：事件计数跳了 \(c) → \(e.count)（丢了事件？）") }
            lastCount = e.count
            if let a = mapper.feed(e) { perform(a) }
        }
    }
    every(0.05, "keys") { if link.info?.hasKeys == true { link.pollEvents() } }
}

/// 退出流程开始后置位：LED 循环不再发灯，免得把刚灭的灯又点亮
private let exitingLock = NSLock()
private var _exiting = false
private var exiting: Bool {
    get { exitingLock.lock(); defer { exitingLock.unlock() }; return _exiting }
    set { exitingLock.lock(); _exiting = newValue; exitingLock.unlock() }
}

/// 播放状态 → 灯；变了才发，重连后重发
func startLed(_ link: DeviceLink) {
    var last: LedCommand?
    var lastGen = -1
    every(0.5, "led") {
        guard !exiting, link.isUp, link.info?.hasLed == true else { last = nil; return }
        let d = shared.data
        let want = ledFor(playing: d.map { $0.isPlaying && $0.state == "playing" } ?? false, waiting: shared.panel.waitingLit)
        let gen = link.generation
        if want == last && gen == lastGen { return }
        if let a = link.request(LinkType.led, want.payload).flatMap(AckStats.init), a.status == 0 {
            last = want; lastGen = gen
        } else {
            log("状态灯：LED 没有成功应答")
        }
    }
}

/// SIGINT / SIGTERM：先灭灯再退出（launchd 停服务发的是 SIGTERM）
private var signalSources: [DispatchSourceSignal] = []
func installExitHandler(_ link: DeviceLink?, led: Bool) {
    for sig in [SIGINT, SIGTERM] {
        signal(sig, SIG_IGN)
        let s = DispatchSource.makeSignalSource(signal: sig, queue: .global())
        s.setEventHandler {
            exiting = true
            if led, let link, link.isUp, link.info?.hasLed == true {
                // request 要等 link 锁：渲染线程正在推整帧时最多等它推完（≈ 0.4 s，launchd 默认给 20 s）
                let ok = link.request(LinkType.led, LedCommand.off.payload, timeoutMs: 500).flatMap(AckStats.init)?.status == 0
                log(ok ? "退出：已灭灯" : "退出：灭灯没有应答（设备 15 s 后自己回到未连接心跳）")
            }
            exit(0)
        }
        s.resume()
        signalSources.append(s)
    }
}
