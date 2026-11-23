#!/usr/bin/env python3
"""Live one-round collection test.

1. locate the WeChat window and bring the app to the foreground
   (synthetic scroll events are delivered to the topmost window under the
   cursor — an occluded feed would eat them)
2. run one real collection round: capture → local OCR → scroll → …
   (max 3 screens) → pipeline dedupe/summarize into the real DB
3. print a compact per-screen report so we can verify the feed actually
   advanced between screens
"""
import sys

sys.path.insert(0, ".")
sys.dont_write_bytecode = True

from services.digest import db
from services.digest.collector import find_moments_window, run_moments_collection
from services.digest.scroller import accessibility_trusted


def wechat_pid(window_id):
    from Quartz import (CGWindowListCopyWindowInfo, kCGWindowListOptionOnScreenOnly,
                        kCGNullWindowID)
    for w in CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID):
        if int(w.get("kCGWindowNumber", -1)) == window_id:
            return int(w.get("kCGWindowOwnerPID", 0))
    return 0


def activate_wechat(pid):
    from AppKit import NSRunningApplication
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    for opts in (0x3, 0x2, None):  # AllWindows|IgnoringOtherApps, then fallbacks
        try:
            if opts is None:
                ok = app.activate()
            else:
                ok = app.activateWithOptions_(opts)
            if ok:
                return True
        except Exception:
            continue
    return False


def main():
    import time
    win = find_moments_window()
    if win is None:
        print("FATAL: no WeChat window on screen")
        return 1
    print(f"window: id={win.window_id} title={win.title!r} bounds={win.bounds}")
    trusted = accessibility_trusted()
    print(f"accessibility trusted: {trusted}")

    pid = wechat_pid(win.window_id)
    if pid:
        print(f"activating WeChat (pid {pid})… ", end="")
        print("ok" if activate_wechat(pid) else "FAILED")
        time.sleep(1.0)
        win2 = find_moments_window()
        if win2:
            win = win2
            print(f"window after activate: id={win.window_id} bounds={win.bounds}")

    conn = db.connect()
    before = len(db.list_digest_items(conn))
    print(f"digest items before: {before}")
    print("collecting (max 3 screens, OCR extract, real scroll)…")
    info = run_moments_collection(conn, max_screens=3)
    after = len(db.list_digest_items(conn))

    print(f"\n== round report ==")
    print(f"warnings: {info['warnings'] or 'none'}")
    for s in info["screens"]:
        print(f"  screen {s['index']}: items={s['items']} known={s['known']} "
              f"→ {s['image_path']}")
    st = info["stats"]
    print(f"pipeline: collected={st['collected']} new={st['new']} "
          f"summarized={st['summarized']} skipped={st['skipped']} "
          f"failed={st['failed']} errors={st['errors'] or 'none'}")
    print(f"digest items: {before} -> {after}")

    print("\nsample items (author | time | comments | text):")
    for it in info["raw_items"][:8]:
        text = (it.get("text") or "").replace("\n", " ")[:42]
        print(f"  · {it.get('author') or '?'} | {it.get('posted_at') or '?'} | "
              f"{len(it.get('thread') or [])}c | {text}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
