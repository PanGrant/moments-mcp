import unittest
import time as _time

from services.digest.walker import (
    decide,
    empty_ledger,
    identify,
    is_boundary_time,
    load_ledger,
    _abs_time,
    _match_entry,
    _bind_headless,
    _actionable,
)


def P(author="张三", time=None, body=("今天天气真不错适合出去走走运动",
                                    "公园里人不多风景也很好心情舒畅"),
      arrow=False):
    """Minimal post-schema-shaped post for the decision core."""
    return {
        "author": author,
        "author_source": "photo" if author else None,
        "doorplate_visible": author is not None,
        "sponsored_arrow": arrow,
        "time_row": ({"text": time, "y": 0.5, "bottom": 0.515}
                     if time else None),
        "body": {"top_y": 0.3, "bottom_y": 0.6,
                 "texts": list(body)},
    }


def scan_of(posts, current=0):
    return {"posts": posts,
            "viewport": {"current_post": current, "head_visible": True}}


OWN = {"Grant Pan"}


class TestBoundaryTime(unittest.TestCase):
    def test_boundary_shapes(self):
        # 用户 2026-11-15 令：超过 24h（yesterday / N days ago）出工作集
        for t in ("yesterday", "Yesterday Official Accounts• 所念所愿皆星河",
                  "昨天", "昨天 21:03", "1 day(s) ago", "1天前",
                  "2 day(s) ago", "2天前", "3 天前", "10 day（s） ago",
                  "前天", "前天 21:03", "last week", "last month",
                  "2周前", "2 个星期前", "3个月前", "1 year ago",
                  "5 years ago"):
            self.assertTrue(is_boundary_time(t), t)

    def test_inside_shapes(self):
        for t in ("just now", "an hour ago", "5 hour(s) ago",
                  "23 hour(s) ago", "5 minute(s) ago", "半小时前",
                  "今天 08:12", "today at 08:12"):
            self.assertFalse(is_boundary_time(t), t)

    def test_unresolvable_is_boundary(self):
        # weekday / absolute date — could be 3 days or 30: 保守停走
        # （宁停勿越，v1 的"继续走"废止 2026-11-15）
        for t in ("星期三", "周三 09:00", "11月05日 17:01"):
            self.assertTrue(is_boundary_time(t), t)

    def test_empty_is_not_boundary(self):
        for t in (None, ""):
            self.assertFalse(is_boundary_time(t), t)


class TestIdentify(unittest.TestCase):
    def test_create_then_rejoin_across_landings(self):
        # THE cross-landing case: doorplate landing sees the body head,
        # the comment-bottom landing is head-cut and shows a DIFFERENT
        # row of the same post — fuzzy anchor must rejoin them
        led = empty_ledger()
        identify(scan_of([P(time="5 hour(s) ago")]), led, {}, landing=0)
        self.assertEqual(len(led["posts"]), 1)
        e = led["posts"][0]
        self.assertEqual(e["status"], "pending")
        self.assertEqual(e["key"], "张三|5 hour(s) ago")
        self.assertEqual(len(e["anchors"]), 2)

        tail = P(author=None, time="5 hour(s) ago",
                 body=("公园里人不多风景也很好心情舒畅",))
        identify(scan_of([tail], current=0), led, {}, landing=1)
        self.assertEqual(len(led["posts"]), 1)          # no duplicate
        self.assertEqual(led["posts"][0]["landings"], [0, 1])
        self.assertEqual(led["sightings"], [])

    def test_time_row_attaches_later(self):
        led = empty_ledger()
        # landing 0: doorplate + body, time row not on screen yet
        identify(scan_of([P(time=None)]), led, {}, landing=0)
        self.assertIsNone(led["posts"][0]["time_text"])
        # landing 1: head-cut continuation carries the closer
        identify(scan_of([P(author=None, time="2 hour(s) ago",
                            body=("公园里人不多风景也很好心情舒畅",))]),
                 led, {}, landing=1)
        self.assertEqual(led["posts"][0]["time_text"], "2 hour(s) ago")
        self.assertEqual(led["posts"][0]["key"], "张三|2 hour(s) ago")

    def test_headcut_unclaimed_becomes_sighting(self):
        led = empty_ledger()
        identify(scan_of([P(author=None, time="1 hour(s) ago",
                            body=("完全陌生的一行正文内容足够长锚",))]),
                 led, {}, landing=3)
        self.assertEqual(led["posts"], [])
        self.assertEqual(len(led["sightings"]), 1)
        self.assertEqual(led["sightings"][0]["landing"], 3)

    def test_ad_and_own_statuses(self):
        led = empty_ledger()
        identify(scan_of([P(arrow=True, time="3 hour(s) ago")]), led,
                 {"own_names": OWN}, landing=0)
        self.assertEqual(led["posts"][0]["status"], "skipped_ad")
        identify(scan_of([P(author="Grant Pan", time="2 hour(s) ago",
                            body=("这是我自己发的一帖正文内容足够长",))]),
                 led, {"own_names": OWN}, landing=1)
        self.assertEqual(led["posts"][1]["status"], "own")

    def test_same_author_new_post_is_new_entry(self):
        # same author, DIFFERENT time rows = different posts, never merged
        led = empty_ledger()
        identify(scan_of([P(time="5 hour(s) ago")]), led, {}, landing=0)
        identify(scan_of([P(time="1 hour(s) ago",
                            body=("另一帖的正文内容一样足够长锚点",))]),
                 led, {}, landing=1)
        self.assertEqual(len(led["posts"]), 2)


class TestDecide(unittest.TestCase):
    def test_retry_on_failed_scan(self):
        self.assertEqual(decide(None, empty_ledger(), {})["action"],
                         "retry")

    def test_boundary_stops_the_walk(self):
        led = empty_ledger()
        identify(scan_of([P(time="2 day(s) ago")]), led, {"own_names": OWN}, landing=0)
        d = decide(scan_of([P(time="2 day(s) ago")]), led, {"own_names": OWN})
        self.assertEqual(d["action"], "boundary")
        self.assertEqual(d["time_text"], "2 day(s) ago")

    def test_known_entry_out_of_today_stops_boundary(self):
        # 48 屏盲走事故回归（2026-11-16 晚）：矮窗口+图多帖长 → 一屏只见
        # 帖头不见时间行。屏幕无时间行，但帐本 entry 是 yesterday → 也停。
        led = empty_ledger()
        identify(scan_of([P(time="Yesterday Channels•新黄河",
                            body=("昨天那帖的正文内容足够长能做锚点用",))]),
                 led, {"own_names": OWN}, landing=0)
        d = decide(scan_of([P(author=None,
                             body=("昨天那帖的正文内容足够长能做锚点用",))]),
                   led, {"own_names": OWN})
        self.assertEqual(d["action"], "boundary", d)
        self.assertEqual(d["reason"], "known-out-of-today")

    def test_known_entry_today_keeps_walking(self):
        # 帐本匹配到今天的 entry：不是边界，继续走（顶部 coast 正常态）。
        led = empty_ledger()
        identify(scan_of([P(time="5 hour(s) ago",
                            body=("今天早上那帖的正文内容足够长锚点",))]),
                 led, {"own_names": OWN}, landing=0)
        d = decide(scan_of([P(author=None,
                             body=("今天早上那帖的正文内容足够长锚点",))]),
                   led, {"own_names": OWN})
        self.assertEqual((d["action"], d["reason"]), ("walk", "head-cut-身份待定"))

    def test_page_bottom_closer_boundary_stops(self):
        # 落屏 schema（2026-11-16 深夜，用户定）：↓ 必动必向下，屏底必
        # 收束于某帖时间行（左，与 •• 评论钮同行）或评论结尾。屏底帖的
        # closer 槽是 boundary 时间 = 边界已到屏上 → 停。不需要帖子身份
        # （无头帖被区域模型丢弃，槽不受影响）。
        scan = scan_of([P(author=None,
                          body=("昨天那帖的正文内容足够长能做锚点用",))])
        scan["page_bottom_closer"] = {"text": "Yesterday at 22:10",
                                      "y": 0.80}
        d = decide(scan, empty_ledger(), {"own_names": OWN})
        self.assertEqual(d["action"], "boundary", d)
        self.assertEqual(d["reason"], "page-bottom-out-of-today")
        self.assertEqual(d["time_text"], "Yesterday at 22:10")

    def test_page_bottom_closer_today_or_absent_keeps_walking(self):
        # 槽是今天（两屏节奏的第一屏：屏底=时间行）或感知失败（None）：
        # 都不是边界，继续纯下翻（None 交由下一屏重新感知，不瞎判）。
        base = scan_of([P(author=None,
                          body=("陌生头切帖的一行正文足够长锚",))])
        scan = dict(base)
        scan["page_bottom_closer"] = {"text": "2 hour(s) ago", "y": 0.75}
        d = decide(scan, empty_ledger(), {"own_names": OWN})
        self.assertEqual((d["action"], d["reason"]),
                         ("walk", "head-cut-身份待定"))

        d = decide(base, empty_ledger(), {"own_names": OWN})
        self.assertEqual((d["action"], d["reason"]),
                         ("walk", "head-cut-身份待定"))

    def test_walk_reasons(self):
        led = empty_ledger()
        # pending new post (identify already folded it in)
        identify(scan_of([P(time="5 hour(s) ago")]), led, {"own_names": OWN}, landing=0)
        d = decide(scan_of([P(time="5 hour(s) ago")]), led, {"own_names": OWN})
        self.assertEqual((d["action"], d["reason"]), ("walk", "known"))

        d = decide(scan_of([P(author=None, time="1 hour(s) ago",
                              body=("陌生头切帖的一行正文足够长锚",))]),
                   led, {"own_names": OWN})
        self.assertEqual((d["action"], d["reason"]),
                         ("walk", "head-cut-身份待定"))

        d = decide(scan_of([P(arrow=True, time="3 hour(s) ago")]), led, {"own_names": OWN})
        self.assertEqual((d["action"], d["reason"]), ("walk", "ad-skipped"))

        d = decide(scan_of([P(author="Grant Pan", time="2 hour(s) ago")]),
                   led, {"own_names": OWN})
        self.assertEqual((d["action"], d["reason"]), ("walk", "own"))

        d = decide(scan_of([P(time="5 hour(s) ago")], current=None), led, {"own_names": OWN})
        self.assertEqual((d["action"], d["reason"]), ("walk", "no-doorplate"))


# ---------------------------------------------------------------------------
# step 2: the walk loop, dry-run against scripted screens (zero live IO)
# ---------------------------------------------------------------------------

import json
import os
import tempfile

from services.digest.walker import walk


def screen(posts, current=0, anchors=(), bottom=0.9):
    """Scripted scan result with the viewport fields walk() consumes."""
    return {"posts": posts,
            "viewport": {"current_post": current, "head_visible": True,
                         "top": {"post": 0, "zone": "body"},
                         "post_bottom_y": bottom,
                         "anchors": [{"norm": a, "text": a} for a in anchors]}}


def make_io(screens):
    """Scripted IO bundle: captures pop the scripted sequence (the last
    screen repeats when exhausted → dead-press detection fires, which is
    the right offline behavior for an overrun walk). One scan per
    landing — no cross-page carry exists anymore (doorplate claims are
    photo-backed by the scanner; identity continuity lives in the
    ledger)."""
    state = {"i": 0, "presses": 0}

    def capture():
        return f"fake://{state['i']}"

    def scan_fn(_path):
        i = state["i"]
        state["i"] += 1
        return screens[i] if i < len(screens) else screens[-1]

    def press_down():
        state["presses"] += 1

    return state, capture, scan_fn, press_down


QUIET = lambda line: None
NOSLEEP = lambda s: None


