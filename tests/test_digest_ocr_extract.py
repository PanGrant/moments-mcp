import os
import unittest
from unittest import mock

from services.digest.ocr_extract import (
    extract_ocr_items,
    extract_ocr_screen,
    is_time_row,
    is_time_row_prefix,
    normalize_for_match,
    read_top_item,
    segment_by_regions,
    segment_posts,
)


def R(y, x, text, conf=1.0, w=0.12, h=0.015):
    return {"y": y, "x": x, "w": w, "h": h, "conf": conf, "text": text}


class StubLayout:
    """Duck-typed stand-in for feed_layout.Layout in region tests."""

    def __init__(self, regions):
        self.regions = regions


# Reconstructed from the real capture data/captures/moments_20261105_200519.png
# (English UI): one complete post, one screen-truncated post, icon glyphs.
REAL_EN_ROWS = [
    R(0.04, 0.49, "Moments"),
    R(0.04, 0.20, "<", 0.30),
    R(0.16, 0.31, "Celina Cao"),
    R(0.20, 0.31, "2026选择性的双证组合背书：继10月份通"),
    R(0.20, 0.09, "∞", 0.30),
    R(0.23, 0.31, "过AWS Cloud Practitioner的认证考试，下", 0.50),
    R(0.27, 0.31, "一步备考AWS Al Practitioner，把过去3.5", 0.50),
    R(0.30, 0.31, "年沉淀的AI实战经验，通过认证进行标准化", 0.50),
    R(0.34, 0.31, "背书（AI基础概念，云上AI服务选型，AI.•", 0.30),
    R(0.38, 0.31, "Show More", 0.50),
    R(0.72, 0.31, "5 hour（s） ago", 0.50),
    R(0.73, 0.88, "••", 0.30),
    R(0.83, 0.31, "无草"),
    R(0.87, 0.31, "留个位置，看几天才能证实骗局心", 0.50),
    R(0.91, 0.31, "博览会上脑力开发、提升注意力、调控健康"),
    R(0.94, 0.31, "这些以前靠气功、平底锅盖头、信息水的绝"),
    R(0.98, 0.31, "汗现左右了A加培 雨旦加唔洗習3", 0.30),
]


class TestNormalize(unittest.TestCase):
    def test_fullwidth_parens_and_colon(self):
        self.assertEqual(normalize_for_match("5 hour（s） ago"), "5 hour(s) ago")

    def test_collapses_whitespace_and_lowers(self):
        self.assertEqual(normalize_for_match("  Just   NOW "), "just now")


class TestTimeRow(unittest.TestCase):
    def test_zh_relative(self):
        for t in ("刚刚", "45秒前", "5分钟前", "1小时前", "半小时前", "3天前",
                  "2周前", "3个月前", "1年前"):
            self.assertTrue(is_time_row(t), t)

    def test_zh_absolute_with_optional_clock(self):
        for t in ("昨天", "昨天 14:30", "前天", "星期三", "周二 09:15",
                  "11月3日", "2025年11月3日 08:00", "11-03", "11/3"):
            self.assertTrue(is_time_row(t), t)

    def test_en_relative_including_fullwidth_parens(self):
        for t in ("Just now", "just now", "Yesterday", "Yesterday at 8:37 PM",
                  "5 hour（s） ago", "2 hours ago", "an hour ago", "3 day(s) ago",
                  "2 weeks ago", "last month"):
            self.assertTrue(is_time_row(t), t)

    def test_en_absolute(self):
        for t in ("Nov 3", "November 3 at 6:00 PM", "Monday at 3:44 PM", "周三 14:30"):
            self.assertTrue(is_time_row(t), t)

    def test_post_text_is_not_a_time_row(self):
        for t in ("昨天我去爬山了", "3月15日活动报名开始", "昨天 14:30 我出发",
                  "5 hours ago we met", "Moments", "Monday morning run photos",
                  "May the force be with you all friends"):
            self.assertFalse(is_time_row(t), t)

    def test_too_long_is_not_time(self):
        self.assertFalse(is_time_row("昨天" * 30))


class TestTimeRowPrefix(unittest.TestCase):
    """Channel/video posts render time + source on one line (live
    2026-11-06): "18 hour(s) ago Official Accounts · 解放日报"."""

    def test_channel_suffix_rows_match_as_prefix(self):
        for t in ("18 hour（s） ago Official Accounts · 解放日…",
                  "16 hour(s) ago Channels · DJI大疆行业应用",
                  "15 hour（s） ago 万得股票",
                  "3 hour（s） ago Channels． 新华网",
                  "1小时前人民网"):
            self.assertTrue(is_time_row_prefix(t), t)

    def test_exact_matching_unchanged(self):
        self.assertTrue(is_time_row("18 hour（s） ago"))
        self.assertFalse(is_time_row("1小时前人民网"))

    def test_long_prose_rejected(self):
        self.assertFalse(is_time_row_prefix("昨天" * 40))

    def test_absolute_date_opening_text_is_not_a_prefix(self):
        # Live 2026-11-07: a post text row "2/3业主同意，这绝不是一件容易
        # 的事。…" prefix-matched ^\d{1,2}/\d{1,2}$, truncated the post and
        # faked posted_at 2026-02-03. WeChat never suffixes an absolute
        # calendar date with a source — only relative durations prefix-match.
        for t in ("2/3业主同意，这绝不是一件容易的事。于⋯",
                  "11月05日 17:01 万得股票",
                  "昨天 14:30 某个来源",
                  "2026-11-07 新闻早班车"):
            self.assertFalse(is_time_row_prefix(t), t)
        self.assertTrue(is_time_row("2/3"))          # exact date still works
        self.assertTrue(is_time_row("11月05日 17:01"))


