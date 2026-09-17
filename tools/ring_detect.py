"""点一下圆环中心, 自动找出白色圆环的内圈/外圈(用来校准环合条检测).

用法:
    tools\\ring_detect.cmd [图片]                        # GUI: 点圆环中心 -> 自动检测
    python tools/ring_detect.py 图片 --center 708,987    # 无窗口, 直接输出结果
    python tools/ring_detect.py 图片 --center 708,987 --save out.png

算法:
  1. 白度 = min(R,G,B)(彩色图标/背景天然被压掉, 白色圆环保留);
  2. 以点击点为中心做极坐标径向扫描(720 个角度 x 0.5px 步长), 得到"白度-半径"曲线;
  3. 取曲线峰值所在的白带, 用半高宽找内边界/外边界;
  4. 用白带中点的最小二乘圆拟合(Kasa)把圆心和半径再精修 3 轮;
  5. 输出圆心、内半径、外半径、厚度、白带里的"白色角度占比"(= 环合实际填充比例),
     以及白色角度的连续区间, 并换算成 app 用的 2560x1440 坐标。

只读图片, 不接游戏、不注入进程。
"""

import argparse
import os
import sys

import cv2
import numpy as np

try:
    from scipy.ndimage import map_coordinates
except ImportError:  # pragma: no cover - scipy 随 librosa 一起装, 正常都在
    map_coordinates = None

ANGLES = 720
R_STEP = 0.5
WHITE_LEVEL = 200  # 与 app 的 cycle_ratio() 用同一个阈值
FIT_ROUNDS = 3
MIN_RADIUS = 8.0  # 小于这个半径的白圈当"圆点/文字笔画", 不当环
MIN_SCORE = 0.2  # 环得分(白带覆盖率 - 内侧覆盖率)低于此值认为没找到白环
DEFAULT_IMAGE = os.path.join("screenshots", "1.png")


def whiteness(bgr: np.ndarray) -> np.ndarray:
    """白度图: min(R,G,B). 纯白=255, 彩色/暗色都低, 正好把白环从彩色 UI 里分出来."""
    return bgr.min(axis=2).astype(np.float32)


def radial_profile(white, cx, cy, r_max, angles=ANGLES, r_step=R_STEP):
    radii = np.arange(0.0, r_max, r_step)
    theta = np.linspace(0.0, 2.0 * np.pi, angles, endpoint=False)
    cos_a, sin_a = np.cos(theta), np.sin(theta)
    ys = cy + radii[:, None] * sin_a[None, :]
    xs = cx + radii[:, None] * cos_a[None, :]
    samples = map_coordinates(white, [ys.ravel(), xs.ravel()], order=1, mode="nearest")
    return radii, samples.reshape(len(radii), angles)


def ring_score(radii, samples, r_min):
    """环像不像"一圈白环": 白带处覆盖率高, 而它内侧(0.35r~0.8r)覆盖率低.

    只用"峰值覆盖率"会被圆心的白色小块骗到(内圈图标上就有白点, 覆盖率 1.0);
    `r_min` 再挡掉"小圆点/文字笔画"这种小半径的假环。
    """
    frac = (samples >= WHITE_LEVEL).mean(axis=1)
    best = (0.0, 0.0)
    for index, radius in enumerate(radii):
        if radius < r_min:
            continue
        inner = frac[(radii >= radius * 0.35) & (radii <= radius * 0.8)]
        inside = float(inner.mean()) if inner.size else 0.0
        score = float(frac[index]) - inside
        if score > best[0]:
            best = (score, float(radius))
    return best


def best_center(white, cx, cy, r_max, r_min):
    """点击只是"大概位置": 先粗搜再细搜, 找环得分最高的圆心."""
    best = (0.0, cx, cy)
    for radius, angles, step, r_step in (
        (12.0, 180, 2.0, 1.0),
        (2.0, ANGLES, 0.5, R_STEP),
    ):
        # 锚定本轮起点: 偏移量相对本轮开始时的圆心算, 否则会在循环里一路走偏
        base_x, base_y = best[1], best[2]
        offsets = np.arange(-radius, radius + 1e-9, step)
        for dy in offsets:
            for dx in offsets:
                px, py = base_x + dx, base_y + dy
                prof_radii, samples = radial_profile(white, px, py, r_max, angles, r_step)
                score = ring_score(prof_radii, samples, r_min)[0]
                if score > best[0]:
                    best = (score, px, py)
    return best


