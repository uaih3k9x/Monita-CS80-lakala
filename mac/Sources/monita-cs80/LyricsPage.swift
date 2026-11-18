// CDE 风格网易云歌词页（CoreGraphics 绘制）。
// 差异：Front Panel 中间四个工作区按钮显示实时数据，Lock 按钮显示 Mac 防睡眠状态。
import MonitaCore
import CoreGraphics
import Foundation

// CDE Default.dp 色板
enum C {
    static let desk = RGB("#5d7f99"), win = RGB("#aeb2c3"), title = RGB("#a5627e"), titleFg = RGB("#ffffff")
    static let well = RGB("#c6cad8"), fg = RGB("#000000"), dim = RGB("#4b4f63"), faint = RGB("#6f7389")
    static let select = RGB("#5b6c8f"), selectFg = RGB("#ffffff"), trough = RGB("#8f93a6"), fill = RGB("#a5627e")
    static let panel = RGB("#9aa3ba"), warn = RGB("#e8c76a"), on = RGB("#8c8fb8")
    static let white = RGB("#ffffff"), trFg = RGB("#dfe3f2")
}

func f(_ px: Double, _ w: Int = 400, mono: Bool = false) -> FontSpec { FontSpec(px: px, bold: w >= 700, mono: mono) }

struct PageConfig: Equatable {
    var translation = true
    var title = "Lyrics — NetEase Cloud Music"
    /// 切句滚动动画（直连且固件支持 COPY 时由 main 打开；见 MonitaCore/Scroll.swift）
    var scroll = true
}

/// Front Panel 实时数据（nil = 还没拿到）
struct PanelInfo: Equatable {
    var agents: AgentCounts?
    var cpu: Int?
    var waitingLit = false          // 经 Debounced 限速后的「有 agent 在等人」
    var sleep: SleepGuard?
}

struct PageInput {
    var data: LyricsData?
    var cover: CGImage?
    var cfg = PageConfig()
    var panel = PanelInfo()
    var nowMs: Double
    var date: Date
}

final class LyricsPage {
    let cv: Canvas
    /// 进度条可填充宽度（上一次 draw 时的几何，帧键用它判断进度条像素有没有变）
    private(set) var progressInnerW = 452.0
    /// 歌词区裁剪矩形：滚动时里面的内容整体平移，COPY 就搬这块
    private(set) var lyricClip = LinkRect(x: 0, y: 0, w: 0, h: 0)
    /// 滚动状态；scrollDelta = 这一帧相对上一帧歌词区内容平移了多少像素（负 = 上移），nil = 不是纯平移
    private(set) var anim = ScrollAnim()
    private(set) var scrollDelta: Int?
    private var lastScroll: (line: Int, song: String, offset: Int)?
    private var lastLayout: (by: Double, blockH: Double)?
    init(_ cv: Canvas) { self.cv = cv }

    /// 帧键 / 唤醒用：按上一次绘制时的动画状态，此刻的偏移和是否还在动
    func scrollOffset(nowMs: Double) -> Int { anim.offset(nowMs: nowMs) }
    func scrolling(nowMs: Double) -> Bool { anim.active(nowMs: nowMs) }

    // MARK: Motif 原语
    func shades(_ c: RGB) -> (hi: RGB, lo: RGB) {
        func mix(_ t: Double, _ k: Double) -> RGB {
            let m = { (v: Int) in Int((Double(v) + (t - Double(v)) * k).rounded()) }
            return RGB(r: m(c.r), g: m(c.g), b: m(c.b))
        }
        return (mix(255, 0.55), mix(0, 0.45))
    }

    func bevel(_ x: Double, _ y: Double, _ w: Double, _ h: Double, _ base: RGB, _ sunken: Bool = false, _ width: Int = 2) {
        let s = shades(base)
        let tl = sunken ? s.lo : s.hi, br = sunken ? s.hi : s.lo
        for i in 0..<width {
            let d = Double(i)
            cv.fillRect(x + d, y + d, w - d * 2 - 1, 1, tl)
            cv.fillRect(x + d, y + d, 1, h - d * 2 - 1, tl)
            cv.fillRect(x + d, y + h - 1 - d, w - d * 2, 1, br)
            cv.fillRect(x + w - 1 - d, y + d, 1, h - d * 2, br)
        }
    }

    func box(_ x: Double, _ y: Double, _ w: Double, _ h: Double, _ base: RGB, _ sunken: Bool = false, _ width: Int = 2) {
        cv.fillRect(x, y, w, h, base)
        bevel(x, y, w, h, base, sunken, width)
    }

