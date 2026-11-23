"""锚定点击（2026-11-17 用户手工实验定案）：⟳ 换血后首按会被"上次看到
这里"分割线拽回旧读位（r-154847：首按从封面飞约 40 屏，精确落上一轮
最深点曹校长边界）。解除 = 刷新后在封面照片下方空白带点一下；x 位置
是嫌疑变量（左缘 0.12 点击未解除），落点取内容区中部。

落点纯函数 find_cover_gap_blank 的门（每扇一测）：
1. schema 首帖 top_y 定带下沿，无帖 = None；
2. 白带 ≥15px 且带顶非白硬边——短带拒绝；400px 白顶到天（无封面
   硬边，白虚空）拒绝；
3. 落点 patch 自洽全白（点击点在图上必须真是空白）。
"""
import unittest
from PIL import Image


def _frame(h=927, w=1021, cover_bottom=0.68, post_top=0.75):
    """合成首屏：白底 + 封面照片（暗块 0.05→cover_bottom，x 0.12→1.0）
    + 首帖文字行（暗行 0.76→0.97，x 0.27→0.90）。"""
    img = Image.new("RGB", (w, h), (255, 255, 255))
    px = img.load()
    for y in range(int(0.05 * h), int(cover_bottom * h)):
        for x in range(int(0.12 * w), w):
            px[x, y] = (40, 40, 40)
    for y in range(int(0.76 * h), int(0.97 * h)):
        for x in range(int(0.27 * w), int(0.90 * w)):
            px[x, y] = (30, 30, 30)
    return img


class TestFindCoverGapBlank(unittest.TestCase):
    def test_hits_mid_content_below_cover_above_first_post(self):
        import scripts.feed_reader as fr
        img = _frame()
        hit = fr.find_cover_gap_blank(img, [{"top_y": 0.75}])
        self.assertIsNotNone(hit)
        fx, fy = hit
        self.assertTrue(0.40 <= fx <= 0.60, f"x={fx} 不在内容区中部")
        self.assertTrue(0.68 < fy < 0.75, f"y={fy} 不在封面-首帖空白带")
        px = img.load()
        cx, cy = int(fx * img.size[0]), int(fy * img.size[1])
        self.assertTrue(all(min(px[x, y]) >= 245
                            for x in range(cx - 8, cx + 9)
                            for y in range(cy - 8, cy + 9)),
                        "落点 patch 不是全白（自洽失败）")

    def test_short_gap_rejected(self):
        # 封面底缘压到首帖顶上方 4px——白带 <15px，拒绝
        import scripts.feed_reader as fr
        img = _frame(cover_bottom=0.7445)
        self.assertIsNone(fr.find_cover_gap_blank(img, [{"top_y": 0.75}]))

    def test_no_posts_no_target(self):
        import scripts.feed_reader as fr
        img = _frame()
        self.assertIsNone(fr.find_cover_gap_blank(img, []))
        self.assertIsNone(fr.find_cover_gap_blank(img, [{"author": "a"}]))

    def test_white_void_without_cover_edge_rejected(self):
        # 无封面：帖顶上方 400px+ 全白且带顶无硬边（白虚空）→ 拒绝
        import scripts.feed_reader as fr
        img = _frame(cover_bottom=0.06)
        self.assertIsNone(fr.find_cover_gap_blank(img, [{"top_y": 0.75}]))


if __name__ == "__main__":
    unittest.main()
