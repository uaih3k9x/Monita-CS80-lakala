// bench.py 用: Apple Compression 的 COMPRESSION_LZ4_RAW (Mac 上位机用的编码器) 压一串等长的条带。
//   apple_lz4 IN OUT CHUNK   IN = 若干个 CHUNK 字节的原始条带; OUT = 每条 [u32 len | LZ4 块]; stdout 打印总编码耗时 ns
import Compression
import Foundation

let a = CommandLine.arguments
let src = [UInt8](try! Data(contentsOf: URL(fileURLWithPath: a[1])))
let chunk = Int(a[3])!
var out: [UInt8] = []
var dst = [UInt8](repeating: 0, count: chunk + 4096)
var ns: UInt64 = 0
for off in stride(from: 0, to: src.count, by: chunk) {
    let s = Array(src[off..<min(off + chunk, src.count)])
    let t0 = DispatchTime.now().uptimeNanoseconds
    let n = dst.withUnsafeMutableBufferPointer { d in
        s.withUnsafeBufferPointer { compression_encode_buffer(d.baseAddress!, d.count, $0.baseAddress!, $0.count, nil, COMPRESSION_LZ4_RAW) }
    }
    ns += DispatchTime.now().uptimeNanoseconds - t0
    for k in 0..<4 { out.append(UInt8((n >> (8 * k)) & 0xFF)) }
    out += dst[0..<n]
}
try! Data(out).write(to: URL(fileURLWithPath: a[2]))
print(ns)
