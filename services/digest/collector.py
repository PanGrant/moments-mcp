"""Moments collector: WeChat window → screenshots → OCR → RawItems (M1.10).

One-item-per-screen model (user frame, 2026-11-07): each capture reads
exactly ONE item — the region between the screen's top boundary and the
first divider below it.

- Item has a bottom divider → complete reading. The next scroll lands that
  divider ALIGN_TOP_OFFSET into the next screen's top (closed loop: scroll
  a computed pixel delta; the next capture verifies and corrects), so the
  following capture starts at this item's end.
- No bottom divider → the item is taller than the viewport: scroll one
  full viewport per screen and merge, at most MAX_POST_SCREENS screens.
- The feed-top page has no top divider (profile cover) — attribution
  there uses the loose floor rule (read_top_item).

The round stops at the last-seen boundary (an already-ingested complete
post — the DB is the position memory), when a capture repeats the previous
one (feed bottom / no-op scroll), or at max_screens.

Extraction defaults to the local, zero-token OCR path (ocr_extract — Apple
Vision; deterministic text keeps dedupe hashes stable). The VLM path
(vision_extract.extract_moments_items) remains importable as a fallback
for OCR-undecipherable screens.

v1 is semi-automatic (triggered by the Digest page's 立即采集 button or a
direct call); hourly scheduling arrives in M2. Only reads our own screen —
no WeChat protocol access, no injection.
"""
import os
import re
from datetime import datetime
from typing import Callable, Dict, List, Optional

from . import db
from .config import project_root
from .ocr_extract import join_lines, normalize_for_match, read_top_item
from .pipeline import compute_source_msg_id, to_raw_item
from .screen_capture import (
    WindowInfo,
    capture_window,
    find_window,
    screen_is_locked,
)
from .scroller import accessibility_trusted, make_expand_fn, make_pixel_scroll_fn
from .time_hints import approx_posted_at
from .vision_extract import extract_moments_items  # noqa: F401 (VLM fallback)

DEFAULT_CAPTURE_DIR = os.path.join(project_root(), "data", "captures")
DEFAULT_MAX_SCREENS = 5
# An item taller than the viewport is read across screens: scroll until its
# closing time row appears, at most this many screens per item (user rule,
# 2026-11-06: "读不到分隔符就继续读下一屏，上限三屏强制结束").
MAX_POST_SCREENS = 3
# Aligned scrolling (user model, 2026-11-07): after reading an item, the
# next scroll lands its bottom divider this far into the next screen's top
# ("滚动到下分隔线顶部几个 pixel"). The list area starts just below the
# fixed nav bar, so the divider can never sit higher than ~0.08.
ALIGN_TOP_OFFSET = 0.09
# No bottom divider (tall item) → scroll this fraction of the viewport per
# screen ("翻动整屏").
TALL_SCROLL_RATIO = 1.0


class MomentsWindowNotFoundError(RuntimeError):
    """No WeChat window on screen — the round is skipped and the UI shows
    a status hint (DIGEST_DESIGN.md §3.3)."""


class ScreenLockedError(RuntimeError):
    """The Mac's display is locked/shielded — window capture cannot work.
    Hourly rounds (M2) hit this routinely; skip quietly and retry later."""


def find_moments_window() -> Optional[WindowInfo]:
    """Locate the Moments surface. Recent WeChat builds embed the feed in the
    main window (owner 'WeChat', title 'Weixin'); older builds open a separate
    朋友圈 window — prefer that when present, else the largest WeChat window."""
    return find_window("WeChat", "朋友圈") or find_window("WeChat")


_EXPAND_MATCH_CHARS = 12


def _key_close(a: str, b: str) -> bool:
    """Bounded edit distance ≤ 2 for variant keys of the same author.
    Re-reads of the same post jitter by 1-2 chars at the text head
    ('添立冬至…' vs '立冬至…' — live 2026-11-07 服务管家), which prefix
    matching cannot absorb because neither reading is a prefix of the
    other. Keys are the [:30] normalized cuts, so this is tiny."""
    if abs(len(a) - len(b)) > 2:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1,
                         prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1] <= 2


