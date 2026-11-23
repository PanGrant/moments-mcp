"""封面顶宣称必须有过位置门的封面名牌证据（2026-11-16 深夜真机竞态）：
⟳ 簇在标题栏常驻，"无时间行 + ⟳ 簇"两次被半路帧冒充——评论区的
'Grant Pan' 回复行假充封面署名，pre-check 假判在顶 → 跳过 reopen →
⟳ 换血后的延迟回位把起步视口拖回半路（一次 ↓ 瞬移 30 帖、两轮都
2 落地即停）。真机证据帧固化：真封面顶（cover_top_true.png，名牌
@(0.73,0.63)）必须判在顶；边界评论帧（boundary_comments_frame.png，
含 'Grant Pan Reply …' 长行）不得因名牌而判在顶。"""
import unittest
from unittest import mock


COVER_TOP = "tests/fixtures/cover_top_true.png"
BOUNDARY = "tests/fixtures/boundary_comments_frame.png"

# cover_name_evidence 按 own_names 名单认封面名牌，而名单是运行时设置
# （独立仓库默认空）。fixture 截图里烤的就是这个名字，测试级补丁显式
# 提供它——不依赖维护者的本机设置文件。
GRANT_PAN = "Grant Pan"


def _with_own_names(pt):
    return mock.patch.object(pt, "OWN_NAMES", {GRANT_PAN})


class TestCoverNameEvidence(unittest.TestCase):
    def test_true_cover_top_hits_name_in_cover_position(self):
        import scripts.post_test as pt
        with _with_own_names(pt):
            row = pt.cover_name_evidence(COVER_TOP)
        self.assertIsNotNone(row)
        self.assertEqual((row.get("text") or "").strip(), GRANT_PAN)

    def test_boundary_comment_rows_are_not_cover_evidence(self):
        import scripts.post_test as pt
        # 边界帧带 'Grant Pan Reply ZhangRobin：…' 长行（用户手回）——
        # 名字等价测试必须排除；该帧还有 Yesterday 时间行，双重排除。
        with _with_own_names(pt):
            self.assertIsNone(pt.cover_name_evidence(BOUNDARY))


class TestFeedTopCheck(unittest.TestCase):
    def _check(self, cap_path):
        import scripts.post_test as pt
        win = mock.Mock(bounds={"x": 0, "y": 0, "width": 510, "height": 464})
        with _with_own_names(pt), \
             mock.patch.object(pt, "win_capture", return_value=cap_path), \
             mock.patch.object(pt, "locate_refresh", return_value={"x_pt": 1}):
            return pt.feed_top_check(win, "t")

    def test_true_cover_top_is_top(self):
        at_top, verdict, why = self._check(COVER_TOP)
        self.assertTrue(at_top)
        self.assertEqual(verdict, "moments")
        self.assertIn("封面名牌", why)

    def test_boundary_frame_with_time_rows_is_not_top(self):
        at_top, verdict, why = self._check(BOUNDARY)
        self.assertFalse(at_top)
        # 时间行是半路铁证：该帧的 Yesterday 行先于名牌检查就否了顶部
        self.assertEqual(verdict, "moments")

    def test_tall_window_true_top_with_relative_time_row_is_top(self):
        # 2026-11-22 真机：窗口 510x577pt，真封面顶同屏可见首帖的
        # "58 minute（s） ago" 相对时间行——名牌证据须优先于时间行。
        import scripts.post_test as pt
        win = mock.Mock(bounds={"x": 0, "y": 0, "width": 510, "height": 577})
        with mock.patch.object(pt, "win_capture", return_value=COVER_TOP), \
             mock.patch.object(pt, "classify_capture",
                               return_value=("moments", "1 time rows")), \
             mock.patch.object(pt, "locate_refresh", return_value={"x_pt": 1}), \
             mock.patch.object(pt, "cover_name_evidence",
                               return_value={"text": "Grant Pan"}):
            at_top, verdict, why = pt.feed_top_check(win, "t")
        self.assertTrue(at_top)
        self.assertIn("封面名牌", why)

    def test_refresh_cluster_without_cover_name_rejects_top(self):
        # 竞态的精确形态：无时间行、⟳ 簇在，但名牌不在封面位 → 拒绝
        # 在顶、走恢复梯子（修复前：locate_refresh 一票通过 → 假判在顶）。
        import scripts.post_test as pt
        win = mock.Mock(bounds={"x": 0, "y": 0, "width": 510, "height": 464})
        with mock.patch.object(pt, "win_capture", return_value=COVER_TOP), \
             mock.patch.object(pt, "locate_refresh", return_value={"x_pt": 1}), \
             mock.patch.object(pt, "cover_name_evidence", return_value=None):
            at_top, verdict, why = pt.feed_top_check(win, "t")
        self.assertFalse(at_top)
        self.assertIn("冒充", why)


if __name__ == "__main__":
    unittest.main()