class TestWalk(unittest.TestCase):
    # distinct-content rows — realistic posts are NOT 2-char-apart
    # variants of each other (a template fixture once tripped the fuzzy
    # ruler at 0.875 ≥ 0.85 and correctly merged three "different" posts)
    A_ROW = "阿明的帖子讲的是云南旅行攻略游记"
    B_ROW = "小雨分享了一首钢琴曲很好听啊"
    C_ROW = "老周转了篇城市更新深度报道文章"

    def test_happy_path_boundary_stop(self):
        # 3 landings: 阿明 complete → 小雨 complete → 老周 2 days old
        # (BOUNDARY). Anchor chains share one row between neighbors so the
        # self-check sees sub-screen steps.
        s1 = screen([P(author="阿明", time="3 hour(s) ago",
                       body=(self.A_ROW,))],
                    anchors=(self.A_ROW,))
        s2 = screen([P(author=None, time="3 hour(s) ago",
                       body=(self.A_ROW,)),
                     P(author="小雨", time="1 hour(s) ago",
                       body=(self.B_ROW,))],
                    current=1, anchors=(self.A_ROW, self.B_ROW))
        s3 = screen([P(author="小雨", time="1 hour(s) ago",
                       body=(self.B_ROW,)),
                     P(author="老周", time="2 day(s) ago",
                       body=(self.C_ROW,))],
                    current=1, anchors=(self.B_ROW, self.C_ROW))
        state, capture, scan_fn, press = make_io([s1, s2, s3])
        r = walk(press_down=press, capture=capture, scan_fn=scan_fn,
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP)
        self.assertTrue(r["stop_reason"].startswith("boundary"))
        self.assertEqual(r["stop_reason"], "boundary (2 day(s) ago)")
        self.assertEqual(r["landings"], 3)
        self.assertEqual(r["presses"], 2)          # boundary lands BEFORE
        authors = [e["author"] for e in r["ledger"]["posts"]]
        self.assertEqual(authors, ["阿明", "小雨", "老周"])
        self.assertEqual(r["ledger"]["sightings"], [])
        # the tail landings rejoined their doorplate entries
        self.assertEqual(r["ledger"]["posts"][0]["landings"], [1, 2])

    def test_emit_hook_streams_structured_events(self):
        # 脸/代理的事件流（FACE_DESIGN §3）：landing/action/stop 三种结构化
        # 事件按序流出；不传 emit 时默认无操作（352 老测试全绿即为证明）。
        # 夹具走亚屏步渐入边界（锚重叠1）——首按锚重叠0 的边界现在会被
        # 瞬移守卫标 teleport-suspect（2026-11-17），不再是 plain boundary。
        s1 = screen([P(author="阿明", time="3 hour(s) ago",
                       body=(self.A_ROW,))],
                    anchors=(self.A_ROW,))
        s2 = screen([P(author="老周", time="2 day(s) ago",
                       body=(self.A_ROW, self.C_ROW))],
                    anchors=(self.A_ROW, self.C_ROW))
        state, capture, scan_fn, press = make_io([s1, s2])
        seen = []
        r = walk(press_down=press, capture=capture, scan_fn=scan_fn,
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP,
                 emit=seen.append)
        kinds = [e["t"] for e in seen]
        self.assertEqual(kinds[0], "landing")
        self.assertTrue(kinds[-1], "stop" in kinds)
        stops = [e for e in seen if e["t"] == "stop"]
        self.assertEqual(len(stops), 1)
        self.assertTrue(stops[0]["reason"].startswith("boundary"))
        self.assertEqual(stops[0]["landings"], 2)
        landings = [e for e in seen if e["t"] == "landing"]
        self.assertEqual([e["n"] for e in landings], [1, 2])
        self.assertEqual(landings[0]["cur_author"], "阿明")

    def test_subscreen_step_rejoins_entry(self):
        # a sub-screen step re-shows the old post's row: the self-check
        # logs 锚重叠1 and the ledger's three-ruler identity matching
        # re-joins the landed tail to its doorplate entry — no carry, no
        # re-scan; the ledger IS the continuity (landing numbers are
        # 1-based: identify runs after landing += 1)
        s1 = screen([P(author="阿明", time="3 hour(s) ago",
                       body=(self.A_ROW,))],
                    anchors=(self.A_ROW,))
        s2 = screen([P(author=None, time="3 hour(s) ago",
                       body=(self.A_ROW, "另一行完全不同的正文内容做锚点"))],
                    anchors=(self.A_ROW, "另一行完全不同的正文内容做锚点"))
        s3 = screen([P(author="小雨", time="2 day(s) ago",
                       body=(self.C_ROW,))],
                    anchors=(self.C_ROW,))
        state, capture, scan_fn, press = make_io([s1, s2, s3])
        r = walk(press_down=press, capture=capture, scan_fn=scan_fn,
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP)
        self.assertTrue(r["stop_reason"].startswith("boundary"))
        self.assertEqual(r["landings"], 3)
        self.assertEqual(r["presses"], 2)
        self.assertEqual(len(r["ledger"]["posts"]), 2)
        self.assertEqual(r["ledger"]["posts"][0]["landings"], [1, 2])
        self.assertEqual(r["ledger"]["sightings"], [])
        self.assertEqual(r["events"][1]["overlap"], 1)   # the sub-screen step

    def test_fly_landing_becomes_sighting(self):
        # a true fly (zero anchor overlap — the jump's block-bottom
        # alignment shows only rows never seen before): the screen has NO
        # evidence of whose post this is, so the honest outcome is a
        # sighting (身份待定), and the walk keeps going to the boundary
        s1 = screen([P(author="阿明", time="3 hour(s) ago",
                       body=(self.A_ROW,))],
                    anchors=(self.A_ROW,))
        s2 = screen([P(author=None, time="3 hour(s) ago",
                       body=("老友重聚爬山看日出的经历记录",))],
                    anchors=("老友重聚爬山看日出的经历记录",))
        s3 = screen([P(author="小雨", time="2 day(s) ago",
                       body=(self.C_ROW,))],
                    anchors=(self.C_ROW,))
        state, capture, scan_fn, press = make_io([s1, s2, s3])
        r = walk(press_down=press, capture=capture, scan_fn=scan_fn,
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP)
        self.assertTrue(r["stop_reason"].startswith("boundary"))
        self.assertEqual(r["landings"], 3)
        # the fly landing is a sighting, not a phantom entry
        self.assertEqual(len(r["ledger"]["posts"]), 2)
        self.assertEqual(r["ledger"]["posts"][0]["author"], "阿明")
        self.assertEqual(r["ledger"]["posts"][0]["landings"], [1])
        self.assertEqual(len(r["ledger"]["sightings"]), 1)
        self.assertEqual(r["ledger"]["sightings"][0]["landing"], 2)
        self.assertEqual(r["events"][1]["overlap"], 0)   # the fly

    def test_stuck_on_dead_presses(self):
        s1 = screen([P(time="3 hour(s) ago")],
                    anchors=("阿明一帖的正文行内容足够长当锚点",))
        state, capture, scan_fn, press = make_io([s1])
        r = walk(press_down=press, capture=capture, scan_fn=scan_fn,
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP)
        self.assertEqual(r["stop_reason"], "stuck")
        self.assertEqual(r["presses"], 2)          # stops BEFORE a 3rd press

    def test_runaway_on_consecutive_blind_flies(self):
        # image-wall screens: pairwise-DISSIMILAR anchor rows (a one-char
        # template variant would fuzzy-overlap at 0.93 and never fly) →
        # zero overlap, top zone missing (盲飞) three landings in a row →
        # runaway. 2026-11-16 起 runaway 只数盲飞：对齐块底的合法锚定跳
        # 有据可跨不算嫌疑（真机两轮在深区长帖区被合法跳误刹）。
        rows = ("旅行攻略云南七日自由行游记分享",
                "钢琴曲月光奏鸣曲的演奏视频分享",
                "城市更新报道老楼加电梯工程纪实",
                "深夜食堂剧评孤独美食家的人生")
        screens = []
        for r in rows:
            s = screen([P(author=None, time="5 hour(s) ago", body=(r,))],
                       anchors=(r,))
            s["viewport"]["top"]["zone"] = "none"      # 顶区缺失 = 盲飞
            screens.append(s)
        state, capture, scan_fn, press = make_io(screens)
        r = walk(press_down=press, capture=capture, scan_fn=scan_fn,
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP)
        self.assertEqual(r["stop_reason"], "runaway")
        self.assertEqual(r["presses"], 3)
        # every unclaimed head-cut screen became a sighting
        self.assertEqual(len(r["ledger"]["sightings"]), 4)

    def test_legal_anchored_jumps_do_not_runaway(self):
        # 同样的图墙，但顶沿续接区在（对齐块底=合法锚定跳）→ 不计嫌疑，
        # 不刹车；末屏重复后由死脚收场（证明不是靠运气活着）。
        rows = ("旅行攻略云南七日自由行游记分享",
                "钢琴曲月光奏鸣曲的演奏视频分享",
                "城市更新报道老楼加电梯工程纪实",
                "深夜食堂剧评孤独美食家的人生")
        screens = [screen([P(author=None, time="5 hour(s) ago", body=(r,))],
                          anchors=(r,)) for r in rows]
        state, capture, scan_fn, press = make_io(screens)
        r = walk(press_down=press, capture=capture, scan_fn=scan_fn,
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP)
        self.assertEqual(r["stop_reason"], "stuck")
        self.assertEqual(r["landings"], 6)   # 4 屏各跳一次 + 2 屏死脚

    def test_scan_retry_recovers(self):
        s1 = screen([P(time="3 hour(s) ago")],
                    anchors=("阿明一帖的正文行内容足够长当锚点",))
        state, capture, scan_fn, press = make_io([None, s1])
        r = walk(press_down=press, capture=capture, scan_fn=scan_fn,
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP,
                 max_landings=3)
        # one retry, then the landing lands; the exhausted tail repeats
        # s1 → dead-press stop. The retry must not consume a landing.
        self.assertEqual(r["events"][0]["landing"], 1)
        self.assertEqual(r["stop_reason"], "stuck")

    def test_scan_failed_after_consecutive_failures(self):
        state, capture, scan_fn, press = make_io([None, None])
        r = walk(press_down=press, capture=capture, scan_fn=scan_fn,
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP)
        self.assertEqual(r["stop_reason"], "scan-failed")
        self.assertEqual(r["landings"], 0)

    def test_ledger_persists_and_recognizes(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "ledger.json")
        s1 = screen([P(author="阿明", time="3 hour(s) ago",
                       body=(self.A_ROW,))],
                    anchors=(self.A_ROW,))
        s2 = screen([P(author="小雨", time="1 hour(s) ago",
                       body=(self.B_ROW,))],
                    current=0, anchors=(self.A_ROW, self.B_ROW))
        state, capture, scan_fn, press = make_io([s1, s2])
        r = walk(press_down=press, capture=capture, scan_fn=scan_fn,
                 cfg={"own_names": OWN}, ledger_path=path, log=QUIET,
                 sleep=NOSLEEP, max_landings=2)
        self.assertEqual(r["stop_reason"], "max-landings")
        with open(path, encoding="utf-8") as f:
            persisted = f.read()
        self.assertIn("阿明", persisted)
        # second walk, same ledger: 小雨's screen must read as KNOWN
        state, capture, scan_fn, press = make_io([s2])
        r2 = walk(press_down=press, capture=capture, scan_fn=scan_fn,
                  cfg={"own_names": OWN}, ledger_path=path, log=QUIET,
                  sleep=NOSLEEP, max_landings=1)
        self.assertEqual(r2["events"][0]["reason"], "known")
        self.assertEqual(len(r2["ledger"]["posts"]), 2)   # no duplicate


def PA(author="阿明", time="3 hour(s) ago", body=None,
       button=(0.9, 0.5), arrow=False):
    """An actionable-shaped post: doorplate + time row + button slots."""
    p = P(author=author, time=time,
          body=body or (TestWalk.A_ROW,), arrow=arrow)
    if button:
        p["button_x"], p["button_y"] = button
    return p


