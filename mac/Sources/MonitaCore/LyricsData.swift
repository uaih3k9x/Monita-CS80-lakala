// GET /api/data/lyrics 的数据模型（tools/lyrics/lyrics.py 产出；旧 host 链路同格式）。字段缺失一律宽容处理。
import Foundation

public struct LyricLine: Equatable {
    public var t: Double
    public var x: String
    public var tr: String
    public init(t: Double, x: String, tr: String) { self.t = t; self.x = x; self.tr = tr }
}

public struct Lyric: Equatable {
    public var status = "idle"
    public var lines: [LyricLine] = []
    public var translated = false
    public init() {}
}

public struct LyricsData: Equatable {
    public var state = "stopped"
    public var title = ""
    public var artist = ""
    public var album = ""
    public var app = ""
    public var source = ""
    public var error: String?
    public var estimated = false
    public var songId = ""
    public var cover = ""
    public var play = PlayState(state: "stopped", pos: nil, at: nil, rate: nil, duration: nil)
    public var lyric = Lyric()
    public init() {}

    /// 有状态、不是 stopped、没有 error
    public var isPlaying: Bool { state != "stopped" && !state.isEmpty && error == nil }

    public static func parse(_ bytes: [UInt8]) -> LyricsData? {
        guard let obj = try? JSONSerialization.jsonObject(with: Data(bytes)) as? [String: Any] else { return nil }
        func num(_ v: Any?) -> Double? { (v as? NSNumber)?.doubleValue }
        func str(_ v: Any?) -> String {
            if let s = v as? String { return s }
            if let n = v as? NSNumber { return n.stringValue }
            return ""
        }
        var d = LyricsData()
        d.state = str(obj["state"])
        d.title = str(obj["title"])
        d.artist = str(obj["artist"])
        d.album = str(obj["album"])
        d.app = str(obj["app"])
        d.source = str(obj["source"])
        if let e = obj["error"], !(e is NSNull), !str(e).isEmpty { d.error = str(e) }
        d.estimated = (obj["estimated"] as? Bool) ?? false
        if let id = obj["songId"], !(id is NSNull) {
            if let n = id as? NSNumber { d.songId = String(n.int64Value) } else { d.songId = str(id) }
        }
        d.cover = str(obj["cover"])
        d.play = PlayState(state: d.state, pos: num(obj["pos"]), at: num(obj["at"]),
                           rate: num(obj["rate"]), duration: num(obj["duration"]))
        if let l = obj["lyric"] as? [String: Any] {
            d.lyric.status = str(l["status"])
            d.lyric.translated = (l["translated"] as? Bool) ?? false
            d.lyric.lines = ((l["lines"] as? [[String: Any]]) ?? []).map {
                LyricLine(t: num($0["t"]) ?? 0, x: str($0["x"]), tr: str($0["tr"]))
            }
        }
        return d
    }
}
