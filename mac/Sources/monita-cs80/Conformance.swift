// --conformance：对着固件模拟器（tools/fwsim）跑一遍和 Python 参考实现同样的脚本，
// 最后打印一行 "@result {json}"，由 tools/fwsim/conformance.py 和 Python 那一份、和 docs/protocol.md 对照。
// 要模拟器按键时打印 "@key K ms"（harness 收到后让模拟器按一下键）；
// 打印 "@stat 名字" 后等 stdin 一行：harness 在这一步看一眼模拟器里固件的状态（灯、段码屏、时钟、滚动 …）。
// HELLO 带的时间：环境变量 MONITA_FAKE_NOW（unix 秒）+ TZ，由 harness 给固定值。
// 走的都是渲染器真正用的代码：DeviceLink（HELLO / push / request / send / setFlipNow）、MonitaCore 的编码。
// 不是给真设备用的：会切沉浸模式、发坏帧。
import Foundation
import MonitaCore

/// 和 tools/fwsim/conformance.py 里的同名函数逐像素一致
private func patA(_ x: Int, _ y: Int) -> UInt16 { UInt16((x * 31 + y * 17 + (x ^ y)) & 0xFFFF) }
private func patB(_ x: Int, _ y: Int) -> UInt16 { UInt16(((x * 3 + y * 5) & 0xFFFF) ^ 0x5555) }
private func patRow(_ row: Int, _ x: Int) -> UInt16 { UInt16((row * 7 + x * 3) & 0xFFFF) }
private func patVis(_ row: Int, _ x: Int) -> UInt16 { UInt16(((row * 11 + x) & 0xFFFF) ^ 0xA5A5) }

/// 和 conformance.py 的 SEG_PAT 一致
private let segPattern: [UInt8] = (0..<72).map { UInt8(($0 * 37 + 11) & 0xFF) }

/// 让 harness 看一眼模拟器状态，等它回一行再往下走
private func snap(_ label: String) { print("@stat \(label)"); fflush(stdout); _ = readLine() }

private func frameA() -> [UInt16] {
    var b = [UInt16](repeating: 0, count: screenW * screenH)
    for y in 0..<screenH { for x in 0..<screenW { b[y * screenW + x] = patA(x, y) } }
    return b
}

