"""classify_capture 的 discover 判卷回归（2026-11-16 实机教训）。

不变量：内容=最右栏。宽窗口的发现菜单列常驻内容栏左侧（cx≈0.19），
此时最右栏是朋友圈封面 → 不得判 discover（曾冤判 → 恢复失败 → FATAL）。
菜单行只有占据最右栏（cx>0.45）才是 discover 页。
"""
import importlib.util
import os

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "post_test", os.path.join(_REPO, "scripts", "post_test.py"))
post_test = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(post_test)


def _row(text, cx, cy=0.5):
    return {"text": text, "x": cx - 0.05, "y": cy - 0.01,
            "w": 0.1, "h": 0.02}


def _classify(rows, monkeypatch):
    monkeypatch.setattr(post_test, "ocr_rows", lambda path: rows)
    return post_test.classify_capture("fake.png")


def test_wide_moments_with_side_menu_is_not_discover(monkeypatch):
    rows = [
        _row("Moments", 0.19, 0.16),      # 发现菜单列（侧栏位）
        _row("Channels", 0.19, 0.28),
        _row("Discover", 0.30, 0.06),     # 菜单列标题
        _row("如果关注人寿险种，一定知道这三份保单都是", 0.74, 0.82),
    ]
    verdict, why = _classify(rows, monkeypatch)
    assert verdict == "unknown", f"侧栏菜单行不该判 discover: {why}"


def test_narrow_discover_menu_page_is_discover(monkeypatch):
    # 窄窗口：菜单页占据内容栏（最右栏）→ menu rows cx>0.45
    rows = [
        _row("Discover", 0.60, 0.10),
        _row("Moments", 0.60, 0.25),
        _row("Channels", 0.60, 0.40),
    ]
    verdict, why = _classify(rows, monkeypatch)
    assert verdict == "discover", why


def test_time_rows_still_win_over_menu_rows(monkeypatch):
    rows = [
        _row("Moments", 0.60, 0.25),
        _row("Channels", 0.60, 0.40),
        _row("Yesterday", 0.70, 0.60),   # 时间牌 = moments 铁证
    ]
    verdict, why = _classify(rows, monkeypatch)
    assert verdict == "moments", why


def test_chat_clocks_still_win(monkeypatch):
    rows = [_row("14:02", 0.30, 0.20), _row("14:05", 0.30, 0.35),
            _row("Moments", 0.60, 0.60)]
    verdict, why = _classify(rows, monkeypatch)
    assert verdict == "chat", why


# ---- check_view 的 unknown 消歧（2026-11-16 用户令：绝不在非朋友圈页找⟳）----

class _FakeWin:
    bounds = {"x": 0, "y": 0, "width": 548, "height": 540}


def _check(rows, locate_ret, monkeypatch):
    monkeypatch.setattr(post_test, "ocr_rows", lambda path: rows)
    monkeypatch.setattr(post_test, "win_capture", lambda win, tag: "fake.png")
    calls = []
    monkeypatch.setattr(post_test, "locate_refresh",
                        lambda *a, **k: calls.append(a) or locate_ret)
    return post_test.check_view(_FakeWin(), "t"), calls


def test_unknown_with_toolbar_cluster_is_moments_cover_top(monkeypatch):
    rows = [_row("Grant Pan", 0.61, 0.58)]          # 封面顶：只有名字
    (verdict, why), calls = _check(rows, {"x_pt": 440}, monkeypatch)
    assert verdict == "moments", why
    assert len(calls) == 1


def test_unknown_without_cluster_is_foreign_not_ok(monkeypatch):
    # 教训（2026-11-16 contact management 误入）：无簇时若仍返回 unknown
    # （在 VIEW_OK 白名单里）= 消歧无效 = 非朋友圈页被放行盲点。
    rows = [_row("Service Accounts", 0.30, 0.60)]   # 聊天列表类页面
    (verdict, why), calls = _check(rows, None, monkeypatch)
    assert verdict == "foreign", why
    assert verdict not in post_test.VIEW_OK


def test_chat_verdict_skips_evidence_check(monkeypatch):
    rows = [_row("14:02", 0.30, 0.20), _row("14:05", 0.30, 0.35)]
    (verdict, why), calls = _check(rows, {"x_pt": 440}, monkeypatch)
    assert verdict == "chat", why
    assert calls == [], "chat 判定不该再花一次截图去验证"


def test_expanded_post_with_blank_grid_is_not_viewer():
    # 2026-11-16 21:21 胡吟帖真机证据（fixtures/view_expand_blank_grid.png
    # = 当时的 av2.png）：展开成功帧（Show More→Hide），全部文字都在
    # y<0.35，其下是未加载的空白九宫格。旧判读带 y∈[0.42,0.95] 把整帧
    # 文字排除 → 冤判 viewer → act-error，好端端的展开被当事故。
    # 样本即契约：这帧必须判 unknown（封面顶/正常态），viewer 是冤案。
    import services.digest.chrome_schema as cs
    fix = os.path.join(os.path.dirname(__file__), "fixtures",
                       "view_expand_blank_grid.png")
    verdict, why = post_test.classify_capture(fix)
    assert verdict == "unknown", f"健康展开帧不得判 viewer（旧带冤案）：{why}"
    # check_view 语义：unknown + ⟳ 簇量得（窄窗标题栏深色图标）= moments
    b = {"width": 510.0, "height": 464.0}
    assert cs.locate_refresh(fix, b["width"], b["height"]) is not None, \
        "健康展开帧必须能量得 ⟳ 簇（否则 check_view 会冤判 foreign）"
