"""feed_reader Actor 的前窗守卫（2026-11-15 Anne 事故）：弹层链路中途
终端升到最前，Cmd+V 把回复贴进了终端。守卫=粘贴前最前窗必须是微信，
否则放弃（诚实 paste-failed，发送核验链照旧兜底）。"""
import unittest
from unittest import mock


class TestFrontmostGuard(unittest.TestCase):
    def _actor(self):
        import scripts.feed_reader as fr
        return fr, fr.Actor({"schema": None}, "/tmp")

    def test_paste_aborts_when_wechat_not_frontmost(self):
        fr, actor = self._actor()
        with mock.patch.object(fr.Actor, "_front_is_wechat",
                               return_value=False), \
             mock.patch("subprocess.run") as run:
            self.assertFalse(actor._paste("回复文本"))
        run.assert_not_called()          # 剪贴板都没碰

    def test_paste_proceeds_when_wechat_frontmost(self):
        fr, actor = self._actor()
        fake_q = mock.Mock()             # Quartz 全假——绝不真发 ⌘V
        with mock.patch.object(fr.Actor, "_front_is_wechat",
                               return_value=True), \
             mock.patch("subprocess.run") as run, \
             mock.patch.dict("sys.modules", {"Quartz": fake_q}), \
             mock.patch.object(fr, "t", mock.Mock()):
            run.return_value.returncode = 0
            ok = actor._paste("回复文本")
            self.assertTrue(ok)          # pbcopy + ⌘V 照常
            self.assertGreaterEqual(run.call_count, 1)
            fake_q.CGEventPost.assert_called()

    def test_frontmost_owner_names(self):
        # 中英文系统名都认（kCGWindowOwnerName: WeChat / 微信）
        fr, actor = self._actor()
        import sys as _sys
        cases = [("WeChat", True), ("微信", True), ("Terminal", False),
                 ("zsh", False)]
        for owner, want in cases:
            fake = mock.Mock()
            fake.get = lambda k, d=99, _o=owner: (
                0 if k == "kCGWindowLayer" else _o)
            with mock.patch.dict("sys.modules", {"Quartz": _fake_quartz([fake])}):
                self.assertEqual(actor._front_is_wechat(), want, owner)


def _fake_quartz(windows):
    import types
    q = types.SimpleNamespace()
    q.kCGWindowListOptionOnScreenOnly = 1
    q.kCGWindowListExcludeDesktopElements = 2
    q.kCGNullWindowID = None
    q.CGWindowListCopyWindowInfo = lambda opts, nid: windows
    return q


if __name__ == "__main__":
    unittest.main()
