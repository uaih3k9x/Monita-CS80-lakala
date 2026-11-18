// 直连 R05 固件，走 docs/protocol.md 的 v0 协议。
// 编帧/拆帧/打包在 MonitaCore/Link.swift；这里是传输抽象 LinkPort、termios 串口（Linux / 有串口驱动时）
// 和"发一批 + 等 PRESENT 的 ACK"。macOS 上模组接口没有驱动、没有 /dev/cu.*，走 UsbLink.swift（IOKit bulk）。
// 线程：所有调用经 lock 串行化（渲染线程推帧，link 线程负责重连和心跳）。
import Foundation
import MonitaCore

/// 字节流传输：写完才返回；读到点什么就返回，超时 []，断开 nil
protocol LinkPort: AnyObject {
    func write(_ bytes: [UInt8]) -> Bool
    func read(timeoutMs: Int32) -> [UInt8]?
}

final class SerialPort: LinkPort {
    let fd: Int32

    init?(path: String) {
        let fd = open(path, O_RDWR | O_NOCTTY | O_NONBLOCK)
        if fd < 0 { return nil }
        var t = termios()
        if tcgetattr(fd, &t) != 0 { close(fd); return nil }
        cfmakeraw(&t)
        t.c_cflag |= tcflag_t(CLOCAL | CREAD)
        cfsetspeed(&t, speed_t(B115200))   // USB CDC 不看波特率，设一个合法值就行
        if tcsetattr(fd, TCSANOW, &t) != 0 { close(fd); return nil }
        _ = fcntl(fd, F_SETFL, 0)          // 之后用阻塞写 + poll 读
        tcflush(fd, TCIOFLUSH)
        self.fd = fd
    }

    deinit { close(fd) }

    func write(_ bytes: [UInt8]) -> Bool {
        var off = 0
        while off < bytes.count {
            let n = bytes.withUnsafeBytes { Darwin.write(fd, $0.baseAddress! + off, bytes.count - off) }
            if n < 0 { if errno == EINTR || errno == EAGAIN { continue }; return false }
            off += n
        }
        return true
    }

    /// 读到点什么就返回；超时返回 []，出错返回 nil（设备拔了）
    func read(timeoutMs: Int32) -> [UInt8]? {
        var p = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
        let r = poll(&p, 1, timeoutMs)
        if r < 0 { return errno == EINTR ? [] : nil }
        if r == 0 { return [] }
        if p.revents & Int16(POLLHUP | POLLERR | POLLNVAL) != 0 { return nil }
        var buf = [UInt8](repeating: 0, count: 4096)
        let n = buf.withUnsafeMutableBytes { Darwin.read(fd, $0.baseAddress!, 4096) }
        if n <= 0 { return nil }
        return Array(buf[0..<n])
    }
}

final class DeviceLink {
    let name: String
    private let open: () -> LinkPort?
    private let lock = NSLock()
    private var port: LinkPort?
    private var parser = FrameParser()
    private var seq: UInt8 = 0
    private var errBase: UInt32 = 0
    private(set) var info: HelloInfo?
    var isUp: Bool { lock.lock(); defer { lock.unlock() }; return port != nil }

    init(name: String, open: @escaping () -> LinkPort?) { self.name = name; self.open = open }

    private func drop(_ why: String) {
        if port != nil { log("设备断开：\(why)") }
        port = nil; info = nil
    }

    private func wait(type: UInt8, seq want: UInt8, timeoutMs: Int) -> [UInt8]? {
        let end = Date().addingTimeInterval(Double(timeoutMs) / 1000)
        while Date() < end {
            guard let port, let data = port.read(timeoutMs: 50) else { drop("读失败"); return nil }
            for f in parser.feed(data) where f.type == type && f.seq == want { return f.payload }
        }
        return nil
    }

