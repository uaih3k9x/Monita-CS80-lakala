import Testing
import MonitaCore

private func a(_ type: String, _ proc: String, level: Int = 255) -> [String: Any] {
    ["AssertType": type, "AssertLevel": level, "Process Name": proc]
}

@Test func sleepGuardIdleWhenNoSystemSleepAssertion() {
    #expect(sleepGuard([]) == SleepGuard(awake: false, holder: nil, others: 0))
    // 只挡显示器睡眠 / 用户活动 / 关掉的断言都不算
    let g = sleepGuard([a("PreventUserIdleDisplaySleep", "Chrome"), a("UserIsActive", "WindowServer"),
                        a("NoDisplaySleepAssertion", "x"), a("PreventUserIdleSystemSleep", "y", level: 0)])
    #expect(g == SleepGuard(awake: false, holder: nil, others: 0))
}

@Test func sleepGuardIgnoresPowerdDisplayOnAssertion() {
    // powerd 在屏幕亮着时一直持有 PreventUserIdleSystemSleep，熄屏就放掉：不算「有人挡睡眠」
    #expect(sleepGuard([a("PreventUserIdleSystemSleep", "powerd")]).awake == false)
}

@Test func sleepGuardPrefersCaffeinateThenApps() {
    let g = sleepGuard([a("PreventUserIdleSystemSleep", "coreaudiod"), a("PreventUserIdleSystemSleep", "NeteaseMusic"),
                        a("PreventUserIdleSystemSleep", "caffeinate"), a("PreventUserIdleSystemSleep", "caffeinate"),
                        a("PreventSystemSleep", "backupd")])
    #expect(g == SleepGuard(awake: true, holder: "caffeinate", others: 3))
    #expect(sleepGuard([a("PreventUserIdleSystemSleep", "coreaudiod"), a("PreventSystemSleep", "Music")])
            == SleepGuard(awake: true, holder: "Music", others: 1))
    #expect(sleepGuard([a("NoIdleSleepAssertion", "old")]) == SleepGuard(awake: true, holder: "old", others: 0))
}

@Test func agentCountsFollowHostStates() {
    let json = """
    {"items":[{"agent":"claude","state":"running"},{"agent":"claude","state":"waiting"},
              {"agent":"codex","state":"waiting"},{"agent":"deepseek","state":"running"},
              {"agent":"claude","state":"done"},{"agent":"pi","state":"running"}],"running":3}
    """
    let c = AgentCounts.parse(Array(json.utf8), agentState: Array("{\"state\":\"waiting\"}".utf8))
    #expect(c == AgentCounts(claude: 1, waiting: 3, dsh: 1))
    let c2 = AgentCounts.parse(Array(json.utf8), agentState: Array("{\"state\":\"idle\"}".utf8))
    #expect(c2?.waiting == 2)
    #expect(AgentCounts.parse(Array("x".utf8), agentState: nil) == nil)
}

@Test func debouncedFlagSwitchesAtMostEvery2s() {
    var f = Debounced(false, minInterval: 2000)
    #expect(f.update(true, nowMs: 0) == true)
    #expect(f.update(false, nowMs: 500) == true)      // 太快：保持
    #expect(f.update(false, nowMs: 2000) == false)
    #expect(f.update(true, nowMs: 2100) == false)
    #expect(f.update(true, nowMs: 4000) == true)
}

@Test func cpuFromTickDeltas() {
    // ticks = user, system, idle, nice
    #expect(cpuFraction(prev: [100, 100, 800, 0], cur: [130, 120, 850, 0]) == 0.5)
    #expect(cpuFraction(prev: [1, 1, 1, 1], cur: [1, 1, 1, 1]) == nil)
    #expect(cpuFraction(prev: [], cur: [1, 2, 3, 4]) == nil)
}

@Test func screenModeFollowsLockAndYieldsToHostSaver() {
    #expect(screenMode(locked: false, saverPlaying: false) == .lyrics)
    #expect(screenMode(locked: true, saverPlaying: false) == .lockWallpaper)
    #expect(screenMode(locked: true, saverPlaying: true) == .yieldToSaver)
    #expect(screenMode(locked: false, saverPlaying: true) == .yieldToSaver)
}

@Test func aspectFillCentersAndCrops() {
    // 1920×1080 横图填 800×1280：按高度缩放 1280/1080，宽度溢出后居中裁
    let r = aspectFill(srcW: 1920, srcH: 1080, dstW: 800, dstH: 1280)
    #expect(abs(r.h - 1280) < 0.001)
    #expect(abs(r.w - 1920 * 1280 / 1080.0) < 0.001)
    #expect(abs(r.x - (800 - r.w) / 2) < 0.001)
    #expect(r.y == 0)
    // 竖长图：按宽度
    let t = aspectFill(srcW: 400, srcH: 2000, dstW: 800, dstH: 1280)
    #expect(t.w == 800 && t.h == 4000 && t.x == 0 && t.y == (1280 - 4000) / 2)
}
