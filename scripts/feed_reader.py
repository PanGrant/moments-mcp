#!/usr/bin/env python3
"""The LIVE reading+reply loop: pure-↓ walk, per-post 处置 with replies.

Constitution (user 2026-11-15), mapped to code:
  1. schema 驱动 — walker owns WHO via _actionable (schema slots + ledger
     predicates only); this runner owns HOW. Every click coordinate comes
     from the post dict the schema filled on THIS run's pixels.
  2. 纯下翻 — the walk body moves by ↓ (keycode 125) only. Clicks
     (expand / •• / Comment / Send) are in-place actions, never motion;
     no wheel, no drags. 唯一例外（2026-11-17 用户授权）：↑ 门牌回攀
     （keycode 126）——超屏帖头部被 ↓ 对齐跳过时块内显帖头用，
     walker A2/A3，见 ASSUMPTIONS.md。
  3. 先测量后阅览 — FeedSchema.init once from the first capture; the
     stale-screen guard re-captures + re-scans with the SAME schema right
     before every click chain, so coordinates are never trusted across a
     screen change (the 2026-11-15 live lesson).

Per actionable post (doorplate + time row + •• button + <48h, not own /
ad / acted): expand 全文 (A1 lesson: collapsed text is a truncated read)
→ generate a fresh persona reply from what was actually read
(reviewer's tuned prompt; signature appended deterministically) → log
the card → paste (pbcopy + ⌘V) → verify in-box → click Send → verify
the comment landed and the box closed.

Card policy (user 2026-11-15, this run): 自提交 — the walker does not
pause for a human confirmation; every generated reply is logged BEFORE
pasting (author / post / reply text = the audit trail), and the ledger
keeps act_result. Signature (persona default):
（AI助手胡乱解读自动回复，内容请忽略）

Usage: python3 scripts/feed_reader.py [--keep] [--landings N]
"""
import importlib.util
import os
import subprocess
import sys
import time as t
from difflib import SequenceMatcher

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))
sys.dont_write_bytecode = True

spec = importlib.util.spec_from_file_location(
    "post_test", os.path.join(_HERE, "post_test.py"))
pt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pt)

from services.digest.reviewer import TooShortToReply
from services.digest.post_schema import FeedSchema, normalize_for_match, \
    scan_screen
from services.digest.walker import walk, is_top_tail

OUT = "/tmp/feed_reader"
LEDGER = "/tmp/walker_ledger.json"
LOG = "/tmp/walker.log"
ARROW_DOWN = 125
ARROW_UP = 126        # 门牌回攀（2026-11-17 用户授权，walker A2/A3）
KEY_V = 9
AUTHOR_FLOOR = 0.75      # OCR char jitter ('蛋壳'→'蛋売') — same bar as keys_match
POPUP_Y_TOL = 0.04       # Comment pill within ±0.04·SH of the •• click y


def wlog(msg):
    line = f"{t.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


import json as _json


def make_emitter(path):
    """Structured JSONL event stream for the executor host (FACE_DESIGN
    §3). Human logging (stdout + /tmp/walker.log) stays untouched —
    events go to their own file so the host can tail them cleanly."""
    if not path:
        return lambda ev: None
    f = open(path, "w")

    def emit(ev):
        rec = dict(ev)
        rec["ts"] = round(t.time(), 3)
        f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()

    return emit


def press_down():
    import Quartz
    dn = Quartz.CGEventCreateKeyboardEvent(None, ARROW_DOWN, True)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, dn)
    t.sleep(0.05)
    up = Quartz.CGEventCreateKeyboardEvent(None, ARROW_DOWN, False)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)


def press_up():
    """门牌回攀（2026-11-17 用户授权）：↑ 键，块内显帖头用。"""
    import Quartz
    dn = Quartz.CGEventCreateKeyboardEvent(None, ARROW_UP, True)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, dn)
    t.sleep(0.05)
    up = Quartz.CGEventCreateKeyboardEvent(None, ARROW_UP, False)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)


