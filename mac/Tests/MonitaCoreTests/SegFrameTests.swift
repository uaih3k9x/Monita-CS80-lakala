// 段码屏展示帧：状态 → 72 字节显存（映射见 docs/segmap.md / fw/fg00/segmap.h）
import Testing
import MonitaCore

private let clock = SegClock(hour: 9, minute: 5, month: 11, day: 18)
private func play(_ state: String, pos: Double, dur: Double?) -> PlayState {
    PlayState(state: state, pos: pos, at: 1_000_000, rate: state == "playing" ? 1 : 0, duration: dur)
}
private func frame(_ p: PlayState?, nowMs: Double = 1_000_000, battery: SegBattery? = nil) -> [UInt8] {
    segFrame(SegInput(play: p, nowMs: nowMs, clock: clock, battery: battery))
}
private func on(_ fb: [UInt8], _ s: SegBit) -> Bool { fb[s.byte] & s.mask != 0 }
private func digits(_ fb: [UInt8], _ cols: Range<Int>) -> [UInt8] { cols.map { segDigitAt(fb, $0) } }

// 7 段掩码
private let d0: UInt8 = 0x3F, d1: UInt8 = 0x06, d2: UInt8 = 0x5B, d3: UInt8 = 0x4F, d4: UInt8 = 0x66
private let d5: UInt8 = 0x6D, d8: UInt8 = 0x7F, d9: UInt8 = 0x6F, dash: UInt8 = 0x40

@Test func segClockText() {
    #expect(segClockText(0) == "00:00")
    #expect(segClockText(65.9) == "01:05")
    #expect(segClockText(3599) == "59:59")
    #expect(segClockText(3723) == "1:02:03")
    #expect(segClockText(-3) == "00:00")
}

@Test func segBatteryCells() {
    // 25% 一格四舍五入，>0% 至少 1 格
    #expect(segBatteryCells(0) == 0)
    #expect(segBatteryCells(1) == 1)
    #expect(segBatteryCells(12) == 1)
    #expect(segBatteryCells(13) == 1)
    #expect(segBatteryCells(37) == 1)
    #expect(segBatteryCells(38) == 2)
    #expect(segBatteryCells(62) == 2)
    #expect(segBatteryCells(63) == 3)
    #expect(segBatteryCells(87) == 3)
    #expect(segBatteryCells(88) == 4)
    #expect(segBatteryCells(100) == 4)
    #expect(segBatteryCells(150) == 4)
    #expect(segBatteryCells(-5) == 0)
}

private func cellsLit(_ fb: [UInt8]) -> [Bool] { [SegBit.batA, .batB, .batC, .batD].map { on(fb, $0) } }

@Test func batteryShowsMacChargeInAllModes() {
    let b = SegBattery(percent: 60, onAC: false)
    for p in [play("playing", pos: 65, dur: 222), play("paused", pos: 65, dur: 222), nil] {
        for t in [1_000_000.0, 1_000_600] {
            let fb = frame(p, nowMs: t, battery: b)
            #expect(on(fb, .batFrame))
            #expect(cellsLit(fb) == [false, false, true, true])   // A..D 左(正极)→右(负极)，从负极端 D 开始亮
            #expect(!on(fb, .charge))
        }
    }
    #expect(cellsLit(frame(nil, battery: SegBattery(percent: 100, onAC: true))) == [true, true, true, true])
    #expect(cellsLit(frame(nil, battery: SegBattery(percent: 0, onAC: true))) == [false, false, false, false])
    #expect(cellsLit(frame(nil, battery: SegBattery(percent: 20, onAC: false))) == [false, false, false, true])
    #expect(cellsLit(frame(nil, battery: SegBattery(percent: 75, onAC: false))) == [false, true, true, true])
}

@Test func lightningMeansOnAC() {
    #expect(on(frame(play("playing", pos: 1, dur: 10), battery: SegBattery(percent: 50, onAC: true)), .charge))
    #expect(on(frame(nil, battery: SegBattery(percent: 50, onAC: true)), .charge))
    #expect(!on(frame(nil, battery: SegBattery(percent: 50, onAC: false)), .charge))
}

