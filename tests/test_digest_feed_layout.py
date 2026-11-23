import os
import unittest


class TestFeedLayout(unittest.TestCase):
    """Pixel-level divider detection on synthetic captures that reproduce
    the measured feed geometry (gray bg 225/228/229, hairline dividers
    242/242/242 2px tall, content blocks between them). The avatar column
    is ignored — no avatar detection exists."""

    W, H = 441, 561
    DIV1 = 150   # → y_norm ≈ 0.2674
    DIV2 = 400   # → y_norm ≈ 0.7130
    AV_Y0, AV_Y1 = 160, 190
    AV_X0, AV_X1 = 79, 114   # ≈ 0.18-0.26 of width

    def _blank_capture(self):
        from PIL import Image
        return Image.new("RGB", (self.W, self.H), (225, 228, 229))

    def _make_capture(self, path):
        img = self._blank_capture()
        px = img.load()
        # content surface (e.g. card background) in region 0 — indented to
        # the content column (x≥0.30) like real post content; the avatar
        # column to its left stays background (nothing reads it anymore)
        for yy in range(20, self.DIV1 - 10):
            for xx in range(132, 200):
                px[xx, yy] = (250, 250, 250)
        # the two divider hairlines
        for div in (self.DIV1, self.DIV2):
            for yy in (div, div + 1):
                for xx in range(66, 424):
                    px[xx, yy] = (242, 242, 242)
        # a colored 2px line (photo content) — must NOT become a divider
        for yy in (300, 301):
            for xx in range(66, 424):
                px[xx, yy] = (60, 120, 200)
        # an avatar-like block just below the first divider (region 1):
        # nothing analyzes it — proves dividers are found regardless
        for yy in range(self.AV_Y0, self.AV_Y1):
            for xx in range(self.AV_X0, self.AV_X1):
                px[xx, yy] = (60, 120, 200)
        img.save(path)

    def test_dividers_and_regions(self):
        import os
        import tempfile
        from services.digest.feed_layout import analyze
        tmp = tempfile.TemporaryDirectory()
        try:
            p = os.path.join(tmp.name, "shot.png")
            self._make_capture(p)
            layout = analyze(p)
            self.assertIsNotNone(layout)
            self.assertEqual(len(layout.regions), 3)
            d1, d2 = layout.regions[1][0], layout.regions[2][0]
            self.assertAlmostEqual(d1, (self.DIV1 + 1) / self.H, places=2)
            self.assertAlmostEqual(d2, (self.DIV2 + 1) / self.H, places=2)
            self.assertEqual(layout.regions[0][0], 0.0)
            self.assertEqual(layout.regions[-1][1], 1.0)
        finally:
            tmp.cleanup()

    def test_colored_photo_line_is_not_a_divider(self):
        import os
        import tempfile
        from services.digest.feed_layout import analyze
        tmp = tempfile.TemporaryDirectory()
        try:
            p = os.path.join(tmp.name, "shot.png")
            self._make_capture(p)
            layout = analyze(p)
            ys = [top for top, _ in layout.regions[1:]]
            self.assertNotIn(300 / self.H, ys)
            self.assertNotIn(301 / self.H, ys)
        finally:
            tmp.cleanup()

    def test_one_px_gray_transition_row_is_not_a_divider(self):
        # Live 2026-11-06 feed-top capture: 1px uniform-gray rows INSIDE a
        # post (share-card edges, anti-aliasing, e.g. (238,238,238)/(246,
        # 246,246)) pass the brightness/spread test but must not split the
        # item — real separators measured 2px, always.
        import os
        import tempfile
        from services.digest.feed_layout import analyze
        tmp = tempfile.TemporaryDirectory()
        try:
            p = os.path.join(tmp.name, "shot.png")
            img = self._blank_capture()
            px = img.load()
            for yy, color in ((200, (238, 238, 238)), (300, (246, 246, 246)),
                              (350, (247, 247, 247))):
                for xx in range(66, 424):
                    px[xx, yy] = color
            img.save(p)
            layout = analyze(p)
            self.assertEqual(len(layout.regions), 1)  # nothing split
        finally:
            tmp.cleanup()

    def test_feed_top_cover_screen_has_no_divider(self):
        # The cover abuts post 1 with no hairline (measured 2026-11-06):
        # dark cover on top, white card below, NO divider anywhere → one
        # region spanning the screen (the cover is never image-analyzed;
        # nothing else here to detect).
        import os
        import tempfile
        from services.digest.feed_layout import analyze
        tmp = tempfile.TemporaryDirectory()
        try:
            p = os.path.join(tmp.name, "shot.png")
            W, H = self.W, self.H
            img = self._blank_capture()
            px = img.load()
            cover_bottom = int(0.57 * H)
            for yy in range(0, cover_bottom):
                for xx in range(0, W):
                    px[xx, yy] = (40, 90, 50)  # dark cover photo, full width
            img.save(p)
            layout = analyze(p)
            self.assertEqual(len(layout.regions), 1)
            self.assertEqual(layout.regions[0], (0.0, 1.0))
        finally:
            tmp.cleanup()

    def test_comments_box_edge_is_not_a_divider(self):
        # Live 2026-11-07: the likes/comments box (indent x≈0.30, light-gray
        # fill) produces 2px gray edge lines. Under the window model
        # (2026-11-08) a divider row must be UNIFORM line color across the
        # pane; an indented edge leaves the pane's left white (uniformity
        # ~0.8 < 0.9) and is rejected — no avatar-column check involved.
        import os
        import tempfile
        from services.digest.feed_layout import analyze
        tmp = tempfile.TemporaryDirectory()
        try:
            p = os.path.join(tmp.name, "shot.png")
            img = self._blank_capture()
            px = img.load()
            for yy in (300, 301, 360, 361):     # box top/bottom edges
                for xx in range(int(0.30 * self.W), 424):
                    px[xx, yy] = (242, 242, 242)
            img.save(p)
            layout = analyze(p)
            self.assertEqual(len(layout.regions), 1)  # nothing split
        finally:
            tmp.cleanup()

    def test_text_row_median_artifact_is_not_a_divider(self):
        # Live 2026-11-08 (Gracie/李磊 19-char partials): a text row's ink +
        # anti-aliasing pushed the pane MEDIAN into the gray band (median
        # 235, brightness 705, spread 0, thickness 2 — every old gate
        # passed) and the fake divider sliced posts in half. The median is
        # a rank, not a color the row has: uniformity was 0.02. The
        # uniformity gate must reject it.
        import os
        import tempfile
        from services.digest.feed_layout import analyze
        tmp = tempfile.TemporaryDirectory()
        try:
            p = os.path.join(tmp.name, "shot.png")
            img = self._blank_capture()
            px = img.load()
            for yy in (300, 301):
                # pane samples: ~56 mid-gray (incl. avatar column — old gate
                # passed), a few ink, the rest white → median 235, uniform
                # fraction 56/90 ≈ 0.62
                for xx in range(66, 271):
                    px[xx, yy] = (235, 235, 235)
                for xx in range(274, 301):
                    px[xx, yy] = (26, 26, 26)
                for xx in range(305, 424):
                    px[xx, yy] = (250, 250, 250)
            img.save(p)
            layout = analyze(p)
            self.assertEqual(len(layout.regions), 1)  # nothing split
        finally:
            tmp.cleanup()

    def test_divider_touching_content_is_not_a_divider(self):
        # Window model, blank-side rule: a separator is sandwiched in empty
        # space (≥40 rows measured); a uniform 2px gray band with text ink
        # immediately below is inside content and must not split.
        import os
        import tempfile
        from services.digest.feed_layout import analyze
        tmp = tempfile.TemporaryDirectory()
        try:
            p = os.path.join(tmp.name, "shot.png")
            img = self._blank_capture()
            px = img.load()
            for yy in (300, 301):
                for xx in range(66, 424):
                    px[xx, yy] = (242, 242, 242)
            for xx in range(66, 300):               # ink right below the band
                px[xx, 302] = (26, 26, 26)
            img.save(p)
            layout = analyze(p)
            self.assertEqual(len(layout.regions), 1)  # nothing split
        finally:
            tmp.cleanup()

    def test_unreadable_file_returns_none(self):
        from services.digest.feed_layout import analyze
        self.assertIsNone(analyze("does-not-exist.png"))
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        try:
            p = os.path.join(tmp.name, "junk.png")
            with open(p, "wb") as f:
                f.write(b"not an image")
            self.assertIsNone(analyze(p))
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
