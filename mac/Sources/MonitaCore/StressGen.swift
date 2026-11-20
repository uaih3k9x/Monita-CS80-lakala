// --stress 的负载生成（monita-cs80 Stress.swift 用；单测拿同一个生成器、同一个种子看每批线上长度，见 UsbPadTests）。
// 模仿日常歌词页：小块差分 + COPY 上移 8 行 + 底部条带 + 零碎小块 + 偶尔整帧。

public struct XorShift {
    public var s: UInt32
    public init(s: UInt32) { self.s = s }
    public mutating func next() -> UInt32 { s ^= s << 13; s ^= s >> 17; s ^= s << 5; return s }
}

/// 像"文字"的块：大部分是背景色，夹杂随机笔画，LZ4 能压但压不到测试图那么狠
public func paintText(_ fb: inout [UInt16], _ r: LinkRect, _ rng: inout XorShift) {
    let bg: UInt16 = 0xC618, ink: UInt16 = UInt16(truncatingIfNeeded: rng.next()) | 0x0821
    for y in r.y..<(r.y + r.h) {
        for x in r.x..<(r.x + r.w) {
            fb[y * screenW + x] = (rng.next() & 7) == 0 ? ink : bg
        }
    }
}

public struct StressGen {
    public var rng: XorShift
    /// 上位机眼里的整屏（每批画完之后）
    public var fb: [UInt16]
    public let region = LinkRect(x: 43, y: 337, w: 750, h: 514)

    public init(seed: UInt32 = 0x9E3779B9) {
        rng = XorShift(s: seed)
        fb = [UInt16](repeating: 0x8410, count: screenW * screenH)
        paintText(&fb, LinkRect(x: 0, y: 0, w: screenW, h: screenH), &rng)
    }

    /// 这一批是哪类：< 3 整帧，< 55 滚动，其余差分（SEG 那一拍也照样消耗一个）
    public mutating func roll() -> Int { Int(rng.next() % 100) }

    public mutating func full() -> [LinkRect] {
        paintText(&fb, LinkRect(x: 0, y: 0, w: screenW, h: screenH), &rng)
        return fullRects()
    }

    /// COPY 上移 8 行 + 底部 750×8 条带 + 0–5 个小块（像高亮行变色）
    public mutating func scroll() -> ([LinkRect], LinkCopy) {
        let c = scrollCopy(region: region, dy: -8)!
        applyCopy(&fb, c)
        let strip = LinkRect(x: region.x, y: region.y + region.h - 8, w: region.w, h: 8)
        paintText(&fb, strip, &rng)
        var rs = [strip]
        for _ in 0..<Int(rng.next() % 6) {
            let r = LinkRect(x: Int(rng.next() % 50) * 16, y: Int(rng.next() % 80) * 16, w: 16 * (1 + Int(rng.next() % 8)), h: 16)
            let rr = LinkRect(x: r.x, y: r.y, w: min(r.w, screenW - r.x), h: r.h)
            paintText(&fb, rr, &rng); rs.append(rr)
        }
        return (rs, c)
    }

    /// 1–40 个 16 倍数的小块
    public mutating func diff() -> [LinkRect] {
        var rs: [LinkRect] = []
        for _ in 0..<(1 + Int(rng.next() % 40)) {
            let x = Int(rng.next() % 50) * 16, y = Int(rng.next() % 80) * 16
            let r = LinkRect(x: x, y: y, w: min(16 * (1 + Int(rng.next() % 10)), screenW - x), h: 16 * (1 + Int(rng.next() % 4)))
            let rr = LinkRect(x: r.x, y: r.y, w: r.w, h: min(r.h, screenH - r.y))
            paintText(&fb, rr, &rng); rs.append(rr)
        }
        return rs
    }
}
