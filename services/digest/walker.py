"""The ↓ walker — decision layer (topmost), v1 READ-ONLY.

Opportunistic design (accepted 2026-11-15): the walker never positions
precisely. It presses ↓ (keycode 125, anchor-scroll — the movement model:
each press reveals the next block's bottom, staged time-row → comment-
bottom stations, tall blocks fly), and at EVERY landing asks the
post-schema contract "where am I". The first landing that can attribute
a post IS good enough — displacement precision is unnecessary because
only <1 screen of overlap is needed to read a doorplate.

v1 scope (user-approved plan): walk + ledger + self-checks, NO expand
clicks, NO cards, NO comments, NO wheels (pure ↓), NO drags (banned).
2026-11-17 宪法修正案（用户授权）：↑ 门牌回攀——唯一允许的非↓运动，
限块内门牌恢复用（A2 超屏帖头部结构性跳过 / A3 ↑显头·↓自愈；
假设总账见 ASSUMPTIONS.md）。
Hard rules inherited: every future comment still gets a confirmation
card; strict 1-day boundary — a post whose time row reads ≥2 days old
ends the whole walk (BOUNDARY); sponsored posts (arrow marker) skip
without a card; own posts never enter the pipeline.

This module is the PURE decision core: ledger semantics + per-landing
decisions. The IO loop (capture → scan → identify → decide → press ↓)
lands in step 2 with injectable press/capture/scan functions so the
whole walk runs offline against scripted screens.

Ledger shape (persisted as JSON by the step-2 loop):

    {"posts": [entry...], "sightings": [sighting...]}

    entry (a post we could attribute at least once):
        key        "author|time" — stable identity
        author     str
        time_text  latest known time-row text (None until seen)
        body_head  first long body row, normalized (fuzzy identity)
        anchors    up to 3 anchor norms (fuzzy identity, image-wall safe)
        status     "pending" (v2 will card it) | "skipped_ad" | "own"
        landings   landing numbers where this entry was re-recognized

    sighting (a head-cut post no entry claims):
        body_head, anchors, landing — 身份待定, never carded (walker v1
        keeps walking; the ↑-mirror rescue is test C, deferred).

Identity across landings: doorplate (author) + body rarely share a
screen for tall posts — the doorplate landing sees the body HEAD, the
comment-bottom landing sees the time row. Matching therefore runs on
three rulers, first hit wins: exact (author, time), fuzzy anchor match
against the entry's stored anchors (SequenceMatcher ≥ BODY_FUZZY), exact
anchor overlap ≥ ANCHOR_OVERLAP.
"""
import json
import os
import re
import time as _time
from difflib import SequenceMatcher
from typing import Callable, Dict, List, Optional, Tuple

from .post_schema import normalize_for_match, parse_time_hours

BODY_FUZZY = 0.85       # body-head identity bar (garble-resistant)
ANCHOR_OVERLAP = 2      # exact anchor hits that confirm identity
BODY_HEAD_MIN_LEN = 10  # same bar as the schema's anchor rows
MAX_ANCHORS = 3
# armed（挂账）条目的锚上限：第四尺尾屏认领拿"尾屏行 vs 锚"当证据，
# 而尾行只在门牌/回攀落地全帖入镜时才可得——3 行帽只够装帖头，尾行
# 永远进不来 = 认领必败（2026-11-17 James 梅岭帖尾屏拒认定案）。
# 挂账期间放宽到 12；摘账后照旧 3（普通匹配不吃这个宽度）。
MAX_ANCHORS_ARMED = 12

# 绝对时间合并门（2026-11-17，A9 + 用户裁定"小时级比较、±1小时"）：
# 同一帖跨轮重见时 "6 hour(s) ago"→"7 hour(s) ago"，作者尺的时间相等
# 判定失效（2026-11-15 夜 Samuel Cui/小安 双回复事故）。旧漂移尺按墙钟
# 对账（标签增量 ≈ now−seen_at）——seen_at 是 identify 每次并账都刷新
# 的活变量，一刷即"并账自毁"（同落地重匹配全败）。abs 门改用冻结量：
# abs = 见到时刻 − 标签小时数（发布窗上沿，A9 floor 制），entry.abs_time
# 创建即冻结、永不回写，自毁在这把尺上结构不可能。
# 容差 = 用户裁定 ±1h（2026-11-17 终版：先前议的 15 分钟对冲/取整被
# 用户收回——分钟标签生命周期太短，小时粒度足够，算法从简）。floor 制
# 下同帖两次估值 |Δabs| 最坏恰好 <60min，±60 恰好覆盖。照片帖 anchors
# 全空、无文字可辨——宁可漏一条 1 小时差的新帖，不可对同一帖回复两次。
ABS_TOL_MIN = 60
# 弱路（作者匹配但一侧时间没露面）并帐所需的最低正文锚亲缘：
# 同作者两条帖的行交叉相似度实测 ~0.2（AuthorE 双帖），同一帖
# 自身行是 1.0，0.4 分得很开
WEAK_AUTHOR_FLOOR = 0.4
# 精确锚绑定的单行证据门槛（2026-11-17 深夜）：片段行归一后 ≥12 字才
# 配当身份证据——与送达探针的头/尾 12 字同一约定，短行/噪声行不作数。
BIND_ROW_MIN = 12

# 门牌回攀（2026-11-17 用户授权的宪法修正案；A2/A3，见 ASSUMPTIONS.md）：
# ↓ 智能滚动对齐落在下一贴时间条上，超屏帖的头部整段被跳过——门牌
# 结构性不可见，纯↓重跑永不可见（梅岭/James 实证）。↑ 回攀显帖头，
# 此后 ↓ 原规律自愈（智能滚动按当前位置重对齐，屏底不变量自动恢复）。
CLIMB_MAX_PRESS = 3    # 单块回攀按键限额。Q2（用户裁决 2026-11-17）：
                       # 超长帖微信会折叠，一般一按 ↑ 即见帖头——3 只是
                       # 防御异常形状的安全限额，不是预期用量
CLIMB_BOTTOM_Y = 0.85  # 时间条贴此线以下 = 对齐块（A1 落点形状）
CLIMB_TOP_Y = 0.06     # 块体起于此线以上 = 帖头确在折叠线上方（A2）

# 照片尾认领（2026-11-15 AuthorE 12图帖漏回）：门牌帖跨过屏幕底缘、
# 时间/•• 未露面时 entry 挂 tail_pending；下一落地顶在屏幕最上沿、无门牌、
# 无文字行的尾巴凭该挂账认领（三尺都要文字行，纯照片尾一行都没有）。
STRADDLE_Y = 0.985      # post bottom crossing here = straddles the fold
TAIL_TOP_Y = 0.02       # a tail starting here = top-of-screen tail

