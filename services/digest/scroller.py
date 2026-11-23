"""Synthetic scrolling for the Moments collector (DIGEST_DESIGN.md §3.1).

Posts mouse-move + scroll-wheel CGEvents so the feed scrolls under the
cursor. Requires the **Accessibility** permission for the running process;
without it CGEventPost is silently ignored, so callers should check
accessibility_trusted() first and degrade to single-screen capture.

Tuning constants are module-level so a real run can adjust them quickly.
"""
import subprocess
import time

# One scroll step ≈ 0.7 screenful: EVENTS_PER_STEP line-events of
# LINES_PER_EVENT each, then a pause while the list settles. The step MUST
# undershoot the viewport (live 2026-11-07): at ~1.15 screens/post-step, a
# post header that started near the previous screen's bottom fell entirely
# into the skipped band — no screen ever showed its avatar/name. With
# ~0.7 screens every header lands on some screen; the stitcher dedupes
# the overlapping rows.
LINES_PER_EVENT = -5   # negative = wheel down = read further down the feed
EVENTS_PER_STEP = 3
SETTLE_SECONDS = 0.6

# Pixel-unit scrolling (one-item-per-screen model, 2026-11-07): line events
# cannot target a divider position — their pixel yield depends on the
# app's smooth-scroll handling. Pixel events land close to the requested
# delta; the collector's closed loop (scroll → capture → re-measure →
# correct) absorbs the residual.
PIXELS_PER_EVENT = 30
# Gap between the burst's events. Live 2026-11-13: at 0.02s the burst reads
# as a flick (only real fast flicks reach ~50 events/sec), and WeChat's
# WKWebView answers with inertial glide — a 0.3h command flew ≥1.2h twice
# (2026-11-09 17:00 ~2h, 2026-11-13 skipped Viva entirely). 0.08s ≈ a real
# mouse wheel's ratchet cadence: each event is consumed on its own, no
# momentum. Pixel yield is unaffected — only the gesture reading changes.
PIXEL_EVENT_GAP_SECONDS = 0.08


def accessibility_trusted() -> bool:
    """Whether this process may post synthetic input events.

    Tries the AX API first; falls back to an osascript System Events probe
    (querying System Events itself requires the same Accessibility grant,
    so a specific assistive-access error means "not trusted").
    """
    try:
        from ApplicationServices import AXIsProcessTrusted  # type: ignore
        return bool(AXIsProcessTrusted())
    except ImportError:
        pass
    try:
        from Quartz import AXIsProcessTrusted  # type: ignore
        return bool(AXIsProcessTrusted())
    except ImportError:
        pass
    try:
        proc = subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to count processes'],
            capture_output=True, text=True, timeout=10,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def scroll_at(x: float, y: float, lines: int = LINES_PER_EVENT,
              repeats: int = EVENTS_PER_STEP, settle: float = SETTLE_SECONDS) -> None:
    """Move the cursor to (x, y) and post one scroll step."""
    import Quartz

    move = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventMouseMoved, (x, y), Quartz.kCGMouseButtonLeft)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, move)
    time.sleep(0.1)
    for _ in range(repeats):
        ev = Quartz.CGEventCreateScrollWheelEvent(
            None, Quartz.kCGScrollEventUnitLine, 1, lines)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.04)
    time.sleep(settle)  # let the list finish scrolling before the next capture


def make_scroll_fn(bounds: dict):
    """A scroll callable centered in the given window bounds."""
    cx = bounds["x"] + bounds["width"] / 2
    cy = bounds["y"] + bounds["height"] / 2

    def scroll():
        scroll_at(cx, cy)

    return scroll


def scroll_pixels_at(x: float, y: float, pixels: float,
                     settle: float = SETTLE_SECONDS) -> None:
    """Scroll by ≈`pixels` points. Positive = content moves DOWN (view up /
    wheel up); negative = content moves UP (read further down the feed).
    Chunked into small pixel events so a large delta still lands near the
    target."""
    import Quartz

    move = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventMouseMoved, (x, y), Quartz.kCGMouseButtonLeft)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, move)
    time.sleep(0.1)
    amount = -PIXELS_PER_EVENT if pixels < 0 else PIXELS_PER_EVENT
    for _ in range(max(1, int(abs(pixels) // PIXELS_PER_EVENT))):
        ev = Quartz.CGEventCreateScrollWheelEvent(
            None, Quartz.kCGScrollEventUnitPixel, 1, amount)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(PIXEL_EVENT_GAP_SECONDS)
    time.sleep(settle)


def make_pixel_scroll_fn(bounds: dict):
    """A scroll callable taking a pixel delta, centered in the window."""
    cx = bounds["x"] + bounds["width"] / 2
    cy = bounds["y"] + bounds["height"] / 2

    def scroll(pixels: float):
        scroll_pixels_at(cx, cy, pixels)

    return scroll


def click_at(x: float, y: float, settle: float = 0.8) -> None:
    """Move the cursor to (x, y) and post one synthetic left click."""
    import Quartz

    move = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventMouseMoved, (x, y), Quartz.kCGMouseButtonLeft)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, move)
    time.sleep(0.15)
    down = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventLeftMouseDown, (x, y), Quartz.kCGMouseButtonLeft)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    time.sleep(0.05)
    up = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventLeftMouseUp, (x, y), Quartz.kCGMouseButtonLeft)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
    time.sleep(settle)  # let the UI settle (text reflow) before capture


def make_expand_fn(bounds: dict):
    """Expand folded post text: click the 'Show More' link. Target coords
    are normalized (0-1) row centers from the OCR; converted to screen
    points via the window bounds."""
    def expand(target: dict) -> None:
        sx = bounds["x"] + target["cx"] * bounds["width"]
        sy = bounds["y"] + target["cy"] * bounds["height"]
        click_at(sx, sy)

    return expand
