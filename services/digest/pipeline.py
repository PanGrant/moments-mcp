"""Digest pipeline: dedupe → interest match + summarize → store (§4).

collect（各采集器）产出的统一 RawItem 进入这里：
1. 计算 source_msg_id（author + 归一化正文的哈希）并入库去重
2. 尚无摘要的条目逐条调用总结（一次 LLM 调用同时完成兴趣匹配与摘要）
3. 摘要失败不阻塞批次 —— 原文已入库，下一轮自动重试（get_raw_items_without_digest）
"""
import hashlib
import re
from typing import Any, Callable, Dict, List, Optional

from . import db
from .summarize import summarize_item


def normalize_text(text: str) -> str:
    """Collapse all whitespace runs so visually identical posts hash equal."""
    return re.sub(r"\s+", " ", (text or "")).strip()


def compute_source_msg_id(author: str, text: str) -> str:
    """Stable dedupe key per (source) — hash of author + normalized text."""
    basis = f"{author or ''}\n{normalize_text(text)}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def to_raw_item(vlm_item: Dict[str, Any], source: str = "moments") -> Dict[str, Any]:
    """Map a vision-model item {author, text, images_desc, comments, time_hint}
    to a source-agnostic RawItem dict (source_msg_id computed in the pipeline)."""
    return {
        "source": source,
        "author": str(vlm_item.get("author") or ""),
        "posted_at": str(vlm_item.get("time_hint") or ""),
        "text": str(vlm_item.get("text") or ""),
        "media_desc": str(vlm_item.get("images_desc") or ""),
        "thread": vlm_item.get("comments") or [],
        # service-account dropdown arrow detected at ingest (自主批阅 skips
        # these); sources without an arrow probe yield 0
        "sponsored": int(vlm_item.get("sponsored", 0) or 0),
        # underscore keys are collector-internal markers (e.g.
        # _stitch_suspect) that ride along for in-round dedupe decisions;
        # they never reach the DB (storage projects named columns)
        **{k: v for k, v in vlm_item.items() if k.startswith("_")},
    }


def run_pipeline(conn, raw_items: List[Dict[str, Any]],
                 summarize_fn: Optional[Callable[[Dict, List[Dict]], Dict]] = None) -> Dict[str, int]:
    """Ingest raw items and produce digest items. summarize_fn is injectable
    for tests/offline runs; defaults to summarize.summarize_item. Raw items
    from earlier rounds that never got a digest item (failed summarize) are
    retried in the same pass as the newly collected ones.

    Returns stats: {collected, new, summarized, skipped, failed, errors}."""
    if summarize_fn is None:
        summarize_fn = summarize_item

    interests = db.list_interests(conn, enabled_only=True)
    name_by_id = {i["id"]: i["name"] for i in interests}

    # Raw items whose summarize failed in a previous round are retried now.
    pending = db.get_raw_items_without_digest(conn)
    all_items, seen = [], set()
    for item in list(raw_items) + pending:
        if not item.get("source_msg_id"):
            item["source_msg_id"] = compute_source_msg_id(
                item.get("author", ""), item.get("text", ""))
        key = (item["source"], item["source_msg_id"])
        if key in seen:  # same post captured again while still pending retry
            continue
        seen.add(key)
        all_items.append(item)

    stats = {"collected": len(all_items), "new": 0, "summarized": 0,
             "skipped": 0, "failed": 0, "errors": []}
    for item in all_items:
        raw_id, is_new = db.insert_raw_item(conn, item)
        if is_new:
            stats["new"] += 1
        if db.has_digest(conn, raw_id):
            stats["skipped"] += 1
            continue
        raw = db.get_raw_item(conn, raw_id)
        try:
            result = summarize_fn(raw, interests)
            matched = [
                {"id": vid, "name": name_by_id[vid]}
                for vid in result.get("matched_interest_ids", [])
                if vid in name_by_id
            ]
            db.insert_digest_item(
                conn, raw_id, result.get("title", ""), result.get("summary", ""),
                matched, result.get("score", 0),
            )
            stats["summarized"] += 1
        except Exception as e:
            stats["failed"] += 1
            stats["errors"].append(f"{item.get('author') or '?'}: {e}")
    conn.commit()
    return stats