# ------------------------------------------- 锚定点击（⟳ 回位解除）
def find_cover_gap_blank(img, posts):
    """锚定点击落点：封面照片下方、首帖上方的空白带（2026-11-17）。

    落点三道门（UI 假设集中在此，布局变动只改本函数）：
    1. 带下沿 = schema 首帖 top_y——空白带 OCR 不到，只有帖子结构
       能标定它的下沿；
    2. 白带 ≥15px 且带顶是**非白硬边**——防把封面照片里的白色天空
       误当空白（点击封面照片有打开相册的风险，硬边=照片底缘）；
    3. 点击 patch（17x16px）全白，x 取内容区中部 0.40-0.60——避开
       封面右下头像/名牌带（0.80+）；左缘 0.12 的点击实证不解除
       （r-154847），x 位置是嫌疑变量，故取中部。
    找不到合格白带 → None（调用方 FATAL 不点不走先报告）。
    """
    tops = [float(p["top_y"]) for p in posts if p.get("top_y")]
    if not tops:
        return None
    gy = int(min(tops) * img.size[1])
    if gy < 30:
        return None
    px = img.load()
    for fx in (0.55, 0.50, 0.60, 0.45, 0.40):
        cx = int(fx * img.size[0])
        run, y = 0, gy - 1
        while y >= 0 and min(px[cx, y]) >= 245 and run < 400:
            run += 1
            y -= 1
        if run < 15 or y < 0 or min(px[cx, y]) >= 245:
            continue  # 白带太短 / 400px 白顶到天（无封面硬边）
        if all(min(px[x, yy]) >= 245
               for x in range(cx - 8, cx + 9)
               for yy in range(gy - 16, gy)):
            return fx, (gy - 6) / img.size[1]
    return None


def anchor_click_after_refresh(win, b, schema_box, wlog):
    """⟳ 后的"锚定点击"——把微信的"上次读到处"锚定在顶部。

    Why（机制，2026-11-17 用户手工实验定案）：⟳ 换血后朋友圈有条
    隐形分割线"上次看到这里"。换血当下视口稳在顶部（esc 重入验证
    过），但**首次下翻**会跳回旧读位——r-154847 实录：首按从封面
    飞过约 40 屏今日积压，精确落在上一轮的最深点（曹校长 Yesterday
    边界行）。用户手工实证：刷新后在首页点一下鼠标即可解除。本函数
    = 该动作的程序化：在封面照片下方的空白带点一下（落点由
    find_cover_gap_blank 从 schema+像素双验证得出）。

    历史与残余假设：r-154847 的旧焦点点击 (0.122,0.70) 其实也在同一
    空白带，但 x 贴最左缘（0.12）未解除——嫌疑变量是 x 位置，故落点
    取中部。若真机仍首按飞，解除变量另有其他（如时机），届时只改
    本函数换方案（fallback=↑回顶重按一次）。残余假设：真机封面-帖
    间距 ≥15px 恒成立；点击后 check_view 兜底任何导航意外。

    失败纪律：找不到落点 = 不点、不走，先报告（不盲点）。
    """
    pt.capture_window(win.window_id, f"{OUT}/pre.png")
    if schema_box["schema"] is None:
        schema_box["schema"] = FeedSchema.init(f"{OUT}/pre.png")
        wlog("骨架已初始化（首屏量定，全程 follow）")
    scan = scan_screen(f"{OUT}/pre.png",
                       schema=schema_box["schema"]) or {}
    posts = scan.get("posts") or []
    from PIL import Image
    img = Image.open(f"{OUT}/pre.png").convert("RGB")
    hit = find_cover_gap_blank(img, posts)
    if not hit:
        wlog("FATAL: 封面下空白带找不到合格落点——不点、不走，先报告")
        return False
    fx, fy = hit
    pt.guarded_click(win, b["x"] + fx * b["width"],
                     b["y"] + fy * b["height"], settle=1.0)
    wlog(f"锚定点击 ({fx:.3f},{fy:.2f}) 封面下空白带·首帖上方，已验纯白")
    return True


# ------------------------------------------------------- the act layer
def send_landed(seen: str, text: str) -> bool:
    """Send-verification probe: TEXT-SPECIFIC ONLY (head/tail 12
    normalized chars of our own reply). The signature probe is shared by
    EVERY comment we post — any one of our comments visible on screen
    satisfies it (the temple-post false-positive, 2026-11-17: a nearby
    真评论的签名单独满足了 any()，叠加输入框占位符没读到时的盲点降级
    → 假 ok 封账、帖上无评论、永不重试）。"""
    n = normalize_for_match(text).replace(" ", "")
    return bool(n) and (n[:12] in seen or n[-12:] in seen)


