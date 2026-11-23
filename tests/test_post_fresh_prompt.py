"""组稿定制注入（2026-11-22 切片二）：build_fresh_prompt 把 reply_settings
的【本次定制】节插进 reviewer 宪法模板，且插在『要求：』之前（输出格式
指令保持在末尾）。真实 LLM 调用不在测试里——那只该发生在真机 act 轮。

导入 scripts.post_test 会连带 Quartz 等（与 feed_reader 系测试同款代价），
本机真机环境可承受。
"""
import os
import tempfile
import unittest
from unittest import mock

from services.digest import reply_settings as rs


class TestFreshPromptInjection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "settings.json")
        self._old = rs.SETTINGS_PATH
        rs.SETTINGS_PATH = self.path

    def tearDown(self):
        rs.SETTINGS_PATH = self._old

    def test_prompt_carries_customization_before_requirements(self):
        rs.save("humor", "别聊政治", self.path)
        from scripts.post_test import build_fresh_prompt
        prompt = build_fresh_prompt("张三", "爬山归来，腿已经不是自己的了")
        self.assertIn("【本次定制】", prompt)
        self.assertIn("别聊政治", prompt)
        self.assertIn(rs.STYLES["humor"], prompt)
        self.assertLess(prompt.index("【本次定制】"), prompt.index("要求："))
        # 宪法模板主体原样在（乱码忽略 / SKIP 规则没被动过）
        self.assertIn("只输出 SKIP", prompt)
        self.assertIn("识别噪音", prompt)

    def test_no_settings_file_injects_default_style_only(self):
        # 无定制文件 → 风格落默认 friend（其片段照常注入——默认也是一种
        # 选择的定制），但没有用户补充行
        from scripts.post_test import build_fresh_prompt
        prompt = build_fresh_prompt("张三", "爬山归来")
        self.assertIn("【本次定制】", prompt)
        self.assertIn(rs.STYLES["friend"], prompt)
        self.assertNotIn("用户补充要求", prompt)
        self.assertIn("只输出 SKIP", prompt)

    def test_generate_fresh_end_to_end_appends_signature(self):
        # 2026-11-22 Erin gen-failed 冤案回归：切片二重构把 persona 挪进
        # build_fresh_prompt 后，generate_fresh 尾部 sig=persona[…] 变
        # NameError——LLM 成功、署名步炸掉，回复白烧。mock LLM 端到端，
        # 锁死「组稿→定制→LLM→署名」整链。
        from services.digest.reviewer import _resolve_persona
        from scripts.post_test import generate_fresh
        sig = _resolve_persona(None)["signature"]
        fake_resp = mock.Mock()
        fake_resp.choices = [mock.Mock()]
        fake_resp.choices[0].message.content = "这趟爬山看着真痛快，下次带我一个"
        fake_client = mock.Mock()
        fake_client.chat.completions.create.return_value = fake_resp
        with mock.patch("openai.OpenAI", return_value=fake_client):
            out = generate_fresh(
                "张三", "今天爬山归来，腿已经不是自己的了，山顶的风景值了")
        self.assertIn("这趟爬山看着真痛快", out)
        self.assertIn(sig, out)
        # LLM 收到的 prompt 里带定制节（组稿链真通到了模型调用）
        sent = fake_client.chat.completions.create.call_args
        self.assertIn("【本次定制】", sent.kwargs["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
