// 背面段码屏：每 250 ms 算一帧（MonitaCore/SegFrame），变了立即发 SEG，没变也每 1.5 s 重发一次
// （固件 5 s 收不到 SEG 就接回自己的时钟）。--no-seg 关掉；--seg-png 出离线示意图。
import CoreGraphics
import Foundation
import IOKit.ps
import MonitaCore

/// Mac 内置电池电量 + 是否接交流电；台式机 / 拿不到 → nil（电池区整块不亮）
func macBattery() -> SegBattery? {
    guard let info = IOPSCopyPowerSourcesInfo()?.takeRetainedValue(),
          let list = IOPSCopyPowerSourcesList(info)?.takeRetainedValue() as? [CFTypeRef] else { return nil }
    for src in list {
        guard let d = IOPSGetPowerSourceDescription(info, src)?.takeUnretainedValue() as? [String: Any],
              (d[kIOPSTypeKey] as? String) == kIOPSInternalBatteryType,
              let cur = d[kIOPSCurrentCapacityKey] as? Int, let max = d[kIOPSMaxCapacityKey] as? Int, max > 0 else { continue }
        let ac = (d[kIOPSPowerSourceStateKey] as? String) == kIOPSACPowerValue
        return SegBattery(percent: Int((Double(cur) * 100 / Double(max)).rounded()), onAC: ac)
    }
    return nil
}

func segInput(_ d: LyricsData?, _ date: Date, battery: SegBattery?) -> SegInput {
    let c = Calendar.current.dateComponents([.hour, .minute, .month, .day], from: date)
    let clock = SegClock(hour: c.hour ?? 0, minute: c.minute ?? 0, month: c.month ?? 0, day: c.day ?? 0)
    let play = d.flatMap { $0.isPlaying ? $0.play : nil }
    return SegInput(play: play, nowMs: date.timeIntervalSince1970 * 1000, clock: clock, battery: battery)
}

func startSegLoop(_ link: DeviceLink) {
    var last: [UInt8]?
    var lastSent = 0.0
    var battery = macBattery(), batteryAt = nowMs()
    var sent = 0, fails = 0, statAt = nowMs()
    var wasUp = false
    every(0.25, "seg") {
        let now = nowMs()
        if now - batteryAt > 30_000 { battery = macBattery(); batteryAt = now }
        guard link.isUp else { wasUp = false; last = nil; return }
        let fb = segFrame(segInput(shared.data, Date(), battery: battery))
        if fb != last || now - lastSent >= 1500 {
            let a = link.request(LinkType.seg, fb).flatMap(AckStats.init)
            if let a, a.status == 0 {
                if !wasUp { log("段码屏：主机接管（播放进度 / 时钟）") }
                wasUp = true; last = fb; lastSent = now; sent += 1
            } else {
                fails += 1; last = nil
                log("段码屏：SEG \(a.map { "status=\($0.status)" } ?? "没有应答")")
            }
        }
        if now - statAt >= 60_000 {
            log("60s: seg sent=\(sent) fail=\(fails)")
            sent = 0; fails = 0; statAt = now
        }
    }
}

