// 前面板按键 KEY（0x90，设备 → 主机）和状态灯 LED（0x31，主机 → 设备）的纯逻辑（fw v8，docs/protocol.md）。
// 执行动作（音量、播放/暂停、切页、发 LED）在 monita-cs80/Controls.swift。

public enum PanelKey: UInt8 {
    case query = 1   // 左上「查询」（PWRKEY）
    case menu = 2    // 左下 ≡
    case plus = 3    // 右上 +
    case minus = 4   // 右下 −
}

public enum KeyEdge: UInt8 { case down = 1, up = 2, long = 3 }

/// KEY 载荷：u8 key, u8 ev, u16 n（HELLO 以来的事件计数，回绕），u32 uptime（1.024 ms）
public struct KeyEvent: Equatable {
    public var key: PanelKey, edge: KeyEdge, count: Int, uptime: UInt32
    public init(key: PanelKey, edge: KeyEdge, count: Int, uptime: UInt32) {
        self.key = key; self.edge = edge; self.count = count; self.uptime = uptime
    }
    public init?(_ p: [UInt8]) {
        guard p.count >= 8, let k = PanelKey(rawValue: p[0]), let e = KeyEdge(rawValue: p[1]) else { return nil }
        self.init(key: k, edge: e, count: rd16(p, 2), uptime: rd32(p, 4))
    }
}

public enum KeyAction: Equatable {
    case volumeUp, volumeDown, togglePage, playPause
}

/// 按键事件 → 动作。+/− 按下立即生效；≡、查询 在抬起时判短按（中间没有 LONG）。
/// 固件对 PWRKEY 长按会超时补一个 UP（实机收不到抬起回调），所以 LONG 之后的 UP 一律不算短按。
/// 长按不绑动作（v9 查询键长按进 / 出沉浸模式，容易误触，已去掉；沉浸模式只能 --immersive 进）。
public struct KeyMapper {
    private var armed: Set<UInt8> = []   // 收到过 DOWN、还没 LONG 的键
    public init() {}

    public mutating func feed(_ e: KeyEvent) -> KeyAction? {
        let k = e.key.rawValue
        switch e.edge {
        case .down:
            armed.insert(k)
            if e.key == .plus { return .volumeUp }
            if e.key == .minus { return .volumeDown }
            return nil
        case .long:
            armed.remove(k)
            return nil
        case .up:
            guard armed.remove(k) != nil else { return nil }
            switch e.key {
            case .menu: return .togglePage
            case .query: return .playPause
            default: return nil
            }
        }
    }
}

/// 改系统音量的 AppleScript（osascript 执行）。delta 是 0–100 刻度；+ 顺带解除静音，− 不动静音
public func volumeScript(delta: Int) -> String {
    var s = """
        set v to (output volume of (get volume settings)) + \(delta)
        if v > 100 then set v to 100
        if v < 0 then set v to 0
        set volume output volume v
        """
    if delta > 0 { s += "\nset volume without output muted" }
    return s
}

// MARK: 状态灯

public let ledRed: UInt8 = 1, ledGreen: UInt8 = 2, ledBlue: UInt8 = 4

public enum LedMode: UInt8 { case off = 0, on = 1, blink = 2 }

/// LED 载荷：u8 mask, u8 mode, u16 period_ms, u16 on_ms, u16 duration_ms（0 = 一直保持）
public struct LedCommand: Equatable {
    public var mask: UInt8, mode: LedMode, periodMs: Int, onMs: Int, durationMs: Int
    public init(mask: UInt8, mode: LedMode, periodMs: Int = 0, onMs: Int = 0, durationMs: Int = 0) {
        self.mask = mask; self.mode = mode; self.periodMs = periodMs; self.onMs = onMs; self.durationMs = durationMs
    }
    public static let off = LedCommand(mask: 0, mode: .off)
    public var payload: [UInt8] {
        var out: [UInt8] = [mask, mode.rawValue]
        put16(&out, periodMs); put16(&out, onMs); put16(&out, durationMs)
        return out
    }
}

/// 灯语：有会话在等人 → 蓝灯 1 Hz 闪；播放中 → 绿灯常亮；其余熄灭
public func ledFor(playing: Bool, waiting: Bool) -> LedCommand {
    if waiting { return LedCommand(mask: ledBlue, mode: .blink, periodMs: 1000, onMs: 500) }
    return playing ? LedCommand(mask: ledGreen, mode: .on) : .off
}

// MARK: mediaremote-adapter 在哪

/// 候选目录，按顺序探测：设了 MONITA_MRA_DIR 就只认它；否则 $MONITA_HOME/mediaremote-adapter → ~/.monita-cs80/… →
/// ~/.cs80_panel/…（旧链路 host 的目录，以前在那儿编译过的不用再挪）。tools/lyrics/lyrics.py 用同一顺序
public func mraCandidates(env: [String: String], home: String) -> [String] {
    if let d = env["MONITA_MRA_DIR"], !d.isEmpty { return [d] }
    var out: [String] = []
    if let h = env["MONITA_HOME"], !h.isEmpty { out.append(h + "/mediaremote-adapter") }
    for d in [home + "/.monita-cs80/mediaremote-adapter", home + "/.cs80_panel/mediaremote-adapter"] where !out.contains(d) {
        out.append(d)
    }
    return out
}

/// 第一个装好了的；都没装返回第一个（日志里报它）
public func pickMraDir(_ candidates: [String], installed: (String) -> Bool) -> String? {
    candidates.first(where: installed) ?? candidates.first
}
