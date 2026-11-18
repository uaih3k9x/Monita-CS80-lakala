// 极简「canvas 2D」：CoreGraphics 位图 + CoreText 文字，坐标系与浏览器 canvas 一致（左上原点、y 向下），
// 好让同一套绘制代码在浏览器原型和 Swift 之间几乎逐行对照。
import CoreGraphics
import CoreText
import Foundation
import ImageIO

struct RGB: Hashable {
    var r: Int, g: Int, b: Int
    init(_ hex: String) {
        let n = Int(hex.dropFirst(), radix: 16) ?? 0
        r = (n >> 16) & 255; g = (n >> 8) & 255; b = n & 255
    }
    init(r: Int, g: Int, b: Int) { self.r = r; self.g = g; self.b = b }
    var cg: CGColor { CGColor(srgbRed: CGFloat(r) / 255, green: CGFloat(g) / 255, blue: CGFloat(b) / 255, alpha: 1) }
}

struct FontSpec: Hashable {
    var px: Double
    var bold = false
    var mono = false
}

enum Align { case left, center, right }
enum Baseline { case alphabetic, middle }

final class Canvas {
    let w: Int, h: Int
    let ctx: CGContext
    let pixels: UnsafeMutablePointer<UInt8>   // RGBA8，行优先，第 0 行 = 屏幕最上一行
    private var fonts: [FontSpec: CTFont] = [:]

    init(w: Int, h: Int) {
        self.w = w; self.h = h
        pixels = .allocate(capacity: w * h * 4)
        pixels.initialize(repeating: 0, count: w * h * 4)
        ctx = CGContext(data: pixels, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
                        space: CGColorSpace(name: CGColorSpace.sRGB)!,
                        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue | CGBitmapInfo.byteOrder32Big.rawValue)!
        ctx.translateBy(x: 0, y: CGFloat(h))
        ctx.scaleBy(x: 1, y: -1)
        ctx.textMatrix = CGAffineTransform(scaleX: 1, y: -1)
        // 灰度抗锯齿（与 headless Chrome 一致），不要 LCD 亚像素彩边
        ctx.setShouldSmoothFonts(false)
        ctx.setAllowsFontSmoothing(false)
        ctx.interpolationQuality = .high
    }

    deinit { pixels.deallocate() }

    // MARK: 字体：Helvetica / Courier New，中日文回落 PingFang SC（再回落 Hiragino）
    func font(_ s: FontSpec) -> CTFont {
        if let f = fonts[s] { return f }
        let name = s.mono ? (s.bold ? "CourierNewPS-BoldMT" : "CourierNewPSMT") : (s.bold ? "Helvetica-Bold" : "Helvetica")
        let cjk = s.bold ? ["PingFangSC-Semibold", "HiraginoSans-W6"] : ["PingFangSC-Regular", "HiraginoSans-W3"]
        let cascade = cjk.map { CTFontDescriptorCreateWithNameAndSize($0 as CFString, CGFloat(s.px)) }
        let desc = CTFontDescriptorCreateWithAttributes([
            kCTFontNameAttribute: name,
            kCTFontCascadeListAttribute: cascade,
        ] as CFDictionary)
        let f = CTFontCreateWithFontDescriptor(desc, CGFloat(s.px), nil)
        fonts[s] = f
        return f
    }

    private func line(_ s: String, _ f: FontSpec, _ color: RGB) -> CTLine {
        let attr = NSAttributedString(string: s, attributes: [
            NSAttributedString.Key(kCTFontAttributeName as String): font(f),
            NSAttributedString.Key(kCTForegroundColorAttributeName as String): color.cg,
        ])
        return CTLineCreateWithAttributedString(attr)
    }

    func measure(_ s: String, _ f: FontSpec) -> Double {
        Double(CTLineGetTypographicBounds(line(s, f, RGB(r: 0, g: 0, b: 0)), nil, nil, nil))
    }

