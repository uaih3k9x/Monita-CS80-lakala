// 沉浸模式（fw v9，IMM_*，docs/protocol.md「沉浸模式」）的纯逻辑：消息编码、歌词块布局、居中目标、淡出档位、切句规划。
// 画块、推送在 monita-cs80/Immersive.swift。
//
// 布局：逻辑行坐标里从上到下排一列"块"：头部（歌名，半屏高）→ 每句一块 → 尾部（半屏留白），
// 这样第一句和最后一句也能滚到屏幕正中。当前句的块中心对准屏幕中心；离当前句越远颜色越淡（immClass）。
// 只往下滚：往回（seek）、换歌、一次跳太远 → 重新开始（IMM_ENTER + 整屏重写）。

extension LinkType {
    public static let immEnter: UInt8 = 0x60, immExit: UInt8 = 0x61, immRect: UInt8 = 0x62, immScroll: UInt8 = 0x63
}

extension HelloInfo {
    public var hasImmersive: Bool { caps & 0x40 != 0 }
}

public func immEnterPayload(origin: Int) -> [UInt8] {
    var out: [UInt8] = []
    put32(&out, UInt32(truncatingIfNeeded: origin))
    return out
}

public func immScrollPayload(y: Int, durMs: Int) -> [UInt8] {
    var out: [UInt8] = []
    put32(&out, UInt32(truncatingIfNeeded: y)); put16(&out, max(0, min(durMs, 65535)))
    return out
}

/// IMM_RECT：把 buf（宽 width 的 RGB565 图）里第 rows 行、x..<x+w 列写到逻辑行 y 起
public func immRectPayload(y: Int, x: Int, w: Int, rows: Range<Int>, visible: Bool, _ buf: [UInt16], width: Int) -> [UInt8] {
    var out: [UInt8] = []
    out.reserveCapacity(12 + w * rows.count * 2)
    put32(&out, UInt32(truncatingIfNeeded: y)); put16(&out, x); put16(&out, w); put16(&out, rows.count)
    out.append(visible ? 1 : 0); out.append(0)
    buf.withUnsafeBufferPointer { p in
        for r in rows {
            let base = r * width + x
            for k in 0..<w { let v = p[base + k]; out.append(UInt8(v & 0xFF)); out.append(UInt8(v >> 8)) }
        }
    }
    return out
}

/// 一段行拆成单条 IMM_RECT 放得下的条带（整行宽 800 时 20 行一条）
public func immStrips(rows: Range<Int>, width: Int, maxPayload: Int = linkMaxPayload) -> [Range<Int>] {
    let n = max(1, (maxPayload - 12) / (width * 2))
    return stride(from: rows.lowerBound, to: rows.upperBound, by: n).map { $0..<min($0 + n, rows.upperBound) }
}

/// buf（宽 width）里第 rows 行整行宽写到逻辑行 y + 行号，按 maxPayload（设备的 max_payload，见 HelloInfo.rectPayloadMax）切成 IMM_RECT
public func immRectPayloads(y: Int, rows: Range<Int>, visible: Bool, _ buf: [UInt16], width: Int, maxPayload: Int) -> [[UInt8]] {
    immStrips(rows: rows, width: width, maxPayload: maxPayload).map {
        immRectPayload(y: y + $0.lowerBound, x: 0, w: width, rows: $0, visible: visible, buf, width: width)
    }
}

// MARK: 布局

public struct ImmBlock: Equatable {
    public enum Kind: Equatable { case header, line(Int), tail }
    public var kind: Kind, y: Int, h: Int
    public init(kind: Kind, y: Int, h: Int) { self.kind = kind; self.y = y; self.h = h }
    public var rows: Range<Int> { y..<(y + h) }
}

public func immLayout(headerH: Int, lineHeights: [Int], tailH: Int) -> [ImmBlock] {
    var out = [ImmBlock(kind: .header, y: 0, h: headerH)]
    var y = headerH
    for (i, h) in lineHeights.enumerated() { out.append(ImmBlock(kind: .line(i), y: y, h: h)); y += h }
    out.append(ImmBlock(kind: .tail, y: y, h: tailH))
    return out
}

/// 当前句（-1 = 前奏）对应的窗口顶：块中心在屏幕正中；前奏 / 没有句子时 0
public func immTarget(_ blocks: [ImmBlock], line: Int, screenH: Int) -> Int {
    let lines = blocks.filter { if case .line = $0.kind { return true } else { return false } }
    guard line >= 0, !lines.isEmpty else { return 0 }
    let b = lines[min(line, lines.count - 1)]
    return max(0, b.y + b.h / 2 - screenH / 2)
}

