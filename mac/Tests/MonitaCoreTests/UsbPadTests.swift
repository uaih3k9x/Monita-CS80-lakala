// "PRESENT 没有应答"（fw v11 + LZ4，--stress 每 ~1000 批一次）回归：一批（COPY / RECT / RECT_LZ4 + PRESENT）一次写出去，
// 线上长度是 512 的倍数时最后一包是满包、没有短包收尾，模组把这批的尾巴（含 PRESENT）压在 USB 里不交给固件。
// 关 LZ4 时 --stress 的批长永远不是 512 的倍数（矩形都是 16 的倍数，帧开销固定），开 LZ4 后长度任意，约 1/512 中招。
import Testing
import MonitaCore

/// 设备侧 bulk OUT 的样子（最坏情况）：按最大包长切包，数据只有在收到一个短包（含 ZLP）时才连同前面攒着的满包一起交给应用
private struct BulkOutModel {
    let mps: Int
    var pending: [UInt8] = [], delivered: [UInt8] = []
    init(mps: Int) { self.mps = mps }
    mutating func write(_ b: [UInt8]) {
        var i = 0
        repeat {
            let pkt = b[i..<min(i + mps, b.count)]
            pending += pkt; i += pkt.count
            if pkt.count < mps { delivered += pending; pending = [] }
        } while i < b.count
    }
}

/// 模型本身：满包结尾的写卡住，下一次短写放出来
@Test func bulkOutModelHoldsFullPacketTail() {
    var m = BulkOutModel(mps: 512)
    m.write([UInt8](repeating: 1, count: 1024))
    #expect(m.delivered.isEmpty)
    m.write([2])
    #expect(m.delivered.count == 1025)
}

/// 补齐只在 64 的倍数时补一个 0x00，别的长度原样
@Test func usbPaddedOnlyOnPacketMultiples() {
    #expect(usbPadded([UInt8](repeating: 7, count: 512)).count == 513)
    #expect(usbPadded([UInt8](repeating: 7, count: 64)).count == 65)
    #expect(usbPadded([UInt8](repeating: 7, count: 511)).count == 511)
    #expect(usbPadded([]).isEmpty)
    // 补的字节夹在帧之间，上位机 / 固件拆帧器照样拆出原来的帧（固件侧 fw/link/test_proto.c 也测了不计错）
    let f = encodeFrame(type: LinkType.ping, seq: 3, payload: [UInt8](repeating: 0xA5, count: 54))   // 64 字节
    var p = FrameParser()
    #expect(p.feed(usbPadded(f) + f) == [LinkFrame(type: LinkType.ping, seq: 3, payload: [UInt8](repeating: 0xA5, count: 54))] + [LinkFrame(type: LinkType.ping, seq: 3, payload: [UInt8](repeating: 0xA5, count: 54))])
    #expect(p.bad == 0)
}

/// --stress 同一个生成器 / 种子的 COPY 滚动批（开 LZ4）：每批写完，设备那边都要能拆出这批的 PRESENT。
/// 修之前（不补齐）这里有批次卡住；关 LZ4 时一批都不会卡（对照）
@Test func stressBatchesNeverStallPresent() {
    guard lz4Available else { return }
    for lz4 in [true, false] {
        var gen = StressGen(), seq: UInt8 = 0
        var stuck = 0, raw512 = 0
        for _ in 0..<2000 {
            let (rs, c) = gen.scroll()
            let (b, s, _) = encodeBatchLz4(rs, gen.fb, copies: [c], frameID: 1, lz4: lz4, seq: &seq)
            if b.count % 512 == 0 { raw512 += 1 }
            var m = BulkOutModel(mps: 512), p = FrameParser()
            m.write(usbPadded(b))
            if !p.feed(m.delivered).contains(where: { $0.type == LinkType.present && $0.seq == s }) { stuck += 1 }
        }
        if lz4 { #expect(raw512 > 0, "这个种子下应该有正好 512 倍数的批（不然测试没测到东西）") } else { #expect(raw512 == 0) }
        #expect(stuck == 0, "lz4=\(lz4)：\(stuck) 批的 PRESENT 卡在设备 USB 里（批长 512 的倍数 \(raw512) 批）")
    }
}
