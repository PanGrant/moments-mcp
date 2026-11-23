"""自主批阅朋友圈 (auto-review): persona-driven appreciative replies.

User directive (2026-11-08): given a persona (default 「情绪价值输出机器人」,
user-selectable later), walk the digest items one by one and produce an
appreciative/agreeable comment for each — EXCEPT service/sponsored accounts
(the dropdown-arrow posts: TestBuddy / 糖K经理 / …), which are skipped via
the sponsored flag set at ingest by the arrow probe. Every reply is signed
and stored as a moments_comment draft (status='draft'); nothing is sent —
confirmation happens through the existing draft flow (edit → send).

Search is a pluggable slot: callers may pass search_fn(query) -> [snippets].
Free no-key channels were verified dead for Chinese (2026-11-08: GLM
web_search 429 / no-op, DDG garbage, Bing bot-wall, Baidu captcha), so v1
ships with search off. The prompt contract is 原文为主: search results that
don't clearly relate must never be cited.
"""
import os
from typing import Any, Callable, Dict, List, Optional

from . import db
from .config import load_env_file

DEFAULT_PERSONA = {
    # 2026-11-15 user: 情绪价值机器人 persona is retired — the replier is
    # just 「AI助手」, and the signature itself carries the full disclosure
    # (AI auto-reply, misreading, ignore it).
    "name": "AI助手",
    "signature": "（AI助手胡乱解读自动回复，内容请忽略）",
}
# Persona (later user-selectable) and signature override the defaults via env.
PERSONA_NAME_ENV = "DIGEST_REVIEW_PERSONA"
SIGNATURE_ENV = "DIGEST_REVIEW_SIGNATURE"

MAX_TEXT_CHARS = 4000  # same truncation budget as summarize

# 帖文太短不硬回（用户 2026-11-15 令）：正文可见文字低于此字数直接跳过，
# 不调用 LLM；再往上交由模型按 prompt 的 SKIP 规则自行判断
MIN_UNDERSTANDABLE_CHARS = 6


class TooShortToReply(Exception):
    """正文不足以理解帖子内容——按用户规则不硬回，walker 跳过该帖。"""


def body_understandable(text: Optional[str]) -> bool:
    """Deterministic floor under the LLM's SKIP judgment: near-empty
    visible text (photo-only posts, OCR debris) can never ground a reply."""
    import re
    return len(re.sub(r"\s+", "", text or "")) >= MIN_UNDERSTANDABLE_CHARS


CHANNEL = "moments_comment"

PROMPT_TEMPLATE = """你是「{persona_name}」，替用户在朋友圈里逐条批阅好友动态。

【人设】
{persona_name}：替用户回应好友动态，口吻是一个成熟、真诚的成年人——像熟识的朋友刷到这条后随手写的一句评论，自然、有分寸、不夸张。

【任务】
读完动态写一条真实自然的评论，1-2 句话（只有一句也行）。写法不限：回应正文里某个具体细节、附和对方的心情或选择、好奇地问一句、顺带一句自己相关的经历——哪样最像真人就写哪样，不必每条都夸。

【语气基线】
- 成年人的克制：不夸张惊叹、不感叹号连用，不用「太棒啦」「绝绝子」一类浮夸网语，不用叠词卖萌
- 表情符号默认不用；整条至多 1 个，且只在确实自然时
- 不要空洞吹捧（没有具体内容的「太美了」「真棒」），不要客套套话，不要复述原文
- 开头措辞自然多样

【搜索参考】（可能为空；与动态无关或看不懂关联时绝对不要引用，只依据原文写）
{search_block}

【动态】
作者: {author}
正文: {text}
评论: {comments_block}

要求：
1. 评论基于原文，不编造原文和评论中没有的内容；原文里无法理解的乱码或残缺片段（识别噪音）直接忽略——绝不引用、绝不评价。
2. 先判断动态的情绪倾向再定语气：喜事/成就→夸到点上（指出具体之处）；中性事务性内容→自然表达关注；负面新闻→表达共情或关切，语气克制，禁止兴奋语气。
3. 若正文文字太少或残缺到无法理解这条动态在发什么（例如近乎纯图片、
   正文只有一两个词），不要硬写——只输出 SKIP 三个字母，不输出其他任何内容。
4. 只输出评论正文本身——不要署名（署名由系统统一添加）、不要任何解释、引号或 markdown 代码块标记。"""


def _resolve_persona(persona: Optional[Dict[str, str]]) -> Dict[str, str]:
    p = dict(DEFAULT_PERSONA)
    if persona:
        p.update({k: v for k, v in persona.items() if v})
    load_env_file()
    p["name"] = os.environ.get(PERSONA_NAME_ENV) or p["name"]
    p["signature"] = os.environ.get(SIGNATURE_ENV) or p["signature"]
    return p


def _comments_block(thread: List[Dict[str, Any]]) -> str:
    if not thread:
        return "（无可见评论）"
    return "\n".join(f"- {c.get('author', '?')}: {c.get('text', '')}"
                     for c in thread)


def _search_block(snippets: List[str]) -> str:
    if not snippets:
        return "（无）"
    return "\n".join(f"- {s}" for s in snippets)


