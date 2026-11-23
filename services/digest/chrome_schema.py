"""窗口 chrome schema（用户 2026-11-16 令）：布局不变量 + 按钮定位。

不变量（唯一允许的假设）：**最右栏 = 内容栏**。
窗口左侧有菜单栏；窗口拉宽时中间可能出现二级栏——本 schema 不定义它，
也不允许"菜单栏右边就是内容"的假设，一切定位只从截图最右（或最左）量出。

本模块只定义两个按钮（其他用不上暂不定义）：
  refresh_btn  ⟳   内容栏顶部工具栏带，白色图标簇（📷 🔔 ⟳）右缘锚定、
                    ~35pt 等距；⟳ = 从右数第三个图标。
  discover_btn 🧭   最左菜单栏图标列，自上而下第 4 个小图标
                    （头像按尺寸排除，红色角标按颜色排除）。

原则：先测量后点击。locate_* 从实况截图量出点位（窗口左上原点、pt 单位）；
量不到就返回 None，由调用方走回退 + 事后核验，绝不瞎点。
"""
from __future__ import annotations

import math
import os

from PIL import Image
import numpy as np

# ---- refresh：右缘锚定图标簇的几何常数（pt，来自 2026-11-16 实测）----
REF_CLUSTER_GAP_PT = 35.0        # 相邻图标中心距
REF_CLUSTER_GAP_TOL_PT = 14.0    # 允许偏差
REF_ROW_Y_TOL_PT = 8.0           # 簇内图标同行（y 差上界）——碎片不在行上
REF_BAND_Y0_PT, REF_BAND_Y1_PT = 8.0, 52.0     # 工具栏带（垂直）
REF_BAND_X_FRAC = 0.55           # 只看内容栏右半，躲开封面图大面积亮区
REF_MIN_WH_PT, REF_MAX_WH_PT = 7.0, 34.0       # 图标 blob 的合理尺寸

# ---- discover：左菜单栏的几何常数（pt）----
DIS_BAND_W_PT = 60.0                            # 菜单栏宽度上界
DIS_Y0_PT = 30.0                                # 避开窗口红绿灯
DIS_ICON_MIN_H_PT, DIS_ICON_MAX_H_PT = 12.0, 34.0  # 头像(~44pt)按尺寸排除
DIS_ROW_MERGE_PT = 10.0                         # 同一行 blob 的中心距容差
DIS_SLOT = 4                                    # 发现 = 第 4 个小图标位


def _load_gray(path, win_w_pt):
    """读截图为灰度 ndarray，返回 (arr, scale)。scale = 每 pt 多少 px。"""
    if not os.path.exists(path):
        return None, 0.0
    img = Image.open(path).convert("L")
    arr = np.asarray(img, dtype=np.uint8)
    scale = arr.shape[1] / float(win_w_pt)
    if scale <= 0:
        return None, 0.0
    return arr, scale


def _blobs(mask):
    """连通域（4邻接，栈式泛洪）。输入 bool 数组，输出 [(cx,cy,w,h,n)]（px）。"""
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    out = []
    for y0 in range(h):
        for x0 in range(w):
            if not mask[y0, x0] or seen[y0, x0]:
                continue
            stack = [(y0, x0)]
            seen[y0, x0] = True
            pts = []
            while stack:
                y, x = stack.pop()
                pts.append((y, x))
                for ny, nx in ((y-1, x), (y+1, x), (y, x-1), (y, x+1)):
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            ys = [p[0] for p in pts]
            xs = [p[1] for p in pts]
            out.append((sum(xs)/len(xs), sum(ys)/len(ys),
                        max(xs)-min(xs)+1, max(ys)-min(ys)+1, len(pts)))
    return out


def _pt(bx, scale):
    """blob 中心 px → pt。"""
    return bx[0]/scale, bx[1]/scale


def _rgb_not_red(path):
    """读截图为 RGB bool 掩码：非红色像素（用于剔除红色角标）。"""
    img = Image.open(path).convert("RGB")
    a = np.asarray(img, dtype=np.int16)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    red = (r > g + 50) & (r > b + 50)
    return ~red


