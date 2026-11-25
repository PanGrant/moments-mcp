"""朋友圈布局图纸（ldl L1）——门牌/时间行试点（任务 3 §5 第 1 步）。

这是 post_schema.py 里同居的图纸第一次被搬出来单独成文：座位、契约、
判据阈值全部声明在数据里，每条带语料证据出处。感知层目前仍在
post_schema.py 施工（第 2 步才改读声明），所以本模块自带一张
**对账表**：图纸数值 ↔ 施工常量逐项比对，`python -m
services.digest.layout_def` 即验。漂移在施工侧改数而忘改图时当场报警。

值的两分类（宪法第三条）：
- habits（物种习性）＝全语料量定的群体边界，每条带证据；
- measured（窗口几何）＝每运行实测槽位（FeedSchema 惰性量定）。

公开仓内容纪律：本文件只有数值与方法名，零真实案例名。
"""
from ldl.schema import ColumnSpec, ContractSpec, LayoutDef, MEASURED, RowSpec

# 对账表：habit 键 → (施工侧常量名)。__main__ 逐项比对；施工侧没有
# 对应常量的判据（如 col_x 簇距 0.012 是 measure_col_x 函数体内联字
# 面量）不在表内——那正是第 2 步要让施工改读声明的点。
_LEDGER = {
    "doorplate_hug_up": "DOORPLATE_HUG_UP",
    "doorplate_hug_down": "DOORPLATE_HUG_DOWN",
    "doorplate_next_min": "DOORPLATE_NEXT_MIN",
    "doorplate_band_down": "DOORPLATE_BAND_DOWN",
    "doorplate_photo_min_ratio": "DOORPLATE_PHOTO_MIN_RATIO",
    "author_zone": "AUTHOR_ZONE",
    "author_x_max": "AUTHOR_X_MAX",
    "time_row_x_slack": "TIME_ROW_X_SLACK",
    "content_x_lo": None,   # CONTENT_X[0]
    "content_x_hi": None,   # CONTENT_X[1]
}