class TestSegmentRealEnglishCapture(unittest.TestCase):
    def test_one_complete_post_one_truncated_dropped(self):
        posts, meta = segment_posts(REAL_EN_ROWS)
        self.assertEqual(len(posts), 1)
        self.assertEqual(meta["dropped_truncated"], 1)
        post = posts[0]
        self.assertEqual(post["author"], "Celina Cao")
        self.assertEqual(post["time_hint"], "5 hour（s） ago")
        self.assertTrue(post["text"].startswith("2026选择性的双证组合背书"))
        self.assertIn("通过认证进行标准化", post["text"])
        # CJK wrap-join: no injected spaces inside the Chinese sentence
        self.assertNotIn(" 继10月份 ", post["text"])

    def test_folded_post_carries_expand_target(self):
        posts, _meta = segment_posts(REAL_EN_ROWS)
        target = posts[0].get("expand")
        self.assertIsNotNone(target)  # "Show More" row at y=0.38
        self.assertAlmostEqual(target["cx"], 0.31 + 0.06, places=3)
        self.assertAlmostEqual(target["cy"], 0.38 + 0.0075, places=3)
        # without the "Show More" row the same post carries no target
        posts2, _ = segment_posts([r for r in REAL_EN_ROWS
                                   if normalize_for_match(r["text"])
                                   not in ("show more", "全文")])
        self.assertEqual(len(posts2), 1)
        self.assertNotIn("expand", posts2[0])

    def test_icons_and_chrome_filtered(self):
        _posts, meta = segment_posts(REAL_EN_ROWS)
        # Moments, <, ∞, ••  ("Show More" is intercepted as the expand
        # target, not counted as noise)
        self.assertEqual(meta["noise_rows"], 4)

    def test_cover_name_is_not_taken_as_author(self):
        # Top-of-feed layout: profile cover name sits right-aligned (x≈0.61)
        # ABOVE the first real post — it must not hijack the author slot.
        rows = [
            R(0.10, 0.61, "Grant Pan"),
            R(0.16, 0.31, "无草"),
            R(0.20, 0.31, "如此大力推进各类消费基础设施建设，完美"),
            R(0.24, 0.31, "诠释了要想富先修路"),
            R(0.28, 0.31, "2 hours ago", 0.5),
        ]
        posts, _meta = segment_posts(rows)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["author"], "无草")
        self.assertEqual(posts[0]["text"], "如此大力推进各类消费基础设施建设，完美诠释了要想富先修路")


ZH_ROWS = [
    R(0.05, 0.50, "朋友圈"),
    R(0.12, 0.31, "张三"),
    R(0.16, 0.31, "今天天气好，出去跑了五公里"),
    R(0.19, 0.31, "配速终于进六了"),
    R(0.36, 0.31, "1小时前", 0.8),
    R(0.40, 0.88, "••", 0.3),
    R(0.44, 0.31, "李四、王五觉得很赞"),
    R(0.48, 0.31, "王小明：太厉害了，求跑步路线"),
    R(0.52, 0.31, "赵六： +1"),
    # second post, English comment style, complete
    R(0.60, 0.31, "David"),
    R(0.64, 0.31, "Released v2.0 today"),
    R(0.67, 0.31, "check it out"),
    R(0.71, 0.31, "3 hours ago", 0.9),
    R(0.75, 0.31, "Amy: congrats!"),
    # third post, truncated at the bottom → must be dropped
    R(0.85, 0.31, "陈七"),
    R(0.90, 0.31, "这个帖子还没显示完整就被屏幕截断了"),
]


class TestSegmentChineseUI(unittest.TestCase):
    def test_two_posts_comments_attached_truncated_dropped(self):
        posts, meta = segment_posts(ZH_ROWS)
        self.assertEqual(len(posts), 2)
        self.assertEqual(meta["dropped_truncated"], 1)

        p1, p2 = posts
        self.assertEqual(p1["author"], "张三")
        self.assertEqual(p1["time_hint"], "1小时前")
        self.assertEqual(p1["text"], "今天天气好，出去跑了五公里配速终于进六了")
        self.assertEqual(p1["comments"], [
            {"author": "王小明", "text": "太厉害了，求跑步路线"},
            {"author": "赵六", "text": "+1"},
        ])

        self.assertEqual(p2["author"], "David")
        self.assertEqual(p2["time_hint"], "3 hours ago")
        self.assertEqual(p2["text"], "Released v2.0 today check it out")
        self.assertEqual(p2["comments"], [{"author": "Amy", "text": "congrats!"}])


class TestOrphanFooterRows(unittest.TestCase):
    def test_screen_top_footer_rows_with_no_posts_do_not_crash(self):
        # Mid-scroll screen: the post above scrolled off, so the visible top
        # is its closing time row + like/comment rows — no attribution is
        # possible, the rows must be dropped without crashing (2026-11-06).
        rows = [
            R(0.12, 0.31, "18 hour（s） ago", 0.5),
            R(0.16, 0.38, "Viva"),
            R(0.20, 0.31, "王小明：说得太好了"),
            R(0.26, 0.31, "张三"),
            R(0.30, 0.31, "正常的新帖子"),
            R(0.34, 0.31, "2 hours ago", 0.5),
        ]
        posts, _meta = segment_posts(rows)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["author"], "张三")
        self.assertEqual(posts[0]["text"], "正常的新帖子")
        self.assertEqual(posts[0]["comments"], [])

    def test_liker_names_indented_in_footer_are_not_authors(self):
        rows = [
            R(0.10, 0.31, "张三"),
            R(0.14, 0.31, "今天天气好"),
            R(0.18, 0.31, "2 hours ago", 0.5),
            R(0.22, 0.38, "Viva"),          # liker name, indented box
            R(0.26, 0.38, "Celina Cao"),    # second liker, same box
            R(0.30, 0.31, "李四"),           # next post author, unindented
            R(0.34, 0.31, "晚上跑步去"),
            R(0.38, 0.31, "5 hours ago", 0.5),
        ]
        posts, _meta = segment_posts(rows)
        self.assertEqual([p["author"] for p in posts], ["张三", "李四"])
        self.assertEqual(posts[0]["comments"], [])