@Test func noBatteryLeavesBatteryAreaDark() {
    for p in [play("playing", pos: 199, dur: 200), nil] {
        let fb = frame(p, battery: nil)
        for s in [SegBit.batFrame, .batA, .batB, .batC, .batD, .charge] { #expect(!on(fb, s)) }
    }
}

@Test func segDigitRoundTrip() {
    // segMain 写的数字能被 segDigitAt 读回
    let fb = segMain("123456")
    #expect(digits(fb, 0..<6) == [d1, d2, d3, d4, d5, 0x7D])
}

@Test func playingShowsProgressAndDuration() {
    // 位置 65 s / 共 222 s → 主区 01:05，小时钟 03:42
    let fb = frame(play("playing", pos: 65, dur: 222))
    #expect(digits(fb, 0..<6) == [0, 0, d0, d1, d0, d5])
    #expect(on(fb, .mainColon4) && !on(fb, .mainColon2))
    #expect(digits(fb, 8..<14) == [0, 0, d0, d3, d4, 0x5B])
    #expect(on(fb, .clkColonB) && !on(fb, .clkColonA))
    #expect(on(fb, .speaker))
    #expect(on(fb, .vol1))                 // 播放中至少一道声波
    #expect(!on(fb, .charge))
}

@Test func playingExtrapolatesPosition() {
    // 10 秒后外推到 75 s = 01:15
    let fb = frame(play("playing", pos: 65, dur: 222), nowMs: 1_010_000)
    #expect(digits(fb, 2..<6) == [d0, d1, d1, d5])
}

@Test func playingOverAnHour() {
    let fb = frame(play("playing", pos: 3723, dur: 4000))
    #expect(digits(fb, 0..<6) == [0, d1, d0, d2, d0, d3])
    #expect(on(fb, .mainColon2) && on(fb, .mainColon4))
    #expect(on(fb, .clkColonA) && on(fb, .clkColonB))   // 1:06:40
    #expect(digits(fb, 8..<14) == [0, d1, d0, 0x7D, d4, d0])
}

@Test func playingWavesAnimate() {
    let p = play("playing", pos: 10, dur: 100)
    let levels = Set(stride(from: 0.0, to: 4000, by: 125).map { segFrame(SegInput(play: p, nowMs: 1_000_000 + $0, clock: clock, battery: nil)) }
        .map { fb in [SegBit.vol1, .vol2, .vol3, .vol4, .vol5].filter { on(fb, $0) }.count })
    #expect(levels.count >= 3)
    #expect(!levels.contains(0))
}

@Test func pausedBlinksMainAndSilencesWaves() {
    let p = play("paused", pos: 65, dur: 222)
    let a = frame(p, nowMs: 1_000_000), b = frame(p, nowMs: 1_000_600)
    let shown = digits(a, 2..<6).contains(d1) ? a : b
    let hidden = shown == a ? b : a
    #expect(digits(shown, 2..<6) == [d0, d1, d0, d5])
    #expect(digits(hidden, 0..<6) == [0, 0, 0, 0, 0, 0])
    for fb in [a, b] {
        #expect(on(fb, .speaker))
        #expect(![SegBit.vol1, .vol2, .vol3, .vol4, .vol5].contains { on(fb, $0) })
        #expect(digits(fb, 8..<14) == [0, 0, d0, d3, d4, 0x5B])   // 时长不闪
    }
    // 暂停不外推
    #expect(digits(frame(p, nowMs: 1_000_000 + 60_000 * 2), 2..<6) == digits(shown, 2..<6))
}

@Test func idleShowsClockAndDate() {
    let a = frame(nil, nowMs: 1_000_000), b = frame(nil, nowMs: 1_000_500)
    for fb in [a, b] {
        #expect(digits(fb, 0..<6) == [0, 0, d0, d9, d0, d5])        // 09:05
        #expect(digits(fb, 8..<14) == [0, d1, d1, dash, d1, d8])    // 11-18
        #expect(!on(fb, .clkColonA) && !on(fb, .clkColonB))
        for s in [SegBit.speaker, .vol1, .vol5] { #expect(!on(fb, s)) }
    }
    #expect(on(a, .mainColon4) != on(b, .mainColon4))               // 冒号按秒闪
    #expect(!on(a, .mainColon2) && !on(b, .mainColon2))
}

@Test func unknownDurationLeavesClockBlank() {
    let fb = frame(play("playing", pos: 5, dur: nil))
    #expect(digits(fb, 8..<14) == [0, 0, 0, 0, 0, 0])
}

@Test func segBitPositions() {
    // 与 fw/fg00/segmap.h 对齐
    #expect((SegBit.mainColon2.byte, SegBit.mainColon2.mask) == (55, 0x08))
    #expect((SegBit.mainColon4.byte, SegBit.mainColon4.mask) == (57, 0x08))
    #expect((SegBit.clkColonA.byte, SegBit.clkColonA.mask) == (63, 0x08))
    #expect((SegBit.clkColonB.byte, SegBit.clkColonB.mask) == (65, 0x08))
    #expect((SegBit.speaker.byte, SegBit.speaker.mask) == (43, 0x08))
    #expect([SegBit.vol1, .vol2, .vol3, .vol4, .vol5].map { [$0.byte, Int($0.mask)] } == [[61, 8], [61, 2], [43, 2], [7, 2], [25, 2]])
    #expect([SegBit.batFrame, .charge, .batA, .batB, .batC, .batD].map { [$0.byte, Int($0.mask)] }
            == [[70, 0x80], [52, 0x80], [70, 0x40], [16, 0x40], [16, 0x80], [34, 0x80]])
}
