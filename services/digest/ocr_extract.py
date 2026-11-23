"""Vision OCR engine + legacy consumers (post-centric refactor
2026-11-15).

ALL post-structure primitives — normalization, time-row matchers, author
predicate, comment/like parsers, chrome filters, the sponsored-arrow
probe — moved verbatim into `post_schema` (the perception foundation:
feed = sequence of posts). This module is now:

1. the ENGINE: `_vision_rows` (Apple Vision zh-Hans + en-US OCR → rows
   with confidence + normalized geometry), and
2. the LEGACY consumers the collector still calls (segment_posts state
   machine, segment_by_regions, read_top_item) — they import every
   structural name back from post_schema; the `_underscore` aliases below
   die together with the legacy pipeline (nothing new may import them).

UI-language note: both English and Chinese WeChat UIs are supported, since
this collector may be reused by others. Vision often renders ASCII
parentheses as fullwidth（ ）when the zh-Hans recognizer dominates, so
matching always runs on a fullwidth-normalized copy of the text; the stored
text keeps the original OCR characters (see post_schema.normalize_for_match).
"""
from typing import Callable, Dict, List, Optional, Tuple

from .post_schema import (
    AUTHOR_MAX_LEN,
    AUTHOR_MIN_CONF,
    CHROME_TEXTS,
    CONTENT_X,
    FOOTER_INDENT_X,
    GLYPH_CONF,
    LOOSE_AUTHOR_MIN_Y,
    MID_FEED_DROP_FLOOR_MIN_Y,
    SHOW_MORE_TEXTS,
    SPONSOR_BADGE_MAX_Y,
    TITLE_BAND_Y,
    TOP_ALIGNED_MAX_Y,
    author_arrow_present,
    author_unit as _region_unit,
    filter_region_rows as _filter_region_rows,
    is_time_row,
    is_time_row_prefix,
    join_lines,
    join_lines as _join_lines,
    normalize_for_match,
    split_time_row_prefix,
    _comment_match,
    _is_like_row,
)


def segment_posts(rows: List[Dict],
                  avatar_fn: Optional[Callable[[float], bool]] = None
                  ) -> Tuple[List[Dict], Dict[str, int]]:
    """OCR rows (top→bottom) → closed posts + meta.

    Returns ({author, text, comments, time_hint} list, meta dict). Posts
    without a closing time row (bottom-truncated) are dropped. With
    avatar_fn, author candidates must pass the pixel probe: a screen's top
    can show the TAIL of a post whose header scrolled off (mid-text or
    share-card text), and taking its first row as the author creates
    phantom posts — those headerless units are discarded instead.
    """
    posts: List[Dict] = []
    meta = {"dropped_truncated": 0, "noise_rows": 0, "dropped_no_author": 0}
    cur: Optional[Dict] = None
    state = "SEEK"  # SEEK → POST (author seen) → FOOTER (time row seen)

    def close_current(time_hint=""):
        """Seal the open post (joined text) or count it as truncated."""
        nonlocal cur
        if cur is None:
            return
        if cur.get("orphan"):  # headerless unit — attribution impossible
            meta["dropped_no_author"] += 1
        elif time_hint is None:  # loop end without a closing time row
            meta["dropped_truncated"] += 1
        else:
            post = {
                "author": cur["author"],
                "text": _join_lines(cur["text_rows"]),
                "comments": cur["comments"],
                "time_hint": time_hint,
            }
            if cur.get("expand"):
                post["expand"] = cur["expand"]
            posts.append(post)
        cur = None

    def header_ok(y: float) -> bool:
        return avatar_fn is None or avatar_fn(y)

    for row in sorted(rows, key=lambda r: (r.get("y", 0.0), r.get("x", 0.0))):
        text = (row.get("text") or "").strip()
        if not text:
            continue
        y = float(row.get("y", 0.0))
        x = float(row.get("x", 0.0))
        conf = float(row.get("conf", 1.0))
        norm = normalize_for_match(text)

        if state == "POST" and cur is not None and norm in SHOW_MORE_TEXTS:
            # folded post: remember the link's center (normalized) as the
            # click target for text expansion
            cur["expand"] = {
                "cx": x + float(row.get("w", 0.12)) / 2,
                "cy": y + float(row.get("h", 0.015)) / 2,
            }
            continue
        if y < TITLE_BAND_Y or not (CONTENT_X[0] <= x <= CONTENT_X[1]) \
                or norm in CHROME_TEXTS:
            meta["noise_rows"] += 1
            continue
        if len(norm) <= 2 and conf < GLYPH_CONF:  # "<", "∞", "••"
            meta["noise_rows"] += 1
            continue

        if is_time_row(text):
            close_current(time_hint=text.strip())
            state = "FOOTER"
            continue

        if state == "SEEK":
            if conf >= AUTHOR_MIN_CONF and header_ok(y):
                cur = {"author": text, "text_rows": [], "comments": []}
            else:
                # header scrolled off screen: swallow the unit (mid-text or
                # share-card tail) and discard it at its closing time row —
                # never fabricate an author from card/ad text
                cur = {"author": "", "text_rows": [], "comments": [],
                       "orphan": True}
            state = "POST"
            continue

        if state == "POST":
            if cur.get("orphan") and avatar_fn is not None \
                    and conf >= AUTHOR_MIN_CONF \
                    and len(text) <= AUTHOR_MAX_LEN and header_ok(y):
                # a real header follows the headerless unit (live 2026-11-06:
                # a baseline-merged card label preceded an intact post) —
                # discard the orphan, start the real post here. Requires the
                # pixel probe: without it, high-conf card text could fake a
                # "rescue" (准点抽500元加油礼 did exactly that).
                close_current(time_hint="")
                cur = {"author": text, "text_rows": [], "comments": []}
                continue
            cur["text_rows"].append(text)
            continue

        # FOOTER: likes/comments of the just-closed post, then next author.
        # The screen's top can show footer rows of a post whose body already
        # scrolled off — posts may be empty; those orphan rows are dropped.
        if _is_like_row(norm):
            continue
        cm = _comment_match(text)
        if cm:
            if posts:
                posts[-1]["comments"].append({"author": cm[0], "text": cm[1]})
            continue
        if x >= FOOTER_INDENT_X:
            # inside the indented likes/comments box: liker names or a
            # wrapped comment continuation — never the next author
            if posts and posts[-1]["comments"]:
                posts[-1]["comments"][-1]["text"] += " " + text
            continue
        if conf >= AUTHOR_MIN_CONF and len(text) <= AUTHOR_MAX_LEN \
                and header_ok(y):
            cur = {"author": text, "text_rows": [], "comments": []}
            state = "POST"
            continue
        # unclassifiable unindented footer row: wrapped comment continuation
        if posts and posts[-1]["comments"]:
            posts[-1]["comments"][-1]["text"] += " " + text

    close_current(time_hint=None)
    return posts, meta