def _common_prefix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _merge_expanded(pre_items: List[Dict], post_items: List[Dict]) -> List[Dict]:
    """Merge pre- and post-expansion extractions of the same screen.

    Expansion is a reflow gamble: a folded post gains its full text, but the
    growth can push it (or neighbours) past the fold, where a post without a
    closing time row is dropped. So the expanded extraction never *replaces*
    the original — for each pre-expansion item, take the expanded full-text
    version when one matches (same leading text), else keep the original
    elided-but-valid version. Live-verified failure this guards against
    (2026-11-06): expanding a long essay dropped BOTH visible posts.
    """
    remaining = list(post_items)
    out: List[Dict] = []
    for pre in pre_items:
        prefix = normalize_for_match(pre.get("text") or "")[:_EXPAND_MATCH_CHARS]
        hit = None
        for cand in remaining:
            if prefix and normalize_for_match(cand.get("text") or "").startswith(prefix):
                hit = cand
                break
        if hit is not None:
            remaining.remove(hit)
            out.append(hit)
        else:
            out.append(pre)
    out.extend(remaining)  # posts newly pushed fully into view by the reflow
    return out


def _unit_item(unit: Dict) -> Dict:
    """read_top_item's unit → the item dict the collector ingests.
    `_expand` (internal, stripped before convert) carries the normalized
    click target for expanding folded text."""
    return {
        "author": unit.get("author") or "",
        "text": join_lines(unit.get("texts") or []),
        "images_desc": "",
        "comments": unit.get("comments") or [],
        "time_hint": unit.get("closer") or "",
        "sponsored": 1 if unit.get("author_arrow") else 0,
        "_expand": unit.get("expand"),
    }