func runConformance(_ link: DeviceLink) -> Bool {
    var keys: [[Int]] = []
    link.onKey = { e in keys.append([Int(e.key.rawValue), Int(e.edge.rawValue), e.count]) }
    var res: [String: Any] = [:]
    if let v = ProcessInfo.processInfo.environment["MONITA_FAKE_NOW"], let t = Double(v) {
        link.clock = { Date(timeIntervalSince1970: t) }
    }
    let t0 = Date()
    while !link.connectIfNeeded() {
        if Date().timeIntervalSince(t0) > 5 { print("@result {\"error\":\"HELLO 失败\"}"); return false }
        Thread.sleep(forTimeInterval: 0.2)
    }
    guard let info = link.info else { return false }
    snap("hello")
    res["hello"] = ["proto": info.proto, "fw": info.fw, "w": info.width, "h": info.height, "pixfmt": info.pixfmt,
                    "seg_bytes": info.segBytes, "caps": info.caps, "max_payload": info.maxPayload]

    func ack(_ type: UInt8, _ pl: [UInt8] = []) -> AckStats? { link.request(type, pl).flatMap(AckStats.init) }
    func ping() -> AckStats { ack(LinkType.ping)! }
    func diag(_ pl: [UInt8]) -> [Int] {
        guard let r = link.request(LinkType.diag, pl, replyType: LinkType.diagR, timeoutMs: 10_000), r.count >= 4 else { return [] }
        return [Int(r[0]), Int(r[1]), Int(r[2]) | Int(r[3]) << 8]
    }
    func fbcrc(_ x: Int, _ y: Int, _ w: Int, _ h: Int) -> AckStats {
        var pl: [UInt8] = []
        for v in [x, y, w, h] { pl += [UInt8(v & 0xFF), UInt8(v >> 8)] }
        return ack(LinkType.fbcrc, pl)!
    }
    /// 一串原始字节 (+ 可选的等待) 之后再 PING：返回计数增量 [frames, crc_err, len_err, rx_bytes]
    func delta(_ junk: [UInt8], sleep: Double = 0) -> [Int] {
        let a = ping()
        _ = link.writeRaw(junk)
        if sleep > 0 { Thread.sleep(forTimeInterval: sleep) }
        let b = ping()
        return [Int(b.frames - a.frames), Int(b.crcErr - a.crcErr), Int(b.lenErr - a.lenErr), Int(b.rxBytes - a.rxBytes)]
    }

    let p0 = ping()
    res["ping"] = ["for_type": Int(p0.forType), "status": Int(p0.status), "frames": Int(p0.frames), "rx_bytes": Int(p0.rxBytes),
                   "crc_err": Int(p0.crcErr), "len_err": Int(p0.lenErr)]

    // 整帧 + FBCRC
    let a = frameA()
    let full = link.push(fullRects(), a, frameID: 1)
    let pf = ping()
    res["full"] = ["ok": full, "rects": Int(pf.rects), "presents": Int(pf.presents),
                   "fbcrc_ok": fbcrc(0, 0, screenW, screenH).extra == fbRegionCRC(a, x: 0, y: 0, w: screenW, h: screenH)]

    // COPY + 两个 RECT
    var b = a
    let copy = LinkCopy(sx: 0, sy: 108, w: 800, h: 592, dx: 0, dy: 100)
    applyCopy(&b, copy)
    let rects = [LinkRect(x: 0, y: 692, w: 800, h: 8), LinkRect(x: 41, y: 40, w: 33, h: 17)]
    for r in rects { for y in r.y..<(r.y + r.h) { for x in r.x..<(r.x + r.w) { b[y * screenW + x] = patB(x, y) } } }
    let diffOK = link.push(rects, b, copies: [copy], frameID: 2)
    res["diff"] = ["ok": diffOK, "rects": Int(ping().rects - pf.rects),
                   "fbcrc_ok": fbcrc(0, 0, screenW, screenH).extra == fbRegionCRC(b, x: 0, y: 0, w: screenW, h: screenH),
                   "sub_fbcrc_ok": fbcrc(41, 40, 33, 17).extra == fbRegionCRC(b, x: 41, y: 40, w: 33, h: 17)]
    res["fbcrc_bad"] = Int(fbcrc(799, 0, 2, 1).status)

    res["seg"] = [Int(ack(LinkType.seg, segPattern)!.status), Int(ack(LinkType.seg, [UInt8](repeating: 0, count: 71))!.status)]
    snap("seg")
    res["backlight"] = [Int(ack(LinkType.backlight, [0])!.status), Int(ack(LinkType.backlight, [])!.status)]
    snap("backlight")
    res["led"] = [Int(ack(LinkType.led, [2, 2, 0xF4, 0x01, 100, 0, 0, 0])!.status),   // 绿灯闪 500/100
                  Int(ack(LinkType.led, [8, 1, 0, 0, 0, 0, 0, 0])!.status),            // mask 位不对
                  Int(ack(LinkType.led, [1, 2, 100, 0, 100, 0, 0, 0])!.status)]        // on >= period
    snap("led")

    // 按键：模拟器按一下 +，再长按查询键
    func waitKeys(_ n: Int) {
        let end = Date().addingTimeInterval(3)
        while keys.count < n && Date() < end { link.pollEvents(); Thread.sleep(forTimeInterval: 0.01) }
    }
    print("@key 3 60"); fflush(stdout); waitKeys(2)
    print("@key 1 1300"); fflush(stdout); waitKeys(5)
    res["keys"] = keys
    snap("keys")

    let u = ack(0x55)!
    res["unknown"] = ["for_type": Int(u.forType), "status": Int(u.status)]
    res["diag"] = ["flip": diag([11, 1]), "op8": diag([8]), "empty": diag([]), "tri": diag([31]), "set_flip_now": link.setFlipNow()]

    let bad: [UInt8] = { var f = encodeFrame(type: LinkType.ping, seq: 0x33, payload: []); f[f.count - 1] ^= 0xFF; return f }()
    let mp = info.maxPayload + 1
    let swallow = [0xA5, 0x5A, LinkType.ping, 0x34, 20, 0] + encodeFrame(type: LinkType.ping, seq: 0x35, payload: []) + [UInt8](repeating: 0, count: 14)
    res["errors"] = ["bad_crc": delta(bad),
                     "oversize": delta([0xA5, 0x5A, LinkType.rect, 0x36, UInt8(mp & 0xFF), UInt8(mp >> 8)]),
                     "swallow": delta(swallow),
                     "garbage": delta([0x00, 0x11, 0xA5, 0x00, 0x5A]),
                     "half": delta([0xA5, 0x5A, LinkType.ping, 0x37, 4, 0, 1, 2], sleep: 0.7)]

    if info.hasImmersive { res["imm"] = immSession(link, ack: { ack($0, $1) }, ping: ping, fbcrc: fbcrc, a: a) }

    // 重新 HELLO：计数清零（DeviceLink 只在断开后才重发 HELLO，这里直接发）
    let h2 = link.request(LinkType.hello, [], replyType: LinkType.helloAck)
    let p2 = ping()
    res["rehello"] = ["ok": h2 != nil, "frames": Int(p2.frames), "rx_bytes": Int(p2.rxBytes)]
    snap("rehello")

    guard let data = try? JSONSerialization.data(withJSONObject: res, options: [.sortedKeys]), let s = String(data: data, encoding: .utf8) else { return false }
    print("@result " + s)
    return true
}