class TestActHook(unittest.TestCase):
    """The v2 处置 hook: walker decides WHO (schema slots + ledger
    verdicts), the hook does HOW (IO); one action per landing, acted
    posts never re-act, consecutive failures stop the walk."""

    def test_act_called_once_then_ledger_marks(self):
        s1 = screen([PA()], anchors=(TestWalk.A_ROW,))
        s2 = screen([PA(author="小雨", time="2 hour(s) ago",
                        body=(TestWalk.B_ROW,))],
                    anchors=(TestWalk.B_ROW,))
        calls = []

        def act(p):
            calls.append(p["author"])
            return {"status": "ok", "did": "entered"}

        state, capture, scan_fn, press = make_io([s1, s2])
        r = walk(press_down=press, capture=capture, scan_fn=scan_fn,
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP,
                 act_fn=act, max_landings=2)
        # one action PER landing: landing 1 处置 阿明, landing 2 处置 小雨
        # (two different posts — both are the first actionable on their
        # own landing; the once-only rule is per POST, tested below)
        self.assertEqual(calls, ["阿明", "小雨"])
        by_author = {e["author"]: e for e in r["ledger"]["posts"]}
        self.assertTrue(by_author["阿明"]["acted"])
        self.assertEqual(by_author["阿明"]["act_result"]["status"], "ok")
        self.assertTrue(by_author["小雨"]["acted"])
        self.assertEqual(r["presses"], 2)      # walk continues after acting

    def test_predicates_block_every_non_actionable(self):
        s1 = screen([
            P(author=None, body=(TestWalk.A_ROW,)),       # head-cut
            P(author="Grant Pan", time="1 hour(s) ago",
              body=("这是我自己发的帖子正文内容够长锚",)),   # own
            P(arrow=True, time="2 hour(s) ago"),           # ad
            P(author="小雨", time=None),                   # time not revealed
            PA(author="老周", button=None),                # button missing
            PA(author="老郑", time="Nov 14"),              # unparseable age
            PA(author="老孙", time="2 day(s) ago"),        # ≥2d
        ], anchors=(TestWalk.A_ROW,))
        calls = []

        def act(p):
            calls.append(p["author"])
            return {"status": "ok"}

        state, capture, scan_fn, press = make_io([s1])
        walk(press_down=press, capture=capture, scan_fn=scan_fn,
             cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP,
             act_fn=act, max_landings=1)
        self.assertEqual(calls, [])

    def test_acted_post_never_reacts_on_re_sight(self):
        s1 = screen([PA()], anchors=(TestWalk.A_ROW,))
        s2 = screen([PA()], anchors=(TestWalk.A_ROW,))   # same post again
        calls = []

        def act(p):
            calls.append(p["author"])
            return {"status": "ok"}

        state, capture, scan_fn, press = make_io([s1, s2])
        walk(press_down=press, capture=capture, scan_fn=scan_fn,
             cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP,
             act_fn=act, max_landings=2)
        self.assertEqual(calls, ["阿明"])   # ledger marks, no second act

    def test_failed_act_retries_next_landing_not_same_screen(self):
        # 用户令（2026-11-16 深夜）：失败要重试不跳过——失败不封账，
        # 下落地重新处置；但同一屏不原地重试（tried 集：屏没变，重试
        # 只会原样再败且烧光 act_fails 配额）。
        s1 = screen([PA()], anchors=(TestWalk.A_ROW,))
        s2 = screen([PA()], anchors=(TestWalk.A_ROW,))   # same post again
        calls = []

        def act(p):
            calls.append(p["author"])
            return {"status": "stale-screen"}

        state, capture, scan_fn, press = make_io([s1, s2])
        r = walk(press_down=press, capture=capture, scan_fn=scan_fn,
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP,
                 act_fn=act, max_landings=2)
        self.assertEqual(calls, ["阿明", "阿明"])   # 落地 2 重试
        by_author = {e["author"]: e for e in r["ledger"]["posts"]}
        self.assertFalse(by_author["阿明"].get("acted"))  # 不封账

    def test_failed_act_no_same_screen_refire(self):
        # 单落地单帖失败：只处置一次（tried 集挡住 while 第二遍的原地重试）
        s1 = screen([PA()], anchors=(TestWalk.A_ROW,))
        calls = []

        def act(p):
            calls.append(p["author"])
            return {"status": "stale-screen"}

        state, capture, scan_fn, press = make_io([s1])
        walk(press_down=press, capture=capture, scan_fn=scan_fn,
             cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP,
             act_fn=act, max_landings=1)
        self.assertEqual(calls, ["阿明"])

    def test_two_consecutive_failures_stop_the_walk(self):
        s1 = screen([PA()], anchors=(TestWalk.A_ROW,))
        s2 = screen([PA(author="小雨", time="2 hour(s) ago",
                        body=(TestWalk.B_ROW,))],
                    anchors=(TestWalk.B_ROW,))
        state, capture, scan_fn, press = make_io([s1, s2])
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as f:
            path = f.name
        r = walk(press_down=press, capture=capture, scan_fn=scan_fn,
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP,
                 act_fn=lambda p: {"status": "entry-failed"},
                 ledger_path=path)
        self.assertEqual(r["stop_reason"], "act-failed")
        self.assertEqual(r["presses"], 1)   # 2nd failure stops BEFORE press
        # the stop landing's act marks must be ON DISK (live 11-15: an
        # act-failed stop lost the last landing's 处置 mark)
        import json as _json
        with open(path, encoding="utf-8") as f:
            on_disk = _json.load(f)
        by_author = {e["author"]: e for e in on_disk["posts"]}
        # 用户令（2026-11-16 深夜）：失败不封账——下轮要重试
        self.assertFalse(by_author["小雨"].get("acted"))
        self.assertEqual(by_author["小雨"]["act_result"]["status"],
                         "entry-failed")

    def test_soft_results_never_kill_the_walk(self):
        # expanded-no-button (honest unreachable: the expansion pushed the
        # •• below the fold) is recorded + never retried, but two in a row
        # must NOT trip act-failed (live 2026-11-15: two adjacent tall
        # posts stopped the walk before the soft rule existed)
        s1 = screen([PA()], anchors=(TestWalk.A_ROW,))
        s2 = screen([PA(author="小雨", time="2 hour(s) ago",
                        body=(TestWalk.B_ROW,))],
                    anchors=(TestWalk.B_ROW,))
        state, capture, scan_fn, press = make_io([s1, s2])
        r = walk(press_down=press, capture=capture, scan_fn=scan_fn,
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP,
                 act_fn=lambda p: {"status": "expanded-no-button",
                                   "soft": True},
                 max_landings=2)
        self.assertEqual(r["stop_reason"], "max-landings")   # kept walking
        self.assertEqual(r["presses"], 2)
        # expanded-no-button 是硬失败（soft 只是不计失败数）——按用户令
        # 不封账，下落地重试（按钮露出后自然可回）
        self.assertFalse(any(e.get("acted") for e in r["ledger"]["posts"]))
        self.assertTrue(all(
            e["act_result"]["status"] == "expanded-no-button"
            for e in r["ledger"]["posts"]))

    def test_deferred_keeps_post_reactable(self):
        # expanded-tail-deferred: the read happened but the reply waits
        # for the tail landing — the entry must NOT be marked acted, or
        # the reply is orphaned forever
        s1 = screen([PA()], anchors=(TestWalk.A_ROW,))
        s2 = screen([PA()], anchors=(TestWalk.A_ROW,))   # same post again
        calls = []

        def act(p):
            calls.append(p["author"])
            return {"status": "expanded-tail-deferred", "soft": True,
                    "deferred": True}

        state, capture, scan_fn, press = make_io([s1, s2])
        r = walk(press_down=press, capture=capture, scan_fn=scan_fn,
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP,
                 act_fn=act, max_landings=2)
        self.assertEqual(calls, ["阿明", "阿明"])       # re-acted, not locked
        self.assertFalse(r["ledger"]["posts"][0].get("acted"))

    def test_tall_post_full_chain_doorplate_then_tail(self):
        # THE tall-post reply: doorplate landing expands (deferred), the
        # walker folds the post-act screen rows into identity, the tail
        # landing (head-cut, time row + button) is claimed via the ledger
        # and the reply lands there
        FULL = "这是长文的续段内容足够长可以当身份锚点用"
        # D2 门（2026-11-17）：尾屏认领只认 armed tail_pending——门牌
        # 落地按真机制挂账（帖底越缘、控件未露面）
        head = P(author="阿明", body=(TestWalk.A_ROW,))
        head["bottom_y"] = 1.0
        s1 = screen([head], anchors=(TestWalk.A_ROW,))
        # post-act screen: the expanded view shows the preview head PLUS
        # the continuation (real expansion keeps the preview rows visible)
        s1b = screen([P(author=None, body=(TestWalk.A_ROW, FULL))],
                     anchors=(TestWalk.A_ROW, FULL))
        tail = P(author=None, time="2 hour(s) ago", body=(FULL,))
        tail["button_x"], tail["button_y"] = 0.9, 0.5
        s2 = screen([tail], anchors=(FULL,))               # tail: claimed
        calls = []

        def act(p):
            calls.append(p.get("author") or f"tail:{(p.get('time_row') or {}).get('text')}")
            if len(calls) == 1:
                return {"status": "expanded-tail-deferred", "soft": True,
                        "deferred": True}
            return {"status": "ok", "sent": True}

        state, capture, scan_fn, press = make_io([s1, s1b, s2])
        r = walk(press_down=press, capture=capture, scan_fn=scan_fn,
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP,
                 act_fn=act, max_landings=4)
        self.assertEqual(len(calls), 2)            # deferred + tail reply
        e = r["ledger"]["posts"][0]
        self.assertTrue(e["acted"])                # the tail landing marks
        self.assertEqual(e["act_result"]["status"], "ok")
        self.assertEqual(r["stop_reason"], "max-landings")

    def test_headcut_tail_of_ad_is_blocked(self):
        # a head-cut tail claimed by a SKIPPED-AD entry must never act —
        # the arrow marker lives at the doorplate, invisible at the tail;
        # the ledger status is the only guard
        s1 = screen([P(author="广告君", time="2 hour(s) ago",
                       body=(TestWalk.B_ROW,), arrow=True)],
                    anchors=(TestWalk.B_ROW,))
        s2 = screen([P(author=None, time="1 hour(s) ago",
                       body=(TestWalk.B_ROW,))],
                    anchors=(TestWalk.B_ROW,))
        calls = []

        def act(p):
            calls.append(p["author"])
            return {"status": "ok"}

        state, capture, scan_fn, press = make_io([s1, s2])
        walk(press_down=press, capture=capture, scan_fn=scan_fn,
             cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP,
             act_fn=act, max_landings=2)
        self.assertEqual(calls, [])    # doorplate skip + tail block

    def test_two_actionable_on_one_landing_both_act(self):
        # live 2026-11-15: one-action-per-landing stranded Gracie behind
        # Vicky.L on the same screen — the next ↓ flew past her. Same
        # screen acts ALL actionable posts before the single press
        s1 = screen([PA(), PA(author="小雨", time="1 hour(s) ago",
                              body=(TestWalk.B_ROW,))],
                    anchors=(TestWalk.A_ROW, TestWalk.B_ROW))
        calls = []

        def act(p):
            calls.append(p["author"])
            return {"status": "ok"}

        state, capture, scan_fn, press = make_io([s1])
        r = walk(press_down=press, capture=capture, scan_fn=scan_fn,
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP,
                 act_fn=act, max_landings=1)
        self.assertEqual(calls, ["阿明", "小雨"])       # both, same landing
        self.assertEqual(r["presses"], 1)               # ONE press after

    def test_deferred_ends_the_landings_acting(self):
        # a deferred post stays un-acted in the ledger — the landing's
        # multi-act loop must stop on it, or it re-fires forever
        s1 = screen([PA()], anchors=(TestWalk.A_ROW,))
        calls = []

        def act(p):
            calls.append(p["author"])
            return {"status": "expanded-tail-deferred", "soft": True,
                    "deferred": True}

        state, capture, scan_fn, press = make_io([s1])
        r = walk(press_down=press, capture=capture, scan_fn=scan_fn,
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP,
                 act_fn=act, max_landings=1)
        self.assertEqual(calls, ["阿明"])               # exactly once
        self.assertEqual(r["presses"], 1)

    def test_deferred_reply_rides_to_the_tail_landing(self):
        # cross-process handoff: the deferred reply lives in the ledger's
        # act_result; the tail landing's act target carries it back
        FULL = "长文续段内容足够长可以当身份锚点用吧"
        # D2 门（2026-11-17）：无门牌落地只认 armed tail_pending——
        # 夹具按真机制模拟：门牌落地时帖底越缘、控件未露面 → 挂账
        head = P(author="阿明", body=(TestWalk.A_ROW,))
        head["bottom_y"] = 1.0
        s1 = screen([head], anchors=(TestWalk.A_ROW,))
        s1b = screen([P(author=None, body=(TestWalk.A_ROW, FULL))],
                     anchors=(TestWalk.A_ROW, FULL))
        tail = P(author=None, time="1 hour(s) ago", body=(FULL,))
        tail["button_x"], tail["button_y"] = 0.9, 0.5
        s2 = screen([tail], anchors=(FULL,))
        seen = []

        def act(p):
            seen.append(dict(p))
            if len(seen) == 1:
                return {"status": "expanded-tail-deferred", "soft": True,
                        "deferred": True,
                        "reply": "生成好的回复",
                        "full_body": [TestWalk.A_ROW, FULL]}
            return {"status": "ok", "sent": True}

        state, capture, scan_fn, press = make_io([s1, s1b, s2])
        r = walk(press_down=press, capture=capture, scan_fn=scan_fn,
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP,
                 act_fn=act, max_landings=4)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[1].get("pending_reply"), "生成好的回复")
        self.assertEqual(seen[1].get("pending_full_body"),
                         [TestWalk.A_ROW, FULL])
        e = r["ledger"]["posts"][0]
        self.assertTrue(e["acted"])          # the tail delivery marks it
        self.assertEqual(e["act_result"]["status"], "ok")


