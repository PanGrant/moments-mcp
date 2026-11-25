"""Post-schema perception foundation (photo-first model, 2026-11-15).

The feed IS a sequence of posts (user's model). This module is the single
source of truth for everything post-structural:

    POST (block) :=
    ├─ divider_top ──────────────────────────── 上分隔线（post 边界）
    ├─ left column  photo column   ONLY the avatar (photo column = pane
    │                              left edge → text column; body text,
    │                              cards, 9-grid all start further right)
    └─ right column top-down:
         ├─ doorplate     the name row BESIDE the avatar photo
         ├─ body_zone     text / shared link card / images (incl. 9-grid)
         │                / video
         │    ├─ expand_link   the 「全文」 fold link (click target)
         │    └─ time_row      正文结束 marker (NOT the post end — the
         │                     comments zone continues below it; it is
         │                     also where the post's time is read, the
         │                     1-day rule's input)
         └─ comments_zone  optional: likes / comments / reply area, down
                           to divider_bottom
    └─ divider_bottom

Primitives that USED to live in ocr_extract (normalization, time-row
matchers, author predicate, comment/like parsers, chrome filters, the
arrow probe) were moved here verbatim — live-lesson comments included.
ocr_extract is now only the Vision engine + the legacy consumers (old
segmenter/reader) that import these names back; it owns nothing
structural. The import direction is one-way: foundation ← engine.

THE BIG LOOP (user's schema, 2026-11-15): find the post's divider; if
the line is missing, look at the photo column. A post runs from its
divider to the NEXT divider — doorplate, body, time row and comments are
all INSIDE one post; the time row only marks where the body ends. A
doorplate claim is photo-backed: a name row counts as an author only
with a photo beside it in the photo column (schema rule 2; validated on
the 177-capture corpus — every true doorplate has one, every residual
phantom has none). The photo check also covers a MISSED divider: every
avatar found inside a region starts a post there.

Text-claiming machinery retired 2026-11-15 (user verdict: 不必要) — the
first-row state machine, block-end signature rules, measured indent and
the cross-page claim-suppression carry are gone. What 连续图片 (the
sequence of captures) still decides: post boundaries (a region without
a photo is the open post's continuation) and position within a post
(zone logic), via this module's per-capture answers + the walker's
ledger identity matching across landings.

scan_screen(image_path) → {"posts": [...], "viewport": {...},
"page_bottom_closer": {...}|None} or None (pixel analysis unavailable).
POST-CENTRIC semantics: a post may
span several divider regions —

    posts[i] is one of
    - head-cut post   doorplate not on screen (above the viewport) —
                      attribution impossible from this capture,
                      doorplate_visible False
    - complete post   doorplate found (photo-backed, author_source
                      "photo")
    - tail-cut post   continues below the viewport (content_bottom_y is
                      the last VISIBLE row's bottom)

Assembly rules (one vocabulary: region boundaries are pixel events, not
post identities):
    - rows above the first photo of a region belong to the OPEN post
      before it (its tail) — or open a headless fragment when there is
      none;
    - a photo-less region merges into the open post (long comments / long
      body crossing a divider cut); the screen's bottom-edge dividers
      (nothing scrolled in below them yet) produce no post either;
    - empty regions just extend the open post's span.

The upper layers — walk loop, carding, fire — ask ONLY this contract:

    viewport.current_post    the post owning the viewport mid band
    viewport.head_visible    that post's doorplate (author row) is on
                             screen → attribution possible
    viewport.top             which post/zone owns the viewport top
                             ("I am inside someone's tail/body")
    viewport.post_bottom_y   the current post's content bottom — the
                             anchor-scroll target (↓ reveals it)
    viewport.anchors         unique long-text rows, for the overlap
                             self-check between consecutive landings

Movement-model note: ↓ anchors to the NEXT block's bottom (time row →
comment area bottom, staged stations; tall blocks fly). post_bottom_y of
the current post + the posts[] sequence give the walker everything it
needs to predict and verify that — multi-screen anchor jumps land aligned
at a post bottom, which the contract makes checkable per landing.

v2 TODOs (contract shape stays fixed): body media features (9-grid /
video / link-card extents). Comment entry LANDED (2026-11-15): every
time-row post carries post["button_x"]/post["button_y"] (the •• pill's
pixel-measured center; feed_layout.more_button), and the schema keeps
the run's column at schema.button_x (median).
"""
import re
from typing import Callable, Dict, List, Optional, Tuple

from .layout_def import build as _build_layout_def

# ---------------------------------------------------------------------------
# 判据声明化（2026-11-25，ldl 试点第 2 步）：下面经 _H[...] 装订的阈值，
# 源头不再是本文件——是 services/digest/layout_def.build().habits（每条
# 带语料证据出处）。改阈值 → 改图纸（layout_def.py），施工侧导入时自动
# 跟随；图纸本身损坏则在这里响亮拒绝启动。历史教训注释保留原位。
# 其余未声明阈值（AUTHOR_MAX_LEN / REGION_AUTHOR_MIN_CONF / ANCHOR_* 等）
# 仍住施工侧，待扩图纸后收编。
# ---------------------------------------------------------------------------
_LAYOUT_DEF = _build_layout_def()
_LAYOUT_ERRS = _LAYOUT_DEF.validate()
if _LAYOUT_ERRS:
    raise RuntimeError(f"布局图纸校验失败，施工拒绝启动: {_LAYOUT_ERRS}")
_H = _LAYOUT_DEF.habits

# ---------------------------------------------------------------------------
# tuning constants (moved verbatim from ocr_extract 2026-11-15; the comments
# are live-lesson history — recalibrate against real captures)
# ---------------------------------------------------------------------------

TITLE_BAND_Y = 0.08     # rows starting above this = navigation chrome
# Post content column (author/text/time all start here). The upper bound
# deliberately excludes the profile-cover name ("Test Owner" sits at x≈0.61,
# right-aligned over the avatar) — without this, the cover name would be
# taken as the first post's author and the first real post mis-attributed.
CONTENT_X = (_H["content_x_lo"]["value"], _H["content_x_hi"]["value"])
CLUSTER_GAP = _H["cluster_gap"]["value"]   # measure_col_x 最左簇簇距
GLYPH_CONF = 0.50       # short rows below this confidence are icon glyphs
AUTHOR_MIN_CONF = 0.55
AUTHOR_MAX_LEN = 30     # visual characters (long service-account names like
                        # 安徽服务管家0127@平安产险服务管家, 19 chars)
