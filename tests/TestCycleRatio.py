import os
import unittest

import cv2
import numpy as np

from src.combat.BaseCombatTask import BaseCombatTask

SIZE = 49
RING_THICKNESS = 3


def _ring_crop(fill: float, size: int = SIZE) -> np.ndarray:
    """合成环合条裁剪图: 暗环 + 从底部(90 度)向两侧对称上涨的白色填充.

    游戏里的环合就是这样涨的(像倒水), 未满时缺口一定在 12 点方向。
    "差一点点(98%)"那种顶部只剩亚像素白边的状态合成图还原不了, 用真实截图用例覆盖。
    """
    img = np.full((size, size, 3), 150, dtype=np.uint8)
    center = (size // 2, size // 2)
    outer = size // 2 - 1
    cv2.circle(img, center, outer, (40, 40, 40), -1)
    cv2.circle(img, center, int(outer * 0.87), (150, 150, 150), -1)
    mid = int(outer * 0.93)
    thickness = RING_THICKNESS if size == SIZE else max(1, size // 16)
    white = (238, 238, 238)
    if fill >= 1.0:
        cv2.circle(img, center, mid, white, thickness)
        return img
    half = fill * 180.0
    cv2.ellipse(img, center, (mid, mid), 0, 90 - half, 90 + half, white, thickness)
    return img


def _app_crop(path: str) -> np.ndarray:
    """等价于 app 的 box_of_screen_scaled(2560,1440,944,1316,66,66).crop_frame()."""
    img = cv2.imread(path)
    height, width = img.shape[:2]
    x, y = int(944 / 2560 * width), int(1316 / 1440 * height)
    w, h = int(66 / 2560 * width), int(66 / 1440 * height)
    return img[y : y + h, x : x + w]


class TestCycleRatio(unittest.TestCase):
    """环合条判满: 顶部白环厚度 / 整圈中位厚度.

    实测(2026-09-17): 目测 40%/75% 顶部整段是暗的; 目测 98% 顶部虽然碰到白色,
    但只剩一条 ~0.3px 的白边(整圈 ~2.5px); 满环顶部厚度与整圈一致。
    """

    def test_partial_fill_is_not_full(self):
        for fill in (0.0, 0.4, 0.75, 0.9):
            with self.subTest(fill=fill):
                ratio = BaseCombatTask._cycle_top_thickness_ratio(_ring_crop(fill))
                self.assertLess(ratio, BaseCombatTask.CYCLE_FULL_RATIO)

    def test_closed_ring_is_full(self):
        ratio = BaseCombatTask._cycle_top_thickness_ratio(_ring_crop(1.0))
        self.assertGreaterEqual(ratio, BaseCombatTask.CYCLE_FULL_RATIO)

    def test_dark_ring_is_not_white(self):
        self.assertEqual(BaseCombatTask._cycle_top_thickness_ratio(_ring_crop(0.0)), 0.0)

    def test_tiny_image_is_safe(self):
        self.assertEqual(
            BaseCombatTask._cycle_top_thickness_ratio(np.zeros((4, 4, 3), np.uint8)), 0.0
        )

    def test_real_screenshots(self):
        """真实截图回归: 0.98.png 不算满, 1.png 算满.

        screenshots/ 不进仓库; 本地放好截图就会跑, 否则跳过。
        """
        base = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "screenshots"
        )
        almost = os.path.join(base, "0.98.png")
        full = os.path.join(base, "1.png")
        if not (os.path.exists(almost) and os.path.exists(full)):
            self.skipTest("screenshots/0.98.png or screenshots/1.png not present")

        almost_ratio = BaseCombatTask._cycle_top_thickness_ratio(_app_crop(almost))
        full_ratio = BaseCombatTask._cycle_top_thickness_ratio(_app_crop(full))
        self.assertLess(almost_ratio, BaseCombatTask.CYCLE_FULL_RATIO)
        self.assertGreaterEqual(full_ratio, BaseCombatTask.CYCLE_FULL_RATIO)


if __name__ == "__main__":
    unittest.main()
