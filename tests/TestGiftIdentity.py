import tempfile
import unittest
from pathlib import Path

import numpy as np

from src.gifts.GiftIdentity import (
    MATCH_THRESHOLD,
    GiftIconStore,
    gift_id_for_name,
    match_score,
    normalize_gift_name,
    suggest_gift_id,
)


def _icon(seed, size=(40, 98)):
    """随机纹理图标(纯色会让 TM_CCOEFF_NORMED 退化)."""
    rng = np.random.default_rng(seed)
    return rng.integers(0, 255, (size[0], size[1], 3), dtype=np.uint8)


class TestGiftName(unittest.TestCase):
    def test_normalize_strips_and_collapses(self):
        self.assertEqual(normalize_gift_name("  手写信  "), "手写信")
        self.assertEqual(normalize_gift_name("橙 礼\tA"), "橙 礼 A")
        self.assertEqual(normalize_gift_name(None), "")

    def test_normalize_uses_nfkc(self):
        self.assertEqual(normalize_gift_name("ＡＢ"), "AB")

    def test_same_name_same_id(self):
        self.assertEqual(gift_id_for_name("手写信"), gift_id_for_name("  手写信 "))
        self.assertEqual(gift_id_for_name("ＡＢ"), gift_id_for_name("AB"))

    def test_different_names_different_ids(self):
        self.assertNotEqual(gift_id_for_name("手写信"), gift_id_for_name("橙礼"))

    def test_blank_name_has_no_id(self):
        self.assertIsNone(gift_id_for_name(""))
        self.assertIsNone(gift_id_for_name("   "))
        self.assertIsNone(gift_id_for_name(None))


class TestMatchScore(unittest.TestCase):
    def test_identical_icon_scores_one(self):
        icon = _icon(1)
        self.assertAlmostEqual(match_score(icon, icon.copy()), 1.0, places=5)

    def test_shifted_icon_still_matches(self):
        icon = _icon(1)
        search = np.zeros((icon.shape[0] + 40, icon.shape[1] + 40, 3), dtype=np.uint8)
        search[20 : 20 + icon.shape[0], 20 : 20 + icon.shape[1]] = icon
        self.assertGreater(match_score(search, icon), 0.99)

    def test_different_icon_scores_low(self):
        self.assertLess(match_score(_icon(1), _icon(2)), MATCH_THRESHOLD)

    def test_incomparable_returns_negative(self):
        self.assertEqual(match_score(None, _icon(1)), -1.0)
        self.assertEqual(match_score(_icon(1), None), -1.0)
        self.assertEqual(match_score(_icon(1, (10, 10)), _icon(1)), -1.0)


class TestSuggestGiftId(unittest.TestCase):
    def test_suggests_known_gift_regardless_of_slot(self):
        templates = {"gift_a": _icon(1), "gift_b": _icon(2)}
        search = np.zeros((80, 140, 3), dtype=np.uint8)
        search[30:70, 40:138] = templates["gift_b"]
        self.assertEqual(suggest_gift_id(search, templates)[0], "gift_b")

    def test_unknown_icon_returns_none(self):
        self.assertIsNone(suggest_gift_id(_icon(3), {"gift_a": _icon(1)})[0])

    def test_empty_templates_returns_none(self):
        self.assertEqual(suggest_gift_id(_icon(1), {}), (None, -1.0))


class TestGiftIconStore(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = GiftIconStore(str(Path(self.temp_dir.name) / "gift_icons"))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_save_and_load_round_trip(self):
        icon = _icon(1)
        self.store.save("gift_a", icon)
        loaded = self.store.load_all()
        self.assertEqual(list(loaded), ["gift_a"])
        self.assertTrue(np.array_equal(loaded["gift_a"], icon))

    def test_load_missing_dir_is_empty(self):
        self.assertEqual(self.store.load_all(), {})

    def test_delete_removes_icon(self):
        self.store.save("gift_a", _icon(1))
        self.store.delete("gift_a")
        self.assertEqual(self.store.load_all(), {})
        self.store.delete("gift_a")

    def test_save_rejects_empty(self):
        with self.assertRaises(ValueError):
            self.store.save("gift_a", np.zeros((0, 0, 3), dtype=np.uint8))


if __name__ == "__main__":
    unittest.main()
