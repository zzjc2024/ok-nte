import json
import os

DB_SCHEMA_VERSION = 2

# 普通礼物档位(经验值), 特殊礼物不参与计算
GIFT_TIERS = (100, 200, 400)
DEFAULT_BUY_TIER = 100
DEFAULT_DAILY_EXTRA_EXP = 200
DEFAULT_TARGET_LEVEL = 10
DEFAULT_PROTAGONIST_SKILL_LEVEL = 1
DEFAULT_DAILY_GIFT_LIMIT_PER_CHAR = 3
DEFAULT_DAILY_GIFT_LIMIT_GLOBAL = 10
MAX_SKILL_LEVEL = 5
MAX_LEVEL = 10


def _to_int(value, default=0, *, minimum=None, maximum=None) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def _normalize_counts(value) -> dict:
    """Normalize a ``{key: count}`` mapping (gift stock, ledger sent/characters)."""
    if not isinstance(value, dict):
        return {}
    counts = {}
    for key, count in value.items():
        key = str(key).strip()
        if not key:
            continue
        counts[key] = _to_int(count, 0, minimum=0)
    return counts


def default_settings() -> dict:
    return {
        "protagonist_skill_level": DEFAULT_PROTAGONIST_SKILL_LEVEL,
        "daily_gift_limit_per_char": DEFAULT_DAILY_GIFT_LIMIT_PER_CHAR,
        "daily_gift_limit_global": DEFAULT_DAILY_GIFT_LIMIT_GLOBAL,
    }


def default_db() -> dict:
    return {
        "schema_version": DB_SCHEMA_VERSION,
        "settings": default_settings(),
        "profiles": {},
        "gift_catalog": {},
        "stock": {},
        "stock_snapshot_at": "",
        "ledger": {},
    }


def normalize_slots(value) -> list[int]:
    """Keep valid slot indexes in their user-selected priority order."""
    if not isinstance(value, list):
        return []
    slots = []
    for item in value:
        try:
            index = int(item)
        except (TypeError, ValueError):
            continue
        if 0 <= index < 10 and index not in slots:
            slots.append(index)
    return slots


def normalize_gift_ids(value) -> list[str]:
    """Keep non-empty gift ids in order, dropping duplicates."""
    if not isinstance(value, list):
        return []
    gift_ids = []
    for item in value:
        gift_id = str(item).strip()
        if gift_id and gift_id not in gift_ids:
            gift_ids.append(gift_id)
    return gift_ids


def normalize_slot_gift_ids(value) -> dict:
    """``{slot: gift_id}``: 用户给每个礼物格标的礼物身份(键统一成字符串便于 JSON)."""
    if not isinstance(value, dict):
        return {}
    mapping = {}
    for slot, gift_id in value.items():
        try:
            index = int(slot)
        except (TypeError, ValueError):
            continue
        gift_id = str(gift_id).strip()
        if 0 <= index < 10 and gift_id:
            mapping[str(index)] = gift_id
    return mapping


def normalize_settings(value) -> dict:
    if not isinstance(value, dict):
        value = {}
    return {
        "protagonist_skill_level": _to_int(
            value.get("protagonist_skill_level"),
            DEFAULT_PROTAGONIST_SKILL_LEVEL,
            minimum=1,
            maximum=MAX_SKILL_LEVEL,
        ),
        "daily_gift_limit_per_char": _to_int(
            value.get("daily_gift_limit_per_char"),
            DEFAULT_DAILY_GIFT_LIMIT_PER_CHAR,
            minimum=1,
        ),
        "daily_gift_limit_global": _to_int(
            value.get("daily_gift_limit_global"),
            DEFAULT_DAILY_GIFT_LIMIT_GLOBAL,
            minimum=1,
        ),
    }


def normalize_catalog(value) -> dict:
    if not isinstance(value, dict):
        return {}
    catalog = {}
    for gift_id, entry in value.items():
        gift_id = str(gift_id).strip()
        if not gift_id or not isinstance(entry, dict):
            continue
        catalog[gift_id] = {
            "exp": _to_int(entry.get("exp"), 0, minimum=0),
            "name": str(entry.get("name", "")).strip(),
        }
    return catalog


def normalize_stock(value) -> dict:
    return _normalize_counts(value)