# ---------------------------------------------------------------------------
# divider-region segmentation (primary path — feed_layout model)
# ---------------------------------------------------------------------------


def segment_by_regions(rows: List[Dict], layout
                       ) -> Tuple[List[Dict], Dict[str, int], Dict]:
    """OCR rows + feed_layout.Layout → (closed posts, meta, tail).

    The formal model (user's frame, 2026-11-07/08): items tile the screen
    between full-width separator lines; the FIRST item is whatever sits
    above the first divider (its region may include the profile cover —
    never image-analyzed); every later item spans divider→divider and its
    author sits within AUTHOR_ZONE of its top divider. A region without a
    qualifying author row is skipped (footer strip "余下N条" / scrolled-off
    tail / cover-only). Content runs until the time row closes the post
    (rows after it are likes/comments). The LAST time-row candidate in the
    region wins, so a share-card date mid-text ("11月05日 17:01" inside a
    card) can neither close the post early nor hide the article body after
    it. No time row → bottom-truncated → NOT emitted as a post — it
    travels out in `tail` so the collector can finish reading it on the
    next screen (taller-than-viewport posts, capped at MAX_POST_SCREENS).

    tail = {"first": unit, "last": unit} — region 0 (stitch source on the
    next screen; "divider_bounded" says whether a divider ends it on THIS
    screen) and the bottom region (the next post's head). With a single
    region (no divider on screen — e.g. the feed top where post 1 fills
    everything) the lone unit plays both roles, which is how a tall post 1
    opens the stitch.
    """
    posts: List[Dict] = []
    meta = {"dropped_truncated": 0, "noise_rows": 0, "dropped_no_author": 0}
    ordered = sorted(rows, key=lambda r: (r.get("y", 0.0), r.get("x", 0.0)))

    units: List[Dict] = []
    n = len(layout.regions)
    for idx, (top, bottom) in enumerate(layout.regions):
        region_rows = _filter_region_rows(ordered, top, bottom, meta)
        unit = _region_unit(region_rows, top, header_bounded=(idx >= 1))
        units.append(unit)
        if unit["author"] is None:
            # headerless: footer strip / scrolled-off tail / cover-only.
            # A divider-bounded region owes the model a header — but an
            # EMPTY one (divider pair with no OCR rows in it) is a
            # rendering gap, not a dropped post, and is not counted.
            if idx >= 1 and region_rows:
                meta["dropped_no_author"] += 1
            continue
        if unit["closer"] is None:
            meta["dropped_truncated"] += 1
            continue
        post = {
            "author": unit["author"],
            "text": _join_lines(unit["texts"]),
            "comments": unit["comments"],
            "time_hint": unit["closer"],
        }
        if unit["expand"] is not None:
            post["expand"] = unit["expand"]
        posts.append(post)

    # Stitch source = region 0's rows ABOVE its own post: when region 0
    # carries a header those are the previous post's tail (pre_*) — never
    # this region's body (2026-11-07 live lesson: consuming the whole
    # region mixed the NEXT post's text into the stitched post and emitted
    # duplicate variants). Headerless region 0 is one pure continuation.
    first = units[0]
    if first["author"] is not None:
        stitch_src = {"texts": first["pre_texts"],
                      "closer": first["pre_closer"],
                      "comments": first["pre_comments"]}
    else:
        stitch_src = {"texts": first["texts"], "closer": first["closer"],
                      "comments": first["comments"]}
    tail = {
        # "author" lets the collector tell a FOREIGN header in region 0
        # (another post's head — its stitch must drop) from our own
        "first": dict(stitch_src, divider_bounded=(n >= 2),
                      header=first["author"] is not None,
                      author=first["author"]),
        "last": units[-1],
    }
    return posts, meta, tail


