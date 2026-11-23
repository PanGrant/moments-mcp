"""起步安定复检（2026-11-16 深夜真机竞态）：⟳ 换血可能晚于固定 2.5s
睡眠完成——当晚 pre.png 拍到封面顶、起步后一次 ↓ 被换血中的 feed 拽到
边界帖（瞬移 30 帖、2 落地即停）。修法=起步前连续两测指纹一致才放行；
始终不安定=诚实 FATAL，一步不走。指纹=schema 单槽（帖数 +
page_bottom_closer），空读（0 帖白帧）永不算安定。"""
import unittest


def _scan(n_posts, closer_text="Today", y=0.5):
    return {"posts": [{"author": f"a{i}"} for i in range(n_posts)],
            "page_bottom_closer": {"text": closer_text, "y": y}}


class TestSettleFingerprint(unittest.TestCase):
    def test_same_schema_slot_is_same_fingerprint(self):
        import scripts.feed_reader as fr
        self.assertEqual(fr._settle_fingerprint(_scan(2)),
                         fr._settle_fingerprint(_scan(2)))

    def test_closer_change_changes_fingerprint(self):
        import scripts.feed_reader as fr
        self.assertNotEqual(fr._settle_fingerprint(_scan(2, "Today")),
                            fr._settle_fingerprint(_scan(2, "Yesterday")))

    def test_empty_read_is_never_a_settled_fingerprint(self):
        import scripts.feed_reader as fr
        self.assertIsNone(fr._settle_fingerprint({"posts": []}))
        self.assertIsNone(fr._settle_fingerprint(None))


class TestFeedSettled(unittest.TestCase):
    def _run(self, fps, max_rounds=4):
        import scripts.feed_reader as fr
        sleeps = []
        out = fr.feed_settled(lambda k: fps[k],
                              lambda s: sleeps.append(s),
                              lambda m: None, max_rounds=max_rounds)
        return out, len(sleeps)

    def test_two_consistent_reads_settle(self):
        out, n_sleeps = self._run([_scan(1), _scan(1)])
        self.assertTrue(out)
        self.assertEqual(n_sleeps, 1)

    def test_churn_then_stable_settles(self):
        out, _ = self._run([_scan(1), _scan(3), _scan(3)])
        self.assertTrue(out)

    def test_never_stable_aborts(self):
        out, n_sleeps = self._run([_scan(1), _scan(2), _scan(3), _scan(4)])
        self.assertFalse(out)
        self.assertEqual(n_sleeps, 3)

    def test_empty_reads_never_settle(self):
        out, _ = self._run([{"posts": []}, {"posts": []}], max_rounds=2)
        self.assertFalse(out)


if __name__ == "__main__":
    unittest.main()
