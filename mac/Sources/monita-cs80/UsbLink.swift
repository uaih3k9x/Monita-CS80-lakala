// macOS 直连：IOUSBHost 直接打开模组的 USB 接口，走 bulk IN/OUT。
// 模组（VID 0x2c7c PID 0x0901）的串口类接口是厂商自定义类 0xFF，macOS 不绑驱动、没有 /dev/cu.*；
// 没被驱动占用的接口普通用户就能独占打开，不需要 root / entitlement。
// 固件 ql_uart_open(4)（usbmodem）实测是接口 7（bulk IN 0x8A / OUT 0x07，另有中断 IN 0x89 不用）。
//
// 读：始终挂一个异步 bulk IN 请求（不超时），完成就把数据追加进 rx 再挂下一个；read(timeoutMs) 只等 rx。
//     这样超时不会去 abort 正在传的请求，不丢字节。
// 写：同步 bulk OUT，大块按 1 MiB 切。设备处理不过来时 USB 自己限流，写只是变慢。
// 断开：接口 terminate 通知、或任何非 abort 的 IO 错误 → dead，读写返回失败，DeviceLink 负责重开。
import Foundation
import IOKit
import IOUSBHost
import MonitaCore

let usbVID = 0x2c7c, usbPID = 0x0901
/// 默认先试的接口号（实测 usbmodem = 7）；探测成功后记住，重连时先试它
var usbPreferred = [7]
private let kIOMessageServiceIsTerminated: UInt32 = 0xE000_0010   // iokit_common_msg(0x010)，宏 Swift 导不进来

final class UsbPort: LinkPort {
    let number: Int
    private let iface: IOUSBHostInterface
    private let pin: IOUSBHostPipe, pout: IOUSBHostPipe
    private let cond = NSCondition()
    private var rx: [UInt8] = []
    private var dead = false
    private let rxBuf = NSMutableData(length: 16 * 1024)!

    /// 本机上这台设备的全部接口：(接口号, io_service_t)；调用方负责 IOObjectRelease
    static func interfaces() -> [(Int, io_service_t)] {
        let m = IOServiceMatching("IOUSBHostInterface") as NSMutableDictionary
        m["IOPropertyMatch"] = ["idVendor": usbVID, "idProduct": usbPID]
        var it: io_iterator_t = 0
        guard IOServiceGetMatchingServices(kIOMainPortDefault, m, &it) == KERN_SUCCESS else { return [] }
        defer { IOObjectRelease(it) }
        var out: [(Int, io_service_t)] = []
        while case let s = IOIteratorNext(it), s != 0 {
            let n = IORegistryEntryCreateCFProperty(s, "bInterfaceNumber" as CFString, nil, 0)?.takeRetainedValue() as? Int
            if let n { out.append((n, s)) } else { IOObjectRelease(s) }
        }
        return out
    }

    /// 打开指定接口：要有一对 bulk 端点。失败返回 nil（并给出原因）
    static func open(interface want: Int) -> (UsbPort?, String) {
        var found: io_service_t = 0
        for (n, s) in interfaces() { if n == want && found == 0 { found = s } else { IOObjectRelease(s) } }
        if found == 0 { return (nil, "没找到设备 / 接口 \(want)") }
        // IOUSBHostInterface 接管 found 的引用
        do { return (try UsbPort(service: found, number: want), "") } catch { return (nil, "接口 \(want) 打不开（被别的进程占着？）：\(error.localizedDescription)") }
    }

    private init(service: io_service_t, number: Int) throws {
        self.number = number
        weak var weakSelf: UsbPort?
        iface = try IOUSBHostInterface(__ioService: service, options: [], queue: nil) { _, type, _ in
            if type == kIOMessageServiceIsTerminated { weakSelf?.markDead() }
        }
        var inA = -1, outA = -1
        var cur = UnsafeRawPointer(iface.interfaceDescriptor).assumingMemoryBound(to: IOUSBDescriptorHeader.self)
        while let e = IOUSBGetNextEndpointDescriptor(iface.configurationDescriptor, iface.interfaceDescriptor, cur) {
            let a = Int(e.pointee.bEndpointAddress)
            if e.pointee.bmAttributes & 3 == 2 { if a & 0x80 != 0 { if inA < 0 { inA = a } } else if outA < 0 { outA = a } }
            cur = UnsafeRawPointer(e).assumingMemoryBound(to: IOUSBDescriptorHeader.self)
        }
        guard inA >= 0, outA >= 0 else {
            iface.destroy()
            throw NSError(domain: "UsbPort", code: 1, userInfo: [NSLocalizedDescriptionKey: "没有 bulk IN/OUT 端点"])
        }
        do {
            pin = try iface.copyPipe(withAddress: inA)
            pout = try iface.copyPipe(withAddress: outA)
        } catch { iface.destroy(); throw error }
        weakSelf = self
        guard arm() else { iface.destroy(); throw NSError(domain: "UsbPort", code: 2, userInfo: [NSLocalizedDescriptionKey: "挂不上 bulk IN 请求"]) }
    }

