"""Actor 身份携带（2026-11-16 深夜真机教训）：无门牌卡帖（视频/Channels
分享，author=None）走 is_top_tail 路解析成功后，resolved post 是
fresh-scan 的裸帖——不带 entry_author。entry_chain 的再解析若从 post
身上重新推导 hint，is_top_tail 路（hint 门控）断链 → 恒 stale-screen，
卡帖永远发不出（真机：费荔思/胡吟 连续两败 → act-failed 停轮）。
修法=解析一次、hint 随身（act 入口取，三处再解析全用它）。"""
import unittest
from unittest import mock


CARD_POST = {
    "author": None,              # 视频卡帖：门牌没量到
    "doorplate_visible": False,
    "top_y": 0.01,               # 顶在屏幕最上沿 → is_top_tail ✓
    "bottom_y": 0.95,
    "time_row": {"text": "10 hour（s） ago Channels• 费荔思"},
    "button_x": 0.9, "button_y": 0.55,
    "expand_y": None,
    "body": {"top_y": 0.1, "bottom_y": 0.4,
             "texts": ["视频卡标题行文字足够长可以当锚点"]},
}


class TestActIdentityCarry(unittest.TestCase):
    def _actor(self):
        import scripts.feed_reader as fr
        return fr, fr.Actor({"schema": None}, "/tmp")

    def test_top_tail_hint_carried_into_entry_chain(self):
        # walker 折叠 entry_author='费荔思' 的 target：act 必须把这个
        # 身份带进 entry_chain 的再解析（修复前 author_hint=None →
        # 三条路全断 → stale-screen，评论生成却发不出）。
        fr, actor = self._actor()
        win = mock.Mock(bounds={"x": 0, "y": 0, "width": 510, "height": 464})
        scan = {"posts": [dict(CARD_POST)], "viewport": {}}
        captured = {}
        with mock.patch.object(fr.Actor, "fresh_scan",
                               return_value=(scan, win, "moments")), \
             mock.patch.object(fr.Actor, "_generate",
                               return_value=("回复文本", None)), \
             mock.patch.object(fr.Actor, "entry_chain",
                               side_effect=lambda post, text,
                               author_hint=None:
                               captured.update(hint=author_hint)
                               or {"status": "ok"}):
            result = actor.act({"author": None, "entry_author": "费荔思",
                                "body": {"texts": []}})
        self.assertEqual(captured.get("hint"), "费荔思",
                         "entry_chain 再解析必须拿到 walker 的帐本身份")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["author"], "费荔思",
                         "审计行（卡·自动批准）的 author 不得再是 None")

    def test_authored_post_prefers_onscreen_author(self):
        # 门牌可见的普通帖：hint 用屏上 author（行为不变——XuElaine 路径）
        fr, actor = self._actor()
        post = dict(CARD_POST, author="XuElaine", doorplate_visible=True)
        win = mock.Mock(bounds={"x": 0, "y": 0, "width": 510, "height": 464})
        scan = {"posts": [post], "viewport": {}}
        captured = {}
        with mock.patch.object(fr.Actor, "fresh_scan",
                               return_value=(scan, win, "moments")), \
             mock.patch.object(fr.Actor, "_generate",
                               return_value=("回复文本", None)), \
             mock.patch.object(fr.Actor, "entry_chain",
                               side_effect=lambda post, text,
                               author_hint=None:
                               captured.update(hint=author_hint)
                               or {"status": "ok"}):
            actor.act({"author": None, "entry_author": "XuElain"})
        self.assertEqual(captured.get("hint"), "XuElaine",
                         "屏上门牌优先于帐本名（OCR 变体以屏为准）")


if __name__ == "__main__":
    unittest.main()