class TestTailPending(unittest.TestCase):
    """纯照片尾认领（2026-11-15 James Guan 12图帖漏回）：门牌帖跨过屏幕
    底缘、时间/•• 未露面 → entry 挂 tail_pending；下一落地顶沿的无门牌
    无文字尾巴凭账认领。飞跳清账不许冒领；回完即摘账。"""

    def test_photo_tail_claimed_and_acted(self):
        # landing1: James 式门牌帖——正文一行，12 图把时间/•• 顶出折叠线
        head = P(author="阿詹", time=None, body=(TestWalk.A_ROW,))
        head["bottom_y"] = 1.0
        s1 = screen([head], anchors=(TestWalk.A_ROW,), bottom=1.0)
        # landing2: 纯照片尾——无门牌；body 里只有时间行噪声（真机 20:41
        # 实扫：OCR 把 "32 minute(s) ago" 并进正文区，17 字非身份线索），
        # 时间+•• 齐；顶沿续接 = 对齐块底的合法锚定跳
        tail = P(author=None, time="32 minute(s) ago",
                 body=("32 minute（s） ago",))
        tail["top_y"] = 0.0
        tail["bottom_y"] = 0.45
        tail["button_x"], tail["button_y"] = 0.913, 0.35
        s2 = screen([tail], anchors=(), bottom=0.9)
        seen = []

        def act(p):
            seen.append(dict(p))
            return {"status": "ok", "sent": True}

        state, capture, scan_fn, press = make_io([s1, s2])
        r = walk(press_down=press, capture=capture, scan_fn=scan_fn,
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP,
                 act_fn=act, max_landings=2)
        self.assertEqual(len(seen), 1)
        # 帐本身份与门牌落地的正文行随 target 交接给 hook（尾屏的时间行
        # 噪声会被 identify 并进 anchors——真机同形，无害随行）
        self.assertEqual(seen[0].get("entry_author"), "阿詹")
        self.assertEqual(seen[0].get("entry_body"),
                         [TestWalk.A_ROW, "32 minute(s) ago"])
        e = r["ledger"]["posts"][0]
        self.assertEqual(e["author"], "阿詹")
        self.assertTrue(e["acted"])
        # 尾屏已见时间+•• 且已回完 → 摘账（post-act identify 完成）
        self.assertIsNone(e.get("tail_pending"))

    def test_blind_fly_clears_the_arm(self):
        # 盲飞（顶上不是续接区）：上一底缘跨越者的尾巴被甩掉了——
        # 挂账必须清掉，屏幕顶的陌生尾巴不许冒领
        head = P(author="阿詹", time=None, body=(TestWalk.A_ROW,))
        head["bottom_y"] = 1.0
        s1 = screen([head], anchors=(TestWalk.A_ROW,), bottom=1.0)
        tail = P(author=None, time="32 minute(s) ago", body=())
        tail["top_y"] = 0.0
        tail["button_x"], tail["button_y"] = 0.913, 0.35
        s2 = screen([tail], anchors=("完全无关的陌生屏幕锚点行",), bottom=0.9)
        s2["viewport"]["top"]["zone"] = "none"     # 顶区缺失 = 盲飞
        seen = []

        def act(p):
            seen.append(dict(p))
            return {"status": "ok"}

        state, capture, scan_fn, press = make_io([s1, s2])
        r = walk(press_down=press, capture=capture, scan_fn=scan_fn,
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP,
                 act_fn=act, max_landings=2)
        self.assertEqual(seen, [])                 # 不许照着挂账乱处置
        # 挂账被飞跳清掉；尾巴成了无主 sighting，而不是被冒领的处置
        self.assertEqual(len(r["ledger"]["posts"]), 1)
        self.assertIsNone(r["ledger"]["posts"][0].get("tail_pending"))
        self.assertEqual(len(r["ledger"]["sightings"]), 1)

    def test_complete_unacted_keeps_arm(self):
        # identify 先于 act 跑：完整（时间+•• 齐）但未回完的帖保留挂账，
        # 本落地的认领尺还要用它；回完（acted）后同屏再认 → 摘账
        led = empty_ledger()
        head = P(author="阿詹", time=None, body=(TestWalk.A_ROW,))
        head["bottom_y"] = 1.0
        identify(scan_of([head]), led, {}, landing=1)
        self.assertTrue(led["posts"][0].get("tail_pending"))
        full = P(author="阿詹", time="1 hour(s) ago", body=(TestWalk.A_ROW,))
        full["button_x"], full["button_y"] = 0.9, 0.5
        identify(scan_of([full]), led, {}, landing=2)
        self.assertTrue(led["posts"][0].get("tail_pending"))
        led["posts"][0]["acted"] = True
        identify(scan_of([full]), led, {}, landing=3)
        self.assertIsNone(led["posts"][0].get("tail_pending"))

    def test_armed_entry_folds_full_body_beyond_3_cap(self):
        # 2026-11-17 James 梅岭帖尾屏拒认定案：第四尺/模糊尺拿"尾屏行
        # vs 锚"当认领证据，而尾行只在门牌/回攀落地全帖入镜——挂账
        # entry 折叠帽放宽到 MAX_ANCHORS_ARMED(12)，未挂账照旧 3
        body8 = tuple(f"梅岭古道第{n}句正文行足够长可以充当锚"
                      for n in range(8))
        led = empty_ledger()
        p = P(author="James Guan", time="15 hour(s) ago", body=body8)
        p["bottom_y"] = 0.99                        # 跨越底缘 → 挂账
        identify(scan_of([p]), led, {}, landing=1)
        e = led["posts"][0]
        self.assertTrue(e.get("tail_pending"))
        self.assertEqual(len(e["anchors"]), 3)      # create 路仍是 3 行帽
        identify(scan_of([p]), led, {}, landing=2)  # 重见：折满 8 行
        self.assertEqual(len(e["anchors"]), 8)
        led2 = empty_ledger()
        p2 = P(author="张三", time="3 hour(s) ago", body=body8)
        identify(scan_of([p2]), led2, {}, landing=1)
        identify(scan_of([p2]), led2, {}, landing=2)
        self.assertEqual(len(led2["posts"][0]["anchors"]), 3)   # 未挂账照旧


class TestMultiArmTailClaim(unittest.TestCase):
    """双挂账互锁解法（2026-11-17 深夜真机 00:31 轮定案）：一轮多次展开
    延后是常态——胡吟+费荔思两条挂账并存时第四尺"单挂账直认"前提失效，
    两条全拒认（互锁），两跳尾屏全饿死、整轮零送达。多挂账消歧：文字
    重叠唯一胜出者（≥BODY_FUZZY 且严格大于次名）认领；无文字、无人过
    线 = 诚实拒绝成 sighting，宁漏勿错送。"""

    def _two_arm_ledger(self):
        led = empty_ledger()
        for author, row, reply in (
                ("阿明", TestWalk.A_ROW, "给阿明的回复"),
                ("阿华", TestWalk.B_ROW, "给阿华的回复")):
            led["posts"].append({
                "key": f"{author}|2 hour(s) ago", "author": author,
                "time_text": "2 hour(s) ago", "body_head": row,
                "anchors": [row], "status": "pending",
                "landings": [0], "seen_at": _time.time(),
                "tail_pending": True,
                "act_result": {"status": "expanded-tail-deferred",
                               "reply": reply, "full_body": [row]}})
        return led

    def _write(self, led):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            import json as _json
            _json.dump(led, f, ensure_ascii=False)
        self.addCleanup(os.unlink, path)
        return path

    def _tail(self, body):
        t = P(author=None, time="1 hour(s) ago", body=body)
        t["top_y"], t["bottom_y"] = 0.0, 0.45
        t["button_x"], t["button_y"] = 0.913, 0.35
        return t

    def test_unique_text_winner_claims_and_delivers(self):
        led = self._two_arm_ledger()
        s1 = screen([self._tail((TestWalk.A_ROW,))],
                    anchors=(TestWalk.A_ROW,))
        seen = []

        def act(p):
            seen.append(dict(p))
            return {"status": "ok", "sent": True}

        state, capture, scan_fn, press = make_io([s1])
        r = walk(press_down=press, capture=capture, scan_fn=scan_fn,
                 ledger_path=self._write(led),
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP,
                 act_fn=act, max_landings=1)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].get("entry_author"), "阿明")
        self.assertEqual(seen[0].get("pending_reply"), "给阿明的回复")
        by = {e["author"]: e for e in r["ledger"]["posts"]}
        self.assertTrue(by["阿明"]["acted"])               # 送达封账
        self.assertIsNone(by["阿明"].get("tail_pending"))  # 回完摘账
        # 轮起手清账（2026-11-17 军团治理）：阿华的旗在轮起手被清掉、
        # 本轮未跨越不重挂——投递靠模糊尺+精确绑定路，不靠旗
        self.assertIsNone(by["阿华"].get("tail_pending"))
        self.assertEqual(by["阿华"]["act_result"]["reply"], "给阿华的回复")

    def test_two_arms_textless_tail_refused(self):
        # 互锁本体（00:31 轮形状）：两条挂账 + 无文字尾屏 → 谁也不许认领，
        # 落 sighting 等下一轮，不冒送
        led = self._two_arm_ledger()
        s1 = screen([self._tail(())], anchors=())
        seen = []

        def act(p):
            seen.append(dict(p))
            return {"status": "ok", "sent": True}

        state, capture, scan_fn, press = make_io([s1])
        r = walk(press_down=press, capture=capture, scan_fn=scan_fn,
                 ledger_path=self._write(led),
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP,
                 act_fn=act, max_landings=1)
        self.assertEqual(seen, [])
        self.assertEqual(len(r["ledger"]["sightings"]), 1)
        by = {e["author"]: e for e in r["ledger"]["posts"]}
        # 轮起手清账（军团治理）：拒认后旗子不存活——本轮未跨越不重挂
        self.assertIsNone(by["阿明"].get("tail_pending"))
        self.assertIsNone(by["阿华"].get("tail_pending"))

    def test_two_arms_foreign_text_refused(self):
        # 有文字但两条都不过线（第三者的行）→ 同样诚实拒绝
        led = self._two_arm_ledger()
        ROW = "老周第三条完全不同的正文内容行"
        s1 = screen([self._tail((ROW,))], anchors=(ROW,))
        seen = []

        def act(p):
            seen.append(dict(p))
            return {"status": "ok", "sent": True}

        state, capture, scan_fn, press = make_io([s1])
        r = walk(press_down=press, capture=capture, scan_fn=scan_fn,
                 ledger_path=self._write(led),
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP,
                 act_fn=act, max_landings=1)
        self.assertEqual(seen, [])
        self.assertEqual(len(r["ledger"]["sightings"]), 1)
        by = {e["author"]: e for e in r["ledger"]["posts"]}
        # 轮起手清账（军团治理）：拒认后旗子不存活
        self.assertIsNone(by["阿明"].get("tail_pending"))
        self.assertIsNone(by["阿华"].get("tail_pending"))

    def _zombie_ledger(self):
        """挂账军团（2026-11-17 真机账）：22 条滞留挂账、其中多条早已
        acted（摘账只在 acted+完整重见时发生，轮次一断就永久滞留）。
        acted 条目不需要投递，留在 armed 集里只会污染单臂/唯一胜者判定。"""
        led = empty_ledger()
        for author in ("僵尸一", "僵尸二"):
            led["posts"].append({
                "key": f"{author}|2 day(s) ago", "author": author,
                "time_text": "2 day(s) ago", "body_head": f"{author}的老帖",
                "anchors": [f"{author}的老帖正文行内容足够长可作锚"],
                "status": "pending", "landings": [0],
                "seen_at": _time.time(), "tail_pending": True,
                "acted": True, "act_result": {"status": "ok"}})
        led["posts"].append({
            "key": "活口|1 hour(s) ago", "author": "活口",
            "time_text": "1 hour(s) ago", "body_head": "活口的待投帖",
            "anchors": ["活口的待投帖正文行内容足够长可作锚"],
            "status": "pending", "landings": [0], "seen_at": _time.time(),
            "tail_pending": True,
            "act_result": {"status": "expanded-tail-deferred",
                           "reply": "给活口的回复", "full_body": ["活口"]}})
        return led

    def test_acted_zombies_filtered_single_arm_claims(self):
        # 无文字照片尾 + 军团里只剩一个未回挂账：acted 僵尸被过滤后
        # len==1 直认路复活（旧行为：3 条挂账互锁全拒）
        led = self._zombie_ledger()
        tail = self._tail(())
        self.assertIs(_match_entry(tail, led),
                      led["posts"][2])           # 认领唯一未回挂账

    def test_tail_rows_in_anchors_claim_via_fuzzy_ruler(self):
        # James 链全形（2026-11-17）：门牌落地全帖 8 行入锚（折叠帽放宽）
        # 后，尾屏行 vs 锚 ~1.0 经第二尺直接认领——军团在场也拦不住
        led = self._zombie_ledger()
        head_row = "梅关古道唐时修的国道宽可行四乘之车走上关楼"
        tail_row = "下关到珠玑巷听广府人中原南迁来路满巷祠堂半部南迁史"
        james = {"key": "james guan|?", "author": "James Guan",
                 "time_text": None, "body_head": head_row,
                 "anchors": [head_row, tail_row], "status": "pending",
                 "landings": [17], "seen_at": _time.time(),
                 "tail_pending": True}
        led["posts"].insert(0, james)            # 僵尸排前面也照样认出
        tail = self._tail((tail_row,))
        self.assertIs(_match_entry(tail, led), james)