# BOUNDARY = 出了"今天"工作集（用户 2026-11-15 令）：yesterday / N days
# ago / 前天 / 周月年 都停走。分钟/小时（含 just now/今天 HH:MM）在界内。
# Weekday 名 / 绝对日期不可析（可能 3 天也可能 30 天）——同样停（保守，
# 宁停勿越）。行动窗口 act_max_hours 同步收紧到 24（yesterday 帖不回）。


def is_boundary_time(time_text: Optional[str]) -> bool:
    """Whether a time-row text means out of today's working set (BOUNDARY
    stop). 用户 2026-11-15 令：标签是相对现在的——<1h 分钟、<24h 小时、
    超过 24h 就是 yesterday / N days ago；看到 yesterday 或 days ago
    就不要跑了（昨天/1 day ago 也出界，v1 的 ≥2d 规则废止）。"""
    if not time_text:
        return False
    low = time_text.lower()
    if "yesterday" in low or "昨天" in time_text:
        return True
    n = normalize_for_match(time_text)
    if not n:
        return False
    if n == "前天" or n.startswith("前天 "):
        return True
    m = re.match(r"^(\d+) ?天前$", n)
    if m:
        return int(m.group(1)) >= 1
    m = re.match(r"^(\d+) day(?:s|\(s\))? ago$", n)
    if m:
        return int(m.group(1)) >= 1
    if re.match(r"^\d+ ?(?:个)?(?:周|星期)前$", n) \
            or re.match(r"^\d+(?:个月|年)前$", n) \
            or re.match(r"^last (?:week|month|year)$", n) \
            or re.match(r"^\d+ (?:week|month|year)(?:s|\(s\))? ago$", n):
        return True
    # 星期名 / 绝对日期 = 昨天 之外（微信：本周显示星期、更早显示日期）；
    # 可能 3 天也可能 30 天——保守出界（宁停勿越）
    if re.search(r"周[一二三四五六日天]|星期[一二三四五六日天]", n) \
            or re.search(r"\d{1,2}月\d{1,2}日", n) \
            or re.match(r"^(mon|tue|wed|thu|fri|sat|sun)", low) \
            or re.match(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)",
                        low):
        return True
    return False


def empty_ledger() -> Dict:
    return {"posts": [], "sightings": []}


def _long_norms(texts: List[str]) -> List[str]:
    out = []
    for t in texts:
        n = normalize_for_match(t)
        if len(n) >= BODY_HEAD_MIN_LEN:
            out.append(n)
    return out


def _arm_tail(entry: Dict, post: Dict) -> None:
    """tail_pending 挂账/摘账（纯照片尾认领的帐本侧）：帖底越过屏幕底缘
    且控件未露面 → 挂；帖完整（时间行+•• 都见了）且已回完（acted）→ 摘。
    完整但未回完的帖保留挂账——identify 先于 act 跑，本落地的认领尺还要
    凭它认领；own/ad/too-old 在处置门处照样拦得住，无误伤。多屏照片帖的
    尾屏自己继续跨越底缘，自动续挂。飞跳落地时由 walk() 统一清账。"""
    if post.get("time_row") is not None \
            and post.get("button_x") is not None:
        if entry.get("acted"):
            entry.pop("tail_pending", None)
    elif post.get("bottom_y", 0.0) >= STRADDLE_Y:
        entry["tail_pending"] = True


def is_top_tail(post: Dict) -> bool:
    """纯照片尾形状：无门牌、顶在屏幕最上沿（文字行有无由调用方另判）。"""
    return (not post.get("author")
            and post.get("top_y") is not None
            and post["top_y"] <= TAIL_TOP_Y)


def _entry_key(author: str, time_text: Optional[str]) -> str:
    t = normalize_for_match(time_text or "") or "?"
    return f"{normalize_for_match(author)}|{t}"


