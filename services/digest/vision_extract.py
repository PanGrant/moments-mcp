"""Vision-model extraction of Moments items from screenshots (M1).

Sends a screenshot to a GLM vision model and parses the structured item
list it returns. The vision model is configured independently from the
chat model via DIGEST_VISION_MODEL (default glm-4v-flash, historically
free tier on bigmodel.cn); the OpenAI-compatible endpoint is reused.
"""
import base64
import os
from typing import Any, Dict, List, Optional

from .config import load_env_file
from .json_utils import parse_json_array

EXTRACTION_PROMPT = """这是微信「朋友圈」窗口的截图。请识别屏幕上可见的朋友圈条目，输出一个 JSON 数组，每个元素包含：
- "author": 发布者名字（字符串）
- "text": 正文文字，保持原文，不要翻译或改写（字符串）
- "images_desc": 配图内容的简短描述，没有配图则为空字符串
- "comments": 可见的评论列表 [{"author": "...", "text": "..."}]，没有则为空数组
- "time_hint": 可见的发布时间提示（如 "1小时前"、"昨天"），没有则为空字符串

要求：
1. 只输出 JSON 数组，不要任何其他文字、不要 markdown 代码块标记。
2. 如果截图中看不到任何朋友圈内容（例如窗口为空、只有壁纸），输出 []。
3. 广告/系统推荐条目也要输出，text 中注明 [广告] 前缀。
"""

# Candidate vision models, tried in order (first that accepts the call wins).
DEFAULT_VISION_MODEL = "glm-4v-flash"
VISION_MODEL_CANDIDATES = [
    "glm-4v-flash",
    "glm-4.5v",
    "glm-4v-plus",
]




def extract_moments_items(
    image_path: str,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Extract Moments items from one screenshot. Returns a list of dicts
    (author/text/images_desc/comments/time_hint). Raises when the model
    call fails or returns unparseable output."""
    load_env_file()
    from openai import OpenAI

    model = model or os.environ.get("DIGEST_VISION_MODEL", DEFAULT_VISION_MODEL)
    base_url = base_url or os.environ.get("LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/")
    api_key = api_key or os.environ.get("LLM_API_KEY") or os.environ.get("GROQ_API_KEY", "")

    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    client = OpenAI(base_url=base_url, api_key=api_key, timeout=120)
    resp = client.chat.completions.create(
        model=model,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": EXTRACTION_PROMPT},
            ],
        }],
        temperature=0.1,
    )
    content = resp.choices[0].message.content if resp.choices else None
    return parse_json_array(content)
