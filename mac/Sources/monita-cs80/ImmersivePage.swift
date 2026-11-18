// 沉浸模式（fw v9）的画法：整屏只有歌词，CDE 桌面底色，当前句大字居中、压在 Motif 选中条上，离得越远越淡（上下淡出）。
// 块布局 / 档位 / 规划是 MonitaCore/Immersive.swift 的纯逻辑；这里只负责"把第 i 块按第 c 档画到画布上"。
// 每块的高度只取决于文字（和档位无关），这样改档只重画这一块、布局不动。
import MonitaCore
import CoreGraphics
import Foundation

/// 一首歌的沉浸页内容（文字已折好行，块已排好）
struct ImmContent {
    var key: String
    var blocks: [ImmBlock]
    var lines: [[String]] = []        // 每句折好的原文
    var trs: [[String]] = []          // 每句折好的翻译（没开翻译 / 没有翻译 = 空）
    var times: [Double] = []
    var title = "", artist = ""
    var message: String?              // 没有可滚的歌词时显示在头部（未在播放 / 纯音乐 / 找不到歌词…）
}

enum Imm {
    static let font = f(54, 700), trFont = f(30)
    static let pad = 22.0, step = 68.0, trStep = 40.0, side = 60.0
    static let headerH = screenH / 2, tailH = screenH / 2
    /// 各档的字色（原文、翻译）：白 → 往桌面色里淡
    static func colors(_ cls: Int) -> (RGB, RGB) {
        func mix(_ a: RGB, _ k: Double) -> RGB {
            let m = { (v: Int, t: Int) in Int((Double(v) + Double(t - v) * k).rounded()) }
            return RGB(r: m(a.r, C.desk.r), g: m(a.g, C.desk.g), b: m(a.b, C.desk.b))
        }
        switch cls {
        case 0: return (C.white, C.trFg)
        case 1: return (mix(C.white, 0.22), mix(C.trFg, 0.40))
        case 2: return (mix(C.white, 0.48), mix(C.trFg, 0.62))
        default: return (mix(C.white, 0.70), mix(C.trFg, 0.80))
        }
    }
}

extension LyricsPage {
    /// 排版：同一份数据 + 配置 → 同一个 key（key 变了规划器就整屏重来）
    func immBuild(_ d: LyricsData?, cfg: PageConfig) -> ImmContent {
        let maxW = Double(cv.w) - Imm.side * 2
        var c = ImmContent(key: "", blocks: [])
        c.title = d?.title ?? ""; c.artist = d?.artist ?? ""
        let lyr = d?.lyric ?? Lyric()
        if d == nil || d!.state == "stopped" || d!.error != nil { c.message = "未在播放" }
        else if lyr.status == "pure" { c.message = "纯音乐，请欣赏" }
        else if lyr.status == "loading" { c.message = "正在查找歌词…" }
        else if lyr.status != "ok" || lyr.lines.isEmpty { c.message = "找不到歌词" }
        var heights: [Int] = []
        if c.message == nil {
            for l in lyr.lines {
                let x = l.x.isEmpty ? "· · ·" : l.x
                let ls = wrapText({ self.cv.measure($0, Imm.font) }, x, maxW, 3)
                let ts = cfg.translation && !l.tr.isEmpty ? wrapText({ self.cv.measure($0, Imm.trFont) }, l.tr, maxW, 2) : []
                c.lines.append(ls); c.trs.append(ts); c.times.append(l.t)
                heights.append(Int(Imm.pad * 2 + Double(ls.count) * Imm.step + (ts.isEmpty ? 0 : 6 + Double(ts.count) * Imm.trStep)))
            }
        }
        c.blocks = immLayout(headerH: Imm.headerH, lineHeights: heights, tailH: Imm.tailH)
        c.key = [d?.songId ?? "", c.title, c.artist, c.message ?? "", String(cfg.translation), heights.map(String.init).joined(separator: ",")]
            .joined(separator: "|")
        return c
    }

    /// 第 i 块按第 cls 档画，块顶在画布 y = top（裁在块里）
    func immDrawBlock(_ c: ImmContent, _ i: Int, cls: Int, top: Double) {
        let b = c.blocks[i], w = Double(cv.w), h = Double(b.h)
        cv.ctx.saveGState()
        cv.ctx.clip(to: CGRect(x: 0, y: top, width: w, height: h))
        cv.fillRect(0, top, w, h, C.desk)
        let cx = w / 2
        switch b.kind {
        case .tail:
            break
        case .header:
            // 头部的内容靠下放：前奏时窗口在最上面，头部占上半屏，紧挨着第一句
            var y = top + h - 200
            let tf = f(40, 700)
            for s in wrapText({ self.cv.measure($0, tf) }, c.title.isEmpty ? "—" : c.title, w - Imm.side * 2, 2) {
                cv.text(s, cx, y, tf, C.white, .center); y += 50
            }
            if !c.artist.isEmpty { cv.text(cv.ellipsis(c.artist, w - Imm.side * 2, f(28)), cx, y + 4, f(28), C.trFg, .center) }
            box(Imm.side, top + h - 70, w - Imm.side * 2, 6, C.desk, true, 2)          // 一道凹槽分隔线
            if let m = c.message { cv.text(m, cx, top + h - 280, f(44, 700), C.white, .center) }
        case .line(let k):
            let (fg, trFg) = Imm.colors(cls)
            if cls == 0 {
                box(Imm.side - 32, top + 6, w - (Imm.side - 32) * 2, h - 12, C.select, false, 2)
            }
            var y = top + Imm.pad + 50
            for s in c.lines[k] { cv.text(s, cx, y, Imm.font, fg, .center); y += Imm.step }
            y += 6 + 30 - 50                                                         // 翻译首行基线: 原文块下 6 px + 30
            for s in c.trs[k] { cv.text(s, cx, y, Imm.trFont, trFg, .center); y += Imm.trStep }
        }
        cv.ctx.restoreGState()
    }

    /// 预览 / 对照：窗口顶 = y、当前句 = line 时整屏的样子
    func immDrawWindow(_ c: ImmContent, y: Int, line: Int) {
        cv.fillRect(0, 0, Double(cv.w), Double(cv.h), C.desk)
        for (i, b) in c.blocks.enumerated() where b.y < y + cv.h && b.y + b.h > y {
            var cls = 0
            if case .line(let k) = b.kind { cls = immClass(k - line) }
            immDrawBlock(c, i, cls: cls, top: Double(b.y - y))
        }
    }
}
