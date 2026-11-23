"""Feed layout analysis: divider-delimited item regions (zero-token).

The formal region model (user's frame, hard implementation 2026-11-08):
a Moments item tiles the feed BETWEEN two full-width separator lines.
Within one region the left column holds ONLY the avatar — and the avatar's
content is irrelevant — while the right column never holds an avatar, so
the avatar column is simply IGNORED: no pixel analysis there, ever. The
right column reads top-to-bottom in a fixed order: author (fixed height
below the region top — configurable per machine/resolution), content
(text / text+images / shared article / video), the time row, then the
expandable comments area. This module only finds the dividers that bound
the regions; everything else is OCR rows.

Detection model (user's window test, 2026-11-08): a small window can
cover a real separator holding ONLY the uniform gray line and blank
space — nothing else.

Measured on real captures (2026-11-06/07/08):
- separator lines: 2px tall (5/5 measured), uniform light gray
  RGB≈(242,242,242), spanning the pane continuously (x 0.136-0.990 of the
  window, no gaps) with blank space on both sides (≥40 rows measured)
- gate = color band + thickness ≥2 + every band row uniform (≥0.9 of pane
  samples within ±6 of the row median: a text row's ink can push the
  MEDIAN into the gray band — live 2026-11-08: 78 white + 41 ink samples,
  median 235, uniformity 0.02 — and an indented likes-box edge leaves the
  pane's left white, uniformity ~0.8) + the 2 rows above and below the
  band content-free
- the first item's upper boundary is simply the screen top (the profile
  cover sits inside its region; per user directive 2026-11-07 the cover is
  never image-analyzed — covers vary per user)
- "余下N条" collapsed-comment strips are divider-delimited regions of their
  own (no author row → skipped by the segmenter)
- photo content can produce 1px colored lines — filtered by requiring gray
  (max-min channel spread ≤ 6) inside a brightness band
- avatar detection: REMOVED 2026-11-08 (the left column was ignored
  wholesale), REINSTATED 2026-11-15 with a different job — the user's
  schema defines the new-post marker as 分隔线 + 照片列有照片, so
  avatar_blocks() answers "is there a photo in the photo column" (a
  doorplate validator, not a segmentation crutch). Validated on the
  177-capture corpus: every divider with content below it has a photo
  (206/206; the only photo-less dividers sit at the screen's bottom edge
  where nothing has scrolled in yet), every true doorplate has a photo
  beside it, and every residual phantom has none.
"""
from typing import List, Optional, Tuple

# Fallback scan band for divider lines, used ONLY when the per-capture
# white-run measurement (measure_pane, user directive 2026-11-09) can't
# produce a sane band — e.g. the feed-top cover screen has no blank rows.
# Values measured on the reference machine 2026-11-06/07/08.
PANE_X = (0.15, 0.96)
# Right margin strip: always blank feed background — the per-row reference.
REF_X = (0.93, 0.98)

# Per-capture calibration: the content pane is the continuous near-white
# run between WeChat's dark sidebar (left) and the window-edge chrome —
# a gray scrollbar-track strip (226,228,229) that ends the white run on
# the right (live 2026-11-09: a right-edge scan without skip-in finds
# run=0 everywhere). Measured fresh on every analyze() call — nothing
# hardcoded survives a window resize or another machine.
WHITE_MIN = 236      # per-channel floor of "feed background" pixels
RUN_MIN_W = 0.30     # a row qualifies when its white run spans this much
SKIP_IN_MAX = 0.06   # window-edge chrome a row may skip before white
PANE_LEFT_MIN = 0.05 # sanity bounds on the measured edges
PANE_LEFT_MAX = 0.45
PANE_RIGHT_MIN = 0.70
PANE_W_MIN = 0.45    # ...and on the pane width
# Insets from the measured white run to the scan band; on the reference
# machine (white run 0.142→0.988) these give ≈ the old (0.15, 0.96).
# Right inset also keeps the scrollbar ghost / •• strip out of the band.
BAND_LEFT_INSET = 0.015
BAND_RIGHT_INSET = 0.035
REF_REL = (-0.03, 0.02)  # REF strip relative to the band's right edge
# Per-channel distance from the row reference beyond which a pixel is
# "content".
DIFF = 40
# Divider uniformity (user's window test, 2026-11-08): every band row must
# have this fraction of pane samples within DIVIDER_UNIFORM_TOL of the row
# median. Real separator rows measure 1.00; a text row whose median
# coincidentally lands in the gray band measures 0.02-0.04; an indented
# likes-box edge ~0.8.
DIVIDER_UNIFORM_TOL = 6
DIVIDER_UNIFORM_MIN = 0.9
# ...and the band must sit in blank space: this many rows above and below
# it carry no content (vs each row's own right-margin reference). Real
# dividers measure ≥40 blank rows on both sides.
DIVIDER_CLEAR_ROWS = 2
DIVIDER_NEIGHBOR_CONTENT = 0.02
_SCAN_STRIDE = 2