def list_review_targets(conn, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Digest items still awaiting a persona comment: sponsored posts are
    skipped (dropdown-arrow rule), and any item that already has a
    moments_comment draft (draft or sent) is not re-reviewed."""
    sql = (
        "SELECT d.id, d.title, r.author, r.sponsored "
        "FROM digest_items d JOIN raw_items r ON r.id = d.raw_item_id "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM drafts t"
        "  WHERE t.digest_item_id = d.id AND t.channel = ?"
        ") AND (r.sponsored IS NULL OR r.sponsored = 0) "
        "ORDER BY d.id"
    )
    rows = conn.execute(sql, (CHANNEL,)).fetchall()
    out = [dict(r) for r in rows]
    return out[:limit] if limit else out


def generate_review(conn, digest_item_id: int,
                    persona: Optional[Dict[str, str]] = None,
                    search_fn: Optional[Callable[[str], List[str]]] = None,
                    model: Optional[str] = None, base_url: Optional[str] = None,
                    api_key: Optional[str] = None) -> Dict[str, Any]:
    """One persona comment draft for one digest item. Skips sponsored items
    and items that already carry a moments_comment draft (ValueError).
    search_fn (query -> snippets) is optional; without it the comment is
    written from the original text alone. Returns the stored draft row."""
    persona = _resolve_persona(persona)
    item = db.get_digest_item(conn, digest_item_id)
    if item is None:
        raise KeyError(f"digest item not found: {digest_item_id}")
    if item.get("sponsored"):
        raise ValueError(
            f"item {digest_item_id} is a service/sponsored account "
            f"(dropdown arrow) — skipped by the 自主批阅 rule")
    existing = [d for d in db.list_drafts(conn, digest_item_id)
                if d["channel"] == CHANNEL]
    if existing:
        raise ValueError(
            f"item {digest_item_id} already has a {CHANNEL} draft "
            f"(#{existing[0]['id']})")

    text = item.get("text", "") or ""
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]

    snippets: List[str] = []
    if search_fn is not None:
        query = " ".join(x for x in (item.get("title"), text[:60]) if x)
        try:
            snippets = [s for s in (search_fn(query) or []) if s][:5]
        except Exception:
            snippets = []  # search is enrichment; never block the reply

    load_env_file()
    from openai import OpenAI

    model = (model or os.environ.get("DIGEST_SUMMARIZE_MODEL")
             or os.environ.get("LLM_MODEL", "glm-4.6"))
    base_url = base_url or os.environ.get(
        "LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/")
    api_key = api_key or os.environ.get("LLM_API_KEY", "")

    prompt = PROMPT_TEMPLATE.format(
        persona_name=persona["name"],
        search_block=_search_block(snippets),
        author=item.get("author") or "（未知）",
        text=text,
        comments_block=_comments_block(item.get("thread") or []),
    )
    client = OpenAI(base_url=base_url, api_key=api_key, timeout=120)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.4,
    )
    content = (resp.choices[0].message.content if resp.choices else "") or ""
    content = content.strip().strip("`").strip()
    # the signature is appended deterministically — a free model prompted
    # not to sign may still drift, so never double-sign
    signature = persona["signature"]
    if signature and signature not in content:
        content = f"{content} {signature}".strip()

    draft_id = db.insert_draft(conn, digest_item_id, CHANNEL, content)
    return db.get_draft(conn, draft_id)


def run_review(conn, persona: Optional[Dict[str, str]] = None,
               search_fn: Optional[Callable[[str], List[str]]] = None,
               limit: Optional[int] = None,
               model: Optional[str] = None, base_url: Optional[str] = None,
               api_key: Optional[str] = None) -> Dict[str, Any]:
    """Batch: review every eligible item. Per-item failures are recorded
    and never block the rest. Returns
    {reviewed: [{item_id, draft_id, author, content}], skipped_sponsored,
    errors}."""
    targets = list_review_targets(conn, limit=limit)
    # targets already exclude sponsored items — count them for the report
    skipped_sponsored = conn.execute(
        "SELECT COUNT(*) FROM digest_items d JOIN raw_items r "
        "ON r.id = d.raw_item_id "
        "WHERE r.sponsored = 1 AND NOT EXISTS ("
        "  SELECT 1 FROM drafts t"
        "  WHERE t.digest_item_id = d.id AND t.channel = ?"
        ")",
        (CHANNEL,),
    ).fetchone()[0]
    reviewed: List[Dict[str, Any]] = []
    errors: List[str] = []
    for t in targets:
        try:
            draft = generate_review(conn, t["id"], persona=persona,
                                    search_fn=search_fn, model=model,
                                    base_url=base_url, api_key=api_key)
        except Exception as e:
            errors.append(f"item {t['id']} ({t['author'] or '?'}): {e}")
            continue
        reviewed.append({
            "item_id": t["id"],
            "draft_id": draft["id"],
            "author": t["author"],
            "content": draft["content"],
        })
    return {
        "reviewed": reviewed,
        "skipped_sponsored": skipped_sponsored,
        "errors": errors,
    }
