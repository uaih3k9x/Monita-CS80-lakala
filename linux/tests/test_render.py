"""渲染：尺寸、关键点取色、提示页、RGB565 转换、折行（纯函数，不要字体）"""
import json
import os
import time
import unittest

from util import REPO
import monita_link as ml
from monita_linux.render import canvas as CV
from monita_linux.render import palette as P
from monita_linux.render.fonts import FontError, find_fonts
from monita_linux.render.lyrics_page import panel_cells

NOW = 1795651620.0          # 固定时刻
LOCAL = time.struct_time((2026, 11, 26, 21, 7, 0, 3, 330, 0))


def sample(name, **kw):
    with open(os.path.join(REPO, "mac", "Samples", "lyrics-%s.json" % name), encoding="utf-8") as f:
        d = json.load(f)
    d["at"] = NOW * 1000
    d.update(kw)
    return d


class Wrap(unittest.TestCase):
    m = staticmethod(lambda s: len(s) * 10.0)

    def test_tokenize(self):
        self.assertEqual(CV.tokenize("Hello world, 你好"), ["Hello ", "world, ", "你", "好"])

    def test_cjk_breaks_per_char(self):
        self.assertEqual(CV.wrap_text(self.m, "一二三四五六", 30, 3), ["一二三", "四五六"])

    def test_latin_breaks_on_space(self):
        self.assertEqual(CV.wrap_text(self.m, "aa bb cc", 50, 3), ["aa bb", "cc"])

    def test_ellipsis_on_overflow(self):
        out = CV.wrap_text(self.m, "一二三四五六七八九", 30, 2)
        self.assertEqual(out, ["一二三", "四五…"])

    def test_hard_cut_long_word(self):
        self.assertEqual(CV.wrap_text(self.m, "abcdefgh", 30, 5), ["abc", "def", "gh"])

    def test_is_cjk(self):
        self.assertTrue(CV.is_cjk("中") and CV.is_cjk("，") and not CV.is_cjk("A"))


class FontCheck(unittest.TestCase):
    def test_cjk_font_without_cjk_glyphs_is_error(self):
        # fc-match 总会给个"最接近的"：没装 Noto CJK 时 CJK 那一项会落到纯拉丁字体上，中文全成豆腐块，得报错而不是默默画
        from monita_linux.render.fonts import fc_match
        latin = fc_match("Liberation Sans")
        if latin is None:
            self.skipTest("没有 fc-match")
        with self.assertRaises(FontError) as cm:
            find_fonts(match=lambda q: latin)
        self.assertIn("CJK", str(cm.exception))


class Rgb565(unittest.TestCase):
    def test_matches_reference(self):
        from PIL import Image
        raw = bytes((i * 37 + 11) & 0xFF for i in range(64 * 3 * 3))
        img = Image.frombytes("RGB", (64, 3), raw)
        self.assertEqual(CV.to_rgb565(img), ml.rgb888_to_rgb565(raw))


class PanelCells(unittest.TestCase):
    def test_dash_when_missing(self):
        self.assertEqual([c[0] for c in panel_cells({})], ["–"] * 4)
        self.assertEqual([c[0] for c in panel_cells(dict(cpu=5, bat=80, player="mpv", load=1.5))], ["5%", "80%", "mpv", "1.50"])


class Pages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            find_fonts()
        except FontError as e:
            raise unittest.SkipTest("缺字体，跳过渲染测试：%s" % e)
        from monita_linux.render import Fonts, Renderer
        cls.r = Renderer(Fonts())

    def draw(self, page, data, **kw):
        inp = dict(data=data, now=NOW, local=LOCAL, panel=dict(cpu=12, bat=87, player="spotify", load=0.42))
        inp.update(kw)
        img, fb = self.r.render(page, inp)
        self.assertEqual(img.size, (800, 1280))
        self.assertEqual(len(fb), 800 * 1280 * 2)
        return img

    def test_playing_key_pixels(self):
        img = self.draw("lyrics", sample("playing"))
        self.assertEqual(img.getpixel((2, 2)), P.DESK)
        self.assertEqual(img.getpixel((100, 30)), P.TITLE)              # 标题栏
        self.assertEqual(img.getpixel((66, 600)), P.SELECT)             # 当前句高亮块
        self.assertEqual(img.getpixel((180, 941)), P.FILL)              # 进度条填充（33 / 214）
        self.assertEqual(img.getpixel((600, 941)), P.TROUGH)            # 没填到的槽
        self.assertEqual(img.getpixel((305, 333)), P.PLAY_GREEN)        # 状态灯

    def test_paused_block_is_trough(self):
        img = self.draw("lyrics", sample("paused"))
        self.assertEqual(img.getpixel((66, 600)), P.TROUGH)

    def test_messages_instead_of_lines(self):
        for data in (sample("pure"), sample("playing", lyric=dict(status="loading", lines=[])),
                     sample("playing", lyric=dict(status="nomatch", lines=[])), None):
            img = self.draw("lyrics", data)
            self.assertNotEqual(img.getpixel((66, 600)), P.SELECT)
            self.assertEqual(img.getpixel((378, 535)), P.WHITE)        # 提示页的圆形图标
        # 无歌词时标题 / 进度条照常（R5）
        img = self.draw("lyrics", sample("playing", lyric=dict(status="nomatch", lines=[])))
        self.assertEqual(img.getpixel((180, 941)), P.FILL)
        self.assertEqual(self.draw("lyrics", None).getpixel((180, 941)), P.TROUGH)

    def test_estimated_badge(self):
        img = self.draw("lyrics", sample("playing", estimated=True))
        self.assertEqual(img.getpixel((455, 340)), P.WARN)

    def test_cover_drawn(self):
        from PIL import Image
        cover = Image.new("RGB", (300, 300), (1, 2, 3))
        img = self.draw("lyrics", sample("playing"), cover=cover)
        self.assertEqual(img.getpixel((150, 240)), (1, 2, 3))

    def test_clock_page(self):
        img = self.draw("clock", None)
        self.assertEqual(img.getpixel((70, 240)), P.WELL)
        self.assertEqual(img.getpixel((2, 2)), P.DESK)
        self.assertNotEqual(self.draw("clock", None, local=time.struct_time((2026, 11, 26, 21, 8, 0, 3, 330, 0))).tobytes(),
                            img.tobytes())


class Cli(unittest.TestCase):
    def test_png_offline(self):
        try:
            find_fonts()
        except FontError as e:
            raise unittest.SkipTest(str(e))
        import tempfile
        from PIL import Image
        from monita_linux.__main__ import main
        out = os.path.join(tempfile.mkdtemp(), "x.png")
        sample_path = os.path.join(REPO, "mac", "Samples", "lyrics-playing.json")
        self.assertEqual(main(["--png", out, "--sample", sample_path, "--at", "40"]), 0)
        self.assertEqual(Image.open(out).size, (800, 1280))
        self.assertEqual(main(["--png", out, "--clock"]), 0)

    def test_defaults(self):
        from monita_linux.__main__ import parse_args
        a = parse_args([])
        self.assertEqual((a.device, a.page, a.tick), ("usb", "lyrics", 0.2))


if __name__ == "__main__":
    unittest.main()