    private func menuItem(_ label: String, _ x: Double, _ y: Double) -> Double {
        cv.text(label, x, y, f(22), C.fg)
        cv.fillRect(x, y + 3, cv.measure(String(label.prefix(1)), f(22)), 2, C.fg)
        return x + cv.measure(label, f(22)) + 30
    }

    // MARK: 窗口框（dtwm）
    private func windowFrame(_ x: Double, _ y: Double, _ w: Double, _ h: Double, _ title: String, _ paused: Bool) -> CGRect {
        let B = 8.0, TH = 44.0
        box(x, y, w, h, C.title, false, 2)
        let s = shades(C.title), corner = 34.0
        let marks: [(Double, Double, Bool)] = [
            (x + corner, y, false), (x + w - corner, y, false), (x + corner, y + h - B, false), (x + w - corner, y + h - B, false),
            (x, y + corner, true), (x + w - B, y + corner, true), (x, y + h - corner, true), (x + w - B, y + h - corner, true),
        ]
        for (cx, cy, horiz) in marks {
            if horiz { cv.fillRect(cx + 2, cy, B - 4, 1, s.lo); cv.fillRect(cx + 2, cy + 1, B - 4, 1, s.hi) }
            else { cv.fillRect(cx, cy + 2, 1, B - 4, s.lo); cv.fillRect(cx + 1, cy + 2, 1, B - 4, s.hi) }
        }
        bevel(x + B - 2, y + B - 2, w - (B - 2) * 2, h - (B - 2) * 2, C.title, true, 2)

        let tx = x + B, ty = y + B, tw = w - B * 2
        box(tx, ty, TH, TH, C.title)
        box(tx + 10, ty + TH / 2 - 4, TH - 20, 8, C.title)
        let maxX = tx + tw - TH, minX = maxX - TH
        box(minX, ty, TH, TH, C.title)
        box(minX + TH / 2 - 4, ty + TH / 2 - 4, 8, 8, C.title)
        box(maxX, ty, TH, TH, C.title)
        box(maxX + 9, ty + 9, TH - 18, TH - 18, C.title)
        box(tx + TH, ty, minX - tx - TH, TH, C.title)
        let label = cv.ellipsis(title + (paused ? "  [Paused]" : ""), minX - tx - TH - 24, f(22, 700))
        cv.text(label, tx + TH + (minX - tx - TH) / 2, ty + TH / 2 + 1, f(22, 700), C.titleFg, .center, .middle)
        let cx = tx, cy = ty + TH, cw = tw, ch = h - B * 2 - TH
        box(cx, cy, cw, ch, C.win, false, 1)
        return CGRect(x: cx, y: cy, width: cw, height: ch)
    }

    // MARK: 图标
    private func audioIcon(_ x: Double, _ y: Double, _ size: Double) {
        let g = cv.ctx
        g.saveGState()
        g.translateBy(x: x, y: y)
        g.scaleBy(x: size / 100, y: size / 100)
        let doc = CGMutablePath()
        doc.addLines(between: [CGPoint(x: 18, y: 6), CGPoint(x: 66, y: 6), CGPoint(x: 84, y: 24), CGPoint(x: 84, y: 94), CGPoint(x: 18, y: 94)])
        doc.closeSubpath()
        g.setLineWidth(2); g.setStrokeColor(C.fg.cg)
        g.addPath(doc); g.setFillColor(RGB("#e6e6e6").cg); g.fillPath()
        g.addPath(doc); g.strokePath()
        let fold = CGMutablePath()
        fold.addLines(between: [CGPoint(x: 66, y: 6), CGPoint(x: 66, y: 24), CGPoint(x: 84, y: 24)])
        fold.closeSubpath()
        g.addPath(fold); g.setFillColor(RGB("#9c9c9c").cg); g.fillPath()
        g.addPath(fold); g.strokePath()
        // 音符
        g.setFillColor(RGB("#2f4f9e").cg)
        for (ex, ey) in [(39.0, 74.0), (63.0, 68.0)] {
            g.saveGState()
            g.translateBy(x: ex, y: ey); g.rotate(by: -0.4)
            g.fillEllipse(in: CGRect(x: -9, y: -7, width: 18, height: 14))
            g.restoreGState()
        }
        g.fill(CGRect(x: 46, y: 34, width: 4, height: 40))
        g.fill(CGRect(x: 70, y: 28, width: 4, height: 40))
        let beam = CGMutablePath()
        beam.addLines(between: [CGPoint(x: 46, y: 34), CGPoint(x: 74, y: 28), CGPoint(x: 74, y: 38), CGPoint(x: 46, y: 44)])
        beam.closeSubpath()
        g.addPath(beam); g.fillPath()
        g.restoreGState()
    }

