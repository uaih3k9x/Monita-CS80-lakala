import Testing
import MonitaCore

// MARK: 锁屏：只在整分钟（时钟）和「已离开 N 分钟」的分钟边界变

@Test func lockNextChangeWallMinuteWithoutSince() {
    let t = 6_000_000.0 + 12.5          // 6_000_000 是整分钟（100000 × 60）
    #expect(nextLockChange(nowS: t, sinceS: nil) == 6_000_060)
}

@Test func lockNextChangeAwayBoundaryBeforeWallMinute() {
    let base = 6_000_000.0
    // 锁于 :20，现在 :50 → 「已离开」在 :80（= 下一分钟 :20）变；墙上整分钟 :60 更早
    #expect(nextLockChange(nowS: base + 50, sinceS: base + 20) == base + 60)
    // 锁于 :50，现在 :55 → 墙上 :60 先变；:60 之后下一次是离开满 1 分钟的 :110
    #expect(nextLockChange(nowS: base + 55, sinceS: base + 50) == base + 60)
    #expect(nextLockChange(nowS: base + 61, sinceS: base + 50) == base + 110)
}

@Test func lockNextChangeIsAlwaysInFuture() {
    let base = 6_000_000.0
    #expect(nextLockChange(nowS: base, sinceS: base) > base)
    #expect(nextLockChange(nowS: base, sinceS: base + 30) > base)   // 时钟回拨：since 在未来
}

@Test func lockFrameKeyChangesOnlyAtBoundaries() {
    let base = 6_000_000.0, since = base + 20
    let k = lockFrameKey(nowS: base + 21, sinceS: since)
    #expect(lockFrameKey(nowS: base + 59.9, sinceS: since) == k)
    #expect(lockFrameKey(nowS: base + 60, sinceS: since) != k)                       // 墙上整分钟
    #expect(lockFrameKey(nowS: base + 80, sinceS: since) != lockFrameKey(nowS: base + 79, sinceS: since))  // 离开满 1 分钟
}

// MARK: 歌词页：画面实际依赖的量（秒数、当前行、进度条像素、分钟）

func song(state: String, pos: Double, at: Double, dur: Double = 200) -> LyricsData {
    var d = LyricsData()
    d.state = state
    d.title = "t"
    d.play = PlayState(state: state, pos: pos, at: at, rate: 1, duration: dur)
    d.lyric.status = "ok"
    d.lyric.lines = [LyricLine(t: 0, x: "a", tr: ""), LyricLine(t: 10, x: "b", tr: ""), LyricLine(t: 20, x: "c", tr: "")]
    return d
}

@Test func lyricsKeyStableWhilePaused() {
    let t0 = 6_000_000_000.0   // 整分钟（ms）
    let d = song(state: "paused", pos: 12.3, at: t0)
    let k = lyricsFrameKey(d, nowMs: t0 + 1000, progressW: 452)
    #expect(lyricsFrameKey(d, nowMs: t0 + 50_000, progressW: 452) == k)
    // 数据源重新打了时间戳（pos/at 变了但位置没变）也不算变化
    var d2 = d
    d2.play.at = t0 + 40_000
    #expect(lyricsFrameKey(d2, nowMs: t0 + 50_000, progressW: 452) == k)
    // 跨分钟：Front Panel 时钟要重画
    #expect(lyricsFrameKey(d, nowMs: t0 + 60_000, progressW: 452) != k)
}

@Test func lyricsKeyTracksVisibleProgress() {
    let t0 = 6_000_000_000.0
    let d = song(state: "playing", pos: 0, at: t0, dur: 452)   // 1 px/s
    let a = lyricsFrameKey(d, nowMs: t0 + 1_100, progressW: 452)
    #expect(lyricsFrameKey(d, nowMs: t0 + 1_400, progressW: 452) == a)   // 同一秒、同一像素
    #expect(lyricsFrameKey(d, nowMs: t0 + 2_000, progressW: 452) != a)   // 秒数变了
    // 换行提前 150 ms（lyricLeadS）
    let before = lyricsFrameKey(d, nowMs: t0 + 9_800, progressW: 452)
    let after = lyricsFrameKey(d, nowMs: t0 + 9_900, progressW: 452)
    #expect(before.line == 0)
    #expect(after.line == 1)
}

@Test func lyricsKeySeesDataChanges() {
    let t0 = 6_000_000_000.0
    let d = song(state: "paused", pos: 5, at: t0)
    var d2 = d
    d2.title = "other"
    #expect(lyricsFrameKey(d, nowMs: t0, progressW: 452) != lyricsFrameKey(d2, nowMs: t0, progressW: 452))
    #expect(lyricsFrameKey(nil, nowMs: t0, progressW: 452) == lyricsFrameKey(nil, nowMs: t0 + 30_000, progressW: 452))
}

@Test func progressFillRounds() {
    #expect(progressFill(pos: 0, dur: 100, width: 452) == 0)
    #expect(progressFill(pos: 50, dur: 100, width: 452) == 226)
    #expect(progressFill(pos: 500, dur: 100, width: 452) == 452)
}

// MARK: 下一次醒来的时刻

@Test func wakePlayingUsesTick() {
    let now = 6_000_000.0 + 3
    #expect(nextWake(mode: .lyrics, active: true, playing: true, retry: false, nowS: now, sinceS: nil, tickS: 0.2) == now + 0.2)
}

@Test func wakeIdleLyricsSleepsToNextMinute() {
    let now = 6_000_000.0 + 3
    let w = nextWake(mode: .lyrics, active: true, playing: false, retry: false, nowS: now, sinceS: nil, tickS: 0.2)
    #expect(w >= 6_000_060 && w < 6_000_060.5)
}

@Test func wakeLockFollowsLockChange() {
    let base = 6_000_000.0
    let w = nextWake(mode: .lockWallpaper, active: true, playing: true, retry: false, nowS: base + 61, sinceS: base + 50, tickS: 0.2)
    #expect(w >= base + 110 && w < base + 110.5)
}

@Test func wakeRetryAfterFailureIsQuick() {
    let now = 6_000_000.0 + 3
    #expect(nextWake(mode: .lockWallpaper, active: true, playing: false, retry: true, nowS: now, sinceS: nil, tickS: 0.2) == now + 0.2)
}

@Test func wakeInactiveIsSlowFallback() {
    // 屏保让路 / 设备不在线：靠状态变化唤醒，这里只是兜底
    let now = 6_000_000.0 + 3
    #expect(nextWake(mode: .yieldToSaver, active: false, playing: true, retry: false, nowS: now, sinceS: nil, tickS: 0.2) == now + 5)
    #expect(nextWake(mode: .lyrics, active: false, playing: true, retry: true, nowS: now, sinceS: nil, tickS: 0.2) == now + 5)
}
