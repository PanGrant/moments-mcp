"""Interest matching + summarization in one LLM call (DIGEST_DESIGN.md §4).

Input: the full enabled-interests definition plus one raw item; output a
validated {title, summary, matched_interest_ids, score}. The chat model
config is reused from .env (LLM_MODEL / LLM_BASE_URL / LLM_API_KEY).
"""
import os
from typing import Any, Dict, List, Optional

from .config import load_env_file
from .json_utils import parse_json_object

MAX_TEXT_CHARS = 4000  # longer bodies are truncated before the LLM call

PROMPT_TEMPLATE = """你是用户的信息订阅助理。下面给出用户的兴趣定义和一条动态，请完成：
1. 判断这条动态与哪些兴趣相关；
2. 用中文为这条动态起标题并写摘要。

【用户兴趣定义】（可能为空）
{interests_block}

【动态】
作者: {author}
正文: {text}
配图: {media_desc}
评论: {comments_block}

只输出一个 JSON 对象，不要任何其他文字、不要 markdown 代码块标记，格式：
{{"title": "不超过20字的标题", "summary": "不超过120字的中文摘要", "matched_interest_ids": [相关兴趣的数字id数组], "score": 相关度0到10的整数}}

要求：
1. matched_interest_ids 只能使用上面给出的 id；与任何兴趣都不相关时为空数组，score 为 0。
2. 摘要基于动态原文，不要编造原文和评论中没有的内容。
3. 评论内容可作为背景纳入摘要，注明是评论观点。"""


def _interests_block(interests: List[Dict[str, Any]]) -> str:
    if not interests:
        return "（用户尚未定义任何兴趣）"
    lines = []
    for i in interests:
        kws = "、".join(i.get("keywords") or [])
        line = f"- id={i['id']} 名称: {i['name']}"
        if kws:
            line += f"（关键词: {kws}）"
        if i.get("description"):
            line += f" — {i['description']}"
        lines.append(line)
    return "\n".join(lines)


def _comments_block(thread: List[Dict[str, Any]]) -> str:
    if not thread:
        return "（无可见评论）"
    return "\n".join(f"- {c.get('author', '?')}: {c.get('text', '')}" for c in thread)


def summarize_item(raw: Dict[str, Any], interests: List[Dict[str, Any]],
                   model: Optional[str] = None, base_url: Optional[str] = None,
                   api_key: Optional[str] = None) -> Dict[str, Any]:
    """Summarize one raw item against the interests. Returns
    {title, summary, matched_interest_ids, score} with matched ids filtered
    to known interests and score clamped to 0-10. Raises on LLM/parse failure
    (the pipeline treats that as a per-item failure, not a batch failure)."""
    load_env_file()
    from openai import OpenAI

    model = (model or os.environ.get("DIGEST_SUMMARIZE_MODEL")
             or os.environ.get("LLM_MODEL", "glm-4.6"))
    base_url = base_url or os.environ.get("LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/")
    api_key = api_key or os.environ.get("LLM_API_KEY", "")

    text = raw.get("text", "") or ""
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]

    prompt = PROMPT_TEMPLATE.format(
        interests_block=_interests_block(interests),
        author=raw.get("author", "") or "（未知）",
        text=text,
        media_desc=raw.get("media_desc", "") or "（无）",
        comments_block=_comments_block(raw.get("thread") or []),
    )

    client = OpenAI(base_url=base_url, api_key=api_key, timeout=120)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    content = resp.choices[0].message.content if resp.choices else None
    out = parse_json_object(content)

    known_ids = {i["id"] for i in interests}
    matched = []
    for v in out.get("matched_interest_ids") or []:
        try:
            vid = int(v)
        except (TypeError, ValueError):
            continue
        if vid in known_ids and vid not in matched:
            matched.append(vid)

    try:
        score = float(out.get("score") or 0)
    except (TypeError, ValueError):
        score = 0.0

    return {
        "title": str(out.get("title") or "").strip()[:50],
        "summary": str(out.get("summary") or "").strip()[:300],
        "matched_interest_ids": matched,
        "score": max(0.0, min(10.0, score)),
    }
