// --bench：链路自检 + 实测，跑完退出。HELLO → PING 往返 → 测试图整帧 + FBCRC 回读 → 整帧带宽 → 裸吞吐
// → 小块差分 → SEG 显示当前时间 HH:MM:SS（10 秒，之后固件 5 秒内接回自己的段码屏）。
import Foundation
import MonitaCore

private func ms(_ t0: Date) -> Double { Date().timeIntervalSince(t0) * 1000 }
private func wireBytes(_ rs: [LinkRect]) -> Int { rs.reduce(12) { $0 + 18 + $1.w * $1.h * 2 } }

/// 彩条（上半）+ 灰度渐变（下半），phase 让每帧都不同
private func testPattern(_ phase: Int) -> [UInt16] {
    let bars: [UInt16] = [0xFFFF, 0xFFE0, 0x07FF, 0x07E0, 0xF81F, 0xF800, 0x001F, 0x0000]
    var buf = [UInt16](repeating: 0, count: screenW * screenH)
    for y in 0..<screenH {
        for x in 0..<screenW {
            let v: UInt16
            if y < screenH / 2 { v = bars[((x + phase * 37) / 100) % 8] }
            else { let g = UInt16((x * 255 / (screenW - 1) + phase * 16) & 0xFF); v = (g >> 3) << 11 | (g >> 2) << 5 | (g >> 3) }
            buf[y * screenW + x] = v
        }
    }
    return buf
}

private func fbBytesCRC(_ buf: [UInt16]) -> UInt32 {
    var b = [UInt8](); b.reserveCapacity(buf.count * 2)
    for v in buf { b.append(UInt8(v & 0xFF)); b.append(UInt8(v >> 8)) }
    return linkCRC32(b)
}

func runBench(_ link: DeviceLink) -> Bool {
    var ok = true
    func check(_ c: Bool, _ s: String) { print((c ? "  OK  " : "  FAIL ") + s); if !c { ok = false } }

    print("== HELLO")
    var t0 = Date()
    while !link.connectIfNeeded() {
        if Date().timeIntervalSince(t0) > 10 { print("  FAIL 10 秒内没连上"); return false }
        Thread.sleep(forTimeInterval: 0.5)
    }
    guard let info = link.info else { return false }
    check(info.width == 800 && info.height == 1280, "HELLO_ACK \(info)（\(String(format: "%.1f", ms(t0))) ms）")

    print("== PING ×200")
    var rtts: [Double] = []
    for _ in 0..<200 { t0 = Date(); guard link.request(LinkType.ping) != nil else { check(false, "PING 超时"); return false }; rtts.append(ms(t0)) }
    rtts.sort()
    print(String(format: "  OK   往返 中位 %.3f ms  最小 %.3f  p99 %.3f  最大 %.3f", rtts[100], rtts[0], rtts[197], rtts[199]))

    print("== 测试图整帧 + FBCRC")
    let rects = fullRects(maxPayload: min(info.maxPayload, linkMaxPayload))
    let wire = wireBytes(rects)
    var times: [Double] = []
    for phase in 0..<6 {
        let buf = testPattern(phase)
        t0 = Date()
        let pushed = link.push(rects, buf, frameID: phase)
        let t = ms(t0)
        guard pushed else { check(false, "整帧 \(phase) 推送失败"); return false }
        times.append(t)
        if phase == 0 || phase == 5 {
            let r = link.request(LinkType.fbcrc, [0, 0, 0, 0, 0x20, 0x03, 0x00, 0x05]).flatMap(AckStats.init)
            let want = fbBytesCRC(buf)
            check(r?.extra == want, String(format: "FBCRC 0,0,800,1280 设备 %08X  主机 %08X", r?.extra ?? 0, want))
        }
    }
    let avg = times.dropFirst().reduce(0, +) / Double(times.count - 1)
    print(String(format: "  整帧 %d 条 RECT + PRESENT，线上 %d B：平均 %.1f ms（%.2f MB/s），各次 %@", rects.count, wire, avg,
                 Double(wire) / avg / 1000, times.map { String(format: "%.0f", $0) }.joined(separator: "/")))
    if let a = link.request(LinkType.ping).flatMap(AckStats.init) {
        check(a.crcErr + a.lenErr + a.badParam == 0, "设备统计 rx=\(a.rxBytes) frames=\(a.frames) rects=\(a.rects) presents=\(a.presents) crc_err=\(a.crcErr) len_err=\(a.lenErr) bad=\(a.badParam)")
    }

    print("== 裸吞吐（8 MB 无同步字的填充，设备只扫同步字）")
    let junk = [UInt8](repeating: 0x00, count: 8 << 20)
    t0 = Date()
    let rawOK = link.writeRaw(junk) && link.request(LinkType.ping) != nil
    let rawMs = ms(t0)
    check(rawOK, String(format: "%.1f ms，%.2f MB/s", rawMs, Double(junk.count) / rawMs / 1000))

    print("== 小块差分（100×100 方块移动 ×30）")
    var cur = testPattern(5)
    var prev = cur
    times = []
    var nrects = 0
    for i in 0..<30 {
        let x0 = 50 + i * 20, y0 = 600
        for y in y0..<(y0 + 100) { for x in x0..<(x0 + 100) { cur[y * screenW + x] = 0xF800 } }
        let rs = mergeTiles(diffTiles(prev, cur))
        nrects += rs.count
        t0 = Date()
        guard link.push(rs, cur, frameID: 100 + i) else { check(false, "差分 \(i) 失败"); return false }
        times.append(ms(t0))
        prev = cur
    }
    let r = link.request(LinkType.fbcrc, [0, 0, 0, 0, 0x20, 0x03, 0x00, 0x05]).flatMap(AckStats.init)
    check(r?.extra == fbBytesCRC(cur), String(format: "差分后 FBCRC 一致；平均 %.1f ms / 次，平均 %.1f 个矩形", times.reduce(0, +) / 30, Double(nrects) / 30))

    if info.hasCopy {
        print("== COPY 滚动（歌词区大小 750×514，每帧上移 8 行 + 底部新条带 ×60）")
        let region = LinkRect(x: 43, y: 337, w: 750, h: 514)
        var fb = testPattern(2)
        guard link.push(fullRects(), fb, frameID: 200) else { check(false, "整帧失败"); return false }
        for mode: UInt8 in [0, 1] {
            let m = diagReplyOK(link.request(LinkType.diag, [11, mode], replyType: LinkType.diagR))
            print("  翻页模式 \(mode)（\(mode == 0 ? "先等帧事件" : "立即写基址")）\(m ? "" : "：固件不认 DIAG 11，跳过")")
            if !m { continue }
            t0 = Date()
            for i in 0..<60 {
                let c = scrollCopy(region: region, dy: -8)!
                applyCopy(&fb, c)
                let strip = LinkRect(x: region.x, y: region.y + region.h - 8, w: region.w, h: 8)
                let col: UInt16 = i % 2 == 0 ? 0xF800 : 0x001F
                for y in strip.y..<(strip.y + strip.h) { for x in strip.x..<(strip.x + strip.w) { fb[y * screenW + x] = col } }
                guard link.push([strip], fb, copies: [c], frameID: 300 + i) else { check(false, "滚动帧 \(i) 失败"); return false }
            }
            let per = ms(t0) / 60
            let r = link.request(LinkType.fbcrc, [0, 0, 0, 0, 0x20, 0x03, 0x00, 0x05]).flatMap(AckStats.init)
            check(r?.extra == fbBytesCRC(fb), String(format: "FBCRC 一致；%.1f ms / 帧（%.1f fps）", per, 1000 / per))
        }
        _ = link.request(LinkType.diag, [11, flipNow ? 1 : 0], replyType: LinkType.diagR)
    } else {
        print("== COPY：固件 caps=\(info.caps) 不支持（要 fw v2），跳过")
    }

    print("== SEG：主区显示当前时间 HH:MM:SS，10 秒")
    let f = DateFormatter(); f.dateFormat = "HH:mm:ss"
    var segMs: [Double] = []
    for _ in 0..<10 {
        let s = f.string(from: Date())
        t0 = Date()
        guard let a = link.request(LinkType.seg, segMain(s)).flatMap(AckStats.init), a.status == 0 else { check(false, "SEG 没有 OK 应答"); return false }
        segMs.append(ms(t0))
        Thread.sleep(forTimeInterval: max(0, 1 - Date().timeIntervalSince1970.truncatingRemainder(dividingBy: 1)))
    }
    check(true, String(format: "SEG 往返平均 %.1f ms", segMs.reduce(0, +) / 10))
    print(ok ? "全部通过" : "有失败项")
    return ok
}