def find_band(radii, samples, r_min):
    """从"最像白环"的半径向外/向内走, 找白带的内外边界."""
    frac = (samples >= WHITE_LEVEL).mean(axis=1)
    _score, peak_radius = ring_score(radii, samples, r_min)
    if peak_radius <= 0.0:
        return None
    peak_index = int(np.argmin(np.abs(radii - peak_radius)))
    peak = float(frac[peak_index])
    if peak <= 0.05:
        return None
    edge = max(0.3, peak * 0.5)
    outer = peak_index
    while outer + 1 < len(frac) and frac[outer + 1] >= edge:
        outer += 1
    inner = peak_index
    while inner - 1 >= 0 and frac[inner - 1] >= edge:
        inner -= 1
    return radii[inner], radii[outer], peak, frac


def fit_circle(points):
    """Kasa 最小二乘圆拟合: 解 x^2+y^2 = 2ax + 2by + c."""
    x, y = points[:, 0], points[:, 1]
    a_mat = np.stack([2 * x, 2 * y, np.ones_like(x)], axis=1)
    rhs = x * x + y * y
    solution, *_ = np.linalg.lstsq(a_mat, rhs, rcond=None)
    cx, cy, c = solution
    return float(cx), float(cy), float(np.sqrt(c + cx * cx + cy * cy))


def band_points(white, cx, cy, r_in, r_out, angles=ANGLES):
    """每个角度在预估白带附近单独找白环的径向位置, 用白带中心半径当该角度的点.

    圆心差 1~2px 时, 固定半径采样会让部分角度掉出环外(看起来像断续的白点),
    所以逐角度单独找, 再拿这些点做圆拟合就能把圆心收准。
    返回 (白色角度(度), 点坐标) —— 角度数就是环合的填充率。
    """
    mid = 0.5 * (r_in + r_out)
    half = max(3.0, 0.5 * (r_out - r_in) + 5.0)
    radii = np.arange(max(0.5, mid - half), mid + half, 0.25)
    theta = np.linspace(0.0, 2.0 * np.pi, angles, endpoint=False)
    ys = cy + radii[:, None] * np.sin(theta)[None, :]
    xs = cx + radii[:, None] * np.cos(theta)[None, :]
    values = map_coordinates(
        white, [ys.ravel(), xs.ravel()], order=1, mode="nearest"
    ).reshape(len(radii), angles)
    mask = values >= WHITE_LEVEL
    keep, points = [], []
    for index in range(angles):
        hits = np.where(mask[:, index])[0]
        if len(hits) == 0:
            continue
        radius = float(radii[hits].mean())
        keep.append(np.degrees(theta[index]))
        points.append(
            (cx + radius * np.cos(theta[index]), cy + radius * np.sin(theta[index]))
        )
    return np.array(keep), np.array(points).reshape(-1, 2)


def detect(white, cx, cy, r_max, r_min=MIN_RADIUS):
    score, cx, cy = best_center(white, cx, cy, r_max, r_min)
    if score <= MIN_SCORE:
        return None
    r_in = r_out = 0.0
    for _ in range(FIT_ROUNDS):
        radii, samples = radial_profile(white, cx, cy, r_max)
        band = find_band(radii, samples, r_min)
        if band is None:
            return None
        r_in, r_out, _peak, _frac = band
        _angles, points = band_points(white, cx, cy, r_in, r_out)
        if len(points) < ANGLES * 0.05:
            break
        fit_cx, fit_cy, fit_r = fit_circle(points)
        mid = 0.5 * (r_in + r_out)
        if abs(fit_r - mid) > mid * 0.5:
            break
        # 拟合只在附近微调, 而且必须让环得分变好才接受(否则会被别的白色物体拉跑)
        if np.hypot(fit_cx - cx, fit_cy - cy) > max(3.0, mid * 0.25):
            break
        radii2, samples2 = radial_profile(white, fit_cx, fit_cy, r_max)
        new_score = ring_score(radii2, samples2, r_min)[0]
        if new_score <= score + 1e-3:
            break
        score, cx, cy = new_score, fit_cx, fit_cy
    radii, samples = radial_profile(white, cx, cy, r_max)
    band = find_band(radii, samples, r_min)
    if band is None:
        return None
    r_in, r_out, peak, _frac = band
    angles_deg, points = band_points(white, cx, cy, r_in, r_out)
    return {
        "center": (cx, cy),
        "r_in": r_in,
        "r_out": r_out,
        "mid": 0.5 * (r_in + r_out),
        "fill": len(points) / ANGLES,
        "band_frac": peak,
        "score": score,
        "runs": _white_runs(angles_deg),
    }