def normalize_ledger(value) -> dict:
    if not isinstance(value, dict):
        return {}
    ledger = {}
    for date, entry in value.items():
        date = str(date).strip()
        if not date or not isinstance(entry, dict):
            continue
        ledger[date] = {
            "sent": _normalize_counts(entry.get("sent")),
            "characters": _normalize_counts(entry.get("characters")),
        }
    return ledger


def normalize_profile(profile_id: str, value) -> dict | None:
    if not isinstance(value, dict):
        return None

    frame_id = str(value.get("frame_id", "")).strip()
    if not frame_id:
        return None

    try:
        target_count = min(3, max(1, int(value.get("target_count", 3))))
    except (TypeError, ValueError):
        target_count = 3

    display_name = str(value.get("display_name", "")).strip() or profile_id
    blocked_slots = normalize_slots(value.get("blocked_slots", []))
    buy_tier = _to_int(value.get("buy_tier"), DEFAULT_BUY_TIER)
    if buy_tier not in GIFT_TIERS:
        buy_tier = DEFAULT_BUY_TIER
    return {
        "display_name": display_name,
        "frame_id": frame_id,
        "selected_slots": [
            slot
            for slot in normalize_slots(value.get("selected_slots", []))
            if slot not in blocked_slots
        ],
        "blocked_slots": blocked_slots,
        "target_count": target_count,
        "enabled": bool(value.get("enabled", True)),
        "priority_gift_ids": normalize_gift_ids(value.get("priority_gift_ids", [])),
        "slot_gift_ids": normalize_slot_gift_ids(value.get("slot_gift_ids", {})),
        "bond_level": _to_int(value.get("bond_level"), 0, minimum=0, maximum=MAX_LEVEL),
        "bond_exp": _to_int(value.get("bond_exp"), 0, minimum=0),
        "target_level": _to_int(
            value.get("target_level"), DEFAULT_TARGET_LEVEL, minimum=1, maximum=MAX_LEVEL
        ),
        "buy_tier": buy_tier,
        "daily_extra_exp": _to_int(
            value.get("daily_extra_exp"), DEFAULT_DAILY_EXTRA_EXP, minimum=0
        ),
        "daily_extra_enabled": bool(value.get("daily_extra_enabled", False)),
    }


def validate_db(db: dict) -> bool:
    """Normalize in place and return whether the data was changed.

    Also acts as the v1 -> v2 migration: missing sections get defaults and new
    profile fields are filled in, so old databases stay readable.
    """
    changed = False

    settings = normalize_settings(db.get("settings"))
    if settings != db.get("settings"):
        db["settings"] = settings
        changed = True

    if not isinstance(db.get("profiles"), dict):
        db["profiles"] = {}
        changed = True

    profiles = {}
    for profile_id, profile in db["profiles"].items():
        profile_id = str(profile_id).strip()
        normalized = normalize_profile(profile_id, profile)
        if not profile_id or normalized is None:
            changed = True
            continue
        profiles[profile_id] = normalized
        if normalized != profile:
            changed = True
    if profiles != db["profiles"]:
        db["profiles"] = profiles
        changed = True

    for key, normalizer in (
        ("gift_catalog", normalize_catalog),
        ("stock", normalize_stock),
        ("ledger", normalize_ledger),
    ):
        normalized = normalizer(db.get(key))
        if normalized != db.get(key):
            db[key] = normalized
            changed = True

    if not isinstance(db.get("stock_snapshot_at"), str):
        db["stock_snapshot_at"] = ""
        changed = True

    if db.get("schema_version") != DB_SCHEMA_VERSION:
        db["schema_version"] = DB_SCHEMA_VERSION
        changed = True
    return changed


def load_db(path: str, logger=None) -> dict:
    data = default_db()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as file:
                loaded = json.load(file)
            if isinstance(loaded, dict):
                data.update(loaded)
        except Exception as error:
            if logger:
                logger.error("Failed to load gift configuration", error)
    validate_db(data)
    return data


def save_db(path: str, db: dict, logger=None) -> None:
    try:
        validate_db(db)
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temporary_path = f"{path}.tmp"
        with open(temporary_path, "w", encoding="utf-8") as file:
            json.dump(db, file, ensure_ascii=False, indent=4)
        os.replace(temporary_path, path)
    except Exception as error:
        if logger:
            logger.error("Failed to save gift configuration", error)
        else:
            raise
