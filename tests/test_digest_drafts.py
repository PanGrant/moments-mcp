import os
import tempfile
import unittest
from unittest import mock

from services.digest import db, drafts


def _fake_completion(text):
    """Build a mock OpenAI chat-completions response returning `text`."""
    resp = mock.Mock()
    resp.choices = [mock.Mock()]
    resp.choices[0].message.content = text
    client = mock.Mock()
    client.chat.completions.create.return_value = resp
    return client


class DraftTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "digest.db"))
        rid, _ = db.insert_raw_item(self.conn, {
            "source": "moments", "source_msg_id": "abc",
            "author": "张三", "posted_at": "1小时前", "text": "新无人机发布了",
            "media_desc": "", "thread": [{"author": "李四", "text": "想要"}],
        })
        self.item_id = db.insert_digest_item(
            self.conn, rid, "无人机发布", "某品牌发布新无人机", [], 8)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()


class TestGenerateDraft(DraftTestBase):
    def test_unknown_channel_rejected(self):
        with self.assertRaises(ValueError):
            drafts.generate_draft(self.conn, self.item_id, "sms")

    def test_missing_item_rejected(self):
        with self.assertRaises(KeyError):
            drafts.generate_draft(self.conn, 999, "moments_comment")

    def test_generates_and_stores_draft(self):
        client = _fake_completion("这台无人机真不错，回头一起飞！")
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "k"}), \
             mock.patch("openai.OpenAI", return_value=client):
            draft = drafts.generate_draft(self.conn, self.item_id,
                                          "moments_comment", "带点兴奋感")
        self.assertEqual(draft["content"], "这台无人机真不错，回头一起飞！")
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(len(db.list_drafts(self.conn, self.item_id)), 1)

        # Prompt carries the original text, comments and style instructions.
        prompt = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("新无人机发布了", prompt)
        self.assertIn("李四: 想要", prompt)
        self.assertIn("1-2 句", prompt)  # moments style
        self.assertIn("带点兴奋感", prompt)  # extra instructions


class TestSendDraft(DraftTestBase):
    def _draft(self):
        return db.get_draft(self.conn, db.insert_draft(
            self.conn, self.item_id, "moments_comment", "草稿内容"))

    def test_send_copies_to_clipboard_and_marks_sent(self):
        d = self._draft()
        with mock.patch("services.digest.drafts.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0)
            res = drafts.send_draft(self.conn, d["id"], "最终版本")
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["input"], "最终版本".encode("utf-8"))
        self.assertTrue(res["sent"])
        after = db.get_draft(self.conn, d["id"])
        self.assertEqual(after["status"], "sent")
        self.assertEqual(after["content"], "最终版本")  # user edit persisted
        self.assertIsNotNone(after["sent_at"])

    def test_send_twice_rejected(self):
        d = self._draft()
        with mock.patch("services.digest.drafts.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0)
            drafts.send_draft(self.conn, d["id"])
            with self.assertRaises(ValueError):
                drafts.send_draft(self.conn, d["id"])

    def test_send_unknown_draft(self):
        with self.assertRaises(KeyError):
            drafts.send_draft(self.conn, 999)

    def test_pbcopy_failure_surfaces(self):
        d = self._draft()
        with mock.patch("services.digest.drafts.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=1, stderr=b"boom")
            with self.assertRaises(RuntimeError):
                drafts.send_draft(self.conn, d["id"])
        # not marked sent on failure
        self.assertEqual(db.get_draft(self.conn, d["id"])["status"], "draft")


if __name__ == "__main__":
    unittest.main()
