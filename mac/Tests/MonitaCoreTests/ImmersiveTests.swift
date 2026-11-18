// 沉浸模式（fw v9，IMM_*）：消息编码、块布局、居中目标、淡出档位、切句规划（docs/protocol.md「沉浸模式」）
import Testing
import MonitaCore

@Test func immersiveModePriority() {
    #expect(screenMode(locked: false, saverPlaying: false, page: .clock, immersive: true) == .immersive)
    #expect(screenMode(locked: true, saverPlaying: false, immersive: true) == .lockWallpaper)   // 锁屏优先：退出沉浸
    #expect(screenMode(locked: false, saverPlaying: true, immersive: true) == .yieldToSaver)
}

@Test func immPayloads() {
    #expect(immEnterPayload(origin: 0x0102_0304) == [4, 3, 2, 1])
    #expect(immScrollPayload(y: 0x0A0B_0C0D, durMs: 360) == [0x0D, 0x0C, 0x0B, 0x0A, 0x68, 0x01])
    let buf: [UInt16] = (0..<12).map { UInt16($0) | 0xAB00 }         // 4 × 3 的小图
    let p = immRectPayload(y: 7, x: 1, w: 2, rows: 1..<3, visible: true, buf, width: 4)
    #expect(Array(p[0..<12]) == [7, 0, 0, 0, 1, 0, 2, 0, 2, 0, 1, 0])
    #expect(Array(p[12...]) == [5, 0xAB, 6, 0xAB, 9, 0xAB, 10, 0xAB])    // 第 1、2 行的 x = 1, 2
    #expect(immRectPayload(y: 0, x: 0, w: 4, rows: 0..<1, visible: false, buf, width: 4)[10] == 0)
    var h = [UInt8](repeating: 0, count: 16); h[10] = 0x40
    #expect(HelloInfo(h)!.hasImmersive)
}

@Test func immStripsFitPayload() {
    // 整行宽 800：一条 ≤ 20 行（12 + 800×20×2 = 32012 ≤ 32768）
    let s = immStrips(rows: 5..<50, width: 800)
    #expect(s == [5..<25, 25..<45, 45..<50])
    #expect(immStrips(rows: 3..<3, width: 800).isEmpty)
}

@Test func immLayoutStacksBlocks() {
    let b = immLayout(headerH: 640, lineHeights: [100, 150], tailH: 640)
    #expect(b == [ImmBlock(kind: .header, y: 0, h: 640), ImmBlock(kind: .line(0), y: 640, h: 100),
                  ImmBlock(kind: .line(1), y: 740, h: 150), ImmBlock(kind: .tail, y: 890, h: 640)])
}

@Test func immTargetCentersLine() {
    let b = immLayout(headerH: 640, lineHeights: [100, 150, 120], tailH: 640)
    #expect(immTarget(b, line: -1, screenH: 1280) == 0)                 // 前奏：窗口在最上面（头部在上半屏）
    #expect(immTarget(b, line: 0, screenH: 1280) == 640 + 50 - 640)     // 第 0 句中心在屏幕正中
    #expect(immTarget(b, line: 1, screenH: 1280) == 740 + 75 - 640)
    #expect(immTarget(b, line: 9, screenH: 1280) == immTarget(b, line: 2, screenH: 1280))   // 越界按最后一句
}

@Test func immClassFades() {
    #expect([0, 1, -1, 2, -2, 3, -7, 40].map(immClass) == [0, 1, 1, 2, 2, 3, 3, 3])
}

private let H = 1280
private func blocks(_ n: Int, h: Int = 120) -> [ImmBlock] { immLayout(headerH: H / 2, lineHeights: Array(repeating: h, count: n), tailH: H / 2) }

