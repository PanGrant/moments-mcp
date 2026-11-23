"""AVATAR_MAX_H 一票拒真头像回归（2026-11-16 深夜真机冤案）：

上限原为窗口高度的 0.08（注释称"Real avatars ≈0.03"），但头像是固定
~39pt 渲染：548pt 高的窗口里 39/548=0.071 通过，464pt 窄窗口里
39/464=0.084 被整条拒掉——门牌消失与否取决于头像自身边缘像素让 run
到达 70px 还是 78px，"有时回有时不回"的硬币就是这枚。

铁证帧（真机 22:05 轮）：
- viva_share_frame.png：Viva 名牌（"Viva/按数字说话/VIVA 香港 1h"）
  清晰可见，scan 却 author=None——78px run > 74.2px cap 整条被拒，
  Viva 连同上文 DeepSeek 卡并成一段 author=None，按 no-doorplate
  安全跳过。修复后必须一帧两帖、Viva 有门牌。
- lilei_reply_frame.png：Wen Jiang 的 78px 头像同因被拒。
"""
import unittest


VIVA = "tests/fixtures/viva_share_frame.png"
LILEI = "tests/fixtures/lilei_reply_frame.png"


class TestAvatarCapAcceptsRealAvatars(unittest.TestCase):

    def test_full_height_avatar_run_is_a_block(self):
        # Viva 头像 run 实测 (0.677, 0.761) = 78px/928 = 0.084 ——
        # 旧 cap 0.08 恰好把它拒掉；新 cap 必须收它为头像块。
        from services.digest.feed_layout import avatar_blocks
        blocks = avatar_blocks(VIVA, 0.1216, 0.2567, 0.625, 0.996)
        self.assertEqual(len(blocks), 1)
        y0, y1 = blocks[0]
        self.assertAlmostEqual(y0, 0.677, delta=0.01)
        self.assertAlmostEqual(y1, 0.765, delta=0.01)

    def test_cover_still_rejected(self):
        # cap 上抬后封面必须仍然被拒：cover_top_true.png 的照片列里
        # 封面照片占 ~半屏，新 cap 0.20 不得把它收成头像块。
        from services.digest.feed_layout import avatar_blocks
        blocks = avatar_blocks("tests/fixtures/cover_top_true.png",
                               0.1216, 0.2567, 0.0, 1.0)
        for y0, y1 in blocks:
            self.assertLess(y1 - y0, 0.20)


class TestVivaFrameSegments(unittest.TestCase):

    def test_viva_gets_her_doorplate_back(self):
        from services.digest.post_schema import scan_screen
        s = scan_screen(VIVA)
        posts = s["posts"]
        self.assertEqual(len(posts), 2)  # 卡帖与 Viva 帖不再并段
        viva = [p for p in posts if (p.get("author") or "") == "Viva"]
        self.assertEqual(len(viva), 1)
        self.assertTrue(viva[0]["doorplate_visible"])
        closer = (viva[0].get("time_row") or {}).get("text") or ""
        self.assertIn("VIVA", closer)

    def test_card_post_is_own_headless_segment(self):
        # DeepSeek 卡帖门牌在帧外（头切）——但它的 closer（人民邮电报
        # 时间行）必须属于 headless 段自己，不再被并进 Viva 的帖。
        from services.digest.post_schema import scan_screen
        s = scan_screen(VIVA)
        headless = [p for p in s["posts"] if p.get("author") is None]
        self.assertEqual(len(headless), 1)
        closer = (headless[0].get("time_row") or {}).get("text") or ""
        self.assertIn("人民邮电报", closer)


class TestWenJiangFrame(unittest.TestCase):

    def test_wen_jiang_doorplate(self):
        from services.digest.post_schema import scan_screen
        s = scan_screen(LILEI)
        wj = [p for p in s["posts"] if (p.get("author") or "") == "Wen Jiang"]
        self.assertEqual(len(wj), 1)
        self.assertTrue(wj[0]["doorplate_visible"])


if __name__ == "__main__":
    unittest.main()
