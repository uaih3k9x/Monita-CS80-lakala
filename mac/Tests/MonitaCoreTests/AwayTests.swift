import Testing
import MonitaCore

@Test func awayLineByDuration() {
    let t0 = 1_000_000.0
    #expect(awayLine(sinceS: nil, nowS: t0) == "可能出去了")
    #expect(awayLine(sinceS: t0, nowS: t0 + (30)) == "刚离开")
    #expect(awayLine(sinceS: t0, nowS: t0 + (25 * 60 + 59)) == "已离开 25 分钟")
    #expect(awayLine(sinceS: t0, nowS: t0 + (60 * 60)) == "已离开 1 小时")
    #expect(awayLine(sinceS: t0, nowS: t0 + (125 * 60)) == "已离开 2 小时 5 分")
    #expect(awayLine(sinceS: t0, nowS: t0 + (-5)) == "刚离开")
}