def collect_moments_once(out_dir: Optional[str] = None,
                         read_item_fn: Optional[Callable[[str], Dict]] = None,
                         scroll_px_fn: Optional[Callable[[float], None]] = None,
                         expand_fn: Optional[Callable[[Dict], None]] = None,
                         max_screens: int = DEFAULT_MAX_SCREENS,
                         conn=None) -> Dict:
    """One collection round under the one-item-per-screen model.

    Returns {window, screens, image_path, raw_items, warnings}. Raises
    MomentsWindowNotFoundError when no WeChat window is visible. With a
    conn, the round stops at the last-seen boundary (an already-ingested
    complete item). Folded items (Show More/全文) are expanded via a
    synthetic click and the region re-read before ingestion.

    read_item_fn (tests) returns {unit, divider_y}; default read_top_item.
    scroll_px_fn takes a pixel delta (negative = read further down).
    """
    if read_item_fn is None:
        read_item_fn = read_top_item

    if screen_is_locked():
        raise ScreenLockedError("屏幕已锁定/休眠，无法截屏，本轮采集跳过")
    win = find_moments_window()
    if win is None:
        raise MomentsWindowNotFoundError(
            "微信窗口不在屏幕上，无法采集朋友圈（请打开微信并停留在朋友圈页面）")
    height = float(win.bounds["height"])

    warnings: List[str] = []
    if scroll_px_fn is None and max_screens > 1:
        if accessibility_trusted():
            scroll_px_fn = make_pixel_scroll_fn(win.bounds)
            if expand_fn is None:
                expand_fn = make_expand_fn(win.bounds)
        else:
            warnings.append(
                "辅助功能权限未授予，无法自动滚动，本次仅采集当前可见一屏"
                "（系统设置 → 隐私与安全性 → 辅助功能）")
            max_screens = 1

    out_dir = out_dir or DEFAULT_CAPTURE_DIR
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    raw_items: List[Dict] = []
    seen_keys = set()
    # One item can still be read twice with OCR jitter (an expansion reflow,
    # an alignment retry): collapse variants by (author, normalized text
    # prefix) keeping the most complete reading (live 2026-11-07: 李磊 ×2).
    seen_prefixes: Dict[tuple, int] = {}
    screens: List[Dict] = []
    prev_fp = None
    open_item: Optional[Dict] = None  # tall item still being read across screens
    item_screens = 0

    def convert_item(d: Dict) -> Dict:
        it = to_raw_item(d)
        it["source_msg_id"] = compute_source_msg_id(it["author"], it["text"])
        it["posted_at_approx"] = approx_posted_at(it.get("posted_at", "")) or ""
        return it

    def ingest_item(it: Dict) -> bool:
        # dedupe for an ALREADY-CONVERTED item — double-converting here
        # would drop the time row (to_raw_item renames time_hint →
        # posted_at) and every collapse comparison would misread the
        # variant as unclosed
        key = (it["source"], it["source_msg_id"])
        if key in seen_keys:
            return False
        norm = normalize_for_match(it.get("text") or "")
        # authors OCR with punctuation jitter ('…服务管家〈' — a merged
        # collapse-arrow); strip everything non-word on the key side only
        author_key = re.sub(r"[^0-9a-z一-鿿]+", "",
                            normalize_for_match(it.get("author") or ""))
        pkey = (author_key, re.sub(r"\s+", "", norm)[:30])
        # Variant match, same author key, three tiers:
        #   1. bidirectional prefix — one reading can hold the FULL text
        #      while another holds only a shorter head. The 2-char floor
        #      lets very short posts ('贴冬膘～') collapse too; short-prefix
        #      false positives cost only a longer-text merge.
        #   2. edit distance ≤ 2 — re-reads jitter 1-2 chars at the head
        #      ('添立冬至…' vs '立冬至…'), absorbing neither prefix
        #      direction.
        #   3. shared 12+ char head — a glued/partial reading diverges from
        #      the clean reading's tail; only reachable for suspect-marked
        #      variants (none in the aligned model today, kept for the
        #      expansion merge path). The 12-char floor keeps distinct
        #      posts apart.
        hit = None
        for k in seen_prefixes:
            a, b = k[1], pkey[1]
            if k[0] != pkey[0]:
                continue
            if (not a or not b
                    or (min(len(a), len(b)) >= 2
                        and (a.startswith(b) or b.startswith(a)))):
                hit = k
                break
            if _key_close(a, b) and min(len(a), len(b)) >= 12:
                hit = k
                break
            if (min(len(a), len(b)) >= 12
                    and _common_prefix_len(a, b) >= 12):
                old_it = raw_items[seen_prefixes[k]]
                if old_it.get("_stitch_suspect") or it.get("_stitch_suspect"):
                    hit = k
                    break
        if hit is not None:
            # merge complementary readings of the same item: the longer
            # non-suspect text wins, and the time row from any closed
            # reading supplies posted_at (the expanded reflow often pushes
            # the closer off-screen)
            idx = seen_prefixes[hit]
            old = raw_items[idx]
            variants = [old, it]
            pool = [v for v in variants if not v.get("_stitch_suspect")] \
                or variants
            merged = dict(max(pool,
                              key=lambda v: len(v.get("text") or "")))
            for v in pool:  # first closed reading supplies the time
                if v.get("posted_at"):
                    if not merged.get("posted_at"):
                        merged["posted_at"] = v["posted_at"]
                        merged["posted_at_approx"] = v["posted_at_approx"]
                    break
            merged.pop("_stitch_suspect", None)
            merged["source_msg_id"] = compute_source_msg_id(
                merged["author"], merged["text"])
            seen_keys.discard((old["source"], old["source_msg_id"]))
            raw_items[idx] = merged
            seen_keys.add((merged["source"], merged["source_msg_id"]))
            return False
        seen_keys.add(key)
        seen_prefixes[pkey] = len(raw_items)
        raw_items.append(it)
        return True

    def ingest(d: Dict) -> bool:
        return ingest_item(convert_item(d))

    def finish_open_item(closer: str = "", comments=None) -> None:
        """Seal the tall item being read across screens — with its closing
        time row when found, or force-finished at the screen cap / when a
        divider proves the item ended without a readable closer. A closer
        seen on the pre-expansion (folded) reading is kept as fallback: it
        is the same post's real time row."""
        nonlocal open_item
        ingest({
            "author": open_item["author"],
            "text": "\n".join(p for p in open_item["parts"] if p),
            "images_desc": "",
            "comments": comments or [],
            "time_hint": closer or open_item.get("stale_closer") or "",
            "sponsored": open_item.get("sponsored", 0),
        })
        open_item = None

    def record(index: int, path: str, items: int = 0, known: int = 0,
               expanded: int = 0) -> None:
        screens.append({"index": index, "image_path": path,
                        "items": items, "known": known, "expanded": expanded})

    def advance(delta: float) -> None:
        """Scroll toward the next capture position — skipped on the final
        screen of the round (nothing follows to read)."""
        if scroll_px_fn is not None and index < max_screens - 1:
            scroll_px_fn(delta)

    for index in range(max_screens):
        image_path = os.path.join(out_dir, f"moments_{ts}_s{index}.png")
        try:
            capture_window(win.window_id, image_path)
        except RuntimeError as e:
            # Screen can lock itself between the pre-check and mid-round —
            # Window Server shields the display and capture fails.
            if "could not create image" in str(e):
                raise ScreenLockedError(
                    "屏幕中途进入锁定/休眠保护状态，无法截屏，本轮采集跳过") from e
            raise

        reading = read_item_fn(image_path)
        unit = reading["unit"]
        div_y = reading["divider_y"]
        # "bounded" = the item's top boundary is a real divider (aligned
        # page). On an UNbounded mid-round screen (misaligned retry, overshoot,
        # undividered card screen) an author-looking row may be a deep tail
        # row, so such a reading may only CONTINUE an open item — it can
        # never open a fresh one (that right belongs to the feed-top page,
        # which is always round index 0).
        bounded = reading.get("bounded", True)

        # Scroll-effect check FIRST: a capture repeating the previous one's
        # whole reading means the scroll did nothing (feed bottom / blocked)
        # — repeated rows are not a tall item's continuation.
        fp = (unit.get("author"), tuple(unit.get("texts") or []),
              unit.get("closer"))
        if prev_fp is not None and fp == prev_fp:
            open_item = None
            record(index, image_path)
            break
        prev_fp = fp

        # --- continue a tall item opened on a previous screen ---
        if open_item is not None:
            if item_screens >= MAX_POST_SCREENS:
                # three-screen cap (user rule, 2026-11-06): this capture
                # would be the item's (MAX+1)-th screen — force-finish
                # WITHOUT consuming its rows, then read this screen fresh
                finish_open_item()
            else:
                foreign = (bounded
                           and bool(unit.get("author"))
                           and unit["author"] != open_item["author"]
                           and not any(normalize_for_match(t) in open_item["seen"]
                                       for t in unit.get("texts") or []))
                if foreign:
                    # The scroll overshot the tall item's end: this screen
                    # already shows the next item's own header (only a bounded
                    # screen can prove that — an unbounded one holds nothing
                    # but this item's tail). Drop the partial (nothing was
                    # ingested under this author, so the next round reads the
                    # post whole) and read this screen fresh.
                    open_item = None
                else:
                    # On an unbounded screen the loose rule may park a deep
                    # tail row as a phantom "author" and split the region —
                    # the rows above it, the row itself, and the rows below
                    # are all this item's: merge everything in y-order.
                    pool = (list(unit.get("pre_texts") or [])
                            + ([unit["author"]]
                               if not bounded and unit.get("author") else [])
                            + list(unit.get("texts") or []))
                    cont = [t for t in pool
                            if normalize_for_match(t) not in open_item["seen"]]
                    if cont:
                        open_item["parts"].append(join_lines(cont))
                        open_item["seen"].update(normalize_for_match(t)
                                                 for t in cont)
                    closer = unit.get("closer") or unit.get("pre_closer")
                    if closer:
                        finish_open_item(closer,
                                         ((unit.get("comments") or [])
                                          + (unit.get("pre_comments") or [])))
                    elif div_y is not None:
                        # a divider ends the item: structurally finished but
                        # its time row was unreadable — close rather than leak
                        finish_open_item()
                    else:
                        item_screens += 1
                        advance(-height * TALL_SCROLL_RATIO)
                        record(index, image_path)
                        continue
                    # finished: this screen's rows belonged to the sealed item
                    # — advance below, never re-read them as a fresh item
                    record(index, image_path, items=1)
                    if div_y is not None:
                        advance(-(div_y - ALIGN_TOP_OFFSET) * height)
                    else:
                        advance(-height * TALL_SCROLL_RATIO)
                    continue

        # --- fresh item from the top region ---
        # An unbounded mid-round reading may only continue an open item
        # (see `bounded`) — its author-looking rows are unverifiable tail
        # rows, so they fall through to the re-align path below.
        if unit.get("author") and (index == 0 or bounded):
            item = _unit_item(unit)
            expanded = 0
            expanded_seen = None  # rows of the adopted expanded reading
            stale_closer = ""     # folded reading's real time row (fallback)
            if unit.get("expand") and expand_fn is not None:
                # folded item: click Show More, re-read the same region and
                # merge — the reflow can push the closer off-screen
                expand_fn(unit["expand"])
                x_path = os.path.join(out_dir, f"moments_{ts}_s{index}x.png")
                capture_window(win.window_id, x_path)
                r2 = read_item_fn(x_path)
                u2 = r2["unit"]
                expanded = 1
                if u2.get("author"):
                    merged = _merge_expanded([item], [_unit_item(u2)])[0]
                    if r2["divider_y"] is not None:
                        # bounded after expansion: the whole item is on
                        # screen, only its time row went unreadable — the
                        # folded reading's closer is a valid fallback
                        if not merged.get("time_hint") and item["time_hint"]:
                            merged = item
                        div_y = r2["divider_y"]
                    else:
                        # unbounded after expansion: the item now runs past
                        # the screen bottom. Adopt the expanded text and go
                        # through the tall-item path — the folded closer is
                        # NOT authoritative here (its divider belonged to
                        # the folded layout), so it only survives as a
                        # force-finish fallback. (Live 2026-11-07: the old
                        # guard swapped Lianchang's ~400-char essay body for
                        # the 98-char folded head, and the stale divider
                        # position then wasted two screens mid-essay.)
                        stale_closer = item["time_hint"]
                        merged["time_hint"] = ""
                        div_y = None
                        if merged is not item:
                            expanded_seen = {normalize_for_match(t)
                                             for t in u2.get("texts") or []}
                    item = merged

            if item.get("time_hint"):
                it = convert_item({k: v for k, v in item.items()
                                   if k != "_expand"})
                known_hit = 1 if (conn is not None and db.is_known_raw(
                    conn, it["source"], it["source_msg_id"])) else 0
                ingest_item(it)
                record(index, image_path, items=1, known=known_hit,
                       expanded=expanded)
                if known_hit:
                    break  # last-seen boundary — stop without scrolling on
            elif div_y is None:
                # taller than the viewport: open the cross-screen read
                open_item = {
                    "author": item["author"],
                    "sponsored": item.get("sponsored", 0),
                    "parts": [item["text"]],
                    "seen": (expanded_seen
                             if expanded_seen is not None
                             else {normalize_for_match(t)
                                   for t in unit.get("texts") or []}),
                    "screens": 1,
                    "stale_closer": stale_closer,
                }
                item_screens = 1
                advance(-height * TALL_SCROLL_RATIO)
                record(index, image_path, expanded=expanded)
                continue
            else:
                # divider-bounded but its time row was unreadable — keep the
                # text rather than dropping a fully-visible item
                it = convert_item({k: v for k, v in item.items()
                                   if k != "_expand"})
                ingest_item(it)
                record(index, image_path, items=1, expanded=expanded)
        else:
            # nothing to ingest here: unattributable screen (misaligned
            # retry, undetectable header) or an unbounded mid-round reading
            # (tail rows, phantom "author") — just re-align below
            record(index, image_path)

        # --- advance: land this item's bottom divider at the top of the
        # next screen (the next capture verifies and corrects), or a full
        # viewport when no divider is visible ---
        if div_y is not None:
            advance(-(div_y - ALIGN_TOP_OFFSET) * height)
        else:
            advance(-height * TALL_SCROLL_RATIO)

    if open_item is not None:
        # round budget exhausted mid-item: keep the text gathered so far
        # (its header is gone next round — this is the only chance)
        finish_open_item()

    return {
        "window": win,
        "screens": screens,
        "image_path": screens[0]["image_path"] if screens else "",
        "raw_items": raw_items,
        "warnings": warnings,
    }


def run_moments_collection(conn, out_dir: Optional[str] = None,
                           summarize_fn: Optional[Callable[[Dict, List[Dict]], Dict]] = None,
                           max_screens: int = DEFAULT_MAX_SCREENS) -> Dict:
    """Full round: one-item-per-screen capture + extract + dedupe +
    summarize + store. Returns the collect info plus pipeline stats."""
    from .pipeline import run_pipeline

    info = collect_moments_once(out_dir=out_dir, max_screens=max_screens,
                                conn=conn)
    info["stats"] = run_pipeline(conn, info["raw_items"], summarize_fn=summarize_fn)
    return info
