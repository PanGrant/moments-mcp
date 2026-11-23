import os
import sys
import tempfile
import unittest
from unittest import mock

from services.digest.collector import (
    ALIGN_TOP_OFFSET,
    MomentsWindowNotFoundError,
    ScreenLockedError,
    _merge_expanded,
    collect_moments_once,
    find_moments_window,
    run_moments_collection,
)
from services.digest import db


def _fake_quartz(windows):
    class _Q:
        kCGWindowListOptionOnScreenOnly = 1
        kCGNullWindowID = 0

        @staticmethod
        def CGWindowListCopyWindowInfo(option, window_id):
            return windows
    return _Q


# Recent builds embed the Moments feed in the main window (title 'Weixin');
# older builds open a dedicated 朋友圈 window.
EMBEDDED_BUILD = [
    {"kCGWindowNumber": 21, "kCGWindowOwnerName": "WeChat", "kCGWindowName": "Weixin",
     "kCGWindowBounds": {"X": 274, "Y": 253, "Width": 441, "Height": 561}},
    {"kCGWindowNumber": 22, "kCGWindowOwnerName": "Safari", "kCGWindowName": "GitHub",
     "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 1200, "Height": 800}},
]

DEDICATED_BUILD = EMBEDDED_BUILD + [
    {"kCGWindowNumber": 23, "kCGWindowOwnerName": "WeChat", "kCGWindowName": "朋友圈",
     "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 380, "Height": 500}},
]


def _write_fake_png(window_id, out_path):
    """Stand-in for capture_window: create a dummy PNG file."""
    with open(out_path, "wb") as f:
        f.write(b"png")
    return out_path


class TestFindMomentsWindow(unittest.TestCase):
    def test_prefers_dedicated_moments_window(self):
        with mock.patch.dict(sys.modules, {"Quartz": _fake_quartz(DEDICATED_BUILD)}):
            win = find_moments_window()
        self.assertEqual(win.window_id, 23)

    def test_falls_back_to_main_window(self):
        with mock.patch.dict(sys.modules, {"Quartz": _fake_quartz(EMBEDDED_BUILD)}):
            win = find_moments_window()
        self.assertEqual(win.window_id, 21)

    def test_none_without_wechat(self):
        empty = [{"kCGWindowNumber": 1, "kCGWindowOwnerName": "Safari",
                  "kCGWindowName": "x", "kCGWindowBounds": {}}]
        with mock.patch.dict(sys.modules, {"Quartz": _fake_quartz(empty)}):
            self.assertIsNone(find_moments_window())


def _reading(author, texts, closer=None, comments=None, expand=None,
             divider_y=None):
    """A read_item_fn return: one item read between the screen's top boundary
    and the first divider below it (divider_y None = runs past the bottom).
    author None = unattributable screen (no header row found)."""
    return {"unit": {"author": author, "texts": texts,
                     "closer": closer, "comments": comments or [],
                     "expand": expand},
            "divider_y": divider_y}


class CollectorHarness(unittest.TestCase):
    """Shared plumbing: fake window, fake captures, an injectable
    read_item_fn returning per-capture readings (last one repeats), and a
    recording scroll_px_fn."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.scrolls = []
        self.calls = {"n": 0}

    def tearDown(self):
        self.tmp.cleanup()

    def _collect(self, readings, max_screens=5, expand_fn=None,
                 accessibility=None):
        def fake_read(_path):
            i = self.calls["n"]
            self.calls["n"] += 1
            return readings[min(i, len(readings) - 1)]

        patches = [
            mock.patch.dict(sys.modules, {"Quartz": _fake_quartz(EMBEDDED_BUILD)}),
            mock.patch("services.digest.collector.capture_window",
                       _write_fake_png),
        ]
        if accessibility is not None:
            patches.append(mock.patch(
                "services.digest.collector.accessibility_trusted",
                return_value=accessibility))
        import contextlib
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            info = collect_moments_once(
                out_dir=self.tmp.name, read_item_fn=fake_read,
                scroll_px_fn=lambda px: self.scrolls.append(px),
                expand_fn=expand_fn, max_screens=max_screens)
        return info


class TestTallItemCrossScreen(CollectorHarness):
    """An item taller than the viewport opens on one screen (header + no
    bottom divider), continues across full-screen scrolls, and seals on its
    closing time row — at most MAX_POST_SCREENS screens (user rule
    2026-11-06/07)."""

    def test_tall_item_continues_and_closes_with_closer(self):
        head = _reading("Viva", ["一个年轻客户问我"])
        cont = _reading(None, ["还做个表比半天"],
                        closer="4 hour（s） ago",
                        comments=[{"author": "小明", "text": "赞"}])
        info = self._collect([head, cont], max_screens=4)
        by_author = {r["author"]: r for r in info["raw_items"]}
        self.assertIn("Viva", by_author)
        viva = by_author["Viva"]
        self.assertEqual(viva["text"], "一个年轻客户问我\n还做个表比半天")
        self.assertEqual(viva["posted_at"], "4 hour（s） ago")
        self.assertEqual(viva["thread"], [{"author": "小明", "text": "赞"}])
        # the identical-repeat stop fired on the 3rd capture: 2 scrolls
        self.assertEqual(len(info["screens"]), 3)
        self.assertEqual(len(self.scrolls), 2)

    def test_three_screen_cap_force_finishes_without_consuming_screen4(self):
        # the cap counts the opening screen: 第一段 (s0) 第二段 (s1) 第三段
        # (s2) — s3's rows belong to the next part and must NOT leak in
        # (2026-11-07: the cap check ran after consumption and let a 4th
        # screen of text into the item).
        readings = [
            _reading("Viva", ["第一段"]),
            _reading(None, ["第二段"]),
            _reading(None, ["第三段"]),
            _reading(None, ["第四段"]),
        ]
        info = self._collect(readings, max_screens=4)
        viva = [r for r in info["raw_items"] if r["author"] == "Viva"]
        self.assertEqual(len(viva), 1)
        self.assertEqual(viva[0]["text"], "第一段\n第二段\n第三段")
        self.assertNotIn("第四段", viva[0]["text"])
        self.assertEqual(viva[0]["posted_at"], "")  # force-finished, no closer

    def test_divider_bounds_the_open_post_even_without_closer(self):
        # A divider ends the item on the next screen but the time row was
        # unreadable — close there; nothing below the divider can leak in.
        readings = [
            _reading("Viva", ["第一段"]),
            _reading(None, ["第二段"], divider_y=0.15),
        ]
        info = self._collect(readings, max_screens=4)
        by_author = {r["author"]: r for r in info["raw_items"]}
        self.assertEqual(by_author["Viva"]["text"], "第一段\n第二段")
        self.assertEqual(by_author["Viva"]["posted_at"], "")
        # the closing advance aligned the divider into the next screen's top
        self.assertAlmostEqual(self.scrolls[-1],
                               -(0.15 - ALIGN_TOP_OFFSET) * 561)

    def test_foreign_overshoot_drops_the_partial_and_reads_fresh(self):
        # The scroll overshot the tall item's end: the next capture shows
        # ANOTHER author's complete post. The partial must not absorb the
        # foreign text/time row — it is dropped (re-read whole next round)
        # and the overshoot screen is ingested as its own item.
        head = _reading("Viva", ["长文的开头一行"])
        foreign = _reading("路人甲", ["外帖的尾巴一行", "外帖的又一行"],
                           closer="18 minute（s） ago",
                           comments=[{"author": "路人", "text": "赞"}],
                           divider_y=0.6)
        info = self._collect([head, foreign], max_screens=2)
        by_author = {r["author"]: r for r in info["raw_items"]}
        self.assertNotIn("Viva", by_author)  # partial dropped, not mis-sealed
        self.assertIn("路人甲", by_author)
        self.assertEqual(by_author["路人甲"]["posted_at"], "18 minute（s） ago")
        self.assertEqual(by_author["路人甲"]["thread"], [{"author": "路人", "text": "赞"}])

    def test_unbounded_midround_reading_continues_never_opens_fresh(self):
        # Loose-attribution guard (user model, 2026-11-08): on an UNbounded
        # mid-round screen the loose rule can park a deep tail row as a
        # phantom "author". That reading may only CONTINUE the open item —
        # everything merges in y-order (rows above the phantom, the phantom
        # row itself, rows below) and no fresh post is ingested from it.
        head = _reading("Viva", ["第一段"])
        phantom = _reading("第四段", ["第五段"], closer="4 hour（s） ago")
        phantom["unit"]["pre_texts"] = ["第二段", "第三段"]
        phantom["bounded"] = False
        info = self._collect([head, phantom], max_screens=3)
        self.assertEqual(len(info["raw_items"]), 1)  # no phantom post
        viva = info["raw_items"][0]
        self.assertEqual(viva["author"], "Viva")
        self.assertEqual(viva["text"],
                         "第一段\n第二段第三段第四段第五段")
        self.assertEqual(viva["posted_at"], "4 hour（s） ago")

    def test_unbounded_first_screen_still_opens_feed_top_post(self):
        # The flip side of the guard: round index 0 IS the feed-top page —
        # its unbounded reading must still open post 1 (loose attribution).
        first = _reading("Catherine凯瑟琳", ["正文一行"],
                         closer="15 minute（s） ago", divider_y=0.95)
        first["bounded"] = False
        info = self._collect([first], max_screens=1)
        self.assertEqual(len(info["raw_items"]), 1)
        self.assertEqual(info["raw_items"][0]["author"], "Catherine凯瑟琳")


class TestCollectMomentsOnce(CollectorHarness):
    def test_collect_maps_read_item(self):
        complete = _reading("张三", ["今天天气好"], closer="1小时前",
                            divider_y=0.5)
        info = self._collect([complete], max_screens=3)
        self.assertEqual(info["window"].window_id, 21)
        self.assertTrue(os.path.exists(info["image_path"]))
        raw = info["raw_items"][0]
        self.assertEqual(raw["source"], "moments")
        self.assertEqual(raw["author"], "张三")
        self.assertEqual(raw["posted_at"], "1小时前")
        import re as _re
        self.assertTrue(_re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$",
                                  raw["posted_at_approx"]), raw["posted_at_approx"])
        # identical repeat on the next capture → stop after the 2nd screen
        self.assertEqual(len(info["screens"]), 2)
        # the advance landed the bottom divider ALIGN_TOP_OFFSET into the
        # next screen's top (user's "滚动到下分隔线顶部几个 pixel")
        self.assertEqual(len(self.scrolls), 1)
        self.assertAlmostEqual(self.scrolls[0],
                               -(0.5 - ALIGN_TOP_OFFSET) * 561)

    def test_unattributable_first_screen_keeps_scrolling(self):
        # Top of feed = profile cover (no attributable header, no divider) →
        # nothing to ingest; the round must continue scrolling.
        cover = _reading(None, ["某人的封面残行"])
        complete = _reading("张三", ["今天天气好"], closer="1小时前",
                            divider_y=0.5)
        info = self._collect([cover, complete], max_screens=4)
        self.assertEqual(len(info["screens"]), 3)
        self.assertEqual(self.scrolls[0], -561.0)  # no divider → full screen
        self.assertEqual(len(info["raw_items"]), 1)

    def test_progressing_unattributable_screens_keep_scrolling(self):
        # Screens with no attributable item but CHANGING rows mean the feed
        # is advancing — never a stop condition (live 2026-11-07 lesson).
        readings = [_reading(None, [f"残行{c}"], divider_y=0.2)
                    for c in "一二三四五"]
        info = self._collect(readings, max_screens=5)
        self.assertEqual(len(info["screens"]), 5)
        self.assertEqual(len(self.scrolls), 4)
        self.assertEqual(info["warnings"], [])

    def test_identical_screens_stop_as_scroll_failure(self):
        # Two captures with the same reading = the scroll had no effect
        # (feed bottom / blocked) — stop.
        same = _reading(None, ["余下2条〉"])
        info = self._collect([same], max_screens=5)
        self.assertEqual(len(info["screens"]), 2)
        self.assertEqual(len(self.scrolls), 1)

    def test_expands_folded_posts_before_ingest(self):
        folded = _reading("张三", ["这是被折叠的短文"], closer="1小时前",
                          expand={"cx": 0.5, "cy": 0.4}, divider_y=0.5)
        expanded = _reading("张三", ["这是被折叠的短文" + "展开" * 100],
                            closer="1小时前", divider_y=0.5)
        clicks = []

        def fake_expand(t):
            clicks.append(t)

        info = self._collect([folded, expanded], max_screens=3,
                             expand_fn=fake_expand)
        self.assertEqual(clicks, [{"cx": 0.5, "cy": 0.4}])
        self.assertEqual(info["screens"][0]["expanded"], 1)
        raw = info["raw_items"][0]
        self.assertEqual(raw["text"], "这是被折叠的短文" + "展开" * 100)
        # relative hint → absolute approximate time (minute precision)
        import re as _re
        self.assertTrue(_re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$",
                                  raw["posted_at_approx"]), raw["posted_at_approx"])

    def test_expansion_reflow_losing_closer_keeps_pre_expansion_item(self):
        # The expanded reflow can drop the closing time row; the merged
        # reading must not lose the time — the pre-expansion reading wins.
        folded = _reading("张三", ["短文开头…被折叠的部分看不到"],
                          closer="1小时前",
                          expand={"cx": 0.5, "cy": 0.4}, divider_y=0.5)
        expanded = _reading("张三",
                            ["短文开头…被折叠的部分看不到现在全文都在了"],
                            closer=None, divider_y=0.5)

        def fake_expand(_t):
            pass

        info = self._collect([folded, expanded], max_screens=3,
                             expand_fn=fake_expand)
        raw = info["raw_items"][0]
        # kept the closed reading at s0; the next capture's variant then
        # collapses in with the longer full text while keeping the time
        self.assertEqual(raw["posted_at"], "1小时前")
        self.assertEqual(raw["text"],
                         "短文开头…被折叠的部分看不到现在全文都在了")

    def test_unbounded_expansion_continues_as_tall_item(self):
        # P1 (live 2026-11-07 Lianchang): when the expanded post runs past
        # the screen bottom, the folded closer is stale — adopt the expanded
        # text and continue across screens; the real closer is found on a
        # continuation screen. The old guard swapped the ~400-char essay
        # body for the 98-char folded head and then aligned to the stale
        # folded divider, wasting two screens mid-essay.
        folded = _reading("Viva", ["长文的开头"], closer="2 hours ago",
                          expand={"cx": 0.5, "cy": 0.4}, divider_y=0.5)
        expanded = _reading("Viva",
                            ["长文的开头", "展开后第一段", "展开后第二段"],
                            closer=None, divider_y=None)
        cont = _reading(None, ["展开后第二段", "新的一段"],
                        closer="2 hours ago")

        def fake_expand(_t):
            pass

        info = self._collect([folded, expanded, cont], max_screens=3,
                             expand_fn=fake_expand)
        rows = [r for r in info["raw_items"] if r["author"] == "Viva"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"],
                         "长文的开头展开后第一段展开后第二段\n新的一段")
        self.assertEqual(rows[0]["posted_at"], "2 hours ago")
        # rows already on the expanded screen must not be re-consumed
        self.assertEqual(rows[0]["text"].count("展开后第二段"), 1)
        # the advance after an unbounded expansion is a FULL screen (the
        # folded divider position is stale)
        self.assertEqual(self.scrolls[0], -561.0)

    def test_unbounded_expansion_stale_closer_falls_back(self):
        # If the continuation never finds a readable closer, the folded
        # reading's real time row still closes the item at its divider.
        folded = _reading("Viva", ["长文的开头"], closer="5 hours ago",
                          expand={"cx": 0.5, "cy": 0.4}, divider_y=0.5)
        expanded = _reading("Viva", ["长文的开头", "展开后第一段"],
                            closer=None, divider_y=None)
        cont = _reading(None, ["续行"], closer=None, divider_y=0.4)

        def fake_expand(_t):
            pass

        info = self._collect([folded, expanded, cont], max_screens=3,
                             expand_fn=fake_expand)
        rows = [r for r in info["raw_items"] if r["author"] == "Viva"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "长文的开头展开后第一段\n续行")
        self.assertEqual(rows[0]["posted_at"], "5 hours ago")
        # the closing advance aligned the continuation screen's divider
        self.assertAlmostEqual(self.scrolls[-1],
                               -(0.4 - ALIGN_TOP_OFFSET) * 561)

    def test_locked_screen_raises_screen_locked_error(self):
        with mock.patch.dict(sys.modules, {"Quartz": _fake_quartz(EMBEDDED_BUILD)}), \
             mock.patch("services.digest.collector.screen_is_locked",
                        return_value=True):
            with self.assertRaises(ScreenLockedError):
                collect_moments_once(out_dir=self.tmp.name)

    def test_midround_screen_lock_raises_screen_locked_error(self):
        # Screen can lock itself between the pre-check and a mid-round capture.
        # scroll_px_fn is explicit: a None scroll_fn would trigger the real
        # accessibility probe, whose pyobjc import breaks under the faked
        # sys.modules Quartz (poisons every later pyobjc import in the run).
        def fail_like_lock(window_id, out_path):
            raise RuntimeError(
                "screencapture failed (exit 1): could not create image from window")

        with mock.patch.dict(sys.modules, {"Quartz": _fake_quartz(EMBEDDED_BUILD)}), \
             mock.patch("services.digest.collector.capture_window",
                        side_effect=fail_like_lock):
            with self.assertRaises(ScreenLockedError):
                collect_moments_once(out_dir=self.tmp.name,
                                     scroll_px_fn=lambda px: None)

    def test_raises_when_window_missing(self):
        with mock.patch.dict(sys.modules, {"Quartz": _fake_quartz([])}):
            with self.assertRaises(MomentsWindowNotFoundError):
                collect_moments_once(out_dir=self.tmp.name,
                                     scroll_px_fn=lambda px: None)


class TestMergeExpanded(unittest.TestCase):
    def test_expanded_version_replaces_matching_pre_item(self):
        pre = [{"author": "A", "text": "一二三四五六七八九十被折叠了",
                "time_hint": "1小时前"}]
        post = [{"author": "A", "text": "一二三四五六七八九十被折叠的部分现在全部展开了",
                 "time_hint": "1小时前"}]
        merged = _merge_expanded(pre, post)
        self.assertEqual(merged, [post[0]])

    def test_unmatched_pre_items_are_kept(self):
        pre = [{"author": "A", "text": "展开后我超出了视口的时间行",
                "time_hint": "2小时前"}]
        post = [{"author": "B", "text": "完全无关的另一帖",
                 "time_hint": "3小时前"}]
        merged = _merge_expanded(pre, post)
        self.assertEqual(merged, [pre[0], post[0]])

    def test_chrome_fullwidth_normalization_in_prefix(self):
        # prefix matching runs through normalize_for_match (fullwidth etc.)
        pre = [{"author": "A", "text": "测试（２）前缀",
                "time_hint": "1小时前"}]
        post = [{"author": "A", "text": "测试（２）前缀后续全文",
                 "time_hint": "1小时前"}]
        self.assertEqual(_merge_expanded(pre, post), [post[0]])


class TestVariantCollapse(CollectorHarness):
    """The same item can be read twice in a round (mis-aligned retry, an
    expansion reflow's follow-up capture) with OCR jitter; the variant
    collapse must store one row per item, merging complementary fields."""

    # long enough that the jitter suffix sits past the 30-char prefix cut
    _BASE = "这是一条会在两屏之间重复出现的帖子内容需要足够长以超过前缀长度三十个字"

    def _closed_reading(self, author, text, hint="1 hour（s） ago"):
        return _reading(author, [text], closer=hint, divider_y=0.5)

    def test_repeat_variant_stores_one_row(self):
        s1 = self._closed_reading("张三", self._BASE + "变体甲")
        s2 = self._closed_reading("张三", self._BASE + "变体。乙")
        info = self._collect([s1, s2], max_screens=2)
        rows = [r for r in info["raw_items"] if r["author"] == "张三"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], self._BASE + "变体。乙")  # longer won

    def test_merge_takes_longer_text_and_any_time_row(self):
        # variants merge complementary fields: the longer text wins (a
        # stitched tall post beats a folded pre-expansion head) and the
        # time row from ANY variant closes the post (the expanded reflow
        # often pushes the closer off-screen — live 2026-11-07)
        s1 = self._closed_reading(
            "李四", self._BASE + "加上更多没读完的续行内容", hint="")
        s2 = self._closed_reading("李四", self._BASE, hint="2 hours ago")
        info = self._collect([s1, s2], max_screens=2)
        rows = [r for r in info["raw_items"] if r["author"] == "李四"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["posted_at"], "2 hours ago")
        self.assertEqual(rows[0]["text"], self._BASE + "加上更多没读完的续行内容")

    def test_head_jitter_variants_collapse_by_edit_distance(self):
        # Live 2026-11-07 服务管家: the same post read twice, one reading
        # gaining a stray head char ('添立冬至…' vs '立冬至…') — neither
        # reading is a prefix of the other, so the bounded edit-distance
        # tier (same author, ≥12-char key) collapses them.
        base = "立冬至，注意添衣保暖这3种取暖模式需要反着来速戳安稳过冬立冬了"
        s1 = self._closed_reading("安徽服务管家0127", "添" + base,
                                  hint="2 hour（s） ago")
        s2 = self._closed_reading("安徽服务管家0127", base,
                                  hint="2 hour（s） ago")
        info = self._collect([s1, s2], max_screens=2)
        rows = [r for r in info["raw_items"]
                if r["author"] == "安徽服务管家0127"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "添" + base)  # longer reading wins

    def test_different_short_posts_by_same_author_stay_separate(self):
        # the loose tiers are floored: two genuinely different short posts
        # (no prefix relation, keys under 12 chars) must both survive
        s1 = self._closed_reading("阿明", "贴冬膘～", hint="3 hour（s） ago")
        s2 = self._closed_reading("阿明", "今晚吃火锅", hint="1 hour（s） ago")
        info = self._collect([s1, s2], max_screens=2)
        rows = [r for r in info["raw_items"] if r["author"] == "阿明"]
        self.assertEqual(len(rows), 2)

    def test_pkey_ignores_whitespace_so_spacing_variants_collapse(self):
        # live 2026-11-07: '| 这3种' vs '|这3种' — a space jitter inside the
        # prefix must not defeat the collapse
        s1 = self._closed_reading(
            "服务管家",
            "立冬至,注意添衣保暖| 这3种「取暖模式」需要反着来?速戳安稳过冬",
            hint="3 hours ago")
        s2 = self._closed_reading(
            "服务管家",
            "立冬至,注意添衣保暖|这3种「取暖模式」需要反着来?速戳安稳过冬",
            hint="3 hours ago")
        info = self._collect([s1, s2], max_screens=2)
        rows = [r for r in info["raw_items"] if r["author"] == "服务管家"]
        self.assertEqual(len(rows), 1)

    def test_shorter_variant_still_collapses_as_prefix(self):
        # live 2026-11-07: a force-finished reading holds the full text; a
        # later variant holds only the re-read head — shorter than the
        # 30-char pkey cut. The prefix match must run both ways.
        s1 = self._closed_reading(
            "服务管家",
            "立冬至,注意添衣保暖|这3种「取暖模式」需要反着来?速戳安稳过冬" + "后面被粘进来的长尾内容" * 20,
            hint="")
        s2 = self._closed_reading(
            "服务管家",
            "立冬至,注意添衣保暖|这3种「取暖模式」需要反着来",
            hint="3 hours ago")
        info = self._collect([s1, s2], max_screens=2)
        rows = [r for r in info["raw_items"] if r["author"] == "服务管家"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["posted_at"], "3 hours ago")


class TestRunMomentsCollection(unittest.TestCase):
    def test_end_to_end_with_fakes(self):
        tmp = tempfile.TemporaryDirectory()
        conn = db.connect(os.path.join(tmp.name, "digest.db"))
        try:
            def summarize(raw, interests):
                return {"title": "天气", "summary": "张三说天气好",
                        "matched_interest_ids": [], "score": 1}

            def fake_read(_path):
                return _reading("张三", ["今天天气好"], closer="1小时前",
                                divider_y=0.5)

            # no scroll permission in the fake env → single-screen round
            with mock.patch.dict(sys.modules, {"Quartz": _fake_quartz(EMBEDDED_BUILD)}), \
                 mock.patch("services.digest.collector.capture_window",
                            _write_fake_png), \
                 mock.patch("services.digest.collector.read_top_item", fake_read), \
                 mock.patch("services.digest.collector.accessibility_trusted",
                            return_value=False):
                info = run_moments_collection(conn, out_dir=tmp.name,
                                              summarize_fn=summarize)
            self.assertEqual(info["stats"]["summarized"], 1)
            items = db.list_digest_items(conn)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["title"], "天气")
            self.assertEqual(items[0]["author"], "张三")
        finally:
            conn.close()
            tmp.cleanup()


class TestSponsoredFlag(CollectorHarness):
    """自主批阅 skip rule: a reading whose unit carries author_arrow=True
    (the dropdown-arrow pixel probe) ingests as sponsored=1 — through both
    the fresh-item path and the tall-item cross-screen merge."""

    def test_fresh_item_carries_sponsored_flag(self):
        head = _reading("糖K经理", ["你好，在上班"], closer="2 hour（s） ago")
        head["unit"]["author_arrow"] = True
        info = self._collect([head], max_screens=2)
        self.assertEqual(len(info["raw_items"]), 1)
        self.assertEqual(info["raw_items"][0]["sponsored"], 1)

    def test_personal_item_defaults_to_zero(self):
        head = _reading("Gracie", ["周末去爬山"], closer="5 hour（s） ago")
        info = self._collect([head], max_screens=2)
        self.assertEqual(info["raw_items"][0]["sponsored"], 0)

    def test_tall_item_keeps_flag_across_screens(self):
        head = _reading("WorkBuddy", ["WORK BUDDY 帮你理清逻辑"])
        head["unit"]["author_arrow"] = True
        cont = _reading(None, ["列出选项与利弊"], closer="4 hour（s） ago")
        info = self._collect([head, cont], max_screens=4)
        wb = [r for r in info["raw_items"] if r["author"] == "WorkBuddy"]
        self.assertEqual(len(wb), 1)
        self.assertEqual(wb[0]["sponsored"], 1)
        self.assertIn("列出选项与利弊", wb[0]["text"])


if __name__ == "__main__":
    unittest.main()
