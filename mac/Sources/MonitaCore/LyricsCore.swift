// 歌词页纯函数：tools/lyrics/lyrics_core.js 的 Swift 版（行为保持一致，测试用例也同一组）
import Foundation

/// positionAt 需要的最小播放状态（at 为毫秒时间戳，pos 为秒）
public struct PlayState: Equatable {
    public var state: String
    public var pos: Double?
    public var at: Double?
    public var rate: Double?
    public var duration: Double?
    public init(state: String, pos: Double?, at: Double?, rate: Double?, duration: Double?) {
        self.state = state; self.pos = pos; self.at = at; self.rate = rate; self.duration = duration
    }
}

/// 进度插值：数据源给的是 at（毫秒）时刻的 pos（秒），按 rate 外推到 nowMs。
public func positionAt(_ d: PlayState?, nowMs: Double) -> Double {
    guard let d, let pos = d.pos else { return 0 }
    let rate = d.state == "playing" ? (d.rate ?? 1) : 0
    var p = pos + (nowMs - (d.at ?? nowMs)) / 1000 * rate
    if p < 0 { p = 0 }
    if let dur = d.duration, dur > 0, p > dur { p = dur }
    return p
}

/// 正在唱的行：最后一个 t <= pos 的下标；前奏返回 -1。
public func lineIndex(_ times: [Double], _ pos: Double) -> Int {
    var lo = 0, hi = times.count
    while lo < hi {
        let mid = (lo + hi) >> 1
        if times[mid] <= pos { lo = mid + 1 } else { hi = mid }
    }
    return lo - 1
}

public func fmtTime(_ s: Double?) -> String {
    guard let s, s.isFinite else { return "-:--" }
    let t = Int(s.rounded(.down))
    let h = t / 3600, m = (t % 3600) / 60, ss = t % 60
    return h > 0 ? String(format: "%d:%02d:%02d", h, m, ss) : String(format: "%d:%02d", m, ss)
}

/// [上一句, 当前句, 下一句, 下下句]，越界为 nil。
public func around<T>(_ lines: [T], _ idx: Int) -> [T?] {
    [idx - 1, idx, idx + 1, idx + 2].map { $0 >= 0 && $0 < lines.count ? lines[$0] : nil }
}

private let wordChars = Set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'’-.,!?")

/// 与 JS 正则 /[A-Za-z0-9'’\-.,!?]+\s*|\s+|./gu 等价的切分
func tokenize(_ s: String) -> [String] {
    let c = Array(s)
    var out: [String] = []
    var i = 0
    while i < c.count {
        var j = i
        if wordChars.contains(c[i]) {
            while j < c.count, wordChars.contains(c[j]) { j += 1 }
            while j < c.count, c[j].isWhitespace { j += 1 }
        } else if c[i].isWhitespace {
            while j < c.count, c[j].isWhitespace { j += 1 }
        } else {
            j = i + 1
        }
        out.append(String(c[i..<j]))
        i = j
    }
    return out
}

private func trimEnd(_ s: String) -> String {
    var c = Substring(s)
    while let l = c.last, l.isWhitespace { c = c.dropLast() }
    return String(c)
}

private func trimStart(_ s: String) -> String { String(s.drop(while: { $0.isWhitespace })) }

/// 折行：measure 返回像素宽；CJK 逐字断，拉丁按空格断；超出 maxLines 末行加 …。
public func wrapText(_ measure: (String) -> Double, _ text: String, _ maxW: Double, _ maxLines: Int) -> [String] {
    var lines: [String] = []
    var cur = ""
    for tok in tokenize(text) {
        let next = cur + tok
        if measure(trimEnd(next)) <= maxW || cur.isEmpty {
            cur = next
            // 单个 token 本身超宽：硬切
            while measure(trimEnd(cur)) > maxW && cur.count > 1 {
                var k = cur.count - 1
                while k > 1 && measure(String(cur.prefix(k))) > maxW { k -= 1 }
                lines.append(String(cur.prefix(k)))
                cur = String(cur.dropFirst(k))
            }
        } else {
            lines.append(trimEnd(cur))
            cur = trimStart(tok)
        }
    }
    lines.append(trimEnd(cur))
    if lines.count > maxLines {
        var kept = Array(lines.prefix(maxLines))
        var last = kept[maxLines - 1]
        while !last.isEmpty && measure(last + "…") > maxW { last.removeLast() }
        kept[maxLines - 1] = last + "…"
        return kept
    }
    return lines
}
