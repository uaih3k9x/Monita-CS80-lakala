// Monita link 协议 v0 (docs/protocol.md) 的纯逻辑: 编帧/拆帧/CRC/RGB565/rect 打包
import Testing
import MonitaCore

@Test func crc32MatchesZlib() {
    #expect(linkCRC32(Array("123456789".utf8)) == 0xCBF4_3926)
    #expect(linkCRC32([]) == 0)
}

@Test func frameLayout() {
    let f = encodeFrame(type: LinkType.ping, seq: 7, payload: [1, 2, 3])
    #expect(Array(f[0..<6]) == [0xA5, 0x5A, 0x7F, 7, 3, 0])
    let c = linkCRC32(Array(f[2..<9]))
    #expect(Array(f[9...]) == [UInt8(c & 0xFF), UInt8((c >> 8) & 0xFF), UInt8((c >> 16) & 0xFF), UInt8(c >> 24)])
}

@Test func parserHandlesGarbageSplitAndBadCRC() {
    var bad = encodeFrame(type: LinkType.ack, seq: 2, payload: [9, 9])
    bad[bad.count - 1] ^= 0xFF
    let stream: [UInt8] = [0x00, 0xA5, 0x11] + bad + encodeFrame(type: LinkType.ack, seq: 1, payload: [7])
        + encodeFrame(type: LinkType.helloAck, seq: 3, payload: [])
    var p = FrameParser()
    var got: [LinkFrame] = []
    for b in stream { got += p.feed([b]) }
    #expect(got == [LinkFrame(type: LinkType.ack, seq: 1, payload: [7]), LinkFrame(type: LinkType.helloAck, seq: 3, payload: [])])
    #expect(p.bad == 1)
}

@Test func ackAndHelloParse() {
    var pl: [UInt8] = [0x21, 0, 0, 0]
    for v in 0..<9 { pl += [UInt8(v), 0, 0, 0] }
    let a = AckStats(pl)
    #expect(a?.forType == 0x21 && a?.rects == 4 && a?.extra == 8 && a?.crcErr == 2)
    let h = HelloInfo([0, 0, 1, 0, 0x20, 0x03, 0x00, 0x05, 1, 72, 3, 0, 0x00, 0x80, 0, 0])
    #expect(h?.width == 800 && h?.height == 1280 && h?.maxPayload == 32768 && h?.doubleBuffered == true)
}

@Test func rgb565HasRedHigh() {
    let rgba: [UInt8] = [255, 0, 0, 255, 0, 255, 0, 255, 0, 0, 255, 255]
    #expect(toRGB565(rgba, count: 3) == [0xF800, 0x07E0, 0x001F])
}

@Test func mergeTilesJoinsHorizontalRuns() {
    let tiles = [Tile(x: 0, y: 0), Tile(x: 32, y: 0), Tile(x: 96, y: 0), Tile(x: 96, y: 16), Tile(x: 128, y: 16)]
    #expect(mergeTiles(tiles) == [LinkRect(x: 0, y: 0, w: 64, h: 16), LinkRect(x: 96, y: 0, w: 32, h: 16),
                                  LinkRect(x: 96, y: 16, w: 64, h: 16)])
    // payload 只够 1 个 tile 时不合并
    #expect(mergeTiles(Array(tiles[0..<2]), maxPayload: 8 + 1024).count == 2)
}

@Test func fullRectsCoverScreenWithinPayload() {
    let rs = fullRects()
    #expect(rs.first == LinkRect(x: 0, y: 0, w: 800, h: 20))
    #expect(rs.reduce(0) { $0 + $1.h } == 1280)
    #expect(rs.allSatisfy { 8 + $0.w * $0.h * 2 <= 32768 })
}