# Reconstructed from data/captures/moments_20261106_133537_s1.png:
# screen top = share-card tail of a post whose header scrolled off, then a
# complete ad post (19-char service-account name) with its own card tail.
S1_AD_ROWS = [
    R(0.10, 0.32, "中国平安", 0.30),
    R(0.10, 0.36, "38", 1.0),
    R(0.14, 0.33, "高质转型 MVP引领", 0.30),
    R(0.16, 0.33, "热烈玩贺卫幕娟经理达成MVP類英保险代理人", 0.30),
    R(0.27, 0.31, "20 hour（s） ago", 0.50),
    R(0.28, 0.88, "。。", 0.30),
    R(0.38, 0.31, "安徽服务管家0127@平安产险服务管家", 1.00),
    R(0.42, 0.36, "答应我，这波羊毛一定要薅！", 1.00),
    R(0.46, 0.31, "换季气温像过山车，全家健康怎么保？", 1.00),
    R(0.49, 0.36, "先领：20元加油/充电券", 1.00),
    R(0.53, 0.36, "再买：家庭保障加购1件9折、2件8折", 0.50),
    R(0.56, 0.31, "不知道咋选？直播间1V1帮你算账！….", 0.30),
    R(0.61, 0.31, "Show More", 0.50),
    R(0.68, 0.37, "I.", 0.30),
    R(0.70, 0.32, "好车主家庭保障节", 0.30),
    R(0.73, 0.35, "一必新領比道！充专料一", 0.30),
    R(0.95, 0.31, "20 hour（s） ago", 0.50),
    R(0.96, 0.89, "••", 0.30),
]

# Reconstructed from data/captures/moments_20261106_133537_s2.png: ad-unit
# chrome + card tail (header off-screen) — the raw#24 phantom's source.
S2_AD_TAIL_ROWS = [
    R(0.10, 0.31, "mdd7fbuj5vYoezv", 1.00),
    R(0.15, 0.31, "Hide", 1.00),
    R(0.21, 0.37, "11.11", 0.50),
    R(0.24, 0.32, "好东主家庭保障节", 0.30),
    R(0.27, 0.35, "一必新領比道！充专料一", 0.30),
    R(0.49, 0.31, "20 hour（s） ago", 0.50),
    R(0.58, 0.31, "余下2条〉", 0.50),
    R(0.69, 0.31, "Santal Li", 1.00),
    R(0.73, 0.31, "排云而上，直入凌霄", 1.00),
]


class TestAvatarGatedSegmentation(unittest.TestCase):
    """Phantom-post protection: headerless screen-top units must never
    produce posts (live 2026-11-06: raw#19/20/22/24 were all phantoms of
    this class — card text / ad chrome taken as authors)."""

    def test_card_tail_top_is_dropped_long_name_author_kept(self):
        avatar_fn = lambda y: y > 0.3  # only the ad post's header on screen
        posts, meta = segment_posts(S1_AD_ROWS, avatar_fn=avatar_fn)
        self.assertEqual(len(posts), 1)
        post = posts[0]
        self.assertEqual(post["author"], "安徽服务管家0127@平安产险服务管家")
        self.assertEqual(post["time_hint"], "20 hour（s） ago")
        self.assertTrue(post["text"].startswith("答应我，这波羊毛一定要薅！"))
        self.assertEqual(meta["dropped_no_author"], 1)

    def test_ad_chrome_tail_never_becomes_a_post(self):
        avatar_fn = lambda y: y > 0.6  # only Santal Li's header would qualify
        posts, meta = segment_posts(S2_AD_TAIL_ROWS, avatar_fn=avatar_fn)
        self.assertEqual(posts, [])  # phantom gone; Santal Li bottom-truncated
        self.assertEqual(meta["dropped_no_author"], 1)
        self.assertEqual(meta["dropped_truncated"], 1)

    def test_without_probe_behavior_is_unchanged(self):
        # No avatar_fn (e.g. Pillow unavailable): old OCR-only behavior.
        posts, _meta = segment_posts(S2_AD_TAIL_ROWS)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["author"], "mdd7fbuj5vYoezv")


# ---------------------------------------------------------------------------
# divider-region segmentation (the primary path, feed_layout model)
# ---------------------------------------------------------------------------