# ---------------------------------------------------------------------------
# the photo column (avatar blocks) — schema rule 2: 新帖 = 分隔线 +
# 照片列有照片. The photo column is the strip between the pane's left edge
# and the text column: it holds ONLY avatars (body text, cards, 9-grid
# images and videos all start at the text column, further right; the ☰
# button sits left of the pane).
# ---------------------------------------------------------------------------

AVATAR_COL_LEFT_INSET = 0.004   # from the pane's measured left edge
# Right bound fallback when the text column is unmeasurable this capture:
# pane-left + this fraction. Measured 2026-11-15 (reference machine):
# avatar ink spans pane+0.006..pane+0.075; body text starts ≈ pane+0.12.
AVATAR_COL_RIGHT_FALLBACK = 0.11
# A pixel row counts as photo content at this much non-white (vs WHITE_MIN)
# across the photo column; a run spanning ≥ AVATAR_MIN_ROWS pixels makes a
# block (probe-validated 2026-11-15 on 177 captures — 206/206 dividers).
AVATAR_ROW_FRAC = 0.15
AVATAR_MIN_ROWS = 5
# A block taller than this is NOT an avatar: the profile cover (a photo
# spanning the pane top on feed-top screens) and page-bottom chrome would
# otherwise read as doorplate photos. Real avatars are a FIXED ~39pt render:
# 0.08 of a 548pt window (=0.071) passed, but the same avatar in tonight's
# 464pt title-bar window measures 0.084 and was rejected wholesale —
# doorplates vanished on frames where the avatar's own edge pixels made the
# run reach full height (70-78px spread is the avatar's edge-content
# variance, not size variance). Cap must clear the avatar's normalized
# height at ANY window height with margin; the cover (~0.45) stays excluded.
# 真机铁证 2026-11-16：AuthorA 帖名牌可见却 author=None（78px run > 74.2px cap），
# 整晚"有时回有时不回"的硬币就是这枚（真机帧证据，私有语料）。
AVATAR_MAX_H = 0.20
# One avatar's own white band (a light row inside the image) can split it
# into two runs; runs this close (fraction of height) are one avatar —
# fuse them before capping (2026-11-15: the split lower half's doorplate
# band missed the real name row and claimed the caption line instead).
# Corpus check: all 10 split pairs sit at gaps 0.003–0.004; the closest
# NON-fusable neighbor pair (real avatar + screen-bottom chrome) sits at
# 0.014 — the threshold splits them cleanly.
AVATAR_MERGE_GAP = 0.012


class Layout:
    """Divider-delimited regions of one capture.

    regions tile the screen: [(0.0, d1), (d1, d2), ..., (dn, 1.0)]. Region 0
    has no upper divider — per the user's frame (2026-11-07) everything
    above the FIRST divider is the first item: cover pixels plus post 1.
    Mid-feed screens' region 0 is a scrolled-off tail instead; which of the
    two a region is (and whether it may be attributed) is the segmenter's
    call, driven by the author row.
    """

    def __init__(self, regions: List[Tuple[float, float]]):
        self.regions = regions