@Test func rectPayloadAndBatch() {
    let w = 4
    let buf: [UInt16] = (0..<12).map { UInt16($0) | 0x100 }   // 4×3
    let pl = rectPayload(LinkRect(x: 1, y: 1, w: 2, h: 2), buf, width: w)
    #expect(Array(pl[0..<8]) == [1, 0, 1, 0, 2, 0, 2, 0])
    #expect(Array(pl[8...]) == [5, 1, 6, 1, 9, 1, 10, 1])
    var seq: UInt8 = 254
    let (bytes, presentSeq) = encodeBatch([LinkRect(x: 0, y: 0, w: 1, h: 1)], buf, width: w, frameID: 3, seq: &seq)
    var p = FrameParser()
    let frames = p.feed(bytes)
    #expect(frames.map(\.type) == [LinkType.rect, LinkType.present])
    #expect(frames.map(\.seq) == [255, 0] && presentSeq == 0 && seq == 0)
    #expect(frames[1].payload == [3, 0])
}

// MARK: 和固件对照出来的几处 (tools/fwsim)

@Test func diagReplyTypeAndProto() {
    #expect(LinkType.diagR == 0xD0)
    #expect(linkProtoVer == 0)
    let h = HelloInfo([1, 0, 9, 0, 0x20, 0x03, 0x00, 0x05, 1, 72, 0x7F, 0, 0x00, 0x40, 0, 0])
    #expect(h?.proto == 1 && h?.protoSupported == false)
    #expect(h?.rectPayloadMax == 16384)
    let big = HelloInfo([0, 0, 9, 0, 0x20, 0x03, 0x00, 0x05, 1, 72, 0x7F, 0, 0xFF, 0xFF, 0, 0])
    #expect(big?.protoSupported == true && big?.rectPayloadMax == linkMaxPayload)
}

@Test func diagStatusMustBeZero() {
    #expect(diagReplyOK([11, 0, 1, 0, 1, 0, 0, 0]))
    #expect(!diagReplyOK([11, 3, 0, 0]))        // EUNSUP (不认识 / 沉浸模式里)
    #expect(!diagReplyOK(nil))
    #expect(!diagReplyOK([11]))
}

@Test func presentAckNeedsStatusOK() {
    var pl: [UInt8] = [0x21, 1, 0, 0]               // 沉浸模式里 PRESENT 回 EPARAM
    for _ in 0..<9 { pl += [0, 0, 0, 0] }
    let a = AckStats(pl)!
    #expect(!presentAckOK(a, errBase: 0))
    pl[1] = 0
    #expect(presentAckOK(AckStats(pl)!, errBase: 0))
    pl[28] = 1                                       // bad_param 比基线多了
    #expect(!presentAckOK(AckStats(pl)!, errBase: 0))
}

@Test func regionCRCMatchesBytes() {
    let w = 5, h = 3
    let buf = (0..<(w * h)).map { UInt16($0 * 0x0101 + 7) }
    var b: [UInt8] = []
    for y in 1..<3 { for x in 2..<4 { let v = buf[y * w + x]; b += [UInt8(v & 0xFF), UInt8(v >> 8)] } }
    #expect(fbRegionCRC(buf, x: 2, y: 1, w: 2, h: 2, width: w) == linkCRC32(b))
}

@Test func immStripsFollowDeviceMaxPayload() {
    #expect(immStrips(rows: 0..<45, width: 800) == [0..<20, 20..<40, 40..<45])
    #expect(immStrips(rows: 100..<130, width: 800, maxPayload: 16384) == [100..<110, 110..<120, 120..<130])
}

@Test func immRectPayloadsSplitByDeviceMax() {
    let buf = (0..<(800 * 25)).map { UInt16($0 & 0xFFFF) }
    let ps = immRectPayloads(y: 1000, rows: 3..<25, visible: true, buf, width: 800, maxPayload: 16384)
    #expect(ps.count == 3)                           // 10 + 10 + 2 行
    #expect(ps.allSatisfy { $0.count <= 16384 })
    #expect(ps[0] == immRectPayload(y: 1003, x: 0, w: 800, rows: 3..<13, visible: true, buf, width: 800))
    #expect(ps[2] == immRectPayload(y: 1023, x: 0, w: 800, rows: 23..<25, visible: true, buf, width: 800))
}
