// DIAG 33（fw v11）：设备拿内嵌测试条带自测 LZ4 解码 / 落帧（fw/link/dispatch.h lz4_bench，回包见 docs/protocol.md "DIAG"）。
// 打印格式和 tools/host-ref/monita_link.py format_lz4bench 逐行一样（三个上位机的 --bench-device 共用，对拍按版式比）。
import Foundation

public let dgLz4Bench: UInt8 = 33

/// DIAG 33 请求：u8 33, u8 n（1..255）
public func lz4BenchRequest(n: Int) -> [UInt8] { [dgLz4Bench, UInt8(max(1, min(255, n)))] }

public struct Lz4BenchResult: Equatable {
    public var n: Int, ok: Bool, fb: Bool, raw: Int, comp: Int
    /// 每组 [min, avg, max]（us）
    public var decFlash: [UInt32], decRAM: [UInt32], copy: [UInt32], total: [UInt32]
    public var memcpyBytes: UInt32, memcpyUs: UInt32

    /// DIAG_R 载荷（u8 op, u8 status, u16 n, n × u32）；op 不是 33、status ≠ 0、不是 16 个字都返回 nil
    public init?(diagReply r: [UInt8]?) {
        guard let r, r.count >= 4, r[0] == dgLz4Bench, r[1] == 0, rd16(r, 2) == 16, r.count >= 4 + 64 else { return nil }
        let w = (0..<16).map { rd32(r, 4 + 4 * $0) }
        n = Int(w[0] & 0xFFFF); ok = (w[0] >> 16) & 1 == 1; fb = (w[0] >> 17) & 1 == 1
        raw = Int(w[1] & 0xFFFF); comp = Int(w[1] >> 16)
        decFlash = Array(w[2..<5]); decRAM = Array(w[5..<8]); copy = Array(w[8..<11]); total = Array(w[11..<14])
        memcpyBytes = w[14]; memcpyUs = w[15]
    }
}

public let lz4BenchUnsupported = "固件不支持 DIAG 33 (要 fw v11 带 LZ4 自测的这一版; 沉浸模式里也会被拒)"

public func formatLz4Bench(_ d: Lz4BenchResult) -> String {
    var out = ["DIAG 33 LZ4 自测: \(d.n) 次, 测试条带 \(d.raw) B (LZ4 \(d.comp) B), 解码结果 \(d.ok ? "OK" : "错"), 后台缓冲 \(d.fb ? "有" : "无")"]
    let rows: [(String, [UInt32], Bool)] = [("  解码 (输入在 Flash)      ", d.decFlash, true), ("  解码 (输入在 RAM)        ", d.decRAM, true),
                                            ("  暂存区 -> 帧缓冲         ", d.copy, false), ("  合计 (RAM 解 + 落帧)     ", d.total, false)]
    for (label, g, always) in rows {
        out.append(always || d.fb ? label + String(format: "min %7u  avg %7u  max %7u us", g[0], g[1], g[2])
                                  : label + "(单缓冲: 没有不显示的那块, 没测)")
    }
    let dec = Double(d.decRAM[1]) * 64 / 1000
    if d.fb {
        let cp = Double(d.copy[1]) * 64 / 1000
        out.append(String(format: "  x64 条 (约一整帧, 按 avg): 解码 %.1f ms + 落帧 %.1f ms = %.1f ms (合计实测 %.1f ms)",
                          dec, cp, dec + cp, Double(d.total[1]) * 64 / 1000))
        out.append(String(format: "  CPU 整屏拷贝 %u B: %u us (%.1f MB/s)", d.memcpyBytes, d.memcpyUs,
                          Double(d.memcpyBytes) / Double(max(d.memcpyUs, 1))))
    } else {
        out.append(String(format: "  x64 条 (约一整帧, 按 avg): 解码 %.1f ms", dec))
        out.append("  CPU 整屏拷贝: (单缓冲, 没测)")
    }
    return out.joined(separator: "\n")
}
