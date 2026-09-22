import json
import tempfile
import unittest
from pathlib import Path

from src.gifts import GiftDb


class TestGiftDbSchema(unittest.TestCase):
    def test_default_db_has_v2_sections(self):
        db = GiftDb.default_db()
        self.assertEqual(db["schema_version"], GiftDb.DB_SCHEMA_VERSION)
        self.assertEqual(db["profiles"], {})
        self.assertEqual(db["gift_catalog"], {})
        self.assertEqual(db["stock"], {})
        self.assertEqual(db["ledger"], {})
        self.assertEqual(db["settings"], GiftDb.default_settings())

    def test_normalize_settings_clamps_skill_level_and_limits(self):
        settings = GiftDb.normalize_settings(
            {
                "protagonist_skill_level": 99,
                "daily_gift_limit_per_char": 0,
                "daily_gift_limit_global": "7",
            }
        )
        self.assertEqual(settings["protagonist_skill_level"], GiftDb.MAX_SKILL_LEVEL)
        self.assertEqual(settings["daily_gift_limit_per_char"], 1)
        self.assertEqual(settings["daily_gift_limit_global"], 7)

    def test_normalize_settings_recovers_from_garbage(self):
        self.assertEqual(GiftDb.normalize_settings("nope"), GiftDb.default_settings())
        self.assertEqual(
            GiftDb.normalize_settings({"protagonist_skill_level": "bad"}),
            GiftDb.default_settings(),
        )

    def test_normalize_gift_ids_keeps_order_and_drops_duplicates(self):
        self.assertEqual(
            GiftDb.normalize_gift_ids(["gift_b", " gift_a ", "gift_b", "", None]),
            ["gift_b", "gift_a", "None"],
        )
        self.assertEqual(GiftDb.normalize_gift_ids("nope"), [])

    def test_normalize_catalog_keeps_exp_and_name(self):
        catalog = GiftDb.normalize_catalog(
            {
                "gift_a": {"exp": "400", "name": " 票券 "},
                "gift_b": {"exp": -5},
                "gift_c": "not-a-dict",
                "": {"exp": 100},
            }
        )
        self.assertEqual(
            catalog,
            {"gift_a": {"exp": 400, "name": "票券"}, "gift_b": {"exp": 0, "name": ""}},
        )

    def test_normalize_stock_and_ledger(self):
        self.assertEqual(
            GiftDb.normalize_stock({"gift_a": "3", "gift_b": -2}), {"gift_a": 3, "gift_b": 0}
        )
        ledger = GiftDb.normalize_ledger(
            {
                "2026-09-22": {"sent": {"gift_a": 2}, "characters": {"残虹": 3}},
                "bad": "not-a-dict",
            }
        )
        self.assertEqual(ledger, {"2026-09-22": {"sent": {"gift_a": 2}, "characters": {"残虹": 3}}})

    def test_normalize_profile_adds_bond_fields_and_validates_buy_tier(self):
        profile = GiftDb.normalize_profile(
            "gift_a",
            {
                "frame_id": "gift_a",
                "selected_slots": [1, 2],
                "priority_gift_ids": ["gift_x"],
                "bond_level": 8,
                "bond_exp": 1550,
                "target_level": 10,
                "buy_tier": 999,
                "daily_extra_exp": 200,
                "daily_extra_enabled": True,
            },
        )
        self.assertEqual(profile["priority_gift_ids"], ["gift_x"])
        self.assertEqual(profile["bond_level"], 8)
        self.assertEqual(profile["bond_exp"], 1550)
        self.assertEqual(profile["target_level"], 10)
        self.assertEqual(profile["buy_tier"], GiftDb.DEFAULT_BUY_TIER)
        self.assertTrue(profile["daily_extra_enabled"])

    def test_normalize_profile_clamps_bond_and_target_level(self):
        profile = GiftDb.normalize_profile(
            "gift_a",
            {"frame_id": "gift_a", "bond_level": 99, "target_level": 0, "buy_tier": 400},
        )
        self.assertEqual(profile["bond_level"], GiftDb.MAX_LEVEL)
        self.assertEqual(profile["target_level"], 1)
        self.assertEqual(profile["buy_tier"], 400)


class TestGiftDbMigration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "gift_configs" / "db.json")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_v1_db(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.db_path).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "profiles": {
                        "gift_old": {
                            "display_name": "残虹",
                            "frame_id": "gift_old",
                            "selected_slots": [7, 2],
                            "blocked_slots": [3],
                            "target_count": 3,
                            "enabled": True,
                        }
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_v1_database_upgrades_and_keeps_existing_profile(self):
        self._write_v1_db()
        db = GiftDb.load_db(self.db_path)

        self.assertEqual(db["schema_version"], GiftDb.DB_SCHEMA_VERSION)
        self.assertEqual(db["settings"], GiftDb.default_settings())
        self.assertEqual(db["gift_catalog"], {})
        self.assertEqual(db["stock"], {})
        self.assertEqual(db["ledger"], {})

        profile = db["profiles"]["gift_old"]
        self.assertEqual(profile["display_name"], "残虹")
        self.assertEqual(profile["selected_slots"], [7, 2])
        self.assertEqual(profile["blocked_slots"], [3])
        self.assertEqual(profile["priority_gift_ids"], [])
        self.assertEqual(profile["bond_level"], 0)
        self.assertEqual(profile["buy_tier"], GiftDb.DEFAULT_BUY_TIER)
        self.assertFalse(profile["daily_extra_enabled"])

    def test_validate_db_reports_change_for_v1(self):
        db = GiftDb.default_db()
        db["schema_version"] = 1
        self.assertTrue(GiftDb.validate_db(db))
        self.assertFalse(GiftDb.validate_db(db))

    def test_saved_v2_database_round_trips(self):
        self._write_v1_db()
        db = GiftDb.load_db(self.db_path)
        db["settings"]["protagonist_skill_level"] = 5
        db["gift_catalog"]["gift_a"] = {"exp": 400, "name": "票券"}
        db["stock"]["gift_a"] = 2
        db["ledger"]["2026-09-22"] = {"sent": {"gift_a": 2}, "characters": {"残虹": 3}}
        GiftDb.save_db(self.db_path, db)

        reloaded = GiftDb.load_db(self.db_path)
        self.assertEqual(reloaded["settings"]["protagonist_skill_level"], 5)
        self.assertEqual(reloaded["gift_catalog"]["gift_a"], {"exp": 400, "name": "票券"})
        self.assertEqual(reloaded["stock"], {"gift_a": 2})
        self.assertEqual(reloaded["ledger"]["2026-09-22"]["sent"], {"gift_a": 2})

    def test_invalid_json_recovers_to_default(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.db_path).write_text("{not valid json", encoding="utf-8")
        self.assertEqual(GiftDb.load_db(self.db_path), GiftDb.default_db())


if __name__ == "__main__":
    unittest.main()