MAX_TIME_LEN = 32       # normalized length guard for time-row patterns
# The likes/comments box is indented relative to post content (live
# 2026-11-06: author row x≈0.31, liker names x≈0.38). Short rows this far
# right in FOOTER state are liker names / wrapped comments, never the next
# post's author.
FOOTER_INDENT_X = 0.36
# Region model: an author row is the region's FIRST qualifying OCR row in
# the author column [AUTHOR_X_MIN, AUTHOR_X_MAX]. Name rows start at the
# fixed content indent (x≈0.306-0.309); the avatar column to the left is
# ignored wholesale (user directive 2026-11-08). Live 2026-11-13: OCR
# left-edge jitter by first glyph puts real names as low as 0.289
# ('AuthorA' — v's left bearing vs 卫's 0.293; content rows share the same
# edge, so the y-zone does the real separation on the bounded path) —
# 0.29 rejected the name by 0.001 and honest-braked at her door.
AUTHOR_X_MIN = 0.285
# Region path's author confidence bar. Vision sometimes returns a FLAT
# 0.50 for a whole screen (feed-top capture 2026-11-06: the author name
# AND all body rows at 0.50) — attribution is guaranteed structurally
# (divider-bounded region + fixed author zone), so the bar only needs to
# sit below that floor. The legacy state machine keeps AUTHOR_MIN_CONF.
REGION_AUTHOR_MIN_CONF = 0.45
# Fixed author zone (user's formal model, 2026-11-08): the row at this
# fixed height below the region's top divider is the author — "前面固定
# 高度是作者". Measured across 51 real divider-bounded screens
# (2026-11-08): author_y - divider_y ∈ [0.049, 0.055]; 0.07 covers the
# band and excludes the first content row. Hard-coded for the current
# machine/resolution — must become configuration (user directive).
AUTHOR_ZONE = _H["author_zone"]["value"]
# Name rows start at x≈0.306-0.309; share-card titles at x≈0.34+ (the
# card's own indent). Without this ceiling the card title row qualifies as
# the author whenever the real name row is missing.
AUTHOR_X_MAX = _H["author_x_max"]["value"]
# Doorplate name band around a photo block (normalized window height).
# Corpus geometry (2026-11-15, 235 keep claims vs the head-cut card-cover
# false claims): a real name row HUGS the block's ink top — offsets span
# −0.004..+0.011 (169/235 at exactly +0.007) — while a clipped cover (its
# top behind the nav bar, name scrolled off) lets the card title/body row
# stand in at up to +0.020. The name is the topmost row hugging the block
# (_doorplate_row); the band below is the flowing-text clearance test.
DOORPLATE_HUG_UP = _H["doorplate_hug_up"]["value"]     # name may start this far above the ink top
DOORPLATE_HUG_DOWN = _H["doorplate_hug_down"]["value"]  # ...or this far below it (keep max +0.0107,
                             # closest impostor +0.0143)
DOORPLATE_BAND_DOWN = _H["doorplate_band_down"]["value"]   # the old band bottom, kept for the next-row
                             # clearance scan below the name
# The post's flowing text (body, or the reshare card title) starts well
# clear of the block top (corpus 2026-11-15: keep min +0.039, bulk +0.050;
# closest impostor +0.032); text pressing on closer than this means the
# rows are flowing BESIDE a clipped cover — no name exists.
DOORPLATE_NEXT_MIN = _H["doorplate_next_min"]["value"]
# The picture beside a name is ALWAYS taller than the name line itself —
# an avatar or reshare cover is a square whose ink stands ≥ 1.38× the
# name row (corpus 2026-11-15, 235 keeps; name lines h ≤ 0.031). A block
# SHORTER than the row beside it is not a picture: the real name row
# scrolled off screen top and the body's first line took its place beside
# the avatar (受邀参加 ratio 0.94, 很多年前 0.59 — killed 2026-11-15;
# threshold 1.15 sits mid-gap to keep-min 1.38 and impostor-max 0.94).
DOORPLATE_PHOTO_MIN_RATIO = _H["doorplate_photo_min_ratio"]["value"]
# Loose attribution floor (unbounded region — no divider above it): the
# feed-top page's post 1 sits below the profile cover, which is never
# shorter than ~0.19 of the window (measured across 174 real captures),
# while a mid-feed screen's unbounded region is the previous post's
# scrolled-off tail whose rows start at the screen top (≤ ~0.15 after the
# nav band). An author-column row above this floor in an unbounded region
# is therefore tail body text, not a name. Residual: a DEEP tail row can
# still pass — the collector only lets index-0 (feed-top) readings open
# fresh items from an unbounded region, so mid-feed phantoms can at worst
# split a continuation, never ingest.
LOOSE_AUTHOR_MIN_Y = 0.18
# Walk-only opt-in (read_top_item(mid_feed=True), 2026-11-09): an unbounded
# region whose closing divider sits DEEP below holds the COMPLETE next post
# — the previous post's tail left the screen together with its own divider,
# so the real header may sit right at the screen top and the feed-top cover
# floor must be dropped. Live case: 作者甲's header at y=0.078 went
# unattributed on such a screen and the suffixed time row
# ('2 hour(s) ago Channels·…') masqueraded as the author. Shallow regions
# (landed-low tail slivers) and divider-less tall posts keep the floor.
MID_FEED_DROP_FLOOR_MIN_Y = 0.28
# Sponsored-badge scan band (user rule: the badge marks a promoted post).
# The badge is meaningful for THIS read only inside the author-row band —
# a badge lower on screen belongs to a LOWER post's header riding in the
# same capture (live 2026-11-09 false skips: 作者乙's poem post eaten by
# 千问 PC's badge at y=0.72; a friend's company check-in by one at 0.95).
SPONSOR_BADGE_MAX_Y = 0.35
# The folded-comments strip ("余下2条〉") sits in the author column
# (x≈0.31, conf≈0.5, 5 chars) and is divider-delimited like a post region —
# but it is UI chrome, never a name. ("Show More", the en-UI strip, is in
# CHROME_TEXTS already.)
_FOOTER_STRIP_RE = re.compile(r"^余下\s*\d+\s*条")

# More-menu arrow probe (自主批阅, 2026-11-08): service/sponsored accounts
# render a dropdown arrow at the far right of the AUTHOR row; personal posts
# leave that zone blank. Measured on the 091925 round (882px captures):
# arrow ink 糖K经理 [0.890..0.909] / 服务管家 [0.890..0.909], TestBuddy's
# "Sponsored v" run [0.799..0.920]; personal names' ink never leaves their
# own OCR bbox (max name bbox right edge ~0.54). Phantom long-text
# "authors" (unbounded tail rows) also only ink INSIDE their bbox — their
# runs start left of bbox_right + GAP (s3/s16/s27 all clean under this
# rule). So: an ink run starting ≥ ARROW_GAP right of the name row's right
# edge AND reaching into the arrow zone = sponsored. Screen-edge artifacts
# (≥0.968) are outside the scan band; the time row's "••" is a different
# y band entirely.
ARROW_SCAN_X = (0.80, 0.955)
ARROW_ZONE_X = 0.86
ARROW_GAP = 0.02
ARROW_INK_MAX = 215   # min channel value: background is 242+, arrow core ~172
# single blank px between anti-aliased glyphs doesn't split a run
ARROW_RUN_JOIN = 0.004

# Fixed UI chrome rows (both UI languages), never post content
CHROME_TEXTS = {"moments", "朋友圈", "show more", "全文", "收起",
                "hide", "隐藏"}
# The folded-post expander link — chrome, but special: its coordinates are
# the click target to reveal the full text ("Show More" en / "全文" zh).
SHOW_MORE_TEXTS = {"show more", "全文"}

# A divider this close to the screen top means the previous capture ended
# exactly at this item's TOP edge (aligned one-item-per-screen reading) —
# the region below it is header-bounded (author in the fixed zone).
# The feed-top page can't produce one: its first divider sits below the
# cover (~0.3+).
TOP_ALIGNED_MAX_Y = 0.18

# ---------------------------------------------------------------------------
# normalization + time-row detection (zh & en UIs)
# ---------------------------------------------------------------------------

_FULLWIDTH = str.maketrans({
    "（": "(", "）": ")", "：": ":", "，": ",", "。": ".", "！": "!", "？": "?",
    "；": ";", "　": " ",
    "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
    "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
})


def normalize_for_match(text: str) -> str:
    """Fullwidth→halfwidth, collapsed whitespace, lowercased — for matching
    only. Stored post text is never normalized this way."""
    return re.sub(r"\s+", " ", (text or "").translate(_FULLWIDTH)).strip().lower()


