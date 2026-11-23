import os
import tempfile
import unittest
from unittest import mock

from PIL import Image

from services.digest import feed_layout
from services.digest.feed_layout import avatar_blocks
from services.digest.post_schema import FeedSchema, find_author, scan_screen


def R(y, x, text, conf=1.0, w=0.12, h=0.015):
    return {"y": y, "x": x, "w": w, "h": h, "conf": conf, "text": text}


def stub_rows_fn(rows):
    return lambda path: rows


def arrow_true(path, author_y, author_h, author_x2):
    return True


def arrow_false(path, author_y, author_h, author_x2):
    return False


def make_photo_fn(blocks):
    """Fixture-declared avatar photo blocks ([y0, y1] spans). The stub
    mirrors the real avatar_blocks' windowing: a block is only returned
    when it falls INSIDE the scanned window (blocks never straddle a
    divider — avatars render below their own divider)."""
    def photo_fn(path, x0, x1, y_top, y_bot):
        return [(a, b) for (a, b) in blocks
                if a >= y_top and b <= y_bot]
    return photo_fn


# Screen: region 0 = a head-cut post's tail (2 rows, no photo → fragment);
# region 1 = a complete post (张三 + avatar photo + body + expand link +
# time row + comments); region 2 = rows with NO photo → continuation of
# the open post (大循环: 找不到线看照片，无照片即上一帖的延续).
ROWS_TAIL_PLUS_POST = [
    R(0.04, 0.49, "Moments"),                      # chrome, filtered
    R(0.10, 0.30, "上一帖的尾行内容足够长可以当锚", 0.40),
    R(0.20, 0.30, "上一帖的另一行也是长文本锚点哦", 0.40),
    R(0.30, 0.30, "张三"),
    R(0.34, 0.30, "今天天气真不错适合出去走走运动", 0.6),
    R(0.38, 0.30, "公园里人不多风景也很好心情舒畅", 0.6),
    R(0.42, 0.31, "全文", 0.5),
    R(0.46, 0.30, "5 hour（s） ago", 0.5),
    R(0.50, 0.31, "♡ 李四，王五", 0.5),
    R(0.54, 0.38, "李四：写得真好，收藏了", 0.6),
    R(0.60, 0.38, "王五：同感，这条评论值得反复读", 0.6),
    R(0.66, 0.38, "这条评论的换行续接内容也很长锚", 0.6),
]
ROWS_TAIL_PLUS_POST_PHOTOS = [(0.288, 0.325)]      # 张三's avatar