def _abs_time(time_text: Optional[str], now: float) -> Optional[float]:
    """时间标签 → 发布窗上沿的绝对时间（A9 floor 制）：abs = now − 标签。
    None 当标签不可解析、已是日历带（昨天/N 天——A6 边界族，≥24h 不进
    换算）、或"今天"族（今天 HH:MM 的钟点 parse_time_hours 不解析，
    硬换算会错几小时）。全程小时粒度（用户 2026-11-17 裁定），不做
    分钟级取整。"""
    if not time_text:
        return None
    h = parse_time_hours(time_text)
    if h is None or h <= 0.0 or h >= 24.0:
        return None
    return now - h * 3600.0


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _match_entry(post: Dict, ledger: Dict) -> Optional[Dict]:
    """Three-ruler identity match against known entries (first hit wins):
    exact (author, time) → fuzzy anchor vs entry anchors (a tall post's
    continuation landing shows DIFFERENT rows than its doorplate landing,
    so the fuzzy ruler compares every row against every stored anchor,
    not just the heads) → exact anchor overlap."""
    author = post.get("author")
    time_text = (post.get("time_row") or {}).get("text")
    body = post.get("body") or {}
    norms = _long_norms(body.get("texts") or [])
    for entry in ledger["posts"]:
        if author and entry["author"] == author:
            et = normalize_for_match(entry.get("time_text") or "")
            nt = normalize_for_match(time_text or "")
            if et and nt and et == nt:
                return entry
            if not et or not nt:
                # 弱路（一侧时间没露面）不能盲并：同作者两条帖时会把另一
                # 条帖的行折进来（2026-11-15 AuthorE 双帖并帐——新帖
                # 的 acted 把旧帖未发出的回复遮成"已回过"）。正文锚毫无
                # 亲缘就跳过该 entry 继续试后面候选；norms/anchors 缺一
                # 边（纯照片尾）照旧放行，第四尺兜底不受影响。
                if norms and entry.get("anchors"):
                    best = max(_ratio(a, b) for a in norms
                               for b in entry["anchors"])
                    if best >= WEAK_AUTHOR_FLOOR:
                        return entry
                else:
                    return entry
            else:
                # 绝对时间合并门（2026-11-17 A9 落地；只对已回过的帖——
                # 并错未回帖会把回复送到错帖）。旧墙钟漂移尺退役：标签
                # 增量 ≈ now−seen_at 里 seen_at 是 identify 每次并账都刷
                # 的活变量，一刷即并账自毁（同落地重匹配全败→acted 闸被
                # 绕过→发帖后裸断不记账）。abs 门只用冻结量：两侧各算
                # 发布窗上沿（见到时刻−标签），|Δ| ≤ ABS_TOL_MIN 即同帖；
                # 同一帖的 abs 不随标签长大而动（标签增量≈墙钟增量），自毁
                # 在这把尺上结构不可能。
                # 内容亲缘闸（2026-11-17 无草 DS 帖三连回复定案）：同作者
                # 的另一条帖 abs 同样可能落窗内（一天 9 条 Channels 转发）。
                # 两侧都有行而锚最大亲缘 < WEAK_AUTHOR_FLOOR = 拒并（弱路
                # 2026-11-15 AuthorE 双帖同款闸）；一侧无行（纯照片尾）
                # 照旧放行，第四尺兜底。
                ea = entry.get("abs_time")
                pa = _abs_time(time_text, _time.time())
                if (entry.get("acted") and ea is not None and pa is not None
                        and abs(pa - ea) <= ABS_TOL_MIN * 60.0):
                    if norms and entry.get("anchors") and max(
                            _ratio(a, b) for a in norms
                            for b in entry["anchors"]) < WEAK_AUTHOR_FLOOR:
                        pass  # 锚无亲缘 = 同作者的另一条帖，不并
                    else:
                        return entry
        if norms and entry.get("anchors"):
            best = max(_ratio(a, b) for a in norms
                       for b in entry["anchors"])
            if best >= BODY_FUZZY:
                return entry
        if len(set(norms) & set(entry.get("anchors") or [])) >= ANCHOR_OVERLAP:
            return entry
    # 第四尺（兜底，2026-11-15 AuthorE 12图帖漏回）：纯照片尾——无
    # 门牌，文字尺对它无从着力（真实扫描里时间行会并进 body：17 字的
    # "32 minute(s) ago" 不是身份线索），前三尺全空后按位置+挂账认领。
    # 时间行门（2026-11-17 AuthorA 照片尾滞留定案，用户设计“记住状态+
    # 用同一套历史-vs-当前的比较法认这贴是不是刚才那贴”）：碎片时间行
    # 在（照片尾唯一能读的文本证据）时，账上有标签的 armed 条目必须与
    # 碎片标签 normalize 严格相等，不符出局——挂账条目若有标签（门牌
    # 落地见过时间行）还与尾巴对不上，位置证据就不足为凭。账上全无标
    # 签（James 式：门牌落地时间行未露面，标签本就空）没法比，维持旧
    # 路。不做小时容差：贴容差=给同轮巧合撞车开门。真机定案：AuthorA 滞
    # 留的真因是僵尸挂账（无标签、旧伤疤）把单臂变多臂、照片尾拿不出
    # 文字打破僵局——本门让无标签僵尸在有标签碎片前自动出局。盲飞清
    # 账（位置连续性）；acted 拦重放。
    if is_top_tail(post):
        # 挂账军团过滤（2026-11-17 James 梅岭帖尾屏拒认定案）：摘账只
        # 发生在"acted 且完整重见"，轮次一断挂账就永久滞留——真机账上
        # 22 条挂账、其中多条早已 acted。acted 条目不需要任何投递，留
        # 在 armed 集里只会污染判定。
        armed = [e for e in ledger["posts"]
                 if e.get("tail_pending") and not e.get("acted")]
        tr_norm = normalize_for_match(
            ((post.get("time_row") or {}).get("text")) or "")
        cands = armed
        if tr_norm:
            labeled = [e for e in armed
                       if normalize_for_match(e.get("time_text") or "")]
            if labeled:
                cands = [e for e in labeled
                         if normalize_for_match(e.get("time_text") or "")
                         == tr_norm]
        if len(cands) == 1:
            return cands[0]
        if len(cands) > 1 and norms:
            best_e, best_v, second_v = None, 0.0, 0.0
            for e in cands:
                v = max((_ratio(a, b) for a in norms
                         for b in (e.get("anchors") or [])),
                        default=0.0)
                if v > best_v:
                    best_e, best_v, second_v = e, v, best_v
                elif v > second_v:
                    second_v = v
            if best_e is not None and best_v >= BODY_FUZZY \
                    and best_v > second_v:
                return best_e
    return None


def _resolved_entry(post: Optional[Dict], ledger: Dict) -> Optional[Dict]:
    """判决分叉修的消费端（2026-11-17）：identify 在本落地盖的章
    （post["_matched"]）是唯一身份判决——有章用章，章里的 None 也是
    判决（身份未决），绝不因账本中途变动而重算翻脸；无章（直接单测
    调用/未经 identify 的旧扫描）才回退老路重算。post 缺席（cur 无）
    = 无从判决，None。"""
    if post is None:
        return None
    if "_matched" in post:
        return post["_matched"]
    return _match_entry(post, ledger)


def _bind_headless(post: Dict, ledger: Dict) -> Optional[Dict]:
    """精确锚绑定（2026-11-17 深夜，D2 身份门的窄门）：无门牌文本片段
    的唯一安全身份路——片段的长行（归一 ≥BIND_ROW_MIN）必须逐行精确
    命中某条 entry 的 anchors（门牌落地存下的正文行），且全部证据只
    指向这一条 entry。模糊比率在这条路上一律无效：L18 冤案 = 无门牌
    坨（作者己帖尾+作者甲卡顶）被模糊匹配成作者甲、坨里作者己的正文喂了
    组稿；两条 entry 的证据并存 = 混块，拒绝（宁漏勿错送）。OCR 花字
    破坏精确相等 → 绑不上 → 诚实跳过，同旧路。acted/ad/own 照常绑定
    ——绑定后由 _actionable 后续关卡各自拦住（跳过已回过 等留痕）。
    只认 anchors（entry 建账必经门牌落地 → 身份出处门牌已证）；纯照片
    尾/无字片段无行可绑，归第四尺与 armed 路管。"""
    norms = [n for n in _long_norms((post.get("body") or {}).get("texts")
                                    or []) if len(n) >= BIND_ROW_MIN]
    if not norms:
        return None
    hit = None
    for e in ledger["posts"]:
        anchors = set(e.get("anchors") or [])
        if any(n in anchors for n in norms):
            if hit is not None and hit is not e:
                return None          # 证据指向两条 entry = 混块
            hit = e
    return hit


def _climb_target(post: Dict, ledger: Dict) -> Optional[Tuple[str, str]]:
    """门牌回攀目标判定（2026-11-17 用户授权的宪法修正案；A2/A3）。
    目标形状，四条同时成立：作者缺席 + 身份未决 + 时间条贴屏底
    （对齐块，A1 落点）+ 块体起于折叠线上方（帖头确被跳过，A2）。
    让路规则：armed 尾（tail_pending）由投递机器认领，回攀会打乱
    送达编排 → 拒；精确绑定已认出身份 → 拒；未武装的模糊认领**不拒**
    ——回攀露出真门牌恰好验证/证伪模糊身份（L18 教训的正向运用）。
    广告不攀。返回块键（时间条文本, 首长行）供每块每轮一次的去重。"""
    if post.get("author") or post.get("doorplate_visible"):
        return None
    if post.get("sponsored_arrow"):
        return None
    tr = post.get("time_row") or {}
    ty = tr.get("y")
    top = (post.get("body") or {}).get("top_y")
    if ty is None or top is None:
        return None
    if ty < CLIMB_BOTTOM_Y or top > CLIMB_TOP_Y:
        return None
    e = _resolved_entry(post, ledger)
    if e is not None and e.get("tail_pending"):
        return None            # armed 尾：投递路 owns
    if _bind_headless(post, ledger) is not None:
        return None            # 精确绑定可识别：无需回攀
    norms = _long_norms((post.get("body") or {}).get("texts") or [])
    return (tr.get("text") or "", norms[0] if norms else "")