_TIME_PATTERNS = [
    # zh relative
    r"^刚刚$", r"^\d+秒前$", r"^\d+分钟前$", r"^\d+小时前$", r"^半小时前$",
    r"^\d+天前$", r"^\d+(?:周|个星期)前$", r"^\d+个月前$", r"^\d+年前$",
    # zh absolute — exact or "date HH:MM" only, so post text like
    # "3月15日活动报名开始" is NOT mistaken for a time row
    r"^昨天(?: \d{1,2}:\d{2}(?::\d{2})?)?$",
    r"^前天(?: \d{1,2}:\d{2}(?::\d{2})?)?$",
    r"^星期[一二三四五六日天](?: \d{1,2}:\d{2}(?::\d{2})?)?$",
    r"^周[一二三四五六日天](?: \d{1,2}:\d{2}(?::\d{2})?)?$",
    r"^\d{1,2}月\d{1,2}日(?: \d{1,2}:\d{2}(?::\d{2})?)?$",
    r"^\d+年\d{1,2}月\d{1,2}日(?: \d{1,2}:\d{2}(?::\d{2})?)?$",
    r"^\d{1,2}-\d{1,2}$", r"^\d{1,2}/\d{1,2}$",
    r"^\d{4}-\d{1,2}-\d{1,2}$", r"^\d{4}/\d{1,2}/\d{1,2}$",
    # en relative — "(s)" covers Vision's fullwidth-paren reading of "hour(s)"
    r"^just now$", r"^yesterday(?: at .*)?$",
    r"^last (?:week|month|year)$",
    r"^an? (?:second|minute|hour|day) ago$",
    r"^\d+ (?:second|minute|hour|day|week|month|year)(?:s|\(s\))? ago$",
    # en absolute
    r"^(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2}(?: at .*)?$",
    r"^(?:mon|tues?|wed|thur?s?|fri|sat|sun)[a-z]*(?: at .*)?$",
]

_TIME_RE = [re.compile(p) for p in _TIME_PATTERNS]

# Channel/video posts render the time row with an inline source suffix on
# the same line ("18 hour(s) ago Official Accounts · 解放日报",
# "16 hour(s) ago Channels · DJI大疆行业应用", "15 hour（s） ago 万得股票")
# — live-verified 2026-11-06. Exact matching misses these, so the region
# segmenter also accepts a length-capped PREFIX match — but ONLY for the
# relative-duration forms: WeChat never suffixes an absolute calendar date
# with a source, while post text freely starts with one ("2/3业主同意…"
# live 2026-11-07 truncated a post and faked posted_at 2026-02-03).
_RELATIVE_TIME_PATTERNS = [
    r"^刚刚$", r"^\d+秒前$", r"^\d+分钟前$", r"^\d+小时前$", r"^半小时前$",
    r"^\d+天前$", r"^\d+(?:周|个星期)前$", r"^\d+个月前$", r"^\d+年前$",
    r"^just now$", r"^last (?:week|month|year)$",
    r"^an? (?:second|minute|hour|day) ago$",
    r"^\d+ (?:second|minute|hour|day|week|month|year)(?:s|\(s\))? ago$",
    # absolute day-words WITH a source suffix ARE real time rows:
    # video/Channels shares render "Yesterday Channels· 极目新闻"
    # (live 2026-11-08 — the old "WeChat never suffixes an absolute date"
    # assumption is falsified). The suffix is REQUIRED here: match()
    # anchors only at the start, so an optional group would wave prose
    # ("Yesterday I went…") through. Bare "Yesterday" is covered by the
    # exact list above.
    r"^yesterday(?: channels| official accounts)",
    r"^today(?: channels| official accounts)",
    r"^昨天(?: channels| official accounts)",
    r"^今天(?: channels| official accounts)",
    r"^前天(?: channels| official accounts)",
]
MAX_TIME_PREFIX_LEN = 60
_TIME_PREFIX_RE = [re.compile(p.rstrip("$")) for p in _RELATIVE_TIME_PATTERNS]


def is_time_row(text: str) -> bool:
    """Whether an OCR row is exactly the feed's per-post timestamp."""
    n = normalize_for_match(text)
    if not n or len(n) > MAX_TIME_LEN:
        return False
    return any(rx.match(n) for rx in _TIME_RE)


def is_time_row_prefix(text: str) -> bool:
    """Whether an OCR row STARTS with a timestamp (Channel posts' time row
    + source suffix). Length-capped so prose opening with a time word is
    mostly rejected; in the region segmenter a mid-text false positive is
    harmless anyway — the LAST time-row candidate wins, and the real closer
    sits below it."""
    n = normalize_for_match(text)
    if not n or len(n) > MAX_TIME_PREFIX_LEN:
        return False
    return any(rx.match(n) for rx in _TIME_PREFIX_RE)


# 时间行候选的内容列 x 门宽容度：真时间行与内容列左缘对齐，col_x 取
# 最左簇（永不落在真左缘之左——卡行只会待在右边）。万得卡内嵌戳离
# 真左缘 +0.077，OCR 抖动 ~0.01：取 0.03 = 拒卡戳留 0.047 余量、放
# 真行留 3 倍抖动余量。
TIME_ROW_X_SLACK = _H["time_row_x_slack"]["value"]


def time_row_on_col(row: Dict, col_x: Optional[float]) -> bool:
    """A time-row candidate that ALSO sits on the content column. The
    plain shape match (is_time_row / is_time_row_prefix) is necessary
    but not sufficient: 链接卡内嵌的新闻时间戳（2026-11-16 深夜 万得
    股票快讯卡 '11月16日 15:18' @x=0.337，真左缘 0.26）形状完全合法
    ——is_time_row 本就该认绝对日期（老帖子真时间行长那样）——缺的
    是位置：真时间行永远坐在内容列左缘，卡内戳缩进在卡里。col_x 缺
    （纯图屏）时退回纯形状匹配，诚实不瞎判。门不歪的方向性：col_x 取
    最左文本簇（见 measure_col_x），卡片污染只会待在右边（拖不到真
    左缘之左），所以门对真时间行恒放行、对卡戳按缩进深度拒。"""
    t = row.get("text") or ""
    if not (is_time_row(t) or is_time_row_prefix(t)):
        return False
    if col_x is None:
        return True
    return float(row.get("x", 1.0)) <= col_x + TIME_ROW_X_SLACK


def split_time_row_prefix(text: str) -> Tuple[str, str]:
    """Leading timestamp + remainder of an OCR row, both normalized:
    "18 hour(s) ago Official Accounts · 解放日报" → ("18 hour(s) ago",
    "official accounts · 解放日报"). No leading timestamp → (normalized
    text, ""). Feeds the absolute-time conversion for suffixed rows."""
    n = normalize_for_match(text)
    if n and len(n) <= MAX_TIME_PREFIX_LEN:
        for rx in _TIME_PREFIX_RE:
            m = rx.match(n)
            if m:
                return n[:m.end()].strip(), n[m.end():].strip()
    return n, ""


def parse_time_hours(text: str) -> Optional[float]:
    """A time-row text → the post's age in hours (coarse, Chinese and
    English shapes; 昨天 counts 24, 前天 48, 半小时前 0.5). None when the
    text carries no parseable recency (absolute dates / weekdays). The
    act layer's recency window uses this; the confirmation card
    re-checks before anything is sent."""
    t = (text or "").strip().lower()
    m = re.search(r"(\d+)\s*minute", t)
    if m:
        return float(m.group(1)) / 60.0
    m = re.search(r"(\d+)\s*hour", t)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+)\s*day", t)
    if m:
        return float(m.group(1)) * 24.0
    if "半小时" in t:
        return 0.5
    if "today" in t or "今天" in t:
        return 0.0
    if "yesterday" in t or "昨天" in t:
        return 24.0
    if "前天" in t:
        return 48.0
    return None


# ---------------------------------------------------------------------------
# footer (likes / comments) heuristics
# ---------------------------------------------------------------------------

