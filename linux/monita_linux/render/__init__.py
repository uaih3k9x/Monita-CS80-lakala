"""渲染：Pillow 画 800×1280，输出 RGB565 小端（Renderer.render）。"""
from .canvas import Fonts, to_rgb565  # noqa: F401
from .clock_page import draw_clock
from .lyrics_page import LyricsPage


class Renderer:
    def __init__(self, fonts):
        self.page = LyricsPage(fonts)

    @property
    def progress_w(self):
        return self.page.progress_inner_w

    def image(self, page, inp):
        return draw_clock(self.page, inp) if page == "clock" else self.page.draw(inp)

    def render(self, page, inp):
        """-> (PIL 图, RGB565 字节)"""
        img = self.image(page, inp)
        return img, to_rgb565(img)
