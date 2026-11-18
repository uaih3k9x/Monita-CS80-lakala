// --device 参数解析、USB 接口探测顺序、段码屏主区编码
import Testing
import MonitaCore

@Test func deviceSpecParse() {
    #expect(parseDeviceSpec("usb") == .usb(interface: nil))
    #expect(parseDeviceSpec("usb:7") == .usb(interface: 7))
    #expect(parseDeviceSpec("usb:x") == nil)
    #expect(parseDeviceSpec("usb:") == nil)
    #expect(parseDeviceSpec("/dev/cu.usbmodem1") == .serial("/dev/cu.usbmodem1"))
    #expect(parseDeviceSpec("") == nil)
}

@Test func usbProbeOrderPrefersKnown() {
    #expect(usbProbeOrder([2, 3, 7, 8], preferred: [5, 7]) == [7, 2, 3, 8])
    #expect(usbProbeOrder([8, 2], preferred: []) == [2, 8])
}

@Test func segMainDigitsAndDash() {
    let fb = segMain("-1")
    #expect(fb.count == 72)
    #expect(fb[4] & 0x08 == 0x08)          // 第 4 列 g 段
    #expect(fb[18 + 5] & 0x08 == 0x08)     // 第 5 列 b 段
    #expect(fb[36 + 5] & 0x08 == 0x08)     // 第 5 列 c 段
    #expect(fb[0] == 0)
}

@Test func segMainClockColons() {
    let fb = segMain("12:34:56")
    #expect(fb[55] & 0x08 == 0x08 && fb[57] & 0x08 == 0x08)
    #expect(fb[18 + 0] & 0x08 == 0x08)     // 第 0 列 "1" 的 b 段
    #expect(segMain("123456")[55] & 0x08 == 0)
}