    private func infoIcon(_ x: Double, _ y: Double, _ r: Double, _ glyph: String) {
        cv.circle(x, y, r, fill: C.white, stroke: C.fg, lw: 2)
        cv.text(glyph, x, y + 2, f(r * 1.3, 700, mono: glyph == "i"), RGB("#2f4f9e"), .center, .middle)
    }

    // MARK: Front Panel
    private func frontPanel(_ x: Double, _ y: Double, _ w: Double, _ h: Double, _ inp: PageInput) {
        box(x, y, w, h, C.panel, false, 2)
        let pad = 10.0
        func slot(_ sx: Double, _ sw: Double) -> CGRect {
            box(sx, y + pad, sw, h - pad * 2, C.panel, false, 2)
            return CGRect(x: sx, y: y + pad, width: sw, height: h - pad * 2)
        }
        let cal = Calendar.current
        let comp = cal.dateComponents([.hour, .minute, .day, .month, .weekday], from: inp.date)
        let hr = comp.hour ?? 0, mi = comp.minute ?? 0

        // 时钟（分钟级）
        var r = slot(x + pad, 140)
        let cx = r.midX, cy = r.midY - 8, rad = min(r.width, r.height) / 2 - 22
        cv.circle(cx, cy, rad, fill: RGB("#e9e9ef"), stroke: C.fg, lw: 2)
        for i in 0..<12 {
            let a = Double(i) / 12 * .pi * 2
            cv.fillRect(cx + sin(a) * (rad - 7) - 1.5, cy - cos(a) * (rad - 7) - 1.5, 3, 3, C.fg)
        }
        func hand(_ ang: Double, _ len: Double, _ wd: Double) {
            cv.line(cx, cy, cx + sin(ang) * len, cy - cos(ang) * len, C.fg, wd)
        }
        hand((Double(hr % 12) + Double(mi) / 60) / 12 * .pi * 2, rad * 0.55, 4)
        hand(Double(mi) / 60 * .pi * 2, rad * 0.85, 3)
        cv.text(String(format: "%02d:%02d", hr, mi), cx, r.maxY - 10, f(18, 700, mono: true), C.fg, .center)

        // 日历
        r = slot(r.maxX + 6, 120)
        box(r.minX + 18, r.minY + 22, r.width - 36, r.height - 56, C.white, true, 2)
        cv.fillRect(r.minX + 20, r.minY + 24, r.width - 40, 26, C.title)
        let mons = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
        let wds = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        cv.text(mons[(comp.month ?? 1) - 1], r.midX, r.minY + 38, f(17, 700), C.white, .center, .middle)
        cv.text(String(comp.day ?? 1), r.midX, r.maxY - 46, f(44, 700), C.fg, .center)
        cv.text(wds[(comp.weekday ?? 1) - 1], r.midX, r.maxY - 12, f(16), C.fg, .center)

        // 中间四格：实时数据（大号数字 + 小标签），Two 有 agent 在等人时高亮（限速见 Debounced）
        r = slot(r.maxX + 6, 250)
        let bw = (r.width - 30) / 2, bh = (r.height - 30) / 2
        let a = inp.panel.agents
        let dash = "–"
        let cells: [(String, String, Bool)] = [
            (a.map { String($0.claude) } ?? dash, "Claude", false),
            (a.map { String($0.waiting) } ?? dash, "Waiting", inp.panel.waitingLit),
            (a.map { String($0.dsh) } ?? dash, "DSH", false),
            (inp.panel.cpu.map { "\($0)%" } ?? dash, "CPU", false),
        ]
        for (i, cell) in cells.enumerated() {
            let bx = r.minX + 10 + Double(i % 2) * (bw + 10)
            let by = r.minY + 10 + Double(i / 2) * (bh + 10)
            box(bx, by, bw, bh, cell.2 ? C.warn : C.panel, cell.2, 2)
            cv.text(cell.0, bx + bw / 2, by + bh / 2 - 8, f(34, 700), C.fg, .center, .middle)
            cv.text(cell.1, bx + bw / 2, by + bh - 14, f(15, cell.2 ? 700 : 400), C.fg, .center)
        }

        // 右侧：Lock（防睡眠）+ Lyrics（当前页）
        let rest = x + w - pad - (r.maxX + 6)
        r = slot(r.maxX + 6, rest)
        let half = (r.width - 30) / 2
        let lx = r.minX + 10, ly = r.minY + 10, lh = r.height - 20
        box(lx, ly, half, lh, C.panel, false, 2)
        lockButton(lx, ly, half, lh, inp.panel.sleep)
        let ax = lx + half + 10
        box(ax, ly, half, lh, C.on, true, 2)
        audioIcon(ax + half / 2 - 34, ly + 14, 68)
        cv.text("Lyrics", ax + half / 2, ly + r.height - 34, f(16, 700), C.fg, .center)
    }

