#!/usr/bin/env python3
"""Sequential live-fire test: walk the Moments feed newest→older, and for
each post with a matching persona draft, drive the UI comment flow.

The conversation drives it — each subcommand is one phase, and I ask the
user yes/no between `box` (text pasted, NOT sent) and `send`:

  read   scroll to the feed top (first run) or to the next post, attribute
         the top post via OCR, match it to a DB draft, print the pair
  box    open the ••→Comment input box for the CURRENT matched post and
         paste the draft text (or --text "..."), verify by OCR, DO NOT send
  send   click Send, verify the comment appears under the post, mark the
         draft sent in the DB
  abort  Esc away (nothing posted), clear the pending state
  state  print the pending state

State lives in /tmp/post_test_state.json; the feed position lives in WeChat.
Safety rules: every click target is re-verified against fresh window bounds
and OCR evidence; author must match before the box opens; any failed
expectation aborts WITHOUT sending.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from difflib import SequenceMatcher

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.dont_write_bytecode = True

from services.digest.envfile import load_env_file
load_env_file(os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from Quartz import CGDisplayBounds, CGMainDisplayID
import Quartz

from live_round import activate_wechat, wechat_pid
from services.digest import db
from services.digest.chrome_schema import locate_discover, locate_refresh
from services.digest.collector import ALIGN_TOP_OFFSET, find_moments_window
from services.digest.reviewer import TooShortToReply  # re-export (Actor)
from services.digest import reply_settings
from services.digest.ocr_extract import (
    _vision_rows,
    is_time_row,
    is_time_row_prefix,
    normalize_for_match,
    read_top_item,
)
from services.digest.screen_capture import capture_window
from services.digest.scroller import (
    accessibility_trusted,
    click_at,
    make_expand_fn,
    scroll_pixels_at,
)

OUT = "/tmp/poster_probe"
STATE_PATH = "/tmp/post_test_state.json"
MORE_X = 0.884  # measured in poster_probe recon: the •• glyph on the time row
# 自己人的微信名字——评论剥离时绝不许当成帖子正文（安全特性，防止
# 把自己发的内容再回一遍）。单一事实源 = data/moments_settings.json
# 的 own_names（moments_settings 工具/手编均可设）。**必须设置**：
# 空清单意味着自己的帖子不被识别，首次实跑可能回复到自己的动态。
OWN_NAMES = set(reply_settings.load().get("own_names") or [])
# 持久跳过名单（2026-11-25 用户拍板「默认全回+跳过名单」）：feed_reader
# 把它放进 walker cfg 的 skip_authors，名单内作者 act 永不回（帖留
# pending 不封账）。同一存储、同一新鲜度模型（每轮新进程现读）。
SKIP_NAMES = set(reply_settings.load().get("skip_authors") or [])
UI_TOKENS = {"Show More", "Show Less", "Comment", "Like", "Send",
             "查看详情", "广告"}  # chrome, never an author
SW = CGDisplayBounds(CGMainDisplayID()).size.width
SH = CGDisplayBounds(CGMainDisplayID()).size.height


# ---------------------------------------------------------------- helpers

def sh(cmd):
    subprocess.run(cmd, shell=True, check=True, timeout=30)


def full_capture(path):
    sh(f"screencapture -x {path}")


def ocr_rows(path):
    return sorted(_vision_rows(path), key=lambda r: (r["y"], r["x"]))


# One-entry OCR cache for the walk's read.png: read_top_post and the
# motion check consume the SAME capture's rows — OCR runs once per step,
# not twice (each pass costs seconds).
_OCR_CACHE = {"path": None, "rows": None}


def ocr_rows_cached(path):
    if _OCR_CACHE["path"] != path:
        _OCR_CACHE["path"] = path
        _OCR_CACHE["rows"] = _vision_rows(path)
    return _OCR_CACHE["rows"]


def _row_shift(rows_before, rows_after):
    """Median upward shift (fraction of window height) of text rows common
    to two consecutive captures — the walk's execution check, working
    WITH OR WITHOUT a divider on screen (live 2026-11-09 17:00: one
    burst-amplified wheel step flew ~2h past two posts while the
    divider-based check sat blind — the landing post kept every divider
    off screen over its comment fold). Rows match by normalized text (y
    discarded), so a mid-walk re-render survives matching; only texts
    occurring EXACTLY once per capture participate. Returns None when the
    pair is unverifiable (text-poor landing, e.g. mid-photo-wall); 1.0
    when a texty landing shares NO unique text with the previous capture
    (= the view flew — two screens 0.3h apart always share rows)."""
    def uniq(rows):
        seen = {}
        for r in sorted(rows, key=lambda r: (float(r.get("y", 0.0)),
                                             float(r.get("x", 0.0)))):
            n = normalize_for_match(r.get("text") or "")
            if len(n) >= 6:
                seen.setdefault(n, []).append(float(r.get("y", 0.0)))
        return {n: ys[0] for n, ys in seen.items() if len(ys) == 1}
    before, after = uniq(rows_before), uniq(rows_after)
    common = set(before) & set(after)
    if len(common) >= 2:
        shifts = sorted(before[n] - after[n] for n in common)
        return shifts[len(shifts) // 2]
    if len(after) >= 3:
        return 1.0  # texty landing, zero shared text — the view flew
    return None     # can't verify (photo wall / text-poor landing)


def fresh_win():
    w = find_moments_window()
    if w is None:
        print("FATAL: 微信窗口不在屏幕上")
        sys.exit(1)
    return w


def rows_in(rows, b, margin=30):
    """OCR rows whose centers sit inside the window rect (± margin)."""
    out = []
    for r in rows:
        px = (r["x"] + r.get("w", 0.1) / 2) * SW
        py = (r["y"] + r.get("h", 0.015) / 2) * SH
        if (b["x"] - margin <= px <= b["x"] + b["width"] + margin
                and b["y"] - margin <= py <= b["y"] + b["height"] + margin):
            out.append((r, px, py))
    return out


def guarded_click(win, px, py, settle):
    b = win.bounds
    if not (b["x"] <= px <= b["x"] + b["width"]
            and b["y"] <= py <= b["y"] + b["height"]):
        print(f"ABORT: 点击点 ({px:.0f},{py:.0f}) 不在窗口内 {b}")
        sys.exit(1)
    click_at(px, py, settle=settle)


def press_esc():
    d = Quartz.CGEventCreateKeyboardEvent(None, 53, True)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, d)
    time.sleep(0.05)
    u = Quartz.CGEventCreateKeyboardEvent(None, 53, False)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, u)
    time.sleep(0.5)


ARROW_UP = 126


def press_up():
    d = Quartz.CGEventCreateKeyboardEvent(None, ARROW_UP, True)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, d)
    time.sleep(0.05)
    u = Quartz.CGEventCreateKeyboardEvent(None, ARROW_UP, False)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, u)
    time.sleep(0.04)


def recover_top_by_arrow_up(win, tag, check_fn=None, press_fn=None,
                            sleep_fn=None, log=None,
                            max_rounds=8, per_round=20, gap=1.5):
    """↑ 连打回顶（2026-11-16 深夜用户批准；只许恢复梯子用）。

    换血回位把视口拖回"上次读到处"后，esc→重入只会恢复读位
    （23:19 实证：两连重入精准落回同一行 '11月16日 15:18'）——
    程序里原本没有任何向上的手段，人手滚是唯一回顶方式。
    ↑ 连打向上滚，每轮按 per_round 次后用 feed_top_check 的封面
    名牌位置门验证；到顶后隔 gap 复验一次、两测都在顶才认（防
    ↑ 意外触发换血把刚到手的顶又拖走）。walk 本体永远纯 ↓，本
    函数只在走路开始前的恢复阶段存在。"""
    if check_fn is None:
        check_fn = feed_top_check
    if press_fn is None:
        press_fn = press_up
    if sleep_fn is None:
        sleep_fn = time.sleep

    def _at_top():
        ok, _v, why = check_fn(win, tag)
        return ok, why

    ok, why = _at_top()
    if ok:
        return True
    for round_ in range(max_rounds):
        for _ in range(per_round):
            press_fn()
        sleep_fn(gap)
        ok, why = _at_top()
        if log:
            log("%s: ↑ 第%d轮 x%d → %s" % (tag, round_ + 1, per_round,
                                           "到顶" if ok else "未到顶"))
        if ok:
            sleep_fn(gap)
            ok2, why2 = _at_top()
            if ok2:
                return True
            ok = False
            if log:
                log("%s: 复验丢顶（%s）——继续 ↑" % (tag, why2[:60]))
    if log:
        log("%s: ↑ 连打 %d 轮仍未到顶（%s）——放弃" % (tag, max_rounds,
                                                   why[:60]))
    return False


def _sig_probe():
    """Verification probe for paste/send: the signature, from its single
    source (reply_settings.resolve_signature: settings 显式定制 > env >
    默认文本；"" = 无署名 → 探针为空串，paste_verified 的 `p and` 门
    自动跳过它，证据退头/尾 12 字 + 绿按钮结构证据）。The old code
    hardcoded '情绪价值机器人' here — a rename would have silently
    disabled the signature evidence path."""
    from services.digest.reviewer import _resolve_persona
    return normalize_for_match(_resolve_persona(None)["signature"]).replace(" ", "")


def paste_verified(capture_path, text):
    """The narrow input box wraps the text; OCR often only reads the tail
    (live 2026-11-08) — accept head, tail, or the signature as evidence.
    Join rows in reading order: bucket y by line height first, then x —
    OCR splits ONE visual line into left/right fragments whose y jitters
    by ~0.0003, so a plain (y, x) sort scrambles within the line
    (live 2026-11-08: '冒险经历！' + '（by 情绪价值机器' + '人）' joined
    out of order and the paste verify false-FAILED)."""
    b = fresh_win().bounds
    ordered = sorted(rows_in(ocr_rows(capture_path), b),
                     key=lambda t: (round(t[0]["y"] / 0.012), t[0]["x"]))
    seen = normalize_for_match("".join(
        r.get("text") or "" for r, _px, _py in ordered))
    seen = seen.replace(" ", "")
    n = normalize_for_match(text).replace(" ", "")
    probes = []
    if len(n) >= 12:
        probes += [n[:12], n[-12:]]
    probes.append(_sig_probe())
    return any(p and p in seen for p in probes), seen


def keys_match(k1, k2, floor=0.75):
    """OCR jitters at character level ('蛋壳'→'蛋売', live 2026-11-08) —
    exact key comparison breaks post identity and re-cards sent posts.
    Compare author and head separately; a post matches when both parts are
    near-identical (ratio ≥ floor). Different posts by the same author
    still differ on the head part. Both parts normalize FIRST: OCR inserts
    spaces into Chinese text at will ('纳帕赏秋一日游 3' vs '…一日行1')
    and the phantom chars deflate the ratio below the floor — live
    2026-11-09 that re-carded a post the user had already skipped."""
    if k1 == k2:
        return True
    a1, _, h1 = k1.partition("|")
    a2, _, h2 = k2.partition("|")
    strip = lambda s: normalize_for_match(s).replace(" ", "")
    a1, h1 = strip(a1), strip(h1)
    a2, h2 = strip(a2), strip(h2)
    r = SequenceMatcher(None, a1, a2).ratio()
    return (r >= floor
            and SequenceMatcher(None, h1, h2).ratio() >= floor)


def ff_step_decision(key, rows_text, anchor_key, anchor_head, pool):
    """Send-resume fast-forward step verdict: 'anchor' | 'seen' | 'insert'.
    The anchor is the LAST ledger entry (the just-replied post); seeing it
    again ends the fast-forward. A row-level fallback catches the anchor
    even when its doorplate read went phantom (content-line-as-author):
    its head text itself is on screen. Everything else in the ledger is
    'seen' (coast past); anything unknown ABOVE the anchor is a mid-task
    insert (user rule 2026-11-13: 一次任务不处理新增帖) — logged and
    skipped. Failure directions are all safe: a phantom read of a seen
    post degrades to 'insert' (coast past, no card), a false anchor hit
    (quoting post) just ends the fast-forward early into normal walking."""
    if anchor_key and keys_match(key, anchor_key):
        return "anchor"
    ah = normalize_for_match(anchor_head or "").replace(" ", "")[:15]
    if ah and any(ah in normalize_for_match(t or "").replace(" ", "")
                  for t in rows_text):
        return "anchor"
    if any(keys_match(key, k) for k in pool):
        return "seen"
    return "insert"


def motion_verdict(w, cmd_h):
    """Classify a measured row-shift against the commanded step (zero-wait
    design 2026-11-13; same thresholds as the walk's inline motion check):
      'clean'  moved ~as commanded (±0.15h)
      'aim'    fold above collapsed / re-render handed over the line —
               re-aim, keep walking (cmd+0.15 < w ≤ 0.6)
      'over'   FLIGHT — far beyond the command (w > 0.6)
      'stall'  did not move as commanded (spin / dead wheel)
      None     unverifiable (w is None — too little text on screen)
    Every verdict except 'over' is survivable in place; 'over' is the
    send-locked flight and triggers the automatic reopen + re-anchor."""
    if w is None:
        return None
    if abs(w - cmd_h) <= 0.15:
        return "clean"
    if cmd_h + 0.15 < w <= 0.6:
        return "aim"
    return "over" if w > 0.6 else "stall"


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {}


def save_state(st):
    with open(STATE_PATH, "w") as f:
        json.dump(st, f, ensure_ascii=False)


def activate():
    win = fresh_win()
    activate_wechat(wechat_pid(win.window_id))
    time.sleep(1.0)
    return fresh_win()


LOG_PATH = "/tmp/post_test_walk.log"


def wlog(msg):
    """Every scroll/attribution decision goes to the log AND stdout —
    no filtering (the silent exits hid the jumping, live 2026-11-08)."""
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def feed_fp(win, tag):
    """Window-capture fingerprint for stuck/refresh detection. OCR goes
    through the 1-entry cache so the scroll-probe (feed_scroll) can reuse
    the fp_before rows for free — fp paths alternate every call, so the
    cache always misses right after a fresh capture and never goes stale."""
    path = f"{OUT}/fp_{tag}.png"
    capture_window(win.window_id, path)
    rows = ocr_rows_cached(path)
    return tuple((round(r["y"], 3), (r.get("text") or "")[:20]) for r in rows)


def feed_scroll(win, delta_px):
    """Scroll the feed (positive = up) with stuck detection: if the view
    didn't move, re-activate and retry once with the pointer over the left
    margin (embedded Official-Account cards can swallow wheel events).
    Returns True if the view moved.

    scroll-probe (2026-11-13, user-approved evidence line): BEFORE the
    wheel, compare this fp_before capture against the walk's last capture
    (read.png — same screen, seconds earlier, NO wheel in between). A
    non-zero shift proves WeChat moved the page BY ITSELF in that blind
    window (comment-sync re-render / scroll anchoring after a reply) —
    the 'fly' displacement my read#0→read#1 comparison blamed on the
    wheel would then be self-scroll, not wheel overshoot. Evidence ONLY:
    nothing brakes on it, step behavior is unchanged."""
    before = feed_fp(win, "before")
    try:
        prev_path = f"{OUT}/read.png"
        if os.path.exists(prev_path):
            shift = _row_shift(ocr_rows_cached(prev_path),
                               ocr_rows_cached(f"{OUT}/fp_before.png"))
            wlog(f"scroll-probe: 静置位移={shift}"
                 f"（read.png→滚轮前；非零=微信自跳实锤）")
    except Exception as e:
        wlog(f"scroll-probe: 取证失败 {e!r}")
    b = win.bounds
    scroll_pixels_at(b["x"] + 0.5 * b["width"], b["y"] + 0.5 * b["height"],
                     delta_px)
    time.sleep(0.9)
    if feed_fp(win, "after") != before:
        return True
    activate()
    b = win.bounds
    scroll_pixels_at(b["x"] + 40, b["y"] + 0.5 * b["height"], delta_px)
    time.sleep(0.9)
    return feed_fp(win, "after2") != before


# ----------------------- A2 refresh / view-verification (live 2026-11-09)
#
# Moments is a VIEW inside the main window, not a separate one: the window
# id/geometry never change when the user (or a stray click) opens a chat in
# it, and find_moments_window binds by name+geometry — so it happily hands
# back a chat view (live 2026-11-09: the walk OCR'd a conversation about
# watering plants instead of the feed). Content must therefore be verified
# before acting, and re-entry must be programmatic. Drag refresh stays
# BANNED — the native ⟳ toolbar button is the sanctioned refresh, and
# re-entering via Discover re-tops the timeline as a bonus.

# Layout constants are PT-ANCHORED to window edges, NOT fractions: macOS
# app chrome is a fixed-pt layout (icon strip ~55pt tall, sidebar rail
# ~55pt wide), so edge-relative pt offsets survive other machines,
# resolutions and window sizes; fractions die the moment the window isn't
# 441x561. Measured on the window-exact capture crop_now_top.png @2x
# (live 2026-11-09).
#
# COORDINATE HYGIENE: `screencapture -x -l` CLI captures carry ~60px
# window-SHADOW padding — fractions derived from them are shifted (the
# first ⟳ click landed 26pt low and opened the cover image). Window-exact
# pixels come ONLY from capture_window().
REFRESH_FROM_RIGHT_TOP_PT = (108.0, 27.5)  # ⟳ in the Moments toolbar:
# center (666,55)px@2x → 108pt left of the right edge, 27.5pt below top;
# VERIFIED IDENTICAL at window sizes 441x561, 659x464, 424x696 (live
# resize experiment 2026-11-09 — fixed-pt chrome, not proportional;
# toolbar icon spacing ≈36pt)
DISCOVER_FROM_LEFT_TOP_PT = (30.0, 264.0)  # sidebar compass glyph (green
# when active, dark glyph when idle): center (63,532)px@2x → 30pt from
# left, 264pt below top; VERIFIED IDENTICAL at window sizes 441x561,
# 659x464, 424x696 (live resize experiment 2026-11-09 — layout is
# fixed-pt chrome, NOT proportional; rail icon spacing ≈49pt)
MOMENTS_ITEM_FALLBACK = (0.325, 0.147)  # LAST RESORT only — reopen
# normally OCR-locates the 'Moments' row on a window-exact capture; this
# shadow-corrected fraction is for the case that OCR misses the row

BARE_CLOCK_RE = re.compile(r"^\d{1,2}:\d{2}$")  # chat timestamps — feed time
# rows are ALWAYS ago-words/day-words, never a bare clock
PENDING_PHASES = {"read", "boxed"}  # a confirmation card is up or text is
# already in the box: navigation/refresh could dismiss the pending input
VIEW_OK = {"moments", "unknown"}  # unknown = cover-only feed top (honest case)


def win_capture(win, tag):
    path = f"{OUT}/{tag}.png"
    capture_window(win.window_id, path)
    return path


def _region_mean(path, fx0, fy0, fx1, fy1):
    """Mean luminance of an image-fraction region (scale-independent)."""
    from PIL import Image
    im = Image.open(path).convert("L")
    w, h = im.size
    hist = im.crop((int(fx0 * w), int(fy0 * h),
                    int(fx1 * w), int(fy1 * h))).histogram()
    total = sum(hist)
    if not total:
        return 255.0
    return sum(i * c for i, c in enumerate(hist)) / total


def _content_rows(rows):
    """OCR rows with ≥2 chars whose center sits in the CONTENT column
    (right of the sidebar rail, below the toolbar strip ~0.12). 1-char
    junk ('Q', 'D', sidebar glyphs) is dropped. The feed ALWAYS has text
    here — the first post's author name at minimum — while the fullscreen
    image viewer washes/presses text out and the cover photo carries no
    text. y floor 0.42→0.12 (2026-11-16 21:21 作者甲帖真机证据 av2.png)：
    展开后的健康帧全部文字都在 y<0.35（其下是未加载的空白九宫格），
    旧带把整帧文字排除 → 冤判 viewer → act-error。标题栏文字
    （Moments ≈0.06）仍被 0.12 挡住。"""
    out = []
    for r in rows:
        t = (r.get("text") or "").strip()
        if len(t) < 2:
            continue
        cx = r["x"] + r.get("w", 0.1) / 2
        cy = r["y"] + r.get("h", 0.015) / 2
        if 0.25 <= cx <= 0.98 and 0.12 <= cy <= 0.95:
            out.append(r)
    return out


def classify_capture(path):
    """Content check of ONE window-exact capture — a pure function of the
    image so it can be tested offline against saved evidence. Signatures:
      moments  — feed time rows (OCR)
      chat     — ≥2 bare HH:MM rows (one clock row can be an image caption)
      discover — the Discover menu ('discover' header / moments+channels rows)
      viewer   — ZERO ≥2-char text rows in the content column. The viewer
                 has TWO themes (live 2026-11-09): dark backdrop AND a
                 light wash — both erase feed text, and a rail-luminance
                 signature only catches the dark one (it missed the light
                 viewer and passed it as healthy). The cover-photo viewer
                 can never show content-column text.
      unknown  — none of the above; honestly: usually the cover-only feed
                 top, whose first time row is below the fold (live 2026-11-09)
    Feed it ONLY capture_window() images."""
    rows = ocr_rows(path)
    hits = [r for r in rows
            if is_time_row(r.get("text") or "")
            or is_time_row_prefix(r.get("text") or "")]
    if hits:
        return "moments", f"{len(hits)} time rows, e.g. {hits[0].get('text')!r}"
    clocks = [r for r in rows
              if BARE_CLOCK_RE.match((r.get("text") or "").strip())]
    if len(clocks) >= 2:
        return ("chat",
                f"{len(clocks)} bare HH:MM rows (chat timestamps), "
                f"e.g. {clocks[0].get('text')!r}")
    norm = [normalize_for_match(r.get("text") or "") for r in rows]
    # 菜单行只有在最右栏才算 discover 页（窗口不变量：内容=最右栏，
    # 2026-11-16 实机教训：宽窗口的发现菜单列常驻内容栏左侧——
    # cx≈0.19 的 moments/channels 行 + 最右栏朋友圈封面，曾被误判
    # discover → 恢复被冤判失败 → FATAL）。侧栏位置的菜单行不参与判卷。
    menu_right = [t for t, r in zip(norm, rows)
                  if t and (r["x"] + r.get("w", 0.1) / 2) > 0.45]
    if "discover" in menu_right or ("moments" in menu_right
                                    and "channels" in menu_right):
        return "discover", f"menu rows {sorted(set(menu_right))[:5]}"
    content = _content_rows(rows)
    if not content:
        return ("viewer",
                f"zero text rows in the content column "
                f"(of {len(rows)} raw rows) — fullscreen overlay?")
    return ("unknown",
            f"no signature; content col has {len(content)} text rows "
            f"e.g. {(content[0].get('text') or '')[:16]!r} — cover-only "
            f"feed top")


def cover_name_evidence(cap):
    """封面名牌证据（2026-11-16 深夜竞态教训）：⟳ 簇在标题栏常驻，
    "无时间行 + ⟳ 簇"就宣称封面顶被半路帧冒充了两次——评论区自己的
    'Test Owner' 回复行冒充封面署名，pre-check 假判在顶 → 跳过 reopen →
    ⟳ 换血完成后的延迟回位把起步视口拖回半路（一次 ↓ 瞬移 30 帖、
    2 落地即停两轮）。真封面名牌的不变量：归一化后就是名字本身的短行、
    在右侧封面位（cx≥0.55）、封面带（cy∈[0.35,0.8]）。评论行带被回复人
    与正文，名字等价测试天然排除（真机帧实证：真顶 l1 命中 @(0.73,0.63)，
    边界评论帧零命中）。返回命中的 row 或 None。"""
    own = {normalize_for_match(n) for n in OWN_NAMES}
    for r in ocr_rows(cap):
        text = normalize_for_match(r.get("text") or "")
        if not text or text not in own:
            continue
        cx = r["x"] + r.get("w", 0.1) / 2
        cy = r["y"] + r.get("h", 0.02) / 2
        if cx >= 0.55 and 0.35 <= cy <= 0.8:
            return r
    return None


def feed_top_check(win, tag="top"):
    """(at_top, verdict, why) — 是否站在朋友圈可刷新的顶部。

    判据（2026-11-16 教训链）：时间行 = 半路铁证（⟳ 簇在窄窗口标题栏
    常驻，不能作顶部证据）；无时间行且 ⟳ 簇量得 + 封面名牌在封面位
    （cover_name_evidence，同夜竞态教训）= 封面顶；其余 = 异常态。
    """
    cap = win_capture(win, tag)
    verdict, why = classify_capture(cap)
    b = win.bounds
    located = locate_refresh(cap, b["width"], b["height"])
    name = cover_name_evidence(cap) if located else None
    # 2026-11-22：窗口 577pt 高时，真封面顶也能看到首帖的相对时间行
    # （如 "58 minute（s） ago"）——旧判据见时间行即否，真顶被误杀两轮。
    # 封面名牌的短行只在封面可见时出现（边界评论帧的长行过不了名字等价
    # 门），所以名牌证据优先于时间行；无名牌时时间行仍是半路铁证。
    if name:
        return True, "moments", (f"⟳ 簇 + 封面名牌 {name.get('text')!r}"
                                 " = 封面顶（%s）" % why)
    if verdict == "moments":                      # 时间行 + 无名牌 → 半路
        return False, verdict, why
    if located:
        return (False, verdict,
                f"{why}；⟳ 簇虽在（标题栏常驻）但封面名牌不在封面位"
                "——半路帧冒充顶部，走恢复梯子（2026-11-16 竞态教训）")
    return False, verdict, why


def check_view(win, tag="view"):
    """What is the bound window actually showing right now? (see
    classify_capture for the five states and their signatures)

    `unknown` is ambiguous: Moments cover-top (healthy, time rows below
    the fold) OR any other text page — chat list, conversation, contacts
    (user 2026-11-16: refresh must never hunt ⟳ on a non-Moments page).
    Disambiguate with the ⟳ toolbar icon cluster (equidistant right-anchored
    triplet): located → "moments" (cover-top, healthy). NOT located →
    "foreign" (a text page that is NOT Moments — must recover, never
    treated as healthy; lesson 2026-11-16: returning bare `unknown` here
    was a no-op because unknown is already in VIEW_OK → blind click into
    contact management)."""
    verdict, why = classify_capture(win_capture(win, tag))
    if verdict == "unknown":
        b = win.bounds
        if locate_refresh(win_capture(win, tag + "_ev"),
                          b["width"], b["height"]):
            return "moments", f"⟳ 图标簇量得，判为朋友圈封面顶（{why}）"
        return "foreign", f"文字页但无 ⟳ 工具栏——非朋友圈，需导航（{why}）"
    return verdict, why


def recover_view(win, tag, require_top=False):
    """Escape ladder after a mis-click (overlay or wrong view): Esc FIRST —
    it kills the image viewer and any popup with ZERO coordinate
    dependence (version-proof) — then the proven reopen navigation. Every
    step is verified; a human is asked only as the last resort.
    require_top=True (refresh start): success demands the feed TOP —
    mid-feed Moments (time rows) does NOT count (2026-11-16: VIEW_OK
    blessed a boundary-position window and the walk flew)."""
    for step in ("esc", "reopen"):
        if step == "esc":
            press_esc()
            time.sleep(1.2)
        else:
            cmd_reopen()
        if require_top:
            at_top, verdict, why = feed_top_check(win, f"{tag}_{step}")
            if at_top:
                wlog(f"recover[{step}]: 到顶 ✓")
                return True
            wlog(f"recover[{step}]: 未到顶 {verdict} ({why})")
        else:
            verdict, why = check_view(win, f"{tag}_{step}")
            wlog(f"recover[{step}]: {verdict} ({why})")
            if verdict in VIEW_OK:
                return True
    return False


def require_view_ok(win, tag):
    """Guard for acting commands: refuse to act on any non-feed view — chat,
    image viewer, Discover menu (the exact live failure 2026-11-09: geometry
    binding handed back a conversation). Navigation-free: one window capture
    + OCR, no clicks, no focus change."""
    verdict, why = check_view(win, tag)
    if verdict not in VIEW_OK:
        wlog(f"VIEW GUARD: 视图异常 {verdict}（{why}）——拒绝动作，先跑 reopen")
        print(f"FATAL: 绑定窗口不在朋友圈（{verdict}: {why}）。"
              f"先跑: python3 scripts/post_test.py reopen")
        sys.exit(2)
    return verdict, why


def cmd_view():
    """Report what the bound window is actually showing right now."""
    win = activate()
    verdict, why = check_view(win, "view")
    print(f"VIEW: {verdict} ({why})")


def _reopen_enter(win, tag):
    """发现图标（schema 量点）一次进入尝试。已开朋友圈则内容可能是半路
    （由调用方用 feed_top_check 判）；见菜单页则 OCR 点 Moments 行。
    返回 (verdict, why)。"""
    b = win.bounds
    # 发现图标先量后点（chrome_schema，2026-11-16 用户令）。已证（2026-11-16
    # 实测三尺寸）：窗口纵向拉动顶偏移不变，恒 (29.7,265.8)pt——schema 在此
    # 与固定点等价；保留测量是为分辨率/缩放变化（用户点名未验证的变量）。
    loc = locate_discover(win_capture(win, tag + "_discover"),
                          b["width"], b["height"])
    if loc:
        dx_pt, dy_pt = loc["x_pt"], loc["y_pt"]
        wlog(f"reopen: Discover 图标量得 window-pt ({dx_pt:.1f},{dy_pt:.1f})")
    else:
        dx_pt, dy_pt = DISCOVER_FROM_LEFT_TOP_PT
        wlog(f"reopen: Discover 没量到 — 回退固定点 ({dx_pt},{dy_pt})")
    wlog(f"reopen: click Discover icon at window-pt ({dx_pt},{dy_pt})")
    guarded_click(win, b["x"] + dx_pt, b["y"] + dy_pt, settle=1.2)
    # 先看落在哪（2026-11-16 实机教训）：窄窗口发现图标直达朋友圈——
    # 无菜单页；此时若按老流程盲点 MOMENTS_ITEM_FALLBACK 分数点 →
    # 点在封面图上 → 图片查看器弹出。
    at_top, verdict, why = feed_top_check(win, tag + "_menu")
    if at_top or verdict == "moments":
        return verdict, why
    cap = win_capture(win, tag + "_menu2")
    rows = ocr_rows(cap)
    norm = [normalize_for_match(r.get("text") or "") for r in rows]
    menu_visible = ("discover" in norm
                    or ("moments" in norm and "channels" in norm))
    target = None
    if menu_visible:
        for r in rows:
            if normalize_for_match(r.get("text") or "") == "moments":
                target = (r["x"] + r.get("w", 0.06) / 2,
                          r["y"] + r.get("h", 0.02) / 2)
                break
        if target is None:
            wlog("reopen: OCR 未找到 Moments 菜单行 — 用实测兜底点")
            target = MOMENTS_ITEM_FALLBACK
    else:
        wlog(f"reopen: 发现图标后既无朋友圈也无菜单行 "
             f"（{verdict}: {why}）——不盲点，交回上层")
        return verdict, why
    wlog(f"reopen: click 'Moments' item at window "
         f"({target[0]:.3f},{target[1]:.3f})")
    guarded_click(win, b["x"] + target[0] * b["width"],
                  b["y"] + target[1] * b["height"], settle=2.2)
    return check_view(win, tag + "_entered")


def feed_top_check_poll(win, tag, tries=3, gap=2.5):
    """顶部判据轮询（2026-11-22 TestBuddy 实跑 FATAL×2 教训）：重入朋友圈
    后封面照片/名牌是惰性绘制，重入点击后 2-4s 内的单发检查可能量不到
    ⟳ 簇 + 封面名牌 → 假「未达顶部」→ 误 FATAL。间隔重试，tries 次
    仍不达才下结论（真·半路视图多等几秒无害——纯看，不动作）。"""
    at_top, tv, tw = False, "?", "no check"
    for i in range(tries):
        at_top, tv, tw = feed_top_check(win, f"{tag}_t{i}")
        if at_top:
            return at_top, tv, tw
        if i < tries - 1:
            time.sleep(gap)
    return at_top, tv, tw


def reopen_to_top(win):
    """Core of cmd_reopen, callable from inside other commands (the
    zero-wait flight recovery, 2026-11-13): sidebar Discover icon →
    (menu page: OCR-locate 'Moments' row →) re-enter. Pure navigation —
    nothing can be typed or sent. Goal is the feed TOP: re-entry re-tops
    the timeline, BUT clicking Discover while Moments is already open may
    be a no-op (2026-11-16) — if we land mid-feed (time rows = iron
    evidence), escalate: Esc exits Moments, re-enter fresh. Returns
    (verdict, why); "moments" verdicts from here are TOP-verified."""
    verdict, why = _reopen_enter(win, "reopen_a")
    at_top, tv, tw = feed_top_check_poll(win, "reopen_check_a")
    if at_top:
        return "moments", tw
    if verdict == "moments":
        wlog("reopen: 朋友圈已开但不在顶——esc 退出再重入")
        press_esc()
        time.sleep(0.8)
        verdict, why = _reopen_enter(win, "reopen_b")
        at_top, tv, tw = feed_top_check_poll(win, "reopen_check_b")
        if at_top:
            return "moments", tw
    wlog(f"reopen: 未达顶部（{tv}: {tw}）")
    return tv, tw


def cmd_reopen():
    """Re-enter the Moments view from wherever the window drifted (chat,
    profile, Discover). Pure navigation — nothing can be typed or sent."""
    st = load_state()
    if st.get("phase") in PENDING_PHASES:
        print(f"REFUSE: 确认卡挂起（phase={st.get('phase')}）——"
              f"导航会滚走待发帖，先处理确认卡")
        sys.exit(2)
    win = activate()
    verdict, why = reopen_to_top(win)
    print(f"REOPEN: {verdict} ({why})")


def do_refresh(win, px=None, py=None):
    """Core of cmd_refresh (also used by the send-resume flow): pre-check →
    locate ⟳ → click → verify. Order matters (user 2026-11-16): the view
    check runs FIRST — ⟳ is located ONLY on a confirmed-Moments content
    column, never on a wrong tab. Schema measurement (chrome_schema) when
    no explicit (px, py) override is given; fixed right-edge offset
    REFRESH_FROM_RIGHT_TOP_PT if measurement fails.
    Returns (status, why):
      CONFIRMED   timeline observably re-rendered (times rolled / new top)
      UNVERIFIED  view healthy, effect unprovable — NOT a failure
      RECOVERED   definite miss (view broke) → escape ladder ran, refresh
                  did NOT happen
      FATAL       view broken beyond recovery — human needed
    No exits here — the CLI prints + exits; resume_after_send reports and
    keeps its pending flag so the next attempt retries the refresh."""
    b = win.bounds
    # 顶部判据（2026-11-16 教训链）：时间行=半路铁证；⟳ 簇只在"无时间行"
    # 时才算封面顶（窄窗口标题栏 ⟳ 常驻，不能当顶部证据）。不在顶部一律
    # 走恢复梯子（esc → 重入回顶），⟳ 只在站上顶部后才点。
    at_top, verdict, why = feed_top_check(win, "refresh_pre")
    wlog(f"refresh: pre-check 顶部={at_top} {verdict} ({why})")
    if not at_top:
        wlog("refresh: 不在顶部——恢复梯子（esc → 重入回顶）")
        if not recover_view(win, "refresh_prerecover", require_top=True):
            return "FATAL", "起手视图异常且恢复失败（未到顶）"
        at_top, verdict, why = feed_top_check(win, "refresh_pre2")
        if not at_top:
            return "FATAL", f"未能站上顶部（{verdict}: {why}）"
    # baseline for the lucky path: an observable re-render (relative times
    # roll over / new top post) is what upgrades UNVERIFIED to CONFIRMED
    author0, _head0, closer0, _d0, _u0, _b0 = read_top_post(win)
    r_pt, t_pt = REFRESH_FROM_RIGHT_TOP_PT
    if px is None or py is None:
        if os.environ.get("POST_TEST_MISFIRE") == "1":
            px, py = b["x"] + b["width"] - r_pt, b["y"] + 90  # the cover photo
            wlog("refresh: DRILL misfire — clicking the cover photo on purpose")
        else:
            loc = locate_refresh(win_capture(win, "refresh_locate"),
                                 b["width"], b["height"])
            if loc:
                px, py = b["x"] + loc["x_pt"], b["y"] + loc["y_pt"]
                wlog(f"refresh: ⟳ 量得 window-pt ({loc['x_pt']:.1f},"
                     f"{loc['y_pt']:.1f}) — 视图确认后才量")
            else:
                px, py = b["x"] + b["width"] - r_pt, b["y"] + t_pt
                wlog("refresh: ⟳ 没量到 — 回退右缘固定偏移")
        wlog(f"refresh: click ⟳ at screen ({px:.0f},{py:.0f})")
    else:
        wlog(f"refresh: click ⟳ at screen ({px:.0f},{py:.0f}) "
             f"(explicit point)")
    guarded_click(win, px, py, settle=2.4)
    verdict2, why2 = check_view(win, "refresh_post")
    if verdict2 not in VIEW_OK:
        wlog(f"refresh: MISS — view is {verdict2} ({why2}) → recovery ladder")
        if not recover_view(win, "refresh_recover"):
            return "FATAL", "点击后视图异常且恢复失败"
        return "RECOVERED", "这次点击按错了，已善后；刷新未完成"
    author, head, closer, _div, _unit, _bounded = read_top_post(win)
    changed = (bool(closer) and closer != closer0) or \
              (bool(author) and author != author0)
    if changed:
        return "CONFIRMED", (f"时间线已重渲染 "
                             f"({author0!r}/{(closer0 or '-')!r} → "
                             f"{author!r}/{(closer or '-')!r})")
    return "UNVERIFIED", "已执行、视图正常；无新内容，无法确证是否生效"


def cmd_refresh():
    """Click the native ⟳ toolbar button — the sanctioned refresh (drag
    gestures are banned). Call at/near the feed top: the toolbar overlays
    the cover there; mid-feed placement is unverified.

    HONEST OUTCOMES ONLY (design rule 2026-11-09): a refresh with no new
    content is INDISTINGUISHABLE from a miss that hit a harmless dead
    pixel, so this never claims plain 'success'.
    POST_TEST_MISFIRE=1 drills the failure path by clicking the cover
    photo instead (regression drill for the recovery ladder)."""
    st = load_state()
    if st.get("phase") in PENDING_PHASES:
        print(f"REFUSE: 确认卡挂起（phase={st.get('phase')}）——"
              f"刷新可能把输入框关掉，先处理确认卡")
        sys.exit(2)
    win = activate()
    status, why = do_refresh(win)
    if status == "FATAL":
        print(f"FATAL: {why}，请人工看一眼微信")
        sys.exit(2)
    if status == "RECOVERED":
        print(f"RECOVERED: {why}")
        sys.exit(2)
    if status == "CONFIRMED":
        print(f"REFRESH CONFIRMED: {why}")
    else:
        print(f"REFRESH UNVERIFIED: {why}"
              "（这不是失败——下次刷新自然会再验证）")


# Send-resume fast-forward (user design 2026-11-13, replaces pixel memory;
# zero-wait edition same day, user-approved after the noon FF flew): 0.3h
# steps — the smallest sane quantum, verified per step (ff_walk_to_anchor) —
# keep a 0.7h overlap between consecutive screens so every post header lands
# on some screen (the collector's 2026-11-07 lesson — exact tiling can drop
# a short post into a gap forever); 120 steps ≈ 36 screens ≈ 36h of travel,
# deeper than the whole ≤1-day walk could reach. Recovery budget (flight →
# auto reopen + re-anchor) shared with the normal walk's brake: FF_RECOVER.
FF_STEP_H = 0.3
FF_MAX_STEPS = 120
FF_RECOVER_ATTEMPTS = 2


def ff_walk_to_anchor(win, st):
    """Checked fast-forward to the ledger's LAST entry (the anchor) — the
    zero-wait recovery walk (user-approved design 2026-11-13, replaces the
    unverified 0.6h coast that flew 8/8 at 12:46). 0.3h steps; EVERY step's
    real displacement is verified with _row_shift against the commanded
    step (motion_verdict — same thresholds as the normal walk's motion
    check): 'clean'/'aim' keep going, two consecutive stalls fail honestly
    (frontier dead wheels self-heal in one step, so ONE stall is tolerated).
    Returns (outcome, step_i):
      "anchor" — the just-replied post is seen again; normal walk resumes
      "flight" — a wheel overshot far beyond its command. Live-measured
                 2026-11-13 15:24: INSIDE the send window every wheel lands
                 EXACTLY one viewport (+1.00h for a 0.3h command, 3/3
                 identical) — deterministic, so the first flight IS the
                 window probe; callers defer instead of re-trying wheels.
      "stall"  — two consecutive wheels didn't move as commanded
      "cap"    — FF_MAX_STEPS without the anchor (identity lost)
    Inserts (mid-task new posts above the anchor) are logged and skipped
    per the standing task rule; seen posts coast past. Assumes the view is
    ALREADY at the feed top (caller refreshes/reopens first)."""
    seen = st.get("seen") or []
    anchor = seen[-1] if seen else {}
    akey = anchor.get("key") or ""
    if not akey:
        return "anchor", 0  # nothing to anchor to — resume is trivially done
    pool = [e.get("key") or "" for e in seen]
    b = win.bounds
    prev_rows = None
    prev_cmd = 0.0
    frozen = 0
    for i in range(FF_MAX_STEPS):
        author, head, _c, _div, _unit, _bounded = read_top_post(win)
        shutil.copy(f"{OUT}/read.png", f"{OUT}/ff{i}.png")
        rows = ocr_rows_cached(f"{OUT}/read.png")
        # per-step motion check: the previous wheel must have moved ~as
        # commanded. A flight is caught here at one-step latency — this is
        # the brake the noon FF lacked (user rule: 快而准，不等不飞).
        if prev_rows is not None and prev_cmd > 0:
            w = _row_shift(prev_rows, rows)
            v = motion_verdict(w, prev_cmd)
            if v == "over":
                wlog(f"ff: 第 {i} 步位移 {w:+.2f}h（指令 {prev_cmd:.2f}h）"
                     f"——飞行，定位中断")
                return "flight", i
            if v == "stall":
                frozen += 1
                wlog(f"ff: 第 {i} 步位移 {w:+.2f}h（指令 {prev_cmd:.2f}h）"
                     f"——异常 {frozen}/2")
                if frozen >= 2:
                    wlog("ff: 连续两步没按指令移动 — 定位失败")
                    return "stall", i
            else:
                if v == "aim":
                    wlog(f"ff: 第 {i} 步位移 +{w:.2f}h"
                         f"（指令 {prev_cmd:.2f}h）——重瞄继续")
                frozen = 0
        key = (author or "") + "|" + (head or "")[:30]
        d = ff_step_decision(key, [r.get("text") or "" for r in rows],
                             akey, anchor.get("head") or "", pool)
        if d == "anchor":
            wlog(f"ff: 锚点命中（第 {i} 步，{anchor.get('author')!r}）")
            return "anchor", i
        if d == "insert":
            wlog(f"ff: 新增帖（{author!r}）——本任务不处理，快进越过")
            print(f"SKIP-NEW: 新增帖 {author!r}——本任务规则：不处理新增帖")
        else:
            wlog(f"ff: 已处理（{author!r}）——快进越过")
        if not feed_scroll(win, -FF_STEP_H * b["height"]):
            # feed_scroll's fp check saw no movement (frontier dead wheel —
            # self-heals within a step). Do NOT count it here: the stale
            # baseline (prev_rows stays this screen's rows) makes the NEXT
            # motion check measure THIS wheel at ~0 = stall — the row-shift
            # judge is the single counter; two consecutive stalls fail the
            # walk, one self-healed dead (retry wheel moves → clean) resets.
            continue
        prev_rows = rows
        prev_cmd = FF_STEP_H
    return "cap", FF_MAX_STEPS


def recover_to_anchor(win, st):
    """Zero-wait flight recovery for the NORMAL walk (no send just happened
    — flights are send-locked, 14/14 observed): reopen to the feed top,
    then the checked 0.3h fast-forward down the seen zone to the ledger's
    last entry. Returns (outcome, i) from ff_walk_to_anchor, or
    ("view", 0) if the reopen landed somewhere that isn't the feed. The
    send-resume path does NOT use this — inside the send window a reopen
    retry is proven waste (deterministic viewport jumps); it defers
    instead (resume_after_send)."""
    verdict, why = reopen_to_top(win)
    if verdict not in VIEW_OK:
        wlog(f"recover: reopen 后视图异常（{verdict}: {why}）")
        return "view", 0
    return ff_walk_to_anchor(win, st)


def resume_after_send(win, st):
    """Post-send recovery, probe-defer edition (2026-11-13 晚, user-bounded
    改最后一次). Live-measured fact (15:24, 3/3 identical + noon's 8-wheel
    fly-through): INSIDE the send window EVERY wheel lands EXACTLY one
    viewport (+1.00h for a 0.3h command) — deterministic, so reopen
    retries inside the window are proven waste (they all flew). Design:
    ⟳ rebuild the view (re-tops the feed; verified CONFIRMED mid-feed),
    then ff_walk_to_anchor whose first checked step IS the probe.
      anchor → flag cleared, RESUME, normal walk continues
      flight → window open: clean WINDOW defer — ff_pending KEPT, no
               reopen dance, no fake success, no sleep (等待协议 user-
               rejected: 30秒已经太久太久); the next `next` re-probes and
               succeeds once the window has closed (minutes, per both
               live observations)
      stall/cap → honest RESUME-FAIL: flag cleared, normal walking with
               human-gated cards
    Fresh posts met before the anchor are inserts pushed in at the top
    (reverse-chron): per user rule they are logged and skipped this task.
    FATAL/RECOVERED refresh keeps the flag and stops — next `next`
    retries the whole resume."""
    seen = st.get("seen") or []
    anchor = seen[-1] if seen else {}
    status, why = do_refresh(win)
    if status in ("FATAL", "RECOVERED"):
        wlog(f"resume: 刷新未完成（{status}: {why}）——停车，下次 next 重试")
        print(f"FATAL: 发送后刷新未完成（{why}）——请人工看一眼微信；"
              f"ff_pending 保留，下次 next 自动重试")
        sys.exit(2)
    wlog(f"resume: ⟳ {status} — {why}；核验快进定位锚点 "
         f"({anchor.get('author')!r})，首步兼作窗口探针")
    outcome, i = ff_walk_to_anchor(win, st)
    if outcome == "anchor":
        st["ff_pending"] = False
        save_state(st)
        wlog(f"resume: 锚点命中（第 {i} 步）——恢复常规行走")
        print(f"RESUME: 已定位到已处理区底部（{anchor.get('author')!r}），"
              f"继续常规行走")
        return
    if outcome == "flight":
        wlog(f"resume: 第 {i} 步测到窗口签名（位移远超指令）——干净推迟，"
             f"不烧重试，ff_pending 保留")
        print("WINDOW: 发送重整期未过（窗口内滚轮确定性整屏跳）——已干净"
              "退出，ff_pending 保留；下次 next 自动重试，无需任何等待")
        sys.exit(2)
    st["ff_pending"] = False
    save_state(st)
    wlog(f"resume: 快进未命中锚点（{outcome}，第 {i} 步）——转常规行走")
    print(f"RESUME-FAIL: 发送后定位未成（{outcome}），已转常规行走；接下来"
          f"弹的卡请人工把关（截图证据 {OUT}/ff*.png）")


def build_fresh_prompt(author, text, persona=None):
    """组稿 prompt = reviewer 宪法模板 + 回复定制。定制 = reply_settings
    的【本次定制】节（style 风格片段 + extra 自由补充；webapp UI 与
    moments-mcp settings 工具写的同一份存储），插在『要求：』之前——
    输出格式指令保持在末尾不被稀释。persona 可注入（generate_fresh
    复用同一次解析——署名与人设必须同源；2026-11-22 AuthorI 冤案教训：
    拆函数时把 persona 留在原函数外，签名步 NameError，LLM 成功也
    白烧）。无注入时自解析，纯函数可单测。"""
    from services.digest.reviewer import PROMPT_TEMPLATE, _resolve_persona
    from services.digest.reply_settings import (
        customization_block, inject_customization)
    if persona is None:
        persona = _resolve_persona(None)
    prompt = PROMPT_TEMPLATE.format(
        persona_name=persona["name"], search_block="（无）",
        author=author or "（未知）", text=text or "（无正文）",
        comments_block="（无可见评论）")
    return inject_customization(prompt, customization_block())


def generate_fresh(author, text):
    """Fresh per-post persona comment from what's actually on screen —
    reuses reviewer's tuned prompt (乱码忽略/情绪倾向/多样开头) and the free
    summarize model. NO cached drafts, NO DB. The key is never printed.
    回复定制（reply_settings）每帖现读现用——改完定制下一帖即生效；
    署名由 persona 追加（2026-11-25 用户令起可经 settings 的
    signature_label 定制或清空，resolve_signature 单一事实源；""
    =不加署名）。"""
    from services.digest.reviewer import (
        body_understandable, TooShortToReply, _resolve_persona)
    # 帖文太短不硬回（用户 2026-11-15 令）：确定性下限先拦，连模型都不调
    if not body_understandable(text):
        raise TooShortToReply("正文可见文字太少，不足以理解帖子内容")
    from services.digest.config import load_env_file
    load_env_file()
    from openai import OpenAI
    persona = _resolve_persona(None)
    prompt = build_fresh_prompt(author, text, persona=persona)
    model = (os.environ.get("DIGEST_SUMMARIZE_MODEL")
             or os.environ.get("LLM_MODEL", "glm-4.6"))
    base_url = os.environ.get(
        "LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/")
    api_key = os.environ.get("LLM_API_KEY", "")
    client = OpenAI(base_url=base_url, api_key=api_key, timeout=120)
    resp = client.chat.completions.create(
        model=model, messages=[{"role": "user", "content": prompt}],
        temperature=0.4)
    content = (resp.choices[0].message.content if resp.choices else "") or ""
    content = content.strip().strip("`").strip()
    if content == "SKIP":
        # prompt 规则 3：模型判断正文残缺到无法理解动态内容
        raise TooShortToReply("AI 判断正文不足以理解帖子内容")
    sig = persona["signature"]
    if sig and sig not in content:
        content = f"{content} {sig}".strip()
    return content


def older_than_one_day(closer):
    """The walk stops at posts older than ~1 day (user rule 2026-11-08).
    Relative times parse directly; anything else (absolute date/weekday)
    counts as beyond a day; a missing closer never stops the walk."""
    c = (closer or "").lower()
    if not c:
        return False
    m = re.search(r"(\d+)\s*day", c)
    if m:
        return int(m.group(1)) >= 2
    if any(k in c for k in ("hour", "minute", "yesterday", "刚刚", "分钟",
                            "小时", "昨天")):
        return False
    return True


def _author_match(a, b):
    """Fuzzy author identity — OCR char jitter must neither re-identify the
    next post as the target nor miss a real handover (same floor as the
    fire-time author recheck)."""
    return SequenceMatcher(None, normalize_for_match(a),
                           normalize_for_match(b)).ratio() >= 0.75


def read_long_post(win, unit, closer, author=""):
    """Full-text reading for long posts (user: 老侯 case, 2026-11-09).
    The feed FOLDS long text behind Show More — scrolling never reveals
    it, so click the expand link first and re-read. If the closing time
    row is STILL not on screen the post runs past the fold: stitch screens
    downward (≤10 scrolls, user rule '最多翻10次'), absorbing unique rows,
    until the time row closes the post. Returns (closer, text).

    HARD STOP at the next post's door (2026-11-13): a fresh author row
    that is NOT the target's ends the stitch — the target's own tail rows
    on that screen (pre_texts) are absorbed, but the next post's texts and
    closer are never taken. Live 2026-11-09 17:00: the stitch ran past the
    target and grabbed the post below's '2 day(s) ago' as the closer,
    faking a 1-day boundary stop."""
    b = win.bounds
    rows = list(unit.get("texts") or [])
    expand = unit.get("expand")
    if expand is not None:
        wlog(f"expand: 点击 Show More {expand}")
        make_expand_fn(b)(expand)
        time.sleep(1.2)
        _a, _h, closer, _dy, unit, _bounded = read_top_post(win)
        rows = list(unit.get("texts") or [])
        wlog(f"expand 后 closer={closer!r} rows={len(rows)}")
    seen = set()
    ordered = []

    def absorb(texts):
        for t in texts:
            n = normalize_for_match(t)
            if n and n not in seen:
                seen.add(n)
                ordered.append((t or "").strip())

    absorb(rows)
    if closer:
        return closer, "\n".join(ordered)
    door_stopped = False
    for screen in range(9):  # first screen already read → ≤10 reads total
        if not feed_scroll(win, -0.5 * b["height"]):
            wlog("stitch: 滚不动了，用已读部分收工")
            break
        capture_window(win.window_id, f"{OUT}/read.png")
        u2 = read_top_item(f"{OUT}/read.png", mid_feed=True)["unit"]
        u2_author = (u2.get("author") or "").strip()
        if u2_author and author and not _author_match(u2_author, author):
            # Next post's door: absorb only the target's tail (rows above
            # the new author row) and stop — never the new post's texts
            # or its closer. (A folded-comments strip has no author row,
            # so it never triggers this — the stitch correctly walks
            # through the target's own comment folds.)
            absorb(u2.get("pre_texts") or [])
            wlog(f"stitch#{screen + 1}: 下一帖门牌（{u2_author!r}）——停手，"
                 f"累计={len(ordered)} 行，未取新帖 closer")
            door_stopped = True
            break
        absorb(u2.get("texts") or [])
        wlog(f"stitch#{screen + 1}: rows={len(u2.get('texts') or [])} "
             f"closer={u2.get('closer')!r} 累计={len(ordered)} 行")
        if u2.get("closer"):
            closer = u2["closer"]
            break
    if not closer and not door_stopped:
        wlog("stitch: 10 屏读完仍无时间行——用已读部分收工")
    return closer, "\n".join(ordered)


def process_post(win):
    """Attribute the top post; skip sponsored (arrow) and empty-text posts
    with a ledger entry; otherwise generate a FRESH comment for what's on
    screen and set it up for the confirm card."""
    author, head, closer, _div_y, unit, _bounded = read_top_post(win)
    if not author:
        print(f"RESULT: 顶帖无法归属 head={head!r}")
        sys.exit(2)
    # A1 (2026-11-09): long posts get expanded + stitched full-text reading;
    # the stitched closer also makes the 1-day boundary accurate for them
    closer, raw_text = read_long_post(win, unit, closer, author)
    text = raw_text.strip()
    sponsored = bool(unit.get("author_arrow"))
    # NOTE: the signature done-marker was removed per user directive
    # 2026-11-08 — already-replied posts must still get a card (the user
    # skips them by hand); first-line keys keep the walk progressing.
    print(f"RESULT: author={author!r} sponsored={sponsored} text_len={len(text)}")
    print(f"RESULT: closer={closer!r}")
    st = load_state()
    key = author + "|" + (head or "")[:30]
    if older_than_one_day(closer):
        # strict boundary: the feed is strictly newest→oldest top-down
        # (user correction 2026-11-08) — the first ≥1-day post IS the edge
        st["old_streak"] = 0
        entry = {"key": key, "author": author, "head": (head or "")[:40],
                 "closer": closer, "draft_id": None, "action": "boundary"}
        seen = st.setdefault("seen", [])
        if not (seen and seen[-1]["key"] == key):
            seen.append(entry)
        st["last_key"] = key
        st.update({"phase": "boundary", "author": author})
        save_state(st)
        print("BOUNDARY: 这条已超过一天——按规则停（继续往回翻请明示）")
        sys.exit(0)
    if sponsored:
        action = "sponsored"
    elif len(text) == 0 and not closer:
        # a 0-char body with NO time row is a fragment misattribution
        # (live 2026-11-08: my comment's wrapped signature '机器人）' became
        # an "author" with empty text). A REAL photo post keeps its time
        # row, so empty-text-with-closer still gets a card below.
        action = "no-text"
        print("RESULT: 跳过：无可读正文且无时间行（碎片误归属），已记台账")
    elif len(text) < 4:
        # no silent skip: the user must see every post (live 2026-11-08
        # "一天内的被你错过了不少") — card it with edit/skip options
        action = "gen"
        print(f"RESULT: 注意：正文极短（{len(text)} 字），评论会偏泛")
    else:
        action = "gen"
    entry = {"key": key, "author": author, "head": (head or "")[:40],
             "closer": closer, "draft_id": None, "action": action}
    seen = st.setdefault("seen", [])
    if seen and seen[-1]["key"] == key:
        seen[-1] = entry
    else:
        seen.append(entry)
    st["last_key"] = key
    if action != "gen":
        print(f"RESULT: 跳过（{action}），已记台账")
        st.update({"phase": "skip", "author": author, "skip_reason": action})
        save_state(st)
        return
    print("RESULT: 正在为屏幕上的这一帖现场生成新草稿…")
    try:
        fresh = generate_fresh(author, text)
    except Exception as e:
        entry["action"] = "gen-failed"
        st.update({"phase": "skip", "skip_reason": "gen-failed"})
        save_state(st)
        print(f"FATAL: 生成失败 {e}")
        sys.exit(2)
    print(f"RESULT: fresh={fresh!r}")
    entry["action"] = "gen"
    st.update({"phase": "read", "author": author, "head": head,
               "closer": closer, "draft_id": None, "item_id": None,
               "text": fresh})
    save_state(st)


# ------------------------------------------------------------ draft match

def match_draft(conn, author, text_head):
    """Newest-first DB digest items: author must equal, normalized text
    prefix must overlap. Empty-text posts (photo/video) match on author
    alone. Returns (draft_row, item_id) or (None, None)."""
    want = normalize_for_match(text_head)[:20]
    rows = conn.execute(
        "SELECT t.id AS draft_id, t.content, d.id AS item_id, r.author, r.text "
        "FROM drafts t JOIN digest_items d ON d.id = t.digest_item_id "
        "JOIN raw_items r ON r.id = d.raw_item_id "
        "WHERE t.channel = 'moments_comment' AND t.status = 'draft' "
        "ORDER BY d.id DESC").fetchall()
    author_n = normalize_for_match(author)
    for r in rows:
        if normalize_for_match(r["author"]) != author_n:
            continue
        item_n = normalize_for_match(r["text"])
        if not item_n or not want or item_n[:20] == want or \
                (len(want) >= 8 and item_n.startswith(want[:12])):
            draft = conn.execute("SELECT * FROM drafts WHERE id = ?",
                                 (r["draft_id"],)).fetchone()
            return dict(draft), r["item_id"]
    return None, None


def read_top_post(win):
    """Capture + attribute the top post. Returns (author, text_head, closer,
    divider_y, unit, bounded). `bounded` = the read is ALIGNED at a divider
    (top divider ≤ TOP_ALIGNED_MAX_Y) — the region below it is header-
    bounded, so author='' there means 'no doorplate on this line', which
    the walk reads structurally (door check), never as a failure. OCR
    sometimes glues 「作者：正文首行」 into one row (live 2026-11-08) —
    split at the colon when the head looks like a name."""
    capture_window(win.window_id, f"{OUT}/read.png")
    _OCR_CACHE["path"] = None  # read.png was just overwritten
    reading = read_top_item(f"{OUT}/read.png", ocr_rows_fn=ocr_rows_cached,
                            mid_feed=True)
    unit = reading["unit"]
    author = unit.get("author") or ""
    texts = unit.get("texts") or []
    # key on the FIRST text line only: my sent comments glue onto the tail
    # of the unit and must never change a post's identity (live 2026-11-08:
    # AuthorD's post got re-processed after commenting)
    head = (texts[0] if texts else "")[:60]
    closer = unit.get("closer") or ""
    for sep in ("：", ":"):
        if sep in author:
            name, rest = author.split(sep, 1)
            if 0 < len(name) <= 12:
                author, head = name, (rest + head)[:60]
                break
    if not author:
        # EN absolute time glued with author+text in one OCR row
        # ("Yesterday AuthorD：突然怀旧…") — the known extraction
        # limitation, handled here for posting
        m = re.match(r"^(yesterday|today|昨天|今天|前天)\s*([^：:]{1,12})[：:]",
                     head, re.I)
        if m:
            author, head = m.group(2), head[m.end():]
    if author in OWN_NAMES or author in UI_TOKENS:
        # my own comment strip / UI chrome is not a post — the Test Owner
        # comment rows caused the jumping; "Show More" hides collapsed
        # long text whose real header is above (live 2026-11-08)
        author = ""
    if author and (
            re.search(r"(\d+\s*(hour|minute|day)|channels|official\s*accounts"
                      r"|^yesterday|^today)", (author or "").lower())
            or re.search(r"[。！？…；、，（）()]", author)
            or re.search(r"[吧呢吗啊呀哦嘛喔呗咯啦]$", author)
            or "情绪价值" in author or "机器人" in author
            or "AI助手" in author or "自动回复" in author
            or re.search(r"reply|回复", (author or "").lower())
            or (len(author) > 14 and "@" not in author)):
        # a relative-time/channel row, a text fragment (sentence punctuation
        # or >14 chars) is never the author — null it so the walk treats
        # the line structurally (door check) instead of carding a fake
        # post (live 2026-11-08: "绚烂的色彩。" was processed as a fake
        # post and the walk jumped past real ones).
        # LIVE 2026-11-09 additions: (1) reply rows ('A Reply B：…' /
        # 'A 回复 B：…') in comment strips are conversations, not authors;
        # (2) names >14 chars are ONLY trusted when they carry the
        # service-account @ marker ('安徽服务管家0127@平安产险服务管…',
        # 20 chars — the extractor's own AUTHOR_MAX_LEN=30 still bounds
        # it). Consequence under the 2026-11-09 geometric walk: a nulled
        # real door surfaces via the door check (pre_closer → honest
        # brake), never as a phantom failure count.
        author = ""
    return (author, head, closer, reading.get("divider_y"), unit,
            reading.get("bounded", False))


# ---------------------------------------------------------------- phases

def cmd_read(first):
    win = activate()
    require_view_ok(win, "guard_read")
    b = win.bounds
    if first:
        # wheel-up until the view stops moving TWICE in a row (a single
        # stalled scroll over an image post is not the top — live lesson)
        no_change = 0
        for _ in range(12):
            if not feed_scroll(win, b["height"]):
                no_change += 1
                if no_change >= 2:
                    break
            else:
                no_change = 0
        # pull-to-refresh is BANNED (never triggered; the press-drag moved
        # the window and opened a video, live 2026-11-08). Mechanical top of
        # the current render is what we walk.
    else:
        process_post(win)
        return
    process_post(win)


def cmd_box(text_override=None):
    st = load_state()
    if st.get("phase") != "read":
        print("FATAL: 状态不对（先 read）")
        sys.exit(1)
    win = activate()
    require_view_ok(win, "guard_box")
    b = win.bounds
    # re-verify the post's identity on screen before touching anything.
    # Primary: the author (fuzzy — OCR char jitter '蛋壳'→'蛋売' must not
    # abort a legitimate fire). Fallback (2026-11-09 live): after A1
    # full-text reading the view sits at the post TAIL — the author row
    # has scrolled off (author='') — then the post's own closer row visible
    # on screen proves identity just as well (the closer is part of the
    # approved state; the •• menu is anchored on that row anyway).
    capture_window(win.window_id, f"{OUT}/box0.png")
    rows = ocr_rows(f"{OUT}/box0.png")
    author, head, closer, div_y, unit, _bounded = read_top_post(win)
    a_n, s_n = normalize_for_match(author), normalize_for_match(st["author"])
    st_closer_n = normalize_for_match(st.get("closer") or "")
    closer_row = None
    if st_closer_n:
        for r in rows:
            if normalize_for_match(r.get("text") or "").startswith(st_closer_n[:12]):
                closer_row = r
                break
    if not (SequenceMatcher(None, a_n, s_n).ratio() >= 0.75
            or (not author and closer_row is not None)):
        print(f"FATAL: 顶帖作者变了 screen={author!r} state={st['author']!r}，不动")
        sys.exit(1)
    text = text_override or st["text"]

    # locate THIS post's time row (between its author row and the divider),
    # then click the •• glyph at the measured MORE_X on that row — the glyph
    # itself never OCRs (poster_probe recon), hence the fixed x.
    # The state's closer (approved identity) is the top-priority anchor.
    time_row = closer_row
    if time_row is None:
        closer_n = normalize_for_match(closer)
        if closer_n:
            for r in rows:
                if normalize_for_match(r.get("text") or "").startswith(closer_n[:12]):
                    time_row = r
                    break
    if time_row is None:
        lo = unit.get("author_y") or 0.0
        hi = div_y or 1.0
        for r in rows:
            if is_time_row(r.get("text") or "") and lo < r["y"] < hi:
                time_row = r
                break
    if time_row is None:
        # tall post: the time row sits below the fold — scroll down in
        # steps, anchoring on the post's own text so the time row we find
        # provably belongs to THIS post
        anchor_n = normalize_for_match(head)[:12]
        for _try in range(3):
            if not feed_scroll(win, -0.3 * b["height"]):
                break
            capture_window(win.window_id, f"{OUT}/box0.png")
            rows = ocr_rows(f"{OUT}/box0.png")
            anchor_y = None
            if anchor_n:
                for r in rows:
                    if normalize_for_match(r.get("text") or "").startswith(anchor_n):
                        anchor_y = r["y"]
                        break
            cands = [r for r in rows
                     if (is_time_row(r.get("text") or "")
                         or is_time_row_prefix(r.get("text") or ""))
                     and (anchor_y is None or r["y"] > anchor_y)]
            if anchor_y is not None and cands:
                time_row = cands[0]
                break
        if time_row is None:
            print("FATAL: 滚动后仍未找到本帖时间行——不动作")
            sys.exit(1)
    dy = b["y"] + (time_row["y"] + time_row.get("h", 0.015) / 2) * b["height"]
    dx = b["x"] + MORE_X * b["width"]
    guarded_click(win, dx, dy, settle=1.0)

    # popup: Comment pill inside the window rect at the same y band
    full_capture(f"{OUT}/box1.png")
    b = fresh_win().bounds
    comment = None
    for r, px, py in rows_in(ocr_rows(f"{OUT}/box1.png"), b):
        n = normalize_for_match(r.get("text") or "")
        if ("comment" in n or "评论" in n) and abs(py - dy) < 0.04 * SH:
            comment = (r, px, py)
            break
    if comment is None:
        print("FATAL: 弹层没出现或没有 Comment（已不动，请人工看一眼微信）")
        sys.exit(1)
    _, px, py = comment
    guarded_click(win, px, py, settle=1.2)

    # input box: click into the field (its placeholder row / Send row band)
    full_capture(f"{OUT}/box2.png")
    b = fresh_win().bounds
    send_btn = None
    field_row = None
    for r, px, py in rows_in(ocr_rows(f"{OUT}/box2.png"), b):
        n = normalize_for_match(r.get("text") or "")
        if n.startswith("send") and send_btn is None:
            send_btn = (r, px, py)
        if n in ("comment", "评论") and field_row is None and r["y"] > 0.4:
            field_row = (r, px, py)
    if send_btn is None:
        print("FATAL: 输入框没出现（没有 Send 按钮）——中止，未粘贴未发送")
        press_esc()
        sys.exit(1)
    if field_row is not None:
        # click the placeholder text center exactly — offsetting left lands
        # outside the panel and dismisses the box (live 2026-11-08)
        guarded_click(win, field_row[1], field_row[2], settle=0.5)
    else:
        guarded_click(win, b["x"] + 0.5 * b["width"], send_btn[2] - 40, settle=0.5)

    # paste via clipboard
    p = subprocess.run(["pbcopy"], input=text.encode("utf-8"),
                       capture_output=True, timeout=10)
    if p.returncode != 0:
        print("FATAL: pbcopy 失败")
        sys.exit(1)
    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(None, 9, down)  # 9 = V
        Quartz.CGEventSetFlags(ev, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.08)
    time.sleep(0.8)

    # verify the text is in the box
    full_capture(f"{OUT}/box3.png")
    ok, seen = paste_verified(f"{OUT}/box3.png", text)
    print(f"RESULT: 粘贴核验 {'OK' if ok else 'FAIL'}（框内 OCR 是否含文字头）")
    print(f"RESULT: text={text!r}")
    print(f"RESULT: send_btn at screen ({send_btn[1]:.0f}, {send_btn[2]:.0f})")
    if not ok:
        print("FATAL: 文字没进框——不发送；输入框留着，等你决定 abort")
        sys.exit(1)
    st["phase"] = "boxed"
    st["text"] = text
    save_state(st)
    print("READY: 文字已在输入框，等待确认发送")


def do_send(st):
    """Click Send and verify the comment landed. Runs INSIDE the same
    process as the paste — no terminal interaction in between (focus loss
    dismisses the input box, live 2026-11-08)."""
    win = activate()
    full_capture(f"{OUT}/send0.png")
    b = fresh_win().bounds
    send_btn = None
    for r, px, py in rows_in(ocr_rows(f"{OUT}/send0.png"), b):
        if normalize_for_match(r.get("text") or "").startswith("send"):
            send_btn = (r, px, py)
            break
    if send_btn is None:
        print("FATAL: Send 按钮不见了（输入框可能已被关闭）——未发送")
        sys.exit(1)
    guarded_click(win, send_btn[1], send_btn[2], settle=1.5)

    # verify: the comment text now appears under the post (input box gone)
    full_capture(f"{OUT}/send1.png")
    b = fresh_win().bounds
    seen = normalize_for_match("".join(
        r.get("text") or "" for r, px, py in rows_in(ocr_rows(f"{OUT}/send1.png"), b)))
    seen = seen.replace(" ", "")
    n = normalize_for_match(st["text"]).replace(" ", "")
    # 送达只认文本专属探针（头/尾12字）——签名是所有我们评论共用的，
    # 屏上别的自己评论会单独满足它（某历史帧假 ok 同源，2026-11-17）
    ok = bool(n) and (n[:12] in seen or n[-12:] in seen)
    box_gone = all(normalize_for_match(r.get("text") or "") != "send"
                   for r, px, py in rows_in(ocr_rows(f"{OUT}/send1.png"), b))
    print(f"RESULT: 评论可见 {'OK' if ok else 'CHECK'}；输入框已收起 {'OK' if box_gone else 'CHECK'}")
    if ok:
        if st.get("draft_id"):
            conn = db.connect()
            conn.execute("UPDATE drafts SET status='sent', sent_at=datetime('now') "
                         "WHERE id = ? AND status='draft'", (st["draft_id"],))
            conn.commit()
            conn.close()
        st["phase"] = "sent"
        commented = set(st.get("commented", []))
        for e in st.get("seen", []):
            if e["key"] == st.get("last_key"):
                e["action"] = "sent"
                if st.get("draft_id"):
                    e["draft_id"] = st["draft_id"]
        if st.get("last_key"):
            commented.add(st["last_key"])
        st["commented"] = sorted(commented)
        # send-resume (2026-11-13): the send click happened — the replied
        # post's view state is now expanded and the next fine crossing is
        # the one that flies. Arm the refresh+re-anchor for the next `next`.
        st["ff_pending"] = True
        save_state(st)
        tag = (f"draft #{st['draft_id']} 已标记 sent" if st.get("draft_id")
               else "屏幕新草稿已发出")
        print(f"SENT: {tag}（{st['author']}）")
    else:
        # The send click HAPPENED and the box closed — only the visibility
        # probe failed (comment render lags the capture; live 2026-11-09 a
        # collapsed comment truncated all probes). The send attempt must
        # still arm the reply baseline ('sent-unverified' ≠ 'sent' left it
        # disarmed → the next advance read comment-zone fragments as a
        # phantom fresh post: '事地 扯' was a text row next to my own
        # comment). Human eyeballs the CHECK screenshot; if the comment
        # truly failed to land, flip the entry back and re-card.
        st["phase"] = "sent"
        commented = set(st.get("commented", []))
        for e in st.get("seen", []):
            if e["key"] == st.get("last_key"):
                e["action"] = "sent"
        if st.get("last_key"):
            commented.add(st["last_key"])
        st["commented"] = sorted(commented)
        # same arming as the OK branch: the click happened either way
        st["ff_pending"] = True
        save_state(st)
        print("CHECK: 请人工看一眼微信确认评论是否已发出（截图 "
              "/tmp/poster_probe/send1.png）——台账已按已发处理，"
              "如未发出请告知")


def cmd_send():
    st = load_state()
    if st.get("phase") != "boxed":
        print("FATAL: 状态不对（先 box）")
        sys.exit(1)
    do_send(st)


def cmd_fire():
    """Atomic: open box → paste → verify → send, no interaction in between.
    The user's confirmation happened BEFORE this runs."""
    st = load_state()
    if st.get("phase") != "read":
        print("FATAL: 状态不对（先 read 并确认）")
        sys.exit(1)
    cmd_box()
    st = load_state()
    if st.get("phase") != "boxed":
        print("FATAL: 粘贴未通过核验——不发送")
        sys.exit(1)
    do_send(st)


def cmd_abort():
    win = activate()
    press_esc()
    press_esc()
    st = load_state()
    st["phase"] = "aborted"
    save_state(st)
    print("ABORTED: 输入框已撤掉，未发送")


def scan_post(win, author, head, closer, key, unit):
    """A1 read-only stroll step (user permit 2026-11-09: 只读不评): the
    FULL-TEXT reading — expand + ≤10-screen stitch for folded posts —
    WITHOUT drafting or carding. Scanned posts land in their own
    'scanned' ledger so the FORMAL walk still treats them as untouched
    (only the user's skip decisions belong in 'seen')."""
    folded = unit.get("expand") is not None
    closer2, raw = read_long_post(win, unit, closer, author)
    text = raw.strip()
    old = older_than_one_day(closer2)
    wlog(f"scan: author={author!r} folded={folded} text_len={len(text)} "
         f"closer={closer2!r} beyond_day={old}")
    print(f"SCAN: author={author!r} folded={folded} text_len={len(text)} "
          f"closer={closer2!r} beyond_day={old}")
    st = load_state()
    scanned = st.setdefault("scanned", [])
    # evidence discipline (live gap 2026-11-09: the 侯哥 730-char read left
    # NO text on disk — only text_len in a log line): dump every scan's
    # full text next to the other evidence captures
    fname = f"{OUT}/scan_{len(scanned):02d}.txt"
    with open(fname, "w") as f:
        f.write(text)
    scanned.append({"key": key, "author": author, "head": (head or "")[:40],
                    "closer": closer2, "action": "scan",
                    "text_len": len(text), "text_file": fname})
    st["last_key"] = key
    save_state(st)
    return folded


def cmd_next(scan_limit=0):
    """Advance exactly ONE post per step — the DIVIDER is the sole decider
    (user rule 2026-11-08: a new post starts at the next separator line).

    USER MODEL (2026-11-09 定型, plain-language): walking is PURE GEOMETRY;
    attribution is a DOOR duty. The loop never counts 'failed
    attributions' — mid-walk reads are SUPPOSED to be anonymous (the
    doorplate only becomes readable once the divider lands at the top).
    The walk:
    - divider on screen → wheel toward it, ≤0.3h per step, each step's
      REAL displacement (median shift of the text rows themselves —
      works with or without a divider on screen) must match the command
      (spin / inertia-flight detection);
    - no divider on screen → blind 0.3h steps until one appears (2.4h cap
      — more than two screens of nothing means we're lost);
    - line landed (bounded read) → door: author found → post (fresh =
      process/card, known = walk past); author='' → mostly comment-zone
      strips / folded threads (walk on), EXCEPT a region that carries a
      closing TIME row but no name — a real post we failed to read;
      每帖必弹卡 forbids silently crossing it → stop for a human.
    All motion is wheel (PageDown + its focus-click scaffolding removed
    2026-11-09: cursor/focus independence). Only down, never up.

    scan_limit>0 switches to the A1 read-only stroll: same advance
    machinery, fresh posts get full-text reading with NO draft and NO card
    (只读不评 permit), the stroll runs past the 1-day boundary, and it
    STOPS at the first folded long post — that post is the A1 test
    subject and gets a full report."""
    win = activate()
    require_view_ok(win, "guard_next")
    b = win.bounds
    st = load_state()
    if st.get("ff_pending"):
        # 发送后恢复（2026-11-13）：⟳ 重建视图 + 快进认锚，回复后那一步
        # 精细跨越不再被行走。清旗返回或停车退出，两条路都收敛。
        resume_after_send(win, st)
    last_key = st.get("last_key")
    no_brake = bool(os.environ.get("POST_TEST_NOBRAKE"))
    seen_keys = {e["key"] for e in st.get("seen", [])}
    scanned_keys = {e["key"] for e in st.get("scanned", [])}
    known = [last_key] if last_key else []
    session = []  # keys scanned during THIS run — the freshness check must
    # see them or the stroll re-scans the same top post forever
    scanned_count = 0
    recover_budget = 0  # flight auto-recoveries used this invocation (cap
    # FF_RECOVER_ATTEMPTS — zero-wait design 2026-11-13: 飞了不再停车叫人，
    # 自动 reopen + 认帖重走；预算用尽才交人工)
    prev_kind = "start"  # previous advance step: walk / flush / seek.
    # The motion check only compares within a walk streak — a flush, door
    # handoff or blind seek has no row-continuity baseline.
    prev_rows = None   # previous read's OCR rows (row-shift motion check)
    prev_cmd_h = 0.0   # previous commanded wheel step (fraction of window
    # height — the motion check compares against this)
    frozen = 0    # consecutive walk steps where the tracked line failed to
    # advance ~as commanded (scroll fighting inertia / spinning inside a
    # comment zone). Two in a row = execution failure → brake.
    seek_h = 0.0  # cumulative commanded wheel travel with NO divider on
    # screen. Tall divider-less posts are legitimate; >2.4h (≈2.4 screens)
    # of pure nothing means we're stuck somewhere without structure.
    doorless = 0  # consecutive ALIGNED reads whose region below the
    # divider carries no author (comment strips / folded threads). A long
    # streak = grinding through a comment area without reaching a post.
    for _attempt in range(30 * max(1, scan_limit)):
        author, head, _closer, div_y, unit, bounded = read_top_post(win)
        key = (author or "") + "|" + (head or "")[:30]
        # per-step evidence chain: the capture this read is based on
        shutil.copy(f"{OUT}/read.png", f"{OUT}/adv{_attempt}.png")
        wlog(f"read#{_attempt}: author={author!r} div_y={div_y} "
             f"bounded={bounded} head={(head or '')[:18]!r}")
        pool = known + list(seen_keys) + (
            list(scanned_keys) + session if scan_limit else [])
        # user rule (2026-11-09): the author-row dropdown badge marks a
        # promoted post — skip WITHOUT a card and WITHOUT a door failure.
        # The ad card's borders fragment the region model, so this
        # usually fires while the post is still unattributable.
        ad = bool(unit.get("sponsored_ad"))
        if ad:
            adkey = "sponsored|" + (head or "")[:30]
            ledger = st.setdefault("seen", [])
            if not any(e["key"] == adkey for e in ledger):
                ledger.append({"key": adkey, "author": "sponsored",
                               "head": (head or "")[:40], "closer": "",
                               "draft_id": None, "action": "sponsored"})
                save_state(st)
            wlog(f"read#{_attempt}: sponsored badge — skip, advance")
            print("SKIP-AD: sponsored 帖（作者行下拉徽章）——按规则跳过推进")
        if author and not ad and not any(keys_match(key, k) for k in pool):
            if scan_limit:
                folded = scan_post(win, author, head, _closer, key, unit)
                session.append(key)
                scanned_count += 1
                if folded:
                    print(f"SCAN STOP: 折叠长帖已全读（巡逻 {scanned_count} 帖）"
                          f"——这就是 A1 测试对象")
                    return
                if scanned_count >= scan_limit:
                    print(f"SCAN DONE: 巡逻了 {scanned_count} 帖，没有折叠长帖")
                    return
                # scan_post scrolled internally — no row baseline survives it
                prev_kind, prev_rows, prev_cmd_h = "scan", None, 0.0
                continue
            # fresh post with a real name — process it
            process_post(win)
            return
        if bounded and not author and not ad:
            # DOOR CHECK (user rule 2026-11-09: 认门牌是门口的事，走路的
            # 事只有几何). A no-name aligned region is normal (comment
            # strips, folded threads, my own comment zone) — walk on. But
            # a closing TIME ROW without a name is a real post we failed
            # to read: silently crossing it would violate 每帖必弹卡.
            doorless += 1
            if unit.get("pre_closer") and not no_brake:
                wlog(f"brake: 门口有真帖（时间行 {unit['pre_closer']!r}）"
                     f"但读不出名字 — 停车")
                st["phase"] = "walking"
                save_state(st)
                print("BRAKE: 分割线贴顶、下方是一帖真帖子，但作者名读不出来"
                      f"（看 {OUT}/adv{_attempt}.png）。请人工看一眼微信："
                      "认得出就手动处理，认不出就 reopen 换个落点重试；"
                      "确认要硬闯就 POST_TEST_NOBRAKE=1 续跑")
                sys.exit(2)
            if doorless >= 6 and not no_brake:
                wlog(f"brake: 连续 {doorless} 次贴线对齐都没有门牌 — 停车")
                st["phase"] = "walking"
                save_state(st)
                print("BRAKE: 连续 6 屏贴线对齐都读不到帖子门牌——大概率困在"
                      "评论区/折叠线里。已停车，请人工看一眼微信；确认只是"
                      "长评论区就 POST_TEST_NOBRAKE=1 续跑")
                sys.exit(2)
        else:
            doorless = 0
        # MOTION CHECK — row-shift based (2026-11-09): the previous step's
        # REAL displacement is the median shift of the text rows themselves,
        # so the check holds even when NO divider is on screen (the 17:00
        # flight crossed two posts and landed where a likes-row name got
        # misattributed, all while the divider-based check sat blind).
        if prev_kind == "walk" and prev_rows is not None and prev_cmd_h > 0:
            shift = _row_shift(prev_rows, ocr_rows_cached(f"{OUT}/read.png"))
            if shift is not None:
                if abs(shift - prev_cmd_h) <= 0.15:
                    frozen = 0
                elif prev_cmd_h + 0.15 < shift <= 0.6:
                    # content jumped up-screen: a fold above collapsed or
                    # the region model handed to the next line (WeChat
                    # re-renders folds after a reply, adv0.png). Re-aim.
                    wlog(f"  行位移 +{shift:.2f}h（指令 {prev_cmd_h:.2f}h）"
                         f"——重瞄继续")
                    frozen = 0
                else:
                    over = shift > 0.6
                    frozen += 1
                    if frozen >= 2 and not over and not no_brake:
                        # spin/stall stays a HUMAN brake (different failure:
                        # execution layer spinning in place — reopen doesn't
                        # fix it; and frontier deads self-heal in one step,
                        # so two consecutive stalls is already abnormal)
                        wlog(f"brake: 行位移 {shift:+.2f}h"
                             f"（指令 {prev_cmd_h:.2f}h）——停车")
                        st["phase"] = "walking"
                        save_state(st)
                        print("BRAKE: 滚轮步连续两步没有按指令移动——原地"
                              "空转或执行层失控。已停车，请人工看一眼微信")
                        sys.exit(2)
                    if over and not no_brake:
                        # FLIGHT (send-locked, 14/14 observed within minutes
                        # of a send; the send-resume path defers instead —
                        # reaching this branch means NO recent send, so a
                        # flight here would be new evidence against the
                        # send-lock conclusion): auto reopen + identity
                        # re-anchor, the user-approved 连试 2 次 — the
                        # budget loop actually retries (live 15:24 catch:
                        # v1 tried once and stopped, under-delivering).
                        wlog(f"brake: 行位移 {shift:+.2f}h"
                             f"（指令 {prev_cmd_h:.2f}h）——飞行")
                        st["phase"] = "walking"
                        save_state(st)
                        recovered = False
                        for attempt in range(1, FF_RECOVER_ATTEMPTS + 1):
                            recover_budget = attempt
                            wlog(f"recover: 自动恢复 {attempt}/"
                                 f"{FF_RECOVER_ATTEMPTS} — reopen → 认帖快进"
                                 f"重走")
                            print(f"AUTO-RECOVER: 飞行检出，自动回顶重定位"
                                  f"（{attempt}/{FF_RECOVER_ATTEMPTS}）")
                            outcome, ri = recover_to_anchor(win, st)
                            if outcome == "anchor":
                                recovered = True
                                break
                            wlog(f"recover: 第 {attempt} 次未成"
                                 f"（{outcome}，第 {ri} 步）")
                        if not recovered:
                            wlog("recover: 预算用尽——停车叫人")
                            st["phase"] = "walking"
                            save_state(st)
                            print("BRAKE: 自动恢复没有成功回到锚点——可能穿过"
                                  "了没读的帖子，请人工看一眼微信")
                            sys.exit(2)
                        wlog("recover: 锚点命中——恢复常规行走")
                        print("RECOVERED: 已回到已处理区底部，继续行走")
                        prev_kind, prev_rows, prev_cmd_h = \
                            "start", None, 0.0
                        frozen, seek_h, doorless = 0, 0.0, 0
                        continue
        # advance — PURE GEOMETRY (user model 2026-11-09): only down,
        # never up; wheel only (no PageDown, no focus scaffolding).
        if div_y is not None:
            want = div_y - ALIGN_TOP_OFFSET
            seek_h = 0.0
            if want <= 0:
                # divider already at/above the landing zone — alignment is
                # done; a small flush step pushes it off the top so the
                # next read lands the NEXT line (down-only: never wheel up
                # to 'correct' an overshoot)
                step_h = 0.08
                tag = "↓0.08h 冲刷（分割线已到着陆区，把它顶出屏顶）"
                prev_kind = "flush"
            else:
                step_h = min(want, 0.3)
                tag = (f"↓{step_h:.2f}h 对齐分割线"
                       f"（want {want:.2f}h，封顶 0.3h）")
                prev_kind = "walk"
            if not feed_scroll(win, -step_h * b["height"]):
                wlog("STUCK: 滚不动了（下滚失败）")
                st["phase"] = "walking"
                save_state(st)
                print("STUCK: 滚不动了，请人工看一眼微信")
                sys.exit(2)
            prev_cmd_h = step_h
        else:
            # no divider on screen — small blind steps forward until one
            # appears (replaces PageDown: wheel is cursor/focus-proof and
            # a 0.3h quantum can never jump a line we haven't seen yet)
            seek_h += 0.3
            if seek_h >= 2.4 and not no_brake:
                wlog(f"brake: 盲走累计 {seek_h:.1f}h 没见到任何分割线 — 停车")
                st["phase"] = "walking"
                save_state(st)
                print("BRAKE: 连续盲走两屏多没见到任何分割线——被困在没有"
                      "结构的地方（超长帖/非信息流页面）。已停车，请人工看"
                      "一眼微信")
                sys.exit(2)
            step_h = 0.3
            tag = "↓0.30h 盲步（屏内无分割线，小步翻到出现为止）"
            prev_kind = "seek"
            prev_cmd_h = 0.0
            if not feed_scroll(win, -step_h * b["height"]):
                wlog("STUCK: 滚不动了（下滚失败）")
                st["phase"] = "walking"
                save_state(st)
                print("STUCK: 滚不动了，请人工看一眼微信")
                sys.exit(2)
        prev_rows = ocr_rows_cached(f"{OUT}/read.png")
        wlog(f"  {tag}")
    st["phase"] = "walking"
    save_state(st)
    print("RESULT: 30 次尝试内没有走到新的帖子门牌——请人工看一眼微信")
    sys.exit(2)


def cmd_report():
    st = load_state()
    seen = st.get("seen", [])
    for i, e in enumerate(seen, 1):
        print(f"{i:2d}. [{e['action']:8s}] {e['author'][:14]!r:16s} "
              f"{(e['head'] or '')[:24]!r} {e.get('closer') or ''} "
              f"draft={e.get('draft_id')}")
    print(f"共 {len(seen)} 帖")


def cmd_verify():
    """Re-check the currently open input box against the pending text and
    promote the state to 'boxed' if the evidence is there."""
    st = load_state()
    if st.get("phase") != "read":
        print(f"FATAL: 状态不对 phase={st.get('phase')}")
        sys.exit(1)
    full_capture(f"{OUT}/box3.png")
    ok, seen = paste_verified(f"{OUT}/box3.png", st["text"])
    print(f"RESULT: 粘贴核验 {'OK' if ok else 'FAIL'}")
    if not ok:
        print(f"DEBUG seen tail: …{seen[-120:]}")
        sys.exit(1)
    st["phase"] = "boxed"
    save_state(st)
    print("READY: 文字已在输入框，等待确认发送")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "state"
    if cmd == "read":
        cmd_read(first="--first" in sys.argv)
    elif cmd == "box":
        text = None
        if "--text" in sys.argv:
            text = sys.argv[sys.argv.index("--text") + 1]
        cmd_box(text)
    elif cmd == "verify":
        cmd_verify()
    elif cmd == "fire":
        cmd_fire()
    elif cmd == "report":
        cmd_report()
    elif cmd == "send":
        cmd_send()
    elif cmd == "abort":
        cmd_abort()
    elif cmd == "next":
        limit = 0
        if "--scan" in sys.argv:
            i = sys.argv.index("--scan")
            rest = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
            limit = int(rest) if rest.isdigit() else 12
        cmd_next(scan_limit=limit)
    elif cmd == "view":
        cmd_view()
    elif cmd == "reopen":
        cmd_reopen()
    elif cmd == "refresh":
        cmd_refresh()
    else:
        print(json.dumps(load_state(), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