_COMMENT_RE = re.compile(r"^([^：:]{1,30})[：:]\s*(.+)$")
_LIKE_PATTERNS = [re.compile(p) for p in (
    r"觉得(?:很)?赞", r"赞了", r" liked ", r"likes this",
    # WeChat Mac renders the likes row as "♡ 名字，名字" — bare heart +
    # names, no 赞/liked keyword (live 2026-11-09: '♡ Tyrian Qu' passed
    # every other author gate on the loose path and became the phantom
    # author 'S Tyrion Qu'). '^liked' covers the leading form of the
    # English row the spaced pattern misses.
    r"^[♡❤♥]", r"^liked\b")]


def _comment_match(text: str) -> Optional[Tuple[str, str]]:
    """"名字：内容" / "Name: content" → (author, content); else None."""
    if is_time_row(text):
        return None
    m = _COMMENT_RE.match((text or "").strip())
    if not m:
        return None
    return m.group(1).strip(), m.group(2).strip()


def _is_like_row(norm: str) -> bool:
    return any(rx.search(norm) for rx in _LIKE_PATTERNS)


# ---------------------------------------------------------------------------
# wrap join
# ---------------------------------------------------------------------------

_CJK = re.compile(r"[一-鿿　-〿＀-￯]")


def _join_lines(lines: List[str]) -> str:
    """Wrap-join OCR rows: no space across a CJK boundary, space otherwise."""
    out = ""
    for ln in lines:
        if not out:
            out = ln
        elif _CJK.search(out[-1]) or _CJK.match(ln[0]):
            out += ln
        else:
            out += " " + ln
    return out


join_lines = _join_lines  # public: the collector stitches tall posts with it


# ---------------------------------------------------------------------------
# region primitives (moved verbatim from ocr_extract; public names)
# ---------------------------------------------------------------------------

def filter_region_rows(ordered: List[Dict], top: float, bottom: float,
                       meta: Dict[str, int]) -> List[Dict]:
    """The region's OCR rows, chrome/noise filtered (meta counters updated)."""
    region_rows: List[Dict] = []
    for row in ordered:
        y = float(row.get("y", 0.0))
        if not (top <= y < bottom):
            continue
        text = (row.get("text") or "").strip()
        if not text:
            continue
        norm = normalize_for_match(text)
        x = float(row.get("x", 0.0))
        conf = float(row.get("conf", 1.0))
        if norm in SHOW_MORE_TEXTS:
            region_rows.append(row)  # intercepted below as expand target
            continue
        if not (CONTENT_X[0] <= x <= CONTENT_X[1]) or norm in CHROME_TEXTS:
            meta["noise_rows"] += 1
            continue
        if len(norm) <= 2 and conf < GLYPH_CONF:  # "<", "∞", "••"
            meta["noise_rows"] += 1
            continue
        region_rows.append(row)
    return region_rows


def scan_side(rows: List[Dict]) -> Tuple[List[str], Optional[Dict],
                                         Optional[str], List[Dict]]:
    """One side of a region (top→bottom rows) → (texts, expand, closer,
    comments): content runs until the LAST time-row candidate (card-date
    mid-text safe); rows after it are likes/comments."""
    closer_idx = None
    for i, row in enumerate(rows):
        if is_time_row_prefix(row.get("text") or ""):
            closer_idx = i
    closer = None
    if closer_idx is not None:
        closer = (rows[closer_idx].get("text") or "").strip()
    body_end = closer_idx if closer_idx is not None else len(rows)
    texts: List[str] = []
    expand = None
    for row in rows[:body_end]:
        norm = normalize_for_match(row.get("text") or "")
        if norm in SHOW_MORE_TEXTS:
            expand = {
                "cx": float(row.get("x", 0.0)) + float(row.get("w", 0.12)) / 2,
                "cy": float(row.get("y", 0.0)) + float(row.get("h", 0.015)) / 2,
            }
            continue
        texts.append((row.get("text") or "").strip())
    comments: List[Dict] = []
    if closer_idx is not None:
        for row in rows[closer_idx + 1:]:
            text = (row.get("text") or "").strip()
            norm = normalize_for_match(text)
            if _is_like_row(norm):
                continue
            cm = _comment_match(text)
            if cm:
                comments.append({"author": cm[0], "text": cm[1]})
                continue
            if comments:  # wrapped comment continuation / liker row
                comments[-1]["text"] += " " + text
    return texts, expand, closer, comments


