// 段码屏展示帧（背面的"数码管"，给别人看的）：播放状态 → 72 字节显存，主机每 ≤2 s 发一次 SEG。
//   播放中：主区 = 进度 mm:ss（≥1 h 为 h:mm:ss），小时钟 = 总时长，喇叭 + 声波跳动动画
//   暂停：  同上，但主区 1 Hz 闪烁、声波全灭（只留喇叭）
//   没在播：主区 = 当前时间 HH:MM（冒号按秒闪），小时钟 = 日期 MM-DD，喇叭/声波灭
//   所有模式：电池框 + 四格 = Mac 电量（25% 一格四舍五入，>0% 至少 1 格），闪电 = 接交流电；没有内置电池整块不亮
// 电池四格 A(0x2c)..D(0x2f) 从左（正极）到右（负极）；点亮从负极端开始：1 格 = D，2 格 = C+D，…
// 映射见 docs/segmap.md / fw/fg00/segmap.h。

/// 单段：显存字节号 + 位掩码（与 segmap.h 的 FG_SEG_* 一致）
public struct SegBit: Equatable {
    public let byte: Int
    public let mask: UInt8
    init(_ byte: Int, _ mask: UInt8) { self.byte = byte; self.mask = mask }
    public static let mainColon2 = SegBit(55, 0x08), mainColon4 = SegBit(57, 0x08)
    public static let clkColonA = SegBit(63, 0x08), clkColonB = SegBit(65, 0x08)
    public static let speaker = SegBit(43, 0x08)
    public static let vol1 = SegBit(61, 0x08), vol2 = SegBit(61, 0x02), vol3 = SegBit(43, 0x02)
    public static let vol4 = SegBit(7, 0x02), vol5 = SegBit(25, 0x02)
    public static let batFrame = SegBit(70, 0x80), charge = SegBit(52, 0x80)
    public static let batA = SegBit(70, 0x40), batB = SegBit(16, 0x40), batC = SegBit(16, 0x80), batD = SegBit(34, 0x80)
    public static let volumes = [vol1, vol2, vol3, vol4, vol5]
    /// 点亮顺序：从负极端（最右的 D）开始
    public static let cells = [batD, batC, batB, batA]
}

/// 本地时间（调用方用 Calendar 取，纯逻辑这边不碰时区）
public struct SegClock: Equatable {
    public var hour, minute, month, day: Int
    public init(hour: Int, minute: Int, month: Int, day: Int) {
        self.hour = hour; self.minute = minute; self.month = month; self.day = day
    }
}

/// Mac 内置电池（IOPSCopyPowerSourcesInfo）；台式机为 nil
public struct SegBattery: Equatable {
    public var percent: Int
    public var onAC: Bool
    public init(percent: Int, onAC: Bool) { self.percent = percent; self.onAC = onAC }
}

public struct SegInput: Equatable {
    /// nil = 没在播
    public var play: PlayState?
    public var nowMs: Double
    public var clock: SegClock
    public var battery: SegBattery?
    public init(play: PlayState?, nowMs: Double, clock: SegClock, battery: SegBattery?) {
        self.play = play; self.nowMs = nowMs; self.clock = clock; self.battery = battery
    }
}

/// 进度文字：mm:ss，满 1 小时 h:mm:ss（分钟补零，冒号才落得在玻璃上的冒号位）
public func segClockText(_ s: Double) -> String {
    let t = s.isFinite ? max(0, Int(s.rounded(.down))) : 0
    let h = t / 3600, m = (t % 3600) / 60, ss = t % 60
    func two(_ n: Int) -> String { n < 10 ? "0\(n)" : "\(n)" }
    return h > 0 ? "\(h):\(two(m)):\(two(ss))" : "\(two(m)):\(two(ss))"
}

/// 电池格数 0..4：25% 一格四舍五入，>0% 至少 1 格
public func segBatteryCells(_ percent: Int) -> Int {
    guard percent > 0 else { return 0 }
    return min(4, max(1, Int((Double(percent) / 25).rounded())))
}

/// 读回第 k 列的 7 段掩码（a..g = bit0..6），预览和测试用
public func segDigitAt(_ fb: [UInt8], _ k: Int) -> UInt8 {
    var m: UInt8 = 0
    if fb[k] & 0x80 != 0 { m |= 0x20 }
    if fb[k] & 0x08 != 0 { m |= 0x40 }
    if fb[18 + k] & 0x80 != 0 { m |= 0x01 }
    if fb[18 + k] & 0x08 != 0 { m |= 0x02 }
    if fb[36 + k] & 0x80 != 0 { m |= 0x10 }
    if fb[36 + k] & 0x08 != 0 { m |= 0x04 }
    if fb[54 + k] & 0x80 != 0 { m |= 0x08 }
    return m
}

private let seg7: [UInt8] = [0x3F, 0x06, 0x5B, 0x4F, 0x66, 0x6D, 0x7D, 0x07, 0x7F, 0x6F]
/// 有冒号的列（冒号在该列之后，槽位 fb[54+k] bit3）：主区第 2/4 位、小时钟第 2/4 位
private let colonCols: Set<Int> = [1, 3, 9, 11]

/// 在 cols 这几列里右对齐写 text（数字、'-'、' '）；':' 点亮前一位之后的冒号
private func put(_ fb: inout [UInt8], _ cols: Range<Int>, _ text: String) {
    var masks: [UInt8] = [], colonAfter: [Int] = []
    for ch in text {
        if ch == ":" { colonAfter.append(masks.count - 1); continue }
        masks.append(ch.wholeNumberValue.map { seg7[$0] } ?? (ch == "-" ? 0x40 : 0))
    }
    let base = cols.upperBound - masks.count
    for (i, m) in masks.enumerated() where cols.contains(base + i) { segDigit(&fb, base + i, m) }
    for i in colonAfter where colonCols.contains(base + i) && cols.contains(base + i) { fb[54 + base + i] |= 0x08 }
}

private func set(_ fb: inout [UInt8], _ s: SegBit) { fb[s.byte] |= s.mask }

/// 播放中声波的"跳动"序列（拿不到音量，做个节奏感动画），每 250 ms 一步
private let waveSeq = [2, 4, 5, 3, 5, 4, 2, 3]

public func segFrame(_ i: SegInput) -> [UInt8] {
    var fb = [UInt8](repeating: 0, count: 72)
    if let b = i.battery {
        set(&fb, .batFrame)
        for s in SegBit.cells.prefix(segBatteryCells(b.percent)) { set(&fb, s) }
        if b.onAC { set(&fb, .charge) }
    }
    let half = Int((i.nowMs / 500).rounded(.down)) % 2 == 0   // 1 Hz 闪烁的亮半周
    guard let p = i.play else {
        let c = i.clock
        func two(_ n: Int) -> String { n < 10 ? "0\(n)" : "\(n)" }
        put(&fb, 0..<6, two(c.hour) + (half ? ":" : "") + two(c.minute))
        put(&fb, 8..<14, two(c.month) + "-" + two(c.day))
        return fb
    }
    let playing = p.state == "playing"
    let pos = positionAt(p, nowMs: i.nowMs)
    if playing || half { put(&fb, 0..<6, segClockText(pos)) }
    if let d = p.duration, d > 0 { put(&fb, 8..<14, segClockText(d)) }
    set(&fb, .speaker)
    if playing {
        let n = waveSeq[Int((i.nowMs / 250).rounded(.down)) % waveSeq.count]
        for s in SegBit.volumes.prefix(n) { set(&fb, s) }
    }
    return fb
}
