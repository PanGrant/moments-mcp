"""门牌名字区只认位置、不看内容（2026-11-23 用户定案，Viva 两连漏根因修复）：

2026-11-22 真机轮 Viva 两条长帖全被跳过。离线复扫定位：名字行 OCR
读得出"Viva"，但自信分 0.30 < 门槛 0.45，被名字区里的"自信分门"拒
掉 → author=None。同屏 Census：同一个名字在五帧里自信分滚出
0.3/0.3/0.5/1.0 四档——Vision 的自信分是量化的档位，不是校准过的概
率，英文短行随机掉档。而全天 27 帧 31 个候选座的普查：内容门从未拦
下过任何垃圾，只误杀过 Viva 这一个真名（两次）。

宪法（用户 2026-11-23）：头像右上方固定高度内的那行字就是作者名，
不看内容；这段高度之下的东西与身份无关；唯一例外是行右侧有下拉
箭头 = 广告帖（像素探针另判，sponsored_arrow）。

修复：_doorplate_row 撤掉自信分/长度/格式黑名单三道内容门（老的无
照片时代遗产），只留结构三关（贴靠/照片身高/清空，schema 量定值）。

铁证帧（真机 11-22 11:39 轮，/tmp/feed_reader 原名 l1/l8）：
- viva_lowconf_frame1.png：Viva 年糕帖，名字自信分 0.30，修复前
  author=None；修复后必须读出 'Viva'。
- viva_lowconf_frame2.png：Viva 签单帖（"每次签单结束后…"），名字
  自信分 0.30，修复前 author=None；修复后必须读出 'Viva'。

普查回归承诺：全天另外 28 个候选座判定一个不变（已在 11-23 逐座
old-vs-new 同行比对兑现：31 座中只有上述 3 处变化 = 两帖在 pre/L1/L8
的座，其余 28 座逐字相同，含屏幕底边半截头像的 None 座）。
"""
import unittest


F1 = "tests/fixtures/viva_lowconf_frame1.png"
F2 = "tests/fixtures/viva_lowconf_frame2.png"


class TestVivaLowConfFrames(unittest.TestCase):
    """真帧回归：自信分 0.30 的名字行必须读出 Viva（走真 OCR，
    与 test_avatar_cap_regression 同款代价）。"""

    def test_frame1_niangao_post_reads_viva(self):
        from services.digest.post_schema import scan_screen
        posts = scan_screen(F1)["posts"]
        viva = [p for p in posts if (p.get("author") or "") == "Viva"]
        self.assertEqual(len(viva), 1)
        self.assertTrue(viva[0]["doorplate_visible"])
        self.assertFalse(viva[0].get("sponsored_arrow"))

    def test_frame2_qiandan_post_reads_viva(self):
        from services.digest.post_schema import scan_screen
        posts = scan_screen(F2)["posts"]
        viva = [p for p in posts if (p.get("author") or "") == "Viva"]
        self.assertEqual(len(viva), 1)
        self.assertTrue(viva[0]["doorplate_visible"])
        self.assertFalse(viva[0].get("sponsored_arrow"))


class TestZoneIsContentBlind(unittest.TestCase):
    """纯结构单元测试（不依赖 OCR）：名字区里的行——自信分再低、
    字再长、长得再像时间行——都不得因内容被拒。位置定义身份。"""

    def _doorplate(self, row):
        from services.digest.post_schema import FeedSchema, _doorplate_row
        schema = FeedSchema()
        # 照片块 y 0.35-0.45（比名字行高够 photo_min_ratio 倍），
        # 名字行贴块顶（y=0.35 在 hug 窗内），下行 0.50 清空达标。
        rows = [dict(row), {"text": "正文第一行", "x": 0.27, "y": 0.50,
                            "w": 0.4, "h": 0.02, "conf": 1.0}]
        return _doorplate_row(rows, 0.35, 0.45, schema)

    def test_low_conf_row_is_still_the_author(self):
        got = self._doorplate({"text": "Viva", "x": 0.27, "y": 0.352,
                               "w": 0.05, "h": 0.02, "conf": 0.3})
        self.assertIsNotNone(got)
        self.assertEqual(got["author"], "Viva")

    def test_overlong_row_is_still_the_author(self):
        # 长度上限 AUTHOR_MAX_LEN=30 早被真名突破过（糖K经理 21 字全名）。
        got = self._doorplate({"text": "糖K经理（信息没回请打电话）@合肥鑫地文化",
                               "x": 0.27, "y": 0.352, "w": 0.3,
                               "h": 0.02, "conf": 1.0})
        self.assertIsNotNone(got)
        self.assertIn("糖K经理", got["author"])

    def test_time_ish_row_is_still_the_author(self):
        # 格式黑名单在名字区无管辖权——贴靠位置本身就证明它是名字行。
        got = self._doorplate({"text": "1h", "x": 0.27, "y": 0.352,
                               "w": 0.03, "h": 0.02, "conf": 1.0})
        self.assertIsNotNone(got)
        self.assertEqual(got["author"], "1h")

    def test_nothing_hugging_stays_none(self):
        # 结构关原样保留：块顶贴靠窗内没有行 → 仍是无门牌（诚实的头切）。
        got = self._doorplate({"text": "Viva", "x": 0.27, "y": 0.40,
                               "w": 0.05, "h": 0.02, "conf": 1.0})
        self.assertIsNone(got)


if __name__ == "__main__":
    unittest.main()