// MARK: 离线示意图（按显存解码画，段的位置是示意排布，不是玻璃的真实形状）
func writeSegPreview(_ rows: [(String, [UInt8])], _ path: String) -> Bool {
    let rowH = 190.0, W = 820
    let cv = Canvas(w: W, h: Int(rowH) * rows.count + 20)
    let bg = RGB("#2b2f33"), glass = RGB("#dfe6d6"), on = RGB("#1d2a22"), off = RGB("#c9d3c4"), label = RGB("#e6e6e6")
    cv.fillRect(0, 0, Double(cv.w), Double(cv.h), bg)
    func seg(_ r: CGRect, _ lit: Bool) {
        cv.ctx.setFillColor((lit ? on : off).cg)
        cv.ctx.addPath(CGPath(roundedRect: r, cornerWidth: min(r.width, r.height) / 2, cornerHeight: min(r.width, r.height) / 2, transform: nil))
        cv.ctx.fillPath()
    }
    func digit(_ m: UInt8, _ x: Double, _ y: Double, _ w: Double, _ h: Double) {
        let t = w * 0.18, g = t * 0.3
        let hs = { (yy: Double) in CGRect(x: x + t / 2 + g, y: yy - t / 2, width: w - t - 2 * g, height: t) }
        let vs = { (xx: Double, y0: Double, y1: Double) in CGRect(x: xx - t / 2, y: y0 + g, width: t, height: y1 - y0 - 2 * g) }
        let top = y + t / 2, mid = y + h / 2, bot = y + h - t / 2, l = x + t / 2, r = x + w - t / 2
        let rs = [hs(top), vs(r, top, mid), vs(r, mid, bot), hs(bot), vs(l, mid, bot), vs(l, top, mid), hs(mid)]
        for (i, rc) in rs.enumerated() { seg(rc, m >> i & 1 != 0) }
    }
    func digits(_ fb: [UInt8], _ cols: Range<Int>, _ x: Double, _ y: Double, _ w: Double, _ h: Double, _ gap: Double, colons: [(Int, SegBit)]) {
        for (i, k) in cols.enumerated() { digit(segDigitAt(fb, k), x + Double(i) * (w + gap), y, w, h) }
        for (after, b) in colons {
            let cx = x + Double(after + 1) * (w + gap) - gap / 2
            for cy in [y + h * 0.3, y + h * 0.7] { cv.circle(cx, cy, w * 0.09, fill: fb[b.byte] & b.mask != 0 ? on : off, stroke: nil) }
        }
    }
    for (n, (name, fb)) in rows.enumerated() {
        let y0 = 10 + Double(n) * rowH
        func lit(_ b: SegBit) -> Bool { fb[b.byte] & b.mask != 0 }
        cv.text(name, 24, y0 + 22, FontSpec(px: 18, bold: true), label, .left, .middle)
        cv.ctx.setFillColor(glass.cg)
        cv.ctx.addPath(CGPath(roundedRect: CGRect(x: 16, y: y0 + 38, width: Double(W) - 32, height: rowH - 48), cornerWidth: 12, cornerHeight: 12, transform: nil))
        cv.ctx.fillPath()
        digits(fb, 0..<6, 40, y0 + 56, 50, 100, 18, colons: [(1, .mainColon2), (3, .mainColon4)])
        digits(fb, 8..<14, 470, y0 + 56, 26, 48, 10, colons: [(1, .clkColonA), (3, .clkColonB)])
        // 电池：外框 + 四格 + 闪电（正极在左；A..D 左→右，从 D 开始亮）
        let bx = 476.0, by = y0 + 124
        cv.strokeRect(bx, by, 96, 32, lit(.batFrame) ? on : off, 3)
        cv.fillRect(bx - 6, by + 10, 6, 12, lit(.batFrame) ? on : off)
        for (i, b) in [SegBit.batA, .batB, .batC, .batD].enumerated() { cv.fillRect(bx + 6 + Double(i) * 22, by + 6, 18, 20, lit(b) ? on : off) }
        cv.ctx.setFillColor((lit(.charge) ? on : off).cg)
        let lx = bx + 118, ly = by - 2
        cv.ctx.move(to: CGPoint(x: lx + 12, y: ly)); cv.ctx.addLine(to: CGPoint(x: lx, y: ly + 20))
        cv.ctx.addLine(to: CGPoint(x: lx + 9, y: ly + 20)); cv.ctx.addLine(to: CGPoint(x: lx + 5, y: ly + 36))
        cv.ctx.addLine(to: CGPoint(x: lx + 19, y: ly + 14)); cv.ctx.addLine(to: CGPoint(x: lx + 10, y: ly + 14))
        cv.ctx.closePath(); cv.ctx.fillPath()
        // 喇叭 + 五道声波
        let sx = 708.0, sy = y0 + 84
        cv.ctx.setFillColor((lit(.speaker) ? on : off).cg)
        cv.ctx.move(to: CGPoint(x: sx, y: sy - 8)); cv.ctx.addLine(to: CGPoint(x: sx + 10, y: sy - 8))
        cv.ctx.addLine(to: CGPoint(x: sx + 24, y: sy - 20)); cv.ctx.addLine(to: CGPoint(x: sx + 24, y: sy + 20))
        cv.ctx.addLine(to: CGPoint(x: sx + 10, y: sy + 8)); cv.ctx.addLine(to: CGPoint(x: sx, y: sy + 8))
        cv.ctx.closePath(); cv.ctx.fillPath()
        for (i, b) in SegBit.volumes.enumerated() {
            cv.ctx.setStrokeColor((lit(b) ? on : off).cg); cv.ctx.setLineWidth(4)
            cv.addArc(sx + 26, sy, 10 + Double(i) * 8, -0.7, 0.7); cv.ctx.strokePath()
        }
    }
    return cv.writePNG(path)
}