private func immSession(_ link: DeviceLink, ack: (UInt8, [UInt8]) -> AckStats?, ping: () -> AckStats,
                        fbcrc: (Int, Int, Int, Int) -> AckStats, a: [UInt16]) -> [String: Any] {
    var r: [String: Any] = [:]
    func ackT(_ t: UInt8, _ pl: [UInt8] = []) -> AckStats? { ack(t, pl) }
    guard let e = ackT(LinkType.immEnter, immEnterPayload(origin: 0)) else { return ["enter": "no reply"] }
    r["enter"] = [Int(e.status), Int(e.extra)]
    guard e.status == 0 else { return r }
    let R = Int(e.extra)
    var rows: [Int: [UInt16]] = [:]
    func write(_ y: Int, _ h: Int, visible: Bool, _ pat: (Int, Int) -> UInt16, record: Bool = true) {
        var buf = [UInt16](repeating: 0, count: screenW * h)
        for i in 0..<h { for x in 0..<screenW { buf[i * screenW + x] = pat(y + i, x) } }
        _ = link.send(LinkType.immRect, immRectPayloads(y: y, rows: 0..<h, visible: visible, buf, width: screenW, maxPayload: link.rectMax))
        if record { for i in 0..<h { rows[y + i] = Array(buf[(i * screenW)..<((i + 1) * screenW)]) } }
    }
    func windowCRC(_ y: Int) -> UInt32 {
        var buf: [UInt16] = []
        for i in 0..<screenH { buf += rows[y + i] ?? [UInt16](repeating: 0, count: screenW) }
        return fbRegionCRC(buf, x: 0, y: 0, w: screenW, h: screenH)
    }
    func crcIs(_ y: Int) -> Bool { fbcrc(0, 0, screenW, screenH).extra == windowCRC(y) }
    func scroll(_ y: Int, _ dur: Int) -> [Int] { let s = ackT(LinkType.immScroll, immScrollPayload(y: y, durMs: dur))!; return [Int(s.status), Int(s.extra)] }

    let p0 = ping()
    _ = link.newErrors(p0)                                    // 基线对齐: push 只能靠 status 发现被拒
    r["present_status"] = Int(ackT(LinkType.present, [0, 0])!.status)
    r["push_ok"] = link.push([], a, frameID: 9)
    _ = link.send(LinkType.rect, [rectPayload(LinkRect(x: 0, y: 0, w: 1, h: 1), a)])
    _ = link.send(LinkType.copy, [copyPayload(LinkCopy(sx: 0, sy: 0, w: 1, h: 1, dx: 1, dy: 1))])
    let p1 = ping()
    r["plain_writes"] = [Int(p1.badParam - p0.badParam), Int(p1.rects - p0.rects)]
    r["flip_ok"] = link.setFlipNow()

    write(0, 1600, visible: false, patRow)
    r["scroll0"] = scroll(0, 0); r["crc0"] = crcIs(0)
    r["scroll300"] = scroll(300, 0); r["crc300"] = crcIs(300)
    let p2 = ping()
    write(400, 10, visible: false, patVis, record: false)     // 在屏上、没带 bit0：拒
    write(400, 10, visible: true, patVis)
    let p3 = ping()
    r["vis"] = [Int(p3.badParam - p2.badParam), Int(p3.rects - p2.rects)]
    r["anim"] = scroll(320, 100)
    snap("imm_anim")
    Thread.sleep(forTimeInterval: 0.3)
    r["crc320"] = crcIs(320)
    write(1600, R + 200 - 1600, visible: false, patRow)       // 写过卷轴末尾：固件就地换底
    let p4 = ping()
    r["far"] = [Int(p4.badParam - p3.badParam), Int(p4.rects - p3.rects)]
    r["scroll1600"] = scroll(1600, 0); r["crc1600"] = crcIs(1600)
    r["out"] = scroll(R + 2000, 0)[0]
    var win: [UInt16] = []
    for i in 0..<screenH { win += rows[1600 + i]! }
    r["sub"] = fbcrc(5, 7, 33, 17).extra == fbRegionCRC(win, x: 5, y: 7, w: 33, h: 17)
    let x = ackT(LinkType.immExit)!
    if x.status == 0 { link.updateCaps(Int(x.extra)) }
    r["exit"] = [Int(x.status), Int(x.extra)]
    r["crc_exit"] = crcIs(1600)
    r["caps_after"] = link.info?.caps ?? -1
    _ = link.newErrors(ping())                                // 沉浸模式里被拒的写已经看过了 (同 ImmersiveDriver)
    let ok = link.push(fullRects(), a, frameID: 10)
    r["after"] = [ok, fbcrc(0, 0, screenW, screenH).extra == fbRegionCRC(a, x: 0, y: 0, w: screenW, h: screenH)]
    return r
}
