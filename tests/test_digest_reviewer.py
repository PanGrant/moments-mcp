import os
import tempfile
import unittest
from unittest import mock

from services.digest import db
from services.digest.reviewer import (
    DEFAULT_PERSONA,
    CHANNEL,
    generate_review,
    list_review_targets,
    run_review,
)


def _fake_completion(text):
    resp = mock.Mock()
    resp.choices = [mock.Mock()]
    resp.choices[0].message.content = text
    client = mock.Mock()
    client.chat.completions.create.return_value = resp
    return client


class ReviewerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "digest.db"))
        self.item_ids = {}
        for key, (author, text, sponsored) in {
            "zhang": ("张三", "新无人机发布了，试飞成功", 0),
            "li": ("李四", "跑完半马，刷新了个人纪录", 0),
            "wb": ("WorkBuddy", "WORK BUDDY 帮你理清逻辑", 1),
        }.items():
            rid, _ = db.insert_raw_item(self.conn, {
                "source": "moments", "source_msg_id": key,
                "author": author, "posted_at": "1小时前", "text": text,
                "media_desc": "", "thread": [], "sponsored": sponsored,
            })
            self.item_ids[key] = db.insert_digest_item(
                self.conn, rid, f"{key}标题", text[:30], [], 5)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _generate(self, item_id, text="这条真棒！", **kw):
        client = _fake_completion(text)
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "k"}), \
             mock.patch("openai.OpenAI", return_value=client):
            draft = generate_review(self.conn, item_id, **kw)
        return draft, client


class TestListTargets(ReviewerTestBase):
    def test_sponsored_excluded_and_uses_raw_flag(self):
        targets = list_review_targets(self.conn)
        ids = [t["id"] for t in targets]
        self.assertIn(self.item_ids["zhang"], ids)
        self.assertIn(self.item_ids["li"], ids)
        self.assertNotIn(self.item_ids["wb"], ids)

    def test_items_with_existing_moments_draft_excluded(self):
        db.insert_draft(self.conn, self.item_ids["zhang"], CHANNEL, "已有")
        targets = list_review_targets(self.conn)
        ids = [t["id"] for t in targets]
        self.assertNotIn(self.item_ids["zhang"], ids)
        self.assertIn(self.item_ids["li"], ids)

    def test_limit(self):
        self.assertEqual(len(list_review_targets(self.conn, limit=1)), 1)

    def test_other_channel_drafts_do_not_block(self):
        db.insert_draft(self.conn, self.item_ids["zhang"], "email_reply", "x")
        ids = [t["id"] for t in list_review_targets(self.conn)]
        self.assertIn(self.item_ids["zhang"], ids)


class TestGenerateReview(ReviewerTestBase):
    def test_signature_appended_and_draft_stored(self):
        draft, _client = self._generate(self.item_ids["zhang"],
                                        "无人机试飞都成功了，太厉害了 🎉")
        self.assertEqual(draft["channel"], CHANNEL)
        self.assertEqual(draft["status"], "draft")
        self.assertTrue(draft["content"].endswith(DEFAULT_PERSONA["signature"]))

    def test_no_double_signature(self):
        text = f"太强了 {DEFAULT_PERSONA['signature']}"
        draft, _client = self._generate(self.item_ids["zhang"], text)
        self.assertEqual(draft["content"], text)

    def test_prompt_carries_persona_and_original_text(self):
        _draft, client = self._generate(self.item_ids["zhang"])
        prompt = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn(DEFAULT_PERSONA["name"], prompt)
        self.assertIn("新无人机发布了", prompt)
        self.assertIn("不要署名", prompt)  # signature added by code, not LLM
        self.assertIn("（无）", prompt)  # empty search block
        # quality rules landed 2026-11-08 (batch-1 lessons)
        self.assertIn("乱码", prompt)  # never cite OCR-garbled segments
        self.assertIn("情绪倾向", prompt)  # negative news gets empathy, not hype

    def test_sponsored_item_rejected(self):
        with self.assertRaises(ValueError):
            self._generate(self.item_ids["wb"])

    def test_existing_moments_draft_rejected(self):
        db.insert_draft(self.conn, self.item_ids["zhang"], CHANNEL, "已有")
        with self.assertRaises(ValueError):
            self._generate(self.item_ids["zhang"])

    def test_missing_item_rejected(self):
        with self.assertRaises(KeyError):
            self._generate(999)

    def test_search_snippets_reach_prompt(self):
        client = _fake_completion("赞！")
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "k"}), \
             mock.patch("openai.OpenAI", return_value=client):
            generate_review(self.conn, self.item_ids["zhang"],
                            search_fn=lambda q: ["大疆新品发布会报道"])
        prompt = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("大疆新品发布会报道", prompt)

    def test_search_failure_does_not_block(self):
        def boom(q):
            raise RuntimeError("search down")
        draft, _client = self._generate(self.item_ids["zhang"], search_fn=boom)
        self.assertTrue(draft["content"].endswith(DEFAULT_PERSONA["signature"]))

    def test_persona_overrides(self):
        draft, client = self._generate(
            self.item_ids["li"], persona={"name": "技术点评官",
                                          "signature": "（by 技术点评官）"})
        prompt = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("技术点评官", prompt)
        self.assertTrue(draft["content"].endswith("（by 技术点评官）"))