def locate_refresh(capture_path, win_w_pt, win_h_pt):
    """⟳：内容栏顶部带，白色图标簇右缘锚定，从右第 3 个。

    返回 {"x_pt","y_pt","n"} 或 None（量不到 = 老实说，调用方走回退）。
    """
    arr, scale = _load_gray(capture_path, win_w_pt)
    if arr is None:
        return None
    x0 = int(arr.shape[1] * REF_BAND_X_FRAC)
    y0 = int(REF_BAND_Y0_PT * scale)
    y1 = min(int(REF_BAND_Y1_PT * scale), arr.shape[0])
    # 双极性（2026-11-16 实测两种形态）：宽窗口=白图标画在封面图上；
    # 窄窗口(510pt)=深灰图标画在白标题栏上。任一极性量到等距三元组即可。
    band_full = arr[y0:y1, x0:]
    # 不是"从右数第三个"——阈值切割常把图标下缘剥成碎片（2026-11-16 实测：
    # 铃铛碎片 y≈40pt 恰插在相机/铃铛 x 之间），取序数会取到碎片。
    # 改找右锚定等距三元组：x 等距 + y 同行，碎片被行约束出局。
    for polarity in ("bright", "dark"):
        band = band_full > 200 if polarity == "bright" else band_full < 120
        blobs = [b for b in _blobs(band)
                 if REF_MIN_WH_PT*scale <= max(b[2], b[3]) <= REF_MAX_WH_PT*scale]
        if len(blobs) < 3:
            continue
        blobs.sort(key=lambda b: -b[0])               # 从右往左
        for i in range(len(blobs)):
            for j in range(i + 1, len(blobs)):
                gap1 = (blobs[i][0] - blobs[j][0]) / scale
                if abs(gap1 - REF_CLUSTER_GAP_PT) > REF_CLUSTER_GAP_TOL_PT:
                    continue
                if abs(blobs[i][1] - blobs[j][1]) / scale > REF_ROW_Y_TOL_PT:
                    continue
                for k in range(j + 1, len(blobs)):
                    gap2 = (blobs[j][0] - blobs[k][0]) / scale
                    if abs(gap2 - REF_CLUSTER_GAP_PT) > REF_CLUSTER_GAP_TOL_PT:
                        continue
                    if abs(blobs[k][1] - blobs[j][1]) / scale > REF_ROW_Y_TOL_PT:
                        continue
                    x_pt, y_pt = _pt(blobs[k], scale)
                    return {"x_pt": x_pt + x0 / scale,
                            "y_pt": y_pt + y0 / scale, "n": 3}
    return None


def locate_discover(capture_path, win_w_pt, win_h_pt):
    """🧭 发现：最左菜单栏第 4 个小图标（头像排除、红角标排除）。

    返回 {"x_pt","y_pt","n"} 或 None。
    """
    arr, scale = _load_gray(capture_path, win_w_pt)
    if arr is None:
        return None
    not_red = _rgb_not_red(capture_path)
    x1 = min(int(DIS_BAND_W_PT * scale), arr.shape[1])
    y0 = int(DIS_Y0_PT * scale)
    band = (arr[y0:, :x1] < 190) & not_red[y0:, :x1]   # 深色图标/绿色激活态
    blobs = [b for b in _blobs(band)
             if b[4] >= 12 and
             DIS_ICON_MIN_H_PT*scale <= b[3] <= DIS_ICON_MAX_H_PT*scale]
    if not blobs:
        return None
    blobs.sort(key=lambda b: b[1])                    # 自上而下
    rows = []                                         # 同一图标的碎块并成一行
    for b in blobs:
        if rows and abs(b[1] - rows[-1][-1][1]) <= DIS_ROW_MERGE_PT*scale:
            rows[-1].append(b)
        else:
            rows.append([b])
    if len(rows) < DIS_SLOT:
        return None
    row = rows[DIS_SLOT - 1]
    cx = sum(b[0] for b in row)/len(row)
    cy = sum(b[1] for b in row)/len(row)
    return {"x_pt": cx/scale, "y_pt": cy/scale + y0/scale, "n": len(rows)}