@Test func immPlanFirstIsReset() {
    var p = ImmPlanner(rows: 2700, screenH: H)
    let b = blocks(30)
    let plan = p.plan(b, line: 0, key: "song", durMs: 360)
    let t = immTarget(b, line: 0, screenH: H)
    #expect(plan.reset == t)
    #expect(plan.scroll == ImmScroll(y: t, durMs: 0))                   // 重新开始：直接跳
    #expect(plan.after.isEmpty)
    // 窗口里的每一行都被写到，没有一行在窗口顶之上，全是隐藏写
    var covered = Set<Int>()
    for w in plan.before { #expect(!w.visible); #expect(w.rows.lowerBound >= t); covered.formUnion(w.rows) }
    #expect(Set(t..<(t + H)).isSubset(of: covered))
    #expect(p.y == t)
}

@Test func immPlanNextLineScrollsAndRecolors() {
    var p = ImmPlanner(rows: 2700, screenH: H)
    let b = blocks(30)
    _ = p.plan(b, line: 3, key: "s", durMs: 360)
    let y0 = p.y!
    let plan = p.plan(b, line: 4, key: "s", durMs: 360)
    let t = immTarget(b, line: 4, screenH: H)
    #expect(plan.reset == nil)
    #expect(plan.scroll == ImmScroll(y: t, durMs: 360))
    // 新露出来的 [y0+H, t+H) 都在滚动之前写好
    var before = Set<Int>()
    for w in plan.before { before.formUnion(w.rows) }
    #expect(Set((y0 + H)..<(t + H)).isSubset(of: before))
    // 高亮换行（旧当前句 3 → 1 档，新当前句 4 → 0 档）在滚动之前；都在屏上，带可见位
    let hl = plan.before.filter { if case .line(let i) = b[$0.block].kind { return i == 3 || i == 4 } else { return false } }
    #expect(hl.count == 2 && hl.allSatisfy(\.visible))
    #expect(hl.contains { $0.cls == 0 } && hl.contains { $0.cls == 1 })
    // 其余变了档的（2 → 1 档的第 5 句、1 → 2 档的第 2 句……）滚完再改；同一块不重复写
    #expect(!plan.after.isEmpty && plan.after.allSatisfy(\.visible))
    let all = (plan.before + plan.after).map(\.block)
    #expect(Set(all).count == all.count)
    // 再规划同一句：什么都不用做
    let again = p.plan(b, line: 4, key: "s", durMs: 360)
    #expect(again.reset == nil && again.before.isEmpty && again.after.isEmpty && again.scroll == nil)
}

@Test func immPlanNeverWritesAboveWindow() {
    var p = ImmPlanner(rows: 2700, screenH: H)
    let b = blocks(40, h: 90)
    _ = p.plan(b, line: 0, key: "s", durMs: 360)
    for i in 1..<40 {
        let y0 = p.y!
        let plan = p.plan(b, line: i, key: "s", durMs: 360)
        let t = p.y!
        for w in plan.before { #expect(w.rows.lowerBound >= y0); #expect(w.rows.upperBound <= y0 + 2700) }
        for w in plan.after { #expect(w.rows.lowerBound >= t) }
    }
}

@Test func immPlanResetsOnSeekBackSongChangeOrBigJump() {
    var p = ImmPlanner(rows: 2700, screenH: H)
    let b = blocks(60)
    _ = p.plan(b, line: 10, key: "s", durMs: 360)
    #expect(p.plan(b, line: 9, key: "s", durMs: 360).reset != nil)       // 往回
    #expect(p.plan(b, line: 9, key: "t", durMs: 360).reset != nil)       // 换歌
    #expect(p.plan(b, line: 40, key: "t", durMs: 360).reset != nil)      // 一次跳太远（> R - H - 余量）
    #expect(p.plan(b, line: 41, key: "t", durMs: 360).reset == nil)
    p.invalidate()                                                       // 推送出错：下次整屏重来
    #expect(p.plan(b, line: 41, key: "t", durMs: 360).reset != nil)
}

/// 对照模型：按规划照做之后，新窗口里每一行都画着"它所在的块、该有的档位"；不在动画里时屏上也一直对
@Test func immPlanKeepsWindowCorrect() {
    var rng = SystemRandomNumberGenerator()
    let heights = (0..<80).map { _ in Int.random(in: 70...330, using: &rng) }
    let b = immLayout(headerH: H / 2, lineHeights: heights, tailH: H / 2)
    var owner: [Int] = []
    for (i, x) in b.enumerated() { owner += Array(repeating: i, count: x.h) }
    func blockAt(_ row: Int) -> Int { owner[row] }
    func want(_ bi: Int, _ line: Int) -> Int { if case .line(let i) = b[bi].kind { return immClass(i - line) }; return 0 }
    var p = ImmPlanner(rows: 2700, screenH: H)
    var dev = [(Int, Int)?](repeating: nil, count: owner.count)   // 逻辑行 → (块, 档)
    var line = -1
    for _ in 0..<300 {
        let r = Int.random(in: 0..<20, using: &rng)
        line = r == 0 ? max(-1, line - 3) : r == 1 ? line + 12 : min(heights.count - 1, line + (r < 15 ? 1 : 2))
        if line >= heights.count - 1 { line = -1 }
        let plan = p.plan(b, line: line, key: "s", durMs: 360)
        if plan.reset != nil { dev = [(Int, Int)?](repeating: nil, count: owner.count) }
        for w in plan.before + plan.after {
            #expect(w.rows.allSatisfy { blockAt($0) == w.block })
            for row in w.rows { dev[row] = (w.block, w.cls) }
        }
        let t = p.y!
        for row in t..<(t + H) where row < b.last!.rows.upperBound {
            let d = dev[row]
            #expect(d != nil && d!.0 == blockAt(row) && d!.1 == want(blockAt(row), line), "row \(row) line \(line)")
            if d == nil || d!.0 != blockAt(row) || d!.1 != want(blockAt(row), line) { return }
        }
    }
}
