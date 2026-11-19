// fw v11 RECT_LZ4（0x23，docs/protocol.md）：u16 x, y, w, h + u32 raw_len（= w*h*2）+ LZ4 raw block（不带 frame 头）。
// 压缩用 Apple Compression 的 COMPRESSION_LZ4_RAW：输出就是标准 LZ4 块（Lz4Tests 对过 liblz4 编的块，
// tools/fwsim 对拍里固件的解码器 fw/link/lz4.h 解 Apple 编的块）。没有 Compression 框架的平台照发 RECT。
#if canImport(Compression)
import Compression
#endif

/// 这个平台能不能编 RECT_LZ4
public var lz4Available: Bool {
    #if canImport(Compression)
    return true
    #else
    return false
    #endif
}

/// RECT_LZ4 的开关（三个上位机同一语义，Python 版 tools/host-ref/monita_link.py lz4_batch_bad）：
/// 一批确实有 RECT_LZ4、PRESENT status 0（沉浸模式里 RECT / RECT_LZ4 一律被拒、PRESENT 回 1，那是模式不对）、
/// bad_param 比基线涨了（RECT_LZ4 坏块计在这里；CRC 错是链路毛刺，不算）就关。关了之后同一次连接里 fw 没变就一直关着：
/// keepalive / 睡醒的 HELLO 不恢复（编解码真不一致时不会每次 HELLO 后再坏一批）；端口重新打开（reset）或 HELLO 报的 fw 变了才再试
public struct Lz4Gate {
    /// 关掉时设备的 fw；nil = 开着
    public private(set) var offFw: Int?
    private var fw: Int?
    public init() {}
    public var off: Bool { offFw != nil }
    /// HELLO 成功
    public mutating func hello(fw f: Int) {
        fw = f
        if offFw != f { offFw = nil }
    }
    /// 端口关了（下次重新打开 = 设备可能重新枚举过）
    public mutating func reset() { offFw = nil; fw = nil }
    /// 一批推完（PRESENT 的 ACK）；返回 true = 这次刚关掉
    public mutating func batch(nLz4: Int, status: UInt8, badParam: UInt32, badBase: UInt32) -> Bool {
        guard !off, nLz4 > 0, status == 0, badParam != badBase else { return false }
        offFw = fw ?? -1
        return true
    }
}

/// 压缩后严格小于原始的 3/4 才值得发 RECT_LZ4（三个上位机同一条规则）
public func lz4Worth(_ compressed: Int, _ raw: Int) -> Bool { compressed * 4 < raw * 3 }

/// LZ4 raw block；压出来超过 limit 字节（或压不了）返回 nil
public func lz4EncodeRaw(_ src: [UInt8], limit: Int) -> [UInt8]? {
    #if canImport(Compression)
    guard !src.isEmpty, limit > 0 else { return nil }
    var dst = [UInt8](repeating: 0, count: limit)
    let n = dst.withUnsafeMutableBufferPointer { d in
        src.withUnsafeBufferPointer { s in compression_encode_buffer(d.baseAddress!, d.count, s.baseAddress!, s.count, nil, COMPRESSION_LZ4_RAW) }
    }
    return n > 0 ? Array(dst[0..<n]) : nil
    #else
    return nil
    #endif
}

/// 解 LZ4 raw block（单测对格式用）；解出来不是正好 rawCount 字节返回 nil
public func lz4DecodeRaw(_ src: [UInt8], rawCount: Int) -> [UInt8]? {
    #if canImport(Compression)
    guard !src.isEmpty, rawCount > 0 else { return nil }
    var dst = [UInt8](repeating: 0, count: rawCount + 1)
    let n = dst.withUnsafeMutableBufferPointer { d in
        src.withUnsafeBufferPointer { s in compression_decode_buffer(d.baseAddress!, d.count, s.baseAddress!, s.count, nil, COMPRESSION_LZ4_RAW) }
    }
    return n == rawCount ? Array(dst[0..<n]) : nil
    #else
    return nil
    #endif
}

/// 一个矩形编成 (type, payload)：lz4 且压缩后 < 3/4 → RECT_LZ4，否则 RECT。payload 不会比同一块的 RECT 长
public func rectFrame(_ r: LinkRect, _ buf: [UInt16], width: Int = screenW, lz4: Bool) -> (UInt8, [UInt8]) {
    let pl = rectPayload(r, buf, width: width)
    guard lz4, pl.count > 8 else { return (LinkType.rect, pl) }
    let raw = Array(pl[8...])
    // 上限取"刚好不值得"的长度：压不到这么小 Apple 直接返回 0
    guard let c = lz4EncodeRaw(raw, limit: (raw.count * 3 - 1) / 4), lz4Worth(c.count, raw.count), 12 + c.count <= pl.count else {
        return (LinkType.rect, pl)
    }
    var out = Array(pl[0..<8])
    put32(&out, UInt32(raw.count))
    return (LinkType.rectLZ4, out + c)
}

/// encodeBatch 的 RECT_LZ4 版：多返回这一批用了几条 RECT_LZ4（推坏了且 > 0 时调用方关 LZ4，见 Lz4Gate）
public func encodeBatchLz4(_ rects: [LinkRect], _ buf: [UInt16], width: Int = screenW, copies: [LinkCopy] = [],
                           frameID: Int, lz4: Bool, seq: inout UInt8) -> ([UInt8], UInt8, Int) {
    var out: [UInt8] = []
    var n = 0
    for c in copies {
        seq &+= 1
        out += encodeFrame(type: LinkType.copy, seq: seq, payload: copyPayload(c))
    }
    for r in rects {
        seq &+= 1
        let (t, p) = rectFrame(r, buf, width: width, lz4: lz4)
        if t == LinkType.rectLZ4 { n += 1 }
        out += encodeFrame(type: t, seq: seq, payload: p)
    }
    seq &+= 1
    var pl: [UInt8] = []
    put16(&pl, frameID & 0xFFFF)
    out += encodeFrame(type: LinkType.present, seq: seq, payload: pl)
    return (out, seq, n)
}