class Actor:
    """HOW: expand 全文 → fresh reply → paste → send on ONE post, every
    coordinate re-measured from fresh pixels before its click (rule 3)."""

    def __init__(self, schema_box, out_dir):
        self.schema_box = schema_box
        self.out_dir = out_dir
        self.n = 0          # capture counter (act evidence, kept for 阅后)
        # tall-post pending replies: author_norm → {author, full_body,
        # reply}. Written when the expansion pushed the •• below the fold
        # (reply generated from the FULL body at the doorplate landing),
        # consumed at the tail landing (head-cut — identity via full-body
        # row overlap; the ledger entry carries the walker-side identity).
        self.pending = {}

    def _capture(self, win, tag):
        self.n += 1
        path = f"{self.out_dir}/{tag}{self.n}.png"
        pt.capture_window(win.window_id, path)
        return path

    def fresh_scan(self):
        """Fresh window → capture → view guard → re-scan with the SAME
        run schema (never a new measurement — rule 3). Returns
        (scan, win, verdict) — scan is None when the view is not feed."""
        win = pt.fresh_win()
        path = self._capture(win, "a")
        v, why = pt.check_view(win, f"av{self.n}")
        if v not in pt.VIEW_OK:
            wlog(f"  [act] 视图守卫: {v} ({why})")
            return None, win, v
        return scan_screen(path, schema=self.schema_box["schema"]), win, v

    def find_post(self, scan, author):
        """The re-scan's doorplate post matching THIS author (fuzzy — OCR
        char jitter), the stale-guard identity anchor."""
        a_n = normalize_for_match(author or "")
        best, best_r = None, 0.0
        for p in scan.get("posts") or []:
            if not p.get("author") or not p.get("doorplate_visible"):
                continue
            r = SequenceMatcher(None, a_n,
                                normalize_for_match(p["author"])).ratio()
            if r > best_r:
                best, best_r = p, r
        return (best, None) if best_r >= AUTHOR_FLOOR else (None, best_r)

    def _pending_hit(self, post):
        """Which pending (if any) does this on-screen post belong to —
        decided by full-body row overlap (a tail's visible rows are a
        subset of the full body we read at the doorplate landing)."""
        norms = {normalize_for_match(t)
                 for t in (post.get("body") or {}).get("texts") or []}
        for k, pend in self.pending.items():
            pnorms = {normalize_for_match(t) for t in pend["full_body"]}
            if norms & pnorms:
                return k, pend
        return None, None

    def _resolve(self, scan, author_hint, entry_body=None):
        """The on-screen post for THIS act, identity roads: the doorplate
        (fuzzy author match), a pending tail (head-cut, full-body row
        overlap), the ledger bind (head-cut text fragment whose rows
        exactly overlap the entry's doorplate-recorded rows), or a bare
        photo tail. Returns (post, pend_key, near_ratio)."""
        post, near = (self.find_post(scan, author_hint)
                      if author_hint else (None, 0.0))
        if post is not None:
            k, pend = self._pending_hit(post)
            return post, k, near
        # head-cut road: any scan post whose rows hit a pending full body,
        # or one carrying a ledger-deferred reply (cross-process handoff)
        for p in scan.get("posts") or []:
            k, pend = self._pending_hit(p)
            if pend is not None or p.get("pending_reply"):
                return p, (k if pend is not None else None), None
        # 帐本绑定路（2026-11-17 深夜）：walker 精确锚绑定放行的无门牌
        # 文字片段带着 entry_author+entry_body 来；屏上无门牌帖的行与
        # entry_body 精确重叠的就是它。全局唯一性裁决（证据只指向一条
        # entry）在 walker 侧 _bind_headless 已做，这里只定位屏上位置。
        if entry_body:
            eb = {normalize_for_match(t) for t in entry_body}
            for p in scan.get("posts") or []:
                if p.get("author") and p.get("doorplate_visible"):
                    continue
                norms = {normalize_for_match(t)
                         for t in (p.get("body") or {}).get("texts") or []}
                if norms & eb:
                    k, pend = self._pending_hit(p)
                    return p, (k if pend is not None else None), None
        # 纯照片尾路（walker 第四尺认领的同一形状，2026-11-15）：walker
        # 带着 entry_author 来，而尾屏上这条尾巴无门牌、无一字——门牌路
        # 和正文重叠路都无处着力的，就是顶沿这条
        if author_hint:
            for p in scan.get("posts") or []:
                if is_top_tail(p):
                    k, pend = self._pending_hit(p)
                    return p, (k if pend is not None else None), None
        return None, None, near

    def click_frac(self, win, post, xkey, ykey, settle):
        b = win.bounds
        px = b["x"] + post[xkey] * b["width"]
        py = b["y"] + post[ykey] * b["height"]
        pt.guarded_click(win, px, py, settle=settle)
        return px, py

    # ------------------------------------------------ the reply + send
    def _generate(self, post):
        # entry_body 优先（2026-11-17 深夜）：它只在绑定片段上出现，是
        # 帐本门牌落地存的全量正文行；屏上坨里的行是半截，喂组稿会重演
        # L18 的内容错。行是归一文本（无标点小写）——组稿质量略降，
        # 换身份与内容双正确，值。
        body = "\n".join(post.get("entry_body")
                         or (post.get("body") or {}).get("texts") or [])
        try:
            text = pt.generate_fresh(post.get("author"), body)
        except TooShortToReply as e:
            # 用户 2026-11-15 令：帖文太短读不懂就不硬回——软跳过
            return None, {"kind": "too-short", "detail": str(e)}
        except Exception as e:
            return None, repr(e)
        return (text or "").strip(), None

    def _front_is_wechat(self):
        """最前窗守卫（2026-11-15 Anne 事故：微信弹层链路中终端升到最前，
        回复文本差点贴进终端）。屏上最前面的普通层窗口必须是微信——
        不是就不碰剪贴板/键盘。检查本身出错时放行（发送核验仍兜底）。"""
        try:
            import Quartz
            opts = (Quartz.kCGWindowListOptionOnScreenOnly
                    | Quartz.kCGWindowListExcludeDesktopElements)
            wl = Quartz.CGWindowListCopyWindowInfo(
                opts, Quartz.kCGNullWindowID)
            for w in wl or []:
                if w.get("kCGWindowLayer", 99) != 0:
                    continue
                owner = str(w.get("kCGWindowOwnerName", ""))
                return ("wechat" in owner.lower() or "微信" in owner)
            return False
        except Exception:
            return True

    def _paste(self, text):
        if not self._front_is_wechat():
            wlog("  [act] 前窗不是微信——放弃粘贴（防误贴其他应用）")
            return False
        p = subprocess.run(["pbcopy"], input=text.encode("utf-8"),
                           capture_output=True, timeout=10)
        if p.returncode != 0:
            return False
        import Quartz
        for down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(None, KEY_V, down)
            Quartz.CGEventSetFlags(ev, Quartz.kCGEventFlagMaskCommand)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            t.sleep(0.08)
        t.sleep(0.8)
        return True

    def _screen_text(self, path, b):
        return normalize_for_match("".join(
            r.get("text") or ""
            for r, _px, _py in pt.rows_in(pt.ocr_rows(path), b)
        )).replace(" ", "")

    def _send_green(self, path):
        """Mean-color test of the Send button row: green ⇒ the box holds
        content (gray ⇒ empty). Structural evidence, OCR-independent."""
        try:
            from PIL import Image
            rows = pt.ocr_rows(path)
            b = pt.fresh_win().bounds
            for r, _px, _py in pt.rows_in(rows, b):
                if normalize_for_match(r.get("text") or "").startswith("send"):
                    im = Image.open(path).convert("RGB")
                    W, H = im.size
                    crop = im.crop((int(r["x"] * W),
                                    int(r["y"] * H),
                                    int((r["x"] + r.get("w", 0.1)) * W),
                                    int((r["y"] + r.get("h", 0.015)) * H)))
                    pixels = list(crop.getdata())
                    n = len(pixels) or 1
                    mr = sum(p[0] for p in pixels) / n
                    mg = sum(p[1] for p in pixels) / n
                    mb = sum(p[2] for p in pixels) / n
                    return (mg - max(mr, mb)) > 20
        except Exception:
            return False
        return False

    def entry_chain(self, post, text, author_hint=None):
        """•• → Comment → field click → paste → verify → Send → verify.
        Mechanics per post_test cmd_box/do_send (live-proven chain).
        Returns a result dict; status 'ok' = sent and verified."""
        scan, win, v = self.fresh_scan()
        post, _k, _r = self._resolve(scan, author_hint
                                     or post.get("author"),
                                     entry_body=post.get("entry_body"))
        if post is None:
            return {"status": "stale-screen"}
        if post.get("button_x") is None or post.get("button_y") is None:
            # the schema's own button-missing predicate (never click None)
            return {"status": "button-missing"}
        px, py = self.click_frac(win, post, "button_x", "button_y", 1.0)

        popup = f"{self.out_dir}/pop{self.n}.png"
        pt.full_capture(popup)
        fb = pt.fresh_win().bounds
        comment = None
        for r, cpx, cpy in pt.rows_in(pt.ocr_rows(popup), fb):
            n = normalize_for_match(r.get("text") or "")
            if ("comment" in n or "评论" in n) and abs(cpy - py) < POPUP_Y_TOL * pt.SH:
                comment = (cpx, cpy)
                break
        if comment is None:
            pt.press_esc()          # leave the feed clean either way
            return {"status": "no-comment-popup"}
        pt.guarded_click(win, comment[0], comment[1], settle=1.2)

        boxp = f"{self.out_dir}/box{self.n}.png"
        pt.full_capture(boxp)
        send = None
        field = None
        for r, cpx, cpy in pt.rows_in(pt.ocr_rows(boxp), fb):
            n = normalize_for_match(r.get("text") or "")
            if n.startswith("send") and send is None:
                send = (cpx, cpy)
            if n in ("comment", "评论") and field is None and r["y"] > 0.4:
                field = (cpx, cpy)
        if send is None:
            pt.press_esc()
            return {"status": "no-input-box"}
        if field is not None:
            # click the placeholder text center exactly — offsetting left
            # lands outside the panel and dismisses the box (live 11-08)
            pt.guarded_click(win, field[0], field[1], settle=0.5)
        else:
            pt.guarded_click(win, fb["x"] + 0.5 * fb["width"],
                             send[1] - 40, settle=0.5)

        if not self._paste(text):
            pt.press_esc()
            return {"status": "paste-failed"}
        pastep = f"{self.out_dir}/paste{self.n}.png"
        pt.full_capture(pastep)
        ok, seen = pt.paste_verified(pastep, text)
        if not ok:
            # structural second evidence: a GREEN Send button means the
            # box holds content (gray when empty). Live 2026-11-15 the
            # text probes false-negatived on a wrapped box where OCR only
            # read the tail — the paste had actually landed. Measured on
            # real captures: green g−max(r,b)=+48.7, empty=0.0.
            ok = self._send_green(pastep)
        if not ok:
            pt.press_esc()
            wlog(f"  [act] 粘贴核验失败 框内OCR头60字={seen[:60]!r}")
            return {"status": "paste-failed", "seen_head": seen[:60]}

        # Send click on FRESH coords (the box grew after the paste)
        sendp = f"{self.out_dir}/send0_{self.n}.png"
        pt.full_capture(sendp)
        send2 = None
        for r, cpx, cpy in pt.rows_in(pt.ocr_rows(sendp), fb):
            if normalize_for_match(r.get("text") or "").startswith("send"):
                send2 = (cpx, cpy)
                break
        if send2 is None:
            pt.press_esc()
            return {"status": "send-vanished"}
        pt.guarded_click(win, send2[0], send2[1], settle=1.5)

        # verify: comment visible (head/tail/signature probe) + box closed
        sentp = f"{self.out_dir}/send1_{self.n}.png"
        pt.full_capture(sentp)
        fb2 = pt.fresh_win().bounds
        seen = self._screen_text(sentp, fb2)
        # 送达只认文本专属探针（head/tail12；签名单独不算数——某帖假 ok 冤案）
        landed = send_landed(seen, text)
        box_gone = all(normalize_for_match(r.get("text") or "") != "send"
                       for r, _px, _py in pt.rows_in(pt.ocr_rows(sentp), fb2))
        if landed and box_gone:
            return {"status": "ok", "sent": True, "field_found": field is not None}
        if box_gone:
            # the click HAPPENED and the box closed — render lag beat the
            # probe (live 11-09); screenshot kept for human eyeballs
            return {"status": "sent-unverified", "evidence": sentp,
                    "field_found": field is not None}
        pt.press_esc()
        return {"status": "send-stuck", "evidence": sentp}

    def act(self, post):
        """Walker's act_fn: resolve identity (doorplate or pending tail) →
        expand 全文 → reply now, or defer to the tail landing → paste+send."""
        scan, win, v = self.fresh_scan()
        if scan is None:
            return {"status": "stale-screen", "view": v}
        # 身份解析一次、随身携带（2026-11-16 深夜真机教训）：无门牌卡帖
        # 走 is_top_tail 路解析成功后，resolved post 是 fresh-scan 的裸
        # 帖（不带 entry_author）——entry_chain/expand 的再解析若从 post
        # 身上重新推导 hint，纯照片尾路（hint 门控）就断链 → stale-screen。
        # hint 是 walker 折叠的帐本身份，跨再解析不变。
        hint = post.get("author") or post.get("entry_author")
        entry_body = post.get("entry_body") or []
        post, pend_key, near = self._resolve(scan, hint,
                                             entry_body=entry_body)
        if post is None:
            return {"status": "stale-screen", "best_ratio": round(near, 3)}
        if entry_body and not post.get("author"):
            # 绑定片段：组稿正文用帐本的门牌落地行（全量），不用屏上
            # 坨里的半截行（L18 教训：坨行组稿 = 内容错）。_generate 见
            # entry_body 优先取它。
            post["entry_body"] = entry_body
        pend = self.pending.get(pend_key) if pend_key else None
        if pend is None and post.get("pending_reply"):
            # cross-process recovery: the deferred reply rides in the
            # ledger and the walker hands it back on the act target
            pend = {"reply": post["pending_reply"],
                    "full_body": post.get("pending_full_body") or []}
        result = {"author": post.get("author")
                  or post.get("entry_author")
                  or hint
                  or (pend or {}).get("author"), "expanded": False}

        if pend is not None and post.get("expand_y") is None:
            # TAIL LANDING of an earlier expansion: the doorplate is gone
            # (head-cut) or the doorplate road hit the same post — send
            # the stored reply, generated back then from the FULL body
            # (its card is already in the audit log)
            text = pend["reply"]
            result["full_body"] = pend["full_body"]
            result["reply"] = text
            wlog(f"  [卡·尾屏发出] {result['author']}: 评论={text!r}")
            r = self.entry_chain(post, text,
                                 author_hint=post.get("author") or hint)
            if r.get("status") in ("ok", "sent-unverified"):
                self.pending.pop(pend_key, None)   # sent — stop offering
            result.update(r)
            return result

        if post.get("expand_y") is not None:
            wlog(f"  [act] 展开全文 ({post['expand_x']:.3f},"
                 f"{post['expand_y']:.3f})")
            self.click_frac(win, post, "expand_x", "expand_y", 1.0)
            scan2, win, v2 = self.fresh_scan()
            post2, _k2, _r2 = self._resolve(scan2,
                                            post.get("author") or hint)
            if post2 is None:
                result["status"] = "stale-after-expand"
                result["view"] = v2
                return result
            post = post2
            result["expanded"] = True
            result["full_body"] = post["body"]["texts"]   # A1: 真正读完
            if post.get("button_x") is None or post.get("button_y") is None:
                # the expansion pushed the time row / •• below the fold:
                # generate NOW from the full body, defer the send to the
                # tail landing (pure ↓ brings it up; the walker folds the
                # post-act screen rows into identity so the tail landing
                # is claimable — live 2026-11-15 design, replaces the
                # crashed None×width click and the read-but-unreplied miss)
                text, err = self._generate(post)
                if not text:
                    if isinstance(err, dict) and err.get("kind") == "too-short":
                        wlog("  [act] 帖文太短读不懂，不硬回——跳过")
                        result["status"] = "skipped-too-short"
                        result["soft"] = True
                        result["error"] = err["detail"]
                        return result
                    result["status"] = "gen-failed"
                    result["error"] = err
                    return result
                result["reply"] = text
                self.pending[normalize_for_match(result["author"] or "")] = {
                    "author": result["author"],
                    "full_body": result["full_body"], "reply": text}
                wlog(f"  [卡·自动批准·待尾屏] {result['author']}: "
                     f"评论={text!r}")
                result["status"] = "expanded-tail-deferred"
                result["soft"] = True
                result["deferred"] = True
                return result

        text, err = self._generate(post)
        if not text:
            if isinstance(err, dict) and err.get("kind") == "too-short":
                wlog("  [act] 帖文太短读不懂，不硬回——跳过")
                result["status"] = "skipped-too-short"
                result["soft"] = True
                result["error"] = err["detail"]
                return result
            result["status"] = "gen-failed"
            result["error"] = err
            return result
        result["reply"] = text
        # the card, auto-approved this run (user 2026-11-15 令·自提交):
        # logged BEFORE any paste — this line is the audit trail
        wlog(f"  [卡·自动批准] {result['author']}: 评论={text!r}")

        r = self.entry_chain(post, text,
                             author_hint=post.get("author") or hint)
        result.update(r)
        return result


