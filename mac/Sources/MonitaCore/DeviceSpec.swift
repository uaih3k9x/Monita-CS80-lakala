// 直连设备的纯逻辑：--device 参数、USB 接口探测顺序、段码屏主区编码（映射见 docs/segmap.md）

/// `--device` 的写法：`usb`（自动探测接口）、`usb:<接口号>`，其余当作串口路径
public enum DeviceSpec: Equatable {
    case serial(String)
    case usb(interface: Int?)
}

public func parseDeviceSpec(_ s: String) -> DeviceSpec? {
    if s.isEmpty { return nil }
    if s == "usb" { return .usb(interface: nil) }
    if s.hasPrefix("usb:") {
        guard let n = Int(s.dropFirst(4)), n >= 0 else { return nil }
        return .usb(interface: n)
    }
    return .serial(s)
}

/// 自动探测时先试已知的（上次连上的 / 默认的），其余按接口号升序
public func usbProbeOrder(_ ifaces: [Int], preferred: [Int]) -> [Int] {
    let first = preferred.filter(ifaces.contains)
    return first + ifaces.sorted().filter { !first.contains($0) }
}

private let seg7: [UInt8] = [0x3F, 0x06, 0x5B, 0x4F, 0x66, 0x6D, 0x7D, 0x07, 0x7F, 0x6F]

/// 主数字区第 k 列写 7 段（a..g = bit0..6），与固件 fg_digit 一致
func segDigit(_ fb: inout [UInt8], _ k: Int, _ m: UInt8) {
    fb[k] &= 0x77; fb[18 + k] &= 0x77; fb[36 + k] &= 0x77; fb[54 + k] &= 0x7F
    if m & 0x20 != 0 { fb[k] |= 0x80 }
    if m & 0x40 != 0 { fb[k] |= 0x08 }
    if m & 0x01 != 0 { fb[18 + k] |= 0x80 }
    if m & 0x02 != 0 { fb[18 + k] |= 0x08 }
    if m & 0x10 != 0 { fb[36 + k] |= 0x80 }
    if m & 0x04 != 0 { fb[36 + k] |= 0x08 }
    if m & 0x08 != 0 { fb[54 + k] |= 0x80 }
}

/// 主区 6 位：数字、'-'（g 段）、' '（灭）右对齐；':' 点亮前一位之后的冒号（只有第 2、4 位后有）
public func segMain(_ text: String) -> [UInt8] {
    var cols: [UInt8] = [], colonAfter: Set<Int> = []
    for ch in text {
        if ch == ":" { colonAfter.insert(cols.count); continue }
        cols.append(ch.wholeNumberValue.map { seg7[$0] } ?? (ch == "-" ? 0x40 : 0))
    }
    let pad = max(0, 6 - cols.count)
    cols = Array(repeating: 0, count: pad) + cols.prefix(6)
    var fb = [UInt8](repeating: 0, count: 72)
    for (k, m) in cols.enumerated() { segDigit(&fb, k, m) }
    if colonAfter.contains(2 - pad) { fb[55] |= 0x08 }
    if colonAfter.contains(4 - pad) { fb[57] |= 0x08 }
    return fb
}