class TestScanScreen(unittest.TestCase):
    def scan(self, rows, regions, arrow_fn=arrow_false, photos=(),
             schema=None):
        return scan_screen(
            "cap.png", schema=schema, ocr_rows_fn=stub_rows_fn(rows),
            layout=mock.Mock(regions=regions), arrow_fn=arrow_fn,
            photo_fn=make_photo_fn(photos))

    def test_posts_sequence_and_viewport(self):
        out = self.scan(ROWS_TAIL_PLUS_POST,
                        [(0.0, 0.28), (0.28, 0.56), (0.56, 1.0)],
                        photos=ROWS_TAIL_PLUS_POST_PHOTOS)
        self.assertIsNotNone(out)
        posts = out["posts"]
        self.assertEqual(len(posts), 2)

        # post 0: head-cut fragment — the previous post's tail (no photo
        # in region 0 → no doorplate → honest fragment, author None)
        p0 = posts[0]
        self.assertIsNone(p0["author"])
        self.assertIsNone(p0["author_source"])
        self.assertFalse(p0["doorplate_visible"])
        self.assertAlmostEqual(p0["top_y"], 0.0)
        self.assertAlmostEqual(p0["bottom_y"], 0.28)
        self.assertEqual(p0["body"]["texts"],
                         ["上一帖的尾行内容足够长可以当锚",
                          "上一帖的另一行也是长文本锚点哦"])
        self.assertAlmostEqual(p0["content_bottom_y"], 0.215)

        # post 1: photo-backed doorplate 张三; the photo-less region 2
        # merges into HIS post (continuation) — its rows extend his
        # comment zone across the divider cut
        p1 = posts[1]
        self.assertEqual(p1["author"], "张三")
        self.assertEqual(p1["author_source"], "photo")
        self.assertTrue(p1["doorplate_visible"])
        self.assertAlmostEqual(p1["top_y"], 0.30)
        self.assertAlmostEqual(p1["author_y"], 0.30)
        self.assertEqual(p1["body"]["texts"],
                         ["今天天气真不错适合出去走走运动",
                          "公园里人不多风景也很好心情舒畅"])
        self.assertAlmostEqual(p1["expand_y"], 0.4275)
        self.assertEqual(p1["time_row"]["text"], "5 hour（s） ago")
        self.assertAlmostEqual(p1["bottom_y"], 1.0)
        self.assertEqual([it["author"] for it in p1["comments"]["items"]],
                         ["李四", "王五"])
        self.assertEqual(p1["comments"]["items"][1]["text"],
                         "同感，这条评论值得反复读 这条评论的换行续接内容也很长锚")
        self.assertAlmostEqual(p1["content_bottom_y"], 0.675)

        # viewport: mid y=0.5 falls in post 1's span
        vp = out["viewport"]
        self.assertEqual(vp["current_post"], 1)
        self.assertTrue(vp["head_visible"])
        self.assertEqual(vp["top"], {"post": 0, "zone": "body"})
        self.assertEqual(vp["mid_zone"], "comments")
        self.assertAlmostEqual(vp["post_bottom_y"], 0.675)

    def test_page_bottom_closer_slot(self):
        # 落屏 schema（2026-11-16 深夜，用户定）：翻后屏底必收束于某帖
        # 时间行（左，与 •• 评论钮同行）或评论结尾；屏上最后一个时间行
        # 候选=屏底帖的 closer。48 盲走真实形状（l44 截图）：整屏无分隔
        # 线——尾行+closer+评论同在一个 region，模型全丢（author None），
        # 槽照填——边界判定靠槽，不靠帖子归属。
        rows = [
            R(0.04, 0.49, "Moments"),
            R(0.14, 0.30, "上一帖的尾行内容足够长可以当锚", 0.40),
            R(0.20, 0.30, "上一帖的另一行也是长文本锚点哦", 0.40),
            R(0.46, 0.30, "Yesterday at 08:12", 0.5),
            R(0.54, 0.38, "♡ 李四，王五", 0.5),
            R(0.60, 0.38, "李四：写得真好，收藏了", 0.6),
        ]
        out = self.scan(rows, [(0.0, 1.0)])
        self.assertIsNotNone(out)
        self.assertTrue(all(p["author"] is None for p in out["posts"]))
        self.assertEqual(out["page_bottom_closer"],
                         {"text": "Yesterday at 08:12", "y": 0.46})

        # 屏上一个时间行都没有 → 槽=None（诚实感知失败，不判定）
        rows2 = [r for r in rows if "ago" not in r["text"]
                 and "Yesterday" not in r["text"]]
        out2 = self.scan(rows2, [(0.0, 1.0)])
        self.assertIsNone(out2["page_bottom_closer"])

    def test_expand_slots(self):
        # the Show More row ('全文') fills BOTH expand slots: center of the
        # row (x+w/2, y+h/2). Head-cut fragments never have one (their
        # text tail has no Show More of their own).
        out = self.scan(ROWS_TAIL_PLUS_POST,
                        [(0.0, 0.28), (0.28, 0.56), (0.56, 1.0)],
                        photos=ROWS_TAIL_PLUS_POST_PHOTOS)
        p0, p1 = out["posts"]
        self.assertAlmostEqual(p1["expand_x"], 0.37)   # 0.31 + 0.12/2
        self.assertAlmostEqual(p1["expand_y"], 0.4275)  # 0.42 + 0.015/2
        self.assertIsNone(p0["expand_x"])
        self.assertIsNone(p0["expand_y"])

        # a post without a 全文 row keeps both slots None
        rows = [r for r in ROWS_TAIL_PLUS_POST if r["text"] != "全文"]
        out2 = self.scan(rows, [(0.0, 0.28), (0.28, 0.56), (0.56, 1.0)],
                         photos=ROWS_TAIL_PLUS_POST_PHOTOS)
        self.assertIsNone(out2["posts"][1]["expand_x"])
        self.assertIsNone(out2["posts"][1]["expand_y"])

    def test_screen_top_doorplate_needs_photo(self):
        # live 'Lianchang 侯哥'@0.107: a doorplate genuinely at the screen
        # top claims ONLY with a photo beside it (schema rule 2); without
        # the photo the same rows are an honest head-cut fragment
        rows = [
            R(0.039, 0.495, "Moments"),
            R(0.107, 0.267, "Lianchang 侯哥", 1.0),
            R(0.150, 0.267, "今天山里的雾气散得比想象中要晚一些", 1.0),
            R(0.420, 0.267, "5 hour（s） ago", 1.0),
        ]
        ok = self.scan(rows, [(0.0, 1.0)], photos=[(0.095, 0.13)])
        p0 = ok["posts"][0]
        self.assertEqual(p0["author"], "Lianchang 侯哥")
        self.assertEqual(p0["author_source"], "photo")
        self.assertTrue(p0["doorplate_visible"])
        self.assertAlmostEqual(p0["author_y"], 0.107)
        self.assertEqual(p0["time_row"]["text"], "5 hour（s） ago")

        flat = self.scan(rows, [(0.0, 1.0)])
        p0 = flat["posts"][0]
        self.assertIsNone(p0["author"])
        self.assertFalse(p0["doorplate_visible"])

    def test_screen_top_body_row_is_never_an_author(self):
        # the 7-phantom shape ('来了。' / '更这样…' / '样子。'): body text
        # at the screen top with NO photo in the photo column — the old
        # text rules needed cross-page carry to deny this; the photo rule
        # denies it from this capture alone
        rows = [
            R(0.039, 0.495, "Moments"),
            R(0.102, 0.308, "来了。", 1.0),
            R(0.180, 0.308, "上一帖正文的另一行内容也足够长锚点", 1.0),
            R(0.393, 0.306, "1 hour（s） ago", 1.0),
        ]
        out = self.scan(rows, [(0.0, 1.0)])
        p0 = out["posts"][0]
        self.assertIsNone(p0["author"])
        self.assertIsNone(p0["author_source"])
        self.assertFalse(p0["doorplate_visible"])
        self.assertEqual(p0["time_row"]["text"], "1 hour（s） ago")

    def test_photo_without_name_row_is_honest(self):
        # avatar present but the topmost row beside it is a long body row
        # (the name row OCR-missed): no name is read, and the body row is
        # NEVER promoted to author
        rows = [
            R(0.039, 0.495, "Moments"),
            R(0.150, 0.267, "这一行的正文很长远远超过了作者名的长度限制", 1.0),
            R(0.420, 0.267, "3 hour（s） ago", 1.0),
        ]
        out = self.scan(rows, [(0.0, 1.0)], photos=[(0.095, 0.13)])
        p0 = out["posts"][0]
        self.assertIsNone(p0["author"])
        self.assertFalse(p0["doorplate_visible"])
        self.assertEqual(p0["time_row"]["text"], "3 hour（s） ago")

    def test_photo_shorter_than_body_line_is_not_a_doorplate(self):
        # live '受邀参加' capture (moments_20261108_091925_s27): a real
        # white-background avatar sits in the photo column, but the post's
        # name row scrolled off screen top and the body's first line
        # stands beside the avatar (rows/heights = measured OCR values).
        # The follow-up line presses on at +0.0446 — INSIDE the keep
        # clearance range, so clearance passes; what kills the claim is
        # stature: the avatar's ink (0.030) is SHORTER than the body line
        # beside it (0.032) — no picture is shorter than the name line.
        rows = [
            R(0.039, 0.495, "Moments"),
            R(0.102, 0.308, "受邀参加四省边际公共安全科创交流会，最", 1.0,
              h=0.032),
            R(0.137, 0.308, "大感受：警务无人装备不再拼参数、拼噱", 1.0,
              h=0.030),
            R(0.420, 0.306, "2 hour（s） ago", 1.0),
        ]
        out = self.scan(rows, [(0.0, 1.0)], photos=[(0.093, 0.123)])
        p0 = out["posts"][0]
        self.assertIsNone(p0["author"])
        self.assertFalse(p0["doorplate_visible"])
        self.assertEqual(p0["time_row"]["text"], "2 hour（s） ago")

    def test_name_far_below_block_top_is_not_a_name(self):
        # '立冬至' shape: the stand-in row starts +0.014 below the ink
        # top — outside the hug band (keep max +0.011) → no claim
        rows = [
            R(0.039, 0.495, "Moments"),
            R(0.107, 0.308, "立冬至，注意添衣保暖|这3种「取暖模式", 1.0),
            R(0.420, 0.306, "2 hour（s） ago", 1.0),
        ]
        out = self.scan(rows, [(0.0, 1.0)], photos=[(0.093, 0.128)])
        self.assertIsNone(out["posts"][0]["author"])

    def test_name_scrolled_off_text_post_never_claims_body(self):
        # live '很多年前' capture (moments_20261108_091925_s3): same
        # name-scrolled-off shape — the photo sliver's ink (0.018) is
        # far shorter than the body line beside it (0.030, ratio 0.59);
        # its follow-up line at +0.032 also slips the clearance band, so
        # stature is the deciding rule here too (measured rows)
        rows = [
            R(0.039, 0.495, "Moments"),
            R(0.091, 0.308, "很多年前第一次读到这句诗，就一直觉得这", 1.0,
              h=0.030),
            R(0.125, 0.308, "是中国所有古诗词里最惨的一句。", 1.0, h=0.030),
            R(0.420, 0.306, "1 hour（s） ago", 1.0),
        ]
        out = self.scan(rows, [(0.0, 1.0)], photos=[(0.093, 0.111)])
        self.assertIsNone(out["posts"][0]["author"])

    def test_thinnest_real_photo_still_claims_its_name(self):
        # the corpus's thinnest real doorplate (keep min ratio 1.38):
        # photo ink 0.0321 beside a name line 0.0233 — stature passes,
        # and the body row at the bulk clearance (+0.050) never blocks
        rows = [
            R(0.039, 0.495, "Moments"),
            R(0.100, 0.308, "安徽服务管家0127", 1.0, h=0.023),
            R(0.143, 0.308, "平安产险服务管家每天为您播报今日资讯", 1.0,
              h=0.030),
            R(0.420, 0.306, "1 hour（s） ago", 1.0),
        ]
        out = self.scan(rows, [(0.0, 1.0)], photos=[(0.093, 0.125)])
        p0 = out["posts"][0]
        self.assertEqual(p0["author"], "安徽服务管家0127")
        self.assertTrue(p0["doorplate_visible"])

    def test_missed_divider_two_photos_in_one_region(self):
        # a missed divider leaves TWO posts in one region: every avatar
        # photo starts a post (the photo compensates the missing line).
        # Rows above the second photo stay with the open post's tail.
        rows = [
            R(0.30, 0.30, "张三"),
            R(0.34, 0.30, "上一帖的正文内容足够长可以当锚点", 0.6),
            R(0.40, 0.30, "1 hour（s） ago", 0.5),
            R(0.44, 0.38, "李四：写得真好，收藏了", 0.6),
            R(0.60, 0.38, "王五：这条评论的换行续接内容也长", 0.6),
            R(0.70, 0.30, "王五"),
            R(0.74, 0.30, "下一帖的正文行内容足够长可以锚定", 0.6),
            R(0.80, 0.30, "2 hour（s） ago", 0.5),
        ]
        out = self.scan(rows, [(0.0, 0.56), (0.56, 1.0)],
                        photos=[(0.288, 0.325), (0.688, 0.725)])
        posts = out["posts"]
        self.assertEqual(len(posts), 2)
        p0, p1 = posts
        self.assertEqual(p0["author"], "张三")
        self.assertEqual([it["author"] for it in p0["comments"]["items"]],
                         ["李四", "王五"])
        self.assertEqual(p1["author"], "王五")
        self.assertEqual(p1["time_row"]["text"], "2 hour（s） ago")
        self.assertEqual(p1["body"]["texts"],
                         ["下一帖的正文行内容足够长可以锚定"])

    def test_headless_screen_top_rows_own_a_fragment(self):
        # a tail row inside a divider-bounded region, above the region's
        # photo-backed doorplate: the tail stays the PREVIOUS post's
        # fragment, the photo starts the new post after it
        rows = [
            R(0.46, 0.30, "上一帖拖进区域一的尾部行内容长", 0.40),
            R(0.52, 0.30, "张三"),
            R(0.56, 0.30, "正文一行内容足够长能够当作锚点", 0.6),
            R(0.62, 0.30, "3 hour（s） ago", 0.5),
        ]
        out = self.scan(rows, [(0.0, 0.45), (0.45, 1.0)],
                        photos=[(0.508, 0.545)])
        posts = out["posts"]
        self.assertEqual(len(posts), 2)
        p0, p1 = posts
        self.assertIsNone(p0["author"])
        self.assertAlmostEqual(p0["bottom_y"], 0.52)
        self.assertEqual(p0["body"]["texts"],
                         ["上一帖拖进区域一的尾部行内容长"])
        self.assertEqual(p1["author"], "张三")
        self.assertEqual(p1["author_source"], "photo")
        self.assertIsNone(p1["comments"])
        self.assertAlmostEqual(p1["content_bottom_y"], 0.635)
        # mid y=0.5 lands in the fragment's span — we are inside the
        # previous post's tail, doorplate not visible
        vp = out["viewport"]
        self.assertEqual(vp["current_post"], 0)
        self.assertFalse(vp["head_visible"])
        self.assertEqual(vp["top"], {"post": 0, "zone": "body"})
        self.assertEqual(vp["mid_zone"], "body")
        self.assertAlmostEqual(vp["post_bottom_y"], 0.475)

    def test_cross_region_comments_merge(self):
        # long comments crossing a divider cut: the photo-less region is
        # pure continuation of the open post's comments zone; the post's
        # true bottom spans both regions
        rows = [
            R(0.30, 0.30, "张三"),
            R(0.34, 0.30, "正文一行内容足够长能够当作锚点啊", 0.6),
            R(0.40, 0.30, "1 hour（s） ago", 0.5),
            R(0.44, 0.38, "李四：写得真好，收藏了", 0.6),
            R(0.60, 0.38, "王五：同感，写得深刻", 0.6),
            R(0.66, 0.38, "这条评论的换行续接内容也长", 0.6),
        ]
        out = self.scan(rows, [(0.0, 0.28), (0.28, 0.56), (0.56, 1.0)],
                        photos=[(0.288, 0.325)])
        posts = out["posts"]
        self.assertEqual(len(posts), 1)
        p = posts[0]
        self.assertEqual(p["author"], "张三")
        self.assertEqual(p["time_row"]["text"], "1 hour（s） ago")
        items = p["comments"]["items"]
        self.assertEqual([it["author"] for it in items], ["李四", "王五"])
        self.assertEqual(items[1]["text"],
                         "同感，写得深刻 这条评论的换行续接内容也长")
        self.assertAlmostEqual(p["bottom_y"], 1.0)     # span crosses regions
        self.assertAlmostEqual(p["content_bottom_y"], 0.675)
        self.assertAlmostEqual(out["viewport"]["post_bottom_y"], 0.675)

    def test_cross_region_body_merge(self):
        # a post whose BODY crosses the cut: the photo-less region's rows
        # continue the body and carry the previously-missing time row
        rows = [
            R(0.30, 0.30, "张三"),
            R(0.34, 0.30, "上半段正文内容足够长可以锚定哦", 0.6),
            R(0.64, 0.30, "下半段正文继续到分隔线之外", 0.6),
            R(0.70, 0.30, "2 hour（s） ago", 0.5),
        ]
        out = self.scan(rows, [(0.0, 0.28), (0.28, 0.56), (0.56, 1.0)],
                        photos=[(0.288, 0.325)])
        posts = out["posts"]
        self.assertEqual(len(posts), 1)
        p = posts[0]
        self.assertEqual(p["author"], "张三")
        self.assertEqual(p["body"]["texts"],
                         ["上半段正文内容足够长可以锚定哦",
                          "下半段正文继续到分隔线之外"])
        self.assertEqual(p["time_row"]["text"], "2 hour（s） ago")
        self.assertIsNone(p["comments"])
        self.assertAlmostEqual(p["content_bottom_y"], 0.715)

    def test_anchors_filter_and_uniqueness(self):
        out = self.scan(ROWS_TAIL_PLUS_POST,
                        [(0.0, 0.28), (0.28, 0.56), (0.56, 1.0)],
                        photos=ROWS_TAIL_PLUS_POST_PHOTOS)
        anchors = out["viewport"]["anchors"]
        norms = [a["norm"] for a in anchors]
        # chrome, short rows, time rows, expand link, like rows excluded
        self.assertNotIn("moments", norms)
        self.assertNotIn("5 hour(s) ago", norms)
        self.assertNotIn("全文", norms)
        # long unique content rows present (tail + bodies + merged rows)
        self.assertIn("上一帖的尾行内容足够长可以当锚", norms)
        self.assertIn("这条评论的换行续接内容也很长锚", norms)
        self.assertEqual(len(norms), len(set(norms)))
        # anchor post attribution (the merged rows belong to post 1)
        by_norm = {a["norm"]: a["post"] for a in anchors}
        self.assertEqual(by_norm["上一帖的尾行内容足够长可以当锚"], 0)
        self.assertEqual(by_norm["今天天气真不错适合出去走走运动"], 1)
        self.assertEqual(by_norm["这条评论的换行续接内容也很长锚"], 1)

    def test_photoless_regions_extend_open_post_span(self):
        # divider pairs with no rows AND no photos: no post of their own
        # — the open fragment's span runs through them; its content
        # bottom stays at the last real row, never at the span end
        rows = [R(0.10, 0.30, "张三"), R(0.20, 0.30, "孤立帖的正文行")]
        out = self.scan(rows, [(0.0, 0.4), (0.4, 0.7), (0.7, 1.0)])
        posts = out["posts"]
        self.assertEqual(len(posts), 1)
        p0 = posts[0]
        self.assertIsNone(p0["author"])
        self.assertAlmostEqual(p0["bottom_y"], 1.0)
        self.assertAlmostEqual(p0["content_bottom_y"], 0.215)
        vp = out["viewport"]
        self.assertEqual(vp["current_post"], 0)
        self.assertEqual(vp["mid_zone"], "body")

    def test_sponsored_arrow_stub(self):
        out = self.scan(ROWS_TAIL_PLUS_POST,
                        [(0.0, 0.28), (0.28, 0.56), (0.56, 1.0)],
                        arrow_fn=arrow_true,
                        photos=ROWS_TAIL_PLUS_POST_PHOTOS)
        self.assertTrue(out["posts"][1]["sponsored_arrow"])
        out = self.scan(ROWS_TAIL_PLUS_POST,
                        [(0.0, 0.28), (0.28, 0.56), (0.56, 1.0)],
                        photos=ROWS_TAIL_PLUS_POST_PHOTOS)
        self.assertFalse(out["posts"][1]["sponsored_arrow"])

    def test_no_layout_returns_none(self):
        with mock.patch("services.digest.feed_layout.analyze",
                        return_value=None):
            self.assertIsNone(scan_screen(
                "cap.png", ocr_rows_fn=stub_rows_fn(ROWS_TAIL_PLUS_POST)))

    def test_button_and_media_todos_are_none(self):
        out = self.scan(ROWS_TAIL_PLUS_POST,
                        [(0.0, 0.28), (0.28, 0.56), (0.56, 1.0)],
                        photos=ROWS_TAIL_PLUS_POST_PHOTOS)
        p1 = out["posts"][1]
        self.assertIsNone(p1["button_y"])
        self.assertIsNone(p1["media"])
        self.assertIsNone(out["viewport"]["button_y"])

    # ------------------------------------------------ legacy author path
    def test_legacy_band_pins_when_unmeasured(self):
        # find_author (the legacy consumers' path) keeps the hardcoded
        # band: a drifted author column must NOT be found — exactly what
        # the 177-capture regression compares against
        drifted = [R(0.669, 0.265, "丁斌", 1.0)]
        self.assertIsNone(find_author(drifted, 0.62))
        inband = [R(0.669, 0.30, "丁斌", 1.0)]
        hit = find_author(inband, 0.62)
        self.assertEqual(hit["author"], "丁斌")

    def test_legacy_loose_keeps_floor_scan(self):
        # legacy loose path: rows above the 0.18 floor are skipped (a
        # mid-feed screen-top row is the previous post's tail, never a
        # name); the first row at/below the floor claims
        rows = [R(0.10, 0.30, "张三"), R(0.30, 0.30, "王五")]
        legacy = find_author([dict(r) for r in rows], 0.0,
                             header_bounded=False)
        self.assertEqual(legacy["author"], "王五")


