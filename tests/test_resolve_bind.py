"""_resolve 帐本绑定路（2026-11-17 深夜）：walker 精确锚绑定放行的无
门牌文字片段带着 entry_author+entry_body 来；屏上无门牌帖的行与
entry_body 精确重叠的就是它。没有这条路，_actionable 放行了 act 也解析
不到帖——绑定的处置会全数 stale-screen 硬失败，烧光 act_fails 杀死整轮。

全局唯一性裁决（证据只指向一条 entry）在 walker 侧 _bind_headless 已做，
这条路只定位屏上位置；有门牌的帖不走这条路（门牌路优先级更高）。
"""
import unittest

from scripts.feed_reader import Actor


def frag(body, top_y=0.3, author=None, doorplate=False):
    return {"author": author,
            "author_source": "photo" if author else None,
            "doorplate_visible": doorplate or author is not None,
            "sponsored_arrow": False,
            "time_row": None,
            "body": {"top_y": top_y, "bottom_y": top_y + 0.3,
                     "texts": list(body)},
            "button_x": 0.913, "button_y": top_y + 0.35}


HU = "优胜美地的日出把整面花岗岩染成金红色太震撼了"


class TestResolveBind(unittest.TestCase):

    def setUp(self):
        self.actor = Actor(schema_box=None, out_dir="/tmp")

    def test_entry_body_road_finds_headless_fragment(self):
        scan = {"posts": [frag((HU,))]}
        post, k, _r = self.actor._resolve(
            scan, "胡吟", entry_body=[HU])
        self.assertIsNotNone(post)
        self.assertIsNone(k)          # 非 pending 送达路

    def test_no_entry_body_no_bind(self):
        # 旧路回归：不带 entry_body 的解析不认无门牌坨（不会误扩张）
        scan = {"posts": [frag((HU,))]}
        post, _k, _r = self.actor._resolve(scan, "胡吟")
        self.assertIsNone(post)

    def test_textless_fragment_not_taken(self):
        # 纯照片尾无行可重叠：绑定路不接，归顶沿路/armed 路
        scan = {"posts": [frag(())]}
        post, _k, _r = self.actor._resolve(scan, "胡吟", entry_body=[HU])
        self.assertIsNone(post)

    def test_doorplated_post_not_taken_by_bind_road(self):
        # 绑定路只找无门牌的帖：author_hint 缺席（门牌路没跑）时，屏上
        # 门牌帖也不许被这条按行重叠的路接走——那条路认作者，这条认行，
        # 行重叠跨作者是引诱（转帖/引用行的正文行会撞车）
        scan = {"posts": [frag(("完全无关的一行正文内容足够长锚",), author="胡吟")]}
        post, _k, _r = self.actor._resolve(scan, None, entry_body=[HU])
        self.assertIsNone(post)

    def test_pending_road_still_wins_over_bind_road(self):
        # armed 尾屏送达路优先：pending 行重叠先于 entry_body 判定
        # （送达判定的证据链更硬：全文行重叠 + 存好的回复）
        self.actor.pending["胡吟"] = {
            "author": "胡吟", "full_body": [HU], "reply": "存好的回复"}
        scan = {"posts": [frag((HU,))]}
        post, k, _r = self.actor._resolve(scan, "胡吟", entry_body=[HU])
        self.assertIsNotNone(post)
        self.assertEqual(k, "胡吟")


if __name__ == "__main__":
    unittest.main()
