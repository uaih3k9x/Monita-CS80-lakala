// 沉浸模式（fw v9，docs/protocol.md「沉浸模式」）的推送：进入 / 退出、按 ImmPlanner 的规划写卷轴、滚动。
// 只在渲染线程上用（和普通推帧共用一块画布）。--immersive 开机就进；查询键长按切换。
import Foundation
import MonitaCore

final class ImmersiveDriver {
    let link: DeviceLink
    let page: LyricsPage
    private var planner: ImmPlanner?
    private var gen = -1
    private var content: ImmContent?
    private var contentSrc = ""
    static let durMs = 360

    init(link: DeviceLink, page: LyricsPage) { self.link = link; self.page = page }

    /// 设备上正处在沉浸模式（重连过 = 固件已经自己退了）
    var active: Bool { planner != nil && gen == link.generation }

    func enter() -> Bool {
        guard let a = link.request(LinkType.immEnter, immEnterPayload(origin: 0)).flatMap(AckStats.init) else { return false }
        _ = link.newErrors(a)
        guard a.status == 0, a.extra >= UInt32(screenH + 256) else {
            log("沉浸模式：设备拒绝（status \(a.status)，多半是内存不够），留在普通页面")
            return false
        }
        planner = ImmPlanner(rows: Int(a.extra), screenH: screenH)
        gen = link.generation
        log("沉浸模式：进入，卷轴 \(a.extra) 行")
        return true
    }

    func exit() {
        defer { planner = nil }
        guard active else { return }
        if let a = link.request(LinkType.immExit).flatMap(AckStats.init) {
            _ = link.newErrors(a)
            if a.status == 0 { link.updateCaps(Int(a.extra)) }   // 退出后多缓冲可能拿不全
            log(String(format: "沉浸模式：退出，caps=0x%X", a.extra))
        }
    }

    /// 一次唤醒；返回下一次该醒的时刻（秒）
    func tick(_ d: LyricsData?, cfg: PageConfig, nowMs: Double) -> Double {
        let now = nowMs / 1000
        guard var planner else { return now + 1 }
        // 只在排版相关的字段变了时重排（进度 / 时间戳每秒都在变，不算）
        let lines: [String] = (d?.lyric.lines ?? []).map { l in "\(l.t)\u{1}\(l.x)\u{1}\(l.tr)" }
        let stopped: String = d?.state == "stopped" ? "0" : "1"
        var parts: [String] = [d?.songId ?? "", d?.title ?? "", d?.artist ?? "", stopped, d?.error ?? ""]
        parts += [d?.lyric.status ?? "", String(cfg.translation), lines.joined(separator: "\u{2}")]
        let src: String = parts.joined(separator: "\u{3}")
        if content == nil || src != contentSrc { content = page.immBuild(d, cfg: cfg); contentSrc = src }
        guard let c = content else { return now + 1 }
        let pos = d.map { positionAt($0.play, nowMs: nowMs) } ?? 0
        let line = c.times.isEmpty ? -1 : lineIndex(c.times, pos + lyricLeadS + 0.1)   // 再提前 0.1 s：滚动有 360 ms
        let plan = planner.plan(c.blocks, line: line, key: c.key, durMs: Self.durMs)
        self.planner = planner
        if !run(plan, c) { self.planner?.invalidate(); return now + 0.5 }
        // 下一句的时刻（播放中）；暂停 / 没歌词时数据一变就会被叫醒
        if d?.state == "playing", line + 1 < c.times.count {
            return now + max(0.02, min(c.times[line + 1] - (pos + lyricLeadS + 0.1), 5))
        }
        return now + 5
    }

    private func run(_ p: ImmPlan, _ c: ImmContent) -> Bool {
        if let o = p.reset {
            guard let a = link.request(LinkType.immEnter, immEnterPayload(origin: o)).flatMap(AckStats.init), a.status == 0 else { return false }
            _ = link.newErrors(a)
        }
        guard write(p.before, c) else { return false }
        if let s = p.scroll {
            guard let a = link.request(LinkType.immScroll, immScrollPayload(y: s.y, durMs: s.durMs)).flatMap(AckStats.init),
                  a.status == 0, !link.newErrors(a) else { log("沉浸模式：滚动 / 写卷轴出错，整屏重来"); return false }
            if !p.after.isEmpty { Thread.sleep(forTimeInterval: Double(s.durMs) / 1000 * 1.03 + 0.05) }   // 设备 1.024 ms 一单位
        }
        if !p.after.isEmpty {
            guard write(p.after, c), let a = link.request(LinkType.ping).flatMap(AckStats.init), !link.newErrors(a) else { return false }
        }
        return true
    }

    /// 每个写：把那一块按档位画到画布顶上，取需要的行，切成 IMM_RECT 条带
    private func write(_ ws: [ImmWrite], _ c: ImmContent) -> Bool {
        var payloads: [[UInt8]] = []
        let maxP = link.rectMax                       // 设备给的 max_payload 变小时条带跟着变窄
        for w in ws {
            let b = c.blocks[w.block]
            page.immDrawBlock(c, w.block, cls: w.cls, top: 0)
            var buf = [UInt16](repeating: 0, count: screenW * b.h)
            toRGB565(page.cv.pixels, count: screenW * b.h, into: &buf)
            let local = (w.rows.lowerBound - b.y)..<(w.rows.upperBound - b.y)
            payloads += immRectPayloads(y: b.y, rows: local, visible: w.visible, buf, width: screenW, maxPayload: maxP)
        }
        return payloads.isEmpty || link.send(LinkType.immRect, payloads)
    }
}
