// Monita link 协议 v0 的纯逻辑 (docs/protocol.md)：编帧、流式拆帧、CRC-32、RGB565、脏 tile → 矩形打包。
// 帧：A5 5A | u8 type | u8 seq | u16 len | payload | u32 crc32(type..payload)，全部小端。
// 传输（串口）在 monita-cs80/SerialLink.swift。
import Foundation

public enum LinkType {
    public static let hello: UInt8 = 0x01, seg: UInt8 = 0x10, rect: UInt8 = 0x20, present: UInt8 = 0x21, copy: UInt8 = 0x22
    /// fw v11：u16 x, y, w, h + u32 raw_len + LZ4 raw block（caps bit8，MonitaCore/Lz4.swift）；同 RECT 不应答
    public static let rectLZ4: UInt8 = 0x23
    public static let backlight: UInt8 = 0x30, fbcrc: UInt8 = 0x40, diag: UInt8 = 0x50, ping: UInt8 = 0x7F
    public static let led: UInt8 = 0x31
    /// fw v10：空 → ACK，设备关背光 / 段码屏 / 灯；之后任何一帧叫醒（caps bit7）
    public static let sleep: UInt8 = 0x32
    public static let helloAck: UInt8 = 0x81, diagR: UInt8 = 0xD0, ack: UInt8 = 0xFE
    /// 设备主动上报的按键事件（fw v8），seq = 0，主机不应答
    public static let key: UInt8 = 0x90
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

/// 这份实现认的 proto_ver（docs/protocol.md：以后不兼容的改动会升它）
public let linkProtoVer = 0

/// HELLO_ACK（0x81）
public struct HelloInfo: Equatable {
    public var proto, fw, width, height, pixfmt, segBytes, caps, maxPayload: Int
    public var doubleBuffered: Bool { caps & 2 != 0 }
    public var hasCopy: Bool { caps & 4 != 0 }
    public var hasKeys: Bool { caps & 0x10 != 0 }
    public var hasLed: Bool { caps & 0x20 != 0 }
    /// fw v10：SLEEP + 主机 5 s 没帧回开机画面
    public var hasSleep: Bool { caps & 0x80 != 0 }
    /// fw v11：RECT_LZ4
    public var hasLZ4: Bool { caps & 0x100 != 0 }
    public var protoSupported: Bool { proto == linkProtoVer }
    /// 单条 RECT / IMM_RECT 的 payload 上限：设备给的 max_payload，但不超过本实现按 32 KiB 切的条带
    public var rectPayloadMax: Int { min(maxPayload, linkMaxPayload) }
    public init?(_ p: [UInt8]) {
        guard p.count >= 16 else { return nil }
        proto = rd16(p, 0); fw = rd16(p, 2); width = rd16(p, 4); height = rd16(p, 6)
        pixfmt = Int(p[8]); segBytes = Int(p[9]); caps = rd16(p, 10); maxPayload = rd16(p, 12)
    }
}

/// HELLO 的 6 字节：u32 unix 秒 + i16 本地时间 - UTC（分钟，东正西负；夏令时按 now 那一刻）
public func helloPayload(_ now: Date, timeZone: TimeZone = .current) -> [UInt8] {
    let t = UInt32(truncatingIfNeeded: Int(now.timeIntervalSince1970))
    let tz = UInt16(bitPattern: Int16(timeZone.secondsFromGMT(for: now) / 60))
    return [UInt8(t & 0xFF), UInt8(t >> 8 & 0xFF), UInt8(t >> 16 & 0xFF), UInt8(t >> 24), UInt8(tz & 0xFF), UInt8(tz >> 8)]
}

/// 同上，时区按 IANA 名字（单测用，测试目标里不 import Foundation）；不认识的名字返回 nil
public func helloPayload(unixTime: Double, zone: String) -> [UInt8]? {
    TimeZone(identifier: zone).map { helloPayload(Date(timeIntervalSince1970: unixTime), timeZone: $0) }
}

/// PRESENT 的 ACK：status 要是 0（沉浸模式里回 1），出错计数（CRC / 长度 / 被拒）也没比基线多
public func presentAckOK(_ a: AckStats, errBase: UInt32) -> Bool {
    a.status == 0 && a.crcErr + a.lenErr + a.badParam == errBase
}

/// DIAG_R（u8 op, u8 status, …）：status 0 才算成功；不认识的 op、沉浸模式里的 DIAG 回 3
public func diagReplyOK(_ r: [UInt8]?) -> Bool {
    guard let r, r.count >= 2 else { return false }
    return r[1] == 0
}

/// 宽 width 的 RGB565 图里一块矩形的 CRC-32（行优先、小端字节，同固件 FBCRC）
public func fbRegionCRC(_ buf: [UInt16], x: Int, y: Int, w: Int, h: Int, width: Int = screenW) -> UInt32 {
    var b: [UInt8] = []
    b.reserveCapacity(w * h * 2)
    for r in y..<(y + h) {
        for k in (r * width + x)..<(r * width + x + w) { let v = buf[k]; b.append(UInt8(v & 0xFF)); b.append(UInt8(v >> 8)) }
    }
    return linkCRC32(b)
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
    let (out, s, _) = encodeBatchLz4(rects, buf, width: width, copies: copies, frameID: frameID, lz4: false, seq: &seq)
    return (out, s)
}

/// 每次写 USB 之前补齐（DeviceLink.write）：长度正好是 64 的倍数（FS 64 / HS 512 / SS 1024 最大包长的公约数）就在末尾补一个 0x00。
/// 原因：bulk OUT 的最后一包是满包时传输没有"短包"收尾；这个模组（Quectel 2c7c，Quectel 的 Linux 驱动指南要求这种长度补 ZLP）
/// 收到满包会一直等下一个短包才把数据交给 ql_uart_read，于是一批的 PRESENT 卡在设备的 USB 里，直到下一次写（重新 HELLO）才放出来。
/// 0x00 落在两帧之间：固件拆帧器在找同步字（lk_feed SYNC0）时直接跳过，不计错。
public let usbPadUnit = 64
public func usbPadded(_ b: [UInt8]) -> [UInt8] { !b.isEmpty && b.count % usbPadUnit == 0 ? b + [0] : b }