    /// 挂锁：锁扣合上 = Awake（有进程挡系统睡眠）；锁扣抬起打开 = Sleep OK
    private func lockButton(_ lx: Double, _ ly: Double, _ half: Double, _ lh: Double, _ sg: SleepGuard?) {
        let mx = lx + half / 2
        let awake = sg?.awake ?? false
        let g = cv.ctx
        g.setStrokeColor(C.fg.cg); g.setLineWidth(4)
        if awake {
            cv.addArc(mx, ly + 46, 16, .pi, 2 * .pi)
            g.strokePath()
        } else {
            // 抬高 12px，只有左腿插回锁体
            cv.addArc(mx, ly + 34, 16, .pi, 2 * .pi)
            g.addLine(to: CGPoint(x: mx + 16, y: ly + 38))
            g.strokePath()
            cv.line(mx - 16, ly + 34, mx - 16, ly + 46, C.fg, 4, round: false)
        }
        cv.fillRect(mx - 24, ly + 46, 48, 38, RGB("#d6b44a"))
        cv.strokeRect(mx - 24, ly + 46, 48, 38, C.fg, 2)
        if let sg, awake, let holder = sg.holder {
            let extra = sg.others > 0 ? " +\(sg.others)" : ""
            let fs = f(13)
            let s = cv.ellipsis(holder, half - 10 - cv.measure(extra, fs), fs) + extra
            cv.text(s, mx, ly + lh - 40, fs, C.dim, .center)
        }
        cv.text(sg == nil ? "Lock" : awake ? "Awake" : "Sleep OK", mx, ly + lh - 14, f(16, awake ? 700 : 400), C.fg, .center)
    }

    // MARK: 歌词区
    private func fitLine(_ s: String, _ x: Double, _ y: Double, _ maxW: Double, _ px: Double, _ color: RGB) {
        var size = px
        while size > 24 && cv.measure(s, f(size)) > maxW { size -= 1 }
        cv.text(cv.ellipsis(s, maxW, f(size)), x, y, f(size), color, .center)
    }

