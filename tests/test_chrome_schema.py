"""chrome_schema 定位回归：以 2026-11-16 实况截图为样本（retina @2x，
548x540pt 窗口）。样本即契约：量到就必须落在实测点附近；量不到=回退。"""
import os

from services.digest.chrome_schema import locate_refresh, locate_discover

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
W, H = 548.0, 540.0


def _f(name):
    return os.path.join(FIX, name)


def test_refresh_located_on_moments_tab():
    loc = locate_refresh(_f("chrome_probe_moments.png"), W, H)
    assert loc is not None, "朋友圈页顶部应能量到 ⟳ 图标簇"
    assert abs(loc["x_pt"] - 440.0) <= 8   # 实测 ⟳ 中心 ≈ (440, 28.5)pt
    assert abs(loc["y_pt"] - 28.5) <= 8


def test_discover_located_on_main_tab():
    loc = locate_discover(_f("chrome_probe_main.png"), W, H)
    assert loc is not None, "主窗口菜单栏应能量到发现图标"
    assert abs(loc["x_pt"] - 30.0) <= 12   # 菜单栏中线 x≈30pt
    assert abs(loc["y_pt"] - 277.0) <= 12


def test_discover_located_on_moments_tab_too():
    # 激活态=绿色图标 + 红点角标，都必须不影响量点
    loc = locate_discover(_f("chrome_probe_moments.png"), W, H)
    assert loc is not None
    assert abs(loc["x_pt"] - 30.0) <= 12
    assert abs(loc["y_pt"] - 277.0) <= 12


def test_refresh_located_on_wide_window():
    # 722x464pt（2026-11-16 resize 实况）：碎片曾插队 → 取序数取错 → None。
    # 等距三元组 + 同行约束后必须量到 ⟳ ≈ (613.4, 28)pt。
    loc = locate_refresh(_f("chrome_probe_moments_wide.png"), 722.0, 464.0)
    assert loc is not None, "宽窗口也必须量到 ⟳（碎片不得干扰）"
    assert abs(loc["x_pt"] - 613.4) <= 6
    assert abs(loc["y_pt"] - 28.0) <= 6


def test_refresh_located_on_light_titlebar():
    # 窄窗口(510pt)形态：深灰图标画在白标题栏上（极性与封面白图标相反）。
    # 2026-11-16 act 轮教训：此形态下旧逻辑拒量 → 原地刷新不回顶 → 起飞。
    loc = locate_refresh(_f("chrome_probe_moments_titlebar.png"), 510.0, 464.0)
    assert loc is not None, "白标题栏上的深灰 ⟳ 必须量到（双极性）"
    assert abs(loc["x_pt"] - 402.0) <= 6
    assert abs(loc["y_pt"] - 27.5) <= 6


def test_missing_capture_returns_none():
    assert locate_refresh(_f("nope.png"), W, H) is None
    assert locate_discover(_f("nope.png"), W, H) is None