# ------------------------------------------------------------- main
def _settle_fingerprint(scan):
    """起步安定指纹：schema 单槽感知（帖数 + page_bottom_closer），
    不做全帧散点比对（用户 schema 哲学）。空读（0 帖，换血中的白帧）
    返回 None=永不算安定。"""
    s = scan or {}
    posts = s.get("posts") or []
    if not posts:
        return None
    pb = s.get("page_bottom_closer") or {}
    return (len(posts), pb.get("text"), pb.get("y"),
            tuple((p.get("author") or "") for p in posts))


def feed_settled(measure, sleep_fn, log, max_rounds=4, gap=2.0):
    """起步安定复检（2026-11-16 深夜竞态教训）：⟳ 换血可能晚于固定
    2.5s 睡眠完成——当晚 pre.png 拍到封面顶、走路起步后，一次 ↓ 就被
    换血中的 feed 拽到边界帖（瞬移 30 帖、2 落地即停）。故起步前连续
    两次测量指纹一致才放行；始终不安定=诚实 FATAL，一步不走。
    （先测量后阅览：测到安定才开始阅览。）"""
    prev = None
    for k in range(max_rounds):
        fp = _settle_fingerprint(measure(k))
        if fp is None:
            log(f"起步复检 第{k + 1}测: 空读(0帖) → 视 feed 未安定")
        elif fp == prev:
            return True
        elif prev is None:
            log(f"起步复检 第{k + 1}测: 首测建基线 → 等{gap:.0f}s 再测")
        else:
            log(f"起步复检 第{k + 1}测: 指纹有变（换血未完）→ 等{gap:.0f}s 再测")
        prev = fp
        if k < max_rounds - 1:
            sleep_fn(gap)
    return False


