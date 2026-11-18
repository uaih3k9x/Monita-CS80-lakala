// 歌词滚动：easing、切句动画状态、COPY 规划（设备端搬块 + 只发新露出的条带）
import Testing
import MonitaCore

@Test func easeOutCubicShape() {
    #expect(easeOutCubic(0) == 0 && easeOutCubic(1) == 1)
    #expect(easeOutCubic(0.5) == 0.875)
    #expect(easeOutCubic(-1) == 0 && easeOutCubic(2) == 1)
    #expect(easeOutCubic(0.3) < easeOutCubic(0.31))
}

@Test func scrollAnimStartsOnNextLineOnly() {
    var a = ScrollAnim(durMs: 400)
    a.update(line: 3, song: "s", nowMs: 0, distance: 120)        // 第一次见：不动
    #expect(a.offset(nowMs: 0) == 0 && !a.active(nowMs: 0))
    a.update(line: 4, song: "s", nowMs: 1000, distance: 120)     // 下一句：从 +120 滑到 0
    #expect(a.offset(nowMs: 1000) == 120 && a.active(nowMs: 1000))
    #expect(a.offset(nowMs: 1200) == 15)                         // 120 × (1 − 0.875)
    #expect(a.offset(nowMs: 1400) == 0 && !a.active(nowMs: 1400))
    a.update(line: 6, song: "s", nowMs: 2000, distance: 120)     // 跳句（拖进度）：直接到位
    #expect(a.offset(nowMs: 2000) == 0)
    a.update(line: 7, song: "t", nowMs: 3000, distance: 120)     // 换歌：不动
    #expect(a.offset(nowMs: 3000) == 0)
}

@Test func scrollAnimChainsMidway() {
    var a = ScrollAnim(durMs: 400)
    a.update(line: 1, song: "s", nowMs: 0, distance: 100)
    a.update(line: 2, song: "s", nowMs: 0, distance: 100)
    a.update(line: 2, song: "s", nowMs: 200, distance: 100)      // 同一句重复调用不重启
    #expect(a.offset(nowMs: 200) == 13)
    a.update(line: 3, song: "s", nowMs: 200, distance: 100)      // 半路又切句：从当前位置接着走
    #expect(a.offset(nowMs: 200) == 113)
}

private func pattern(_ seed: Int) -> [UInt16] {
    var b = [UInt16](repeating: 0x1234, count: screenW * screenH)
    for y in 0..<screenH { for x in 0..<screenW { b[y * screenW + x] = UInt16(truncatingIfNeeded: (y &* 31 &+ x &* 7 &+ seed) & 0xFFFF) } }
    return b
}

@Test func copyRectsForScroll() {
    let r = LinkRect(x: 40, y: 400, w: 720, h: 500)
    #expect(scrollCopy(region: r, dy: -10) == LinkCopy(sx: 40, sy: 410, w: 720, h: 490, dx: 40, dy: 400))
    #expect(scrollCopy(region: r, dy: 12) == LinkCopy(sx: 40, sy: 400, w: 720, h: 488, dx: 40, dy: 412))
    #expect(scrollCopy(region: r, dy: 0) == nil)
    #expect(scrollCopy(region: r, dy: -500) == nil)
}

@Test func applyCopyIsMemmove() {
    var b = pattern(0)
    let orig = b
    applyCopy(&b, LinkCopy(sx: 0, sy: 10, w: 800, h: 100, dx: 0, dy: 5))   // 往下 5 行，重叠
    #expect(b[5 * screenW + 3] == orig[10 * screenW + 3])
    #expect(b[104 * screenW + 799] == orig[109 * screenW + 799])
    #expect(b[4 * screenW] == orig[4 * screenW] && b[105 * screenW] == orig[105 * screenW])
}

@Test func planScrollSendsOnlyExposedStrip() {
    let region = LinkRect(x: 32, y: 320, w: 736, h: 480)
    let prev = pattern(0)
    // 下一帧：区域内内容上移 10 行，底部露出 10 行新内容；区域外不变
    var next = prev
    for y in region.y..<(region.y + region.h) {
        for x in region.x..<(region.x + region.w) {
            next[y * screenW + x] = y + 10 < region.y + region.h ? prev[(y + 10) * screenW + x] : 0xF800
        }
    }
    let p = planScroll(prev: prev, next: next, region: region, dy: -10)
    #expect(p.copy == scrollCopy(region: region, dy: -10))
    #expect(!p.dirty.isEmpty && p.dirty.allSatisfy { $0.y >= 784 && $0.y < 800 })   // 只剩底部那一排 tile
    // 设备侧：先 COPY 再写脏 tile，结果等于 next
    var dev = prev
    applyCopy(&dev, p.copy!)
    for t in p.dirty { for y in t.y..<(t.y + tileH) { for x in t.x..<(t.x + tileW) { dev[y * screenW + x] = next[y * screenW + x] } } }
    #expect(dev == next)
    // 没有可用的平移：退回普通差分
    let q = planScroll(prev: prev, next: pattern(1), region: region, dy: -10)
    #expect(q.copy == nil && q.dirty == diffTiles(prev, pattern(1)))
}

@Test func batchPutsCopiesFirst() {
    var seq: UInt8 = 0
    let buf = [UInt16](repeating: 0, count: screenW * screenH)
    let c = LinkCopy(sx: 0, sy: 16, w: 800, h: 100, dx: 0, dy: 0)
    let (bytes, s) = encodeBatch([LinkRect(x: 0, y: 0, w: 32, h: 16)], buf, copies: [c], frameID: 1, seq: &seq)
    var p = FrameParser()
    let fr = p.feed(bytes)
    #expect(fr.map(\.type) == [LinkType.copy, LinkType.rect, LinkType.present])
    #expect(fr[0].payload == [0, 0, 16, 0, 0x20, 3, 100, 0, 0, 0, 0, 0])
    #expect(s == 3)
}
