import os
import tempfile
import unittest

from services.digest import db
from services.digest.pipeline import (
    compute_source_msg_id,
    normalize_text,
    run_pipeline,
    to_raw_item,
)


def _fake_summarizer(results_by_text):
    """Return a summarize_fn that maps raw text → canned results; raises
    for texts mapped to an Exception instance."""
    def fn(raw, interests):
        result = results_by_text[raw["text"]]
        if isinstance(result, Exception):
            raise result
        return result
    return fn


class TestNormalizeAndHash(unittest.TestCase):
    def test_normalize_collapses_whitespace(self):
        self.assertEqual(normalize_text("  你好  世界\n\t再见 "), "你好 世界 再见")
        self.assertEqual(normalize_text(None), "")

    def test_hash_is_stable_and_sensitive(self):
        h1 = compute_source_msg_id("张三", "内容A")
        h2 = compute_source_msg_id("张三", "内容A  ")   # whitespace-insensitive
        h3 = compute_source_msg_id("张三", "内容B")
        h4 = compute_source_msg_id("李四", "内容A")
        self.assertEqual(h1, h2)
        self.assertNotEqual(h1, h3)
        self.assertNotEqual(h1, h4)
        self.assertEqual(len(h1), 16)


class TestToRawItem(unittest.TestCase):
    def test_maps_vlm_fields(self):
        raw = to_raw_item({
            "author": "王五", "text": "正文", "images_desc": "一张图",
            "comments": [{"author": "赵六", "text": "评论"}], "time_hint": "昨天",
        })
        self.assertEqual(raw["source"], "moments")
        self.assertEqual(raw["author"], "王五")
        self.assertEqual(raw["media_desc"], "一张图")
        self.assertEqual(raw["posted_at"], "昨天")
        self.assertEqual(raw["thread"], [{"author": "赵六", "text": "评论"}])

    def test_missing_fields_defaulted(self):
        raw = to_raw_item({})
        self.assertEqual(raw["author"], "")
        self.assertEqual(raw["thread"], [])
        self.assertEqual(raw["media_desc"], "")


class TestRunPipeline(unittest.TestCase):
    RESULT_A = {"title": "标题A", "summary": "摘要A",
                "matched_interest_ids": [1], "score": 8}
    RESULT_B = {"title": "标题B", "summary": "摘要B",
                "matched_interest_ids": [], "score": 0}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "digest.db"))
        db.add_interest(self.conn, "AI", keywords=["ai"], description="AI 相关")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _items(self):
        return [
            {"source": "moments", "author": "张三", "text": "内容A", "thread": []},
            {"source": "moments", "author": "李四", "text": "内容B", "thread": []},
        ]

    def test_new_items_summarized_and_stored(self):
        stats = run_pipeline(self.conn, self._items(),
                             summarize_fn=_fake_summarizer({
                                 "内容A": self.RESULT_A, "内容B": self.RESULT_B}))
        self.assertEqual(stats, {"collected": 2, "new": 2, "summarized": 2,
                                 "skipped": 0, "failed": 0, "errors": []})
        items = db.list_digest_items(self.conn)
        self.assertEqual(len(items), 2)
        by_title = {i["title"]: i for i in items}
        self.assertEqual(by_title["标题A"]["matched_interests"],
                         [{"id": 1, "name": "AI"}])  # id → name resolved
        self.assertEqual(by_title["标题B"]["matched_interests"], [])

    def test_second_run_skips_duplicates(self):
        run_pipeline(self.conn, self._items(),
                     summarize_fn=_fake_summarizer(
                         {"内容A": self.RESULT_A, "内容B": self.RESULT_B}))
        stats = run_pipeline(self.conn, self._items(),
                             summarize_fn=_fake_summarizer({"内容A": self.RESULT_A}))
        self.assertEqual(stats["new"], 0)
        self.assertEqual(stats["skipped"], 2)
        self.assertEqual(stats["summarized"], 0)
        self.assertEqual(len(db.list_digest_items(self.conn)), 2)

    def test_single_failure_does_not_block_batch(self):
        stats = run_pipeline(self.conn, self._items(),
                             summarize_fn=_fake_summarizer({
                                 "内容A": RuntimeError("LLM down"), "内容B": self.RESULT_B}))
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["summarized"], 1)
        titles = [i["title"] for i in db.list_digest_items(self.conn)]
        self.assertEqual(titles, ["标题B"])

    def test_failed_item_retried_on_next_run(self):
        run_pipeline(self.conn, self._items(),
                     summarize_fn=_fake_summarizer({
                         "内容A": RuntimeError("LLM down"), "内容B": self.RESULT_B}))
        stats = run_pipeline(self.conn, [],  # empty batch: retry pending only
                             summarize_fn=_fake_summarizer({"内容A": self.RESULT_A}))
        self.assertEqual(stats["summarized"], 1)
        self.assertEqual(len(db.list_digest_items(self.conn)), 2)

    def test_empty_interests_still_summarizes(self):
        db.update_interest(self.conn, 1, enabled=False)
        stats = run_pipeline(self.conn, [{"source": "moments", "author": "a",
                                          "text": "内容A", "thread": []}],
                             summarize_fn=_fake_summarizer({"内容A": self.RESULT_A}))
        self.assertEqual(stats["summarized"], 1)
        item = db.list_digest_items(self.conn)[0]
        self.assertEqual(item["matched_interests"], [])  # unknown id filtered out


if __name__ == "__main__":
    unittest.main()