def identify(scan: Dict, ledger: Dict, cfg: Dict,
             landing: int = 0) -> Dict:
    """Fold one landing's posts into the ledger (in place, returned).

    Posts WITH a doorplate create/update entries (status pending / ad /
    own); head-cut posts attach to a matching entry (time row gained on
    a later landing) or land in sightings as 身份待定."""
    for post in scan.get("posts") or []:
        author = post.get("author")
        time_text = (post.get("time_row") or {}).get("text")
        body = post.get("body") or {}
        norms = _long_norms(body.get("texts") or [])
        entry = _match_entry(post, ledger)
        # 判决分叉修（2026-11-17 DS 三连回复定案）：本落地的身份判决只
        # 在 identify 发生一次，盖在帖子上；decide/_actionable/act 记账
        # 全部查章（_resolved_entry），不再各自重算——账本状态在落地中途
        # 被谁动了，判决也不会翻脸（DS 事故：identify 并账刷 seen_at 后
        # decide 重算翻成 None → acted 闸被绕过）。
        post["_matched"] = entry
        if entry is None:
            if not author:
                # head-cut and unclaimed — recorded, never carded
                ledger["sightings"].append({
                    "body_head": norms[0] if norms else None,
                    "anchors": norms[:MAX_ANCHORS],
                    "landing": landing,
                })
                continue
            own = {normalize_for_match(n)
                   for n in (cfg.get("own_names") or ())}
            status = ("own" if normalize_for_match(author) in own
                      else ("skipped_ad" if post.get("sponsored_arrow")
                            else "pending"))
            entry = {
                "key": _entry_key(author, time_text),
                "author": author,
                "time_text": time_text,
                "body_head": norms[0] if norms else None,
                "anchors": norms[:MAX_ANCHORS],
                "status": status,
                "landings": [landing],
                "seen_at": _time.time(),
                # 发布窗上沿，创建即冻结、永不回写（A9/abs 合并门的承重
                # 不变量——见 _match_entry）。floor 带结构下首见标签最精，
                # 冻结首见即最优估值。
                "abs_time": _abs_time(time_text, _time.time()),
            }
            ledger["posts"].append(entry)
            _arm_tail(entry, post)
            post["_matched"] = entry   # 新建条目入章（上面的章盖在新建
                                       # 之前，还是 None——判决以最终
                                       # created entry 为准）
            continue
        # known post: refresh identity data, keep the stable key
        entry["landings"].append(landing)
        entry["seen_at"] = _time.time()
        if time_text:
            if not entry.get("time_text"):
                entry["time_text"] = time_text
                entry["key"] = _entry_key(entry["author"], time_text)
            elif entry["time_text"] != time_text:
                # 标签是观察值不是身份（身份=冻结 key）：每次重见刷成当前
                # 标签。floor 制下同一帖的标签只增不减——冻结首见会让第四
                # 尺时间行门把"同一帖标签自然走到"拒成 no-doorplate
                # （2026-11-17 15:22 真机：作者甲帖|2h 账上 2h、L13 屏上 3h、
                # L14 尾屏碎片 3h → 门拒绝 → 冰激凌回复二轮滞留）。key 保
                # 持首见形态不动（B4 稳定身份）；abs_time 仍冻结不回写。
                entry["time_text"] = time_text
        # abs_time 补种（迁移路）：旧账/晚得标签的 entry 缺冻结发布窗时
        # 用本次见到时刻补种一次，之后同样永不回写。
        if entry.get("abs_time") is None:
            entry["abs_time"] = _abs_time(time_text, _time.time())
        # 挂账先行于折叠（次序即正确性）：本落地若跨越底缘，得先挂上
        # tail_pending，折叠门才放行本落地的行——展开后 post-act 屏行
        # 必须进锚，尾屏第四尺认领全靠它们（首落地未跨缘、展开后才跨
        # 缘的 tall post 链条，Vicky.L 真机链）。
        _arm_tail(entry, post)
        # 锚折叠门（2026-11-17 深夜）：无门牌片段的行只给 armed entry
        # 折叠（尾屏认领靠它）。未挂账 entry 折叠无门牌片段 = 绑定窄门
        # 的自证循环：模糊认领进锚、锚再"精确"喂绑定，片段的花字行洗
        # 白成身份证据（L18 同族身份漂移）。门牌帖照折（门牌即身份证据）。
        if entry.get("tail_pending") or author:
            cap = (MAX_ANCHORS_ARMED if entry.get("tail_pending")
                   else MAX_ANCHORS)
            for n in norms:
                if n not in entry["anchors"] and len(entry["anchors"]) < cap:
                    entry["anchors"].append(n)
    return ledger


def _actionable(post: Dict, ledger: Dict, cfg: Dict,
                act_max_hours: float) -> Optional[str]:
    """Why a post is NOT actionable right now (None = act on it). All
    predicates come from schema slots / ledger entries (宪法 #1) — the
    caller invents nothing. Acted posts never re-act; posts whose time
    row hasn't been revealed by the pure-↓ walk stay honestly pending.

    A head-cut tail CAN act when its entry carries an ARMED tail_pending:
    a tall post's expansion pushes its •• below the fold, so its reply
    must happen at the tail landing — where the doorplate is already
    gone, but the identity was doorplate-proven on the earlier landing
    and the (pending) reply was composed from the FULL body back then.
    The mere three-ruler fuzzy match is NOT identity:
    用户令（2026-11-17，L18 冤案定案）——22:05 轮把"作者己帖尾+作者甲
    卡顶"的无门牌地屏坨模糊匹配成作者甲，坨里作者己的正文喂了组稿，
    上贴的回复回到了两帖（其中一条被用户删除）。模糊认领一律 no-
    doorplate，诚实跳过。

    唯一窄门（2026-11-17 深夜，三漏定案第三修的延伸）：未挂账 entry 的
    无门牌片段可凭「精确锚绑定」放行——片段长行逐行精确命中该 entry 的
    anchors 且证据不指向第二条 entry（_bind_headless）。这堵的是真机
    00:31 轮的另一半漏：作者甲|2h 的门牌落在合法整屏跳的跨越区里永不再
    露面，文字片段落地时旧门只会 no-doorplate 跳过 → 帖子 24h 窗口内
    永远没回。组稿正文由 hook 取 entry 的 anchors（门牌落地的全量行），
    不取屏上坨里的半截行。"""
    entry = _resolved_entry(post, ledger)
    author = post.get("author")
    if not author or not post.get("doorplate_visible"):
        if not (entry is not None and entry.get("author")
                and entry.get("tail_pending")):
            # D2 主门：armed 尾屏送达路（原样）；其余只走精确锚窄门，
            # 且绑定结果必须就是模糊尺认出的同一条（不相符 = 混块）
            be = _bind_headless(post, ledger)
            if be is None or (entry is not None and be is not entry):
                return "no-doorplate"
            entry = be
        author = entry["author"]
    own = {normalize_for_match(n)
           for n in (cfg.get("own_names") or ())}
    if (normalize_for_match(author) in own
            or (entry is not None and entry.get("status") == "own")):
        return "own"
    if (post.get("sponsored_arrow")
            or (entry is not None and entry.get("status") == "skipped_ad")):
        return "ad"
    tr = post.get("time_row")
    if tr is None:
        return "time-not-revealed"
    if post.get("button_x") is None or post.get("button_y") is None:
        return "button-missing"
    if entry is not None and entry.get("acted"):
        return "acted"
    hrs = parse_time_hours(tr.get("text") or "")
    if hrs is None:
        return "time-unparseable"
    if hrs >= act_max_hours:
        return "too-old"
    return None


