import Testing
import MonitaCore

@Test func bgr565Packing() {
    // ((b & 0xf8) << 8) | ((g & 0xfc) << 3) | (r >> 3)
    let rgba: [UInt8] = [255, 0, 0, 255,  0, 255, 0, 255,  0, 0, 255, 255,  0x12, 0x34, 0x56, 255]
    let out = toBGR565(rgba, count: 4)
    #expect(out == [0x001F, 0x07E0, 0xF800, 0x51A2]) // 0x5000 | 0x01A0 | 0x0002
}

@Test func rgbaToRGBDropsAlpha() {
    #expect(toRGB([1, 2, 3, 255, 4, 5, 6, 255], count: 2) == [1, 2, 3, 4, 5, 6])
}

@Test func tileDiffFindsDirtyTilesInRowMajorOrder() {
    let w = 64, h = 32 // 2×2 tiles
    let a = [UInt16](repeating: 0, count: w * h)
    var b = a
    b[17 * w + 40] = 1   // tile (32,16)
    b[0 * w + 31] = 1    // tile (0,0) 右上角像素
    #expect(diffTiles(a, b, width: w, height: h) == [Tile(x: 0, y: 0), Tile(x: 32, y: 16)])
    #expect(diffTiles(a, a, width: w, height: h).isEmpty)
}

@Test func patchEncodingIsPusherJsLayout() {
    let w = 64, h = 32
    var buf = [UInt16](repeating: 0, count: w * h)
    for row in 0..<16 { for k in 0..<32 { buf[(16 + row) * w + 32 + k] = UInt16(row * 32 + k) } }
    let body = encodePatch([Tile(x: 32, y: 16)], buf, width: w)
    #expect(body.count == 4 + 1 * (4 + 1024))
    #expect(Array(body[0..<4]) == [1, 0, 0, 0])          // u32 count LE
    #expect(Array(body[4..<8]) == [32, 0, 16, 0])        // u16 x, u16 y LE
    #expect(Array(body[8..<12]) == [0, 0, 1, 0])         // 像素 0、1，u16 LE
    #expect(Array(body[(body.count - 2)...]) == [0xFF, 0x01]) // 最后一个像素 511
}

@Test func pushDecision() {
    #expect(decidePush(fullNeeded: true, hasPrev: true, dirty: 3) == .full)
    #expect(decidePush(fullNeeded: false, hasPrev: false, dirty: 0) == .full)
    #expect(decidePush(fullNeeded: false, hasPrev: true, dirty: 0) == .none)
    #expect(decidePush(fullNeeded: false, hasPrev: true, dirty: 120) == .patch)
    #expect(decidePush(fullNeeded: false, hasPrev: true, dirty: 121) == .full)
    // 直连：差分矩形不比整帧贵，脏块再多也走差分
    #expect(decidePush(fullNeeded: false, hasPrev: true, dirty: 2000, maxPatch: .max) == .patch)
    #expect(decidePush(fullNeeded: true, hasPrev: true, dirty: 5, maxPatch: .max) == .full)
}

@Test func statusWatchMarksFullOnReconnectSessionChangeOrFailure() {
    var w = StatusWatch()
    #expect(w.update(device: "up", session: 1, lastEvent: "patch", lastOk: 1) == false) // 首次：本来就要整帧
    #expect(w.update(device: "up", session: 1, lastEvent: "patch", lastOk: 1) == false)
    #expect(w.update(device: "up", session: 2, lastEvent: "ready", lastOk: nil) == true)  // host 侧会话重启
    #expect(w.update(device: "down", session: 2, lastEvent: "lost", lastOk: nil) == false)
    #expect(w.update(device: "up", session: 2, lastEvent: "ready", lastOk: nil) == true)  // 重新上线
    #expect(w.update(device: "up", session: 2, lastEvent: "patch", lastOk: 0) == true)    // patch 失败
    #expect(w.update(device: "up", session: 2, lastEvent: "state", lastOk: 0) == false)
}
