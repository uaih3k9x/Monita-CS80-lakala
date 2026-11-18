// 纯时钟页（≡ 键短按在歌词页 / 时钟页之间切换，fw v8）：CDE 窗口里一个大号 HH:mm + 日期，底下照旧是 Front Panel。
// 整分钟才变（帧键 = 分钟 + Front Panel 数据），平时像素不动。
import MonitaCore
import CoreGraphics
import Foundation

extension LyricsPage {
    func drawClock(_ inp: PageInput) {
        let w = Double(cv.w), h = Double(cv.h)
        cv.fillRect(0, 0, w, h, C.desk)
        let win = windowFrame(12, 12, w - 24, 1012, "Clock", false)
        let c = Calendar.current.dateComponents([.hour, .minute, .day, .month, .year, .weekday], from: inp.date)
        let mons = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
        let wds = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]

        let fx = win.minX + 40, fw = win.width - 80, fy = win.minY + 150, fh = 420.0
        box(fx, fy, fw, fh, C.well, true, 2)
        let hm = String(format: "%02d:%02d", c.hour ?? 0, c.minute ?? 0)
        let ty: Double = fy + fh / 2 + 6
        cv.text(hm, win.midX, ty,
                f(210, 700, mono: true), C.fg, .center, .middle)
        let wd: String = wds[(c.weekday ?? 1) - 1]
        let date: String = "\(mons[(c.month ?? 1) - 1]) \(c.day ?? 1), \(c.year ?? 2000)"
        let dy: Double = fy + fh
        cv.text(wd, win.midX, dy + 90, f(48, 700), C.fg, .center, .middle)
        cv.text(date, win.midX, dy + 160, f(36), C.dim, .center, .middle)

        let by = win.maxY - 50
        box(win.minX + 12, by, win.width - 24, 38, C.win, true, 1)
        cv.text("≡  Lyrics", win.minX + 24, by + 20, f(17, mono: true), C.dim, .left, .middle)

        frontPanel(12, 1040, w - 24, h - 1040 - 12, inp)
    }
}