# 处置结果账（2026-11-16 深夜用户令：失败要重试，不跳过）：只有
# 已发出（ok / sent-unverified——点击已发生、框已收起，按"宁少回不
# 重回"封账防双回复）或已决定不回（skipped-too-short：读不懂是决定
# 不是失败，重试只会原地再 skip）才标 acted。硬失败（stale-screen /
# stale-after-expand / no-comment-popup / no-input-box / paste-failed /
# send-vanished / send-stuck / gen-failed / act-error /
# expanded-no-button）一律不封账——下落地/下轮重新处置；deferred
# （expanded-tail-deferred）本就不封，等尾屏送达。
ACT_SENT_OR_DECIDED = frozenset({
    "ok", "sent-unverified", "skipped-too-short"})


def decide(scan: Optional[Dict], ledger: Dict,
           cfg: Dict) -> Dict:
    """One landing → the next action (v1: walk / boundary / retry).

    Order of checks: retry (scan failed) → BOUNDARY (out of today's
    working set — yesterday / N days ago / 前天 / 星期名 / 绝对日期，
    中英文，用户 2026-11-15 令；ends the WHOLE walk). Boundary
    evidences: the current post's own time row; the PAGE-BOTTOM closer
    slot (since 2026-11-16 深夜, user's landing schema: a ↓ flip always
    moves and always moves down, and the page bottom ALWAYS closes at a
    post's time row (left, same row as the ••• button) or at the end of
    its comments — a commented post takes two landings (bottom=time row,
    then bottom=comments end) and on BOTH the last time-row candidate on
    screen is that post's closer; read straight from OCR without region
    attribution, so a head-cut post's closer still fills the slot);
    and the matched ledger entry's recorded time (矮窗口+图多帖长时一屏
    只见帖头不见时间行) → walk (annotated with why: the
    current post may be pending-new, already known, an ad, own, or
    head-cut 身份待定 — all of them just keep the pure-↓ walk going
    in v1)."""
    if scan is None:
        return {"action": "retry", "reason": "scan-failed"}
    idx = (scan.get("viewport") or {}).get("current_post")
    cur = (scan.get("posts") or [])[idx] if idx is not None else None
    entry = _resolved_entry(cur, ledger)
    if cur is not None:
        tr = cur.get("time_row") or {}
        if is_boundary_time(tr.get("text")):
            return {"action": "boundary", "reason": "time-out-of-today",
                    "post": idx, "time_text": tr.get("text")}
    # 屏底收束槽（2026-11-16 深夜，用户落屏 schema，见 docstring）：槽里
    # 是 boundary 时间 = 边界已到屏上 → 停。单槽单判定，不做全屏散点扫。
    pb = scan.get("page_bottom_closer") or {}
    if pb.get("text") and is_boundary_time(pb["text"]):
        return {"action": "boundary", "reason": "page-bottom-out-of-today",
                "post": idx, "time_text": pb["text"]}
    if cur is not None:
        # 帐本边界（2026-11-16 晚，48 屏盲走事故）：矮窗口+图多帖长时
        # 一屏只见帖头不见时间行，屏幕时间行边界失明，一直盲走到 feed 底。
        # 已见帖的帐本时间同为铁证：匹配到 yesterday 及更前的 entry 即停。
        if entry is not None and is_boundary_time(entry.get("time_text") or ""):
            return {"action": "boundary", "reason": "known-out-of-today",
                    "post": idx, "time_text": entry.get("time_text")}
    why = "no-doorplate"
    if cur is not None:
        own = {normalize_for_match(n)
               for n in (cfg.get("own_names") or ())}
        if cur.get("author") is None:
            why = "head-cut-身份待定"
        elif normalize_for_match(cur["author"]) in own:
            why = "own"
        elif cur.get("sponsored_arrow"):
            why = "ad-skipped"
        else:
            why = "known" if entry is not None else "pending-marked"
    return {"action": "walk", "reason": why, "post": idx}


# ---------------------------------------------------------------------------
# the walk loop (step 2): every IO channel is injectable — offline dry-runs
# pass scripted screens; the live launcher (step 3, scripts/) binds real
# capture/press/scan. The loop itself contains NO post knowledge: identify
# folds the landing into the ledger, decide picks the action, the loop
# handles rhythm (press ↓, settle, self-check, safety stops).
# ---------------------------------------------------------------------------

WALKER_LOG_PATH = "/tmp/walker.log"
LEDGER_PATH = "/tmp/walker_ledger.json"
SETTLE_SECONDS = 1.4   # post-press settle (probe-validated rhythm)