def build() -> LayoutDef:
    return LayoutDef(
        app="wechat-moments",
        columns={
            # FeedSchema.init 首屏量定（init-once 骨架）
            "pane": ColumnSpec("pane", MEASURED, MEASURED),
            "photo": ColumnSpec("photo", MEASURED, MEASURED),
            # measure_col_x：时间行/短名行/长正文行三种形状在 CONTENT_X
            # 带内取最左簇中位数（簇距见 habits.cluster_gap）
            "text": ColumnSpec("text",
                               "leftmost_cluster(time_rows, author_rows,"
                               " long_rows)",
                               MEASURED),
        },
        rows=[
            # ── 试点座位 1：门牌（作者行）──
            # 识别契约：贴靠照片块顶的名字形状行，过三关（贴靠/清空/
            # 照片身高），内容盲——名字区只认位置（2026-11-23 宪法修正）。
            RowSpec("doorplate", "fixed(name_row_h)", ContractSpec(
                admit="author_shape_row_hugging_photo_ink_top",
                gates=["author_zone", "author_x_max",
                       "doorplate_hug_up", "doorplate_hug_down",
                       "doorplate_next_min", "doorplate_band_down",
                       "doorplate_photo_min_ratio"],
                content_blind=True,
                exceptions=["sponsor_arrow"],   # 广告卡箭头像素探针
                emits=["author_name"],
            )),
            # ── 试点座位 2：时间行 ──
            # 时间形状（"刚刚/N分钟前/昨天 HH:MM/…"）+ 内容列 x 门；
            # 顺带产出评论按钮 x 样本（button_x 取中位数的来源）。
            RowSpec("time_row", "fixed(time_row_h)", ContractSpec(
                admit="time_shape_row_on_text_column",
                gates=["time_row_x_slack"],
                content_blind=True,
                emits=["time_text", "comment_button"],
            )),
            # ── 树的上下文（本轮试点不施工，契约后续补齐）──
            RowSpec("body", "variable", ContractSpec(
                admit="under_doorplate",
                content_blind=False,
                content_blind_why="正文内容本身就是要提取的数据",
                emits=["body_text", "expand_link"],
            )),
            RowSpec("comments", "variable", ContractSpec(
                admit="under_time_row",
                gates=[],
                content_blind=True,
                emits=[],
            )),
        ],
        measured=[
            # 窗口几何：每运行由 FeedSchema 量定，定义不写死（宪法第三条）
            "name_row_h", "time_row_h", "body_pitch",
            "avatar_offset_x", "comment_indent_x", "comment_pitch",
            "time_comments_gap", "button_x",
        ],
        habits={
            # ── 门牌三关（2026-11-15，235 真门牌量定）──
            "doorplate_hug_up": {
                "value": 0.008,
                "evidence": "235 真门牌：名字行顶最多高出照片块顶 0.008"
                            "（OCR 抖动余量）",
            },
            "doorplate_hug_down": {
                "value": 0.0125,
                "evidence": "235 真门牌：贴靠偏移最大 +0.0107，最近的假"
                            "门牌 +0.0143，取中",
            },
            "doorplate_next_min": {
                "value": 0.032,
                "evidence": "235 真门牌：门牌行与下一行净空最小 +0.039"
                            "（大宗 +0.050），假门牌恰 +0.032",
            },
            "doorplate_band_down": {
                "value": 0.01,
                "evidence": "235 真门牌：清空判定带，吸收行高抖动",
            },
            "doorplate_photo_min_ratio": {
                "value": 1.15,
                "evidence": "235 真门牌：门牌行文字行高/照片行高最小 1.38，"
                            "最大假门牌 0.94，取中",
            },
            # ── 门牌证据域 ──
            "author_zone": {
                "value": 0.07,
                "evidence": "真门牌只出现在 pane 顶下 7% 带内（语料普查）",
            },
            "author_x_max": {
                "value": 0.33,
                "evidence": "名字区右界 0.33（语料普查；名字行不越过此 x）",
            },
            # ── 时间行 ──
            "time_row_x_slack": {
                "value": 0.03,
                "evidence": "万得卡内嵌戳离真左缘 +0.077，OCR 抖动 ~0.01："
                            "拒卡戳留 0.047 余量、放真行留 3 倍抖动余量",
            },
            # ── 内容列（measure_col_x 三形状最左簇）──
            "content_x_lo": {
                "value": 0.25,
                "evidence": "内容列 x 可信带下界（参与最左簇投票的形状范围）",
            },
            "content_x_hi": {
                "value": 0.50,
                "evidence": "内容列 x 可信带上界（评论/按钮都在带右）",
            },
            "cluster_gap": {
                "value": 0.012,
                "evidence": "真列与第一缩进列实测只偏右 0.02，真列自身抖动"
                            " ~0.01；簇距 0.012 保两列并簇前先分开（施工侧"
                            "尚未命名此常量——第 2 步收编）",
            },
        },
    )


def _check_against_builder() -> list:
    """对账：图纸数值 ↔ 施工侧常量。返回问题列表（空 = 对齐）。"""
    from services.digest import post_schema as ps
    problems = []
    for key, const in _LEDGER.items():
        want = build().habits[key]["value"]
        if const is None:
            got = {"content_x_lo": ps.CONTENT_X[0],
                   "content_x_hi": ps.CONTENT_X[1]}[key]
        else:
            got = getattr(ps, const)
        if abs(float(want) - float(got)) > 1e-9:
            problems.append(f"{key}: 图纸={want} 施工 {const}={got}")
    return problems


if __name__ == "__main__":
    d = build()
    errs = d.validate()
    n_habits = len(d.habits)
    n_measured = len(d.measured)
    n_gates = sum(len(r.contract.gates) for r in d.rows)
    print(f"图纸校验: {'通过' if not errs else errs}")
    print(f"  行 {len(d.rows)} 座位（试点: 门牌/时间行）· 列 {len(d.columns)}"
          f" · 习性 {n_habits} · 实测槽位 {n_measured} · 门引用 {n_gates}")
    round_trip = LayoutDef.from_dict(d.to_dict())
    print(f"  to_dict/from_dict 往返: "
          f"{'一致' if round_trip.to_dict() == d.to_dict() else '不一致!'}")
    problems = _check_against_builder()
    print(f"对账 post_schema 施工常量: {_LEDGER and len(_LEDGER)} 项 → "
          f"{'全部对齐' if not problems else problems}")
    raise SystemExit(1 if (errs or problems) else 0)
