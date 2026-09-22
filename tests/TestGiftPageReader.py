import importlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np

from src.gifts.GiftIdentity import GiftIconStore, gift_id_for_name
from src.gifts.GiftPageReader import (
    BondReading,
    DailyCountReading,
    GiftPageReader,
    GiftPageSnapshot,
    GiftSlotReading,
    crop_region,
    gift_badge_box,
    gift_exp_box,
    gift_slot_boxes,
    label_slots,
    merge_snapshot_into_catalog,
    migrate_priority_gift_ids,
    parse_bond_progress,
    parse_gift_counter,
    parse_global_remaining,
    parse_int,
    special_badge_box,
)
from src.gifts.layout import GIFT_LAYOUT as L
from src.Labels import Labels

WIDTH, HEIGHT = 1920, 1080

gift_reader_module = importlib.import_module("src.gifts.GiftPageReader")


def _same(a, b, tol=1e-6):
    return len(a) == len(b) and all(abs(x - y) < tol for x, y in zip(a, b))


def _frame():
    rng = np.random.default_rng(20260922)
    frame = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    for index, box in enumerate(gift_slot_boxes(WIDTH, HEIGHT)):
        x1, y1 = int(WIDTH * box[0]), int(HEIGHT * box[1])
        x2, y2 = int(WIDTH * box[2]), int(HEIGHT * box[3])
        frame[y1:y2, x1:x2] = rng.integers(0, 255, (y2 - y1, x2 - x1, 3), dtype=np.uint8)
    return frame


class _StubBox:
    def __init__(self, name):
        self.name = name


class _StubTask:
    def __init__(self, frame, texts=None, special_slots=(), fail_ocr=False):
        self.frame = frame
        self.texts = dict(texts or {})
        self.special_slots = set(special_slots)
        self.fail_ocr = fail_ocr

    def ocr(self, x1, y1, x2, y2, frame=None, match=None, **kwargs):
        if self.fail_ocr:
            raise RuntimeError("ocr exploded")
        region = (x1, y1, x2, y2)
        for key, text in self.texts.items():
            if _same(key, region):
                return [_StubBox(text)]
        return []

    def find_one(self, name, box=None, frame=None, **kwargs):
        if name != Labels.unlimit_gift or box is None:
            return None
        for index, slot_box in enumerate(gift_slot_boxes(WIDTH, HEIGHT)):
            if index in self.special_slots and _same(special_badge_box(slot_box), box):
                return _StubBox("special")
        return None


def _slot_texts(exps=None, stocks=None):
    texts = {}
    for index, slot_box in enumerate(gift_slot_boxes(WIDTH, HEIGHT)):
        if exps and exps.get(index) is not None:
            texts[gift_exp_box(slot_box)] = str(exps[index])
        if stocks and stocks.get(index) is not None:
            texts[gift_badge_box(slot_box)] = str(stocks[index])
    return texts


class TestParsers(unittest.TestCase):
    def test_parse_bond_progress(self):
        self.assertEqual(parse_bond_progress("1550/12000"), (1550, 12000))
        self.assertEqual(parse_bond_progress(" 500 / 2000 "), (500, 2000))
        self.assertIsNone(parse_bond_progress("无"))
        self.assertIsNone(parse_bond_progress(None))

    def test_parse_int(self):
        self.assertEqual(parse_int("8"), 8)
        self.assertEqual(parse_int("等级 10"), 10)
        self.assertIsNone(parse_int(""))
        self.assertIsNone(parse_int(None))

    def test_parse_gift_counter_first_number_is_remaining(self):
        reading = parse_gift_counter("赠送3/3")
        self.assertEqual((reading.remaining, reading.limit, reading.used), (3, 3, 0))
        reading = parse_gift_counter("1/3")
        self.assertEqual((reading.remaining, reading.limit, reading.used), (1, 3, 2))
        self.assertIsNone(parse_gift_counter("赠送"))

    def test_parse_global_remaining(self):
        self.assertEqual(parse_global_remaining("今日还能赠送10次礼物"), 10)
        self.assertIsNone(parse_global_remaining("该礼物为特殊礼物，不进入每日赠礼次数限制"))


