"""时间行候选的内容列 x 门（2026-11-16 深夜真机事故）：

人工置顶验证轮起步第 1 屏，韩玉珂的万得股票快讯卡里嵌着新闻时间戳
'11月16日 15:18' @x=0.337（真内容列左缘 0.26）。is_time_row 本就该
认绝对日期（老帖真时间行长那样），纯形状匹配于是把卡戳当 closer →
边界规则判"绝对日期=出今天"→ 0 按键安全停。同时 measure_col_x 旧
优先级（时间行优先）被这枚唯一候选拖到卡缩进 0.337，照片列吞卡。

修法两层，各钉一组测试：
- measure_col_x 长行簇优先、时间行兜底（长行天然免疫卡污染：卡行
  只会缩进在右）；
- time_row_on_col：closer 候选须坐内容列（x ≤ col_x + 0.03）。
"""
import unittest


WANDA = "tests/fixtures/wanda_card_frame.png"
VIVA = "tests/fixtures/viva_share_frame.png"


def _row(text, x, conf=0.9):
    return {"text": text, "x": x, "y": 0.5, "conf": conf}


class TestTimeRowOnColGate(unittest.TestCase):

    def test_real_time_row_on_col_passes(self):
        from services.digest.post_schema import time_row_on_col
        self.assertTrue(time_row_on_col(_row("1小时前", 0.26), 0.26))
        # col_x 被卡行拖右后真行仍放行（簇永不落在真左缘之左）
        self.assertTrue(time_row_on_col(_row("1小时前", 0.26), 0.288))

    def test_card_embedded_ts_off_col_rejected(self):
        from services.digest.post_schema import time_row_on_col
        row = _row("11月16日 15:18", 0.337)
        self.assertFalse(time_row_on_col(row, 0.288))
        self.assertFalse(time_row_on_col(row, 0.26))

    def test_no_col_x_falls_back_to_shape(self):
        # 纯图屏 col_x=None：退回纯形状匹配，诚实不瞎判
        from services.digest.post_schema import time_row_on_col
        self.assertTrue(time_row_on_col(_row("11月16日 15:18", 0.337),
                                        None))
        self.assertFalse(time_row_on_col(_row("不是时间的话", 0.26), None))

    def test_non_time_row_never_passes(self):
        from services.digest.post_schema import time_row_on_col
        self.assertFalse(time_row_on_col(_row("正文一行", 0.26), 0.26))


class TestMeasureColXPriority(unittest.TestCase):

    def test_long_rows_win_over_time_row(self):
        # 万得帧成分复刻：唯一时间行形状候选 = 卡戳 @0.337，长行全在
        # 卡缩进 0.288 —— col_x 必须取长行簇（0.288），不是卡戳。
        from services.digest.post_schema import measure_col_x
        rows = [_row("11月16日 15:18", 0.337),
                _row("外交部发言人毛宁主持例行记者会", 0.286),
                _row("了中俄美三国元首深圳会晤的消息", 0.288)]
        cx = measure_col_x(rows)
        self.assertIsNotNone(cx)
        self.assertLess(cx, 0.32)   # 卡戳 0.337 不得中标

    def test_time_rows_still_fallback(self):
        # 纯时间行屏（罕见）退回时间行中位
        from services.digest.post_schema import measure_col_x
        cx = measure_col_x([_row("1小时前", 0.26), _row("2小时前", 0.26)])
        self.assertAlmostEqual(cx, 0.26, delta=0.005)

    def test_blank_returns_none(self):
        # 三形状（时间行/作者短行/正文长行）都不在场 → None（诚实）：
        # 空行集，或唯一行在内容带之外（照片列 junk 不算列证据）
        from services.digest.post_schema import measure_col_x
        self.assertIsNone(measure_col_x([]))
        self.assertIsNone(measure_col_x([_row("某个正文行长行", 0.10)]))


class TestWandaCardFrame(unittest.TestCase):

    def test_card_ts_never_becomes_closer(self):
        from services.digest.post_schema import scan_screen
        s = scan_screen(WANDA)
        closer = s.get("page_bottom_closer")
        # 本帧真时间行在屏外（卡帖的时间行在卡之下、下屏才有）——
        # 卡戳被 x 门拒后 closer 诚实为 None，而不是卡戳。
        self.assertIsNone(closer)

    def test_han_yuke_doorplated(self):
        # 头像 cap 修复 + x 门同帧共存：卡帖主人照常取得门牌
        from services.digest.post_schema import scan_screen
        s = scan_screen(WANDA)
        hy = [p for p in s["posts"]
              if (p.get("author") or "") == "韩玉珂"]
        self.assertEqual(len(hy), 1)
        self.assertTrue(hy[0]["doorplate_visible"])

    def test_card_ts_stays_in_body_not_comments(self):
        # x 门让卡戳失去 closer 资格后，它留在帖正文中（不截断正文、
        # 不把卡内容误归评论）。
        from services.digest.post_schema import scan_screen
        s = scan_screen(WANDA)
        hy = [p for p in s["posts"]
              if (p.get("author") or "") == "韩玉珂"][0]
        body_text = "".join((hy.get("body") or {}).get("texts") or [])
        self.assertIn("11月16日 15:18", body_text)
        self.assertIsNone(hy.get("time_row"))


class TestRealCloserSurvivesGate(unittest.TestCase):

    def test_viva_time_row_still_recognized(self):
        # 门不得错杀真 closer：Viva 帧的真时间行（前缀形态）过门如常
        from services.digest.post_schema import scan_screen
        s = scan_screen(VIVA)
        viva = [p for p in s["posts"] if (p.get("author") or "") == "Viva"]
        closer = (viva[0].get("time_row") or {}).get("text") or ""
        self.assertIn("VIVA", closer)


if __name__ == "__main__":
    unittest.main()
