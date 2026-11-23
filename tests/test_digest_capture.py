import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from services.digest.screen_capture import (
    WindowInfo,
    find_window,
    list_windows,
    capture_window,
    screen_is_locked,
)
from services.digest.json_utils import parse_json_array
from services.digest.vision_extract import extract_moments_items


# ---------------------------------------------------------------------------
# screen_capture: window discovery (fake Quartz injected into sys.modules)
# ---------------------------------------------------------------------------

FAKE_WINDOWS = [
    {"kCGWindowNumber": 11, "kCGWindowOwnerName": "WeChat", "kCGWindowName": "朋友圈",
     "kCGWindowBounds": {"X": 100, "Y": 50, "Width": 900, "Height": 700}},
    {"kCGWindowNumber": 12, "kCGWindowOwnerName": "WeChat", "kCGWindowName": "",
     "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 400, "Height": 300}},
    {"kCGWindowNumber": 13, "kCGWindowOwnerName": "Safari", "kCGWindowName": "GitHub",
     "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 1200, "Height": 800}},
]


class _FakeQuartz:
    kCGWindowListOptionOnScreenOnly = 1
    kCGNullWindowID = 0

    @staticmethod
    def CGWindowListCopyWindowInfo(option, window_id):
        return FAKE_WINDOWS


class TestListWindows(unittest.TestCase):
    def test_parses_quartz_output(self):
        with mock.patch.dict(sys.modules, {"Quartz": _FakeQuartz}):
            windows = list_windows()
        self.assertEqual(len(windows), 3)
        self.assertEqual(windows[0], WindowInfo(
            window_id=11, owner="WeChat", title="朋友圈",
            bounds={"x": 100, "y": 50, "width": 900, "height": 700},
        ))


class TestFindWindow(unittest.TestCase):
    def run_find(self, owner, title=""):
        with mock.patch.dict(sys.modules, {"Quartz": _FakeQuartz}):
            return find_window(owner, title)

    def test_finds_by_owner_and_title(self):
        win = self.run_find("WeChat", "朋友圈")
        self.assertEqual(win.window_id, 11)

    def test_owner_only_prefers_largest(self):
        # Two WeChat windows: the titled one is also the largest.
        win = self.run_find("WeChat")
        self.assertEqual(win.window_id, 11)

    def test_owner_matching_is_case_insensitive(self):
        self.assertEqual(self.run_find("wechat").window_id, 11)

    def test_title_substring_must_match(self):
        self.assertIsNone(self.run_find("WeChat", "moments"))

    def test_no_match_returns_none(self):
        self.assertIsNone(self.run_find("NotAnApp"))


# Lock screen (observed live): the Window Server shields the display, and
# third-party window capture fails ("could not create image from window").
LOCKED_WINDOWS = [
    {"kCGWindowNumber": 201, "kCGWindowOwnerName": "Window Server",
     "kCGWindowName": "Display 1 Shield",
     "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 1440, "Height": 900}},
]

UNLOCKED_WINDOWSERVER_WINDOWS = [
    {"kCGWindowNumber": 202, "kCGWindowOwnerName": "Window Server",
     "kCGWindowName": "Display 1 Backstop",
     "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 1440, "Height": 900}},
    {"kCGWindowNumber": 203, "kCGWindowOwnerName": "Window Server",
     "kCGWindowName": "Menubar",
     "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 1440, "Height": 24}},
]


class _FakeLockedQuartz(_FakeQuartz):
    @staticmethod
    def CGWindowListCopyWindowInfo(option, window_id):
        return LOCKED_WINDOWS


class TestScreenIsLocked(unittest.TestCase):
    def test_locked_when_shield_onscreen(self):
        with mock.patch.dict(sys.modules, {"Quartz": _FakeLockedQuartz}):
            self.assertTrue(screen_is_locked())

    def test_unlocked_backstop_and_menubar_are_not_lock(self):
        class _FakeUnlockedQuartz(_FakeQuartz):
            @staticmethod
            def CGWindowListCopyWindowInfo(option, window_id):
                return UNLOCKED_WINDOWSERVER_WINDOWS
        with mock.patch.dict(sys.modules, {"Quartz": _FakeUnlockedQuartz}):
            self.assertFalse(screen_is_locked())

    def test_unlocked_normal_desktop(self):
        with mock.patch.dict(sys.modules, {"Quartz": _FakeQuartz}):
            self.assertFalse(screen_is_locked())


class TestCaptureWindow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "shot.png")

    def tearDown(self):
        self.tmp.cleanup()

    def _mock_run(self, returncode=0, stderr=""):
        return mock.patch.object(
            subprocess, "run",
            return_value=SimpleNamespace(returncode=returncode, stderr=stderr, stdout=""),
        )

    def test_invokes_screencapture_with_window_id(self):
        with open(self.out, "wb") as f:
            f.write(b"png")
        with self._mock_run() as run, \
             mock.patch.object(os.path, "getsize", return_value=100):
            result = capture_window(42, self.out)
        self.assertEqual(result, self.out)
        args = run.call_args[0][0]
        self.assertEqual(args[:4], ["screencapture", "-x", "-o", "-l"])
        self.assertEqual(args[4], "42")
        self.assertEqual(args[5], self.out)

    def test_raises_on_nonzero_exit(self):
        with self._mock_run(returncode=1, stderr="could not capture"):
            with self.assertRaises(RuntimeError) as ctx:
                capture_window(42, self.out)
        self.assertIn("could not capture", str(ctx.exception))

    def test_raises_on_empty_output_missing_permission(self):
        with self._mock_run(), \
             mock.patch.object(os.path, "exists", return_value=True), \
             mock.patch.object(os.path, "getsize", return_value=0):
            with self.assertRaises(RuntimeError) as ctx:
                capture_window(42, self.out)
        self.assertIn("Screen Recording", str(ctx.exception))


# ---------------------------------------------------------------------------
# vision_extract: output parsing (no network)
# ---------------------------------------------------------------------------

class TestParseJsonArray(unittest.TestCase):
    def test_plain_array(self):
        self.assertEqual(parse_json_array('[{"author": "A"}]'), [{"author": "A"}])

    def test_markdown_fenced(self):
        self.assertEqual(parse_json_array('```json\n[{"a": 1}]\n```'), [{"a": 1}])

    def test_prose_around_array(self):
        self.assertEqual(parse_json_array('结果如下：[{"a": 1}] 请查收'), [{"a": 1}])

    def test_empty_array(self):
        self.assertEqual(parse_json_array("[]"), [])

    def test_not_an_array_raises(self):
        with self.assertRaises(ValueError):
            parse_json_array('{"author": "A"}')

    def test_garbage_raises(self):
        with self.assertRaises(Exception):
            parse_json_array("完全不是JSON的输出")


class TestExtractMomentsItems(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.image = os.path.join(self.tmp.name, "shot.png")
        with open(self.image, "wb") as f:
            f.write(b"png")

    def tearDown(self):
        self.tmp.cleanup()

    def _mock_openai(self, content):
        msg = SimpleNamespace(content=content)
        resp = SimpleNamespace(choices=[SimpleNamespace(message=msg)])
        client = mock.Mock()
        client.chat.completions.create.return_value = resp
        return mock.patch("openai.OpenAI", return_value=client)

    def test_sends_image_and_parses_items(self):
        with self._mock_openai('[{"author": "Popo", "text": "hi", "images_desc": "", "comments": [], "time_hint": "1小时前"}]') as m:
            items = extract_moments_items(
                self.image, model="glm-test", base_url="http://x/v4/", api_key="k",
            )
        self.assertEqual(items[0]["author"], "Popo")
        kwargs = m.return_value.chat.completions.create.call_args[1]
        self.assertEqual(kwargs["model"], "glm-test")
        content_parts = kwargs["messages"][0]["content"]
        self.assertTrue(content_parts[0]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_non_json_output_raises(self):
        with self._mock_openai("抱歉我看不到内容"):
            with self.assertRaises(ValueError):
                extract_moments_items(self.image, model="m", base_url="http://x", api_key="k")


if __name__ == "__main__":
    unittest.main()
