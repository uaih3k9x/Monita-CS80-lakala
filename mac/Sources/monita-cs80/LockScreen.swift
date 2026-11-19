// 锁屏画面：整屏桌面壁纸（aspect-fill 居中裁剪）+ 右下角 Motif 小时钟（整分钟才变）。
// 拿不到壁纸（Aerial / 纯色 / 读不了）时回退到 CDE 风格的锁屏框。
import AppKit
import MonitaCore
import CoreGraphics
import Foundation
import ImageIO

/// 当前主屏壁纸：URL + mtime 做缓存键，缩放结果（800×1280）只算一次
/// 渲染线程只读 image；刷新走 refreshInBackground（后台队列，正在刷就不再排）。
/// 以前渲染线程每分钟在锁屏页里同步刷新：NSWorkspace 要在主线程问，锁屏久了（显示器睡着）主线程那一下会卡住，
/// 渲染线程跟着卡到解锁后 31 s 才切回歌词（日志里整整 3 小时没有渲染统计）。
final class Wallpaper {
    private let lock = NSLock()
    private var key = ""
    private var _image: CGImage?
    var image: CGImage? { lock.lock(); defer { lock.unlock() }; return _image }
    private let queue = DispatchQueue(label: "monita.wallpaper", qos: .utility)
    private lazy var coalescer = Coalescer(schedule: { [queue] in queue.async(execute: $0) })

    /// 后台刷新；换了就 requestFull（渲染循环下一次推整帧）
    func refreshInBackground() {
        coalescer.request { [self] in if refresh() { shared.requestFull() } }
    }

    /// 同步刷新（--png 用；别在渲染线程上调）。返回 true 表示壁纸换了（需要整帧）
    @discardableResult
    func refresh() -> Bool {
        let url = currentURL()
        var k = "none"
        if let url {
            let mtime = (try? FileManager.default.attributesOfItem(atPath: url.path)[.modificationDate] as? Date)?
                .timeIntervalSince1970 ?? 0
            k = url.path + "|" + String(mtime)
        }
        lock.lock(); let same = k == key; lock.unlock()
        if same { return false }
        let img = url.flatMap { load($0) }
        lock.lock(); key = k; _image = img; lock.unlock()
        log("壁纸：\(url?.path ?? "无（回退 CDE 锁屏）")\(url != nil && img == nil ? "（解码失败，回退）" : "")")
        return true
    }

    private func currentURL() -> URL? {
        // NSWorkspace 要求主线程
        var u: URL?
        let get = { if let s = NSScreen.main ?? NSScreen.screens.first { u = NSWorkspace.shared.desktopImageURL(for: s) } }
        if Thread.isMainThread { get() } else { DispatchQueue.main.sync(execute: get) }
        guard let u, u.isFileURL else { return nil }
        var isDir: ObjCBool = false
        // 有的设置给的是目录（轮换文件夹）或已删除的文件
        guard FileManager.default.fileExists(atPath: u.path, isDirectory: &isDir), !isDir.boolValue else { return nil }
        return u
    }

    private func load(_ url: URL) -> CGImage? {
        guard let src = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
        // 动态壁纸 .heic 是多帧：取主帧（通常就是第一帧）。大图先用缩略图接口降到够用的尺寸，省内存
        let idx = CGImageSourceGetPrimaryImageIndex(src)
        let opts: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceThumbnailMaxPixelSize: 2600,
            kCGImageSourceCreateThumbnailWithTransform: true,
        ]
        guard let img = CGImageSourceCreateThumbnailAtIndex(src, idx, opts as CFDictionary)
                ?? CGImageSourceCreateImageAtIndex(src, idx, nil) else { return nil }
        // 预先缩放到 800×1280（高质量插值），之后每帧只是 blit
        guard let ctx = CGContext(data: nil, width: screenW, height: screenH, bitsPerComponent: 8, bytesPerRow: 0,
                                  space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }
        ctx.interpolationQuality = .high
        let r = aspectFill(srcW: Double(img.width), srcH: Double(img.height), dstW: Double(screenW), dstH: Double(screenH))
        ctx.draw(img, in: CGRect(x: r.x, y: r.y, width: r.w, height: r.h))
        return ctx.makeImage()
    }
}

