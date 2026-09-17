import unittest

import cv2
import numpy as np

from src.combat.BaseCombatTask import BaseCombatTask


def _ring_crop(fill: float, size: int = 50) -> np.ndarray:
    """合成环合条裁剪图: 暗环 + 从底部(90 度)向两侧对称上涨的白色填充.

    游戏里的环合就是这样涨的(像倒水), 未满时缺口一定在 12 点方向。
    """
    img = np.full((size, size, 3), 150, dtype=np.uint8)
    center = (size // 2, size // 2)
    cv2.circle(img, center, 24, (40, 40, 40), -1)
    cv2.circle(img, center, 20, (150, 150, 150), -1)
    if fill > 0:
        half = fill * 180.0
        cv2.ellipse(img, center, (22, 22), 0, 90 - half, 90 + half, (238, 238, 238), -1)
    return img


class TestCycleRatio(unittest.TestCase):
    """环合条判满: 只看 12 点方向有没有缺口(实测 0.4/0.75 两张截图顶部都是暗的)."""

    def test_top_gap_means_not_full(self):
        for fill in (0.0, 0.4, 0.75, 0.9):
            with self.subTest(fill=fill):
                ratio = BaseCombatTask._cycle_top_white_fraction(_ring_crop(fill))
                self.assertLess(ratio, 0.5)
                self.assertFalse(ratio > 0.5)

    def test_closed_ring_is_full(self):
        self.assertGreater(BaseCombatTask._cycle_top_white_fraction(_ring_crop(1.0)), 0.9)

    def test_dark_ring_is_not_white(self):
        # 空环(暗灰 40)不能被当成白
        self.assertEqual(BaseCombatTask._cycle_top_white_fraction(_ring_crop(0.0)), 0.0)

    def test_tiny_image_is_safe(self):
        self.assertEqual(
            BaseCombatTask._cycle_top_white_fraction(np.zeros((4, 4, 3), np.uint8)), 0.0
        )


if __name__ == "__main__":
    unittest.main()