class TestGeometry(unittest.TestCase):
    def test_slot_boxes_are_5_by_2(self):
        boxes = gift_slot_boxes(WIDTH, HEIGHT)
        self.assertEqual(len(boxes), 10)
        self.assertEqual(L.gift_columns, 5)
        self.assertEqual(L.gift_rows, 2)
        self.assertEqual(boxes[0][0], boxes[5][0])
        self.assertEqual(boxes[0][1], boxes[1][1])
        self.assertGreater(boxes[5][1], boxes[0][1])

    def test_exp_and_badge_boxes_are_below_the_icon(self):
        slot = gift_slot_boxes(WIDTH, HEIGHT)[0]
        self.assertGreater(gift_exp_box(slot)[1], slot[3])
        self.assertGreater(gift_badge_box(slot)[0], slot[0])

    def test_crop_region_clips_out_of_range(self):
        frame = np.zeros((100, 200, 3), dtype=np.uint8)
        self.assertEqual(crop_region(frame, (0.9, 0.9, 1.5, 1.5)).shape, (10, 20, 3))
        self.assertEqual(crop_region(frame, (0.0, 0.0, 0.5, 0.5)).shape, (50, 100, 3))
        self.assertIsNone(crop_region(None, (0, 0, 1, 1)))


class TestLabeling(unittest.TestCase):
    def _slots(self):
        return tuple(GiftSlotReading(slot_index=index) for index in range(10))

    def test_label_slots_applies_mapping(self):
        snapshot = GiftPageSnapshot(slots=self._slots())
        labeled = label_slots(snapshot, {0: "gift_a", "3": "gift_b"})
        self.assertEqual(labeled.slot(0).gift_id, "gift_a")
        self.assertEqual(labeled.slot(3).gift_id, "gift_b")
        self.assertIsNone(labeled.slot(1).gift_id)

    def test_label_slots_empty_mapping_keeps_none(self):
        snapshot = GiftPageSnapshot(slots=self._slots())
        self.assertTrue(all(reading.gift_id is None for reading in label_slots(snapshot, {}).slots))

    def test_migration_preserves_order(self):
        mapping = {2: "gift_x", 5: "gift_y", 7: "gift_z"}
        self.assertEqual(
            migrate_priority_gift_ids([7, 2, 5], mapping), ["gift_z", "gift_x", "gift_y"]
        )

    def test_migration_fails_when_a_slot_is_unlabeled(self):
        self.assertIsNone(migrate_priority_gift_ids([7, 2], {2: "gift_x"}))

    def test_migration_empty_selection(self):
        self.assertEqual(migrate_priority_gift_ids([], {}), [])


class TestCatalogMerge(unittest.TestCase):
    def _snapshot(self, *slots):
        return GiftPageSnapshot(slots=tuple(slots))

    def test_new_gift_is_added(self):
        snapshot = self._snapshot(GiftSlotReading(0, gift_id="gift_a", base_exp=400))
        updates, conflicts = merge_snapshot_into_catalog(snapshot, {})
        self.assertEqual([(u.gift_id, u.exp) for u in updates], [("gift_a", 400)])
        self.assertEqual(conflicts, [])

    def test_existing_gift_with_same_exp_is_ok(self):
        snapshot = self._snapshot(GiftSlotReading(0, gift_id="gift_a", base_exp=400))
        updates, conflicts = merge_snapshot_into_catalog(snapshot, {"gift_a": {"exp": 400}})
        self.assertEqual(len(updates), 1)
        self.assertEqual(conflicts, [])

    def test_exp_conflict_is_reported_not_overwritten(self):
        snapshot = self._snapshot(GiftSlotReading(0, gift_id="gift_a", base_exp=200))
        updates, conflicts = merge_snapshot_into_catalog(snapshot, {"gift_a": {"exp": 100}})
        self.assertEqual(updates, [])
        self.assertEqual(len(conflicts), 1)
        self.assertIn("existing exp=100", conflicts[0])
        self.assertIn("observed exp=200", conflicts[0])

    def test_special_and_unread_slots_are_skipped(self):
        snapshot = self._snapshot(
            GiftSlotReading(0, gift_id="gift_a", base_exp=400, not_giftable=True),
            GiftSlotReading(1, gift_id="gift_b", base_exp=None),
            GiftSlotReading(2, gift_id=None, base_exp=100),
        )
        updates, conflicts = merge_snapshot_into_catalog(snapshot, {})
        self.assertEqual(updates, [])
        self.assertEqual(conflicts, [])


