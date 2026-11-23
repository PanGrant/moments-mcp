"""回复内容定制（2026-11-17 设计定案，2026-11-22 切片二接线）。

单一事实源：webapp（UI 读写）、moments-mcp（settings 工具读写）、
walker 组稿（prompt 注入）三方共用本模块。零第三方依赖——MCP 壳
不能为它引入 fastapi/pydantic。

两层叠加：style=单选预设（STYLES 里的组稿提示词片段）；extra=自由
文本，原样追加。署名是宪法级固定项（reviewer persona.signature），
不进定制面。

存储 = data/moments_settings.json（MOMENTS_SETTINGS env 可换路径）。
函数全部显式收 path 参数（缺省用模块级 SETTINGS_PATH），调用方
（webapp 测试）patch 模块属性即可隔离。
"""
import json
import os
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
SETTINGS_PATH = os.environ.get(
    "MOMENTS_SETTINGS", os.path.join(_REPO, "data", "moments_settings.json"))

DEFAULT_STYLE = "friend"
EXTRA_MAX = 500

# 组稿提示词片段（风格=单选；写作时落在 prompt 的【本次定制】节）
STYLES = {
    "friend": "像老朋友随手写的评论：一两句话，自然口语，真诚不油腻，"
              "不堆客套话。",
    "warm": "热情有感染力：积极回应对方分享的内容，可以带 1-2 个 emoji，"
            "语气热络但不浮夸。",
    "brief": "简洁克制：一句话直给，基本不用 emoji，不啰嗦。",
    "humor": "轻松幽默：可以适度调侃或玩梗，但不过火、不冒犯。",
}


OWN_NAMES_MAX = 20


def load(path=None):
    """读设置；坏 JSON / 未知风格 / 无文件 → 默认值（宽容读）。"""
    p = path or SETTINGS_PATH
    try:
        with open(p) as f:
            st = json.load(f)
        if st.get("style") not in STYLES:
            st["style"] = DEFAULT_STYLE
        st.setdefault("extra", "")
        names = st.get("own_names")
        st["own_names"] = [str(n).strip() for n in names
                           if str(n).strip()] if isinstance(names, list) else []
        return st
    except (OSError, ValueError):
        return {"style": DEFAULT_STYLE, "extra": "", "own_names": []}


def validate(style, extra):
    """PUT 前校验；返回错误文案（None=通过）。"""
    if style not in STYLES:
        return f"style must be one of: {', '.join(STYLES)}"
    if len(extra or "") > EXTRA_MAX:
        return f"extra too long (max {EXTRA_MAX})"
    return None


def save(style, extra, path=None, own_names=None):
    """写设置（extra 去首尾空白）；返回落盘内容。调用方先 validate。
    own_names=None 表示沿用已存的值（不改自己的名字清单）。"""
    p = path or SETTINGS_PATH
    if own_names is None:
        own_names = load(p).get("own_names") or []
    st = {"style": style, "extra": (extra or "").strip(),
          "own_names": [str(n).strip() for n in own_names if str(n).strip()],
          "updated_at": time.time()}
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(p, "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    return st


def customization_block(path=None):
    """组稿 prompt 的【本次定制】节。空定制返回 ''（不注入空节）。"""
    st = load(path)
    parts = []
    frag = STYLES.get(st.get("style") or DEFAULT_STYLE)
    if frag:
        parts.append(f"- 语气风格：{frag}")
    extra = (st.get("extra") or "").strip()
    if extra:
        parts.append(f"- 用户补充要求：{extra}")
    if not parts:
        return ""
    return ("【本次定制】（用户为这轮评论指定的写法，"
            "与【语气基线】冲突时以本节为准）\n" + "\n".join(parts))


def inject_customization(prompt, block):
    """把定制节插到 prompt 的『要求：』之前（输出格式指令保持最后，
    不被定制稀释）；模板改动没有该锚点时退化为尾部追加。"""
    if not block:
        return prompt
    marker = "要求："
    if marker in prompt:
        return prompt.replace(marker, block + "\n\n" + marker, 1)
    return prompt + "\n\n" + block
