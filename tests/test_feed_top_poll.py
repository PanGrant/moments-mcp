"""feed_top_check_poll（2026-11-22 WorkBuddy 实跑 FATAL×2 教训）：
重入朋友圈后封面照片/名牌惰性绘制，单发顶部检查在 2-4s 内假阴性
→ 误 FATAL。轮询 tries 次仍不达才下结论。"""
import unittest
from unittest import mock

from scripts import post_test as pt


class TestFeedTopCheckPoll(unittest.TestCase):
    def test_returns_top_as_soon_as_seen(self):
        seq = [(False, "moments", "尚未画好"),
               (True, "moments", "⟳ 簇 + 封面名牌 = 封面顶")]
        with mock.patch.object(pt, "feed_top_check", side_effect=seq), \
                mock.patch.object(pt.time, "sleep") as sleeper:
            at_top, _tv, tw = pt.feed_top_check_poll(None, "ck")
        self.assertTrue(at_top)
        self.assertIn("封面名牌", tw)
        self.assertEqual(sleeper.call_count, 1)   # 命中即停，不空等

    def test_gives_up_after_tries(self):
        miss = (False, "moments", "1 time rows, e.g. '56 minute（s） ago'")
        with mock.patch.object(pt, "feed_top_check",
                               side_effect=[miss] * 3), \
                mock.patch.object(pt.time, "sleep") as sleeper:
            at_top, tv, tw = pt.feed_top_check_poll(None, "ck")
        self.assertFalse(at_top)
        self.assertEqual(tv, "moments")
        self.assertEqual(sleeper.call_count, 2)   # 末次后不再睡


if __name__ == "__main__":
    unittest.main()
