#!/usr/bin/env python3
"""lyrics.py 纯函数测试：python3 -m unittest discover -s tools/lyrics/tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import lyrics as L  # noqa: E402


class MraDir(unittest.TestCase):
    def test_probe_order(self):
        h = "/Users/you"
        self.assertEqual(L.mra_candidates({}, h), ["/Users/you/.monita-cs80/mediaremote-adapter",
                                                   "/Users/you/.cs80_panel/mediaremote-adapter"])
        self.assertEqual(L.mra_candidates({"MONITA_HOME": "/opt/m"}, h)[0], "/opt/m/mediaremote-adapter")
        self.assertEqual(L.mra_candidates({"MONITA_MRA_DIR": "/a", "MONITA_HOME": "/b"}, h), ["/a"])
        self.assertEqual(len(L.mra_candidates({"MONITA_HOME": "/Users/you/.monita-cs80"}, h)), 2)
        c = L.mra_candidates({}, h)
        self.assertEqual(L.pick_mra_dir(c, lambda d: "cs80_panel" in d), c[1])
        self.assertEqual(L.pick_mra_dir(c, lambda d: False), c[0])


class ParseLrc(unittest.TestCase):
    def test_basic_and_sorted(self):
        text = "[00:12.57]第二句\n[00:01.000]第一句\n"
        self.assertEqual(L.parse_lrc(text), [(1.0, "第一句"), (12.57, "第二句")])

    def test_two_digit_and_three_digit_fraction(self):
        self.assertEqual(L.parse_lrc("[01:02.5]a\n[01:02.50]b\n[01:02.500]c")[0][0], 62.5)
        self.assertAlmostEqual(L.parse_lrc("[00:00.05]x")[0][0], 0.05)
        self.assertAlmostEqual(L.parse_lrc("[00:00.005]x")[0][0], 0.005)

    def test_no_fraction_and_colon_fraction(self):
        self.assertEqual(L.parse_lrc("[00:03]a")[0][0], 3.0)
        self.assertEqual(L.parse_lrc("[00:03:50]a")[0][0], 3.5)

    def test_multi_timestamps_per_line(self):
        self.assertEqual(L.parse_lrc("[00:01.00][00:05.00]副歌"), [(1.0, "副歌"), (5.0, "副歌")])

    def test_metadata_ignored_offset_applied(self):
        text = "[ar:歌手]\n[ti:歌名]\n[offset:500]\n[00:01.00]a"
        # offset 为正表示歌词提前（时间减小）
        self.assertEqual(L.parse_lrc(text), [(0.5, "a")])

    def test_empty_text_kept_as_gap(self):
        self.assertEqual(L.parse_lrc("[00:04.05]\n[00:12.00]b"), [(4.05, ""), (12.0, "b")])

    def test_json_credit_lines(self):
        text = '{"t":0,"c":[{"tx":"作词: "},{"tx":"张三"}]}\n[00:02.00]a'
        self.assertEqual(L.parse_lrc(text), [(0.0, "作词: 张三"), (2.0, "a")])

    def test_garbage_and_none(self):
        self.assertEqual(L.parse_lrc(None), [])
        self.assertEqual(L.parse_lrc("hello\n[xx:yy]z"), [])


class MergeTranslation(unittest.TestCase):
    def test_merge_by_time(self):
        lines = [(1.0, "hello"), (5.0, "world"), (9.0, "no tr")]
        tr = [(1.0, "你好"), (5.004, "世界")]
        self.assertEqual(L.merge_translation(lines, tr), [
            {"t": 1.0, "x": "hello", "tr": "你好"},
            {"t": 5.0, "x": "world", "tr": "世界"},
            {"t": 9.0, "x": "no tr", "tr": ""},
        ])

    def test_translation_slash_placeholder_dropped(self):
        self.assertEqual(L.merge_translation([(1.0, "a")], [(1.0, "//")])[0]["tr"], "")


class CurrentIndex(unittest.TestCase):
    LINES = [{"t": 1.0}, {"t": 5.0}, {"t": 9.0}]

    def test_before_first(self):
        self.assertEqual(L.current_index(self.LINES, 0.5), -1)

    def test_exact_and_between(self):
        self.assertEqual(L.current_index(self.LINES, 1.0), 0)
        self.assertEqual(L.current_index(self.LINES, 6.0), 1)
        self.assertEqual(L.current_index(self.LINES, 99), 2)

    def test_empty(self):
        self.assertEqual(L.current_index([], 3), -1)


class Interpolate(unittest.TestCase):
    def test_playing(self):
        self.assertAlmostEqual(L.position(10.0, 1000.0, 1.0, 1003.5), 13.5)

    def test_paused_rate_zero(self):
        self.assertAlmostEqual(L.position(10.0, 1000.0, 0.0, 1100.0), 10.0)

    def test_clamped_to_duration_and_zero(self):
        self.assertEqual(L.position(200.0, 0.0, 1.0, 100.0, duration=207.0), 207.0)
        self.assertEqual(L.position(0.0, 10.0, 1.0, 5.0), 0.0)

    def test_missing_elapsed(self):
        self.assertEqual(L.position(None, 0, 1, 5), 0.0)


class Normalize(unittest.TestCase):
    def test_case_space_punct_fullwidth(self):
        self.assertEqual(L.norm(" Re...Japanesque "), L.norm("re japanesque"))
        self.assertEqual(L.norm("ＡＢＣ（Live）"), L.norm("abc"))
        self.assertEqual(L.norm("情非得已 (童声版)"), L.norm("情非得已"))


class PickSong(unittest.TestCase):
    RESULTS = [
        {"id": 2645687924, "name": "Re...Japanesque（钢琴）", "artists": [{"name": "薛湘灵"}], "duration": 321557},
        {"id": 1913115359, "name": "Re...Japanesque", "artists": [{"name": "DAISHI DANCE"}], "duration": 441000},
        {"id": 26092806, "name": "Take Me Hand", "artists": [{"name": "DAISHI DANCE"}], "duration": 259973},
    ]

    def test_exact_title_artist(self):
        self.assertEqual(L.pick_song(self.RESULTS, "Re...Japanesque", "DAISHI DANCE", 441.06), 1913115359)

    def test_duration_breaks_tie(self):
        res = [
            {"id": 1, "name": "Shine", "artists": [{"name": "Janji"}], "duration": 180000},
            {"id": 2, "name": "Shine", "artists": [{"name": "Janji"}], "duration": 207360},
        ]
        self.assertEqual(L.pick_song(res, "Shine", "Janji", 207.36), 2)

    def test_hint_ids_win_among_plausible(self):
        res = [
            {"id": 1, "name": "Shine", "artists": [{"name": "Janji"}], "duration": 207000},
            {"id": 2, "name": "Shine", "artists": [{"name": "Janji"}], "duration": 207000},
        ]
        self.assertEqual(L.pick_song(res, "Shine", "Janji", 207, hint_ids={2}), 2)

    def test_multi_artist_string(self):
        res = [{"id": 7, "name": "Take Me Hand", "artists": [{"name": "DAISHI DANCE"}, {"name": "Cécile Corbel"}],
                "duration": 259480}]
        self.assertEqual(L.pick_song(res, "Take Me Hand", "DAISHI DANCE/Cécile Corbel", 259.4), 7)

    def test_no_plausible_match(self):
        self.assertIsNone(L.pick_song(self.RESULTS, "完全不相关", "某人", 100))
        self.assertIsNone(L.pick_song([], "a", "b", 1))


class LyricPayload(unittest.TestCase):
    def test_pure_music_flag(self):
        self.assertEqual(L.lyric_payload({"pureMusic": True, "lrc": {"lyric": ""}})["status"], "pure")

    def test_pure_music_text(self):
        raw = {"lrc": {"lyric": "[00:00.00]纯音乐，请欣赏\n"}}
        self.assertEqual(L.lyric_payload(raw)["status"], "pure")

    def test_nolyric_and_empty(self):
        self.assertEqual(L.lyric_payload({"nolyric": True})["status"], "none")
        self.assertEqual(L.lyric_payload({"lrc": {"lyric": ""}})["status"], "none")
        self.assertEqual(L.lyric_payload({"uncollected": True})["status"], "none")

    def test_ok_with_translation(self):
        raw = {"lrc": {"lyric": "[00:01.00]hello\n[00:03.00]bye"}, "tlyric": {"lyric": "[00:01.00]你好"}}
        p = L.lyric_payload(raw)
        self.assertEqual(p["status"], "ok")
        self.assertTrue(p["translated"])
        self.assertEqual(p["lines"][0], {"t": 1.0, "x": "hello", "tr": "你好"})

    def test_unsynced_lyric_has_no_timestamps(self):
        # 只有文本没有时间戳：当作找不到同步歌词
        self.assertEqual(L.lyric_payload({"lrc": {"lyric": "just text\nmore"}})["status"], "none")


class NowPlayingMerge(unittest.TestCase):
    def test_apply_full_and_diff(self):
        st = L.apply_stream_event({}, {"type": "data", "diff": False, "payload": {"title": "A", "playing": True}})
        st = L.apply_stream_event(st, {"type": "data", "diff": True, "payload": {"playing": False}})
        self.assertEqual(st, {"title": "A", "playing": False})

    def test_diff_null_removes_key(self):
        st = L.apply_stream_event({"title": "A", "album": "x"}, {"type": "data", "diff": True,
                                                              "payload": {"album": None}})
        self.assertEqual(st, {"title": "A"})

    def test_empty_full_payload_clears(self):
        self.assertEqual(L.apply_stream_event({"title": "A"}, {"type": "data", "diff": False, "payload": {}}), {})

    def test_non_data_ignored(self):
        self.assertEqual(L.apply_stream_event({"a": 1}, {"type": "other"}), {"a": 1})

    def test_snapshot_from_micros(self):
        raw = {"title": "Shine", "artist": "Janji", "album": "Shine", "durationMicros": 207360000,
               "elapsedTimeMicros": 12500000, "timestampEpochMicros": 1794973070415779,
               "playbackRate": 1, "playing": True, "bundleIdentifier": "com.netease.163music"}
        s = L.snapshot(raw)
        self.assertEqual(s["title"], "Shine")
        self.assertAlmostEqual(s["duration"], 207.36)
        self.assertAlmostEqual(s["pos"], 12.5)
        self.assertEqual(s["at"], 1794973070415)
        self.assertEqual(s["rate"], 1)
        self.assertEqual(s["state"], "playing")
        self.assertEqual(s["app"], "com.netease.163music")

    def test_snapshot_paused_forces_rate_zero(self):
        s = L.snapshot({"title": "x", "playing": False, "playbackRate": 1, "elapsedTimeMicros": 0,
                        "timestampEpochMicros": 0})
        self.assertEqual(s["rate"], 0)
        self.assertEqual(s["state"], "paused")

    def test_snapshot_loading_flicker_not_paused(self):
        # 网易云切歌缓冲时 playing 会来回跳 ~1 秒：进度 0 且刚发生 → 算播放中但不走（rate 0）
        raw = {"title": "x", "playing": False, "playbackRate": 0, "elapsedTimeMicros": 0,
               "timestampEpochMicros": 1000_000_000}
        s = L.snapshot(raw, now=1001.5)
        self.assertEqual((s["state"], s["rate"]), ("playing", 0))
        self.assertEqual(L.snapshot(raw, now=1010)["state"], "paused")

    def test_snapshot_empty(self):
        self.assertEqual(L.snapshot({})["state"], "stopped")


class EstimateClock(unittest.TestCase):
    def test_estimate_starts_on_track_change(self):
        clk = L.EstimateClock()
        a = clk.update("k1", now=100.0)
        self.assertEqual((a["pos"], a["at"]), (0.0, 100000))
        b = clk.update("k1", now=130.0)
        self.assertEqual((b["pos"], b["at"]), (0.0, 100000))  # 同一首：起点不变，前端外推
        c = clk.update("k2", now=140.0)
        self.assertEqual(c["at"], 140000)



class Lazy(unittest.TestCase):
    def test_import_does_not_start_backend(self):
        # 只 import 不该起 adapter 子进程/后台线程
        self.assertIsNone(L._LYRICS)

if __name__ == "__main__":
    unittest.main()
