"""Draft generation + v1 send adapters (DIGEST_DESIGN.md §6).

A draft is produced from the digest item's original text + comment thread in
a channel-specific style, stored as status='draft', and only leaves the
machine after the user edits/confirms it (no auto-send). v1 send adapter =
clipboard (pbcopy) with a UI hint to paste into WeChat; SMTP/API adapters
arrive with M3/M4.
"""
import os
import subprocess
from typing import Any, Dict, Optional

from . import db
from .config import load_env_file

# Channel registry: style instructions for the generator + a human label for
# the UI. v1 ships moments_comment; email/youtube styles are ready for M3/M4.
CHANNELS: Dict[str, Dict[str, str]] = {
    "moments_comment": {
        "label": "朋友圈评论",
        "style": (
            "用中文写一条朋友圈评论：口语化、自然，1-2 句话，"
            "可以带少量表情符号；像朋友之间的真实回应，不要客套套话。"
        ),
    },
    "email_reply": {
        "label": "邮件回信",
        "style": (
            "写一封正式的中文邮件回信：包含称呼、正文（可分段）、落款。"
            "针对对方邮件中的问题逐点回应。"
        ),
    },
    "youtube_comment": {
        "label": "YouTube 评论",
        "style": (
            "Write a short, casual comment in the video's language "
            "(default to English if unclear): 1-2 sentences, genuine, no clickbait."
        ),
    },
}

PROMPT_TEMPLATE = """你是用户的社交回复助理。请根据下面的素材，为用户拟一份{channel_label}。

【风格要求】
{style}

【附加要求】（可能为空）
{extra}

【素材】
标题: {title}
作者: {author}
原文: {text}
配图: {media_desc}
评论: {comments_block}
已有对话要点: {chat_block}

只输出草稿正文本身，不要任何解释、引号或 markdown 代码块标记。"""


def _comments_block(thread) -> str:
    if not thread:
        return "（无可见评论）"
    return "\n".join(f"- {c.get('author', '?')}: {c.get('text', '')}" for c in thread)


def _chat_block(messages) -> str:
    """Condensed view of the item's conversation thread — recent turns only."""
    if not messages:
        return "（无）"
    recent = messages[-6:]
    return "\n".join(f"- {m['role']}: {str(m['content'])[:200]}" for m in recent)


def generate_draft(conn, digest_item_id: int, channel: str,
                   extra_instructions: str = "",
                   model: Optional[str] = None, base_url: Optional[str] = None,
                   api_key: Optional[str] = None) -> Dict[str, Any]:
    """Generate a channel-styled draft for one digest item and store it.
    Returns the stored draft row. Raises ValueError on unknown channel and
    KeyError/LLM errors bubble to the caller (per-request failure)."""
    if channel not in CHANNELS:
        raise ValueError(f"unknown channel: {channel} (known: {list(CHANNELS)})")
    item = db.get_digest_item(conn, digest_item_id)
    if item is None:
        raise KeyError(f"digest item not found: {digest_item_id}")

    load_env_file()
    from openai import OpenAI

    # Default to the free summarize model: LLM_MODEL (glm-5.3-flash) has no
    # balance on this account — live 2026-11-08 the 生成草稿 button 429'd
    # while the reviewer/summarize path (DIGEST_SUMMARIZE_MODEL) worked.
    model = (model or os.environ.get("DIGEST_SUMMARIZE_MODEL")
             or os.environ.get("LLM_MODEL", "glm-4.6"))
    base_url = base_url or os.environ.get(
        "LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/")
    api_key = api_key or os.environ.get("LLM_API_KEY", "")

    thread_id = db.get_or_create_thread(conn, digest_item_id)
    prompt = PROMPT_TEMPLATE.format(
        channel_label=CHANNELS[channel]["label"],
        style=CHANNELS[channel]["style"],
        extra=extra_instructions.strip() or "（无）",
        title=item.get("title") or "（无标题）",
        author=item.get("author") or "（未知）",
        text=item.get("text") or "（无正文）",
        media_desc=item.get("media_desc") or "（无）",
        comments_block=_comments_block(item.get("thread") or []),
        chat_block=_chat_block(db.list_thread_messages(conn, thread_id)),
    )

    client = OpenAI(base_url=base_url, api_key=api_key, timeout=120)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.4,
    )
    content = (resp.choices[0].message.content if resp.choices else "") or ""
    content = content.strip().strip("`").strip()

    draft_id = db.insert_draft(conn, digest_item_id, channel, content)
    return db.get_draft(conn, draft_id)


def send_via_clipboard(content: str) -> Dict[str, Any]:
    """v1 send adapter: copy to the macOS clipboard (pbcopy). The user pastes
    into WeChat themselves — nothing is sent programmatically."""
    p = subprocess.run(["pbcopy"], input=content.encode("utf-8"),
                       capture_output=True, timeout=10)
    if p.returncode != 0:
        raise RuntimeError(f"pbcopy failed: {p.stderr.decode('utf-8', 'replace')}")
    return {
        "sent": True,
        "adapter": "clipboard",
        "message": "已复制到剪贴板，请到微信中粘贴发送",
    }


def send_draft(conn, draft_id: int, content: Optional[str] = None) -> Dict[str, Any]:
    """Send a draft through its channel's adapter. `content` carries the
    user's final edit; the draft must still be in 'draft' status."""
    draft = db.get_draft(conn, draft_id)
    if draft is None:
        raise KeyError(f"draft not found: {draft_id}")
    if draft["status"] != "draft":
        raise ValueError(f"draft {draft_id} already sent")
    if content is not None:
        db.update_draft_content(conn, draft_id, content)
        final = content
    else:
        final = draft["content"]
    result = send_via_clipboard(final)
    db.mark_draft_sent(conn, draft_id)
    return result