/// --png 之外的离线检查：--scroll-sim --sample lyrics.json。不连设备，模拟一次切句滚动（每 30 ms 一帧），
/// 每帧打印 COPY 平移量、脏 tile 数、线上字节，并核对"上一帧 + COPY + 脏 tile = 新一帧"逐像素一致
func runScrollSim(_ d0: LyricsData, render: (PageInput) -> [UInt16], page: LyricsPage) -> Bool {
    guard d0.lyric.lines.count > 3 else { print("样例歌词太少"); return false }
    let t1 = d0.lyric.lines[2].t
    var d = d0
    d.state = "playing"; d.play.state = "playing"; d.play.rate = 1
    d.play.at = 0; d.play.pos = t1 - lyricLeadS - 0.1          // nowMs = 100 时切到第 3 句
    var cfg = PageConfig(); cfg.scroll = true
    func inp(_ ms: Double) -> PageInput { PageInput(data: d, cover: nil, cfg: cfg, panel: PanelInfo(), nowMs: ms, date: Date(timeIntervalSince1970: 0)) }
    var prev = render(inp(0))
    var ok = true, total = 0, frames = 0
    for ms in stride(from: 100.0, through: 700, by: 30) {
        let next = render(inp(ms))
        var plan = ScrollPlan(copy: nil, dirty: diffTiles(prev, next))
        if let dy = page.scrollDelta, dy != 0 { plan = planScroll(prev: prev, next: next, region: page.lyricClip, dy: dy) }
        var dev = prev
        if let c = plan.copy { applyCopy(&dev, c) }
        for t in plan.dirty { for y in t.y..<(t.y + tileH) { for x in t.x..<(t.x + tileW) { dev[y * screenW + x] = next[y * screenW + x] } } }
        let same = dev == next
        if !same { ok = false }
        let rects = mergeTiles(plan.dirty)
        let bytes = rects.reduce(12) { (a: Int, r: LinkRect) -> Int in a + 18 + r.w * r.h * 2 } + (plan.copy == nil ? 0 : 22)
        total += bytes; frames += 1
        print(String(format: "t=%3.0fms off=%3d dy=%4@ copy=%@ tiles=%4d rects=%3d bytes=%7d %@", ms, page.scrollOffset(nowMs: ms),
                     page.scrollDelta.map { String($0) } ?? "-", plan.copy == nil ? "no " : "yes", plan.dirty.count, rects.count, bytes, same ? "" : "MISMATCH"))
        prev = next
    }
    print(String(format: "%d 帧，平均 %.0f KB/帧；%@", frames, Double(total) / Double(frames) / 1024, ok ? "重建全部一致" : "有帧重建不一致"))
    return ok
}
