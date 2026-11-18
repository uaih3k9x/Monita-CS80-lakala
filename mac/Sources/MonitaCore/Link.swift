// Monita link 协议 v0 的纯逻辑 (docs/protocol.md)：编帧、流式拆帧、CRC-32、RGB565、脏 tile → 矩形打包。
// 帧：A5 5A | u8 type | u8 seq | u16 len | payload | u32 crc32(type..payload)，全部小端。
// 传输（串口）在 monita-cs80/SerialLink.swift。

public enum LinkType {
    public static let hello: UInt8 = 0x01, seg: UInt8 = 0x10, rect: UInt8 = 0x20, present: UInt8 = 0x21, copy: UInt8 = 0x22
    public static let backlight: UInt8 = 0x30, fbcrc: UInt8 = 0x40, diag: UInt8 = 0x50, ping: UInt8 = 0x7F
    public static let helloAck: UInt8 = 0x81, ack: UInt8 = 0xFE
}

private let crcTable: [UInt32] = (0..<256).map { i -> UInt32 in
    var c = UInt32(i)
    for _ in 0..<8 { c = (c & 1) != 0 ? (c >> 1) ^ 0xEDB8_8320 : c >> 1 }
    return c
}

/// CRC-32/IEEE（= zlib.crc32）
public func linkCRC32<C: Collection>(_ bytes: C, seed: UInt32 = 0) -> UInt32 where C.Element == UInt8 {
    var c = ~seed
    for b in bytes { c = (c >> 8) ^ crcTable[Int((c ^ UInt32(b)) & 0xFF)] }
    return ~c
}

@inline(__always) func put16(_ out: inout [UInt8], _ v: Int) { out.append(UInt8(v & 0xFF)); out.append(UInt8((v >> 8) & 0xFF)) }
@inline(__always) func put32(_ out: inout [UInt8], _ v: UInt32) { for s in stride(from: 0, to: 32, by: 8) { out.append(UInt8((v >> UInt32(s)) & 0xFF)) } }

public func encodeFrame(type: UInt8, seq: UInt8, payload: [UInt8]) -> [UInt8] {
    var out: [UInt8] = [0xA5, 0x5A, type, seq]
    out.reserveCapacity(payload.count + 10)
    put16(&out, payload.count)
    out += payload
    put32(&out, linkCRC32(out[2...]))
    return out
}

public struct LinkFrame: Equatable {
    public var type: UInt8, seq: UInt8, payload: [UInt8]
    public init(type: UInt8, seq: UInt8, payload: [UInt8]) { self.type = type; self.seq = seq; self.payload = payload }
}

/// 流式拆帧：坏帧（CRC 错 / 长度超限）丢弃并从下一个字节重新找 A5 5A
public struct FrameParser {
    var buf: [UInt8] = []
    public private(set) var bad = 0
    let maxPayload: Int
    public init(maxPayload: Int = 65535) { self.maxPayload = maxPayload }

    public mutating func feed(_ data: [UInt8]) -> [LinkFrame] {
        buf += data
        var out: [LinkFrame] = []
        var i = 0
        while true {
            while i + 1 < buf.count && !(buf[i] == 0xA5 && buf[i + 1] == 0x5A) { i += 1 }
            if i + 6 > buf.count { break }
            let n = Int(buf[i + 4]) | Int(buf[i + 5]) << 8
            if n > maxPayload { bad += 1; i += 1; continue }
            if i + 10 + n > buf.count { break }
            let body = buf[(i + 2)..<(i + 6 + n)]
            let c = UInt32(buf[i + 6 + n]) | UInt32(buf[i + 7 + n]) << 8 | UInt32(buf[i + 8 + n]) << 16 | UInt32(buf[i + 9 + n]) << 24
            if c != linkCRC32(body) { bad += 1; i += 1; continue }
            out.append(LinkFrame(type: buf[i + 2], seq: buf[i + 3], payload: Array(buf[(i + 6)..<(i + 6 + n)])))
            i += 10 + n
        }
        buf.removeFirst(min(i, buf.count))
        return out
    }
}

@inline(__always) func rd16(_ p: [UInt8], _ o: Int) -> Int { Int(p[o]) | Int(p[o + 1]) << 8 }
@inline(__always) func rd32(_ p: [UInt8], _ o: Int) -> UInt32 {
    UInt32(p[o]) | UInt32(p[o + 1]) << 8 | UInt32(p[o + 2]) << 16 | UInt32(p[o + 3]) << 24
}

/// ACK（0xFE）：u8 for_type u8 status u16 0 | 9 × u32
public struct AckStats: Equatable {
    public var forType: UInt8, status: UInt8
    public var rxBytes, frames, crcErr, lenErr, rects, presents, badParam, uptime, extra: UInt32
    public init?(_ p: [UInt8]) {
        guard p.count >= 40 else { return nil }
        forType = p[0]; status = p[1]
        rxBytes = rd32(p, 4); frames = rd32(p, 8); crcErr = rd32(p, 12); lenErr = rd32(p, 16)
        rects = rd32(p, 20); presents = rd32(p, 24); badParam = rd32(p, 28); uptime = rd32(p, 32); extra = rd32(p, 36)
    }
}