def _white_runs(angles_deg):
    """把白色角度整理成连续区间(度), 便于看"满/空/半圈"长什么样."""
    if len(angles_deg) == 0:
        return []
    step = 360.0 / ANGLES
    values = np.sort(angles_deg)
    runs = []
    start = prev = values[0]
    for value in values[1:]:
        if value - prev <= step * 1.5:
            prev = value
            continue
        runs.append((start, prev))
        start = prev = value
    runs.append((start, prev))
    if len(runs) > 1 and runs[0][0] <= step and runs[-1][1] >= 360.0 - step:
        first, last = runs.pop(0), runs.pop()
        runs.append((last[0], first[1] + 360.0))
    return runs


def cycle_full_ratio(img, top_span=12.0, inner=0.84, outer=0.99, white=200, step=0.25):
    """镜像 app 的 `cycle_ratio()`: 顶部(12点±top_span度)白环厚度 / 整圈中位厚度.

    未满时缺口在 12 点方向; "差一点点(98%)"时顶部虽然碰到白色, 但只有一条很薄的白边,
    所以比的是**厚度**而不是"有没有白像素"。整圈基本没白(空环)返回 0.0。
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape
    outer_r = min(width, height) / 2.0
    radii = np.arange(outer_r * inner, outer_r * outer, step)
    if radii.size < 2:
        return 0.0
    angles = np.arange(0.0, 360.0, 1.0)
    theta = np.radians(angles)
    xs = (width / 2.0 + np.outer(np.cos(theta), radii)).astype(np.float32)
    ys = (height / 2.0 + np.outer(np.sin(theta), radii)).astype(np.float32)
    samples = cv2.remap(gray, xs, ys, cv2.INTER_LINEAR)
    thickness = (samples >= white).sum(axis=1).astype(np.float64)
    median = float(np.median(thickness))
    if median < 1.0:
        return 0.0
    top = thickness[np.abs(angles - 270.0) <= top_span]
    return float(min(1.0, top.mean() / median))


def app_cycle_crop(image):
    """app 的 box_of_screen_scaled(2560,1440,944,1316,66,66) 等价裁剪."""
    height, width = image.shape[:2]
    x, y = int(944 / 2560 * width), int(1316 / 1440 * height)
    w, h = int(66 / 2560 * width), int(66 / 1440 * height)
    return image[y : y + h, x : x + w]


def report(result, image, label=""):
    if result is None:
        print(f"[ring] {label} no white ring found near that point")
        return 1
    height, width = image.shape[:2]
    cx, cy = result["center"]
    scale = 2560.0 / width
    print(f"[ring] {label}")
    print(f"  image {width}x{height} -> app space x{scale:.4f} (2560x1440)")
    print(f"  center ({cx:.1f}, {cy:.1f}) -> app ({cx * scale:.1f}, {cy * scale:.1f})")
    print(f"  inner r {result['r_in']:.1f}px -> app {result['r_in'] * scale:.1f}px")
    print(f"  outer r {result['r_out']:.1f}px -> app {result['r_out'] * scale:.1f}px")
    print(
        f"  thickness {result['r_out'] - result['r_in']:.1f}px, "
        f"mid r {result['mid']:.1f}px, ring score {result['score']:.2f}"
    )
    print(f"  white angle coverage {result['fill'] * 100:.1f}%")
    runs = ", ".join(f"{a:.0f}~{b:.0f}deg" for a, b in result["runs"][:8])
    print(f"  white runs: {runs}")
    ratio = cycle_full_ratio(app_cycle_crop(image))
    print(
        f"  cycle_ratio (app 同款裁剪) {ratio:.2f} -> "
        f"{'FULL' if ratio >= 0.5 else 'not full'}"
    )
    return 0


def annotate(image, result, zoom=2.0, pad=40):
    """画圆心/内外圈, 并返回 (整图, 放大裁剪图)."""
    out = image.copy()
    cx, cy = result["center"]
    for radius, color in ((result["r_in"], (0, 255, 255)), (result["r_out"], (0, 0, 255))):
        cv2.circle(out, (int(round(cx)), int(round(cy))), int(round(radius)), color, 1)
    cv2.drawMarker(out, (int(round(cx)), int(round(cy))), (0, 255, 0), cv2.MARKER_CROSS, 15, 1)
    half = int(max(result["r_out"] * 1.6, 20)) + pad
    x0, y0 = max(0, int(cx) - half), max(0, int(cy) - half)
    x1, y1 = min(image.shape[1], int(cx) + half), min(image.shape[0], int(cy) + half)
    crop = out[y0:y1, x0:x1]
    crop = cv2.resize(crop, None, fx=zoom, fy=zoom, interpolation=cv2.INTER_NEAREST)
    return out, crop


def run_gui(path, initial_center=None, r_min=MIN_RADIUS):
    import tkinter as tk
    from tkinter import filedialog, messagebox

    from PIL import Image, ImageTk

    image = cv2.imread(path)
    if image is None:
        raise SystemExit(f"cannot read image: {path}")
    white = whiteness(image)
    height, width = image.shape[:2]
    max_w, max_h = 1500, 800
    scale = min(1.0, max_w / width, max_h / height)
    shown = cv2.resize(image, (int(width * scale), int(height * scale)))

    root = tk.Tk()
    root.title(f"圆环检测 - {path}")
    canvas = tk.Canvas(root, width=shown.shape[1], height=shown.shape[0])
    canvas.pack()
    photo = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(shown, cv2.COLOR_BGR2RGB)))
    canvas.create_image(0, 0, anchor="nw", image=photo)

    info = tk.Label(root, text="点击圆环中心", font=("Consolas", 10), justify="left", anchor="w")
    info.pack(fill="x")
    zoom_label = tk.Label(root)
    zoom_label.pack()

    state = {"image": image.copy(), "photo": photo, "result": None}

    def detect_at(x_orig, y_orig):
        result = detect(white, x_orig, y_orig, r_max=min(width, height) * 0.12, r_min=r_min)
        if result is None:
            info.config(text="这个点附近没找到白色圆环, 换个位置再点")
            return
        state["result"] = result
        lines = [
            f"center ({result['center'][0]:.1f}, {result['center'][1]:.1f})",
            f"inner r {result['r_in']:.1f}px  outer r {result['r_out']:.1f}px "
            f"mid {result['mid']:.1f}px",
            f"white coverage {result['fill'] * 100:.1f}%  "
            f"runs " + ", ".join(f"{a:.0f}~{b:.0f}deg" for a, b in result["runs"][:6]),
            f"app(2560x1440): center ({result['center'][0] * 2560 / width:.1f}, "
            f"{result['center'][1] * 2560 / width:.1f}) "
            f"inner {result['r_in'] * 2560 / width:.1f} outer {result['r_out'] * 2560 / width:.1f}",
        ]
        info.config(text="\n".join(lines))
        out, crop = annotate(image, result)
        state["image"] = out
        shown_out = cv2.resize(out, (int(width * scale), int(height * scale)))
        state["photo"] = ImageTk.PhotoImage(
            Image.fromarray(cv2.cvtColor(shown_out, cv2.COLOR_BGR2RGB))
        )
        canvas.delete("all")
        canvas.create_image(0, 0, anchor="nw", image=state["photo"])
        crop_photo = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)))
        zoom_label.config(image=crop_photo)
        zoom_label.image = crop_photo

    def on_click(event):
        detect_at(event.x / scale, event.y / scale)

    canvas.bind("<Button-1>", on_click)

    def save():
        result = state["result"]
        if result is None:
            messagebox.showinfo("ring", "先点一下圆环中心")
            return
        out, _crop = annotate(image, result)
        target = filedialog.asksaveasfilename(defaultextension=".png")
        if target:
            cv2.imwrite(target, out)
            messagebox.showinfo("ring", f"saved {target}")

    tk.Button(root, text="保存标注图", command=save).pack(side="left")
    if initial_center:
        root.after(200, lambda: detect_at(*initial_center))
    root.mainloop()
    return 0


def main():
    parser = argparse.ArgumentParser(description="white ring inner/outer radius finder")
    parser.add_argument("image", nargs="?", default=DEFAULT_IMAGE)
    parser.add_argument("--center", default="", help="x,y in image pixels (headless mode)")
    parser.add_argument("--save", default="", help="write an annotated png and exit")
    parser.add_argument("--r-max", type=float, default=0.0, help="max search radius in px")
    parser.add_argument(
        "--r-min", type=float, default=MIN_RADIUS, help="ignore rings smaller than this (px)"
    )
    args = parser.parse_args()

    if not os.path.exists(args.image):
        print(f"[ring] image not found: {args.image}")
        return 1

    image = cv2.imread(args.image)
    if image is None:
        print(f"[ring] cannot read image: {args.image}")
        return 1
    height, width = image.shape[:2]
    r_max = args.r_max or min(width, height) * 0.12

    if not args.center:
        return run_gui(args.image, r_min=args.r_min)

    cx, cy = (float(value) for value in args.center.split(","))
    result = detect(whiteness(image), cx, cy, r_max, r_min=args.r_min)
    code = report(result, image, label=os.path.basename(args.image))
    if result is not None and args.save:
        out, crop = annotate(image, result)
        cv2.imwrite(args.save, out)
        cv2.imwrite(args.save[:-4] + "_zoom.png", crop)
        print(f"[ring] annotated -> {args.save}")
    return code


if __name__ == "__main__":
    sys.exit(main())