def _median(values):
    s = sorted(values)
    return s[len(s) // 2]


def _row_content_fraction(px, py, x0, x1, rx0, rx1) -> float:
    refs = [px[pxx, py] for pxx in range(rx0, rx1, _SCAN_STRIDE)]
    if not refs:
        return 0.0
    rr, gg, bb = (_median([c[i] for c in refs]) for i in range(3))
    nonbg = total = 0
    for pxx in range(x0, x1, _SCAN_STRIDE):
        r, g, b = px[pxx, py]
        total += 1
        if abs(r - rr) > DIFF or abs(g - gg) > DIFF or abs(b - bb) > DIFF:
            nonbg += 1
    return nonbg / total if total else 0.0


def measure_pane(image_path: str) -> Optional[Tuple[float, float]]:
    """Per-capture content-pane measurement (user directive 2026-11-09):
    per sampled row, skip in past the window-edge chrome (≤ SKIP_IN_MAX)
    to the first white pixel, then run leftward while white; a row
    qualifies when the run spans ≥ RUN_MIN_W. Blank rows reach the pane's
    true edges (sidebar cut-off on the left, scrollbar-track strip on the
    right); text rows stop at the last glyph. Pane = low percentile of the
    lefts × median of the rights. Returns (left, right) or None — callers
    fall back to the hardcoded PANE_X."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        img = Image.open(image_path).convert("RGB")
    except Exception:
        return None
    w, h = img.size
    if w < 50 or h < 50:
        return None
    px = img.load()
    min_run = int(RUN_MIN_W * w)
    skip_max = int(SKIP_IN_MAX * w)
    lefts, rights = [], []
    for py in range(2, h - 2, 4):
        x = w - 2
        while x >= 0 and (w - 2 - x) <= skip_max:
            r, g, b = px[x, py]
            if r >= WHITE_MIN and g >= WHITE_MIN and b >= WHITE_MIN:
                break
            x -= 2
        if x < 0 or (w - 2 - x) > skip_max:
            continue
        right = x
        while x >= 0:
            r, g, b = px[x, py]
            if r < WHITE_MIN or g < WHITE_MIN or b < WHITE_MIN:
                break
            x -= 2
        left = x + 2
        if right - left >= min_run:
            lefts.append(left / w)
            rights.append(right / w)
    if len(lefts) < 8:
        return None
    lefts.sort()
    rights.sort()
    left = lefts[max(0, len(lefts) // 10)]
    right = rights[len(rights) // 2]
    if not (PANE_LEFT_MIN <= left <= PANE_LEFT_MAX) \
            or right < PANE_RIGHT_MIN or (right - left) < PANE_W_MIN:
        return None
    return left, right


def avatar_blocks(image_path: str, x0: float, x1: float,
                  y_top: float, y_bot: float) -> List[Tuple[float, float]]:
    """Photo blocks in the photo column [x0, x1] within [y_top, y_bot]
    (normalized coords), top-down. A block = a run of rows (sampled at
    stride _SCAN_STRIDE) whose non-white fraction across the column is
    ≥ AVATAR_ROW_FRAC, with two acceptance rules:
      • runs closer than AVATAR_MERGE_GAP are one split avatar — fused
        before capping;
      • the run must span ≥ AVATAR_MIN_ROWS pixels and ≤ AVATAR_MAX_H
        (the profile cover / page chrome, never an avatar).
    Returns [(y_first, y_last), ...] — empty when Pillow is unavailable
    or the image is unreadable (the scan then finds no doorplates,
    honestly)."""
    try:
        from PIL import Image
        img = Image.open(image_path).convert("RGB")
    except Exception:
        return []
    w, h = img.size
    if w < 50 or h < 50:
        return []
    px = img.load()
    px0, px1 = max(0, int(x0 * w)), min(w - 1, int(x1 * w))
    py0, py1 = max(0, int(y_top * h)), min(h - 1, int(y_bot * h))
    if px1 <= px0 or py1 <= py0:
        return []

    def _row_frac(py: int) -> float:
        n = t = 0
        for pxx in range(px0, px1 + 1, 4):
            r, g, b = px[pxx, py]
            t += 1
            if r < WHITE_MIN or g < WHITE_MIN or b < WHITE_MIN:
                n += 1
        return (n / t) if t else 0.0

    runs: List[List[int]] = []          # [first_py, last_py]
    run: Optional[List[int]] = None
    for py in range(py0, py1 + 1, _SCAN_STRIDE):
        if _row_frac(py) >= AVATAR_ROW_FRAC:
            if run is None:
                run = [py, py]
            else:
                run[1] = py
        elif run is not None:
            runs.append(run)
            run = None
    if run is not None:
        runs.append(run)

    # a real avatar's internal white band splits it — fuse close runs
    merged: List[List[int]] = []
    for r in runs:
        if merged and r[0] - merged[-1][1] <= AVATAR_MERGE_GAP * h:
            merged[-1][1] = r[1]
        else:
            merged.append(r)

    blocks: List[Tuple[float, float]] = []
    for a, b in merged:
        if AVATAR_MIN_ROWS <= b - a <= AVATAR_MAX_H * h:
            blocks.append((a / h, (b + _SCAN_STRIDE) / h))
    return blocks


def ink_x_span(image_path: str, x0: float, x1: float,
               y0: float, y1: float) -> Optional[Tuple[float, float]]:
    """The photo ink's own left/right extent (normalized) inside the box
    — the avatar's footprint within the photo column (schema slot:
    照片本身在照片区的 x 起点/宽度). Fails soft: None when Pillow is
    unavailable or the image is unreadable."""
    try:
        from PIL import Image
        img = Image.open(image_path).convert("RGB")
    except Exception:
        return None
    w, h = img.size
    if w < 50 or h < 50:
        return None
    px = img.load()
    px0, px1 = max(0, int(x0 * w)), min(w - 1, int(x1 * w))
    py0, py1 = max(0, int(y0 * h)), min(h - 1, int(y1 * h))
    if px1 <= px0 or py1 <= py0:
        return None
    lo = hi = None
    for py in range(py0, py1 + 1, _SCAN_STRIDE):
        for pxx in range(px0, px1 + 1, 4):
            r, g, b = px[pxx, py]
            if r < WHITE_MIN or g < WHITE_MIN or b < WHITE_MIN:
                if lo is None:
                    lo = pxx
                hi = pxx
    if lo is None:
        return None
    return (lo / w, (hi + 4) / w)


# ---- the •• comment-entry pill on the time row (2026-11-15 corpus:
# ---- 179 time rows, the pill's dark dots hit 160/160 once the window
# ---- chrome is kept out of the band; x-center dead-stable at 0.90,
# ---- dot samples 26, x-span 0.011–0.027, never OCRs → pixel-only)
MORE_DARK_MAX = 210      # dot ink: min channel < 210 (capsule bg ~235,
                         # feed bg ~250 stay out)
MORE_MIN_SAMPLES = 4     # sampled dark points that make a blob (clean
                         # pill = 26 at stride 2)
MORE_CLUSTER_GAP = 0.012  # x-gap splitting clusters (pill's two dots
                          # fuse; 服务号 time-text suffixes separate)
MORE_BAND_LEFT = 0.15    # search band: [pane_right−0.15, pane_right−
MORE_CHROME_PAD = 0.02   #   −0.02] (corpus 2026-11-15: 179/179; chrome
                         #   lines at the pane edge masquerade as 100+
                         #   sample clusters if the pad shrinks)


def more_button(image_path: str, x_lo: float, x_hi: float,
                y_lo: float, y_hi: float) -> Optional[Tuple[float, float]]:
    """The •• comment-entry pill's center (x, y), normalized, inside the
    band — dark points cluster by x-gap; the RIGHTMOST cluster with
    enough samples wins (long time-row suffixes can share the band but
    never reach the pill; the window chrome must stay outside x_hi —
    corpus: chrome lines masquerade as a 100+-sample cluster at the
    pane edge). Fails soft: None when nothing dark enough stands in the
    band (the glyph never OCRs — pixel-only)."""
    try:
        from PIL import Image
        img = Image.open(image_path).convert("RGB")
    except Exception:
        return None
    w, h = img.size
    if w < 50 or h < 50:
        return None
    px = img.load()
    px0, px1 = max(0, int(x_lo * w)), min(w - 1, int(x_hi * w))
    py0, py1 = max(0, int(y_lo * h)), min(h - 1, int(y_hi * h))
    if px1 <= px0 or py1 <= py0:
        return None
    pts = []
    for py in range(py0, py1 + 1, _SCAN_STRIDE):
        for pxx in range(px0, px1 + 1, _SCAN_STRIDE):
            r, g, b = px[pxx, py]
            if min(r, g, b) < MORE_DARK_MAX:
                pts.append((pxx, py))
    if not pts:
        return None
    pts.sort()
    clusters: List[List[Tuple[int, int]]] = [[pts[0]]]
    for v in pts[1:]:
        if (v[0] - clusters[-1][-1][0]) / w > MORE_CLUSTER_GAP:
            clusters.append([v])
        else:
            clusters[-1].append(v)
    for c in reversed(clusters):
        if len(c) >= MORE_MIN_SAMPLES:
            cx = sum(p[0] for p in c) / len(c) / w
            cy = sum(p[1] for p in c) / len(c) / h
            return (cx, cy)
    return None


def _pane_band(image_path: str) -> Tuple[Tuple[float, float],
                                         Tuple[float, float]]:
    """(scan band, reference strip) for this capture — measured when
    possible, module defaults otherwise."""
    m = measure_pane(image_path)
    if m is None:
        return PANE_X, REF_X
    left, right = m
    band = (left + BAND_LEFT_INSET, right - BAND_RIGHT_INSET)
    ref = (max(0.0, band[1] + REF_REL[0]), min(1.0, band[1] + REF_REL[1]))
    return band, ref


def analyze(image_path: str) -> Optional[Layout]:
    """Detect the divider lines bounding item regions. None when pixel
    analysis is unavailable (no Pillow / unreadable file) — callers fall
    back to OCR-only segmentation."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        img = Image.open(image_path).convert("RGB")
    except Exception:
        return None

    w, h = img.size
    px = img.load()
    (bx0, bx1), (rfx0, rfx1) = _pane_band(image_path)
    x0, x1 = int(bx0 * w), int(bx1 * w)
    rx0, rx1 = int(rfx0 * w), int(rfx1 * w)

    # per-row median color across the pane
    medians = []
    for py in range(h):
        samples = [px[pxx, py] for pxx in range(x0, x1, 4)]
        medians.append(_median(samples) if samples else (255, 255, 255))

    def _row_uniformity(py: int) -> float:
        """Fraction of pane samples within DIVIDER_UNIFORM_TOL of the row's
        median color. The median is a rank, not a color the row has: a text
        row's ink+anti-aliasing can land the median in the gray band while
        the row itself is mostly white + ink (uniformity 0.02); a real
        separator row is solid line color (uniformity 1.00)."""
        m = medians[py]
        near = total = 0
        for pxx in range(x0, x1, 4):
            v = px[pxx, py]
            total += 1
            if all(abs(v[i] - m[i]) <= DIVIDER_UNIFORM_TOL for i in range(3)):
                near += 1
        return near / total if total else 0.0

    dividers = []
    py = 1
    while py < h - 1:
        r, g, b = medians[py]
        spread = max(r, g, b) - min(r, g, b)
        end = py
        while (end + 1 < h and abs(medians[end + 1][0] - r) <= 3
               and abs(medians[end + 1][1] - g) <= 3
               and abs(medians[end + 1][2] - b) <= 3):
            end += 1
        thickness = end - py + 1
        brightness = r + g + b
        # thickness ≥2: real separators are 2px; 1px uniform-gray rows occur
        # inside posts (card edges / anti-aliasing) and would slice an item.
        # Window test (user, 2026-11-08): the band rows must be uniform line
        # color, and the rows just above/below must be blank — a separator
        # is sandwiched in empty space, never touching content.
        if (spread <= 6 and 700 <= brightness <= 748 and 2 <= thickness <= 8
                and py >= DIVIDER_CLEAR_ROWS
                and end + 1 + DIVIDER_CLEAR_ROWS < h):
            uniform = all(_row_uniformity(p) >= DIVIDER_UNIFORM_MIN
                          for p in range(py, end + 1))
            clear = all(
                _row_content_fraction(px, p, x0, x1, rx0, rx1)
                <= DIVIDER_NEIGHBOR_CONTENT
                for p in (list(range(py - DIVIDER_CLEAR_ROWS, py))
                          + list(range(end + 1, end + 1 + DIVIDER_CLEAR_ROWS))))
            if uniform and clear:
                dividers.append(round((py + end) / 2 / h, 4))
        py = end + 1

    regions = []
    edges = [0.0] + dividers + [1.0]
    for i in range(len(edges) - 1):
        top, bottom = edges[i], edges[i + 1]
        if bottom - top < 0.02:  # merging hairline pairs
            continue
        regions.append((top, bottom))
    return Layout(regions)
