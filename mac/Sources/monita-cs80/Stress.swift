// --stress [秒]：混合压测，模仿日常歌词页的负载但开足马力：小块差分 + COPY 滚动 + 偶尔整帧 + 每秒 SEG。
// 断开就记下来、等重连、推整帧接着跑；最后报失败次数和各类耗时。--no-lz4 做对照。
import Foundation
import MonitaCore

func runStress(_ link: DeviceLink, seconds: Int) -> Bool {
    let f = DateFormatter(); f.dateFormat = "HH:mm:ss"
    func stamp() -> String { f.string(from: Date()) }
    var t0 = Date()
    while !link.connectIfNeeded() {
        if Date().timeIntervalSince(t0) > 10 { print("10 秒内没连上"); return false }
        Thread.sleep(forTimeInterval: 0.5)
    }
    var gen = StressGen()                                // 负载生成在 MonitaCore/StressGen.swift（单测共用）
    var fails: [String] = [], n = [0, 0, 0, 0], ms: [[Double]] = [[], [], [], []]   // 0 差分 1 滚动 2 整帧 3 SEG
    var bytes = 0, lz4Rects = 0, frame = 0, lastSeg = Date.distantPast
    let start = Date()
    func recover(_ what: String) {
        fails.append("\(stamp()) \(what)")
        print("  [\(stamp())] FAIL \(what)，等重连…")
        let t = Date()
        // PRESENT 超时但 PING 有应答时连接还在、已重新 HELLO（SerialLink stalledProbe），不用重开
        while !link.isUp && !link.connectIfNeeded() { if Date().timeIntervalSince(t) > 30 { break }; Thread.sleep(forTimeInterval: 0.3) }
        print(String(format: "  [%@] 重连用了 %.1f s", stamp(), Date().timeIntervalSince(t)))
        _ = link.push(fullRects(), gen.fb, frameID: frame)
    }
    print("压测 \(seconds) s，LZ4=\(link.lz4 ? "开" : "关")，USB 补齐=\(link.usbPad ? "开" : "关")，开始 \(stamp())")
    while Date().timeIntervalSince(start) < Double(seconds) {
        frame += 1
        let k = gen.roll()
        var ok: Bool, kind: Int
        t0 = Date()
        if Date().timeIntervalSince(lastSeg) >= 1 {
            kind = 3; lastSeg = Date()
            ok = link.request(LinkType.seg, segMain(stamp())).flatMap(AckStats.init)?.status == 0
        } else if k < 3 {
            kind = 2
            let rs = gen.full(); ok = link.push(rs, gen.fb, frameID: frame)
        } else if k < 55 {
            kind = 1
            let (rs, c) = gen.scroll()
            ok = link.push(rs, gen.fb, copies: [c], frameID: frame)
        } else {
            kind = 0
            let rs = gen.diff(); ok = link.push(rs, gen.fb, frameID: frame)
        }
        let dt = Date().timeIntervalSince(t0) * 1000
        if kind != 3 { bytes += link.lastPushBytes; lz4Rects += link.lastPushLz4 }
        if ok { n[kind] += 1; ms[kind].append(dt) } else { recover(["差分", "滚动", "整帧", "SEG"][kind] + String(format: "（%.0f ms，批长 %d，%% 512 = %d）", dt, link.lastPushBytes, link.lastPushBytes % 512)) }
        if frame % 500 == 0 {
            let r = link.request(LinkType.fbcrc, [0, 0, 0, 0, 0x20, 0x03, 0x00, 0x05]).flatMap(AckStats.init)
            var b = [UInt8](); b.reserveCapacity(gen.fb.count * 2)
            for v in gen.fb { b.append(UInt8(v & 0xFF)); b.append(UInt8(v >> 8)) }
            let same = r?.extra == linkCRC32(b)
            print(String(format: "  [%@] %5d 帧  %.1f MB 已发  FBCRC %@  失败 %d", stamp(), frame, Double(bytes) / 1e6, same ? "一致" : "不一致", fails.count))
            if !same && r != nil { fails.append("\(stamp()) FBCRC 不一致"); _ = link.push(fullRects(), gen.fb, frameID: frame) }
        }
    }
    let el = Date().timeIntervalSince(start)
    print(String(format: "== 结束：%.0f s，%d 帧，发 %.1f MB（%.1f MB/min），RECT_LZ4 %d 条", el, frame, Double(bytes) / 1e6, Double(bytes) / 1e6 / el * 60, lz4Rects))
    for (i, name) in ["差分", "滚动", "整帧", "SEG"].enumerated() where !ms[i].isEmpty {
        let s = ms[i].sorted()
        print(String(format: "  %@ ×%d  中位 %.1f  p99 %.1f  最大 %.1f ms", name, n[i], s[s.count / 2], s[min(s.count - 1, s.count * 99 / 100)], s.last!))
    }
    if let a = link.request(LinkType.ping).flatMap(AckStats.init) {
        print("  设备统计（本次连接）crc_err=\(a.crcErr) len_err=\(a.lenErr) bad=\(a.badParam) frames=\(a.frames)")
    }
    print("失败 \(fails.count) 次"); fails.forEach { print("  " + $0) }
    return fails.isEmpty
}