    private func lyricArea(_ r: CGRect, _ inp: PageInput) {
        let clip = CGRect(x: r.minX + 3, y: r.minY + 3, width: r.width - 6, height: r.height - 6)
        lyricClip = LinkRect(x: Int(clip.minX), y: Int(clip.minY), w: Int(clip.width), h: Int(clip.height))
        scrollDelta = nil
        var listDrawn = false                   // 提示信息页（未播放/找歌词…）不是列表：断开平移链
        defer { if !listDrawn { lastScroll = nil; lastLayout = nil } }
        box(r.minX, r.minY, r.width, r.height, C.well, true, 2)
        let d = inp.data
        let lyr = d?.lyric ?? Lyric()
        let cx = r.midX
        func message(_ glyph: String, _ big: String, _ small: String?) {
            infoIcon(cx, r.minY + r.height / 2 - 90, 34, glyph)
            cv.text(big, cx, r.minY + r.height / 2 + 10, f(36, 700), C.fg, .center, .middle)
            if let small { cv.text(small, cx, r.minY + r.height / 2 + 60, f(22), C.dim, .center, .middle) }
        }
        guard let d, d.state != "stopped", d.error == nil else {
            let e = d?.error.map { "数据源：" + String($0.prefix(40)) }
            return message("i", "未在播放", e ?? "No media is playing")
        }
        if lyr.status == "pure" { return message("♪", "纯音乐，请欣赏", "Instrumental") }
        if lyr.status == "loading" { return message("i", "正在查找歌词…", "Searching music.163.com") }
        if lyr.status != "ok" || lyr.lines.isEmpty {
            return message("?", "找不到歌词", lyr.status == "error" ? "网易云接口暂时连不上，稍后重试" : "No synced lyrics for this track")
        }

        let lines = lyr.lines
        let pos = positionAt(d.play, nowMs: inp.nowMs)
        let idx = lineIndex(lines.map(\.t), pos + lyricLeadS)   // 提前 150 ms：推帧+上屏的延迟
        func at(_ i: Int) -> LyricLine? { i >= 0 && i < lines.count ? lines[i] : nil }
        let cur = at(idx)

        cv.ctx.saveGState()
        cv.ctx.clip(to: clip)
        let pad = 34.0, maxW = r.width - pad * 2
        func gap(_ l: LyricLine?) -> String { l.map { $0.x.isEmpty ? "· · ·" : $0.x } ?? "" }

        let curFont = f(50, 700)
        let curLines = cur != nil ? wrapText({ self.cv.measure($0, curFont) }, gap(cur), maxW - 24, 2) : [idx < 0 ? "♪" : ""]
        let trLines = (cur != nil && inp.cfg.translation && !(cur!.tr.isEmpty))
            ? wrapText({ self.cv.measure($0, f(28)) }, cur!.tr, maxW - 24, 2) : []
        let blockH = 36 + Double(curLines.count) * 62 + Double(trLines.count) * 36 + (trLines.isEmpty ? 0 : 8)
        let midY = r.minY + (r.height * 0.42).rounded()
        let restBy = (midY - blockH / 2).rounded()
        // 滚动：前进一句时，新的当前句从它在旧布局里（下一句的位置）滑到中间
        let song = d.songId + "|" + d.title
        if inp.cfg.scroll {
            let old = lastLayout ?? (restBy, blockH)
            anim.update(line: idx, song: song, nowMs: inp.nowMs, distance: (old.by + old.blockH + 56) - (restBy + 68))
        } else {
            anim = ScrollAnim()
        }
        lastLayout = (Double(restBy), Double(blockH))
        let off = anim.offset(nowMs: inp.nowMs)
        if let l = lastScroll, l.line == idx, l.song == song { scrollDelta = off - l.offset }
        lastScroll = (idx, song, off)
        listDrawn = true
        let by = restBy + CGFloat(off)

        // 上面最多三句（滚动时露出来的也要有），下面四句；区域外的被裁掉
        for k in 1...3 { if let l = at(idx - k) { fitLine(gap(l), cx, by - 30 - Double(k - 1) * 58, maxW, 32, C.faint) } }
        let sel = d.state != "playing" ? C.trough : C.select
        cv.fillRect(r.minX + pad - 12, by, r.width - (pad - 12) * 2, blockH, sel)
        bevel(r.minX + pad - 12, by, r.width - (pad - 12) * 2, blockH, sel, false, 2)
        var y = by + 18 + 50
        for s in curLines { cv.text(s, cx, y, curFont, C.selectFg, .center); y += 62 }
        y -= 8
        for s in trLines { cv.text(s, cx, y, f(28), C.trFg, .center); y += 36 }

        var ny = by + blockH + 56
        for k in 1...4 {
            guard let l = at(idx + k) else { break }
            fitLine(gap(l), cx, ny, maxW, 32, C.dim)
            ny += 58
        }
        cv.ctx.restoreGState()
    }

    private func progress(_ r: CGRect, _ d: LyricsData?, _ nowMs: Double) {
        let pos = d.map { positionAt($0.play, nowMs: nowMs) }
        let dur = d?.play.duration
        let tw = 130.0
        cv.text(fmtTime(pos), r.minX, r.midY + 1, f(24, 700, mono: true), C.fg, .left, .middle)
        cv.text(fmtTime(dur), r.maxX, r.midY + 1, f(24, 700, mono: true), C.fg, .right, .middle)
        let bx = r.minX + tw, bw = r.width - tw * 2
        box(bx, r.minY, bw, r.height, C.trough, true, 2)
        progressInnerW = bw - 8
        if let pos, let dur, dur > 0 {
            let fw = progressFill(pos: pos, dur: dur, width: bw - 8)
            if fw > 0 {
                cv.fillRect(bx + 4, r.minY + 4, fw, r.height - 8, C.fill)
                bevel(bx + 4, r.minY + 4, max(fw, 4), r.height - 8, C.fill, false, 2)
            }
        }
    }

