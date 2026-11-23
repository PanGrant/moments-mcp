import os
import tempfile
import unittest

from services.digest import db


class DigestDbTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "digest.db")
        self.conn = db.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _raw(self, source="moments", msg_id="abc", author="张三", text="今天天气不错"):
        return {
            "source": source,
            "source_msg_id": msg_id,
            "author": author,
            "posted_at": "1小时前",
            "text": text,
            "media_desc": "",
            "thread": [{"author": "李四", "text": "同意"}],
        }


class TestSchema(DigestDbTestBase):
    def test_connect_is_idempotent(self):
        conn2 = db.connect(self.db_path)  # CREATE TABLE IF NOT EXISTS
        conn2.close()

    def test_creates_all_tables(self):
        names = {
            r["name"] for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        self.assertTrue({"interests", "raw_items", "digest_items",
                         "threads", "thread_messages", "drafts"} <= names)


class TestInterests(DigestDbTestBase):
    def test_add_and_list_roundtrip(self):
        iid = db.add_interest(self.conn, "AI 产品", keywords=["ai", "大模型"],
                              description="AI 产品动态")
        items = db.list_interests(self.conn)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], iid)
        self.assertEqual(items[0]["keywords"], ["ai", "大模型"])
        self.assertTrue(items[0]["enabled"])

    def test_enabled_only_filter(self):
        db.add_interest(self.conn, "A")
        iid_b = db.add_interest(self.conn, "B", enabled=False)
        names = [i["name"] for i in db.list_interests(self.conn, enabled_only=True)]
        self.assertEqual(names, ["A"])
        self.assertTrue(db.update_interest(self.conn, iid_b, enabled=True))
        names = [i["name"] for i in db.list_interests(self.conn, enabled_only=True)]
        self.assertEqual(names, ["A", "B"])

    def test_update_and_delete(self):
        iid = db.add_interest(self.conn, "旧名")
        self.assertTrue(db.update_interest(self.conn, iid, name="新名", keywords=["x"]))
        items = db.list_interests(self.conn)
        self.assertEqual((items[0]["name"], items[0]["keywords"]), ("新名", ["x"]))
        self.assertTrue(db.delete_interest(self.conn, iid))
        self.assertEqual(db.list_interests(self.conn), [])

    def test_rejects_unknown_field(self):
        iid = db.add_interest(self.conn, "A")
        with self.assertRaises(ValueError):
            db.update_interest(self.conn, iid, admin=1)


class TestRawItems(DigestDbTestBase):
    def test_insert_dedupes_on_source_msg_id(self):
        rid1, is_new1 = db.insert_raw_item(self.conn, self._raw())
        rid2, is_new2 = db.insert_raw_item(self.conn, self._raw())
        self.assertTrue(is_new1)
        self.assertFalse(is_new2)
        self.assertEqual(rid1, rid2)

    def test_same_id_different_source_is_new(self):
        db.insert_raw_item(self.conn, self._raw(source="moments"))
        _, is_new = db.insert_raw_item(self.conn, self._raw(source="email"))
        self.assertTrue(is_new)

    def test_get_raw_item_parses_thread(self):
        rid, _ = db.insert_raw_item(self.conn, self._raw())
        raw = db.get_raw_item(self.conn, rid)
        self.assertEqual(raw["author"], "张三")
        self.assertEqual(raw["thread"], [{"author": "李四", "text": "同意"}])

    def test_get_raw_items_without_digest(self):
        rid, _ = db.insert_raw_item(self.conn, self._raw(msg_id="a"))
        db.insert_raw_item(self.conn, self._raw(msg_id="b"))
        db.insert_digest_item(self.conn, rid, "t", "s", [], 0)
        pending = db.get_raw_items_without_digest(self.conn)
        self.assertEqual([p["source_msg_id"] for p in pending], ["b"])