/// HELLO_ACK（0x81）
public struct HelloInfo: Equatable {
    public var proto, fw, width, height, pixfmt, segBytes, caps, maxPayload: Int
    public var doubleBuffered: Bool { caps & 2 != 0 }
    public var hasCopy: Bool { caps & 4 != 0 }
    public init?(_ p: [UInt8]) {
        guard p.count >= 16 else { return nil }
        proto = rd16(p, 0); fw = rd16(p, 2); width = rd16(p, 4); height = rd16(p, 6)
        pixfmt = Int(p[8]); segBytes = Int(p[9]); caps = rd16(p, 10); maxPayload = rd16(p, 12)
    }
}

/// RGBA8888 → RGB565（R 在高位，R05 大屏的原生格式）
public func toRGB565(_ rgba: UnsafePointer<UInt8>, count: Int, into out: inout [UInt16]) {
    out.withUnsafeMutableBufferPointer { o in
        var j = 0
        for i in 0..<count {
            let r = UInt16(rgba[j]), g = UInt16(rgba[j + 1]), b = UInt16(rgba[j + 2])
            o[i] = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            j += 4
        }
    }
}

public func toRGB565(_ rgba: [UInt8], count: Int) -> [UInt16] {
    var out = [UInt16](repeating: 0, count: count)
    rgba.withUnsafeBufferPointer { toRGB565($0.baseAddress!, count: count, into: &out) }
    return out
}

public struct LinkRect: Equatable {
    public var x, y, w, h: Int
    public init(x: Int, y: Int, w: Int, h: Int) { self.x = x; self.y = y; self.w = w; self.h = h }
}

public let linkMaxPayload = 32768

/// diffTiles 的结果（行优先）里，同一 tile 行相邻的脏 tile 合并成一个矩形；单个矩形不超过 maxPayload
public func mergeTiles(_ tiles: [Tile], maxPayload: Int = linkMaxPayload) -> [LinkRect] {
    let maxTiles = max(1, (maxPayload - 8) / (tileW * tileH * 2))
    var out: [LinkRect] = []
    for t in tiles {
        if var last = out.last, last.y == t.y, last.x + last.w == t.x, last.w / tileW < maxTiles {
            last.w += tileW
            out[out.count - 1] = last
        } else {
            out.append(LinkRect(x: t.x, y: t.y, w: tileW, h: tileH))
        }
    }
    return out
}

/// 整帧：整行宽的条带，每条尽量多行
public func fullRects(width: Int = screenW, height: Int = screenH, maxPayload: Int = linkMaxPayload) -> [LinkRect] {
    let rows = max(1, (maxPayload - 8) / (width * 2))
    return stride(from: 0, to: height, by: rows).map { LinkRect(x: 0, y: $0, w: width, h: min(rows, height - $0)) }
}

public func rectPayload(_ r: LinkRect, _ buf: [UInt16], width: Int = screenW) -> [UInt8] {
    var out: [UInt8] = []
    out.reserveCapacity(8 + r.w * r.h * 2)
    put16(&out, r.x); put16(&out, r.y); put16(&out, r.w); put16(&out, r.h)
    buf.withUnsafeBufferPointer { p in
        for row in 0..<r.h {
            let base = (r.y + row) * width + r.x
            for k in 0..<r.w { let v = p[base + k]; out.append(UInt8(v & 0xFF)); out.append(UInt8(v >> 8)) }
        }
    }
    return out
}

public func copyPayload(_ c: LinkCopy) -> [UInt8] {
    var out: [UInt8] = []
    for v in [c.sx, c.sy, c.w, c.h, c.dx, c.dy] { put16(&out, v) }
    return out
}

/// 一批 COPY（先）+ RECT + PRESENT 的线上字节；返回 PRESENT 的 seq（等它的 ACK）。
/// COPY 必须在 RECT 前面：RECT 是对着"COPY 之后的画面"算出来的
public func encodeBatch(_ rects: [LinkRect], _ buf: [UInt16], width: Int = screenW, copies: [LinkCopy] = [],
                        frameID: Int, seq: inout UInt8) -> ([UInt8], UInt8) {
    var out: [UInt8] = []
    for c in copies {
        seq &+= 1
        out += encodeFrame(type: LinkType.copy, seq: seq, payload: copyPayload(c))
    }
    for r in rects {
        seq &+= 1
        out += encodeFrame(type: LinkType.rect, seq: seq, payload: rectPayload(r, buf, width: width))
    }
    seq &+= 1
    var pl: [UInt8] = []
    put16(&pl, frameID & 0xFFFF)
    out += encodeFrame(type: LinkType.present, seq: seq, payload: pl)
    return (out, seq)
}