def _post_to_item(post: Dict) -> Dict:
    """Segmented post → the same item shape the VLM extractor emits.
    `_expand` (internal, stripped by the collector) carries the normalized
    click target for expanding folded text."""
    item = {
        "author": post.get("author", ""),
        "text": post.get("text", ""),
        "images_desc": "",  # OCR sees no imagery; media described only by VLM path
        "comments": post.get("comments") or [],
        "time_hint": post.get("time_hint", ""),
    }
    if post.get("expand"):
        item["_expand"] = post["expand"]
    return item


# ---------------------------------------------------------------------------
# Vision OCR
# ---------------------------------------------------------------------------

def _vision_rows(image_path: str) -> List[Dict]:
    """Run Apple Vision text recognition; rows normalized to top-left origin."""
    import Vision  # lazy: keeps this module importable without pyobjc-Vision
    from Foundation import NSURL

    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLanguages_(["zh-Hans", "en-US"])
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(False)
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(
        NSURL.fileURLWithPath_(image_path), None)
    ok, err = handler.performRequests_error_([request], None)
    if not ok:
        raise RuntimeError(f"Vision OCR failed: {err}")

    rows: List[Dict] = []
    for obs in request.results() or []:
        candidates = obs.topCandidates_(1)
        if not candidates:
            continue
        top = candidates[0]
        box = obs.boundingBox()  # normalized, origin bottom-left
        rows.append({
            "y": 1.0 - box.origin.y - box.size.height,
            "x": box.origin.x,
            "w": box.size.width,
            "h": box.size.height,
            "conf": float(top.confidence()),
            "text": top.string(),
        })
    return rows


def extract_ocr_screen(image_path: str,
                       ocr_rows_fn: Optional[Callable[[str], List[Dict]]] = None,
                       layout=None) -> Tuple[List[Dict], Optional[Dict]]:
    """Screenshot → (items, tail) for the collector's multi-screen reading.

    items = extract_ocr_items' shape; tail = segment_by_regions' stitch
    payload (None when pixel analysis is unavailable and the legacy state
    machine ran)."""
    rows_fn = ocr_rows_fn or _vision_rows
    rows = rows_fn(image_path)
    if layout is None and ocr_rows_fn is None:
        from . import feed_layout
        layout = feed_layout.analyze(image_path)
    if layout is not None:
        posts, _meta, tail = segment_by_regions(rows, layout)
    else:
        posts, _meta = segment_posts(rows)
        tail = None
    return [_post_to_item(p) for p in posts], tail