class TestSegmentByRegions(unittest.TestCase):
    """Each region between two full-width separator lines is one unit.
    Attribution is avatar-blind (user directive 2026-11-08): a bounded
    region's author sits within AUTHOR_ZONE of its top divider; an
    unbounded region (region 0) attributes its first qualifying author-
    column row at or below LOOSE_AUTHOR_MIN_Y. Time rows and the
    "余下N条" strip are never authors. No author → skip; no time row →
    truncated → drop."""

    def test_ad_screen_card_tail_region_skipped_post_intact(self):
        # S1 layout (from the real capture's divider positions): region0 =
        # the card tail of a post whose header scrolled off, region1 = the
        # complete ad post. Region0's phantom guards: its only author-column
        # row is the TAIL'S OWN TIME ROW ("20 hour（s） ago") — a time row is
        # never a name.
        layout = StubLayout([(0.0, 0.32), (0.32, 1.0)])
        posts, meta, _tail = segment_by_regions(S1_AD_ROWS, layout)
        self.assertEqual(len(posts), 1)
        post = posts[0]
        self.assertEqual(post["author"], "安徽服务管家0127@平安产险服务管家")
        self.assertEqual(post["time_hint"], "20 hour（s） ago")
        self.assertTrue(post["text"].startswith("答应我，这波羊毛一定要薅！"))
        # "Show More" intercepted as the expand target
        self.assertAlmostEqual(post["expand"]["cx"], 0.37, places=3)
        self.assertAlmostEqual(post["expand"]["cy"], 0.6175, places=3)
        self.assertEqual(meta["dropped_truncated"], 0)

    def test_ad_chrome_tail_and_footer_strip_never_become_posts(self):
        # S2 layout: region0 = ad-unit chrome tail, region1 = "余下2条"
        # footer strip (a strip row sits in the author column at conf≈0.5 —
        # the strip guard rejects it), region2 = Santal Li's truncated head.
        layout = StubLayout(
            [(0.0, 0.55), (0.55, 0.66), (0.66, 1.0)])
        posts, meta, _tail = segment_by_regions(S2_AD_TAIL_ROWS, layout)
        self.assertEqual(posts, [])
        self.assertEqual(meta["dropped_truncated"], 1)  # Santal Li, no closer

    def test_merged_row_region_skipped_intact_post_and_truncated_tail(self):
        # S0 layout: region0 = baseline-merged row (previous post's tail),
        # region1 = the intact 工行早餐 share post, region2 = "余下1条"
        # strip, region3 = the ad post's head (bottom-truncated).
        layout = StubLayout(
            [(0.0, 0.20), (0.20, 0.45), (0.45, 0.56), (0.56, 1.0)])
        posts, meta, _tail = segment_by_regions(S0_MERGED_ROWS, layout)
        self.assertEqual(len(posts), 1)
        post = posts[0]
        self.assertEqual(post["author"], "沈津羽@中国工商银行")
        self.assertEqual(post["time_hint"], "4 hour（s） ago")
        self.assertTrue(post["text"].startswith("【安徽分行】财经早餐"))
        self.assertEqual(meta["dropped_truncated"], 1)  # ad post head

    def test_feed_top_first_post_gets_a_proper_region(self):
        # The cover section has a divider below it, so the first real post
        # is region 1 — attributed normally, cover rows never leak in.
        layout = StubLayout([(0.0, 0.14), (0.14, 0.78), (0.78, 1.0)])
        posts, meta, _tail = segment_by_regions(REAL_EN_ROWS, layout)
        self.assertEqual(len(posts), 1)
        post = posts[0]
        self.assertEqual(post["author"], "Celina Cao")
        self.assertEqual(post["time_hint"], "5 hour（s） ago")
        self.assertTrue(post["text"].startswith("2026选择性的双证组合背书"))
        self.assertIn("通过认证进行标准化", post["text"])
        self.assertAlmostEqual(post["expand"]["cy"], 0.3875, places=3)
        # 无草's region: its name row sits in the zone, so the head is now
        # recognized as a bottom-truncated post (avatar-blind model can't
        # tell "head with unreadable time row" from a complete region — it
        # reports truncated, which is the truer reading)
        self.assertEqual(meta["dropped_truncated"], 1)

    def test_card_date_mid_region_does_not_close_the_post_early(self):
        # Last time-row candidate wins: a share-card date ("11月05日 17:01"
        # inside the card) must neither close the post early nor hide the
        # article body below it.
        rows = [
            R(0.10, 0.31, "韩玉珂"),
            R(0.14, 0.31, "万得股票7x24小时快讯"),
            R(0.40, 0.39, "11月05日 17:01"),
            R(0.55, 0.31, "上证指数低开高走，券商板块领涨。"),
            R(0.70, 0.31, "15 hour（s） ago", 0.5),
        ]
        layout = StubLayout([(0.0, 0.06), (0.06, 0.75), (0.75, 1.0)])
        posts, _meta, _tail = segment_by_regions(rows, layout)
        self.assertEqual(len(posts), 1)
        post = posts[0]
        self.assertEqual(post["author"], "韩玉珂")
        self.assertEqual(post["time_hint"], "15 hour（s） ago")
        self.assertIn("上证指数低开高走", post["text"])

    def test_row_outside_author_column_is_dropped(self):
        # the only text sits left of the author column (e.g. OCR text
        # inside the avatar image) — never an author
        rows = [R(0.35, 0.26, "头像图内文字", 0.9)]
        layout = StubLayout([(0.0, 0.32), (0.32, 1.0)])
        posts, meta, _tail = segment_by_regions(rows, layout)
        self.assertEqual(posts, [])
        self.assertEqual(meta["dropped_no_author"], 1)

    def test_scrolled_off_name_row_is_not_replaced_by_card_title(self):
        # Live 2026-11-07 (s12 of the 19:58 run): 服务管家's post tail; the
        # name row had scrolled off the screen top. The card title (x=0.349,
        # the card's own indent) must NOT be promoted to author, and the
        # body row at y=0.141 sits ABOVE the loose floor — a mid-feed tail's
        # rows start at the screen top, so a top-band author-column row is
        # body text. The region drops as unattributable while its rows
        # travel as the stitch source.
        rows = [
            R(0.107, 0.349, "立冬至，注意添衣保暖|这3种「取暖模"),
            R(0.141, 0.308, "式」需要反着来？速戳安稳过冬4"),
            R(0.326, 0.306, "2 hour（s） ago", 0.5),
        ]
        layout = StubLayout([(0.0, 0.475), (0.475, 1.0)])
        posts, meta, tail = segment_by_regions(rows, layout)
        self.assertEqual(posts, [])
        # region 0 is unattributable but not counted: only divider-bounded
        # regions owe the model a header
        self.assertEqual(meta["dropped_no_author"], 0)
        first = tail["first"]
        self.assertFalse(first["header"])
        self.assertIsNone(first["author"])
        self.assertEqual(first["closer"], "2 hour（s） ago")
        self.assertIn("式」需要反着来？速戳安稳过冬4", first["texts"])

    def test_feed_top_cover_pixels_do_not_break_attribution(self):
        # Live 2026-11-07 (s1): a lone whole-screen region at the feed top —
        # the cover's own pixels are never analyzed, and the name is found
        # by the loose rule: the first author-column row at or below
        # LOOSE_AUTHOR_MIN_Y (卫紫娴 at 0.321, below the ~0.19 cover).
        rows = [
            R(0.189, 0.603, "Grant Pan"),   # right of CONTENT_X → filtered
            R(0.321, 0.311, "卫紫娴"),
            R(0.365, 0.308, "国之所向，平安担当"),
        ]
        layout = StubLayout([(0.0, 1.0)])
        posts, _meta, tail = segment_by_regions(rows, layout)
        self.assertEqual(posts, [])  # no closer on screen — opens a stitch
        last = tail["last"]
        self.assertEqual(last["author"], "卫紫娴")
        self.assertEqual(last["texts"], ["国之所向，平安担当"])

    def test_aligned_page_reads_below_the_top_divider(self):
        # One-item-per-screen aligned capture: the previous item's bottom
        # divider landed at the screen top (0.09 ≤ TOP_ALIGNED_MAX_Y). The
        # item is the region BELOW it — region 0 (above the divider) holds
        # only the previous item's tail rows and must not leak in; and
        # divider_y must be the item's BOTTOM divider (reporting the top one
        # would make the collector's advance scroll by zero and dead-loop).
        rows = [
            R(0.03, 0.31, "上一条的残行"),
            R(0.131, 0.308, "张三"),
            R(0.18, 0.31, "正文第一行"),
            R(0.24, 0.31, "正文第二行"),
            R(0.46, 0.31, "3 hour（s） ago", 0.5),
        ]
        layout = StubLayout([(0.0, 0.09), (0.09, 0.5), (0.5, 1.0)])
        reading = read_top_item("x.png", ocr_rows_fn=lambda _p: rows,
                                layout=layout)
        unit = reading["unit"]
        self.assertEqual(unit["author"], "张三")
        self.assertEqual(unit["texts"], ["正文第一行", "正文第二行"])
        self.assertEqual(unit["closer"], "3 hour（s） ago")
        self.assertEqual(reading["divider_y"], 0.5)

    def test_low_top_divider_falls_back_to_region0(self):
        # A misaligned retry: the top divider landed below the aligned band
        # (0.25 > TOP_ALIGNED_MAX_Y). Region 0 (the previous item's tail)
        # reads unattributable; divider_y is that divider so the collector
        # re-aligns it to the screen top on the next scroll.
        rows = [R(0.10, 0.31, "上一条的残行")]
        layout = StubLayout([(0.0, 0.25), (0.25, 1.0)])
        reading = read_top_item("x.png", ocr_rows_fn=lambda _p: rows,
                                layout=layout)
        self.assertIsNone(reading["unit"]["author"])
        self.assertEqual(reading["divider_y"], 0.25)

    def test_body_row_below_author_zone_is_not_the_author(self):
        # Live 2026-11-06 (Aimee Cao): under the avatar model the first
        # content line ("周报") became the author whenever the avatar's
        # detected top sat too low. The zone rule is avatar-blind: the
        # author is the first author-column row within AUTHOR_ZONE of the
        # divider (Aimee at dy=0.063); the body row at dy=0.109 is outside.
        rows = [
            R(0.50, 0.31, "Aimee Cao"),
            R(0.546, 0.31, "周报"),
            R(0.88, 0.31, "17 hour（s） ago", 0.5),
            R(0.945, 0.38, "Lillian, Amina"),
        ]
        layout = StubLayout([(0.0, 0.437), (0.437, 0.917), (0.917, 1.0)])
        posts, _meta, _tail = segment_by_regions(rows, layout)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["author"], "Aimee Cao")
        self.assertEqual(posts[0]["text"], "周报")
        self.assertEqual(posts[0]["time_hint"], "17 hour（s） ago")

    def test_channel_post_time_row_with_source_suffix_closes(self):
        # Live 2026-11-06 (s0): Viva's Channels share closes with
        # "18 hour(s) ago Official Accounts · 解放日…" on one line —
        # exact matching missed it and the post was dropped as truncated.
        rows = [
            R(0.223, 0.31, "Viva"),
            R(0.27, 0.31, "看题目就猜是 ABB"),
            R(0.34, 0.36, "外媒称中国已告知日本软银集团，不会批准其收购瑞士-瑞典合资…"),
            R(0.47, 0.31, "18 hour（s） ago Official Accounts · 解放日…", 0.5),
        ]
        layout = StubLayout([(0.0, 0.167), (0.167, 0.505), (0.505, 1.0)])
        posts, _meta, _tail = segment_by_regions(rows, layout)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["author"], "Viva")
        self.assertEqual(posts[0]["time_hint"],
                         "18 hour（s） ago Official Accounts · 解放日…")
        self.assertEqual(posts[0]["text"], "看题目就猜是 ABB外媒称中国已告知日本软银集团，不会批准其收购瑞士-瑞典合资…")

    def test_tail_exposes_open_bottom_post_for_stitching(self):
        # A tall post's head (author + text, no closing time row) travels
        # out via tail["last"] so the collector keeps reading it on the
        # next screen instead of dropping it as truncated.
        rows = [
            R(0.56, 0.31, "Viva"),
            R(0.60, 0.31, "一个年轻客户问我，这个单数额不大。"),
            R(0.64, 0.31, "还做个表比半天，会有利润么。"),
        ]
        layout = StubLayout([(0.0, 0.505), (0.505, 1.0)])
        posts, meta, tail = segment_by_regions(rows, layout)
        self.assertEqual(posts, [])
        self.assertEqual(meta["dropped_truncated"], 1)
        last = tail["last"]
        self.assertEqual(last["author"], "Viva")  # head present (header)
        self.assertIsNone(last["closer"])
        self.assertEqual(last["texts"],
                         ["一个年轻客户问我，这个单数额不大。",
                          "还做个表比半天，会有利润么。"])
        self.assertTrue(tail["first"]["divider_bounded"])
        self.assertFalse(tail["first"]["header"])

    def test_tail_region0_closer_stitches_previous_screen_post(self):
        # Next screen: region 0 = the essay's continuation + its closing
        # row (the stitch source); the post below is closed normally.
        rows = [
            R(0.12, 0.31, "就好像做饭店，重要的不是客单利润。"),
            R(0.30, 0.31, "4 hour（s） ago", 0.5),
            R(0.36, 0.31, "Viva"),
            R(0.42, 0.31, "下一条新帖"),
            R(0.55, 0.31, "3 hours ago", 0.5),
        ]
        layout = StubLayout([(0.0, 0.32), (0.32, 1.0)])
        posts, _meta, tail = segment_by_regions(rows, layout)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["author"], "Viva")
        first = tail["first"]
        self.assertEqual(first["closer"], "4 hour（s） ago")
        self.assertEqual(first["texts"], ["就好像做饭店，重要的不是客单利润。"])
        self.assertTrue(first["divider_bounded"])

    def test_single_region_screen_tail_not_divider_bounded(self):
        # A whole screen filled by the tall post: no divider → the stitcher
        # must keep the post open (not force-finish at a phantom divider).
        # The lone region plays both tail roles. The loose rule CAN
        # attribute a head here (feed-top semantics); on a mid-feed screen
        # that is the collector's problem — only index-0 readings may open
        # fresh items from an unbounded region, so a mid-round single-
        # region screen continues the open item instead.
        rows = [R(0.56, 0.31, "Viva"), R(0.60, 0.31, "长文第一段")]
        layout = StubLayout([(0.0, 1.0)])
        posts, _meta, tail = segment_by_regions(rows, layout)
        self.assertEqual(posts, [])  # no closer → truncated, not a post
        self.assertFalse(tail["first"]["divider_bounded"])
        self.assertEqual(tail["last"]["author"], "Viva")
        self.assertEqual(tail["last"]["texts"], ["长文第一段"])
        # the stitch source is the rows above the attributed head (none)
        self.assertEqual(tail["first"]["texts"], [])

    def test_feed_top_single_region_post1_attributed(self):
        # Live 2026-11-06 (feed-top capture): the cover abuts post 1 with NO
        # divider — the whole screen is one region above a (non-existent)
        # first divider, so post 1 must be attributed FROM region 0. Cover
        # rows are not author-column rows, so the loose rule skips them.
        rows = [
            R(0.45, 0.61, "Grant Pan"),                    # cover name
            R(0.50, 0.45, "封面上的字"),                    # cover caption
            R(0.694, 0.31, "Catherine凯瑟琳"),
            R(0.72, 0.31, "Denodo在Gartner报告里又领先了"),
            R(0.947, 0.31, "15 minute（s） ago Official Accounts · Den…", 0.5),
        ]
        layout = StubLayout([(0.0, 1.0)])
        posts, meta, tail = segment_by_regions(rows, layout)
        self.assertEqual(len(posts), 1)
        post = posts[0]
        self.assertEqual(post["author"], "Catherine凯瑟琳")
        self.assertNotIn("Grant Pan", post["text"])
        self.assertNotIn("封面上的字", post["text"])
        self.assertEqual(post["text"], "Denodo在Gartner报告里又领先了")
        self.assertEqual(post["time_hint"],
                         "15 minute（s） ago Official Accounts · Den…")
        # cover rows above the author live in the stitch source (never in
        # the body); "Grant Pan" additionally fails the content-x filter
        self.assertEqual(meta["noise_rows"], 1)
        self.assertEqual(tail["first"]["texts"], ["封面上的字"])
        # single region: the closed post must NOT open a stitch
        self.assertIsNotNone(tail["last"]["closer"])

    def test_feed_top_single_region_tall_post_opens_stitch(self):
        # Post 1 taller than the viewport on the feed-top screen: no divider
        # anywhere (n=1), no closing time row → the lone region's head must
        # travel out via tail["last"] so the collector reads on (user rule:
        # 第一条读到下一个分割线为止).
        rows = [
            R(0.70, 0.31, "Viva"),
            R(0.74, 0.31, "第一屏能看到的开头"),
        ]
        layout = StubLayout([(0.0, 1.0)])
        posts, _meta, tail = segment_by_regions(rows, layout)
        self.assertEqual(posts, [])
        last = tail["last"]
        self.assertEqual(last["author"], "Viva")  # head present (header)
        self.assertIsNone(last["closer"])
        self.assertEqual(last["texts"], ["第一屏能看到的开头"])

    def test_long_headline_row_is_not_the_author(self):
        # Live 2026-11-07: a video-share region whose first row was the
        # whole headline (no length cap in the region path) became the
        # "author". The legacy cap applies here too; the real name row
        # (万得股票) sits below the headline in the same column.
        rows = [
            R(0.655, 0.31, "立冬至，注意添衣保暖|这3种「取暖模式」需要反着来？速戳安稳过冬"),
            R(0.667, 0.31, "万得股票"),
            R(0.72, 0.31, "正文一行"),
            R(0.80, 0.31, "1 hour（s） ago", 0.5),
        ]
        layout = StubLayout([(0.0, 0.65), (0.65, 1.0)])
        posts, _meta, _tail = segment_by_regions(rows, layout)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["author"], "万得股票")

    def test_date_opening_text_row_does_not_close_the_post(self):
        # Live 2026-11-07: "2/3业主同意，…" prefix-matched as a time row,
        # truncating the post mid-sentence with a garbage time_hint.
        rows = [
            R(0.567, 0.31, "Lianchang 侯哥"),
            R(0.61, 0.31, "最近小区很热闹。用了快20年的电梯需要更换。"),
            R(0.65, 0.31, "2/3业主同意，这绝不是一件容易的事。于⋯"),
            R(0.69, 0.31, "后面还有更多内容。"),
            R(0.75, 0.31, "3 hour（s） ago", 0.5),
        ]
        layout = StubLayout([(0.0, 0.55), (0.55, 1.0)])
        posts, _meta, _tail = segment_by_regions(rows, layout)
        self.assertEqual(len(posts), 1)
        post = posts[0]
        self.assertEqual(post["author"], "Lianchang 侯哥")
        self.assertIn("2/3业主同意", post["text"])
        self.assertIn("后面还有更多内容", post["text"])
        self.assertEqual(post["time_hint"], "3 hour（s） ago")

    def test_region0_with_header_stitch_source_is_pre_rows_only(self):
        # Live 2026-11-07: region 0 holds the PREVIOUS post's tail AND the
        # next post's header+body. The stitch source must be only the rows
        # above the new author (previous tail); the new post emits intact —
        # consuming the whole region had polluted the stitched post with
        # the next post's text and produced duplicate variants.
        rows = [
            R(0.05, 0.31, "后面还有两行收尾。"),          # previous post tail
            R(0.09, 0.31, "3 hour（s） ago", 0.5),        # its closer
            R(0.20, 0.31, "服务管家"),                     # new author
            R(0.24, 0.31, "新帖正文第一行"),
            R(0.30, 0.31, "新帖正文第二行"),
            R(0.36, 0.31, "1 hour（s） ago", 0.5),         # new post's closer
        ]
        layout = StubLayout([(0.0, 1.0)])
        posts, _meta, tail = segment_by_regions(rows, layout)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["author"], "服务管家")
        self.assertEqual(posts[0]["text"], "新帖正文第一行新帖正文第二行")
        self.assertEqual(posts[0]["time_hint"], "1 hour（s） ago")
        first = tail["first"]
        self.assertEqual(first["texts"], ["后面还有两行收尾。"])
        self.assertEqual(first["closer"], "3 hour（s） ago")
        self.assertTrue(first["header"])  # region 0 carried a header

    def test_extract_uses_region_layout_when_given(self):
        layout = StubLayout([(0.0, 0.14), (0.14, 0.78), (0.78, 1.0)])
        items = extract_ocr_items("fake.png",
                                  ocr_rows_fn=lambda _p: REAL_EN_ROWS,
                                  layout=layout)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["author"], "Celina Cao")
        self.assertIn("_expand", items[0])

    def test_extract_screen_returns_items_and_tail(self):
        layout = StubLayout([(0.0, 0.505), (0.505, 1.0)])
        rows = [R(0.56, 0.31, "Viva"), R(0.60, 0.31, "长文")]
        items, tail = extract_ocr_screen(
            "fake.png", ocr_rows_fn=lambda _p: rows, layout=layout)
        self.assertEqual(items, [])
        self.assertEqual(tail["last"]["author"], "Viva")

    def test_extract_screen_legacy_path_has_no_tail(self):
        items, tail = extract_ocr_screen(
            "fake.png", ocr_rows_fn=lambda _p: REAL_EN_ROWS)
        self.assertEqual(len(items), 1)  # legacy state machine still works
        self.assertIsNone(tail)