class TestAvatarBlocks(unittest.TestCase):
    """The production pixel probe (schema rule 2's implementation)."""

    def make_image(self, w=100, h=200, ink_spans=(), x_span=(6, 13)):
        img = Image.new("RGB", (w, h), (255, 255, 255))
        px = img.load()
        for y0, y1 in ink_spans:
            for py in range(y0, y1 + 1):
                for pxx in range(x_span[0], x_span[1] + 1):
                    px[pxx, py] = (90, 120, 160)
        path = tempfile.mkdtemp() + "/cap.png"
        img.save(path)
        return path

    def test_avatar_block_found(self):
        # a ~10px ink block in the photo column = an avatar
        path = self.make_image(ink_spans=[(20, 30)])
        blocks = avatar_blocks(path, 0.05, 0.15, 0.0, 1.0)
        self.assertEqual(len(blocks), 1)
        y0, y1 = blocks[0]
        self.assertAlmostEqual(y0, 0.10, delta=0.01)
        self.assertAlmostEqual(y1, 0.16, delta=0.02)

    def test_cover_tall_block_discarded(self):
        # half the screen inked = the profile cover, never an avatar
        path = self.make_image(ink_spans=[(0, 100)])
        self.assertEqual(avatar_blocks(path, 0.05, 0.15, 0.0, 1.0), [])

    def test_thin_run_not_a_block(self):
        # a 2px sliver (card edge / anti-aliasing) is below the minimum
        path = self.make_image(ink_spans=[(20, 21)])
        self.assertEqual(avatar_blocks(path, 0.05, 0.15, 0.0, 1.0), [])

    def test_window_clips(self):
        # a block outside the searched y window is not reported
        path = self.make_image(ink_spans=[(20, 30)])
        self.assertEqual(
            avatar_blocks(path, 0.05, 0.15, 0.5, 1.0), [])

    def test_split_avatar_fuses(self):
        # a light row inside an avatar splits it into two runs 4px apart;
        # AVATAR_MERGE_GAP (0.012 → 4.8px at h=400) fuses them back into
        # ONE block — the split lower half's band must never read a door-
        # plate (live lesson: 10 生日季 phantom claims, 2026-11-15)
        path = self.make_image(h=400, ink_spans=[(40, 52), (56, 60)])
        blocks = avatar_blocks(path, 0.05, 0.15, 0.0, 1.0)
        self.assertEqual(len(blocks), 1)
        y0, y1 = blocks[0]
        self.assertAlmostEqual(y0, 0.10, delta=0.01)
        self.assertAlmostEqual(y1, 0.155, delta=0.015)

    def test_gap_beyond_merge_stays_two_blocks(self):
        # 14px apart (3.5× the merge gap) = two genuinely separate
        # pictures — fusing them would hide the second post's doorplate
        path = self.make_image(h=400, ink_spans=[(40, 52), (66, 76)])
        blocks = avatar_blocks(path, 0.05, 0.15, 0.0, 1.0)
        self.assertEqual(len(blocks), 2)

    def test_unreadable_image_is_empty(self):
        self.assertEqual(avatar_blocks("/nonexistent/cap.png",
                                       0.05, 0.15, 0.0, 1.0), [])


