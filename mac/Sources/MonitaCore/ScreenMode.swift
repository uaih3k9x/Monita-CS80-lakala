// 锁屏 → 显示哪个画面（纯函数），以及壁纸 aspect-fill 几何
import Foundation

public enum ScreenMode: Equatable {
    case lyrics          // 正常：歌词页
    case clock           // 纯时钟页（≡ 键切换，fw v8）
    case lockWallpaper   // Mac 锁屏：整屏壁纸 + 小时钟
    case yieldToSaver    // 旧链路 host 自己在播视频屏保：让路，不抢屏
}

/// 用户选的页（≡ 键短按切换）；锁屏 / 屏保优先于它
public enum Page: Equatable {
    case lyrics, clock
    public var toggled: Page { self == .lyrics ? .clock : .lyrics }
}

public func screenMode(locked: Bool, saverPlaying: Bool, page: Page = .lyrics) -> ScreenMode {
    if saverPlaying { return .yieldToSaver }
    if locked { return .lockWallpaper }
    return page == .clock ? .clock : .lyrics
}

public struct FillRect: Equatable {
    public var x: Double, y: Double, w: Double, h: Double
}

/// aspect-fill：等比缩放到盖满目标，居中，超出部分由目标边界裁掉
public func aspectFill(srcW: Double, srcH: Double, dstW: Double, dstH: Double) -> FillRect {
    let s = max(dstW / srcW, dstH / srcH)
    let w = srcW * s, h = srcH * s
    return FillRect(x: (dstW - w) / 2, y: (dstH - h) / 2, w: w, h: h)
}

/// 锁屏留言第二行：按锁屏了多久给一句话（整分钟才变，和右下角时钟同步刷新）
public func awayLine(sinceS: Double?, nowS: Double) -> String {
    guard let sinceS else { return "可能出去了" }
    let m = Int(max(0, nowS - sinceS) / 60)
    if m < 1 { return "刚离开" }
    if m < 60 { return "已离开 \(m) 分钟" }
    return m % 60 == 0 ? "已离开 \(m / 60) 小时" : "已离开 \(m / 60) 小时 \(m % 60) 分"
}