class TestPhotoTailLabelDoor(unittest.TestCase):
    """时间行门（2026-11-17 Viva 照片尾滞留定案）：贴顶照片尾带时间行时
    ——照片尾唯一能读的文本证据——账上有标签的 armed 条目必须与碎片标
    签严格相等才可认领。真机事故：僵尸挂账（无标签、无回复、旧伤疤）把
    单臂变多臂，照片尾拿不出文字打破僵局 → viva|2h 的挂账回复滞留。本门
    让无标签僵尸在有标签碎片前自动出局；同标签多臂仍需唯一；标签对不上
    =位置证据不足为凭，诚实拒绝（宁漏勿错送）。全无标签（James 式门牌
    落地时间行未露面，标签本就空）没法比，维持旧路（TestTailPending）。"""

    @staticmethod
    def _arm(author, label, row, reply="给她的回复"):
        return {"key": f"{author}|{label or '?'}", "author": author,
                "time_text": label, "body_head": row, "anchors": [row],
                "status": "pending", "landings": [0],
                "seen_at": _time.time(), "tail_pending": True,
                "act_result": {"status": "expanded-tail-deferred",
                               "reply": reply, "full_body": [row]}}

    @staticmethod
    def _tail(label, body=()):
        t = P(author=None, time=label, body=body)
        t["top_y"], t["bottom_y"] = 0.0, 0.45
        t["button_x"], t["button_y"] = 0.913, 0.35
        return t

    def _write(self, led):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            import json as _json
            _json.dump(led, f, ensure_ascii=False)
        self.addCleanup(os.unlink, path)
        return path

    def test_labelless_zombie_filtered_viva_case(self):
        # Viva 事故原形：僵尸（无标签、无回复）+ 真挂账（有标签）并存；
        # 碎片时间行=真挂账标签 → 僵尸出局、真挂账认领（旧码：两臂互锁
        # 全拒，冰激凌回复滞留）
        viva = self._arm("Viva", "2 hour(s) ago",
                         "自制的海盐冰激凌看起来很好吃", reply="冰激凌回复")
        zombie = {"key": "viva|?", "author": "Viva", "time_text": None,
                  "body_head": None, "anchors": ["某个旧伤疤行"],
                  "status": "pending", "landings": [4],
                  "seen_at": _time.time(), "tail_pending": True}
        led = empty_ledger()
        led["posts"].extend([zombie, viva])
        self.assertIs(_match_entry(self._tail("2 hour(s) ago"), led), viva)

    def test_label_mismatch_refuses(self):
        # 碎片标签与挂账标签对不上：位置证据不足为凭（巧合撞车防线）
        led = empty_ledger()
        led["posts"].append(
            self._arm("阿香", "2 hour(s) ago", "阿香的正文行"))
        self.assertIsNone(_match_entry(self._tail("5 hour(s) ago"), led))

    def test_label_unique_winner_among_multi_arm(self):
        # 双臂不同标签：碎片标签唯一命中者认领（照片尾互锁的解法）
        a = self._arm("阿明", "2 hour(s) ago", TestWalk.A_ROW, reply="给阿明")
        h = self._arm("阿华", "5 hour(s) ago", TestWalk.B_ROW, reply="给阿华")
        led = empty_ledger()
        led["posts"].extend([a, h])
        self.assertIs(_match_entry(self._tail("5 hour(s) ago"), led), h)
        self.assertIs(_match_entry(self._tail("2 hour(s) ago"), led), a)

    def test_same_label_ambiguous_refuses(self):
        # 双臂同标签 + 无文字：谁也不许认领（唯一性仍硬性要求）
        a = self._arm("阿明", "2 hour(s) ago", TestWalk.A_ROW)
        h = self._arm("阿华", "2 hour(s) ago", TestWalk.B_ROW)
        led = empty_ledger()
        led["posts"].extend([a, h])
        self.assertIsNone(_match_entry(self._tail("2 hour(s) ago"), led))

    def test_resight_refreshes_time_text_key_frozen(self):
        # 标签=观察值（2026-11-17 15:22 真机二轮滞留定案）：重见把
        # time_text 刷成当前标签；key 冻结首见形态（身份）；abs_time
        # 冻结不回写（A9 承重不变量）
        led = empty_ledger()
        head = P(author="Viva", time="2 hour(s) ago", body=(TestWalk.A_ROW,))
        head["bottom_y"] = 1.0
        identify(scan_of([head]), led, {}, landing=1)
        e = led["posts"][0]
        key0, abs0 = e["key"], e["abs_time"]
        later = P(author="Viva", time="3 hour(s) ago",
                  body=(TestWalk.A_ROW,))
        identify(scan_of([later]), led, {}, landing=13)
        self.assertIs(led["posts"][0], e)
        self.assertEqual(e["time_text"], "3 hour(s) ago")   # 刷新
        self.assertEqual(e["key"], key0)                    # 身份冻结
        self.assertEqual(e["abs_time"], abs0)               # abs 冻结

    def test_tail_claim_survives_label_tick(self):
        # L13→L14 事故链全形：门牌落地重见把账上标签刷到 3h 后，尾屏
        # 碎片（3h）凭时间行门认领成功（旧码：账上冻在 2h → 门拒绝
        # → 冰激凌回复滞留）
        led = empty_ledger()
        head = P(author="Viva", time="2 hour(s) ago", body=(TestWalk.A_ROW,))
        head["bottom_y"] = 1.0
        identify(scan_of([head]), led, {}, landing=1)
        e = led["posts"][0]
        e["act_result"] = {"status": "expanded-tail-deferred",
                           "reply": "冰激凌回复"}
        doorplate = P(author="Viva", time="3 hour(s) ago",
                      body=(TestWalk.A_ROW,))
        identify(scan_of([doorplate]), led, {}, landing=13)
        tail = self._tail("3 hour(s) ago")
        self.assertIs(_match_entry(tail, led), e)

    def test_round_start_disarm_kills_legion_collision(self):
        # 三轮滞留全形（2026-11-17 15:33 真机 r-153345）：账上带军团时，
        # 同标签 impostor（安徽服务管家|4h）与真挂账（viva|2h→4h）撞车，
        # 第四尺两候选歧义拒认 → 冰激凌回复三轮滞留。轮起手清全部
        # tail_pending（B4：挂账=轮内瞬态）后，本轮跨越重挂的真挂账
        # 成为唯一候选 → 认领送达
        led = empty_ledger()
        led["posts"].append({
            "key": "安徽服务管家0127@平安产险服务管家|4 hour(s) ago",
            "author": "安徽服务管家0127@平安产险服务管家",
            "time_text": "4 hour(s) ago", "body_head": "服务号的推广行",
            "anchors": ["服务号的推广行"], "status": "skipped_ad",
            "landings": [3], "seen_at": _time.time(),
            "tail_pending": True})
        led["posts"].append(
            self._arm("Viva", "4 hour(s) ago",
                      "自制的海盐冰激凌看起来很好吃", reply="冰激凌回复"))
        # L1: 门牌落地跨越底缘 → _arm_tail 重挂（本轮内瞬态）
        head = P(author="Viva", time="4 hour(s) ago", body=(TestWalk.A_ROW,))
        head["bottom_y"] = 1.0
        s1 = screen([head], anchors=(TestWalk.A_ROW,), bottom=1.0)
        # L2: 纯照片尾——impostor 已被起手清掉且本轮未跨越，唯一候选
        s2 = screen([self._tail("4 hour(s) ago")], anchors=(), bottom=0.9)
        seen = []

        def act(p):
            seen.append(dict(p))
            return {"status": "ok", "sent": True}

        state, capture, scan_fn, press = make_io([s1, s2])
        r = walk(press_down=press, capture=capture, scan_fn=scan_fn,
                 ledger_path=self._write(led),
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP,
                 act_fn=act, max_landings=2)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].get("entry_author"), "Viva")
        self.assertEqual(seen[0].get("pending_reply"), "冰激凌回复")
        by = {e["author"]: e for e in r["ledger"]["posts"]}
        self.assertTrue(by["Viva"]["acted"])
        # 军团旗起手清掉且本轮未重挂
        self.assertIsNone(
            by["安徽服务管家0127@平安产险服务管家"].get("tail_pending"))


class TestBindHeadless(unittest.TestCase):
    """精确锚绑定（2026-11-17 深夜，D2 身份门的窄门）：无门牌+未挂账
    片段在文字行逐行精确命中同一条 entry 的 anchors 且证据唯一时放行
    ——堵 00:31 轮的另一半漏（胡吟|2h 门牌落在整屏跳跨越区永不再露面，
    文字片段落地时旧门只会 no-doorplate）。L18 冤案形状（混坨模糊认领）
    必须仍被拒：模糊比率不作数、两 entry 证据并存=混块拒、花字破精确
    相等=拒、绑定结果与模糊尺认出者不符=拒。"""

    HU_ROW = "优胜美地的日出把整面花岗岩染成金红色太震撼了"
    FEI_ROW = "一百零八呎的巨浪把冲浪的人抛上天空看得我心惊"

    def _led(self):
        led = empty_ledger()
        led["posts"].append({
            "key": "胡吟|2 hour(s) ago", "author": "胡吟",
            "time_text": "2 hour(s) ago", "body_head": self.HU_ROW,
            "anchors": [self.HU_ROW], "status": "pending",
            "landings": [1], "seen_at": _time.time()})
        led["posts"].append({
            "key": "费荔思|1 hour(s) ago", "author": "费荔思",
            "time_text": "1 hour(s) ago", "body_head": self.FEI_ROW,
            "anchors": [self.FEI_ROW], "status": "pending",
            "landings": [1], "seen_at": _time.time(), "acted": True,
            "act_result": {"status": "ok", "reply": "已发过的回复"}})
        return led

    def _frag(self, body, time="2 hour(s) ago", button=True):
        p = P(author=None, time=time, body=body)
        if button:
            p["button_x"], p["button_y"] = 0.913, 0.5
        return p

    def test_unique_exact_rows_bind_and_act(self):
        led = self._led()
        frag = self._frag((self.HU_ROW,))
        self.assertIs(_bind_headless(frag, led), led["posts"][0])
        # 未挂账 + 精确绑定 → 放行到处置（time+button 齐，1h < 24h）
        self.assertIsNone(_actionable(frag, led, {"own_names": OWN}, 24.0))

    def test_l18_mixed_lump_still_refused(self):
        # 费荔思帖尾 + 胡吟卡顶的混坨：模糊尺会认出胡吟（第二行精确），
        # 精确证据却指向两条 entry = 混块 → 绑定 None → no-doorplate
        led = self._led()
        frag = self._frag((self.FEI_ROW, self.HU_ROW))
        self.assertIsNone(_bind_headless(frag, led))
        self.assertEqual(
            _actionable(frag, led, {"own_names": OWN}, 24.0),
            "no-doorplate")

    def test_fei_only_lump_binds_acted_and_skips(self):
        # L18 坨的另一投影：坨里只有费荔思的行 → 绑到费荔思（已回过）
        # → "acted" 诚实跳过，而不是把她的正文喂给胡吟的组稿
        led = self._led()
        frag = self._frag((self.FEI_ROW,), time="1 hour(s) ago")
        self.assertIs(_bind_headless(frag, led), led["posts"][1])
        self.assertEqual(
            _actionable(frag, led, {"own_names": OWN}, 24.0), "acted")

    def test_garbled_row_refused(self):
        # OCR 花字破坏精确相等：模糊尺认得出（0.9+），绑定不上 → 拒
        led = self._led()
        frag = self._frag((self.HU_ROW[:-1],))
        self.assertIsNone(_bind_headless(frag, led))
        self.assertEqual(
            _actionable(frag, led, {"own_names": OWN}, 24.0),
            "no-doorplate")

    def test_bind_must_confirm_the_same_entry(self):
        # 模糊尺认出胡吟（花字行 0.9+），精确证据却只在费荔思身上
        # → be is not entry → 拒（混块的最后一道闸）
        led = self._led()
        frag = self._frag((self.HU_ROW[:-1], self.FEI_ROW))
        self.assertEqual(
            _actionable(frag, led, {"own_names": OWN}, 24.0),
            "no-doorplate")

    def test_short_rows_are_never_evidence(self):
        # 短行（<BIND_ROW_MIN）不是身份证据：纯照片尾/噪声坨归第四尺与
        # armed 路管，绑定窄门不掺和
        led = self._led()
        frag = self._frag(("真想去", "好美"))
        self.assertIsNone(_bind_headless(frag, led))
        self.assertEqual(
            _actionable(frag, led, {"own_names": OWN}, 24.0),
            "no-doorplate")

    def test_fuzzy_fold_cannot_self_confirm_the_bind(self):
        # 自证循环封死（identify 锚折叠门，2026-11-17 深夜）：花字片段
        # 被模糊尺认领后，其行不许折进未挂账 entry 的 anchors——否则
        # 绑定窄门立刻"精确"命中刚折进来的行，花字洗白成身份证据
        led = self._led()
        garbled = P(author=None, body=(self.HU_ROW[:-1],))
        identify(scan_of([garbled]), led, {}, landing=2)
        self.assertEqual(led["posts"][0]["anchors"],
                         [self.HU_ROW])            # 花字行没折进来
        frag2 = self._frag((self.HU_ROW[:-1],))
        self.assertEqual(
            _actionable(frag2, led, {"own_names": OWN}, 24.0),
            "no-doorplate")                        # 诚实拒认到底

    def test_armed_fold_still_feeds_the_tail_claim(self):
        # 折叠门不许误伤尾屏链：armed entry 的无门牌尾屏行照折——
        # 尾屏第四尺认领全靠它们（test_deferred_reply_rides 的锚料）
        led = self._led()
        led["posts"][0]["tail_pending"] = True     # 胡吟挂账
        tail = P(author=None, time="2 hour(s) ago",
                 body=("步道尽头能俯瞰整个海湾值得再来一次的好路线",))
        tail["top_y"] = 0.0                        # 顶沿尾形状 → 第四尺认领
        identify(scan_of([tail]), led, {}, landing=3)
        self.assertIn("步道尽头能俯瞰整个海湾值得再来一次的好路线",
                      led["posts"][0]["anchors"])  # armed → 折叠放行