def default_log(line: str) -> None:
    with open(WALKER_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_ledger(path: str) -> Dict:
    try:
        with open(path, encoding="utf-8") as f:
            return _migrate_abs(json.load(f))
    except (OSError, ValueError):
        return empty_ledger()


def _migrate_abs(ledger: Dict) -> Dict:
    """abs_time 一次性补种（2026-11-17 A9 落地）：旧账条目没有冻结发布
    窗，用 seen_at − 标签补种（与创建同一公式、同一误差界）。必须在
    首次重见**之前**做——漂移门在匹配时就要读 abs_time，等到 identify
    刷新才补种，旧 acted 条目在这轮重见里已经丢了漂移保护（重复回复
    风险）。缺 seen_at/time_text 或日历带标签的条目留空，abs 门对其
    惰性（其余尺照常）。只填 None，绝不回写——补种本身也是冻结值。"""
    for e in ledger.get("posts") or []:
        seen = e.get("seen_at")
        if e.get("abs_time") is None and seen:
            a = _abs_time(e.get("time_text"), seen)
            if a is not None:
                e["abs_time"] = a
    return ledger


def save_ledger(ledger: Dict, path: str) -> None:
    # 原子写（2026-11-25，.corrupt-1711 残骸定案）：直写 open(w) 在进程
    # 被杀/写到一半崩溃时留下截断 JSON，而 load_ledger 对坏 JSON 静默
    # 回空账——已回帖的 acted 痕迹全部清零，下一轮 = 对同一批人重复
    # 发评论。tmp + os.replace 同目录原子改名，截断类损伤从此绝根；
    # tmp 用隐藏名 + pid 后缀（单轮锁下双写者只是理论风险），崩溃残留
    # 的 tmp 不伤读侧（load 只认正名）。
    d = os.path.dirname(path) or "."
    tmp = os.path.join(d, f".{os.path.basename(path)}.tmp-{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _fuzzy_overlap(a_norms: List[str], b_norms: List[str],
                   bar: float = BODY_FUZZY) -> int:
    hits = 0
    for na in a_norms:
        if any(SequenceMatcher(None, na, nb).ratio() >= bar for nb in b_norms):
            hits += 1
    return hits


def walk(*, press_down: Callable[[], None],
         press_up: Optional[Callable[[], None]] = None,
         capture: Callable[[], str],
         scan_fn: Callable[[str], Optional[Dict]],
         cfg: Optional[Dict] = None,
         ledger_path: Optional[str] = None,
         log: Optional[Callable[[str], None]] = None,
         sleep: Optional[Callable[[float], None]] = None,
         max_landings: int = 50,
         max_flies: int = 3,
         max_dead: int = 2,
         max_retries: int = 2,
         act_fn: Optional[Callable[[Dict], Dict]] = None,
         act_max_hours: float = 24.0,
         max_act_fails: int = 2,
         only_authors: Optional[List[str]] = None,
         emit: Optional[Callable[[Dict], None]] = None) -> Dict:
    """The pure-↓ walk loop (v1 read-only; act_fn adds the v2 处置 hook).

    Per landing: capture → scan → self-check (dead press / fly /
    sub-screen step via anchor overlap between consecutive landings —
    the 连续图片 vocabulary) → identify (ledger identity matching) →
    decide → ACT (when act_fn is given: the FIRST post whose
    _actionable verdict is None gets act_fn(post) — expand full text +
    comment entry live in the hook, IO belongs to the runner) → press ↓.
    One action per landing; acted posts never re-act (ledger marks them).
    Name gates (2026-11-25): the persistent skip list (cfg["skip_authors"])
    never acts on its authors; only_authors acts ONLY on its authors this
    round — both gates skip with a log line and leave the post pending
    (never 封账), so a later list-free act round still reaches it.
    After acting the motion self-check re-arms (the screen legitimately
    changed under us — expansion grows the post), and consecutive act
    failures stop the walk (act-failed; `soft` results — honest
    unreachables like expanded-no-button — are recorded but never
    counted). Safety stops: BOUNDARY (time row out of today —
    yesterday / N days ago / 星期名 / 绝对日期, decide), stuck (dead
    presses), runaway (consecutive blind flies — legal anchored jumps
    don't count, 2026-11-16), scan-failed (consecutive scan failures),
    act-failed, max-landings. Returns a summary dict; per-landing
    details land in `events` (tests) and the human log."""
    cfg = cfg or {}
    log = log or default_log
    sleep = sleep or _time.sleep
    emit = emit or (lambda ev: None)   # 结构化事件流（脸/代理用；不传=无操作）
    # 名单闸预归一（2026-11-25 用户拍板，宿主「帐本手术」事件的正门
    # 替代）：① 持久跳过名单 cfg["skip_authors"]（settings 存，默认空
    # =全回）——名单内作者永不回；② 本轮正向挑选 only_authors（工具
    # 参数，仅本轮生效）——传了就只回名单内作者。两道闸都只拦「这轮
    # 不回」，**不封账**：帖留 pending，之后任一轮不带名单的 act 照常
    # 可达（这正是帐本手术被封账的恶果的反面）。
    only_norms = {normalize_for_match(n) for n in (only_authors or [])
                  if str(n).strip()} - {""}
    skip_norms = {normalize_for_match(n) for n in (cfg.get("skip_authors")
                                                   or [])
                  if str(n).strip()} - {""}
    ledger = load_ledger(ledger_path) if ledger_path else empty_ledger()
    # 挂账军团治理（2026-11-17 AuthorA 三轮滞留定案，B4 落地）：tail_pending
    # 是轮内瞬态——上一轮的挂账跨轮存活只产生军团（真机账 13 条：广告/
    # 服务号帖永不会 acted→永不摘账），还与真挂账同标签撞车（安徽服务
    # 某服务号|4h vs 作者甲帖|2h 同为 "4 hour(s) ago"，第四尺歧义拒认 → 一条回复
    # 三轮滞留）。轮起手全清；本轮路过跨越/展开挂账的条目由
    # _arm_tail 自然重挂，投递链不受影响（重入永远从顶走，任何重见都
    # 先经过门牌/跨越落地）。
    for e in ledger["posts"]:
        e.pop("tail_pending", None)
    events: List[Dict] = []
    presses = flies = dead_streak = retries = act_fails = 0
    landing = 0
    prev_anchors: Optional[List[str]] = None
    prev_bottom: Optional[float] = None
    stop_reason: Optional[str] = None
    climbed: set = set()     # 本轮已回攀过的块键——每块每轮一次机会
    climb_streak = 0         # 连续回攀计数（跨落地，见门牌/放弃时归零）
    climb_prev: Optional[Tuple[List[str], float]] = None  # 上一攀落地的
    # (anchors, bottom)——两次攀落地画面全同 = ↑ 无位移（Q4: feed 顶）

    def finish(reason: str) -> None:
        nonlocal stop_reason
        stop_reason = reason
        log(f"[walker] 停止: {reason} (landings={landing} presses={presses})")
        emit({"t": "stop", "reason": reason, "landings": landing,
              "presses": presses})

    while stop_reason is None and landing < max_landings:
        path = capture()
        scan = scan_fn(path)
        if scan is None:
            retries += 1
            log(f"[walker] scan 失败 ({retries}/{max_retries})，原位重试")
            if retries >= max_retries:
                finish("scan-failed")
            continue
        retries = 0
        landing += 1
        vp = scan["viewport"]
        anchors = [a["norm"] for a in vp["anchors"]]
        bottom = vp["post_bottom_y"]

        overlap = None
        if prev_anchors is not None:
            if anchors == prev_anchors and bottom == prev_bottom:
                dead_streak += 1
                overlap = 0
                log(f"[walker] landing {landing}: 死脚 (画面未动 "
                    f"{dead_streak}/{max_dead})")
            else:
                dead_streak = 0
                overlap = _fuzzy_overlap(prev_anchors, anchors)
                if overlap == 0:
                    aligned = bool(scan["posts"]) and (
                        vp["top"]["zone"] != "none"
                        or any(p["doorplate_visible"] for p in scan["posts"]))
                    if aligned:
                        # 合法锚定跳：底缘对齐，跨越者的尾巴正落在这一屏
                        # 顶沿——有据可跨，不漏内容。不计入 runaway 嫌疑
                        # （2026-11-16 两轮真机：深区长帖区合法跳连串把
                        # 刹车误触发，停在 ~20h；改由边界停+上限收场）。
                        log(f"[walker] landing {landing}: 飞 (锚重叠0, "
                            "对齐块底=合法锚定跳，不计嫌疑)")
                    else:
                        flies += 1
                        log(f"[walker] landing {landing}: 飞 (锚重叠0, "
                            f"盲飞，{flies}/{max_flies})")
                        # 盲飞=尾巴真的被跳过了：挂账清掉，下一屏顶的陌生
                        # 尾巴不许冒领。对齐块底的合法锚定跳则相反——底缘
                        # 跨越者的尾巴正落在这一屏顶沿，挂账保留给认领尺
                        for e in ledger["posts"]:
                            e.pop("tail_pending", None)
                else:
                    flies = 0
                    log(f"[walker] landing {landing}: 亚屏步 (锚重叠{overlap})"
                        if overlap else
                        f"[walker] landing {landing}: 亚屏步")

        identify(scan, ledger, cfg, landing)
        if ledger_path:
            save_ledger(ledger, ledger_path)
        d = decide(scan, ledger, cfg)

        cur = ((scan["posts"] or [])[vp["current_post"]]
               if vp["current_post"] is not None else None)
        log(f"[walker] landing {landing}: {d['action']}/{d['reason']} "
            f"posts={len(scan['posts'])} "
            f"cur_author={cur['author'] if cur else None}")
        events.append({"landing": landing, "action": d["action"],
                       "reason": d["reason"], "overlap": overlap,
                       "posts": len(scan["posts"]),
                       "cur_author": cur["author"] if cur else None})
        emit({"t": "landing", "n": landing, "action": d["action"],
              "reason": d["reason"], "overlap": overlap,
              "posts": len(scan["posts"]),
              "cur_author": cur["author"] if cur else None})

        if d["action"] == "boundary":
            if presses == 1 and overlap == 0:
                # 首按即边界 + 锚重叠0 = 需要人看帧的异常签名（2026-11-17
                # 晨两轮一按直落昨天区——但用户自述当时在 multi-tasking，
                # 两轮证据被人为滚动污染：成因可能是微信「上次读到处」
                # 回位，也可能就是人手，无法带内区分）。守卫只做诚实
                # 标注：顶屏在窗内（landing 1 没停）却一按到 Yesterday，
                # 值得看帧再定，不当真过界收场。真过界是亚屏步渐入
                # （overlap>0）或 landing 1 顶即出界（presses=0），
                # 各自照旧 plain boundary。
                finish("teleport-suspect (1st-press boundary)")
            else:
                finish(f"boundary ({d.get('time_text')})")
            break
        if dead_streak >= max_dead:
            finish("stuck")
            break
        if flies >= max_flies:
            finish("runaway")
            break

        # ---- the act phase: 处置 EVERY post the schema says is
        # actionable on this landing (the hook re-scans fresh before each
        # chain, so the walker's stale snapshot only decides WHO; the
        # ledger's acted marks gate each pass). One action per landing was
        # the v2 shape — live 2026-11-15 it stranded same-screen posts
        # (AuthorK lost her landing to Vicky.L and the next ↓ flew past).
        # A deferred result ends the landing's acting: the deferral needs
        # ↓ motion to reach the tail, and not marking acted means the
        # same post would re-fire forever on this screen.
        acted = False
        tried = set()   # 本落地已处置过的帖（下标）——同屏一键一试：
                        # 失败不封账但也不原地重试（屏没变，重试只会
                        # 原样再败）；下落地/下轮照常重试（用户令）。
        if act_fn is not None:
            while stop_reason is None:
                acted_one = False
                deferred = False
                for pi, p in enumerate(scan["posts"]):
                    if pi in tried:
                        continue
                    why = _actionable(p, ledger, cfg, act_max_hours)
                    if why is not None:
                        e = _resolved_entry(p, ledger)
                        if e is None and not p.get("author") \
                                and not p.get("doorplate_visible"):
                            # 精确锚绑定的无门牌片段：留痕用帐本名，不冒 "?"
                            e = _bind_headless(p, ledger)
                        who = (p.get("author")
                               or (e or {}).get("author") or "?")
                        if why == "acted":
                            log(f"[walker] landing {landing}: "
                                f"跳过已回过 {who!r}")
                        else:
                            # 跳过原因留痕（2026-11-16 用户问"有些帖子
                            # 漏掉了什么原因"——静默跳过无法对账）
                            log(f"[walker] landing {landing}: "
                                f"跳过 {who!r} 原因={why}")
                        continue
                    e0 = _resolved_entry(p, ledger)
                    if e0 is None and not p.get("author") \
                            and not p.get("doorplate_visible"):
                        # 精确锚绑定认领的片段（_actionable 已放行）：
                        # entry_author/entry_body 随 target 交接给 hook，
                        # 组稿正文取帐本全量行而非屏上坨里的半截行
                        e0 = _bind_headless(p, ledger)
                    who = (p.get("author") or (e0 or {}).get("author")
                           or "?")
                    # 名单闸（先于处置）：跳过要留痕且 tried 记账（同屏
                    # 一键一试），绝不 acted 封账——名单外/跳过名单内的
                    # 帖保持待回状态。
                    who_norm = normalize_for_match(who)
                    if who_norm and who_norm in skip_norms:
                        tried.add(pi)
                        log(f"[walker] landing {landing}: 跳过 {who!r} "
                            "原因=skip-list（持久跳过名单）")
                        continue
                    if only_norms and who_norm not in only_norms:
                        tried.add(pi)
                        log(f"[walker] landing {landing}: 跳过 {who!r} "
                            "原因=not-on-list（本轮 only_authors 挑选）")
                        continue
                    log(f"[walker] landing {landing}: 处置 {who!r} "
                        f"时间={p['time_row']['text']!r}")
                    target = dict(p)
                    if e0 is not None and not p.get("author") \
                            and e0.get("author"):
                        # 照片尾第四尺认领：屏上没有门牌，把帐本身份和
                        # 门牌落地存的正文行（anchors）交给 hook
                        target["entry_author"] = e0["author"]
                        target["entry_body"] = e0.get("anchors") or []
                    if e0 is not None and \
                            (e0.get("act_result") or {}).get("reply"):
                        # a deferred reply from an earlier landing or
                        # process — the tail landing delivers it (walker
                        # owns the ledger, the hook owns the IO; live
                        # 2026-11-15 Vicky.L: a 4000-char post's tail lies
                        # beyond a short pass, the generated reply must
                        # survive the process boundary)
                        target["pending_reply"] = e0["act_result"]["reply"]
                        target["pending_full_body"] = \
                            e0["act_result"].get("full_body") or []
                    tried.add(pi)
                    try:
                        result = act_fn(target) or {}
                    except Exception as e:  # the hook must not kill the walk
                        result = {"status": "act-error", "error": repr(e)}
                    # 裸断修（2026-11-17 DS 三连回复定案）：回复已发出，
                    # 记账不许再赌一次匹配——identify 盖的章（或发帖前
                    # 交给 hook 的 e0）就是记帐对象。两者皆无只可能发生在
                    # 根本没发的路上；真发生时先 emit 喊出来再停，绝不
                    # 静默失忆（旧裸断：发帖后重匹配 None → break，
                    # act_result/acted/日志全没有 → 下轮再回 = 三连回复）。
                    entry = p.get("_matched") or e0
                    if entry is None:
                        emit({"t": "action-unaccounted", "author": who,
                              "result": result.get("status")})
                        log(f"[walker] landing {landing}: 发帖后无账可记 "
                            f"{who!r} result={result.get('status')}")
                        break
                    entry["act_result"] = result
                    emit({"t": "action", "author": who,
                          "result": result.get("status"),
                          "deferred": bool(result.get("deferred")),
                          "error": (str(result.get("error"))
                                    [:80] if result.get("error") else None),
                          "reply": (result.get("reply") or "")[:60] or None})
                    if result.get("status") in ACT_SENT_OR_DECIDED:
                        # 已发出/已决定不回才封账（ACT_SENT_OR_DECIDED
                        # 注释）；失败与 deferred 留给下落地/下轮重试
                        entry["acted"] = True
                    acted = True
                    acted_one = True
                    if result.get("status") != "ok":
                        if result.get("soft"):
                            # an honest unreachable/deferral (expanded-no-
                            # button / expanded-tail-deferred) — recorded,
                            # never counted as a chain failure (live
                            # 2026-11-15: two tall posts in a row must not
                            # kill the walk)
                            log(f"[walker] 处置搁置 (soft): "
                                f"{result.get('status')}")
                        else:
                            act_fails += 1
                            log(f"[walker] 处置失败 "
                                f"({act_fails}/{max_act_fails}): "
                                f"{result.get('status')}")
                            if act_fails >= max_act_fails:
                                finish("act-failed")
                    else:
                        act_fails = 0
                    if result.get("deferred") or stop_reason is not None:
                        deferred = True
                        break
                if not acted_one or deferred or stop_reason is not None:
                    break
        if acted:
            # the screen changed under the act (expansion grows the post,
            # popups came and went) — fold the NEW rows into identity so a
            # tall post's tail landing is still claimable: its rows
            # overlap the POST-ACT screen, not the doorplate landing's
            # preview; without this the tail is an unclaimed sighting
            path2 = capture()
            scan2 = scan_fn(path2)
            if scan2 is not None:
                identify(scan2, ledger, cfg, landing)
        # one save per landing, AFTER the act phase and BEFORE any stop
        # break — acted/act_result must reach disk even when this landing
        # ends the walk (live 2026-11-15: an act-failed stop lost the last
        # landing's 处置 mark; boundary stops lost identify updates)
        if ledger_path:
            save_ledger(ledger, ledger_path)
        if stop_reason is not None:
            break

        # ---- 屏尾按键决定：门牌回攀（A2/A3，2026-11-17 用户授权的
        # 宪法修正案——纯↓的唯一例外）或常规 ↓。回攀中每按必重扫：
        # 见门牌即停（下一落地正常判读，↓ 恢复后智能滚动按当前位置
        # 重对齐，屏底不变量自愈）；CLIMB_MAX_PRESS 按仍无门牌 → 放弃
        # 此块（climbed 记账）↓ 离开。↑ 的屏幕不适用自检词汇
        # （屏底≠时间条），prev 基线重置。
        up = False
        if press_up is not None:
            if climb_streak > 0:
                # 见门牌即停（2026-11-17 climb 轮 live L17-19 教训）：回攀
                # 落地里门牌帖常不是 cur（cur 可指向顶部碎片）——看**屏上
                # 任一**帖的门牌，别只看 cur，否则白烧按键攀到限额
                if any(p.get("author") or p.get("doorplate_visible")
                       for p in scan["posts"]):
                    climb_streak = 0
                    climb_prev = None
                    log(f"[walker] landing {landing}: 回攀见门牌，恢复 ↓")
                elif climb_prev is not None and anchors == climb_prev[0] \
                        and bottom == climb_prev[1]:
                    # Q4（用户令 2026-11-17）：↑ 无位移 = feed 顶（或键无
                    # 响应）→ 立即放弃此块，不烧满限额。带内判据的盲区：
                    # 纯照片墙攀爬时文字锚不变会误判成无位移——代价只是
                    # 提前放弃（诚实跳过），不是误动作
                    climb_streak = 0
                    climb_prev = None
                    log(f"[walker] landing {landing}: 回攀无位移（feed 顶/"
                        "无响应），放弃此块")
                elif climb_streak < CLIMB_MAX_PRESS:
                    up = True   # 门牌未现：同块续攀（此屏通常无时间条，
                                # _climb_target 判定不可用，按 streak 续攀；
                                # Q2 用户裁决：超长帖微信会折叠，一般一按
                                # 即见头，3 按只是安全限额）
                else:
                    climb_streak = 0
                    climb_prev = None
                    log(f"[walker] landing {landing}: 回攀到限仍无门牌，"
                        "放弃此块")
            if not up and climb_streak == 0:
                tk = _climb_target(cur, ledger) if cur is not None else None
                if tk is not None:
                    if tk in climbed:
                        tk = None   # 每块每轮只给一次回攀机会
                    else:
                        up = True
                        climbed.add(tk)
                        log(f"[walker] landing {landing}: 门牌回攀锁定 "
                            f"(时间条={tk[0]!r})")
        if up:
            climb_streak += 1
            climb_prev = (anchors, bottom)   # 供 Q4 无位移判据比对
            press_up()
            emit({"t": "climb", "n": landing, "streak": climb_streak})
        else:
            press_down()
        presses += 1
        sleep(SETTLE_SECONDS)
        if acted or up:
            # the screen legitimately changed under us (expansion grows
            # the post, entry popups came and went, ↑ breaks the
            # bottom-alignment vocabulary) — re-arm the motion self-check
            # instead of trusting the pre-act anchors
            prev_anchors, prev_bottom = None, None
        else:
            prev_anchors, prev_bottom = anchors, bottom

    if stop_reason is None:
        finish("max-landings")
    return {"stop_reason": stop_reason, "landings": landing,
            "presses": presses, "ledger": ledger, "events": events}