class TestDigestItems(DigestDbTestBase):
    def test_insert_and_list_with_join_fields(self):
        rid, _ = db.insert_raw_item(self.conn, self._raw())
        did = db.insert_digest_item(
            self.conn, rid, "标题", "摘要",
            [{"id": 1, "name": "AI"}], 7.5)
        items = db.list_digest_items(self.conn)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["id"], did)
        self.assertEqual(item["source"], "moments")
        self.assertEqual(item["author"], "张三")
        self.assertEqual(item["matched_interests"], [{"id": 1, "name": "AI"}])
        self.assertEqual(item["score"], 7.5)

    def test_list_filters_by_source_and_interest(self):
        rid, _ = db.insert_raw_item(self.conn, self._raw(source="moments", msg_id="a"))
        db.insert_digest_item(self.conn, rid, "t1", "s1", [{"id": 1, "name": "AI"}], 5)
        rid2, _ = db.insert_raw_item(self.conn, self._raw(source="email", msg_id="b"))
        db.insert_digest_item(self.conn, rid2, "t2", "s2", [], 0)

        self.assertEqual(len(db.list_digest_items(self.conn, source="moments")), 1)
        self.assertEqual(db.list_digest_items(self.conn, interest_id=1)[0]["title"], "t1")
        self.assertEqual(len(db.list_digest_items(self.conn, interest_id=99)), 0)
        self.assertEqual(len(db.list_digest_items(self.conn)), 2)

    def test_get_digest_item_detail_includes_original(self):
        rid, _ = db.insert_raw_item(self.conn, self._raw(text="原始正文"))
        db.insert_digest_item(self.conn, rid, "t", "s", [], 0)
        did = db.list_digest_items(self.conn)[0]["id"]
        detail = db.get_digest_item(self.conn, did)
        self.assertEqual(detail["text"], "原始正文")
        self.assertEqual(detail["thread"], [{"author": "李四", "text": "同意"}])
        self.assertIsNone(db.get_digest_item(self.conn, 999))

    def test_has_digest(self):
        rid, _ = db.insert_raw_item(self.conn, self._raw())
        self.assertFalse(db.has_digest(self.conn, rid))
        db.insert_digest_item(self.conn, rid, "t", "s", [], 0)
        self.assertTrue(db.has_digest(self.conn, rid))


class TestThreadsAndMessages(DigestDbTestBase):
    def _digest_item(self):
        rid, _ = db.insert_raw_item(self.conn, self._raw())
        item_id = db.insert_digest_item(self.conn, rid, "t", "s", [], 0)
        return item_id

    def test_thread_is_one_per_item(self):
        item_id = self._digest_item()
        t1 = db.get_or_create_thread(self.conn, item_id)
        t2 = db.get_or_create_thread(self.conn, item_id)
        self.assertEqual(t1, t2)

    def test_messages_append_in_order(self):
        tid = db.get_or_create_thread(self.conn, self._digest_item())
        db.add_thread_message(self.conn, tid, "user", "这条在说什么？")
        db.add_thread_message(self.conn, tid, "assistant", "在说无人机。")
        msgs = db.list_thread_messages(self.conn, tid)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
        self.assertEqual(msgs[1]["content"], "在说无人机。")


class TestDrafts(DigestDbTestBase):
    def setUp(self):
        super().setUp()
        rid, _ = db.insert_raw_item(self.conn, self._raw())
        self.item_id = db.insert_digest_item(self.conn, rid, "t", "s", [], 0)

    def test_insert_and_get(self):
        did = db.insert_draft(self.conn, self.item_id, "moments_comment", "好文！")
        d = db.get_draft(self.conn, did)
        self.assertEqual(d["channel"], "moments_comment")
        self.assertEqual(d["status"], "draft")
        self.assertIsNone(d["sent_at"])
        self.assertEqual(len(db.list_drafts(self.conn, self.item_id)), 1)

    def test_update_content_only_for_drafts(self):
        did = db.insert_draft(self.conn, self.item_id, "moments_comment", "初稿")
        self.assertTrue(db.update_draft_content(self.conn, did, "改过的"))
        self.assertEqual(db.get_draft(self.conn, did)["content"], "改过的")
        db.mark_draft_sent(self.conn, did)
        self.assertFalse(db.update_draft_content(self.conn, did, "再改"))

    def test_mark_sent_is_one_way(self):
        did = db.insert_draft(self.conn, self.item_id, "email_reply", "正文")
        self.assertTrue(db.mark_draft_sent(self.conn, did))
        self.assertEqual(db.get_draft(self.conn, did)["status"], "sent")
        self.assertIsNotNone(db.get_draft(self.conn, did)["sent_at"])
        self.assertFalse(db.mark_draft_sent(self.conn, did))


class TestMeta(DigestDbTestBase):
    def test_set_and_get(self):
        self.assertIsNone(db.get_meta(self.conn, "last_collect"))
        db.set_meta(self.conn, "last_collect", '{"ok": true}')
        self.assertEqual(db.get_meta(self.conn, "last_collect"), '{"ok": true}')
        db.set_meta(self.conn, "last_collect", '{"ok": false}')  # upsert
        self.assertEqual(db.get_meta(self.conn, "last_collect"), '{"ok": false}')

    def test_collection_status_roundtrip(self):
        self.assertIsNone(db.get_collection_status(self.conn))
        db.save_collection_status(self.conn, {"ok": True, "screens": 2})
        self.assertEqual(db.get_collection_status(self.conn),
                         {"ok": True, "screens": 2})


if __name__ == "__main__":
    unittest.main()
