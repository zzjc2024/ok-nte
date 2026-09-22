import os
import uuid
from pathlib import Path
from threading import Lock, RLock

import cv2
import numpy as np
from ok import Logger, og

from src.gifts import GiftDb

logger = Logger.get_logger(__name__)

GIFT_CONFIG_DIR = "gift_configs"
FRAMES_DIR = os.path.join(GIFT_CONFIG_DIR, "frames")
DB_PATH = os.path.join(GIFT_CONFIG_DIR, "db.json")
LEDGER_MAX_DAYS = 365


class GiftManager:
    """Owns user-local gift captures, inventory snapshots and the daily ledger."""

    _instance = None
    _lock = Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if getattr(self, "initialized", False):
            return
        self._data_lock = RLock()
        os.makedirs(FRAMES_DIR, exist_ok=True)
        self.db = GiftDb.load_db(DB_PATH, logger)
        self.initialized = True

    @staticmethod
    def _normalize_capture(frame) -> np.ndarray:
        if not isinstance(frame, np.ndarray) or frame.size == 0:
            raise ValueError("Capture frame is empty")
        if frame.ndim not in (2, 3):
            raise ValueError("Capture frame has an unsupported shape")
        return frame.copy()

    @staticmethod
    def _normalized_slots(selected_slots, *, required=False) -> list[int]:
        slots = GiftDb.normalize_slots(selected_slots)
        if required and not slots:
            raise ValueError("At least one gift slot must be selected")
        return slots

    @staticmethod
    def _normalized_target_count(target_count) -> int:
        try:
            return min(3, max(1, int(target_count)))
        except (TypeError, ValueError):
            return 3

    @staticmethod
    def _merge_counts(existing, added) -> dict:
        counts = {}
        for source in (existing, added):
            if not isinstance(source, dict):
                continue
            for key, value in source.items():
                key = str(key).strip()
                if not key:
                    continue
                try:
                    counts[key] = counts.get(key, 0) + int(value)
                except (TypeError, ValueError):
                    continue
        return counts

    def _frame_path(self, frame_id: str) -> Path:
        return Path(FRAMES_DIR) / f"{frame_id}.png"

    def _write_frame(self, frame_id: str, frame) -> None:
        path = self._frame_path(frame_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        frame = frame.copy()
        config = getattr(og, "config", None)
        blur_area = config.get("blur_area") if config else None
        if blur_area:
            blur_box = blur_area(frame.shape[1], frame.shape[0])
            frame[
                blur_box.y : blur_box.y + blur_box.height, blur_box.x : blur_box.x + blur_box.width
            ] = 0
        if not cv2.imwrite(str(path), frame):
            raise IOError(f"Failed to write gift capture: {path}")

    def save(self) -> None:
        with self._data_lock:
            GiftDb.save_db(DB_PATH, self.db, logger)

    # ------------------------------------------------------------- settings

    def get_settings(self) -> dict:
        with self._data_lock:
            return self.db["settings"].copy()

    def update_settings(self, **changes) -> dict:
        with self._data_lock:
            merged = {**self.db["settings"], **changes}
            self.db["settings"] = GiftDb.normalize_settings(merged)
            self.save()
            return self.db["settings"].copy()

    # --------------------------------------------------------- gift catalog

    def get_catalog(self) -> dict[str, dict]:
        with self._data_lock:
            return {gift_id: entry.copy() for gift_id, entry in self.db["gift_catalog"].items()}

    def upsert_gift(self, gift_id: str, exp, name: str = "") -> None:
        gift_id = str(gift_id).strip()
        if not gift_id:
            raise ValueError("Gift id must not be empty")
        with self._data_lock:
            catalog = {key: dict(value) for key, value in self.db["gift_catalog"].items()}
            entry = catalog.setdefault(gift_id, {})
            entry["exp"] = exp
            if name:
                entry["name"] = name
            self.db["gift_catalog"] = GiftDb.normalize_catalog(catalog)
            self.save()

    # ------------------------------------------------------------- inventory

    def get_stock(self) -> dict[str, int]:
        with self._data_lock:
            return dict(self.db["stock"])

    def get_stock_snapshot_at(self) -> str:
        with self._data_lock:
            return self.db.get("stock_snapshot_at", "")

    def set_stock(self, stock: dict, snapshot_at: str | None = None) -> None:
        with self._data_lock:
            self.db["stock"] = GiftDb.normalize_stock(stock)
            if snapshot_at is not None:
                self.db["stock_snapshot_at"] = str(snapshot_at)
            self.save()

    # ---------------------------------------------------------------- ledger

    def get_ledger(self) -> dict[str, dict]:
        with self._data_lock:
            return {
                date: {
                    "sent": dict(entry["sent"]),
                    "characters": dict(entry["characters"]),
                }
                for date, entry in self.db["ledger"].items()
            }

    def record_sent(self, date: str, sent=None, characters=None) -> dict:
        """Merge one run's consumption into the daily ledger entry."""
        date = str(date).strip()
        if not date:
            raise ValueError("Ledger date must not be empty")
        with self._data_lock:
            ledger = {key: dict(value) for key, value in self.db["ledger"].items()}
            entry = ledger.get(date) or {"sent": {}, "characters": {}}
            ledger[date] = {
                "sent": self._merge_counts(entry.get("sent"), sent),
                "characters": self._merge_counts(entry.get("characters"), characters),
            }
            if len(ledger) > LEDGER_MAX_DAYS:
                for stale in sorted(ledger)[:-LEDGER_MAX_DAYS]:
                    ledger.pop(stale, None)
            self.db["ledger"] = GiftDb.normalize_ledger(ledger)
            self.save()
            return dict(self.db["ledger"][date])

    # -------------------------------------------------------------- profiles

    def get_profiles(self) -> dict[str, dict]:
        with self._data_lock:
            return {
                profile_id: profile.copy() for profile_id, profile in self.db["profiles"].items()
            }

    def get_profile(self, profile_id: str) -> dict | None:
        with self._data_lock:
            profile = self.db["profiles"].get(profile_id)
            return profile.copy() if profile else None

    def get_enabled_profiles(self) -> dict[str, dict]:
        return {
            profile_id: profile
            for profile_id, profile in self.get_profiles().items()
            if profile["enabled"] and profile["selected_slots"]
        }

    def create_profile(
        self, display_name, frame, selected_slots, target_count=3, blocked_slots=None
    ) -> str:
        frame = self._normalize_capture(frame)
        blocked_slots = self._normalized_slots(blocked_slots or [])
        slots = [
            slot for slot in self._normalized_slots(selected_slots) if slot not in blocked_slots
        ]
        profile_id = f"gift_{uuid.uuid4().hex}"
        profile = GiftDb.normalize_profile(
            profile_id,
            {
                "display_name": str(display_name).strip() or profile_id,
                "frame_id": profile_id,
                "selected_slots": slots,
                "blocked_slots": blocked_slots,
                "target_count": self._normalized_target_count(target_count),
                "enabled": True,
            },
        )
        with self._data_lock:
            self._write_frame(profile_id, frame)
            self.db["profiles"][profile_id] = profile
            self.save()
        return profile_id

    def recapture_profile(
        self,
        profile_id,
        frame,
        selected_slots,
        target_count=3,
        display_name=None,
        blocked_slots=None,
    ) -> None:
        frame = self._normalize_capture(frame)
        blocked_slots = self._normalized_slots(blocked_slots or [])
        slots = [
            slot for slot in self._normalized_slots(selected_slots) if slot not in blocked_slots
        ]
        with self._data_lock:
            profile = self.db["profiles"].get(profile_id)
            if profile is None:
                raise KeyError(profile_id)
            if display_name is not None:
                profile["display_name"] = str(display_name).strip() or profile_id
            profile["selected_slots"] = slots
            profile["blocked_slots"] = blocked_slots
            profile["target_count"] = self._normalized_target_count(target_count)
            self._write_frame(profile["frame_id"], frame)
            self.db["profiles"][profile_id] = GiftDb.normalize_profile(profile_id, profile)
            self.save()

    def update_profile(
        self,
        profile_id,
        *,
        enabled=None,
        target_count=None,
        display_name=None,
        selected_slots=None,
        priority_gift_ids=None,
        slot_gift_ids=None,
        bond_level=None,
        bond_exp=None,
        target_level=None,
        buy_tier=None,
        daily_extra_exp=None,
        daily_extra_enabled=None,
    ) -> None:
        with self._data_lock:
            profile = self.db["profiles"].get(profile_id)
            if profile is None:
                raise KeyError(profile_id)
            if enabled is not None:
                profile["enabled"] = bool(enabled)
            if target_count is not None:
                profile["target_count"] = self._normalized_target_count(target_count)
            if display_name is not None:
                profile["display_name"] = str(display_name).strip() or profile_id
            if selected_slots is not None:
                profile["selected_slots"] = self._normalized_slots(selected_slots)
            if priority_gift_ids is not None:
                profile["priority_gift_ids"] = GiftDb.normalize_gift_ids(priority_gift_ids)
            if slot_gift_ids is not None:
                profile["slot_gift_ids"] = GiftDb.normalize_slot_gift_ids(slot_gift_ids)
            if bond_level is not None:
                profile["bond_level"] = bond_level
            if bond_exp is not None:
                profile["bond_exp"] = bond_exp
            if target_level is not None:
                profile["target_level"] = target_level
            if buy_tier is not None:
                profile["buy_tier"] = buy_tier
            if daily_extra_exp is not None:
                profile["daily_extra_exp"] = daily_extra_exp
            if daily_extra_enabled is not None:
                profile["daily_extra_enabled"] = bool(daily_extra_enabled)
            self.db["profiles"][profile_id] = GiftDb.normalize_profile(profile_id, profile)
            self.save()

    def delete_profile(self, profile_id: str) -> None:
        with self._data_lock:
            profile = self.db["profiles"].pop(profile_id, None)
            if profile is None:
                return
            path = self._frame_path(profile["frame_id"])
            if path.exists():
                path.unlink()
            self.save()

    def load_frame(self, profile_or_id) -> np.ndarray | None:
        if isinstance(profile_or_id, str):
            profile = self.get_profile(profile_or_id)
        else:
            profile = profile_or_id
        if not profile:
            return None
        image = cv2.imread(str(self._frame_path(profile["frame_id"])))
        return image.copy() if image is not None else None