def read_top_item(image_path: str,
                  ocr_rows_fn: Optional[Callable[[str], List[Dict]]] = None,
                  layout=None,
                  arrow_fn: Optional[Callable[..., bool]] = None,
                  mid_feed: bool = False) -> Dict:
    """One-item-per-screen reading (user model, 2026-11-07): each capture
    shows exactly one item — bounded above by the screen top (feed-top page)
    or by the top divider (aligned page), and below by the FIRST divider
    past the item. Returns {unit, divider_y, bounded}:

    - unit: _region_unit dict {author (None = unattributable), texts,
      expand, closer (None = no closing time row), comments, pre_*}.
    - divider_y: normalized y of the item's bottom divider (None = the
      item runs past the screen bottom — a tall item to continue on the
      next full-screen scroll).

    Top boundary cases (both required by the model):
    - aligned page: a divider sits at the very top (≤ TOP_ALIGNED_MAX_Y) —
      the item below it is header-bounded: its author sits within
      AUTHOR_ZONE of that divider.
    - feed-top page: no top divider exists; the region starts at the
      screen top with the profile cover. The author (post 1's header) is
      the region's first qualifying author-column row; the cover's own
      rows are filtered as chrome/noise before that search.
    """
    rows_fn = ocr_rows_fn or _vision_rows
    rows = rows_fn(image_path)
    if layout is None:
        from . import feed_layout
        layout = feed_layout.analyze(image_path)
    meta: Dict[str, int] = {"dropped_truncated": 0, "noise_rows": 0,
                            "dropped_no_author": 0}
    ordered = sorted(rows, key=lambda r: (r.get("y", 0.0), r.get("x", 0.0)))
    # Which region holds the item? (user model, 2026-11-07)
    # - aligned page: a divider sits within TOP_ALIGNED_MAX_Y of the top —
    #   that is the PREVIOUS item's bottom divider, i.e. this item's top
    #   boundary; the item is the region BELOW it (regions[1]).
    # - otherwise (feed-top page; or a misaligned retry with the top
    #   divider scrolled off / landed low): the item is region 0, its
    #   header located loosely (first author-column row in the region).
    idx = 0
    if len(layout.regions) >= 2 and layout.regions[1][0] <= TOP_ALIGNED_MAX_Y:
        idx = 1
    top, bottom = layout.regions[idx]
    region_rows = _filter_region_rows(ordered, top, bottom, meta)
    divider_y = (layout.regions[idx + 1][0]
                 if len(layout.regions) > idx + 1 else None)
    loose_floor = LOOSE_AUTHOR_MIN_Y
    if mid_feed and idx == 0 and divider_y is not None \
            and divider_y >= MID_FEED_DROP_FLOOR_MIN_Y:
        # walk-only opt-in: deep closing divider = COMPLETE next post in the
        # region (previous tail left with its own divider, off-screen above)
        # — the real header may sit right at the screen top. See the
        # MID_FEED_DROP_FLOOR_MIN_Y note. Collector default path unchanged.
        loose_floor = 0.0
    unit = _region_unit(region_rows, top, header_bounded=(idx == 1),
                        loose_floor=loose_floor)
    # Dropdown-arrow probe (自主批阅 skip rule): a service/sponsored account
    # shows a more-menu arrow right of its name. Best-effort — extraction
    # never depends on it, and tests inject a stub arrow_fn.
    if arrow_fn is None:
        arrow_fn = author_arrow_present
    unit["author_arrow"] = bool(
        unit.get("author")
        and unit.get("author_y") is not None
        and arrow_fn(image_path, unit["author_y"], unit["author_h"],
                     unit["author_x2"]))
    # Sponsored-badge signature (user rule 2026-11-09: the dropdown badge
    # right of the author row marks a promoted post — the walk skips it, no
    # card, no attribution miss). Scanned at CAPTURE level, NOT region
    # level: ad cards draw borders that the divider detector reads as
    # region boundaries (live 2026-11-09: the MBTI ad's author row landed
    # above a phantom border — unattributable, and the badge row itself got
    # cut out of the chosen region), so the badge must be sought in ALL
    # rows — but only inside SPONSOR_BADGE_MAX_Y, because a badge lower on
    # screen belongs to the NEXT post's header (false-skip evidence in the
    # constant's comment). When a mid-scroll landing parks the ad's author
    # row deeper than the band, this early layer misses; the
    # attribution + author_arrow layer in process_post is the backstop.
    # Walk-only consumer; the collector ignores the extra field.
    unit["sponsored_ad"] = any(
        (lambda n, y: y <= SPONSOR_BADGE_MAX_Y and (
            n.startswith("sponsored") or n in ("广告", "推广")))(
            normalize_for_match(r.get("text") or ""), float(r["y"]))
        for r in ordered)
    # "bounded" = the item's top boundary is a real divider (aligned page).
    # The collector lets only index-0 (feed-top) readings open fresh items
    # from an UNbounded region: there an author-looking row may be a deep
    # tail row, so a mid-round unbounded reading may only continue an open
    # item, never ingest a new one.
    return {"unit": unit, "divider_y": divider_y, "bounded": idx == 1}


def extract_ocr_items(image_path: str,
                      ocr_rows_fn: Optional[Callable[[str], List[Dict]]] = None,
                      layout=None) -> List[Dict]:
    """Screenshot → posts in the same item shape as vision_extract.extract_
    moments_items ({author, text, images_desc, comments, time_hint}), so the
    collector/pipeline need no changes. Raises on OCR failure.

    Primary path: divider-region segmentation (segment_by_regions) on a
    pixel-analyzed feed_layout.Layout. Injectable layout overrides analysis
    (tests). When pixel analysis is unavailable (injected ocr_rows_fn with
    no layout, no Pillow, unreadable file) the legacy OCR-only state machine
    (segment_posts) runs instead."""
    items, _tail = extract_ocr_screen(image_path, ocr_rows_fn=ocr_rows_fn,
                                      layout=layout)
    return items
