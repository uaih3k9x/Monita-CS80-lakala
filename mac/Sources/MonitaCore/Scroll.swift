// 歌词滚动动画的纯逻辑：easing、切句动画状态、COPY 规划。
// 做法：切到下一句时，新布局先整体往下偏 distance 像素，再按 easeOutCubic 在 durMs 内滑回 0。
// 相邻两帧之间歌词区里的内容只是整数像素的平移，所以让设备 COPY（搬块）一下，
// 再只发新露出的条带 + 其它真的变了的 tile。正确性不依赖平移假设：脏块是拿"COPY 之后的预测画面"和新画面比出来的。

public func easeOutCubic(_ t: Double) -> Double {
    let c = min(max(t, 0), 1)
    let u = 1 - c
    return 1 - u * u * u
}

public struct ScrollAnim {
    public var durMs: Double
    var line: Int?
    var song = ""
    var from = 0.0
    var startMs = 0.0
    public init(durMs: Double = 450) { self.durMs = durMs }

    func exact(_ nowMs: Double) -> Double {
        from == 0 ? 0 : from * (1 - easeOutCubic((nowMs - startMs) / durMs))
    }

    /// 每次画歌词区调用。distance：前进一句时，新布局里当前句相对它在旧布局里位置的上移量（像素）
    public mutating func update(line l: Int, song s: String, nowMs: Double, distance: Double) {
        defer { line = l; song = s }
        guard let old = line, s == song else { from = 0; return }
        if l == old { return }
        if l == old + 1 {
            from = exact(nowMs) + distance
            startMs = nowMs
        } else {
            from = 0
        }
    }

    /// 内容相对最终位置往下偏多少像素（整数：整像素平移才能和 COPY 逐像素对上）
    public func offset(nowMs: Double) -> Int { Int(exact(nowMs).rounded()) }
    public func active(nowMs: Double) -> Bool { from != 0 && nowMs - startMs < durMs }
}

/// 设备端 COPY：(sx,sy,w,h) 搬到 (dx,dy)，memmove 语义
public struct LinkCopy: Equatable {
    public var sx, sy, w, h, dx, dy: Int
    public init(sx: Int, sy: Int, w: Int, h: Int, dx: Int, dy: Int) {
        self.sx = sx; self.sy = sy; self.w = w; self.h = h; self.dx = dx; self.dy = dy
    }
}

/// 区域内内容整体平移 dy（负 = 上移）对应的 COPY；平移量为 0 或超出区域时 nil
public func scrollCopy(region r: LinkRect, dy: Int) -> LinkCopy? {
    if dy == 0 || abs(dy) >= r.h { return nil }
    return dy < 0 ? LinkCopy(sx: r.x, sy: r.y - dy, w: r.w, h: r.h + dy, dx: r.x, dy: r.y)
                  : LinkCopy(sx: r.x, sy: r.y, w: r.w, h: r.h - dy, dx: r.x, dy: r.y + dy)
}

/// 主机侧镜像设备的 COPY（和固件 lk_copy_apply 同语义）
public func applyCopy(_ buf: inout [UInt16], _ c: LinkCopy, width: Int = screenW) {
    buf.withUnsafeMutableBufferPointer { p in
        let rows = c.dy > c.sy ? Array((0..<c.h).reversed()) : Array(0..<c.h)
        for r in rows {
            let s = (c.sy + r) * width + c.sx, d = (c.dy + r) * width + c.dx
            (p.baseAddress! + d).update(from: p.baseAddress! + s, count: c.w)   // 行内重叠由 memmove 语义处理
        }
    }
}

public struct ScrollPlan: Equatable {
    public var copy: LinkCopy?
    public var dirty: [Tile]
    public init(copy: LinkCopy?, dirty: [Tile]) { self.copy = copy; self.dirty = dirty }
}

/// 上一帧 → 下一帧：试一下"区域平移 dy"的 COPY，能让脏块变少才用
public func planScroll(prev: [UInt16], next: [UInt16], region: LinkRect, dy: Int) -> ScrollPlan {
    let plain = diffTiles(prev, next)
    guard let c = scrollCopy(region: region, dy: dy) else { return ScrollPlan(copy: nil, dirty: plain) }
    var pred = prev
    applyCopy(&pred, c)
    let d = diffTiles(pred, next)
    return d.count < plain.count ? ScrollPlan(copy: c, dirty: d) : ScrollPlan(copy: nil, dirty: plain)
}
