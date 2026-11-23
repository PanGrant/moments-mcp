"""Tolerant JSON extraction from LLM output (shared by vision + summarizer).

Models occasionally wrap JSON in markdown fences or prose; these helpers
locate the first JSON value in the text and parse it.
"""
import json
import re
from typing import Any, Dict, List


def _extract_json(text: str) -> Any:
    if text is None:
        raise ValueError("model returned empty content")
    cleaned = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    starts = [i for i in (cleaned.find("["), cleaned.find("{")) if i != -1]
    ends = [i for i in (cleaned.rfind("]"), cleaned.rfind("}")) if i != -1]
    if starts and ends:
        start, end = min(starts), max(ends)
        if end > start:
            cleaned = cleaned[start:end + 1]
    return json.loads(cleaned)


def parse_json_array(text: str) -> List[Any]:
    """Parse the model output into a JSON array; tolerant to fences/prose."""
    data = _extract_json(text)
    if not isinstance(data, list):
        raise ValueError("model output is not a JSON array")
    return data


def parse_json_object(text: str) -> Dict[str, Any]:
    """Parse the model output into a JSON object; tolerant to fences/prose."""
    data = _extract_json(text)
    if not isinstance(data, dict):
        raise ValueError("model output is not a JSON object")
    return data