    private func call(_ type: UInt8, _ payload: [UInt8] = [], replyType: UInt8 = LinkType.ack, timeoutMs: Int = 2000) -> [UInt8]? {
        seq &+= 1
        let s = seq
        guard let port, port.write(encodeFrame(type: type, seq: s, payload: payload)) else { drop("写失败"); return nil }
        return wait(type: replyType, seq: s, timeoutMs: timeoutMs)
    }

    /// 没连上就试着打开 + HELLO；返回是否刚刚连上（调用方据此推整帧）
    func connectIfNeeded() -> Bool {
        lock.lock(); defer { lock.unlock() }
        if port != nil { return false }
        guard let p = open() else { return false }
        port = p; parser = FrameParser()
        let now = Date()
        let tz = TimeZone.current.secondsFromGMT(for: now) / 60
        var pl: [UInt8] = []
        let t = UInt32(truncatingIfNeeded: Int(now.timeIntervalSince1970))
        for s in stride(from: 0, to: 32, by: 8) { pl.append(UInt8((t >> UInt32(s)) & 0xFF)) }
        pl += [UInt8(UInt16(bitPattern: Int16(tz)) & 0xFF), UInt8(UInt16(bitPattern: Int16(tz)) >> 8)]
        guard let r = call(LinkType.hello, pl, replyType: LinkType.helloAck), let h = HelloInfo(r) else {
            drop("HELLO 没有应答"); return false
        }
        guard h.width == screenW, h.height == screenH, h.pixfmt == 1 else { drop("屏参数不符 \(h)"); return false }
        info = h; errBase = 0
        log("设备已连上 \(name)：fw=\(h.fw) caps=\(h.caps)\(h.doubleBuffered ? "（双缓冲）" : "") max_payload=\(h.maxPayload)")
        return true
    }

    /// 单条请求-应答（SEG、FBCRC、bench 用）；超时当断开
    func request(_ type: UInt8, _ payload: [UInt8] = [], replyType: UInt8 = LinkType.ack, timeoutMs: Int = 2000) -> [UInt8]? {
        lock.lock(); defer { lock.unlock() }
        guard port != nil else { return nil }
        let r = call(type, payload, replyType: replyType, timeoutMs: timeoutMs)
        if r == nil { drop(String(format: "0x%02X 没有应答", type)) }
        return r
    }

    /// 原样写字节（bench 测裸吞吐用）
    func writeRaw(_ bytes: [UInt8]) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard let port else { return false }
        if !port.write(bytes) { drop("写失败"); return false }
        return true
    }

    /// 心跳；顺便发现设备拔了
    func ping() {
        lock.lock(); defer { lock.unlock() }
        if port != nil, call(LinkType.ping) == nil { drop("PING 超时") }
    }

    /// 推一批 COPY（固件不支持就不发，调用方别给）+ 矩形（RGB565 整帧 buf 里截取）+ PRESENT。失败（断开、超时、设备侧计到新的坏帧）返回 false，调用方下次推整帧
    func push(_ rects: [LinkRect], _ buf: [UInt16], copies: [LinkCopy] = [], frameID: Int) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard let port, let info else { return false }
        let maxP = min(info.maxPayload, linkMaxPayload)
        var rs: [LinkRect] = []
        for r in rects where 8 + r.w * r.h * 2 > maxP {   // 调用方给的矩形太大就按行切
            let rows = max(1, (maxP - 8) / (r.w * 2))
            for y in stride(from: r.y, to: r.y + r.h, by: rows) { rs.append(LinkRect(x: r.x, y: y, w: r.w, h: min(rows, r.y + r.h - y))) }
        }
        rs += rects.filter { 8 + $0.w * $0.h * 2 <= maxP }
        let (bytes, s) = encodeBatch(rs, buf, copies: info.hasCopy ? copies : [], frameID: frameID, seq: &seq)
        guard port.write(bytes) else { drop("写失败"); return false }
        guard let r = wait(type: LinkType.ack, seq: s, timeoutMs: 10_000), let a = AckStats(r) else { drop("PRESENT 没有应答"); return false }
        let errs = a.crcErr + a.lenErr + a.badParam
        defer { errBase = errs }
        return errs == errBase
    }
}
