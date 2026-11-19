// 按键 KEY (0x90) / 状态灯 LED (0x31)：解析、按键 → 动作、播放状态 → 灯、页面切换（fw v8，docs/protocol.md）
import Testing
import MonitaCore

func keyPayload(_ key: UInt8, _ edge: UInt8, n: Int = 0, uptime: UInt32 = 0) -> [UInt8] {
    [key, edge, UInt8(n & 0xFF), UInt8(n >> 8),
     UInt8(uptime & 0xFF), UInt8((uptime >> 8) & 0xFF), UInt8((uptime >> 16) & 0xFF), UInt8(uptime >> 24)]
}

@Test func keyEventParse() {
    let e = KeyEvent(keyPayload(3, 3, n: 0x1234, uptime: 0xA1B2_C3D4))
    #expect(e == KeyEvent(key: .plus, edge: .long, count: 0x1234, uptime: 0xA1B2_C3D4))
    #expect(KeyEvent(keyPayload(5, 1)) == nil)          // 没有键 5
    #expect(KeyEvent(keyPayload(1, 4)) == nil)          // 没有事件 4
    #expect(KeyEvent(Array(keyPayload(1, 1).prefix(7))) == nil)
}

@Test func helloCapsKeysAndLed() {
    var pl = [UInt8](repeating: 0, count: 16)
    pl[10] = 0x3D                                       // FBCRC | COPY | TRIBUF | KEYS | LED
    let h = HelloInfo(pl)!
    #expect(h.hasKeys && h.hasLed)
    pl[10] = 0x0D
    #expect(!HelloInfo(pl)!.hasKeys && !HelloInfo(pl)!.hasLed)
}

func feed(_ m: inout KeyMapper, _ evs: [(PanelKey, KeyEdge)]) -> [KeyAction] {
    evs.compactMap { m.feed(KeyEvent(key: $0.0, edge: $0.1, count: 0, uptime: 0)) }
}

@Test func volumeKeysActOnDown() {
    var m = KeyMapper()
    #expect(feed(&m, [(.plus, .down), (.plus, .up)]) == [.volumeUp])
    #expect(feed(&m, [(.minus, .down), (.minus, .long), (.minus, .up)]) == [.volumeDown])
}

@Test func menuShortPressTogglesPage() {
    var m = KeyMapper()
    #expect(feed(&m, [(.menu, .down)]) == [])
    #expect(feed(&m, [(.menu, .up)]) == [.togglePage])
    #expect(feed(&m, [(.menu, .down), (.menu, .long), (.menu, .up)]) == [])   // 长按不切
}

@Test func queryShortPlayPauseLongDoesNothing() {
    var m = KeyMapper()
    #expect(feed(&m, [(.query, .down), (.query, .up)]) == [.playPause])
    // 固件对 PWRKEY 长按超时自恢复：LONG 之后补一个 UP，不能再当短按。
    // 长按留空（v9 是进 / 出沉浸模式，容易误触；沉浸模式只能 --immersive 进）
    #expect(feed(&m, [(.query, .down), (.query, .long), (.query, .up)]) == [])
}

@Test func upWithoutDownIsIgnored() {
    var m = KeyMapper()                                 // 连上时键已按着（DOWN 发生在 HELLO 之前）
    #expect(feed(&m, [(.menu, .up), (.query, .up)]) == [])
}

@Test func ledPayloadLayout() {
    let c = LedCommand(mask: ledBlue, mode: .blink, periodMs: 3000, onMs: 2000, durationMs: 0x1234)
    #expect(c.payload == [4, 2, 0xB8, 0x0B, 0xD0, 0x07, 0x34, 0x12])
    #expect(LedCommand.off.payload == [0, 0, 0, 0, 0, 0, 0, 0])
}

@Test func ledFollowsPlayback() {
    #expect(ledFor(playing: true, waiting: false) == LedCommand(mask: ledGreen, mode: .on))
    #expect(ledFor(playing: false, waiting: false) == .off)
    // 有会话在等人：蓝灯闪，优先于播放
    let w = ledFor(playing: true, waiting: true)
    #expect(w.mask == ledBlue && w.mode == .blink)
    #expect(ledFor(playing: false, waiting: true) == w)
}

@Test func pageToggleAndScreenMode() {
    #expect(Page.lyrics.toggled == .clock && Page.clock.toggled == .lyrics)
    #expect(screenMode(locked: false, saverPlaying: false, page: .clock) == .clock)
    #expect(screenMode(locked: true, saverPlaying: false, page: .clock) == .lockWallpaper)   // 锁屏优先
    #expect(screenMode(locked: false, saverPlaying: true, page: .clock) == .yieldToSaver)
    #expect(screenMode(locked: false, saverPlaying: false) == .lyrics)
}

@Test func clockPageWakesOnMinute() {
    let now = 6_000_000.0 + 12.5
    let w = nextWake(mode: .clock, active: true, playing: true, retry: false, nowS: now, sinceS: nil, tickS: 0.2)
    #expect(w >= 6_000_060 && w < 6_000_060.5)          // 纯时钟页只到整分钟才变，播放中也不按 tick 醒
}

@Test func volumeScriptClampsInAppleScript() {
    let up = volumeScript(delta: 6)
    #expect(up == """
        set v to (output volume of (get volume settings)) + 6
        if v > 100 then set v to 100
        if v < 0 then set v to 0
        set volume output volume v
        set volume without output muted
        """)
    let down = volumeScript(delta: -6)
    #expect(down.hasPrefix("set v to (output volume of (get volume settings)) + -6"))
    #expect(!down.contains("muted"))                    // 静音时按 − 不解除静音
}

// mediaremote-adapter 目录：MONITA_MRA_DIR 指定就只认它；否则 $MONITA_HOME → ~/.monita-cs80 → ~/.cs80_panel 顺序探测
@Test func mraDirProbeOrder() {
    let home = "/Users/you"
    #expect(mraCandidates(env: [:], home: home) == [
        "/Users/you/.monita-cs80/mediaremote-adapter", "/Users/you/.cs80_panel/mediaremote-adapter",
    ])
    #expect(mraCandidates(env: ["MONITA_HOME": "/opt/m"], home: home).first == "/opt/m/mediaremote-adapter")
    #expect(mraCandidates(env: ["MONITA_HOME": ""], home: home).count == 2)                   // 空串当没设
    #expect(mraCandidates(env: ["MONITA_MRA_DIR": "/a", "MONITA_HOME": "/b"], home: home) == ["/a"])
    #expect(mraCandidates(env: ["MONITA_HOME": "/Users/you/.monita-cs80"], home: home).count == 2)  // 去重
    // 第一个装好了的生效；都没装返回第一个（日志里报它）
    let c = mraCandidates(env: [:], home: home)
    #expect(pickMraDir(c) { $0.contains("cs80_panel") } == "/Users/you/.cs80_panel/mediaremote-adapter")
    #expect(pickMraDir(c) { _ in true } == "/Users/you/.monita-cs80/mediaremote-adapter")
    #expect(pickMraDir(c) { _ in false } == "/Users/you/.monita-cs80/mediaremote-adapter")
}
