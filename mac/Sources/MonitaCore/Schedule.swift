// 省电：渲染循环什么时候醒、醒了要不要真画（纯函数）。
//   * 帧键（FrameKey）只含画面真正依赖的量：数据（去掉 pos/at 时间戳）、显示的秒数、当前歌词行、
//     进度条像素宽、墙上分钟；键没变就不画、不转 BGR565、不做 diff。
//   * 播放中按 tick 醒；暂停/无播放睡到下一个整分钟；锁屏睡到下一次「时钟/已离开 N 分钟」变化。
//     数据、锁屏、屏保、设备状态一变由 Shared 立即唤醒（见 main.swift），不靠轮询。
import Foundation

/// 换行提前量：推帧 + 上屏的延迟
public let lyricLeadS = 0.15

/// 进度条填充像素宽（LyricsPage 画进度条和帧键共用）
public func progressFill(pos: Double, dur: Double, width: Double) -> Double {
    (width * min(1, pos / dur)).rounded()
}

// MARK: 锁屏

public struct LockFrameKey: Equatable {
    var minute: Int
    var away: Int?
}

/// 锁屏画面依赖的量：右下角时钟（墙上分钟）+ 留言条「已离开 N 分钟」（相对锁屏时刻的分钟）
public func lockFrameKey(nowS: Double, sinceS: Double?) -> LockFrameKey {
    LockFrameKey(minute: Int((nowS / 60).rounded(.down)),
                 away: sinceS.map { Int(max(0, nowS - $0) / 60) })
}

/// 下一次锁屏画面会变的时刻（秒）：墙上整分钟和「已离开」分钟边界里早的那个
public func nextLockChange(nowS: Double, sinceS: Double?) -> Double {
    let wall = ((nowS / 60).rounded(.down) + 1) * 60
    guard let sinceS else { return wall }
    let elapsed = max(0, nowS - sinceS)   // 时钟回拨（since 在未来）按刚锁算
    return min(wall, sinceS + ((elapsed / 60).rounded(.down) + 1) * 60)
}

// MARK: 歌词页

public struct LyricsFrameKey: Equatable {
    var data: LyricsData?
    var sec: Int
    public internal(set) var line: Int
    var fill: Int
    var minute: Int
}

public func lyricsFrameKey(_ d: LyricsData?, nowMs: Double, progressW: Double) -> LyricsFrameKey {
    let minute = Int((nowMs / 60_000).rounded(.down))
    guard var d else { return LyricsFrameKey(data: nil, sec: 0, line: 0, fill: 0, minute: minute) }
    let pos = positionAt(d.play, nowMs: nowMs)
    let dur = d.play.duration ?? 0
    let fill = dur > 0 ? Int(progressFill(pos: pos, dur: dur, width: progressW)) : 0
    let line = lineIndex(d.lyric.lines.map(\.t), pos + lyricLeadS)
    d.play.pos = nil
    d.play.at = nil   // 数据源每次都重新打时间戳；位置已体现在 sec/line/fill 里
    return LyricsFrameKey(data: d, sec: Int(pos.rounded(.down)), line: line, fill: fill, minute: minute)
}

// MARK: 唤醒时刻

/// 整分钟边界之后多睡一点，保证醒来时 Date() 已经跨过边界
let boundaryEpsS = 0.02
/// 屏保让路 / 设备不在线：状态变化会立即唤醒，这里只是兜底
let inactiveWakeS = 5.0

/// 渲染循环下一次醒来的绝对时刻（秒）
public func nextWake(mode: ScreenMode, active: Bool, playing: Bool, retry: Bool,
                     nowS: Double, sinceS: Double?, tickS: Double) -> Double {
    if !active { return nowS + inactiveWakeS }
    if retry { return nowS + tickS }
    switch mode {
    case .lockWallpaper:
        return nextLockChange(nowS: nowS, sinceS: sinceS) + boundaryEpsS
    case .lyrics where playing:
        return nowS + tickS
    case .lyrics, .clock:
        return ((nowS / 60).rounded(.down) + 1) * 60 + boundaryEpsS
    case .yieldToSaver, .immersive:      // 沉浸模式自己排下一句的时刻（monita-cs80/Immersive.swift），这里只是兜底
        return nowS + inactiveWakeS
    }
}
