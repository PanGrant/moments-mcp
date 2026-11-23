import os
import tempfile
import unittest
from datetime import datetime

from services.digest.time_hints import approx_posted_at

# 2026-11-06 is a Friday
NOW = datetime(2026, 11, 6, 12, 0, 0)


class TestApproxPostedAt(unittest.TestCase):
    def test_relative_clock(self):
        cases = {
            "2 hour（s） ago": "2026-11-06 10:00",
            "13 hour（s） ago": "2026-11-05 23:00",
            "5小时前": "2026-11-06 07:00",
            "45分钟前": "2026-11-06 11:15",
            "3 minute(s) ago": "2026-11-06 11:57",
            "an hour ago": "2026-11-06 11:00",
            "半小时前": "2026-11-06 11:30",
            "3天前": "2026-11-03 12:00",
            "2 weeks ago": "2026-10-23 12:00",
        }
        for hint, want in cases.items():
            self.assertEqual(approx_posted_at(hint, NOW), want, hint)

    def test_now(self):
        self.assertEqual(approx_posted_at("刚刚", NOW), "2026-11-06 12:00")
        self.assertEqual(approx_posted_at("Just now", NOW), "2026-11-06 12:00")

    def test_yesterday_optional_clock(self):
        self.assertEqual(approx_posted_at("昨天", NOW), "2026-11-05")
        self.assertEqual(approx_posted_at("yesterday", NOW), "2026-11-05")
        self.assertEqual(approx_posted_at("昨天 14:30", NOW), "2026-11-05 14:30")
        self.assertEqual(approx_posted_at("Yesterday at 8:37 PM", NOW),
                         "2026-11-05 20:37")
        self.assertEqual(approx_posted_at("前天", NOW), "2026-11-04")

    def test_weekday_within_last_week(self):
        # NOW is Friday → 星期三 = this week's Wednesday = 11-04
        self.assertEqual(approx_posted_at("星期三", NOW), "2026-11-04")
        self.assertEqual(approx_posted_at("monday at 3:44 PM", NOW),
                         "2026-11-02 15:44")

    def test_absolute_dates(self):
        self.assertEqual(approx_posted_at("11月3日", NOW), "2026-11-03")
        self.assertEqual(approx_posted_at("2025-11-03", NOW), "2025-11-03")
        self.assertEqual(approx_posted_at("11/3", NOW), "2026-11-03")
        self.assertEqual(approx_posted_at("nov 3", NOW), "2026-11-03")
        self.assertEqual(approx_posted_at("Nov 3 at 6:00 PM", NOW),
                         "2026-11-03 18:00")

    def test_future_date_means_last_year(self):
        self.assertEqual(approx_posted_at("12月31日", NOW), "2025-12-31")

    def test_unparseable(self):
        self.assertIsNone(approx_posted_at("", NOW))
        self.assertIsNone(approx_posted_at("转发的一条动态", NOW))

    def test_channel_suffix_time_rows(self):
        # Live 2026-11-06: video/Channel posts store the time row with an
        # inline source suffix — the leading timestamp must still convert.
        cases = {
            "21 hour（s） ago Channels•画家任重": "2026-11-05 15:00",
            "16 hour(s) ago Channels · DJI大疆行业应用": "2026-11-05 20:00",
            "18 hour（s） ago Official Accounts · 解放日…": "2026-11-05 18:00",
            "1小时前人民网": "2026-11-06 11:00",
        }
        for hint, want in cases.items():
            self.assertEqual(approx_posted_at(hint, NOW), want, hint)


class TestDbApproxOrdering(unittest.TestCase):
    def test_list_ordered_by_approx_time(self):
        from services.digest import db
        tmp = tempfile.TemporaryDirectory()
        try:
            conn = db.connect(os.path.join(tmp.name, "d.db"))
            fixtures = [  # (author, posted_at_hint, posted_at_approx)
                ("新的", "2 hour(s) ago", "2026-11-06 10:00"),
                ("中", "3 hour(s) ago", "2026-11-06 09:00"),
                ("旧的", "昨天", "2026-11-05"),
                ("未知", "", ""),
            ]
            for i, (author, hint, approx) in enumerate(fixtures, start=1):
                rid = conn.execute(
                    "INSERT INTO raw_items (source, source_msg_id, author,"
                    " posted_at, posted_at_approx) VALUES ('moments', ?, ?, ?, ?)",
                    (f"k{i}", author, hint, approx)).lastrowid
                conn.execute(
                    "INSERT INTO digest_items (raw_item_id, title, summary)"
                    " VALUES (?, 't', 's')", (rid,))
            conn.commit()
            items = db.list_digest_items(conn)
            self.assertEqual([i["author"] for i in items],
                             ["新的", "中", "旧的", "未知"])
            self.assertEqual(items[0]["posted_at_approx"], "2026-11-06 10:00")
            conn.close()
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
