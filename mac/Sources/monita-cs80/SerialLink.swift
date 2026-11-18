// 直连 R05 固件，走 docs/protocol.md 的 v0 协议。
// 编帧/拆帧/打包在 MonitaCore/Link.swift；这里是传输抽象 LinkPort、termios 串口（Linux / 有串口驱动时）
// 和"发一批 + 等 PRESENT 的 ACK"。macOS 上模组接口没有驱动、没有 /dev/cu.*，走 UsbLink.swift（IOKit bulk）。
// 线程：所有调用经 lock 串行化（渲染线程推帧，link 线程负责重连和心跳）。
// 设备主动上报的 KEY（fw v8）：任何一次读到的都交给 onKey（在 lock 里调，回调只能排队、不能回调 DeviceLink）；
// 没有请求在等时由 pollEvents()（keys 线程每 50 ms）去读。
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
    /// 每次连上（HELLO 成功）+1：设备 HELLO 会把灯灭掉，LED 循环据此重发
    private var gen = 0
    var generation: Int { lock.lock(); defer { lock.unlock() }; return gen }
    /// 收到 KEY 时调（持有 lock，只做排队）
    var onKey: ((KeyEvent) -> Void)?
    var isUp: Bool { lock.lock(); defer { lock.unlock() }; return port != nil }
    /// HELLO 带的时间从这里取（--conformance 换成固定值，见 Conformance.swift）
    var clock: () -> Date = { Date() }

    init(name: String, open: @escaping () -> LinkPort?) { self.name = name; self.open = open }

    private func drop(_ why: String) {
        if port != nil { log("设备断开：\(why)") }
        port = nil; info = nil
    }

    /// 拆帧；KEY 交给 onKey，其余原样返回
    private func feed(_ data: [UInt8]) -> [LinkFrame] {
        var rest: [LinkFrame] = []
        for f in parser.feed(data) {
            if f.type == LinkType.key { if let e = KeyEvent(f.payload) { onKey?(e) } } else { rest.append(f) }
        }
        return rest
    }

    private func wait(type: UInt8, seq want: UInt8, timeoutMs: Int) -> [UInt8]? {
        let end = Date().addingTimeInterval(Double(timeoutMs) / 1000)
        while Date() < end {
            guard let port, let data = port.read(timeoutMs: 50) else { drop("读失败"); return nil }
            // 同一段字节里应答后面可能还跟着 KEY：整段拆完再返回
            if let f = feed(data).first(where: { $0.type == type && $0.seq == want }) { return f.payload }
        }
        return nil
    }

    /// 不等任何应答，只把已经到了的字节读掉（收 KEY 用）
    func pollEvents() {
        lock.lock(); defer { lock.unlock() }
        guard let port, info != nil else { return }
        guard let data = port.read(timeoutMs: 0) else { drop("读失败"); return }
        if !data.isEmpty { _ = feed(data) }
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
        guard let r = call(LinkType.hello, helloPayload(clock()), replyType: LinkType.helloAck), let h = HelloInfo(r) else {
            drop("HELLO 没有应答"); return false
        }
        guard h.protoSupported, h.width == screenW, h.height == screenH, h.pixfmt == 1 else { drop("协议版本 / 屏参数不符 \(h)"); return false }
        info = h; errBase = 0; gen += 1
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

    /// 不等应答地发一串同类型帧（IMM_RECT 用；和 RECT 一样设备不应答，之后的一条请求的 ACK 就是同步点）
    func send(_ type: UInt8, _ payloads: [[UInt8]]) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard let port, info != nil else { return false }
        var out: [UInt8] = []
        for p in payloads {
            seq &+= 1
            out += encodeFrame(type: type, seq: seq, payload: p)
            if out.count >= 1 << 20 { guard port.write(out) else { drop("写失败"); return false }; out.removeAll(keepingCapacity: true) }
        }
        if !out.isEmpty, !port.write(out) { drop("写失败"); return false }
        return true
    }

    /// 单条 RECT / IMM_RECT 的 payload 上限（没连上时按 32 KiB）
    var rectMax: Int { lock.lock(); defer { lock.unlock() }; return info?.rectPayloadMax ?? linkMaxPayload }

    /// caps 运行中会变（IMM_EXIT 的 ACK extra 是退出后的 caps）
    func updateCaps(_ caps: Int) { lock.lock(); defer { lock.unlock() }; info?.caps = caps }

    /// DIAG 11：翻页时机切到"立即写基址"。DIAG_R 的 status 不是 0（固件不认 / 沉浸模式里）返回 false
    func setFlipNow() -> Bool {
        diagReplyOK(request(LinkType.diag, [11, 1], replyType: LinkType.diagR, timeoutMs: 10_000))
    }

    /// 这条 ACK 里的出错计数（CRC / 长度 / 被拒）比上一次多了吗；push 和沉浸模式共用一个基线
    func newErrors(_ a: AckStats) -> Bool {
        lock.lock(); defer { lock.unlock() }
        let errs = a.crcErr + a.lenErr + a.badParam
        defer { errBase = errs }
        return errs != errBase
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
        let maxP = info.rectPayloadMax
        var rs: [LinkRect] = []
        for r in rects where 8 + r.w * r.h * 2 > maxP {   // 调用方给的矩形太大就按行切
            let rows = max(1, (maxP - 8) / (r.w * 2))
            for y in stride(from: r.y, to: r.y + r.h, by: rows) { rs.append(LinkRect(x: r.x, y: y, w: r.w, h: min(rows, r.y + r.h - y))) }
        }
        rs += rects.filter { 8 + $0.w * $0.h * 2 <= maxP }
        let (bytes, s) = encodeBatch(rs, buf, copies: info.hasCopy ? copies : [], frameID: frameID, seq: &seq)
        guard port.write(bytes) else { drop("写失败"); return false }
        guard let r = wait(type: LinkType.ack, seq: s, timeoutMs: 10_000), let a = AckStats(r) else { drop("PRESENT 没有应答"); return false }
        defer { errBase = a.crcErr + a.lenErr + a.badParam }
        return presentAckOK(a, errBase: errBase)       // 沉浸模式里 PRESENT 回 status 1
    }
}
