"""回复内容定制（2026-11-17 设计定案，2026-11-22 切片二接线；
2026-11-25 用户令：加持久跳过名单 + 署名可定制）。

单一事实源：moments-mcp（settings 工具读写）、walker 组稿（prompt
注入 + 署名解析 + 名单闸）共用本模块。零第三方依赖——MCP 壳
不能为它引入 fastapi/pydantic。

内容定制两层叠加：style=单选预设（STYLES 里的组稿提示词片段）；
extra=自由文本，原样追加。

圈定（2026-11-25 用户拍板「默认全回+跳过名单」）：skip_authors=
持久跳过名单，名单内作者的帖子 act 永不回（walker 名单闸拦，帖留
pending 不封账）；空名单=全量（原行为）。

署名（2026-11-25 用户令，推翻 2026-11-17 宪法案）：signature_label
可定制；显式存了键就用它（""=不加署名——发出的评论不再带 AI 披露，
调用方必须先警告用户）；没存过键退 DIGEST_REVIEW_SIGNATURE env，
再退默认宪法文本。经 resolve_signature 解析（reviewer 与 post_test
组稿同源）。

存储 = data/moments_settings.json（MOMENTS_SETTINGS env 可换路径）。
函数全部显式收 path 参数（缺省用模块级 SETTINGS_PATH），调用方
（测试）patch 模块属性即可隔离。
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
SKIP_AUTHORS_MAX = 50
SIGNATURE_MAX = 60
DEFAULT_SIGNATURE = "（AI助手胡乱解读自动回复，内容请忽略）"

# save() 的「未传保持原值」哨兵：signature_label 用它区分「没传」（键
# 保持原样——没存过就继续不存在，env 覆盖路径不被堵死）与「传空串」
# （显式清空署名）。own_names/skip_authors 用 None 表达同义。
KEEP = object()

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


def _clean_names(names):
    """名单字段共用清洗：非列表→[]；去首尾空白、丢空串（不去重——
    调用方决定要不要去重，重复项无害）。"""
    if not isinstance(names, list):
        return []
    return [str(n).strip() for n in names if str(n).strip()]


def load(path=None):
    """读设置；坏 JSON / 未知风格 / 无文件 → 默认值（宽容读）。
    signature_label 特殊：**不做** setdefault——键缺席=用户没定制过，
    resolve_signature 才能退到 env/默认文本。"""
    p = path or SETTINGS_PATH
    try:
        with open(p) as f:
            st = json.load(f)
        if st.get("style") not in STYLES:
            st["style"] = DEFAULT_STYLE
        st.setdefault("extra", "")
        st["own_names"] = _clean_names(st.get("own_names"))
        st["skip_authors"] = _clean_names(st.get("skip_authors"))
        return st
    except (OSError, ValueError):
        return {"style": DEFAULT_STYLE, "extra": "", "own_names": [],
                "skip_authors": []}


def validate(style, extra):
    """PUT 前校验；返回错误文案（None=通过）。"""
    if style not in STYLES:
        return f"style must be one of: {', '.join(STYLES)}"
    if len(extra or "") > EXTRA_MAX:
        return f"extra too long (max {EXTRA_MAX})"
    return None


def save(style, extra, path=None, own_names=None, skip_authors=None,
         signature_label=KEEP):
    """写设置（extra 去首尾空白）；返回落盘内容。调用方先 validate。
    own_names/skip_authors=None 表示沿用已存的值；signature_label=KEEP
    表示键原样保留（没存过就继续不存在），传字符串（含 ""）= 显式
    设置（"" = 清空署名）。"""
    p = path or SETTINGS_PATH
    cur = load(p)
    if own_names is None:
        own_names = cur.get("own_names") or []
    if skip_authors is None:
        skip_authors = cur.get("skip_authors") or []
    st = {"style": style, "extra": (extra or "").strip(),
          "own_names": _clean_names(own_names),
          "skip_authors": _clean_names(skip_authors)}
    if signature_label is not KEEP:
        st["signature_label"] = str(signature_label or "").strip()
    elif "signature_label" in cur:
        st["signature_label"] = cur["signature_label"]
    st["updated_at"] = time.time()
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(p, "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    return st


def resolve_signature(path=None):
    """署名解析（单一事实源）：settings 显式存过 signature_label 就用
    它（"" = 不加署名——调用方必须先向用户警告「发出的评论不再带 AI
    披露」）；没存过退 DIGEST_REVIEW_SIGNATURE env；env 也没有退默认
    宪法文本。reviewer._resolve_persona 与 post_test 组稿都经此解析，
    署名与人设同源不变。"""
    st = load(path)
    if "signature_label" in st:
        return str(st.get("signature_label") or "").strip()
    env = os.environ.get("DIGEST_REVIEW_SIGNATURE", "")
    return (env or DEFAULT_SIGNATURE).strip()


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
