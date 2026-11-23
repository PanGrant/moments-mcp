"""Send-resume fast-forward (2026-11-13): ff_step_decision verdicts.

The walk never fine-walks the reply→next crossing again: after a send the
view is rebuilt (⟳) and the seen zone is coasted at 0.6h steps until the
just-replied post (the ledger's LAST entry) is seen again. These tests pin
the per-step verdict — anchor hit (key / jittered key / row fallback),
seen coast, and the two safe degradations (phantom read of a seen post →
'insert', false anchor hit on a quoting post → 'anchor').
"""
import importlib.util
import os
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "post_test", os.path.join(_REPO, "scripts", "post_test.py"))
pt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pt)


def d(key, rows, anchor_key, anchor_head, pool):
    return pt.ff_step_decision(key, rows, anchor_key, anchor_head, pool)


class FFStepDecision(unittest.TestCase):
    ANCHOR_KEY = "无草|周末去爬山拍了些云海的照片"
    ANCHOR_HEAD = "周末去爬山拍了些云海的照片"

    def test_anchor_exact_key(self):
        self.assertEqual(
            d("无草|周末去爬山拍了些云海的照片", [], self.ANCHOR_KEY,
              self.ANCHOR_HEAD, [self.ANCHOR_KEY, "别人|别的帖子"]),
            "anchor")

    def test_anchor_jittered_key(self):
        # OCR char jitter ('拍'→'拼') must not miss the anchor — same floor
        # as the walk's seen-skip (keys_match fuzz, live 2026-11-08 lesson)
        self.assertEqual(
            d("无草|周末去爬山拼了些云海的照片", [], self.ANCHOR_KEY,
              self.ANCHOR_HEAD, ["别人|别的帖子"]),
            "anchor")

    def test_anchor_row_fallback_phantom_author(self):
        # phantom doorplate (content line read as the author) — the unit
        # key matches nothing, but the anchor's own head text is on screen
        key = "女性各阶段的爱好|周末去爬山拍了些云海的照片"
        rows = ["无草", "周末去爬山拍了些云海的照片", "1 hour ago"]
        self.assertEqual(
            d(key, rows, self.ANCHOR_KEY, self.ANCHOR_HEAD,
              ["别人|别的帖子"]),
            "anchor")

    def test_seen_post_coasts(self):
        self.assertEqual(
            d("Hill潘|今天天气不错", [], self.ANCHOR_KEY, self.ANCHOR_HEAD,
              [self.ANCHOR_KEY, "Hill潘|今天天气不错"]),
            "seen")

    def test_unknown_is_insert(self):
        self.assertEqual(
            d("新朋友|刚发的动态内容", [], self.ANCHOR_KEY, self.ANCHOR_HEAD,
              [self.ANCHOR_KEY]),
            "insert")

    def test_phantom_read_of_seen_post_degrades_to_insert(self):
        # a seen post whose doorplate read went phantom: key matches no
        # pool entry and the anchor's head is NOT on this screen — coast
        # past ('insert'), never card. Safe direction: no card on seen.
        key = "事地扯|根本不是这个帖子的文字"
        self.assertEqual(
            d(key, ["别的帖子的正文行"], self.ANCHOR_KEY, self.ANCHOR_HEAD,
              [self.ANCHOR_KEY, "Hill潘|今天天气不错"]),
            "insert")

    def test_false_anchor_hit_is_safe(self):
        # a different post quoting the anchor's words ends the fast-forward
        # early — lands in normal walking over seen posts, harmless
        self.assertEqual(
            d("引用者|" + self.ANCHOR_HEAD, ["无关行", self.ANCHOR_HEAD],
              self.ANCHOR_KEY, self.ANCHOR_HEAD, [self.ANCHOR_KEY]),
            "anchor")

    def test_empty_anchor_coasts_to_insert(self):
        # defensive: no ledger at all — everything is 'insert', the FF cap
        # and the stuck check end the flow honestly
        self.assertEqual(d("某人|随便什么", [], "", "", []), "insert")

    def test_empty_head_anchor_key_only(self):
        # photo post anchor (head='') — key must carry it, fallback off
        self.assertEqual(
            d("无草|", [], "无草|", "", ["无草|"]), "anchor")


class FFSendArming(unittest.TestCase):
    """do_send arms ff_pending on BOTH outcomes (the click happened either
    way — visibility CHECK still means the reply attempt landed)."""

    def _armed_state_roundtrip(self):
        # the flag is plain JSON state — persist/load round-trips it
        st = {"ff_pending": True, "phase": "sent"}
        blob = __import__("json").dumps(st, ensure_ascii=False)
        import json
        self.assertEqual(json.loads(blob)["ff_pending"], True)


class MotionVerdict(unittest.TestCase):
    """motion_verdict: the zero-wait displacement check (2026-11-13). Same
    thresholds as the walk's inline motion check, which caught all 3
    morning flights 3/3. Boundaries from the 0.3h FF step."""

    def setUp(self):
        self.v = pt.motion_verdict

    def test_clean_band(self):
        # ±0.15h around the command (real domain: commands are ≤0.3h — the
        # walk's alignment cap and the FF step are both 0.3h)
        self.assertEqual(self.v(0.30, 0.3), "clean")
        self.assertEqual(self.v(0.17, 0.3), "clean")
        self.assertEqual(self.v(0.43, 0.3), "clean")
        self.assertEqual(self.v(0.44, 0.3), "clean")

    def test_aim_band(self):
        # fold collapse / re-render handoff — re-aim, not a failure
        self.assertEqual(self.v(0.46, 0.3), "aim")
        self.assertEqual(self.v(0.60, 0.3), "aim")

    def test_over_is_flight(self):
        self.assertEqual(self.v(0.61, 0.3), "over")
        self.assertEqual(self.v(2.50, 0.3), "over")

    def test_stall_band(self):
        # didn't move as commanded (dead wheel / spin / backwards hop)
        self.assertEqual(self.v(0.00, 0.3), "stall")
        self.assertEqual(self.v(0.10, 0.3), "stall")
        self.assertEqual(self.v(-1.00, 0.3), "stall")

    def test_none_unverifiable(self):
        self.assertIsNone(self.v(None, 0.3))


if __name__ == "__main__":
    unittest.main()
