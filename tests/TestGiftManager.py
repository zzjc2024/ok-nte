import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from ok import Box

from src.gifts import GiftDb

gift_manager_module = importlib.import_module("src.gifts.GiftManager")
GiftManager = gift_manager_module.GiftManager


class TestGiftManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name) / "gift_configs"
        self.patches = [
            patch.object(gift_manager_module, "GIFT_CONFIG_DIR", str(root)),
            patch.object(gift_manager_module, "FRAMES_DIR", str(root / "frames")),
            patch.object(gift_manager_module, "DB_PATH", str(root / "db.json")),
        ]
        for mocked in self.patches:
            mocked.start()
        GiftManager._instance = None
        self.manager = GiftManager()

    def tearDown(self):
        GiftManager._instance = None
        for mocked in reversed(self.patches):
            mocked.stop()
        self.temp_dir.cleanup()

    def test_create_profile_persists_full_frame_and_priority(self):
        frame = np.full((120, 240, 3), 37, dtype=np.uint8)
        profile_id = self.manager.create_profile("安魂曲", frame, [7, 2, 7, 15], target_count=9)

        profile = self.manager.get_profile(profile_id)
        self.assertEqual(profile["display_name"], "安魂曲")
        self.assertEqual(profile["selected_slots"], [7, 2])
        self.assertEqual(profile["target_count"], 3)
        self.assertNotIn("width", profile)
        self.assertNotIn("height", profile)
        self.assertTrue((Path(gift_manager_module.FRAMES_DIR) / f"{profile_id}.png").exists())
        self.assertTrue(np.array_equal(self.manager.load_frame(profile_id), frame))

        GiftManager._instance = None
        reloaded = GiftManager()
        self.assertEqual(reloaded.get_profile(profile_id), profile)

    def test_saved_frame_blacks_out_configured_blur_area(self):
        frame = np.full((20, 30, 3), 255, dtype=np.uint8)
        with patch.object(
            gift_manager_module.og,
            "config",
            {"blur_area": lambda _width, _height: Box(3, 5, 7, 4)},
        ):
            profile_id = self.manager.create_profile("角色", frame, [0])

        saved = self.manager.load_frame(profile_id)
        self.assertTrue(np.all(saved[5:9, 3:10] == 0))
        self.assertTrue(np.all(frame == 255))

    def test_delete_profile_removes_frame(self):
        profile_id = self.manager.create_profile("角色", np.zeros((20, 30, 3), dtype=np.uint8), [0])
        frame_path = Path(gift_manager_module.FRAMES_DIR) / f"{profile_id}.png"
        self.manager.delete_profile(profile_id)

        self.assertIsNone(self.manager.get_profile(profile_id))
        self.assertFalse(frame_path.exists())

    def test_capture_can_be_configured_inline_after_saving(self):
        profile_id = self.manager.create_profile(
            "未命名角色", np.zeros((20, 30, 3), dtype=np.uint8), []
        )
        self.assertEqual(self.manager.get_enabled_profiles(), {})

        self.manager.update_profile(profile_id, selected_slots=[4, 1, 4])
        self.assertEqual(self.manager.get_profile(profile_id)["selected_slots"], [4, 1])
        self.assertIn(profile_id, self.manager.get_enabled_profiles())

    def test_blocked_slots_cannot_be_selected(self):
        profile_id = self.manager.create_profile(
            "角色", np.zeros((20, 30, 3), dtype=np.uint8), [0, 1], blocked_slots=[1]
        )
        self.assertEqual(self.manager.get_profile(profile_id)["selected_slots"], [0])
        self.assertEqual(self.manager.get_profile(profile_id)["blocked_slots"], [1])

        self.manager.update_profile(profile_id, selected_slots=[1, 2])
        self.assertEqual(self.manager.get_profile(profile_id)["selected_slots"], [2])

    def test_invalid_database_is_recovered(self):
        db_path = Path(gift_manager_module.DB_PATH)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.write_text("{not valid json", encoding="utf-8")

        loaded = GiftDb.load_db(str(db_path))
        self.assertEqual(loaded, GiftDb.default_db())

    def test_slot_normalization_and_layout(self):
        self.assertEqual(GiftDb.normalize_slots([0, "1", 0, -1, 10, "bad"]), [0, 1])

    # ------------------------------------------------- v2 settings/catalog/stock/ledger

    def test_settings_round_trip(self):
        self.assertEqual(self.manager.get_settings(), GiftDb.default_settings())
        settings = self.manager.update_settings(protagonist_skill_level=5, unknown_key=1)
        self.assertEqual(settings["protagonist_skill_level"], 5)
        self.assertNotIn("unknown_key", settings)
        self.assertEqual(self.manager.get_settings()["protagonist_skill_level"], 5)

    def test_catalog_upsert_keeps_existing_entry(self):
        self.manager.upsert_gift("gift_a", 400, "票券")
        self.manager.upsert_gift("gift_a", 400)
        self.assertEqual(self.manager.get_catalog()["gift_a"], {"exp": 400, "name": "票券"})
        with self.assertRaises(ValueError):
            self.manager.upsert_gift("  ", 100)

    def test_stock_snapshot_round_trip(self):
        self.manager.set_stock({"gift_a": 2, "gift_b": -1}, snapshot_at="2026-09-22T19:56")
        self.assertEqual(self.manager.get_stock(), {"gift_a": 2, "gift_b": 0})
        self.assertEqual(self.manager.get_stock_snapshot_at(), "2026-09-22T19:56")

    def test_record_sent_merges_same_day(self):
        self.manager.record_sent("2026-09-22", {"gift_a": 2}, {"残虹": 3})
        entry = self.manager.record_sent("2026-09-22", {"gift_a": 1, "gift_b": 1}, {"残虹": 1})
        self.assertEqual(entry["sent"], {"gift_a": 3, "gift_b": 1})
        self.assertEqual(entry["characters"], {"残虹": 4})
        self.assertEqual(self.manager.get_ledger()["2026-09-22"], entry)
        with self.assertRaises(ValueError):
            self.manager.record_sent("  ")

    def test_record_sent_prunes_old_days(self):
        with patch.object(gift_manager_module, "LEDGER_MAX_DAYS", 2):
            self.manager.record_sent("2026-09-20")
            self.manager.record_sent("2026-09-21")
            self.manager.record_sent("2026-09-22")
        self.assertEqual(sorted(self.manager.get_ledger()), ["2026-09-21", "2026-09-22"])

    # ------------------------------------------------------------- v2 profile fields

    def test_create_profile_fills_v2_defaults(self):
        profile_id = self.manager.create_profile("角色", np.zeros((20, 30, 3), dtype=np.uint8), [0])
        profile = self.manager.get_profile(profile_id)
        self.assertEqual(profile["priority_gift_ids"], [])
        self.assertEqual(profile["bond_level"], 0)
        self.assertEqual(profile["target_level"], GiftDb.DEFAULT_TARGET_LEVEL)
        self.assertEqual(profile["buy_tier"], GiftDb.DEFAULT_BUY_TIER)
        self.assertFalse(profile["daily_extra_enabled"])

    def test_update_profile_stores_bond_and_priority_fields(self):
        profile_id = self.manager.create_profile("角色", np.zeros((20, 30, 3), dtype=np.uint8), [0])
        self.manager.update_profile(
            profile_id,
            priority_gift_ids=["gift_a", "gift_a", "gift_b"],
            bond_level=8,
            bond_exp=1550,
            target_level=10,
            buy_tier=400,
            daily_extra_exp=200,
            daily_extra_enabled=True,
        )
        profile = self.manager.get_profile(profile_id)
        self.assertEqual(profile["priority_gift_ids"], ["gift_a", "gift_b"])
        self.assertEqual(profile["bond_level"], 8)
        self.assertEqual(profile["bond_exp"], 1550)
        self.assertEqual(profile["buy_tier"], 400)
        self.assertTrue(profile["daily_extra_enabled"])

    def test_update_profile_rejects_invalid_buy_tier(self):
        profile_id = self.manager.create_profile("角色", np.zeros((20, 30, 3), dtype=np.uint8), [0])
        self.manager.update_profile(profile_id, buy_tier=999)
        self.assertEqual(self.manager.get_profile(profile_id)["buy_tier"], GiftDb.DEFAULT_BUY_TIER)

    def test_manager_migrates_v1_database(self):
        GiftManager._instance = None
        db_path = Path(gift_manager_module.DB_PATH)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.write_text(json.dumps({"schema_version": 1, "profiles": {}}), encoding="utf-8")
        manager = GiftManager()
        self.assertEqual(manager.db["schema_version"], GiftDb.DB_SCHEMA_VERSION)
        self.assertEqual(manager.get_settings(), GiftDb.default_settings())
