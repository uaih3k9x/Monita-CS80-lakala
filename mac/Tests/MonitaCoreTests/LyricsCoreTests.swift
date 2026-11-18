// 与 panel/tests/lyrics_core.test.mjs 同一组用例：Swift 版和浏览器版行为一致
import Testing
import MonitaCore

private func play(_ state: String, pos: Double, at: Double, rate: Double = 1, duration: Double? = nil) -> PlayState {
    PlayState(state: state, pos: pos, at: at, rate: rate, duration: duration)
}

@Test func positionAtExtrapolatesWhilePlaying() {
    #expect(positionAt(play("playing", pos: 10, at: 1_000_000, duration: 200), nowMs: 1_003_500) == 13.5)
}

@Test func positionAtPausedClampsAndFloors() {
    #expect(positionAt(play("paused", pos: 10, at: 0), nowMs: 99_000) == 10)
    #expect(positionAt(play("playing", pos: 199, at: 0, duration: 200), nowMs: 50_000) == 200)
    #expect(positionAt(play("playing", pos: 0, at: 10_000), nowMs: 5_000) == 0)
    #expect(positionAt(nil, nowMs: 1) == 0)
}

@Test func lineIndexBinarySearch() {
    let t: [Double] = [1, 5, 9]
    #expect(lineIndex(t, 0.2) == -1)
    #expect(lineIndex(t, 1) == 0)
    #expect(lineIndex(t, 8.99) == 1)
    #expect(lineIndex(t, 100) == 2)
    #expect(lineIndex([], 3) == -1)
}

@Test func fmtTimeFormats() {
    #expect(fmtTime(0) == "0:00")
    #expect(fmtTime(65.9) == "1:05")
    #expect(fmtTime(3600 + 61) == "1:01:01")
    #expect(fmtTime(nil) == "-:--")
    #expect(fmtTime(.infinity) == "-:--")
}

@Test func aroundPicksNeighbours() {
    let l = ["a", "b", "c", "d"]
    #expect(around(l, 1) == ["a", "b", "c", "d"])
    #expect(around(l, 0) == [nil, "a", "b", "c"])
    #expect(around(l, 3) == ["c", "d", nil, nil])
    #expect(around(l, -1) == [nil, nil, "a", "b"])
}

@Test func wrapTextByWidthWithEllipsis() {
    let m: (String) -> Double = { Double($0.count * 10) }
    #expect(wrapText(m, "一二三四五六", 30, 3) == ["一二三", "四五六"])
    #expect(wrapText(m, "一二三四五六七", 30, 2) == ["一二三", "四五…"])
    #expect(wrapText(m, "ab cd ef", 50, 3) == ["ab cd", "ef"])
    #expect(wrapText(m, "", 50, 2) == [""])
}

@Test func wrapTextHardSplitsOverlongWord() {
    let m: (String) -> Double = { Double($0.count * 10) }
    #expect(wrapText(m, "abcdefgh", 30, 5) == ["abc", "def", "gh"])
}

@Test func parseLyricsPayload() throws {
    let json = """
    {"state":"playing","title":"T","artist":"A","duration":202.5,"pos":5.9,"at":1794975060781,"rate":1.0,
     "app":"com.netease.163music","source":"mediaremote","estimated":false,"songId":2617392439,
     "cover":"/api/lyrics/cover?id=2617392439",
     "lyric":{"status":"ok","translated":true,"lines":[{"t":0.0,"x":"作词","tr":""},{"t":3.6,"x":"x","tr":"译"}]}}
    """
    let d = try #require(LyricsData.parse(Array(json.utf8)))
    #expect(d.state == "playing")
    #expect(d.play.at == 1794975060781)
    #expect(d.songId == "2617392439")
    #expect(d.lyric.status == "ok")
    #expect(d.lyric.lines.count == 2)
    #expect(d.lyric.lines[1].tr == "译")
    #expect(d.lyric.translated)
    #expect(d.isPlaying)
    #expect(LyricsData.parse(Array("{\"state\":\"stopped\"}".utf8))?.isPlaying == false)
    #expect(LyricsData.parse(Array("{\"state\":\"playing\",\"error\":\"x\"}".utf8))?.isPlaying == false)
    #expect(LyricsData.parse(Array("nope".utf8)) == nil)
}
