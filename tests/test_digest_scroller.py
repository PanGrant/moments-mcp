import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from services.digest.scroller import accessibility_trusted, make_scroll_fn, scroll_at


class _FakeAX:
    @staticmethod
    def AXIsProcessTrusted():
        return 1


class TestAccessibilityTrusted(unittest.TestCase):
    def test_true_via_applicationservices(self):
        with mock.patch.dict(sys.modules, {"ApplicationServices": _FakeAX}):
            self.assertTrue(accessibility_trusted())

    def test_true_via_quartz(self):
        # sys.modules entry None → `from X import ...` raises ImportError,
        # forcing the second import path.
        with mock.patch.dict(sys.modules, {"ApplicationServices": None, "Quartz": _FakeAX}):
            self.assertTrue(accessibility_trusted())

    def test_falls_back_to_osascript_probe(self):
        with mock.patch.dict(sys.modules, {"ApplicationServices": None, "Quartz": None}), \
             mock.patch("services.digest.scroller.subprocess.run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout="42", stderr="")
            self.assertTrue(accessibility_trusted())
            run.return_value = SimpleNamespace(returncode=1, stdout="", stderr="not allowed")
            self.assertFalse(accessibility_trusted())


def _fake_quartz_events():
    calls = []

    class Q:
        kCGScrollEventUnitLine = 1
        kCGHIDEventTap = 0
        kCGEventMouseMoved = 5
        kCGMouseButtonLeft = 0

        @staticmethod
        def CGEventCreateMouseEvent(src, evtype, pos, button):
            return ("move", evtype, pos)

        @staticmethod
        def CGEventCreateScrollWheelEvent(src, unit, n, lines):
            return ("scroll", lines)

        @staticmethod
        def CGEventPost(tap, ev):
            calls.append(ev)

    return Q, calls


class TestScrollAt(unittest.TestCase):
    def test_posts_move_then_scroll_events(self):
        Q, calls = _fake_quartz_events()
        with mock.patch.dict(sys.modules, {"Quartz": Q}), \
             mock.patch("services.digest.scroller.time.sleep"):
            scroll_at(100, 200, lines=-5, repeats=3, settle=0)
        moves = [c for c in calls if c[0] == "move"]
        scrolls = [c for c in calls if c[0] == "scroll"]
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0][2], (100, 200))
        self.assertEqual(len(scrolls), 3)
        self.assertTrue(all(ev[1] == -5 for ev in scrolls))


class TestMakeScrollFn(unittest.TestCase):
    def test_targets_window_center(self):
        with mock.patch("services.digest.scroller.scroll_at") as sa:
            fn = make_scroll_fn({"x": 10, "y": 20, "width": 100, "height": 60})
            fn()
        sa.assert_called_once_with(60.0, 50.0)


if __name__ == "__main__":
    unittest.main()
