"""Screen reading helpers for the Moments collector (M1, Mac).

Window discovery uses the Quartz on-screen window list; capture shells out
to the macOS `screencapture` CLI. One-time user permissions required:
- Screen Recording (to capture other apps' window content) — without it
  macOS silently returns a wallpaper-only image, so callers should verify
  captured content (e.g. via the vision model) before trusting it.
- Accessibility (only needed for synthetic scrolling in M2).
"""
import os
import subprocess
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class WindowInfo:
    window_id: int
    owner: str
    title: str
    bounds: dict  # {x, y, width, height}


def list_windows() -> List[WindowInfo]:
    """List all on-screen windows via the Quartz window list."""
    from Quartz import (
        CGWindowListCopyWindowInfo,
        kCGWindowListOptionOnScreenOnly,
        kCGNullWindowID,
    )

    windows = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
    result: List[WindowInfo] = []
    for w in windows:
        window_id = w.get("kCGWindowNumber")
        owner = w.get("kCGWindowOwnerName", "")
        title = w.get("kCGWindowName", "")
        bounds = w.get("kCGWindowBounds", {})
        if window_id is None or not owner:
            continue
        result.append(WindowInfo(
            window_id=int(window_id),
            owner=str(owner),
            title=str(title or ""),
            bounds={
                "x": bounds.get("X", 0),
                "y": bounds.get("Y", 0),
                "width": bounds.get("Width", 0),
                "height": bounds.get("Height", 0),
            },
        ))
    return result


def find_window(owner_substring: str, title_substring: str = "") -> Optional[WindowInfo]:
    """Find the first on-screen window whose owner (and optionally title)
    contains the given substrings, case-insensitive. When several match,
    the largest one wins (the Moments view is the main content surface).
    """
    owner_sub = owner_substring.lower()
    title_sub = title_substring.lower()
    candidates = [
        w for w in list_windows()
        if owner_sub in w.owner.lower()
        and (not title_sub or title_sub in w.title.lower())
    ]
    candidates.sort(
        key=lambda w: w.bounds.get("width", 0) * w.bounds.get("height", 0),
        reverse=True,
    )
    return candidates[0] if candidates else None


def screen_is_locked() -> bool:
    """Whether the Mac's screen is currently locked / shielded.

    Observed live (2026-11-06): when the display is locked or privacy-
    shielded, the Window Server's "Display N Shield" window appears in the
    on-screen list — and window capture of other apps fails ("could not
    create image from window"). For the passive collector this is a normal,
    frequent state (M2 runs hourly): callers should skip, not crash.
    """
    from Quartz import (
        CGWindowListCopyWindowInfo,
        kCGWindowListOptionOnScreenOnly,
        kCGNullWindowID,
    )
    for w in CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly,
                                        kCGNullWindowID) or []:
        owner = str(w.get("kCGWindowOwnerName") or "").lower()
        title = str(w.get("kCGWindowName") or "").lower()
        if "shield" in title or owner == "loginwindow":
            return True
    return False


def capture_window(window_id: int, out_path: str) -> str:
    """Capture a window by CGWindowID to out_path (PNG) via `screencapture`.

    -x: no capture sound; -o: no window shadow.
    Raises RuntimeError on capture failure or empty output (the latter is
    the typical symptom of missing Screen Recording permission).
    """
    cmd = ["screencapture", "-x", "-o", "-l", str(window_id), out_path]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(f"screencapture failed (exit {proc.returncode}): {detail}")
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError(
            "screencapture produced no output - Screen Recording permission "
            "is likely missing for this process."
        )
    return out_path