class TestParseTimeHours(unittest.TestCase):
    """Time-row text → age hours (the act layer's recency window)."""

    def test_relative_shapes(self):
        from services.digest.post_schema import parse_time_hours
        self.assertAlmostEqual(parse_time_hours("23 hour（s） ago"), 23.0)
        self.assertAlmostEqual(parse_time_hours("5 minute（s） ago"),
                               5 / 60)
        self.assertAlmostEqual(parse_time_hours("2 day（s） ago"), 48.0)
        self.assertAlmostEqual(parse_time_hours("半小时前"), 0.5)
        self.assertAlmostEqual(parse_time_hours("今天 08:12"), 0.0)
        self.assertAlmostEqual(parse_time_hours("昨天 21:03"), 24.0)
        self.assertAlmostEqual(
            parse_time_hours("Yesterday Channels· 极目新闻"), 24.0)
        self.assertIsNone(parse_time_hours("11月05日 17:01"))
        self.assertIsNone(parse_time_hours("星期三"))
        self.assertIsNone(parse_time_hours(""))
        self.assertIsNone(parse_time_hours(None))


class TestMoreButton(unittest.TestCase):
    """The •• comment-entry pill detector (corpus 2026-11-15: 179/179
    time rows hit once chrome stays out of the band; x-center 0.90,
    26 dot samples, x-span 0.027, dy −0.005..−0.002)."""

    def make_image(self, w=200, h=400, dots=(), text_tail=None):
        img = Image.new("RGB", (w, h), (255, 255, 255))
        px = img.load()
        for x0, x1, y0, y1 in dots:      # blue-gray pill dots
            for pxx in range(x0, x1 + 1):
                for py in range(y0, y1 + 1):
                    px[pxx, py] = (130, 150, 180)
        if text_tail:                     # long time-row suffix ink
            x0, x1, y0, y1 = text_tail
            for pxx in range(x0, x1 + 1):
                for py in range(y0, y1 + 1):
                    px[pxx, py] = (60, 60, 60)
        path = tempfile.mkdtemp() + "/cap.png"
        img.save(path)
        return path

    def test_pill_center_measured(self):
        # two dots at stride-2 sampling → centroid is the click target
        path = self.make_image(dots=[(150, 157, 190, 196)])
        hit = feed_layout.more_button(path, 0.70, 0.95, 0.46, 0.49)
        self.assertIsNotNone(hit)
        cx, cy = hit
        self.assertAlmostEqual(cx, 153 / 200, delta=0.005)
        self.assertAlmostEqual(cy, 193 / 400, delta=0.005)

    def test_text_suffix_never_steals_the_blob(self):
        # a long 服务号-style suffix shares the band left of the pill;
        # the RIGHTMOST cluster is the pill (UI truncates text before it)
        path = self.make_image(dots=[(150, 157, 190, 196)],
                               text_tail=(120, 132, 188, 198))
        cx, cy = feed_layout.more_button(path, 0.55, 0.95, 0.46, 0.50)
        self.assertAlmostEqual(cx, 153 / 200, delta=0.005)
        self.assertAlmostEqual(cy, 193 / 400, delta=0.005)

    def test_blank_band_is_none(self):
        path = self.make_image()
        self.assertIsNone(
            feed_layout.more_button(path, 0.70, 0.95, 0.40, 0.50))

    def test_unreadable_image_is_none(self):
        self.assertIsNone(feed_layout.more_button(
            "/nonexistent/cap.png", 0.70, 0.95, 0.40, 0.50))


