"""发送送达探针只认文本专属证据（2026-11-17 James Guan 文庙假 ok 定案）：

旧探针 landed = any([头12字, 尾12字, 签名])——签名是所有我们的评论
共用的（「AI助手胡乱解读自动回复，内容请忽略」），屏上任何一条自己
的评论都能单独满足它。文庙帖发送时输入框占位符没读到、降级盲点继续
走，核验时旁边博物馆帖（真回过）的评论签名在屏上 → any() 成立 →
假 ok 封账 → 帖上其实无评论、永不重试。

修法：landed 只认头/尾12字（我们这条回复的专属文本）。签名不参与
送达判定——它只该用于别处（草稿匹配等）。
"""
import unittest


class TestSendLanded(unittest.TestCase):

    def test_signature_alone_never_confirms(self):
        # 文庙冤案形状：屏上只有签名（别的帖的评论尾部），没有本帖
        # 回复的头/尾文本 → 不得判送达
        from scripts.feed_reader import send_landed
        SIG = "ai助手胡乱解读自动回复内容请忽略"
        self.assertFalse(send_landed(SIG, "这文庙的氛围确实让人感"
                                        "觉穿越了下次有机会我也想去感受一下"))

    def test_head_twelve_confirms(self):
        from scripts.feed_reader import send_landed
        text = "这文庙的氛围确实让人感觉穿越了，下次有机会我也想去感受一下。"
        seen = " preprocessing 这文庙的氛围确实让人感觉穿越了 之类"
        self.assertTrue(send_landed(seen, text))

    def test_tail_twelve_confirms(self):
        from scripts.feed_reader import send_landed
        text = "这文庙的氛围确实让人感觉穿越了，下次有机会我也想去感受一下。"
        seen = "评论见下 有机会我也想去感受一下."
        self.assertTrue(send_landed(seen, text))

    def test_empty_or_missing_never_confirms(self):
        from scripts.feed_reader import send_landed
        self.assertFalse(send_landed("什么都没有", "一段回复文本"))
        self.assertFalse(send_landed("什么都没有", ""))
        self.assertFalse(send_landed("", "一段回复文本"))


if __name__ == "__main__":
    unittest.main()
