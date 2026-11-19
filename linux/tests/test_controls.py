"""按键 → 动作、灯语（照 MonitaCore/Controls.swift）"""
import unittest

from util import FakeClock  # noqa: F401
import monita_link as ml
from monita_linux.controls import KeyMapper, led_for, QUERY, MENU, PLUS, MINUS


def ev(key, e):
    return dict(key=key, ev=e, n=0, uptime=0)


class KeyMapperTests(unittest.TestCase):
    def test_plus_minus_on_down(self):
        m = KeyMapper()
        self.assertEqual(m.feed(ev(PLUS, ml.KEY_DOWN)), "vol+")
        self.assertIsNone(m.feed(ev(PLUS, ml.KEY_UP)))
        self.assertEqual(m.feed(ev(MINUS, ml.KEY_DOWN)), "vol-")

    def test_menu_query_short_press_on_up(self):
        m = KeyMapper()
        self.assertIsNone(m.feed(ev(MENU, ml.KEY_DOWN)))
        self.assertEqual(m.feed(ev(MENU, ml.KEY_UP)), "page")
        m.feed(ev(QUERY, ml.KEY_DOWN))
        self.assertEqual(m.feed(ev(QUERY, ml.KEY_UP)), "play_pause")

    def test_long_press_cancels(self):
        m = KeyMapper()
        m.feed(ev(MENU, ml.KEY_DOWN))
        m.feed(ev(MENU, ml.KEY_LONG))
        self.assertIsNone(m.feed(ev(MENU, ml.KEY_UP)))
        m.feed(ev(QUERY, ml.KEY_DOWN))
        m.feed(ev(QUERY, ml.KEY_LONG))
        self.assertIsNone(m.feed(ev(QUERY, ml.KEY_UP)))       # PWRKEY 长按后固件补的 UP 不算短按

    def test_up_without_down_ignored(self):
        self.assertIsNone(KeyMapper().feed(ev(MENU, ml.KEY_UP)))

    def test_unknown_key(self):
        self.assertIsNone(KeyMapper().feed(ev(9, ml.KEY_DOWN)))


class LedTests(unittest.TestCase):
    def test_led_for(self):
        self.assertEqual(led_for(True), (ml.LED_G, ml.LED_ON, 0, 0))
        self.assertEqual(led_for(False), (0, ml.LED_OFF, 0, 0))


if __name__ == "__main__":
    unittest.main()