# Reconstructed from data/captures/moments_20261106_140020_s0.png: a
# baseline-merged row (previous post's time row + Channels card label)
# precedes an intact 工行早餐 share post and the head of an ad post.
S0_MERGED_ROWS = [
    R(0.13, 0.31, "3 hour（s） ago Channels． 新华网", 0.50),
    R(0.23, 0.31, "沈津羽@中国工商银行", 1.00),
    R(0.32, 0.45, "【安徽分行】财经早餐", 1.00),
    R(0.34, 0.34, "ICBC", 1.00),
    R(0.42, 0.31, "4 hour（s） ago", 0.50),
    R(0.42, 0.88, "••", 0.30),
    R(0.51, 0.31, "余下1条〉", 0.50),
    R(0.61, 0.31, "安徽服务管家0127@平安产险服务管家", 1.00),
    R(0.66, 0.31, "- llw.wl 抢五折.Il.m.！", 0.30),
    R(0.70, 0.35, "准点抽500元加油礼", 0.50),
    R(0.73, 0.35, "各类爆款商品5折放送", 0.50),
]


class TestOrphanRescue(unittest.TestCase):
    def test_merged_row_does_not_swallow_following_real_post(self):
        # The low-conf merged row starts an orphan; the next row is a REAL
        # header (high conf + avatar) — the orphan must be discarded there,
        # not swallow the intact post (live 2026-11-06: 沈津羽's post was
        # ingested with the merged row as its author).
        posts, meta = segment_posts(S0_MERGED_ROWS, avatar_fn=lambda y: True)
        self.assertEqual(len(posts), 1)
        post = posts[0]
        self.assertEqual(post["author"], "沈津羽@中国工商银行")
        self.assertEqual(post["time_hint"], "4 hour（s） ago")
        self.assertTrue(post["text"].startswith("【安徽分行】财经早餐"))
        self.assertEqual(meta["dropped_no_author"], 1)
        self.assertEqual(meta["dropped_truncated"], 1)  # ad post, bottom cut

    def test_low_conf_first_row_is_orphan_even_without_probe(self):
        # conf gate alone (probe unavailable): card text at the screen top
        # must not become an author (live: "…lmwl抢五折…llmal", conf 0.30)
        rows = [
            R(0.13, 0.31, "…lmwl抢五折…llmal", 0.30),
            R(0.16, 0.34, "准点抽500元加油礼", 1.00),
            R(0.45, 0.31, "4 hour（s） ago", 0.50),
            R(0.86, 0.31, "韩玉珂", 1.00),
            R(0.94, 0.44, "这可能是美国AI最魔幻的一天。", 0.50),
        ]
        posts, meta = segment_posts(rows)  # no avatar_fn
        self.assertEqual(posts, [])  # 韩玉珂's post has no closing time row
        self.assertEqual(meta["dropped_no_author"], 1)
        self.assertEqual(meta["dropped_truncated"], 1)