/// 淡出档位：0 当前句（高亮条），1 相邻，2 隔一句，3 更远
public func immClass(_ distance: Int) -> Int { min(abs(distance), 3) }

// MARK: 规划

public struct ImmWrite: Equatable {
    public var block: Int, cls: Int, rows: Range<Int>, visible: Bool
    public init(block: Int, cls: Int, rows: Range<Int>, visible: Bool) {
        self.block = block; self.cls = cls; self.rows = rows; self.visible = visible
    }
}

public struct ImmScroll: Equatable {
    public var y: Int, durMs: Int
    public init(y: Int, durMs: Int) { self.y = y; self.durMs = durMs }
}

/// 一次切句要做的事，按顺序：reset（非 nil = 先 IMM_ENTER 这个起点）→ before → scroll → 等动画 → after
public struct ImmPlan: Equatable {
    public var reset: Int?
    public var before: [ImmWrite] = []
    public var scroll: ImmScroll?
    public var after: [ImmWrite] = []
}

private func clip(_ a: Range<Int>, _ b: Range<Int>) -> Range<Int> {
    let lo = max(a.lowerBound, b.lowerBound), hi = min(a.upperBound, b.upperBound)
    return lo < hi ? lo..<hi : lo..<lo
}

/// 记着设备卷轴里每块画成了哪一档、画了哪些行，算出下一句要写什么。
/// plan() 假定会被照做；推送出错时调 invalidate()，下一次整屏重来。
public struct ImmPlanner {
    public let rows: Int, screenH: Int
    public private(set) var y: Int?
    private var key = ""
    private var rendered: [Int: (cls: Int, rows: Range<Int>)] = [:]

    /// rows = 设备卷轴行数（IMM_ENTER 的 ACK extra）
    public init(rows: Int, screenH: Int) { self.rows = rows; self.screenH = screenH }

    public mutating func invalidate() { y = nil; rendered = [:] }

    private func desired(_ b: ImmBlock, _ line: Int) -> Int {
        if case .line(let i) = b.kind { return immClass(i - line) }
        return 0
    }

    public mutating func plan(_ blocks: [ImmBlock], line: Int, key k: String, durMs: Int) -> ImmPlan {
        let t = immTarget(blocks, line: line, screenH: screenH)
        let H = screenH
        var p = ImmPlan()
        guard let y, k == key, t >= y, t - y <= rows - H else {
            // 重新开始：起点 = 新窗口顶，窗口里的块全写（屏上留着旧画面，卷轴不可见，全是隐藏写）
            key = k; rendered = [:]; self.y = t
            p.reset = t
            let win = t..<(t + H)
            for (i, b) in blocks.enumerated() {
                let r = clip(b.rows, win)
                if r.isEmpty { continue }
                let c = desired(b, line)
                p.before.append(ImmWrite(block: i, cls: c, rows: r, visible: false))
                rendered[i] = (c, r)
            }
            p.scroll = ImmScroll(y: t, durMs: 0)
            return p
        }
        let cur = y..<(y + H), fut = t..<(t + H), span = y..<(t + H)
        for (i, b) in blocks.enumerated() {
            let need = clip(b.rows, span)
            if need.isEmpty || clip(b.rows, fut).isEmpty { continue }      // 会滚出去的不管
            let c = desired(b, line)
            if let r = rendered[i], r.cls == c {
                // 同一档：只补还没画过的行（只往下滚，已画的是一段连续的、从上往下长）
                let miss = clip(need, max(r.rows.upperBound, need.lowerBound)..<need.upperBound)
                if miss.isEmpty { continue }
                p.before.append(ImmWrite(block: i, cls: c, rows: miss, visible: !clip(miss, cur).isEmpty))
                rendered[i] = (c, min(r.rows.lowerBound, miss.lowerBound)..<miss.upperBound)
                continue
            }
            let old = rendered[i]?.cls
            let revealed = !clip(b.rows, (y + H)..<(t + H)).isEmpty
            if revealed || c == 0 || old == 0 || old == nil {
                // 新露出来的 / 高亮换行：滚动之前写
                p.before.append(ImmWrite(block: i, cls: c, rows: need, visible: !clip(need, cur).isEmpty))
                rendered[i] = (c, need)
            } else {
                // 其余变了档的：滚完再改
                let r = clip(b.rows, fut)
                p.after.append(ImmWrite(block: i, cls: c, rows: r, visible: true))
                rendered[i] = (c, r)
            }
        }
        if t != y { p.scroll = ImmScroll(y: t, durMs: durMs) }
        self.y = t
        return p
    }
}
