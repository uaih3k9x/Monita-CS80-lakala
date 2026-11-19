// DIAG 33（fw v11，设备侧 LZ4 自测）：回包解析和打印格式，和 tools/host-ref/test_monita_link.py Lz4BenchTests 同一组数字、逐行一样
import Testing
import MonitaCore

private func diagR(_ w: [UInt32], status: UInt8 = 0) -> [UInt8] {
    var b: [UInt8] = [33, status, UInt8(w.count & 0xFF), UInt8(w.count >> 8)]
    for v in w { for s in stride(from: 0, to: 32, by: 8) { b.append(UInt8((v >> UInt32(s)) & 0xFF)) } }
    return b
}
private let w16: [UInt32] = [3 | 3 << 16, 32000 | 1319 << 16, 100, 110, 130, 90, 95, 99, 400, 420, 450, 500, 515, 560, 2048000, 31250]

@Test func lz4BenchRequest() {
    #expect(lz4BenchRequest(n: 3) == [33, 3])
    #expect(lz4BenchRequest(n: 0) == [33, 1])
    #expect(lz4BenchRequest(n: 999) == [33, 255])
}

@Test func lz4BenchParse() {
    let d = Lz4BenchResult(diagReply: diagR(w16))!
    #expect(d.n == 3 && d.ok && d.fb && d.raw == 32000 && d.comp == 1319)
    #expect(d.decFlash == [100, 110, 130] && d.decRAM == [90, 95, 99] && d.copy == [400, 420, 450] && d.total == [500, 515, 560])
    #expect(d.memcpyBytes == 2048000 && d.memcpyUs == 31250)
    #expect(Lz4BenchResult(diagReply: diagR(Array(w16.prefix(15)))) == nil)
    #expect(Lz4BenchResult(diagReply: diagR([], status: 3)) == nil)
    #expect(Lz4BenchResult(diagReply: nil) == nil)
}

@Test func lz4BenchFormat() {
    let lines = formatLz4Bench(Lz4BenchResult(diagReply: diagR(w16))!).split(separator: "\n").map(String.init)
    #expect(lines == [
        "DIAG 33 LZ4 自测: 3 次, 测试条带 32000 B (LZ4 1319 B), 解码结果 OK, 后台缓冲 有",
        "  解码 (输入在 Flash)      min     100  avg     110  max     130 us",
        "  解码 (输入在 RAM)        min      90  avg      95  max      99 us",
        "  暂存区 -> 帧缓冲         min     400  avg     420  max     450 us",
        "  合计 (RAM 解 + 落帧)     min     500  avg     515  max     560 us",
        "  x64 条 (约一整帧, 按 avg): 解码 6.1 ms + 落帧 26.9 ms = 33.0 ms (合计实测 33.0 ms)",
        "  CPU 整屏拷贝 2048000 B: 31250 us (65.5 MB/s)",
    ])
}

@Test func lz4BenchFormatSingleBufferAndBad() {
    let w: [UInt32] = [8, 32000 | 1319 << 16, 1, 2, 3, 1, 2, 3] + [UInt32](repeating: 0, count: 8)
    let lines = formatLz4Bench(Lz4BenchResult(diagReply: diagR(w))!).split(separator: "\n").map(String.init)
    #expect(lines[0] == "DIAG 33 LZ4 自测: 8 次, 测试条带 32000 B (LZ4 1319 B), 解码结果 错, 后台缓冲 无")
    #expect(Array(lines[3...]) == ["  暂存区 -> 帧缓冲         (单缓冲: 没有不显示的那块, 没测)",
                                   "  合计 (RAM 解 + 落帧)     (单缓冲: 没有不显示的那块, 没测)",
                                   "  x64 条 (约一整帧, 按 avg): 解码 0.1 ms",
                                   "  CPU 整屏拷贝: (单缓冲, 没测)"])
    #expect(lz4BenchUnsupported == "固件不支持 DIAG 33 (要 fw v11 带 LZ4 自测的这一版; 沉浸模式里也会被拒)")
}