    // MARK: 整页
    func draw(_ inp: PageInput) {
        let w = Double(cv.w), h = Double(cv.h)
        cv.fillRect(0, 0, w, h, C.desk)
        let d = inp.data
        let playing = d?.isPlaying ?? false
        let win = windowFrame(12, 12, w - 24, 1012, inp.cfg.title, playing && d?.state == "paused")

        var mx = win.minX + 20
        let my = win.minY + 30
        for m in ["File", "Edit", "View", "Options"] { mx = menuItem(m, mx, my) }
        _ = menuItem("Help", win.maxX - 70, my)
        cv.fillRect(win.minX + 4, win.minY + 44, win.width - 8, 1, shades(C.win).lo)
        cv.fillRect(win.minX + 4, win.minY + 45, win.width - 8, 1, shades(C.win).hi)

        // 封面 + 歌名歌手
        let top = win.minY + 62, cs = 220.0
        box(win.minX + 20, top, cs + 12, cs + 12, C.win, true, 2)
        if playing, let img = inp.cover {
            cv.image(img, win.minX + 26, top + 6, cs, cs)
        } else {
            cv.fillRect(win.minX + 24, top + 4, cs + 4, cs + 4, C.well)
            audioIcon(win.minX + 26 + cs / 2 - 70, top + 6 + cs / 2 - 70, 140)
        }
        let ix = win.minX + 20 + cs + 12 + 26
        let iw = win.maxX - 20 - ix
        func field(_ s: String, _ y: Double) { cv.text(s, ix, y, f(18, 700), C.dim) }
        field("Title:", top + 22)
        if playing, let d {
            let one = cv.measure(d.title, f(38, 700)) <= iw
            let tf = one ? f(38, 700) : f(30, 700)
            let tl = one ? [d.title] : wrapText({ self.cv.measure($0, tf) }, d.title, iw, 2)
            for (i, s) in tl.enumerated() { cv.text(s, ix, top + (one ? 66 : 60) + Double(i) * 36, tf, C.fg) }
            let ay = top + (one ? 66 : 60 + 36) + 30
            field("Artist:", ay)
            cv.text(cv.ellipsis(d.artist.isEmpty ? "—" : d.artist, iw, f(26)), ix, ay + 34, f(26), C.fg)
            if one && !d.album.isEmpty && d.album != d.title {
                cv.text(cv.ellipsis(d.album, iw, f(20)), ix, ay + 64, f(20), C.dim)
            }
        } else {
            cv.text("—", ix, top + 66, f(38, 700), C.dim)
            field("Artist:", top + 126)
            cv.text("—", ix, top + 160, f(26), C.dim)
        }
        // 状态行
        let sy = top + cs + 12 - 26
        let isPlay = playing && d?.state == "playing"
        box(ix, sy - 9, 18, 18, isPlay ? RGB("#57b05a") : C.win, true, 2)
        cv.text(playing ? (isPlay ? "Playing" : "Paused") : "Idle", ix + 28, sy + 1, f(20), C.fg, .left, .middle)
        if playing && (d?.estimated ?? false) {
            box(ix + 150, sy - 16, 176, 32, C.warn, false, 2)
            cv.text("进度为估算", ix + 238, sy + 1, f(19, 700), C.fg, .center, .middle)
        }

        let lr = CGRect(x: win.minX + 20, y: top + cs + 34, width: win.width - 40, height: 520)
        lyricArea(lr, inp)
        progress(CGRect(x: win.minX + 20, y: lr.maxY + 24, width: win.width - 40, height: 34), playing ? d : nil, inp.nowMs)

        // 底部消息栏
        let fy = win.maxY - 50
        box(win.minX + 12, fy, win.width - 24, 38, C.win, true, 1)
        var parts: [String] = [!playing ? "No player" : d?.source == "mediaremote" ? "MediaRemote" : "estimate"]
        if playing, let d {
            if !d.app.isEmpty { parts.append(d.app.hasPrefix("com.") ? String(d.app.dropFirst(4)) : d.app) }
            if !d.songId.isEmpty { parts.append("id " + d.songId) }
            if d.lyric.translated { parts.append("tr") }
        }
        let fm = f(17, mono: true)
        cv.text(cv.ellipsis(parts.joined(separator: "  ·  "), win.width - 60, fm), win.minX + 24, fy + 20, fm, C.dim, .left, .middle)

        frontPanel(12, 1040, w - 24, h - 1040 - 12, inp)
    }
}