class TestGiftPageReader(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = GiftIconStore(str(Path(self.temp_dir.name) / "icons"))
        self.frame = _frame()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _reader(self, task):
        return GiftPageReader(task, icon_store=self.store)

    def test_read_bond(self):
        task = _StubTask(
            self.frame, {L.bond_level_box: "8", L.bond_progress_box: "1550/12000"}
        )
        expected = BondReading(level=8, exp=1550, required_exp=12000)
        self.assertEqual(self._reader(task).read_bond(self.frame), expected)

    def test_read_bond_failure_is_none(self):
        self.assertEqual(self._reader(_StubTask(self.frame)).read_bond(self.frame), BondReading())

    def test_read_counts(self):
        task = _StubTask(
            self.frame,
            {L.gift_counter_button_box: "赠送3/3", L.daily_banner_box: "今日还能赠送10次礼物"},
        )
        character, global_count = self._reader(task).read_counts(self.frame)
        self.assertEqual((character.remaining, character.limit, character.used), (3, 3, 0))
        self.assertEqual(global_count.remaining, 10)

    def test_read_counts_failure_is_none(self):
        character, global_count = self._reader(_StubTask(self.frame)).read_counts(self.frame)
        self.assertEqual(character, DailyCountReading())
        self.assertEqual(global_count, DailyCountReading())

    def test_read_slots_exp_and_stock(self):
        task = _StubTask(self.frame, _slot_texts(exps={0: 400, 1: 100}, stocks={0: 2, 1: 28}))
        slots = self._reader(task).read_slots(self.frame)
        self.assertEqual(len(slots), 10)
        self.assertEqual((slots[0].base_exp, slots[0].stock), (400, 2))
        self.assertEqual((slots[1].base_exp, slots[1].stock), (100, 28))

    def test_unreadable_stock_stays_none(self):
        slots = self._reader(_StubTask(self.frame)).read_slots(self.frame)
        self.assertIsNone(slots[0].stock)
        self.assertIsNone(slots[0].base_exp)

    def test_special_slot_is_flagged(self):
        slots = self._reader(_StubTask(self.frame, special_slots={0})).read_slots(self.frame)
        self.assertTrue(slots[0].not_giftable)
        self.assertFalse(slots[1].not_giftable)

    def test_read_reports_errors_when_ocr_fails(self):
        with patch.object(gift_reader_module.logger, "info"):
            snapshot = self._reader(_StubTask(self.frame, fail_ocr=True)).read(self.frame)
        self.assertEqual(snapshot.bond, BondReading())
        self.assertEqual(snapshot.character_count, DailyCountReading())
        self.assertTrue(any("羁遇等级" in message for message in snapshot.errors))
        self.assertTrue(any("礼物经验" in message for message in snapshot.errors))

    def test_read_without_frame(self):
        snapshot = self._reader(_StubTask(None)).read(None)
        self.assertEqual(snapshot.errors, ("没有可用的游戏画面",))

    def test_read_applies_user_labels(self):
        reader = self._reader(_StubTask(self.frame))
        snapshot = reader.read(self.frame, slot_gift_ids={0: "gift_a", 4: "gift_b"})
        self.assertEqual(snapshot.slot(0).gift_id, "gift_a")
        self.assertEqual(snapshot.slot(4).gift_id, "gift_b")
        self.assertIsNone(snapshot.slot(1).gift_id)

    def test_learn_labels_saves_templates_for_labeled_slots_only(self):
        reader = self._reader(_StubTask(self.frame))
        slots = reader.read_slots(self.frame)
        labeled = [
            replace(slots[0], gift_id="gift_a"),
            replace(slots[1], gift_id="gift_b"),
            slots[2],
        ]
        reader.learn_labels(labeled)
        self.assertEqual(sorted(self.store.load_all()), ["gift_a", "gift_b"])

    def test_suggest_labels_inherits_previously_learned_labels(self):
        reader = self._reader(_StubTask(self.frame))
        slots = reader.read_slots(self.frame)
        reader.learn_labels([replace(slots[0], gift_id="gift_a")])

        # 同一帧再次读取: slot 0 应被建议继承 gift_a, 其它格子没有建议
        suggested = reader.suggest_labels(reader.read_slots(self.frame))
        self.assertEqual(suggested[0].suggested_gift_id, "gift_a")
        self.assertGreater(suggested[0].match_score, 0.8)
        self.assertIsNone(suggested[1].suggested_gift_id)

    def test_special_slots_are_not_suggested(self):
        reader = self._reader(_StubTask(self.frame, special_slots={0}))
        slots = reader.read_slots(self.frame)
        suggested = reader.suggest_labels(slots)
        self.assertIsNone(suggested[0].suggested_gift_id)

    def test_gift_id_for_name_used_by_labels(self):
        self.assertEqual(gift_id_for_name("票券"), gift_id_for_name(" 票券 "))


if __name__ == "__main__":
    unittest.main()