class TestTeleportGuard(unittest.TestCase):
    """首按即边界守卫（2026-11-17 晨两轮异常，成因悬置）：置顶后第一
    次↓直落昨天区（锚重叠0），步行器原本当真过界边界停=整轮静默报废。
    成因无法带内区分——微信「上次读到处」回位 or 用户 multi-tasking
    人为滚动（用户自述当时在用机，两轮证据被污染）。守卫只改停走标注
    不改行为：首按+锚重叠0+边界 = teleport-suspect（给人看帧用的）；
    亚屏步渐入的边界照旧 plain boundary（feed 真的只有一屏窗内内容时
    也会一按进界）。"""

    Y_ROW = "完全不同区域的正文行内容足够长"

    def test_first_press_zero_overlap_boundary_is_teleport_suspect(self):
        s1 = screen([P(author="阿明", time=None, body=(TestWalk.A_ROW,))],
                    anchors=(TestWalk.A_ROW,))
        s2 = screen([P(author=None, time="Yesterday", body=(self.Y_ROW,))],
                    anchors=(self.Y_ROW,))
        state, capture, scan_fn, press = make_io([s1, s2])
        r = walk(press_down=press, capture=capture, scan_fn=scan_fn,
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP,
                 max_landings=3)
        self.assertEqual(r["stop_reason"], "teleport-suspect (1st-press boundary)")
        self.assertEqual(r["presses"], 1)

    def test_gradual_step_into_boundary_stays_plain(self):
        # 亚屏步渐入 Yesterday = 真过界（feed 窗内区就这么长），照旧
        s1 = screen([P(author="阿明", time=None, body=(TestWalk.A_ROW,))],
                    anchors=(TestWalk.A_ROW,))
        s2 = screen([P(author=None, time="Yesterday",
                       body=(TestWalk.A_ROW, self.Y_ROW))],
                    anchors=(TestWalk.A_ROW, self.Y_ROW))
        state, capture, scan_fn, press = make_io([s1, s2])
        r = walk(press_down=press, capture=capture, scan_fn=scan_fn,
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP,
                 max_landings=3)
        self.assertTrue(r["stop_reason"].startswith("boundary ("),
                        r["stop_reason"])

    def test_top_screen_itself_boundary_stays_plain(self):
        # landing 1 顶屏即出界（presses=0）= 真没窗内内容，不是瞬移
        s1 = screen([P(author="阿明", time="Yesterday", body=(TestWalk.A_ROW,))],
                    anchors=(TestWalk.A_ROW,))
        state, capture, scan_fn, press = make_io([s1])
        r = walk(press_down=press, capture=capture, scan_fn=scan_fn,
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP,
                 max_landings=2)
        self.assertTrue(r["stop_reason"].startswith("boundary ("),
                        r["stop_reason"])


class TestDriftMerge(unittest.TestCase):
    """相对时间标签漂移并帐（2026-11-15 夜 Samuel Cui/小安 双回复事故）：
    同一照片帖跨轮重见，"6 hour(s) ago"→"7 hour(s) ago" 骗过作者尺的
    时间相等判定 → 两条 entry → 同帖回复两次。修复演进：
    ① 墙钟漂移尺（标签增量 ≈ 墙钟间隔）→ 被并账自毁击穿（identify
      并账刷 seen_at，同落地重匹配全败 → acted 闸被绕过，2026-11-17
      无草 DS 帖三连回复定案）；
    ② 绝对时间合并门（2026-11-17 A9 + 用户裁定"小时级比较、±1h"）：
      两侧各算发布窗上沿 abs = 见到时刻 − 标签（entry.abs_time 创建即
      冻结），|Δabs| ≤ 60min 才并——seen_at 从此与匹配无关，自毁结构
      不可能。只对已回过的帖（并错未回帖会把回复送到错帖）；照片帖
      anchors 全空，abs 门是唯一信号。
    floor 制下同帖两次估值的 |Δabs| 最坏恰好 60min，故测试里 1 小时
    标签差都贴着容差上沿（≤60min 通过）。"""

    def test_acted_entry_drifted_label_merges(self):
        # 真照片帖：body=() 让锚真全空（P 默认正文会让 fixture 带上文字
        # 锚——2026-11-17 亲缘闸落地后，"带文字锚+零亲缘照并"正是无草
        # DS 帖事故行为，docstring 说的 anchors 全空必须当真）
        led = empty_ledger()
        identify(scan_of([P(author="塞缪尔", time="6 hour(s) ago",
                            body=())]),
                 led, {}, landing=1)
        e = led["posts"][0]
        e["acted"] = True
        e["seen_at"] = _time.time() - 1.1 * 3600     # 1.1h 前见过
        # abs 门只读冻结的 abs_time（identify 创建时盖的章），seen_at
        # 是什么值完全无关——merge 与否全看 |Δabs| ≤ 60min
        post = P(author="塞缪尔", time="7 hour(s) ago",
                 body=("完全不同的一行正文也没有用",))
        self.assertIs(_match_entry(post, led), e)

    def test_unacted_entry_never_drift_merges(self):
        led = empty_ledger()
        identify(scan_of([P(author="某人", time="6 hour(s) ago")]),
                 led, {}, landing=1)
        led["posts"][0]["seen_at"] = _time.time() - 1.1 * 3600
        post = P(author="某人", time="7 hour(s) ago",
                 body=("完全不同的一行正文也没有用",))
        self.assertIsNone(_match_entry(post, led))   # 未回过：新帖是新帖

    def test_label_jump_beyond_wall_gap_does_not_merge(self):
        # 标签涨 3h：|Δabs| = 3h 远超 60min 容差，不并（保守——错并会
        # 吞同作者更老的另一帖）。seen_at 摆什么值都不影响判决。
        led = empty_ledger()
        identify(scan_of([P(author="某人", time="6 hour(s) ago")]),
                 led, {}, landing=1)
        e = led["posts"][0]
        e["acted"] = True
        e["seen_at"] = _time.time() - 0.2 * 3600
        post = P(author="某人", time="9 hour(s) ago",
                 body=("完全不同的一行正文也没有用",))
        self.assertIsNone(_match_entry(post, led))

    def test_drift_refuses_same_author_zero_kinship(self):
        # 2026-11-17 无草 DS 帖三连回复事故定案：同作者两条不同帖（新黄河
        # 12h / DS·人民邮电报），真实事故里标签增量 2h 对 0.95h 墙钟照样
        # 过旧漂移门。此处标签取 12h→13h（|Δabs|=60min 贴容差过 abs 门），
        # 让亲缘闸亲自拦：两侧都有行而锚毫无亲缘 = 拒并（弱路同款闸；
        # 事故里并账还自毁：seen_at 刷新让同落地重匹配全败 → acted 闸
        # 被绕过——自毁已由 abs 门根治，另有专测）。
        led = empty_ledger()
        identify(scan_of([P(author="无草", time="12 hour(s) ago",
                            body=("721人住院,有500人夜间不在病房",
                                  "唐山一医院被举报涉嫌虚假住院骗保"))]),
                 led, {}, landing=1)
        e = led["posts"][0]
        e["acted"] = True
        e["seen_at"] = _time.time() - 0.95 * 3600
        post = P(author="无草", time="13 hour(s) ago",
                 body=("自从Anthropic和OpenAI闭关锁国之后全世界所有",
                       "DS的需要明显提升了"))
        self.assertIsNone(_match_entry(post, led))

    def test_drift_accepts_genuine_kinship(self):
        # 真同帖重见：行与锚有亲缘（≥WEAK_AUTHOR_FLOOR）照常并——
        # 亲缘闸只拦"同作者另一条帖"，不伤原设计场景（标签 12h→13h
        # 贴容差过 abs 门，让亲缘层被真测到）
        led = empty_ledger()
        row = "721人住院,有500人夜间不在病房"
        identify(scan_of([P(author="无草", time="12 hour(s) ago",
                            body=(row, "唐山一医院被举报涉嫌虚假住院骗保"))]),
                 led, {}, landing=1)
        e = led["posts"][0]
        e["acted"] = True
        e["seen_at"] = _time.time() - 0.95 * 3600
        post = P(author="无草", time="13 hour(s) ago",
                 body=(row, "这次换了一条新评论行也一起入库"))
        self.assertIs(_match_entry(post, led), e)

    def test_abs_gate_ignores_seen_at_self_destruct(self):
        # 并账自毁的根治证明（2026-11-17 无草 DS 帖定案）：同帖 12h→14h
        # 首次并账成功后 identify 刷 seen_at=now，同落地再次匹配——旧墙
        # 钟尺 |2h−0| > 1.5 拒并（→ 新建重复 entry → acted 闸被绕过 →
        # 重复回复）；abs 门两侧都是冻结量，|Δabs| ≈ 0 照常并。abs_time
        # 取"2h 前冻结于 12h 标签"的真值 now−14h。
        led = empty_ledger()
        identify(scan_of([P(author="塞缪尔", time="12 hour(s) ago",
                            body=())]),
                 led, {}, landing=1)
        e = led["posts"][0]
        e["acted"] = True
        e["abs_time"] = _time.time() - 14 * 3600
        e["seen_at"] = _time.time()          # 刚被同落地首次并账刷新过
        post = P(author="塞缪尔", time="14 hour(s) ago", body=())
        self.assertIs(_match_entry(post, led), e)

    def test_abs_gate_merges_across_long_wall_gap(self):
        # 语义钉：标签随墙钟同步长大，abs 不动——5h 前冻结于 12h 标签，
        # 现在显示 17h，|Δabs| ≈ 0 照并。墙钟隔多久都行（旧尺 |5−5|=0
        # 也能并，此测钉的是 abs 语义本身：合并不依赖 seen_at 新鲜度）。
        led = empty_ledger()
        identify(scan_of([P(author="塞缪尔", time="12 hour(s) ago",
                            body=())]),
                 led, {}, landing=1)
        e = led["posts"][0]
        e["acted"] = True
        e["abs_time"] = _time.time() - 17 * 3600
        e["seen_at"] = _time.time() - 5 * 3600
        post = P(author="塞缪尔", time="17 hour(s) ago", body=())
        self.assertIs(_match_entry(post, led), e)

    def test_today_label_entry_has_no_abs_and_drift_inert(self):
        # "今天 HH:MM" 族不进换算（钟点 parse_time_hours 不解析，硬算
        # 会错几小时）→ abs_time=None → abs 门惰性，同作者重见不并
        # （其余尺照常兜底）。
        led = empty_ledger()
        identify(scan_of([P(author="阿禾", time="今天 09:15", body=())]),
                 led, {}, landing=1)
        e = led["posts"][0]
        self.assertIsNone(e["abs_time"])
        e["acted"] = True
        post = P(author="阿禾", time="13 hour(s) ago", body=())
        self.assertIsNone(_match_entry(post, led))

    def test_identify_stamps_seen_at(self):
        led = empty_ledger()
        before = _time.time()
        identify(scan_of([P(time="3 hour(s) ago")]), led, {}, landing=1)
        self.assertGreaterEqual(led["posts"][0]["seen_at"], before - 1)
        identify(scan_of([P(author=None, time="3 hour(s) ago",
                            body=("公园里人不多风景也很好心情舒畅",))]),
                 led, {}, landing=2)
        self.assertGreaterEqual(led["posts"][0]["seen_at"], before)

    def test_identify_stamps_abs_time_at_create(self):
        # 创建即冻结发布窗上沿（A9）：3h 标签 → abs ≈ now−3h
        led = empty_ledger()
        before = _time.time()
        identify(scan_of([P(author="阿禾", time="3 hour(s) ago")]),
                 led, {}, landing=1)
        e = led["posts"][0]
        self.assertIsNotNone(e["abs_time"])
        self.assertLess(abs((before - 3 * 3600) - e["abs_time"]), 5)

    def test_identify_seeds_abs_time_on_refresh(self):
        # 迁移路：旧 entry（手工造的无 abs 条目）首次重见得标签时补种
        # 一次；已有 abs 的 entry 刷新绝不回写（冻结不变量）
        led = empty_ledger()
        identify(scan_of([P(author="阿禾", time=None)]),
                 led, {}, landing=1)
        e = led["posts"][0]
        self.assertIsNone(e["abs_time"])
        frozen = _time.time() - 8 * 3600
        e["abs_time"] = frozen
        identify(scan_of([P(author="阿禾", time="9 hour(s) ago")]),
                 led, {}, landing=2)
        self.assertEqual(e["abs_time"], frozen)   # 永不回写
        led2 = empty_ledger()
        identify(scan_of([P(author="小北", time=None)]),
                 led2, {}, landing=1)
        before = _time.time()
        identify(scan_of([P(author="小北", time="5 hour(s) ago")]),
                 led2, {}, landing=2)
        e2 = led2["posts"][0]
        self.assertIsNotNone(e2["abs_time"])
        self.assertLess(abs((before - 5 * 3600) - e2["abs_time"]), 5)

    def test_load_ledger_migrates_abs_time(self):
        # 旧账加载补种：abs = seen_at − 标签（必须在首次重见**之前**——
        # 漂移门匹配时就读 abs_time，晚补 = 旧 acted 条目这轮重见已丢
        # 漂移保护）。日历带/无 seen_at 的留空；已有 abs 的不动。
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump({"posts": [
                {"key": "甲|12", "author": "甲",
                 "time_text": "12 hour(s) ago",
                 "seen_at": _time.time() - 2 * 3600, "acted": True,
                 "anchors": []},
                {"key": "乙|y", "author": "乙",
                 "time_text": "1 day(s) ago",
                 "seen_at": _time.time() - 2 * 3600, "anchors": []},
                {"key": "丙|?", "author": "丙", "time_text": None,
                 "anchors": []},
                {"key": "丁|6", "author": "丁",
                 "time_text": "6 hour(s) ago",
                 "seen_at": _time.time() - 3600, "anchors": [],
                 "abs_time": 12345.0},
            ], "sightings": []}, f, ensure_ascii=False)
        self.addCleanup(os.unlink, path)
        led = load_ledger(path)
        p1, p2, p3, p4 = led["posts"]
        self.assertIsNotNone(p1["abs_time"])
        self.assertLess(abs(p1["abs_time"]
                            - (p1["seen_at"] - 12 * 3600)), 5)
        self.assertIsNone(p2.get("abs_time"))    # 日历带（≥24h）不进换算
        self.assertIsNone(p3.get("abs_time"))    # 无 seen_at 不补种
        self.assertEqual(p4["abs_time"], 12345.0)  # 只填 None，不回写

    def test_abs_time_band_edges(self):
        # 小时带两端：23h 仍换算（小时带最后一级）；≥24h（昨天/天）与
        # 缺标签不换算（A6 边界族守卫）
        self.assertAlmostEqual(_abs_time("23 hour(s) ago", 1000.0),
                               1000.0 - 23 * 3600)
        self.assertIsNone(_abs_time("1 day(s) ago", 1000.0))
        self.assertIsNone(_abs_time("Yesterday", 1000.0))
        self.assertIsNone(_abs_time("昨天", 1000.0))
        self.assertIsNone(_abs_time(None, 1000.0))
        # 半小时在小时带内（0.5h），照常换算
        self.assertAlmostEqual(_abs_time("半小时前", 1000.0),
                               1000.0 - 0.5 * 3600)