    deinit { markDead(); iface.destroy() }

    private func markDead() { cond.lock(); dead = true; cond.broadcast(); cond.unlock() }

    /// 挂一个 bulk IN；完成回调在接口自己的串行队列上
    private func arm() -> Bool {
        do {
            try pin.enqueueIORequest(with: rxBuf, completionTimeout: 0) { [weak self] status, n in
                guard let self else { return }
                if status == kIOReturnSuccess {
                    let b = [UInt8](UnsafeRawBufferPointer(start: self.rxBuf.bytes, count: n))
                    self.cond.lock(); self.rx += b; self.cond.broadcast(); self.cond.unlock()
                    if !self.arm() { self.markDead() }
                } else {
                    self.markDead()   // abort（关闭时）或设备没了
                }
            }
            return true
        } catch { return false }
    }

    func write(_ bytes: [UInt8]) -> Bool {
        var off = 0
        while off < bytes.count {
            if isDead { return false }
            let n = min(1 << 20, bytes.count - off)
            let d = bytes.withUnsafeBytes { NSMutableData(bytes: $0.baseAddress! + off, length: n) }
            var done = 0
            do { try pout.__sendIORequest(with: d, bytesTransferred: &done, completionTimeout: 10) } catch { markDead(); return false }
            off += done
            if done == 0 { markDead(); return false }
        }
        return true
    }

    private var isDead: Bool { cond.lock(); defer { cond.unlock() }; return dead }

    func read(timeoutMs: Int32) -> [UInt8]? {
        let end = Date().addingTimeInterval(Double(timeoutMs) / 1000)
        cond.lock(); defer { cond.unlock() }
        while rx.isEmpty && !dead && cond.wait(until: end) {}
        if !rx.isEmpty { let b = rx; rx = []; return b }
        return dead ? nil : []
    }
}

/// 发 HELLO（不带时间），等 HELLO_ACK
func usbAnswersHello(_ p: UsbPort, timeoutMs: Int = 300) -> Bool {
    guard p.write(encodeFrame(type: LinkType.hello, seq: 0xF0, payload: [])) else { return false }
    var parser = FrameParser()
    let end = Date().addingTimeInterval(Double(timeoutMs) / 1000)
    while Date() < end {
        guard let d = p.read(timeoutMs: 50) else { return false }
        if parser.feed(d).contains(where: { $0.type == LinkType.helloAck && $0.seq == 0xF0 }) { return true }
    }
    return false
}

/// 打开 USB 链路：给了接口号就只开它；没给就按 usbProbeOrder 逐个开、发 HELLO，谁回 HELLO_ACK 用谁。
/// 只探测厂商类（0xFF）且有 bulk 端点的接口；同一原因的失败只打一次日志
private var lastUsbErr = ""
func openUsb(interface: Int?) -> LinkPort? {
    func fail(_ s: String) -> LinkPort? { if s != lastUsbErr { log("USB：\(s)"); lastUsbErr = s }; return nil }
    if let n = interface {
        let (p, why) = UsbPort.open(interface: n)
        guard let p else { return fail(why) }
        lastUsbErr = ""
        return p
    }
    let all = UsbPort.interfaces()
    var cands: [Int] = []
    for (n, s) in all {
        let cls = IORegistryEntryCreateCFProperty(s, "bInterfaceClass" as CFString, nil, 0)?.takeRetainedValue() as? Int
        if cls == 0xFF { cands.append(n) }
        IOObjectRelease(s)
    }
    if cands.isEmpty { return fail("没找到设备（VID 0x2c7c PID 0x0901）") }
    var why: [String] = []
    for n in usbProbeOrder(cands, preferred: usbPreferred) {
        let (p, w) = UsbPort.open(interface: n)
        guard let p else { why.append(w); continue }
        if usbAnswersHello(p) {
            if usbPreferred.first != n { log("USB：探测到固件在接口 \(n)") }
            usbPreferred = [n]; lastUsbErr = ""
            return p
        }
        why.append("接口 \(n) 不回 HELLO")
    }
    return fail(why.joined(separator: "；"))
}
