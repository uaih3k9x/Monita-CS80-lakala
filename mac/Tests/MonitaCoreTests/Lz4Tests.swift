// fw v11 RECT_LZ4 (0x23)：Apple Compression 的 COMPRESSION_LZ4_RAW 是标准 LZ4 raw block（和 liblz4 / 固件 fw/link/lz4.h 互通），
// 压缩后 < 3/4 才发，旧固件（caps 没 bit8）照发 RECT
import Testing
import MonitaCore

/// liblz4（python lz4.block.compress(store_size=False)）编的块：0x1234 × 300 + "Monita" × 20
private let liblz4Block: [UInt8] = [47, 52, 18, 2, 0, 255, 255, 69, 111, 77, 111, 110, 105, 116, 97, 6, 0, 90, 80, 111, 110, 105, 116, 97]
private let liblz4Raw: [UInt8] = Array([[UInt8]](repeating: [0x34, 0x12], count: 300).joined()) + Array([[UInt8]](repeating: Array("Monita".utf8), count: 20).joined())

@Test func appleLz4RawIsStandardBlock() {
    guard lz4Available else { return }
    #expect(lz4DecodeRaw(liblz4Block, rawCount: liblz4Raw.count) == liblz4Raw)       // liblz4 编 → Apple 解
    let c = lz4EncodeRaw(liblz4Raw, limit: liblz4Raw.count)!
    #expect(lz4DecodeRaw(c, rawCount: liblz4Raw.count) == liblz4Raw)                 // Apple 自己往返
    #expect(c.count < liblz4Raw.count / 4)                                           // Apple 的编码器比 liblz4 啰嗦 (132 vs 24 B), 但照样小得多
}

@Test func lz4WorthIsStrictThreeQuarters() {
    #expect(lz4Worth(74, 100))
    #expect(!lz4Worth(75, 100))
}

@Test func rectFrameCompressesUIAndFallsBack() {
    guard lz4Available else { return }
    var buf = [UInt16](repeating: 0x18E3, count: screenW * screenH)
    for y in 300..<700 { for x in 100..<700 { buf[y * screenW + x] = 0xFFFF } }
    let r = LinkRect(x: 0, y: 300, w: 800, h: 20)
    let (t, p) = rectFrame(r, buf, lz4: true)
    #expect(t == LinkType.rectLZ4)
    #expect(Array(p[0..<8]) == Array(rectPayload(r, buf)[0..<8]))
    let rawLen: Int = Int(p[8]) | (Int(p[9]) << 8) | (Int(p[10]) << 16) | (Int(p[11]) << 24)
    #expect(rawLen == 32000)
    #expect(lz4DecodeRaw(Array(p[12...]), rawCount: 32000) == Array(rectPayload(r, buf)[8...]))
    #expect(p.count < 1000)
    // 关掉 / 1 个像素（压不小）/ 噪声：RECT
    let off = rectFrame(r, buf, lz4: false)
    #expect(off.0 == LinkType.rect && off.1 == rectPayload(r, buf))
    #expect(rectFrame(LinkRect(x: 0, y: 0, w: 1, h: 1), buf, lz4: true).0 == LinkType.rect)
    var x: UInt32 = 1
    for i in 0..<(screenW * 20) { x = x &* 1_103_515_245 &+ 12345; buf[i] = UInt16(truncatingIfNeeded: x >> 12) }
    #expect(rectFrame(LinkRect(x: 0, y: 0, w: 800, h: 20), buf, lz4: true).0 == LinkType.rect)
}

@Test func encodeBatchCountsLz4AndCapsBit() {
    guard lz4Available else { return }
    let buf = [UInt16](repeating: 0x18E3, count: screenW * screenH)
    var seq: UInt8 = 0
    let (bytes, s, n) = encodeBatchLz4(fullRects(), buf, frameID: 1, lz4: true, seq: &seq)
    #expect(n == 64 && s == 65)
    var p = FrameParser()
    let fr = p.feed(bytes)
    #expect(fr.filter { $0.type == LinkType.rectLZ4 }.count == 64 && fr.last?.type == LinkType.present)
    var seq2: UInt8 = 0
    let (plain, _) = encodeBatch(fullRects(), buf, frameID: 1, seq: &seq2)               // 旧接口：照发 RECT
    #expect(FrameParser().feedAll(plain).filter { $0.type == LinkType.rect }.count == 64)
    let h = HelloInfo([0, 0, 11, 0, 0x20, 0x03, 0x00, 0x05, 1, 72, 0xFF, 1, 0x00, 0x80, 0, 0])
    #expect(h?.hasLZ4 == true && h?.fw == 11)
    #expect(HelloInfo([0, 0, 10, 0, 0x20, 0x03, 0x00, 0x05, 1, 72, 0xFF, 0, 0x00, 0x80, 0, 0])?.hasLZ4 == false)
}

private extension FrameParser {
    func feedAll(_ b: [UInt8]) -> [LinkFrame] { var p = self; return p.feed(b) }
}

/// 推坏了一批 RECT_LZ4 之后：同一次连接、fw 没变就一直关着（keepalive HELLO 不恢复）；重新打开端口或换了 fw 再试
@Test func lz4GateStaysOffUntilReopenOrFwChange() {
    var g = Lz4Gate()
    g.hello(fw: 11)
    #expect(!g.off)
    let r1 = g.batch(nLz4: 64, status: 0, badParam: 1, badBase: 0)
    #expect(r1)      // 这次刚关
    #expect(g.off)
    let r2 = g.batch(nLz4: 0, status: 0, badParam: 1, badBase: 1)
    #expect(!r2)      // 已经关了
    g.hello(fw: 11)                                                     // keepalive / 睡醒
    #expect(g.off)
    g.hello(fw: 12)                                                     // 刷了别的固件
    #expect(!g.off)
    _ = g.batch(nLz4: 1, status: 0, badParam: 2, badBase: 1)
    #expect(g.off)
    g.reset()                                                           // 端口关了 / 重新枚举
    g.hello(fw: 12)
    #expect(!g.off)
}

/// 只归因到 LZ4：这批确实有 RECT_LZ4、PRESENT status 0（沉浸模式里回 1 是模式不对）、bad_param 涨了（CRC 错不算）
@Test func lz4GateAttribution() {
    var g = Lz4Gate()
    g.hello(fw: 11)
    let r3 = g.batch(nLz4: 64, status: 1, badParam: 64, badBase: 0)
    #expect(!r3)    // 沉浸模式
    let r4 = g.batch(nLz4: 0, status: 0, badParam: 1, badBase: 0)
    #expect(!r4)      // 这批全是 RECT
    let r5 = g.batch(nLz4: 64, status: 0, badParam: 0, badBase: 0)
    #expect(!r5)     // 只有 CRC 错之类（不在 bad_param 里）
    #expect(!g.off)
}
