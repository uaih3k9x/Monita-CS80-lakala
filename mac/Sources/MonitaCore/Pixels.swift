// 像素与推帧格式（兼容旧链路：第三方 host 的 HTTP 接口，只做接口互操作）
//   POST /api/patch：u32 n + n × (u16 x, u16 y, 32×16 个 u16 BGR565)，全部小端
//   POST /api/frame：800×1280×3 RGB888（整帧）
// 帧校验/回读由 host 那一侧负责，客户端只需在 host 重启/设备重连/patch 失败时退回整帧（见 StatusWatch）。
// 以后走自己的 USB 协议时 tile 格式见 docs/protocol.md。
import Foundation

public let screenW = 800
public let screenH = 1280
public let tileW = 32
public let tileH = 16
/// 脏 tile 超过 120 个就推整帧
public let maxPatchTiles = 120

public struct Tile: Equatable, Hashable {
    public var x: Int
    public var y: Int
    public init(x: Int, y: Int) { self.x = x; self.y = y }
}

public func toBGR565(_ rgba: UnsafePointer<UInt8>, count: Int, into out: inout [UInt16]) {
    out.withUnsafeMutableBufferPointer { o in
        var j = 0
        for i in 0..<count {
            let r = UInt16(rgba[j]), g = UInt16(rgba[j + 1]), b = UInt16(rgba[j + 2])
            o[i] = ((b & 0xF8) << 8) | ((g & 0xFC) << 3) | (r >> 3)
            j += 4
        }
    }
}

public func toBGR565(_ rgba: [UInt8], count: Int) -> [UInt16] {
    var out = [UInt16](repeating: 0, count: count)
    rgba.withUnsafeBufferPointer { toBGR565($0.baseAddress!, count: count, into: &out) }
    return out
}

public func toRGB(_ rgba: UnsafePointer<UInt8>, count: Int) -> [UInt8] {
    var out = [UInt8](repeating: 0, count: count * 3)
    out.withUnsafeMutableBufferPointer { o in
        for i in 0..<count {
            o[i * 3] = rgba[i * 4]; o[i * 3 + 1] = rgba[i * 4 + 1]; o[i * 3 + 2] = rgba[i * 4 + 2]
        }
    }
    return out
}

public func toRGB(_ rgba: [UInt8], count: Int) -> [UInt8] {
    rgba.withUnsafeBufferPointer { toRGB($0.baseAddress!, count: count) }
}

/// 32×16 tile 差分（行优先）
public func diffTiles(_ a: [UInt16], _ b: [UInt16], width: Int = screenW, height: Int = screenH) -> [Tile] {
    var tiles: [Tile] = []
    a.withUnsafeBufferPointer { pa in
        b.withUnsafeBufferPointer { pb in
            var y0 = 0
            while y0 < height {
                var x0 = 0
                while x0 < width {
                    var dirty = false
                    for row in 0..<tileH where !dirty && y0 + row < height {
                        let base = (y0 + row) * width + x0
                        // 一行 32 个 u16 = 64 字节，直接 memcmp
                        if memcmp(pa.baseAddress! + base, pb.baseAddress! + base, tileW * 2) != 0 { dirty = true }
                    }
                    if dirty { tiles.append(Tile(x: x0, y: y0)) }
                    x0 += tileW
                }
                y0 += tileH
            }
        }
    }
    return tiles
}

public func encodePatch(_ tiles: [Tile], _ buf: [UInt16], width: Int = screenW) -> [UInt8] {
    var out = [UInt8]()
    out.reserveCapacity(4 + tiles.count * (4 + tileW * tileH * 2))
    func u16(_ v: UInt16) { out.append(UInt8(v & 0xFF)); out.append(UInt8(v >> 8)) }
    let n = UInt32(tiles.count)
    out += [UInt8(n & 0xFF), UInt8((n >> 8) & 0xFF), UInt8((n >> 16) & 0xFF), UInt8(n >> 24)]
    for t in tiles {
        u16(UInt16(t.x)); u16(UInt16(t.y))
        for row in 0..<tileH {
            let idx = (t.y + row) * width + t.x
            for k in 0..<tileW { u16(buf[idx + k]) }
        }
    }
    return out
}

public enum PushKind: Equatable { case none, patch, full }

/// maxPatch：旧 host 用 maxPatchTiles；直连传 .max（合并后的差分矩形最多和整帧一样大，没必要升级成整帧）
public func decidePush(fullNeeded: Bool, hasPrev: Bool, dirty: Int, maxPatch: Int = maxPatchTiles) -> PushKind {
    if fullNeeded || !hasPrev { return .full }
    if dirty == 0 { return .none }
    return dirty <= maxPatch ? .patch : .full
}

/// 读 /api/status 判断要不要整帧重推：
/// 设备从离线变在线、host 侧 session 变化、最近一条 patch/frame 事件失败。
public struct StatusWatch {
    var session: Int?
    var wasUp: Bool?
    public init() {}

    public mutating func update(device: String?, session: Int?, lastEvent: String?, lastOk: Int?) -> Bool {
        let up = device == "up"
        var full = false
        if up && wasUp == false { full = true }
        wasUp = up
        if (lastEvent == "patch" || lastEvent == "frame") && lastOk == 0 { full = true }
        if let s = session {
            if let old = self.session, old != s { full = true }
            self.session = s
        }
        return full
    }
}