class TestJoinLines(unittest.TestCase):
    def test_cjk_joins_without_space(self):
        from services.digest.ocr_extract import _join_lines
        self.assertEqual(_join_lines(["今天天气", "很好"]), "今天天气很好")

    def test_latin_joins_with_space(self):
        from services.digest.ocr_extract import _join_lines
        self.assertEqual(_join_lines(["hello", "world"]), "hello world")


class TestAuthorArrow(unittest.TestCase):
    """More-menu arrow probe (自主批阅 skip rule), calibrated on the
    091925 round: 糖K经理 arrow ink [0.890..0.909] with the name bbox
    ending 0.844; WorkBuddy's "Sponsored v" run reaches 0.920 from a
    short-name bbox; personal posts leave the zone blank; phantom
    long-text "authors" only ink inside their own bbox."""

    W, H = 882, 1600  # real capture size

    def _save(self, img, name="arrow_probe.png"):
        import tempfile
        d = tempfile.mkdtemp()
        path = os.path.join(d, name)
        img.save(path)
        self.addCleanup(lambda: os.remove(path))
        return path

    def _img(self):
        from PIL import Image
        return Image.new("RGB", (self.W, self.H), (250, 250, 250))

    def _band_y(self, ay=0.18, ah=0.025):
        return ay, ah

    def _ink(self, img, x0, x1, ay=0.18, ah=0.025):
        px = img.load()
        y0 = int((ay + 0.006) * self.H)
        y1 = int((ay + ah - 0.004) * self.H)
        for xx in range(int(x0 * self.W), int(x1 * self.W)):
            for yy in range(y0, y1):
                px[xx, yy] = (140, 140, 140)

    def test_arrow_ink_right_of_name(self):
        from services.digest.ocr_extract import author_arrow_present
        img = self._img()
        self._ink(img, 0.890, 0.909)  # 糖K经理's measured arrow cluster
        path = self._save(img)
        self.assertTrue(author_arrow_present(path, 0.18, 0.025, 0.844))

    def test_sponsored_text_run_from_short_name(self):
        from services.digest.ocr_extract import author_arrow_present
        img = self._img()
        self._ink(img, 0.799, 0.891)  # WorkBuddy's "Sponsored v" run
        path = self._save(img)
        self.assertTrue(author_arrow_present(path, 0.19, 0.029, 0.488))

    def test_blank_zone_is_personal(self):
        from services.digest.ocr_extract import author_arrow_present
        path = self._save(self._img())
        self.assertFalse(author_arrow_present(path, 0.18, 0.025, 0.844))

    def test_name_own_tail_ink_does_not_count(self):
        # phantom long-text "author": ink only inside its own bbox —
        # runs never start ARROW_GAP right of bbox right edge (s3/s16/s27)
        from services.digest.ocr_extract import author_arrow_present
        img = self._img()
        self._ink(img, 0.814, 0.873)
        path = self._save(img)
        self.assertFalse(author_arrow_present(path, 0.188, 0.027, 0.875))

    def test_screen_edge_artifact_outside_scan_band(self):
        from services.digest.ocr_extract import author_arrow_present
        img = self._img()
        self._ink(img, 0.968, 0.985)  # Gracie s2's screen-edge artifact
        path = self._save(img)
        self.assertFalse(author_arrow_present(path, 0.155, 0.021, 0.408))

    def test_unreadable_image_is_best_effort_false(self):
        from services.digest.ocr_extract import author_arrow_present
        self.assertFalse(author_arrow_present("no_such_file.png",
                                              0.18, 0.025, 0.844))

    def test_read_top_item_exposes_author_geometry_and_arrow(self):
        rows = [
            R(0.131, 0.308, "张三"),
            R(0.18, 0.31, "正文第一行"),
            R(0.46, 0.31, "3 hour（s） ago", 0.5),
        ]
        layout = StubLayout([(0.0, 0.09), (0.09, 0.5), (0.5, 1.0)])
        reading = read_top_item("x.png", ocr_rows_fn=lambda _p: rows,
                                layout=layout,
                                arrow_fn=lambda p, y, h, x2: x2 < 0.6)
        unit = reading["unit"]
        self.assertEqual(unit["author_y"], 0.131)
        self.assertAlmostEqual(unit["author_x2"], 0.308 + 0.12, places=6)
        self.assertTrue(unit["author_arrow"])

    def test_read_top_item_no_author_row_means_no_arrow(self):
        # feed-top cover residue only: the row sits above LOOSE_AUTHOR_MIN_Y,
        # so the loose rule (rightly) declines to attribute it
        rows = [R(0.10, 0.31, "封面上的残行")]
        layout = StubLayout([(0.0, 1.0)])
        reading = read_top_item("x.png", ocr_rows_fn=lambda _p: rows,
                                layout=layout)
        self.assertIsNone(reading["unit"]["author"])
        self.assertIsNone(reading["unit"]["author_y"])
        self.assertFalse(reading["unit"]["author_arrow"])


class TestExtractOcrItems(unittest.TestCase):
    def test_maps_to_vlm_item_shape(self):
        items = extract_ocr_items("fake.png", ocr_rows_fn=lambda _p: ZH_ROWS)
        self.assertEqual(len(items), 2)
        it = items[0]
        self.assertEqual(set(it.keys()),
                         {"author", "text", "images_desc", "comments", "time_hint"})
        self.assertEqual(it["author"], "张三")
        self.assertEqual(it["images_desc"], "")

    def test_vision_failure_raises(self):
        import services.digest.ocr_extract as mod
        with mock.patch.object(mod, "_vision_rows",
                               side_effect=RuntimeError("Vision OCR failed")):
            with self.assertRaises(RuntimeError):
                extract_ocr_items("fake.png")


if __name__ == "__main__":
    unittest.main()