    func text(_ s: String, _ x: Double, _ y: Double, _ f: FontSpec, _ color: RGB,
              _ align: Align = .left, _ base: Baseline = .alphabetic) {
        guard !s.isEmpty else { return }
        let l = line(s, f, color)
        let width = Double(CTLineGetTypographicBounds(l, nil, nil, nil))
        var px = x
        if align == .center { px -= width / 2 } else if align == .right { px -= width }
        var py = y
        if base == .middle {
            // canvas 'middle' = em 框中线：基线 = y + (ascent - descent) / 2
            let font = font(f)
            py += Double(CTFontGetAscent(font) - CTFontGetDescent(font)) / 2
        }
        ctx.textPosition = CGPoint(x: px, y: py)
        CTLineDraw(l, ctx)
    }

    func ellipsis(_ s: String, _ maxW: Double, _ f: FontSpec) -> String {
        if measure(s, f) <= maxW { return s }
        var t = s
        while t.count > 1 && measure(t + "…", f) > maxW { t.removeLast() }
        return t + "…"
    }

    // MARK: 形状
    func fillRect(_ x: Double, _ y: Double, _ w: Double, _ h: Double, _ c: RGB) {
        ctx.setFillColor(c.cg)
        ctx.fill(CGRect(x: x, y: y, width: w, height: h))
    }

    func strokeRect(_ x: Double, _ y: Double, _ w: Double, _ h: Double, _ c: RGB, _ lw: Double) {
        ctx.setStrokeColor(c.cg)
        ctx.setLineWidth(lw)
        ctx.stroke(CGRect(x: x, y: y, width: w, height: h))
    }

    /// canvas 语义的圆弧（角度增大 = 屏幕顺时针），用折线近似，避开翻转坐标系里 clockwise 的歧义
    func addArc(_ cx: Double, _ cy: Double, _ r: Double, _ a0: Double, _ a1: Double, move: Bool = true) {
        let steps = max(16, Int(abs(a1 - a0) / (.pi * 2) * 96))
        for i in 0...steps {
            let a = a0 + (a1 - a0) * Double(i) / Double(steps)
            let p = CGPoint(x: cx + cos(a) * r, y: cy + sin(a) * r)
            if i == 0 && move { ctx.move(to: p) } else { ctx.addLine(to: p) }
        }
    }

    func circle(_ cx: Double, _ cy: Double, _ r: Double, fill: RGB?, stroke: RGB?, lw: Double = 2) {
        let rect = CGRect(x: cx - r, y: cy - r, width: r * 2, height: r * 2)
        if let fill { ctx.setFillColor(fill.cg); ctx.fillEllipse(in: rect) }
        if let stroke { ctx.setStrokeColor(stroke.cg); ctx.setLineWidth(lw); ctx.strokeEllipse(in: rect) }
    }

    func line(_ x0: Double, _ y0: Double, _ x1: Double, _ y1: Double, _ c: RGB, _ lw: Double, round: Bool = true) {
        ctx.setStrokeColor(c.cg)
        ctx.setLineWidth(lw)
        ctx.setLineCap(round ? .round : .butt)
        ctx.move(to: CGPoint(x: x0, y: y0))
        ctx.addLine(to: CGPoint(x: x1, y: y1))
        ctx.strokePath()
        ctx.setLineCap(.butt)
    }

    func image(_ img: CGImage, _ x: Double, _ y: Double, _ w: Double, _ h: Double) {
        ctx.saveGState()
        ctx.translateBy(x: x, y: y + h)
        ctx.scaleBy(x: 1, y: -1)
        ctx.draw(img, in: CGRect(x: 0, y: 0, width: w, height: h))
        ctx.restoreGState()
    }

    func writePNG(_ path: String) -> Bool {
        guard let img = ctx.makeImage(),
              let dest = CGImageDestinationCreateWithURL(URL(fileURLWithPath: path) as CFURL,
                                                         "public.png" as CFString, 1, nil) else { return false }
        CGImageDestinationAddImage(dest, img, nil)
        return CGImageDestinationFinalize(dest)
    }
}

func decodeImage(_ bytes: [UInt8]) -> CGImage? {
    guard let src = CGImageSourceCreateWithData(Data(bytes) as CFData, nil) else { return nil }
    return CGImageSourceCreateImageAtIndex(src, 0, nil)
}