def recover_post_refresh(win, wlog, pt_mod=None):
    """刷新后的回顶梯子：先 esc→重入——无换血时它就是真回顶（22:05
    轮实证）；重入不行＝换血真发生了，重入只恢复读位（23:19 实证
    两连重入落回同一行）→ ↑ 连打兜底（2026-11-16 深夜用户批准，
    仅限恢复阶段，walk 本体永远纯 ↓）。"""
    pt_ = pt_mod or pt
    if pt_.recover_view(win, "post_refresh_top", require_top=True):
        return True
    wlog("重入未到顶 → ↑ 连打回顶（恢复专用）")
    return pt_.recover_top_by_arrow_up(win, "post_refresh_up", log=wlog)


def main():
    max_landings = 10
    if "--landings" in sys.argv:
        max_landings = int(sys.argv[sys.argv.index("--landings") + 1])
    keep = "--keep" in sys.argv
    mode = "act"
    if "--mode" in sys.argv:
        mode = sys.argv[sys.argv.index("--mode") + 1]
    events_path = None
    if "--events" in sys.argv:
        events_path = sys.argv[sys.argv.index("--events") + 1]
    emit = make_emitter(events_path)
    action_events = []
    # run_start 提前到 spawn 即发（2026-11-22 TestBuddy 实跑教训：刷新链
    # 可长至 ~4 分钟且原先全程零事件，status 一直"0 屏"被当卡死停掉——
    # 事件文件一出生就有 run_start + phase，进度随时可查）。
    emit({"t": "run_start", "mode": mode, "max_landings": max_landings,
          "pid": os.getpid()})

    def emit_wrap(ev):
        if ev.get("t") == "action":
            action_events.append(ev)
        emit(ev)

    os.makedirs(OUT, exist_ok=True)
    if keep:
        wlog(f"帐本续用: {LEDGER}")
    else:
        if os.path.exists(LEDGER):
            os.remove(LEDGER)
        wlog("帐本已清（新走一趟；--keep 可续）")

    win = pt.activate()
    b = win.bounds
    wlog(f"窗口 {b['width']:.0f}x{b['height']:.0f}pt")

    # ---- ⟳ 起手刷新【2026-11-17 用户批准恢复】。历史（2026-11-16 深夜
    # 用户令曾摘出）：⟳ 换血触发微信"回位到上次读到处"（点击后 ~9-13s
    # 把视口拖离顶部），换血后顶部在程序上不可达——esc→重入只恢复读位
    # （23:19 轮两连重入落回同一行），↑/Home/PageUp 在此视图全部
    # 无绑定（160 按 + 单测实证）。恢复时的安全网：⟳ 前 pre-check 不在
    # 顶部先走恢复梯子；⟳ 后等 20s（换血+延迟回位彻底结束）再 recover_
    # post_refresh（esc→重入 → ↑ 连打兜底，恢复专用）；回顶验证失败
    # = FATAL 一步不走先报告。14:04 受控复核已证：无 ⟳ 场景 feed 静止
    # 诚实（A7），本次恢复的是"新鲜内容+墙钟锚"的能力，回位风险由梯子
    # 与 FATAL 停机兜住。
    if "--no-refresh" in sys.argv:
        st, why = "SKIPPED", "--no-refresh"
    else:
        st, why = pt.do_refresh(win)
        emit({"t": "phase", "step": "refresh", "status": st})
        if st in ("CONFIRMED", "UNVERIFIED"):
            wlog("等 20s：⟳ 换血 + 延迟回位彻底结束再回顶")
            emit({"t": "phase", "step": "settle-wait", "seconds": 20})
            t.sleep(20)
            if not recover_post_refresh(win, wlog):
                emit({"t": "summary", "stop_reason": "refresh-fatal",
                      "landings": 0, "presses": 0, "acted": 0,
                      "replied_ok": 0})
                wlog("FATAL: 刷新后回位/回顶失败——一步没走，停（先报告）")
                return 2
    if st == "FATAL":
        emit({"t": "summary", "stop_reason": "refresh-fatal",
              "landings": 0, "presses": 0, "acted": 0, "replied_ok": 0})
        wlog("FATAL: 刷新失败且视图无法恢复——一步没走，停（先报告）")
        return 2
    wlog(f"起手刷新: {st} ({why})")

    # ---- 锚定点击（⟳ 回位解除，2026-11-17 用户手工实验定案）。
    # 封装在 anchor_click_after_refresh：落点三道门+失败纪律见其 docstring。
    schema_box = {"schema": None}
    anchored = anchor_click_after_refresh(win, b, schema_box, wlog)
    emit({"t": "phase", "step": "anchor-click", "ok": bool(anchored)})
    if not anchored:
        emit({"t": "summary", "stop_reason": "anchor-fail",
              "landings": 0, "presses": 0, "acted": 0, "replied_ok": 0})
        return 2
    v, why = pt.check_view(win, "pre_view")
    wlog(f"点击后视图: {v} ({why})")
    if v not in pt.VIEW_OK:
        wlog(f"FATAL: 视图异常 {v}——一步没走，停（先报告）")
        emit({"t": "summary", "stop_reason": "view-broken",
              "landings": 0, "presses": 0, "acted": 0, "replied_ok": 0})
        return 2

    # ---- 起步安定复检（2026-11-16 深夜竞态，见 feed_settled docstring）。
    # 骨架已由锚定点击帧量定（schema_box 预填）——不在换血中的帧上开局。
    state0 = {"win": win}

    def settle_measure(k):
        state0["win"] = pt.fresh_win()
        p = f"{OUT}/settle{k}.png"
        pt.capture_window(state0["win"].window_id, p)
        if schema_box["schema"] is None:
            schema_box["schema"] = FeedSchema.init(p)
            wlog("骨架已初始化（首屏量定，全程 follow）")
        return scan_screen(p, schema=schema_box["schema"])

    if not feed_settled(settle_measure, t.sleep, wlog):
        emit({"t": "summary", "stop_reason": "feed-unsettled",
              "landings": 0, "presses": 0, "acted": 0, "replied_ok": 0})
        wlog("FATAL: feed 未安定（⟳ 换血未完成）——一步没走，停（先报告）")
        return 2
    emit({"t": "phase", "step": "settled"})
    wlog("feed 已安定（连续两测一致），起步")

    state = {"n": 0, "win": win}
    state["win"] = state0["win"]

    def capture():
        state["n"] += 1
        state["win"] = pt.fresh_win()
        path = f"{OUT}/l{state['n']}.png"
        pt.capture_window(state["win"].window_id, path)
        return path

    # run skeleton measured ONCE (rule 3)——骨架已在安定复检中量定
    # （schema_box 传入 settle_measure），scan_fn 直接 follow 同一张表。

    def scan_fn(path):
        v, why = pt.check_view(state["win"], f"l{state['n']}_v")
        if v not in pt.VIEW_OK:
            wlog(f"视图守卫: {v} ({why}) → 本脚不动作，交循环处理")
            return None
        if schema_box["schema"] is None:
            schema_box["schema"] = FeedSchema.init(path)
            wlog("骨架已初始化（首屏量定，全程 follow）")
        return scan_screen(path, schema=schema_box["schema"])

    actor = None if mode == "readonly" else Actor(schema_box, OUT)
    r = walk(press_down=press_down, press_up=press_up,
             capture=capture, scan_fn=scan_fn,
             cfg={"own_names": pt.OWN_NAMES}, ledger_path=LEDGER,
             log=wlog, max_landings=max_landings,
             act_fn=actor.act if actor else None, emit=emit_wrap)

    # ------------------------------------------------------- summary
    ok = sum(1 for ev in action_events if ev.get("result") == "ok")
    emit({"t": "summary", "stop_reason": r["stop_reason"],
          "landings": r["landings"], "presses": r["presses"],
          "acted": len(action_events), "replied_ok": ok})
    wlog(f"== 停止: {r['stop_reason']} | 落地 {r['landings']} "
         f"按键 {r['presses']}")
    for e in r["ledger"]["posts"]:
        ar = e.get("act_result") or {}
        if e.get("acted"):
            line = (f"  帖 {e['author']!r} status={e['status']} "
                    f"reply={ar.get('status')}")
            if ar.get("expanded"):
                line += " 展开全文"
            if ar.get("reply"):
                line += f" 评论={ar['reply']!r}"
            wlog(line)
        else:
            wlog(f"  帖 {e['author']!r} status={e['status']} 只读 "
                 f"time={e['time_text']!r}")
    wlog(f"  sightings(头切待定): {len(r['ledger']['sightings'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