extension LyricsPage {
    func drawLock(wallpaper: CGImage?, date: Date, host: String, since: Date? = nil) {
        let w = Double(cv.w), h = Double(cv.h)
        if let wallpaper {
            cv.image(wallpaper, 0, 0, w, h)
        } else {
            cdeLockDialog(w, h, host)
        }
        lockClock(w, h, date)
        awayNote(w, h, date, since)
    }

    /// 左下角留言条（和时钟同高并排）：「<名字> 暂时不在」（名字见 --name / MONITA_AWAY_NAME）+ 离开多久
    private func awayNote(_ w: Double, _ h: Double, _ date: Date, _ since: Date?) {
        let pw = w - 16 - 240 - 16 - 16, ph = 96.0, x = 16.0, y = h - 16 - ph
        box(x, y, pw, ph, C.panel, false, 2)
        box(x + 8, y + 8, pw - 16, ph - 16, C.well, true, 2)
        let line = since == nil ? "可能出去了" : "可能出去了 · " + awayLine(sinceS: since!.timeIntervalSince1970, nowS: date.timeIntervalSince1970)
        cv.text("\(awayName) 暂时不在", x + 26, y + 40, f(30, 700), C.fg, .left, .middle)
        cv.text(line, x + 26, y + 74, f(18, 700), C.dim, .left, .middle)
    }

    private func cdeLockDialog(_ w: Double, _ h: Double, _ host: String) {
        cv.fillRect(0, 0, w, h, C.desk)
        let dw = 560.0, dh = 360.0, x = (w - dw) / 2, y = (h - dh) / 2 - 80
        box(x, y, dw, dh, C.title, false, 2)
        box(x + 8, y + 8, dw - 16, 44, C.title)
        cv.text("Screen Locked", x + dw / 2, y + 31, f(22, 700), C.titleFg, .center, .middle)
        box(x + 8, y + 52, dw - 16, dh - 60, C.win, false, 1)
        // 挂锁
        let mx = x + 110, ly = y + 110
        cv.ctx.setStrokeColor(C.fg.cg); cv.ctx.setLineWidth(6)
        cv.addArc(mx, ly + 40, 26, .pi, 2 * .pi)
        cv.ctx.strokePath()
        cv.fillRect(mx - 38, ly + 40, 76, 60, RGB("#d6b44a"))
        cv.strokeRect(mx - 38, ly + 40, 76, 60, C.fg, 2)
        cv.text("This display is locked.", x + 190, y + 140, f(24, 700), C.fg)
        cv.text(host, x + 190, y + 178, f(20), C.dim)
        cv.text("Password:", x + 190, y + 236, f(20, 700), C.dim)
        box(x + 190, y + 254, dw - 230, 40, C.well, true, 2)
    }

    /// 右下角 Motif 小面板：HH:mm + 日期，整分钟才变 → 平时像素不动
    private func lockClock(_ w: Double, _ h: Double, _ date: Date) {
        let pw = 240.0, ph = 96.0, x = w - 16 - pw, y = h - 16 - ph
        box(x, y, pw, ph, C.panel, false, 2)
        box(x + 8, y + 8, pw - 16, ph - 16, C.well, true, 2)
        let c = Calendar.current.dateComponents([.hour, .minute, .day, .month, .weekday], from: date)
        let mons = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
        let wds = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        cv.text(String(format: "%02d:%02d", c.hour ?? 0, c.minute ?? 0), x + pw / 2, y + 44, f(44, 700, mono: true), C.fg, .center, .middle)
        cv.text("\(wds[(c.weekday ?? 1) - 1])  \(mons[(c.month ?? 1) - 1]) \(c.day ?? 1)", x + pw / 2, y + 76, f(17, 700), C.dim, .center, .middle)
    }
}