class TestVerdictFork(unittest.TestCase):
    """判决分叉+裸断修（2026-11-17 DS 三连回复事故收尾）：一轮之内身份
    判决只在 identify 发生一次（盖章 post["_matched"]），decide/
    _actionable/_climb_key/act 记账全部查章（_resolved_entry）——账本
    中途被任何致因改动，判决也不许翻脸（DS 事故：identify 并账刷
    seen_at 后 decide 重算翻 None → acted 闸被绕过）。记账铁律：回复
    已发出 ⇒ 必然入账；旧裸断（发帖后重匹配 None → break，
    act_result/acted/日志全无）是三连回复的最后一环。"""

    def test_identify_stamps_verdict_annotation(self):
        led = empty_ledger()
        s1 = scan_of([P(author="阿明", time="3 hour(s) ago")])
        identify(s1, led, {}, landing=1)
        self.assertIs(s1["posts"][0]["_matched"], led["posts"][0])
        s2 = scan_of([P(author="阿明", time="3 hour(s) ago")])
        identify(s2, led, {}, landing=2)
        self.assertIs(s2["posts"][0]["_matched"], led["posts"][0])
        s3 = scan_of([P(author=None,
                        body=("这行谁也不认得是新鲜孤行的正文内容",))])
        identify(s3, led, {}, landing=3)
        # 身份未决（ sighting）也盖章：None 是判决，不是"没判"
        self.assertIsNone(s3["posts"][0]["_matched"])

    def test_actionable_follows_annotation_not_rematch(self):
        # 致因模拟：条目被摘出账本（任意致因的代理）→ 旧代码重算得
        # None → 已回帖被当新帖处置（acted 闸失效）；新代码认章 → 拦下
        led = empty_ledger()
        s = scan_of([P(author="阿明", time="3 hour(s) ago")])
        identify(s, led, {}, landing=1)
        e = led["posts"][0]
        e["acted"] = True
        post = s["posts"][0]
        post["button_x"], post["button_y"] = 0.9, 0.5
        led["posts"].remove(e)
        self.assertEqual(_actionable(post, led, {"own_names": OWN}, 6.0),
                         "acted")

    def test_decide_follows_annotation_not_rematch(self):
        led = empty_ledger()
        s = scan_of([P(author="阿明", time="3 hour(s) ago")])
        identify(s, led, {}, landing=1)
        e = led["posts"][0]
        led["posts"].remove(e)          # 同款致因：重算必 None
        d = decide(s, led, {"own_names": OWN})
        self.assertEqual(d["action"], "walk")
        self.assertEqual(d["reason"], "known")   # 不是 pending-marked
        # 帐本边界路同享判决：章里条目是 Yesterday → 照样停
        e["time_text"] = "Yesterday"
        d2 = decide(s, led, {"own_names": OWN})
        self.assertEqual(d2["action"], "boundary")
        self.assertEqual(d2["reason"], "known-out-of-today")

    def test_sent_reply_marked_even_when_rematch_fails(self):
        # 裸断修判别测试：_match_entry 被打成"act 触发后一律 None"
        # （DS 事故发帖后的真实账本签名）。旧代码：记账前再匹配 → None
        # → 裸断，act_result/acted 全丢；新代码：认章入账，不赌匹配。
        s1 = screen([PA()], anchors=(TestWalk.A_ROW,))
        calls = []
        state = {"sent": False}
        import services.digest.walker as _w
        real = _w._match_entry

        def fake(post, ledger):
            if state["sent"]:
                return None
            return real(post, ledger)

        def act(p):
            calls.append(p["author"])
            state["sent"] = True
            return {"status": "ok"}

        _w._match_entry = fake
        try:
            r = walk(press_down=(lambda: None), capture=(lambda: "x"),
                     scan_fn=(lambda _p: s1), cfg={"own_names": OWN},
                     log=QUIET, sleep=NOSLEEP, act_fn=act,
                     max_landings=1)
        finally:
            _w._match_entry = real
        self.assertEqual(calls, ["阿明"])
        e0 = r["ledger"]["posts"][0]
        self.assertEqual(e0["act_result"]["status"], "ok")
        self.assertTrue(e0["acted"])


class TestWeakAuthorRoad(unittest.TestCase):
    """弱路守卫（2026-11-15 夜 James Guan 双帖并帐遮蔽）：作者匹配但一侧
    时间没露面时（`not et or not nt`），不能再盲并第一条同作者 entry——
    两帖的行互折、新帖的 acted 把旧帖未发出的回复遮成"已回过"。守卫：
    正文锚亲缘 <WEAK_AUTHOR_FLOOR(0.4) 就跳过该 entry 试后面候选；
    纯照片尾（norms 空）照旧放行，第四尺兜底不受影响。"""

    OLD_BODY = ("赣州是一座很有历史的城市文庙福寿沟宋城墙都是宋代遗存",
                "博物馆开到晚上九点晚上去人也不多")
    NEW_BODY = ("打卡赣州所有地标赣州博物馆郁孤台蒋经国故居八境台涌金门",
                "标准钟一天走完赣州老城区腿都废了但是值得")

    def _two_entries(self):
        led = empty_ledger()
        identify(scan_of([P(author="老王", time="23 hour(s) ago",
                            body=self.OLD_BODY)]), led, {}, landing=1)
        identify(scan_of([P(author="老王", time="1 hour(s) ago",
                            body=self.NEW_BODY)]), led, {}, landing=2)
        led["posts"][1]["acted"] = True          # 新帖已回过
        return led

    def test_time_hidden_post_wont_fold_into_other_post(self):
        # 新帖门牌露面、时间行没露（照片墙挡着）：不许折进旧帖 entry
        led = self._two_entries()
        plate = P(author="老王", time=None, body=self.NEW_BODY)
        self.assertIs(_match_entry(plate, led), led["posts"][1])

    def test_unrelated_rows_decline_every_candidate_then_none(self):
        # 帐本里只有旧帖一条时，另一条帖的门牌 sighting 不许折进去
        led = empty_ledger()
        identify(scan_of([P(author="老王", time="23 hour(s) ago",
                            body=self.OLD_BODY)]), led, {}, landing=1)
        plate = P(author="老王", time=None, body=self.NEW_BODY)
        self.assertIsNone(_match_entry(plate, led))

    def test_own_rows_still_match_via_weak_road(self):
        # 同一帖门牌重见（时间没露）：自身行亲缘 1.0，弱路照常认领
        led = self._two_entries()
        plate = P(author="老王", time=None, body=self.OLD_BODY)
        self.assertIs(_match_entry(plate, led), led["posts"][0])

    def test_photo_tail_no_norms_still_matches_weak_road(self):
        # 纯照片尾帖 norms 空：守卫不拦（昨晚照片尾认领链不回归）
        led = self._two_entries()
        plate = P(author="老王", time=None, body=())
        self.assertIs(_match_entry(plate, led), led["posts"][0])

    def test_exact_time_still_beats_everything(self):
        led = self._two_entries()
        self.assertIs(_match_entry(P(author="老王", time="23 hour(s) ago",
                                     body=self.OLD_BODY), led),
                      led["posts"][0])


if __name__ == "__main__":
    unittest.main()