class TestFeedSchema(unittest.TestCase):
    """The run-level table skeleton（用户 2026-11-15：初始化一次，遍历
    follow；每帖往模板里填）."""

    def make_image(self, w=100, h=200, ink_spans=(), x_span=(6, 13)):
        img = Image.new("RGB", (w, h), (255, 255, 255))
        px = img.load()
        for y0, y1 in ink_spans:
            for py in range(y0, y1 + 1):
                for pxx in range(x_span[0], x_span[1] + 1):
                    px[pxx, py] = (90, 120, 160)
        path = tempfile.mkdtemp() + "/cap.png"
        img.save(path)
        return path

    def test_init_measures_photo_column(self):
        # one capture measures the skeleton: col_x from the time row,
        # photo column = 格 A 的定宽（pane falls back to the default on
        # a blank image）
        path = self.make_image()
        s = FeedSchema.init(path, ocr_rows_fn=stub_rows_fn([
            R(0.10, 0.495, "Moments"),
            R(0.40, 0.308, "1 hour（s） ago", 1.0),
        ]))
        self.assertEqual(s.col_x, 0.308)
        self.assertAlmostEqual(s.photo_x1, 0.300, delta=0.005)
        self.assertAlmostEqual(
            s.photo_x0,
            feed_layout.PANE_X[0] + feed_layout.AVATAR_COL_LEFT_INSET,
            delta=0.005)

    def test_scan_follows_injected_schema_and_fills_photo_slot(self):
        # the traversal reads the column bounds from the schema object;
        # each photo block lands on its post's slot (照片本身在照片区的
        # 起始 y 与宽高); a photo-less post's slot stays None
        s = FeedSchema()
        s.pane_x0, s.pane_x1 = 0.0, 1.0
        s.photo_x0, s.photo_x1 = 0.05, 0.15
        out = scan_screen(
            "cap.png", schema=s,
            ocr_rows_fn=stub_rows_fn(ROWS_TAIL_PLUS_POST),
            layout=mock.Mock(regions=[(0.0, 0.28), (0.28, 0.56),
                                      (0.56, 1.0)]),
            photo_fn=make_photo_fn(ROWS_TAIL_PLUS_POST_PHOTOS))
        p1 = out["posts"][1]
        self.assertEqual(p1["author"], "张三")
        self.assertEqual(p1["photo"]["x0"], 0.05)
        self.assertEqual(p1["photo"]["x1"], 0.15)
        self.assertAlmostEqual(p1["photo"]["y0"], 0.288, delta=0.001)
        self.assertAlmostEqual(p1["photo"]["y1"], 0.325, delta=0.001)
        self.assertIsNone(p1["photo"]["ink_x0"])     # stub image → soft None
        self.assertIsNone(out["posts"][0]["photo"])  # fragment, no photo

    def test_observe_fills_lazy_slots(self):
        # the skeleton's未量定 slots fill from the first post showing
        # them: 门牌行高 / 正文行距 / 时间行高 / 评论区缩进·行距·顶距
        s = FeedSchema()
        s.pane_x0, s.pane_x1 = 0.0, 1.0
        s.photo_x0, s.photo_x1 = 0.05, 0.15
        rows = [
            R(0.30, 0.30, "张三", 1.0, h=0.025),
            R(0.34, 0.30, "今天天气真不错适合出去走走运动", 0.6, h=0.030),
            R(0.37, 0.30, "公园里人不多风景也很好心情舒畅", 0.6, h=0.030),
            R(0.42, 0.30, "5 hour（s） ago", 0.5, h=0.020),
            R(0.50, 0.31, "♡ 李四，王五", 0.5),
            R(0.54, 0.38, "李四：写得真好，收藏了", 0.6),
            R(0.60, 0.38, "王五：同感，这条评论值得反复读", 0.6),
        ]
        scan_screen("cap.png", schema=s, ocr_rows_fn=stub_rows_fn(rows),
                    layout=mock.Mock(regions=[(0.0, 1.0)]),
                    photo_fn=make_photo_fn([(0.288, 0.325)]))
        self.assertEqual(s.name_row_h, 0.025)
        self.assertAlmostEqual(s.body_pitch, 0.03, delta=0.005)
        self.assertAlmostEqual(s.time_row_h, 0.020, delta=0.002)
        self.assertAlmostEqual(s.comment_indent_x, 0.38, delta=0.005)
        self.assertAlmostEqual(s.comment_pitch, 0.06, delta=0.005)
        self.assertAlmostEqual(s.time_comments_gap, 0.06, delta=0.01)

    def test_photo_ink_measured_and_offset_recorded(self):
        # real pixels: the photo slot records the ink's own footprint in
        # the column, and the skeleton takes its first avatar samples
        path = self.make_image(h=400, ink_spans=[(115, 130)])
        s = FeedSchema()
        s.pane_x0, s.pane_x1 = 0.0, 1.0
        s.photo_x0, s.photo_x1 = 0.05, 0.15
        rows = [R(0.30, 0.30, "张三", 1.0, h=0.025),
                R(0.42, 0.30, "1 hour（s） ago", 0.5)]
        out = scan_screen(path, schema=s, ocr_rows_fn=stub_rows_fn(rows),
                          layout=mock.Mock(regions=[(0.0, 1.0)]))
        p0 = out["posts"][0]
        self.assertEqual(p0["author"], "张三")
        self.assertIsNotNone(p0["photo"])
        self.assertAlmostEqual(p0["photo"]["ink_x0"], 0.09, delta=0.015)
        self.assertAlmostEqual(p0["photo"]["ink_x1"], 0.17, delta=0.015)
        self.assertAlmostEqual(s.avatar_offset_x, 0.09, delta=0.015)
        self.assertAlmostEqual(s.avatar_std_h, 0.04, delta=0.01)

    def test_button_entry_measured_for_time_row_posts(self):
        # real pixels: a post with a time row gets its •• comment entry
        # measured (pill dots right of the time text); the schema takes
        # the run's column sample. A photo-less fragment without a time
        # row keeps both slots None.
        path = self.make_image(w=200, h=400, ink_spans=[(120, 135)],
                               x_span=(20, 26))
        img = Image.open(path)
        px = img.load()
        # the pill's two dots, 2px apart (real inter-dot gap ~0.010 <
        # MORE_CLUSTER_GAP 0.012 — the corpus's 179 pills all fuse into
        # one 26-sample cluster; wider gaps would split the cluster)
        for pxx in list(range(170, 177)) + list(range(178, 187)):
            for py in range(168, 175):
                px[pxx, py] = (130, 150, 180)
        img.save(path)
        s = FeedSchema()
        s.pane_x0, s.pane_x1 = 0.0, 1.0
        s.photo_x0, s.photo_x1 = 0.05, 0.15
        rows = [R(0.31, 0.30, "张三", 1.0, h=0.025),
                R(0.42, 0.30, "1 hour（s） ago", 0.5, w=0.15, h=0.015)]
        out = scan_screen(path, schema=s, ocr_rows_fn=stub_rows_fn(rows),
                          layout=mock.Mock(regions=[(0.0, 1.0)]))
        p0 = out["posts"][0]
        self.assertEqual(p0["author"], "张三")
        self.assertIsNotNone(p0["time_row"])
        self.assertAlmostEqual(p0["button_x"], 0.89, delta=0.005)
        self.assertAlmostEqual(p0["button_y"], 0.4275, delta=0.005)
        self.assertAlmostEqual(s.button_x, 0.89, delta=0.005)


if __name__ == "__main__":
    unittest.main()
