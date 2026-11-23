"""↑ 连打回顶（2026-11-16 深夜用户批准）：换血回位把视口拖回"上次
读到处"后，esc→重入只恢复读位（23:19 实证两连重入精准落回同一行
'11月16日 15:18'）——恢复梯子需要唯一的向上手段。本文件钉三件事：
① 已在顶时零按键；② 每轮 ↑ 后用封面名牌位置门验证，到顶后隔
gap 复验、两测都在顶才认（防 ↑ 意外触发换血把顶又拖走）；
③ feed_reader 的刷新后恢复梯子组合顺序 = 先重入、失败才 ↑。"""
import unittest
from unittest import mock


def make_checker(sequence):
    calls = []

    def check(win, tag):
        calls.append(tag)
        ok = sequence[min(len(calls) - 1, len(sequence) - 1)]
        return ok, "moments", "why-%d" % len(calls)

    return check, calls


class TestRecoverTopByArrowUp(unittest.TestCase):

    def _run(self, sequence, **kw):
        import scripts.post_test as pt
        check, calls = make_checker(sequence)
        presses = []
        sleeps = []
        logs = []
        ok = pt.recover_top_by_arrow_up(
            None, "t", check_fn=check,
            press_fn=lambda: presses.append(1),
            sleep_fn=lambda s: sleeps.append(s),
            log=logs.append, **kw)
        return ok, presses, calls, logs

    def test_already_top_zero_presses(self):
        ok, presses, calls, logs = self._run([True])
        self.assertTrue(ok)
        self.assertEqual(presses, [])
        self.assertEqual(len(calls), 1)

    def test_reaches_top_after_bursts(self):
        # 初始不在顶；两轮 ↑ 后到顶且复验仍在顶
        seq = [False, False, True, True]
        ok, presses, calls, logs = self._run(seq, per_round=5)
        self.assertTrue(ok)
        self.assertEqual(len(presses), 10)  # 2 轮 x5
        self.assertEqual(len(calls), 4)     # 初测+2轮测+复验

    def test_double_verify_rejects_single_top(self):
        # 第2轮测到顶但复验丢顶（↑ 触发换血把顶拖走）——必须继续按，
        # 直到某轮"测到顶+复验仍在顶"为止
        seq = [False, False, True, False, True, True]
        ok, presses, calls, _ = self._run(seq, per_round=5)
        self.assertTrue(ok)
        self.assertEqual(len(presses), 15)  # 3 轮 x5
        self.assertEqual(len(calls), 6)

    def test_never_top_returns_false(self):
        ok, presses, _, logs = self._run([False], max_rounds=3,
                                         per_round=4)
        self.assertFalse(ok)
        self.assertEqual(len(presses), 12)
        self.assertTrue(any("放弃" in l for l in logs))


class TestRecoverPostRefreshWiring(unittest.TestCase):

    def _fake_pt(self, view_ok, arrow_ok):
        calls = {"view": 0, "arrow": 0}

        def recover_view(win, tag, require_top=False):
            calls["view"] += 1
            return view_ok

        def recover_up(win, tag, log=None):
            calls["arrow"] += 1
            self.assertEqual(tag, "post_refresh_up")
            return arrow_ok

        return mock.Mock(recover_view=recover_view,
                         recover_top_by_arrow_up=recover_up), calls

    def test_reopen_success_skips_arrow(self):
        import scripts.feed_reader as fr
        fake, calls = self._fake_pt(view_ok=True, arrow_ok=True)
        ok = fr.recover_post_refresh(None, lambda s: None, pt_mod=fake)
        self.assertTrue(ok)
        self.assertEqual(calls["view"], 1)
        self.assertEqual(calls["arrow"], 0)

    def test_reopen_failure_falls_to_arrow(self):
        import scripts.feed_reader as fr
        fake, calls = self._fake_pt(view_ok=False, arrow_ok=True)
        ok = fr.recover_post_refresh(None, lambda s: None, pt_mod=fake)
        self.assertTrue(ok)
        self.assertEqual(calls["view"], 1)
        self.assertEqual(calls["arrow"], 1)

    def test_both_fail_is_false(self):
        import scripts.feed_reader as fr
        fake, calls = self._fake_pt(view_ok=False, arrow_ok=False)
        ok = fr.recover_post_refresh(None, lambda s: None, pt_mod=fake)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