def _median(values: List[float]) -> float:
    s = sorted(values)
    return s[len(s) // 2]


def measure_col_x(rows: List[Dict]) -> Optional[float]:
    """The body text column's left edge, measured from this capture's own
    OCR rows (user directive: nothing about the window may be hardcoded).

    The text column is a physical invariant of the layout: author names,
    body text, location lines and time rows all START at the same x, while
    link cards, the likes/comments box and comment indents all sit RIGHT
    of it. So the content column is the leftmost dense text cluster —
    collected across all three tenant shapes (time rows, author-shaped
    short rows, long body rows) within the CONTENT_X band, then the
    leftmost cluster's median.

    2026-11-16 深夜两连教训（单一信号源都会被链接卡下毒）：
    - 只信时间行：万得股票快讯卡内嵌新闻时间戳 '11月16日 15:18'
      @0.337（真左缘 0.26）是全屏唯一时间行形状候选 → col_x=0.337、
      照片列吞卡、起步屏被判"绝对日期出界"0 按键即停；
    - 只信长行（当晚第一次改）：AuthorA 帧唯一合格长行是卡内文章标题
      @0.382 → col_x=0.382、photo_x1=0.374、照片列吞掉卡缩略图，
      分段全毁。卡帧上作者名（短行）与正文长行常被卡行挤出局，
      没有任何单一形状保证在场。
    两条命换来的不变量：真内容列是任意帧上最靠左的文本列——卡行、
    评论缩进永远在它右边。三形状合流取最左簇，单形状缺席不致命，
    单形状被卡污染时被更左的真列压住。"""
    xs: List[float] = []
    for row in rows:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        x = float(row.get("x", 0.0))
        if not (CONTENT_X[0] <= x <= CONTENT_X[1]):
            continue
        if is_time_row(text) or is_time_row_prefix(text):
            xs.append(x)
            continue
        if _author_gates(row, CONTENT_X):
            xs.append(x)   # author-shaped short rows share the column
            continue
        norm = normalize_for_match(text)
        if (len(norm) >= ANCHOR_MIN_LEN
                and float(row.get("conf", 1.0)) >= ANCHOR_MIN_CONF):
            xs.append(x)
    if not xs:
        return None
    xs = sorted(xs)
    # 簇距 = CLUSTER_GAP（图纸 habits.cluster_gap）：真列与第一缩进列
    # （卡片/评论，实测只偏右 0.02）不得
    # 链式并簇——0.02 的旧簇距正好把万得帧的真列 0.267 和卡缩进
    # 0.286-0.296 并成一簇、中位数 0.288。真列自身抖动 ~0.01，0.012
    # 保簇不散；即便偶发裂簇，最左子簇仍 ≈ 真列。
    clusters: List[List[float]] = [[xs[0]]]
    for v in xs[1:]:
        if v - clusters[-1][-1] <= CLUSTER_GAP:
            clusters[-1].append(v)
        else:
            clusters.append([v])
    return _median(clusters[0])


def _author_gates(row: Dict, band: Tuple[float, float]) -> bool:
    """The per-row author-qualification gates (x band, confidence, length,
    and the pattern exclusions: time rows / footer strip / like rows /
    comment rows — see find_author's docstring for the live evidence
    behind each). No y logic here; the callers own position."""
    text = (row.get("text") or "").strip()
    if not text:
        return False
    x = float(row.get("x", 0.0))
    lo, hi = band
    if not (lo <= x <= hi):
        return False
    if float(row.get("conf", 1.0)) < REGION_AUTHOR_MIN_CONF:
        return False
    if len(text) > AUTHOR_MAX_LEN:
        return False
    if is_time_row(text) or is_time_row_prefix(text):
        return False
    if _FOOTER_STRIP_RE.match(text):
        return False
    if normalize_for_match(text) in SHOW_MORE_TEXTS:
        return False   # the expand link lives in region_rows (expand_y)
    if _is_like_row(normalize_for_match(text)):
        return False
    if _comment_match(text):
        return False
    return True


class FeedSchema:
    """The feed's table skeleton — 用户定义（2026-11-15）：初始化一次，
    之后每次扫描都 follow 这张表（每帖往模板里填）。

    字段三种：
    • 运行几何（init() 从真像素一次量定）：窗格 x 范围、正文列左缘、
      照片列 x 范围（格 A 的定宽）。评论区的几何要有带评论的帖子在屏
      上才量得到 → observe_post 惰性补齐——有些区没有定高，遇到了才量。
    • 每帖槽位在 post dict 上（photo 块几何、门牌、正文 span、时间行、
      评论 span）——scan 每帖填一遍模板。
    • 判据阈值（门牌三关：贴靠/清空/照片身高；2026-11-15 全库 235 真
      门牌量出的群体边界，非窗口几何）——挂在这张表上，遍历只认这一个
      对象。"""
    # samples cap: a walk re-sights posts across screens, the pool only
    # needs enough for a robust median (informational slot, v1)
    _STD_SAMPLES_MAX = 32

    def __init__(self) -> None:
        # 格 A：照片列（定宽格子）
        self.pane_x0: Optional[float] = None
        self.pane_x1: Optional[float] = None
        self.col_x: Optional[float] = None    # 正文列左缘（格 B 左界）
        self.photo_x0: Optional[float] = None
        self.photo_x1: Optional[float] = None
        # 惰性量定（第一次遇到才填）
        self.avatar_offset_x: Optional[float] = None  # 墨迹左缘−窗格左
        self.avatar_std_samples: List[float] = []     # 照片墨迹高样本
        self.name_row_h: Optional[float] = None       # 门牌行高
        self.body_pitch: Optional[float] = None       # 正文行距
        self.time_row_h: Optional[float] = None
        self.comment_indent_x: Optional[float] = None  # 评论区缩进
        self.comment_pitch: Optional[float] = None
        self.time_comments_gap: Optional[float] = None  # 时间行底→评论顶
        self.button_x_samples: List[float] = []   # •• 列 x 样本（定宽列）
        # 判据阈值（门牌三关 + 清空扫描带）
        self.hug_up = DOORPLATE_HUG_UP
        self.hug_down = DOORPLATE_HUG_DOWN
        self.band_down = DOORPLATE_BAND_DOWN
        self.next_min = DOORPLATE_NEXT_MIN
        self.photo_min_ratio = DOORPLATE_PHOTO_MIN_RATIO

    @property
    def avatar_std_h(self) -> Optional[float]:
        """The avatar's standard ink stature (median of samples so far;
        None until a post with a photo was scanned)."""
        if not self.avatar_std_samples:
            return None
        ss = sorted(self.avatar_std_samples)
        return ss[len(ss) // 2]

    @property
    def button_x(self) -> Optional[float]:
        """The run's •• comment-entry column x (median of samples so
        far; None until a post with a time row was scanned)."""
        if not self.button_x_samples:
            return None
        ss = sorted(self.button_x_samples)
        return ss[len(ss) // 2]

    @classmethod
    def init(cls, image_path: str,
             ocr_rows_fn: Optional[Callable] = None) -> "FeedSchema":
        """Measure the skeleton from ONE capture（运行初始化：看第一屏）."""
        if ocr_rows_fn is None:
            from .ocr_extract import _vision_rows
            ocr_rows_fn = _vision_rows
        from . import feed_layout
        s = cls()
        ordered = sorted(ocr_rows_fn(image_path),
                         key=lambda r: (float(r.get("y", 0.0)),
                                        float(r.get("x", 0.0))))
        s.col_x = measure_col_x(ordered)
        pane = feed_layout.measure_pane(image_path) or feed_layout.PANE_X
        s.pane_x0, s.pane_x1 = pane
        s.photo_x0 = pane[0] + feed_layout.AVATAR_COL_LEFT_INSET
        s.photo_x1 = ((s.col_x - 0.008)
                      if s.col_x is not None and s.col_x - 0.008 > s.photo_x0
                      else pane[0] + feed_layout.AVATAR_COL_RIGHT_FALLBACK)
        return s

    def observe_post(self, post: Dict, body_rows: List[Dict],
                     closer_row: Optional[Dict],
                     comment_rows: List[Dict]) -> None:
        """Fill the lazy slots from the first post that exhibits them."""
        photo = post.get("photo")
        if photo:
            if photo.get("ink_x0") is not None \
                    and self.avatar_offset_x is None:
                self.avatar_offset_x = photo["ink_x0"] \
                    - (self.pane_x0 or 0.0)
            if photo.get("y0") is not None \
                    and len(self.avatar_std_samples) < self._STD_SAMPLES_MAX:
                self.avatar_std_samples.append(
                    photo["y1"] - photo["y0"])
        if post.get("author_h") and self.name_row_h is None:
            self.name_row_h = post["author_h"]
        if post.get("button_x") is not None \
                and len(self.button_x_samples) < self._STD_SAMPLES_MAX:
            self.button_x_samples.append(post["button_x"])
        if closer_row is not None and self.time_row_h is None:
            self.time_row_h = float(closer_row.get("h", 0.015))
        if self.body_pitch is None:
            ys = [float(r.get("y", 0.0)) for r in body_rows
                  if normalize_for_match(r.get("text") or "")
                  not in SHOW_MORE_TEXTS]
            if len(ys) >= 2:
                self.body_pitch = _median([b - a for a, b in zip(ys, ys[1:])])
        cm_rows = [r for r in comment_rows
                   if _comment_match((r.get("text") or "").strip())]
        if cm_rows:
            if self.comment_indent_x is None:
                self.comment_indent_x = min(float(r.get("x", 0.0))
                                            for r in cm_rows)
            if self.comment_pitch is None:
                ys = sorted(float(r.get("y", 0.0)) for r in cm_rows)
                if len(ys) >= 2:
                    self.comment_pitch = _median([b - a
                                                  for a, b in zip(ys, ys[1:])])
        if self.time_comments_gap is None \
                and post.get("comments") and post.get("time_row"):
            self.time_comments_gap = (post["comments"]["top_y"]
                                      - post["time_row"]["bottom"])


def _doorplate_row(region_rows: List[Dict], by0: float, by1: float,
                   schema: "FeedSchema") -> Optional[Dict]:
    """The name row beside a photo block (schema: the doorplate is the
    name BESIDE the photo — 照片列有照片处的正文列那行).

    Two corpus-measured rules kill the head-cut impostors (a reshare
    cover clipped by the nav bar, its name row scrolled off, whose band
    would otherwise claim the card title / body row):

    • HUG: the name hugs the block's ink top — the topmost text row
      within [by0 − schema.hug_up, by0 + schema.hug_down] IS the name
      (content-blind); if nothing hugs, no name is read (author None,
      honest head-cut — a lower row is NEVER promoted).
    • CLEARANCE: the next text row after the name (skipping rows inside
      the name's own span — same-line OCR fragments) must start ≥ by0 +
      schema.next_min; flowing text pressing on closer than that is
      flowing beside a clipped cover, not below a name.
    • PHOTO STATURE: the block must stand taller than the name line
      beside it (≥ schema.photo_min_ratio × row height) — avatars and
      covers always do; a block shorter than the "name" is a picture-less
      post whose name scrolled off and whose body line stands in.

    GATES: none on content — 2026-11-23 用户定案（schema 宪法）：名字区
    只认位置。照片列是丈量出的头像专属带（avatar_blocks 的墨迹行），
    正文列顶上贴着块顶的那行就是作者名，不管内容；自信分/长度/格式
    黑名单在这里没有管辖权（普查 27 帧 26 候选：内容门从未拦下过垃
    圾，只误杀过真名 —— AuthorA 两连漏，conf 0.3 < 0.45）。唯一的例外
    是右侧下拉箭头（广告帖 → 跳过），由 scan_screen 在命中之后用像
    素探针另判（sponsored_arrow），不进本函数。唯一的结构要求：那行
    得是文字行（OCR 行都满足）。"""
    chosen = None
    for row in region_rows:            # filter_region_rows keeps order
        y = float(row.get("y", 0.0))
        if y < by0 - schema.hug_up:
            continue
        if y > by0 + schema.hug_down:
            break                      # nothing hugs the block top → no name
        if not (row.get("text") or "").strip():
            continue                   # not a text row — keep looking
        chosen = row
        break
    if chosen is None:
        return None
    cy = float(chosen.get("y", 0.0))
    ch = float(chosen.get("h", 0.015))
    if by1 - by0 < ch * schema.photo_min_ratio:
        return None        # "photo" shorter than the name line → not a photo
    for row in region_rows:            # rows are y-sorted
        y = float(row.get("y", 0.0))
        if y < cy + ch:
            continue                   # same-line OCR fragments
        if y > by1 + schema.band_down:
            break
        if y - by0 < schema.next_min:
            return None                # flowing text beside a clipped cover
        break
    return {"author": (chosen.get("text") or "").strip(),
            "y": cy,
            "h": ch,
            "x2": float(chosen.get("x", 0.0))
                  + float(chosen.get("w", 0.0))}


def find_author(region_rows: List[Dict], region_top: float,
                header_bounded: bool = True,
                loose_floor: float = LOOSE_AUTHOR_MIN_Y) -> Optional[Dict]:
    """The region's author row under the formal model, or None (LEGACY
    path — the photo-first scan_screen no longer calls this; kept for
    the legacy consumers (author_unit → read_top_item) byte-identical).

    A name is a short author-column row that is neither the post's own
    time row nor the folded-comments strip — those share the column and
    length of a name (S1/S2 fixtures: "20 hour（s） ago", "余下2条〉" both
    qualify geometrically; suffixed time rows like
    "2 hour(s) ago Channels·非洲的青山" share it too and are excluded via
    the prefix matcher — the exact matcher alone let one become the
    author, live 2026-11-09). Likes rows ("♡ 名字，名字") and comment rows
    ("名字：内容" / "名字 Reply 名字：") share the column and length too
    and are excluded by pattern — live 2026-11-09: the likes row
    '♡ Tyrian Qu' passed every other gate on the loose path and became
    the phantom author 'S Tyrion Qu', which also hid the real "Yesterday"
    closer into pre_texts. Bounded regions add the fixed zone below the
    top divider; unbounded regions add the loose floor (loose_floor param,
    default LOOSE_AUTHOR_MIN_Y).

    Returns {"author", "y", "h", "x2"} — the geometry the arrow probe
    consumes — or None when no row qualifies."""
    band = (AUTHOR_X_MIN, AUTHOR_X_MAX)
    for row in region_rows:
        if not _author_gates(row, band):
            continue
        y = float(row.get("y", 0.0))
        if header_bounded:
            if y > region_top + AUTHOR_ZONE:
                continue
        else:
            if y < loose_floor:
                continue
        return {"author": (row.get("text") or "").strip(),
                "y": y,
                "h": float(row.get("h", 0.015)),
                "x2": float(row.get("x", 0.0))
                      + float(row.get("w", 0.0))}
    return None


def author_unit(region_rows: List[Dict], region_top: float,
                header_bounded: bool = True,
                loose_floor: float = LOOSE_AUTHOR_MIN_Y) -> Dict:
    """One region parsed under the formal model → unit dict (the LEGACY
    shape consumed by ocr_extract's segmenter/reader; the post-centric
    assembler uses find_author + its own zone assembly instead).

    unit = {author (None = unattributable), texts, expand, closer (None =
    no closing time row on screen), comments, pre_*}: the rows ABOVE the
    author row belong to a different post — on mid-feed screens the
    PREVIOUS post's tail (stitch source), at the feed top cover residue —
    and are kept separate so they can never mix into this region's own
    post.

    `header_bounded=False` (region 0 — no divider above, the region top is
    the screen edge): the post header can sit anywhere below the cover /
    tail rows, so the author is the region's first qualifying author-
    column row AT OR BELOW LOOSE_AUTHOR_MIN_Y (the feed-top cover is never
    shorter than that, while a mid-feed tail's rows start at the screen
    top). No qualifying row at all → the region is a pure continuation /
    footer strip / cover-only screen and ALL rows travel as the post part.
    """
    unit = {"author": None, "texts": [], "expand": None, "closer": None,
            "comments": [],
            "pre_texts": [], "pre_closer": None, "pre_comments": []}
    # author-row geometry (normalized): y band + right edge. Consumed by the
    # more-menu arrow probe (read_top_item) — a service account's dropdown
    # arrow renders ink to the right of the name where personal posts have
    # none.
    unit["author_y"] = None
    unit["author_h"] = None
    unit["author_x2"] = None

    hit = find_author(region_rows, region_top, header_bounded=header_bounded,
                      loose_floor=loose_floor)
    if hit is not None:
        unit["author"] = hit["author"]
        unit["author_y"] = hit["y"]
        unit["author_h"] = hit["h"]
        unit["author_x2"] = hit["x2"]
        pre = [r for r in region_rows if float(r.get("y", 0.0)) < hit["y"]]
        below = [r for r in region_rows if float(r.get("y", 0.0)) > hit["y"]]
    elif header_bounded:
        pre, below = region_rows, []  # header expected but unattributable
    else:
        pre, below = [], region_rows  # continuation / footer region

    (unit["pre_texts"], _, unit["pre_closer"],
     unit["pre_comments"]) = scan_side(pre)
    (unit["texts"], unit["expand"],
     unit["closer"], unit["comments"]) = scan_side(below)
    return unit


def author_arrow_present(image_path: str, author_y: float, author_h: float,
                         author_x2: float) -> bool:
    """Pixel probe for the author row's dropdown arrow (自主批阅 skip rule).

    Scans the author-row y band across ARROW_SCAN_X for ink runs; an ink
    run that starts at least ARROW_GAP right of the name row's own right
    edge and reaches into the arrow zone (ARROW_ZONE_X) means a
    service/sponsored account (TestBuddy / 糖K经理 / 服务管家). False when
    Pillow is unavailable or the image unreadable (probe is best-effort
    enrichment — extraction itself never depends on it)."""
    try:
        from PIL import Image
        img = Image.open(image_path).convert("RGB")
    except Exception:
        return False
    w, h = img.size
    px = img.load()
    y0 = max(0, int((author_y - 0.002) * h))
    y1 = min(h - 1, int((author_y + author_h + 0.004) * h))
    x0 = max(0, int(ARROW_SCAN_X[0] * w))
    x1 = min(w - 1, int(ARROW_SCAN_X[1] * w))
    runs: List[Tuple[float, float]] = []
    run: Optional[List[float]] = None
    for bx in range(x0, x1 + 1):
        inked = any(min(px[bx, by]) < ARROW_INK_MAX
                    for by in range(y0, y1 + 1))
        nx = bx / w
        if inked:
            if run is None:
                run = [nx, nx]
            else:
                run[1] = nx
        elif run is not None and nx - run[1] > ARROW_RUN_JOIN:
            runs.append((run[0], run[1]))
            run = None
    if run is not None:
        runs.append((run[0], run[1]))
    gap_x = author_x2 + ARROW_GAP
    return any(start >= gap_x and end >= ARROW_ZONE_X for start, end in runs)


# ---------------------------------------------------------------------------
# feed assembly: the capture as a SEQUENCE OF POSTS (post-centric)
# ---------------------------------------------------------------------------

VIEW_MID_Y = 0.5        # the band that decides the "current" post
ANCHOR_MIN_LEN = 10     # normalized chars — junk-resistant anchor rows
ANCHOR_MIN_CONF = 0.40  # garbled-but-stable rows still anchor fuzzily


def _row_bottom(row: Dict) -> float:
    return float(row.get("y", 0.0)) + float(row.get("h", 0.015))


def _unique_long_rows(post_rows: List[Tuple[int, List[Dict]]]) -> List[Dict]:
    """Anchors for the overlap self-check: long (>= ANCHOR_MIN_LEN
    normalized chars), non-time, screen-unique rows. OCR garble of news
    cards garbles DIFFERENTLY per capture, so consumers match anchors
    fuzzily between landings — here we only preselect candidates."""
    seen = set()
    out: List[Dict] = []
    for post_idx, rows in post_rows:
        for row in rows:
            text = (row.get("text") or "").strip()
            norm = normalize_for_match(text)
            if len(norm) < ANCHOR_MIN_LEN:
                continue
            if is_time_row(text) or is_time_row_prefix(text):
                continue
            if norm in seen:
                continue
            if float(row.get("conf", 1.0)) < ANCHOR_MIN_CONF:
                continue
            seen.add(norm)
            out.append({"text": text, "norm": norm,
                        "y": float(row.get("y", 0.0)),
                        "post": post_idx})
    return out


def _zone_of(y: float, post: Dict) -> str:
    """Which zone of `post` owns normalized y. "none" = no row zone
    (empty span / pure gap)."""
    if post["author_y"] is not None:
        ay = post["author_y"]
        ah = post["author_h"] or 0.015
        if ay <= y < ay + ah:
            return "author"
        body_top = ay + ah
    else:
        body_top = post["top_y"]
    tr = post["time_row"]
    if tr is not None and tr["y"] <= y < tr["bottom"]:
        return "time_row"
    cm = post["comments"]
    if cm is not None and cm["top_y"] <= y <= cm["bottom_y"]:
        return "comments"
    body = post["body"]
    body_hi = tr["y"] if tr is not None else post["bottom_y"]
    if body["bottom_y"] > body["top_y"] \
            and body["top_y"] <= y < max(body["bottom_y"], body_hi):
        return "body"
    return "none"


def scan_screen(image_path: str,
                schema: Optional["FeedSchema"] = None,
                ocr_rows_fn: Optional[Callable[[str], List[Dict]]] = None,
                layout=None,
                arrow_fn: Optional[Callable[..., bool]] = None,
                photo_fn: Optional[Callable[..., list]] = None
                ) -> Optional[Dict]:
    """One capture → {"posts": [...], "viewport": {...}} (see module
    docstring for the photo-first semantics and assembly rules).

    schema injectable — the run's initialized skeleton (用户原则：每次
    运行初始化一次，各屏 follow 同一张表); standalone callers omit it
    and the scan measures its own from THIS capture. ocr_rows_fn /
    layout / arrow_fn / photo_fn injectable for tests (house style);
    photo_fn(path, x0, x1, y_top, y_bot) → [(y0, y1), ...] stubs the
    photo-column probe. Returns None when feed_layout pixel analysis is
    unavailable.

    The big loop (user's schema): each divider region is scanned for
    photo blocks in the photo column; every block starts a post whose
    doorplate is the name row beside it. A region with no photo is the
    open post's continuation (long comments / body crossing the cut);
    a region with photos and rows above the first photo contributes that
    rows to the open post's tail first. No cross-page carry: whether a
    doorplate exists is decidable from THIS capture alone (photo or no
    photo) — cross-page continuity lives in the walker's ledger."""
    if ocr_rows_fn is None:
        from .ocr_extract import _vision_rows  # engine, one-way dependency
        ocr_rows_fn = _vision_rows
    rows = ocr_rows_fn(image_path)
    if layout is None:
        from . import feed_layout
        layout = feed_layout.analyze(image_path)
    if layout is None:
        return None
    if arrow_fn is None:
        arrow_fn = author_arrow_present
    if photo_fn is None:
        from . import feed_layout
        photo_fn = feed_layout.avatar_blocks
    if schema is None:
        schema = FeedSchema.init(image_path, ocr_rows_fn=ocr_rows_fn)
    from . import feed_layout
    ordered = sorted(rows, key=lambda r: (float(r.get("y", 0.0)),
                                          float(r.get("x", 0.0))))
    # 屏底收束槽（2026-11-16 深夜，用户 schema 哲学）：纯下翻的基本假设
    # ——下翻必动且必向下；翻后屏底必收束于某帖的时间行（左，与 •• 评论
    # 钮同行）或其评论结尾；有评论的帖占两屏（屏底先时间行、后评论结尾），
    # 两屏里屏上最后一个时间行候选都是这一帖的 closer。故每屏有唯一定义
    # 的感知槽：page_bottom_closer = 屏上最后一个时间行候选。直接取自原
    # 始 OCR 行、不经区域归属（无头帖会被区域模型丢弃，槽不受影响）。
    # 感知失败=None（诚实，交由循环重试/安全停，绝不瞎判）。候选须过
    # 内容列 x 门（time_row_on_col）：万得卡内嵌戳也形似时间行（教训
    # 见函数 docstring），不设门则起步第 1 屏会被它判"绝对日期出界"
    # 0 按键即停。
    page_bottom_closer: Optional[Dict] = None
    for r in ordered:
        if time_row_on_col(r, schema.col_x):
            page_bottom_closer = {"text": r.get("text") or "",
                                  "y": float(r.get("y", 0.0))}
    meta: Dict[str, int] = {"dropped_truncated": 0, "noise_rows": 0,
                            "dropped_no_author": 0}

    posts: List[Dict] = []
    open_post: Optional[Dict] = None

    def start_post(idx: int, span_top: float, span_bottom: float,
                   hit: Optional[Dict], rows_: List[Dict],
                   photo: Optional[Tuple[float, float]] = None) -> Dict:
        slot = None
        if photo is not None:
            slot = {"x0": schema.photo_x0, "x1": schema.photo_x1,
                    "y0": photo[0], "y1": photo[1]}
            ink = feed_layout.ink_x_span(image_path, schema.photo_x0,
                                         schema.photo_x1,
                                         photo[0], photo[1])
            slot["ink_x0"], slot["ink_x1"] = ink if ink else (None, None)
        post = {
            "index": len(posts),
            "top_y": span_top, "bottom_y": span_bottom,
            "author": hit["author"] if hit else None,
            "author_y": hit["y"] if hit else None,
            "author_h": hit["h"] if hit else None,
            "author_x2": hit["x2"] if hit else None,
            "author_source": "photo" if hit else None,
            "photo": slot,          # 照片在照片区的起始 y 与宽高（schema 槽）
            "doorplate_visible": hit is not None,
            "sponsored_arrow": False,
            "rows": list(rows_),
            "expand_y": None,   # Show More 行中心 y（全文展开点击目标）
            "expand_x": None,   # Show More 行中心 x（同上）
            "time_row": None,
            "comments": None,
            "body": {"top_y": span_top, "bottom_y": span_top, "texts": []},
            "button_x": None,   # •• 评论入口质心 x（finalize 真像素量定）
            "button_y": None,   # •• 评论入口质心 y（finalize 真像素量定）
            "media": None,      # TODO: 9-grid/video/link-card features (v2)
            "content_bottom_y": None,
        }
        posts.append(post)
        return post

    ax0, ax1 = schema.photo_x0, schema.photo_x1
    for idx, (top, bottom) in enumerate(layout.regions):
        region_rows = filter_region_rows(ordered, top, bottom, meta)
        # the big loop: the divider (region boundary) is the post
        # boundary; the photo column decides where a doorplate sits.
        # The bottom-edge inset keeps the screenshot's rounded-corner
        # pixels from reading as a photo (live lesson 2026-11-15: the
        # 7 photo-less dividers all sit at the screen's bottom edge).
        blocks = photo_fn(image_path, ax0, ax1,
                          top + 0.002, bottom - 0.004)
        if not blocks:
            # no photo → no doorplate → this region is the open post's
            # continuation (or a headless fragment at walk start)
            if region_rows:
                if open_post is None:
                    open_post = start_post(idx, top, bottom, None,
                                           region_rows)
                else:
                    open_post["rows"].extend(region_rows)
                    open_post["bottom_y"] = bottom
            elif open_post is not None:
                open_post["bottom_y"] = bottom
            continue

        # every photo block starts a post; its doorplate is the name row
        # beside the photo (no name read → honest head-cut start)
        seats = []
        for by0, by1 in blocks:
            hit = _doorplate_row(region_rows, by0, by1, schema)
            seats.append((hit["y"] if hit else by0, hit))
        first_y = seats[0][0]
        pre = [r for r in region_rows if float(r.get("y", 0.0)) < first_y]
        if pre:
            if open_post is None:
                # rows above the first doorplate: a headless fragment of
                # an even earlier post — its own post in the sequence
                open_post = start_post(idx, top, first_y, None, pre)
            else:
                # the PREVIOUS post's tail riding in this region
                open_post["rows"].extend(pre)
                open_post["bottom_y"] = first_y
        for k, ((by0, by1), (sy, hit)) in enumerate(zip(blocks, seats)):
            nxt = seats[k + 1][0] if k + 1 < len(seats) else bottom
            if hit is not None:
                below = [r for r in region_rows
                         if sy < float(r.get("y", 0.0)) < nxt]
            else:
                below = [r for r in region_rows
                         if sy <= float(r.get("y", 0.0)) < nxt]
            open_post = start_post(idx, sy, bottom, hit, below,
                                   photo=(by0, by1))

    # ---- finalize zones per post from its accumulated row stream ----
    post_rows_all: List[Tuple[int, List[Dict]]] = []
    for post in posts:
        rows_ = post.pop("rows")
        post_rows_all.append((post["index"], rows_))
        ay, ah = post["author_y"], post["author_h"]
        author_bottom = (ay + (ah or 0.015)) if ay is not None else None

        closer_idx = None
        for i, r in enumerate(rows_):
            if time_row_on_col(r, schema.col_x):
                closer_idx = i
        body_rows = rows_ if closer_idx is None else rows_[:closer_idx]
        closer_row = None if closer_idx is None else rows_[closer_idx]
        comment_rows = [] if closer_idx is None else rows_[closer_idx + 1:]

        body_top = author_bottom if author_bottom is not None else post["top_y"]
        body_bottom = (_row_bottom(body_rows[-1]) if body_rows
                       else (author_bottom or post["top_y"]))
        for r in body_rows:
            if normalize_for_match(r.get("text") or "") in SHOW_MORE_TEXTS:
                post["expand_y"] = (float(r.get("y", 0.0))
                                    + float(r.get("h", 0.015)) / 2)
                post["expand_x"] = (float(r.get("x", 0.0))
                                    + float(r.get("w", 0.12)) / 2)
                break
        post["body"] = {
            "top_y": body_top, "bottom_y": body_bottom,
            "texts": [(r.get("text") or "").strip() for r in body_rows
                      if normalize_for_match(r.get("text") or "")
                      not in SHOW_MORE_TEXTS],
        }
        if closer_row is not None:
            tr_y = float(closer_row.get("y", 0.0))
            tr_x = float(closer_row.get("x", 0.0))
            post["time_row"] = {
                "text": (closer_row.get("text") or "").strip(),
                "x": tr_x,
                "x2": tr_x + float(closer_row.get("w", 0.0)),
                "y": tr_y,
                "bottom": _row_bottom(closer_row),
            }
            # the •• comment entry lives in the fixed band right of the
            # time text, clear of the window chrome (corpus 2026-11-15:
            # 179/179 time rows hit at x≈0.90). NOT from the OCR text
            # edge — Vision sometimes reads the pill's dots as '••'
            # INTO the row box (服务号 rows), pushing the box edge past
            # the dots; the fixed band + rightmost-cluster is immune
            # (suffix ink clusters left of the pill never win).
            bx = by = None
            if schema.pane_x1 is not None:
                btn = feed_layout.more_button(
                    image_path,
                    schema.pane_x1 - feed_layout.MORE_BAND_LEFT,
                    schema.pane_x1 - feed_layout.MORE_CHROME_PAD,
                    tr_y - 0.006, _row_bottom(closer_row) + 0.006)
                if btn is not None:
                    bx, by = btn
            post["button_x"], post["button_y"] = bx, by
        if comment_rows:
            post["comments"] = {
                "top_y": min(float(r.get("y", 0.0)) for r in comment_rows),
                "bottom_y": max(_row_bottom(r) for r in comment_rows),
                "items": _parse_comments(comment_rows),
            }
        if rows_:
            post["content_bottom_y"] = max(_row_bottom(r) for r in rows_)
        elif author_bottom is not None:
            post["content_bottom_y"] = author_bottom
        else:
            post["content_bottom_y"] = post["bottom_y"]
        if post["author"] is not None:
            post["sponsored_arrow"] = bool(
                arrow_fn(image_path, ay, ah, post["author_x2"]))
        # the run's skeleton fills its lazy slots from every new post
        schema.observe_post(post, body_rows, closer_row, comment_rows)

    # ---- viewport answers ----
    current = None
    for p in posts:
        if p["top_y"] <= VIEW_MID_Y < p["bottom_y"]:
            current = p["index"]
            break
    cur = posts[current] if current is not None else None

    top_post, top_zone = None, "none"
    top_row: Optional[Tuple[int, Dict]] = None
    for bidx, rows_ in post_rows_all:
        for r in rows_:
            if top_row is None \
                    or float(r.get("y", 0.0)) < float(top_row[1].get("y", 0.0)):
                top_row = (bidx, r)
    if top_row is not None:
        top_post = top_row[0]
        top_zone = _zone_of(float(top_row[1].get("y", 0.0)),
                            posts[top_row[0]])

    anchors = _unique_long_rows(post_rows_all)
    viewport = {
        "current_post": current,
        "head_visible": bool(cur is not None and cur["doorplate_visible"]),
        "top": {"post": top_post, "zone": top_zone},
        "mid_zone": _zone_of(VIEW_MID_Y, cur) if cur is not None else "none",
        "post_bottom_y": cur["content_bottom_y"] if cur is not None else None,
        "button_x": cur["button_x"] if cur is not None else None,
        "button_y": cur["button_y"] if cur is not None else None,
        "anchors": anchors,
    }
    return {"posts": posts, "viewport": viewport,
            "page_bottom_closer": page_bottom_closer}


def _parse_comments(rows: List[Dict]) -> List[Dict]:
    """Comment-zone rows → items, mirroring scan_side's parsing (likes
    rows skipped; '名字：内容' split; wrapped continuations appended)."""
    items: List[Dict] = []
    for row in rows:
        text = (row.get("text") or "").strip()
        norm = normalize_for_match(text)
        if _is_like_row(norm):
            continue
        cmr = _comment_match(text)
        if cmr:
            items.append({"author": cmr[0], "text": cmr[1],
                          "y": float(row.get("y", 0.0))})
            continue
        if items:
            items[-1]["text"] += " " + text
    return items