class TestRunReview(ReviewerTestBase):
    def test_batch_reviews_all_eligible(self):
        client = _fake_completion("了不起！")
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "k"}), \
             mock.patch("openai.OpenAI", return_value=client):
            result = run_review(self.conn)
        self.assertEqual(len(result["reviewed"]), 2)  # zhang + li, wb skipped
        self.assertEqual(result["skipped_sponsored"], 1)
        self.assertEqual(result["errors"], [])
        for r in result["reviewed"]:
            self.assertTrue(r["content"].endswith(DEFAULT_PERSONA["signature"]))

    def test_batch_is_idempotent(self):
        client = _fake_completion("赞")
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "k"}), \
             mock.patch("openai.OpenAI", return_value=client):
            run_review(self.conn)
            result2 = run_review(self.conn)
        self.assertEqual(result2["reviewed"], [])

    def test_one_failure_does_not_block_batch(self):
        client = _fake_completion("好文采！")
        responses = iter([Exception("boom"), None])

        def flaky_create(**kw):
            r = next(responses)
            if isinstance(r, Exception):
                raise r
            resp = mock.Mock()
            resp.choices = [mock.Mock()]
            resp.choices[0].message.content = "好文采！"
            return resp

        client.chat.completions.create.side_effect = flaky_create
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "k"}), \
             mock.patch("openai.OpenAI", return_value=client):
            result = run_review(self.conn)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(len(result["reviewed"]), 1)
        # the failed item can be retried on the next run
        ids = [t["id"] for t in list_review_targets(self.conn)]
        self.assertEqual(len(ids), 1)


class TestTooShortToReply(unittest.TestCase):
    """帖文太短不硬回（用户 2026-11-15 令）：确定性下限先拦（不调模型），
    再由 prompt 的 SKIP 规则兜底；两路都归成 TooShortToReply。"""

    def test_prompt_carries_skip_rule(self):
        from services.digest.reviewer import PROMPT_TEMPLATE
        self.assertIn("SKIP", PROMPT_TEMPLATE)

    def test_body_understandable_floor(self):
        from services.digest.reviewer import body_understandable
        self.assertFalse(body_understandable(""))            # 纯图帖
        self.assertFalse(body_understandable("哈哈"))
        self.assertFalse(body_understandable("… \n …"))      # OCR 碎屑
        self.assertTrue(body_understandable("打卡赣州所有地标逛了一天"))

    def test_generate_fresh_short_body_raises_before_llm(self):
        # 下限拦截发生在建 client 之前——不联网、不读 key
        import scripts.post_test as pt
        with mock.patch("openai.OpenAI") as fake:
            with self.assertRaises(pt.TooShortToReply):
                pt.generate_fresh("阿詹", "")
        fake.assert_not_called()

    def test_generate_fresh_model_skip_verdict_raises(self):
        import scripts.post_test as pt
        client = _fake_completion("SKIP")
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "k"}), \
             mock.patch("openai.OpenAI", return_value=client):
            with self.assertRaises(pt.TooShortToReply):
                pt.generate_fresh("阿詹", "今天去了赣州博物馆打卡参观")
        # 签名绝不加在 SKIP 判定上
        client.chat.completions.create.assert_called_once()


if __name__ == "__main__":
    unittest.main()