class TestSkipReasonLogging(unittest.TestCase):
    def test_skip_reasons_are_logged(self):
        # 用户问"有些帖子漏掉了什么原因"——静默跳过无法对账。
        # 每个落地每个帖子没处置都要留一行原因。
        s1 = screen([
            P(author="小何", body=(TestWalk.A_ROW,)),      # 门牌在、时间行未露面
            PA(author="老周", button=None),                # button missing
            P(arrow=True, time="2 hour(s) ago"),           # ad
        ], anchors=(TestWalk.A_ROW,))
        lines = []
        state, capture, scan_fn, press = make_io([s1])
        walk(press_down=press, capture=capture, scan_fn=scan_fn,
             cfg={"own_names": OWN}, log=lines.append, sleep=NOSLEEP,
             act_fn=lambda p: {"status": "ok"}, max_landings=1)
        text = "\n".join(lines)
        self.assertIn("原因=time-not-revealed", text)
        self.assertIn("原因=button-missing", text)
        self.assertIn("原因=ad", text)
        # D2 门（2026-11-17）：无门牌+未挂账的无头帖，模糊认领一律
        # no-doorplate（L18 冤案路径）。花字行（0.9+ 相似但非精确）在
        # 2026-11-17 深夜的精确锚窄门下依然拒认——花字破坏精确相等，
        # 绑不上就是绑不上。
        s2 = screen([P(author=None, body=("阿明的帖子讲的是云南旅行攻略游补给",))],
                    anchors=(TestWalk.A_ROW,))
        lines2 = []
        led2 = empty_ledger()
        identify(scan_of([P(author="小何", body=(TestWalk.A_ROW,))]),
                 led2, {}, landing=1)          # 先门牌落地建帐（不挂账）
        import json as _json, tempfile as _tf, os as _os
        fd, lpath = _tf.mkstemp(suffix=".json")
        with _os.fdopen(fd, "w") as f:
            _json.dump(led2, f, ensure_ascii=False)
        try:
            state, capture, scan_fn, press = make_io([s2])
            walk(press_down=press, capture=capture, scan_fn=scan_fn,
                 ledger_path=lpath,
                 cfg={"own_names": OWN}, log=lines2.append, sleep=NOSLEEP,
                 act_fn=lambda p: {"status": "ok"}, max_landings=1)
        finally:
            _os.unlink(lpath)
        self.assertIn("原因=no-doorplate", "\n".join(lines2))
        # 窄门正例（2026-11-17 深夜）：精确命中小何帐内锚的片段放行，
        # 不再 no-doorplate——落到下一道诚实关卡 time-not-revealed
        # （时间行未露面）。这正是胡吟|2h 类的解法：门牌落在整屏跳跨越
        # 区永不再露面时，文字片段落地照样能回。
        s3 = screen([P(author=None, time=None, body=(TestWalk.A_ROW,))],
                    anchors=(TestWalk.A_ROW,))
        lines3 = []
        led3 = empty_ledger()
        identify(scan_of([P(author="小何", body=(TestWalk.A_ROW,))]),
                 led3, {}, landing=1)
        fd, lpath3 = _tf.mkstemp(suffix=".json")
        with _os.fdopen(fd, "w") as f:
            _json.dump(led3, f, ensure_ascii=False)
        try:
            state, capture, scan_fn, press = make_io([s3])
            walk(press_down=press, capture=capture, scan_fn=scan_fn,
                 ledger_path=lpath3,
                 cfg={"own_names": OWN}, log=lines3.append, sleep=NOSLEEP,
                 act_fn=lambda p: {"status": "ok"}, max_landings=1)
        finally:
            _os.unlink(lpath3)
        self.assertIn("原因=time-not-revealed", "\n".join(lines3))
        # 帐本里匹配到名字的无头帖用帐本名，匹配不到用 "?"
        self.assertTrue(any("跳过" in ln and "原因=" in ln for ln in lines))


class TestDoorplateClimb(unittest.TestCase):
    """门牌回攀（2026-11-17 用户授权的宪法修正案；A2/A3，ASSUMPTIONS.md）：
    ↓ 对齐落在超屏帖的时间条上，头部整段被跳过（A2）——对齐落地的
    无门牌未知块 → ↑ 回攀显帖头，见门牌即停，↓ 恢复后原规律自愈（A3）。
    armed 尾不攀（投递路 owns）；精确绑定可识别不攀；非对齐块不攀；
    press_up 缺省（旧绑定）行为照旧。"""

    HU_ROW = "优胜美地的日出把整面花岗岩染成金红色太震撼了"

    def _tail(self, top_y=0.0, tr_y=0.95):
        # A1 对齐落地上的超屏帖尾块：作者缺席、时间条贴底、块体起于顶沿
        p = P(author=None, time="14 hour(s) ago", body=(self.HU_ROW,))
        p["time_row"]["y"] = tr_y
        p["body"]["top_y"] = top_y
        return screen([p], anchors=(self.HU_ROW,), bottom=tr_y)

    def _head(self):
        # ↑ 一按后：帖头入镜（门牌可见）
        return screen([P(author="胡吟", time="14 hour(s) ago",
                         body=(self.HU_ROW,))],
                      anchors=(self.HU_ROW,), bottom=0.9)

    def test_climb_fires_sees_doorplate_and_resumes(self):
        state, capture, scan_fn, press_down = make_io(
            [self._tail(), self._head()])
        ups, downs, evs = [], [], []
        r = walk(press_down=lambda: downs.append(1),
                 press_up=lambda: ups.append(1),
                 capture=capture, scan_fn=scan_fn,
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP,
                 emit=evs.append)
        self.assertEqual(len(ups), 1)              # 一按即见门牌
        self.assertGreaterEqual(len(downs), 1)     # 随后 ↓ 恢复
        self.assertEqual(r["ledger"]["posts"][0]["author"], "胡吟")
        climbs = [e for e in evs if e.get("t") == "climb"]
        self.assertEqual(len(climbs), 1)
        self.assertEqual(climbs[0]["streak"], 1)

    def test_climb_gives_up_at_bound_and_never_reclimbs_block(self):
        # 3 按到限仍无门牌（超超屏，攀爬途中画面在变=确有位移）→ 放弃
        # 此块 ↓ 离开；同块再现不再攀
        mid1 = self._tail()
        mid1["posts"][0]["body"]["texts"] = ("梅岭古道是兵家必争之地的传",)
        mid1["viewport"]["anchors"] = [{"norm": "梅岭古道是兵家必争之地的传",
                                        "text": "梅岭古道是兵家必争之地的传"}]
        mid2 = self._tail()
        mid2["posts"][0]["body"]["texts"] = ("陈毅在这里打游击写下梅岭三章篇",)
        mid2["viewport"]["anchors"] = [{"norm": "陈毅在这里打游击写下梅岭三章篇",
                                        "text": "陈毅在这里打游击写下梅岭三章篇"}]
        state, capture, scan_fn, press_down = make_io(
            [self._tail(), mid1, mid2, self._tail(), self._tail()])
        ups = []
        r = walk(press_down=press_down, press_up=lambda: ups.append(1),
                 capture=capture, scan_fn=scan_fn,
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP)
        self.assertEqual(len(ups), 3)              # CLIMB_MAX_PRESS 封顶
        self.assertEqual(r["stop_reason"], "stuck")

    def test_climb_stops_immediately_at_feed_top_no_movement(self):
        # Q4（用户令 2026-11-17）：↑ 无位移（两次攀落地画面全同）=
        # feed 顶/无响应 → 立即放弃此块，不烧满限额
        state, capture, scan_fn, press_down = make_io(
            [self._tail(), self._tail(), self._tail(), self._tail()])
        ups = []
        walk(press_down=press_down, press_up=lambda: ups.append(1),
             capture=capture, scan_fn=scan_fn,
             cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP)
        self.assertEqual(len(ups), 1)

    def test_climb_stops_when_any_screen_post_has_doorplate(self):
        # 2026-11-17 climb 轮 live L17-19 教训：回攀落地里门牌帖常不是
        # cur（cur 指顶部碎片）——见**屏上任一**门牌即停，不烧满限额
        head = P(author="胡吟", time="14 hour(s) ago", body=(self.HU_ROW,))
        stop = screen([self._tail()["posts"][0], head], current=0,
                      anchors=(self.HU_ROW,), bottom=0.9)
        ups, downs, lines_ = [], [], []
        state, capture, scan_fn, press_down = make_io(
            [self._tail(), stop, stop])
        walk(press_down=lambda: downs.append(1),
             press_up=lambda: ups.append(1),
             capture=capture, scan_fn=scan_fn,
             cfg={"own_names": OWN}, log=lines_.append, sleep=NOSLEEP)
        self.assertEqual(len(ups), 1)              # 一按即停
        self.assertGreaterEqual(len(downs), 1)     # ↓ 恢复
        self.assertTrue(any("回攀见门牌" in ln for ln in lines_))

    def test_armed_tail_is_not_climbed(self):
        # armed 尾（tail_pending）：投递机器 owns，回攀会打乱送达编排
        led = {"posts": [{"key": "胡吟|14 hour(s) ago", "author": "胡吟",
                          "time_text": "14 hour(s) ago",
                          "anchors": [self.HU_ROW], "tail_pending": True,
                          "acted": False, "landings": [1],
                          "act_result": {"status": "expanded-tail-deferred",
                                         "reply": "存好的回复",
                                         "full_body": [self.HU_ROW]}}],
               "sightings": []}
        fd, lpath = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(led, f, ensure_ascii=False)
        try:
            state, capture, scan_fn, press_down = make_io(
                [self._tail(), self._tail()])
            ups = []
            walk(press_down=press_down, press_up=lambda: ups.append(1),
                 capture=capture, scan_fn=scan_fn,
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP,
                 ledger_path=lpath)
            self.assertEqual(ups, [])
        finally:
            os.unlink(lpath)

    def test_unarmed_fuzzy_claim_still_climbs_to_confirm(self):
        # 未武装的模糊认领不拒攀：锚是模糊变体行（四尺 0.96 能认领、
        # 精确绑定绑不上）→ 回攀露出真门牌验证/证伪模糊身份
        # （L18 教训的正向运用）
        led = {"posts": [{"key": "胡吟|unknown", "author": "胡吟",
                          "time_text": None, "landings": [1],
                          "anchors": ["优胜美地的日出把整面花岗岩染成金红色真震撼"],
                          "acted": False}],
               "sightings": []}
        fd, lpath = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(led, f, ensure_ascii=False)
        try:
            state, capture, scan_fn, press_down = make_io(
                [self._tail(), self._head()])
            ups = []
            r = walk(press_down=press_down, press_up=lambda: ups.append(1),
                     capture=capture, scan_fn=scan_fn,
                     cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP,
                     ledger_path=lpath)
            self.assertEqual(len(ups), 1)
            self.assertEqual(r["ledger"]["posts"][0]["author"], "胡吟")
        finally:
            os.unlink(lpath)

    def test_bind_resolvable_fragment_is_not_climbed(self):
        # 精确绑定已认出身份（非贴顶、四尺不认领、绑定命中）→ 不攀
        led = {"posts": [{"key": "胡吟|15 hour(s) ago", "author": "胡吟",
                          "time_text": "15 hour(s) ago", "landings": [1],
                          "anchors": [self.HU_ROW], "acted": True}],
               "sightings": []}
        fd, lpath = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(led, f, ensure_ascii=False)
        try:
            state, capture, scan_fn, press_down = make_io(
                [self._tail(top_y=0.3), self._tail(top_y=0.3)])
            ups = []
            walk(press_down=press_down, press_up=lambda: ups.append(1),
                 capture=capture, scan_fn=scan_fn,
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP,
                 ledger_path=lpath)
            self.assertEqual(ups, [])
        finally:
            os.unlink(lpath)

    def test_no_climb_when_block_not_bottom_aligned(self):
        # 时间条在屏中 = 不是对齐块（已知帖的尾在中屏）→ 不攀
        state, capture, scan_fn, press_down = make_io(
            [self._tail(tr_y=0.3), self._tail(tr_y=0.3)])
        ups = []
        walk(press_down=press_down, press_up=lambda: ups.append(1),
             capture=capture, scan_fn=scan_fn,
             cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP)
        self.assertEqual(ups, [])

    def test_walk_works_without_press_up_backward_compat(self):
        # press_up 缺省（旧 runner/旧测试绑定）：纯↓ 行为照旧
        state, capture, scan_fn, press_down = make_io(
            [self._tail(), self._head()])
        r = walk(press_down=press_down, capture=capture, scan_fn=scan_fn,
                 cfg={"own_names": OWN}, log=QUIET, sleep=NOSLEEP)
        self.assertTrue(r["stop_reason"])
        self.assertEqual(r["ledger"]["posts"][0]["author"], "胡吟")
