"""赠礼页读取器: 把游戏 UI 读成标准化快照, 供 Step 2 的计算层消费.

职责边界: **只读游戏**, 不计算经验/购买量/天数, 也不赠礼。
所有解析都是纯函数(便于单测), 只有 ``GiftPageReader`` 需要 task 提供 OCR / 模板匹配。

读取失败一律返回 ``None``(未识别), **绝不**用 0 代替, 否则后续会把"读不到"当成
"库存没了"/"没有经验"。

礼物身份来自**用户命名**(见 ``GiftIdentity``): 快照里的 ``gift_id`` 由调用方传入的
"每格 -> 名字"标注决定; 图标模板匹配只用来给出 ``suggested_gift_id``(建议继承)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

import numpy as np
from ok import Logger

from src.gifts.GiftIdentity import (
    MATCH_THRESHOLD,
    SEARCH_EXPAND_X,
    SEARCH_EXPAND_Y,
    GiftIconStore,
    gift_id_for_name,
    suggest_gift_id,
)
from src.gifts.layout import GIFT_LAYOUT as L
from src.Labels import Labels

logger = Logger.get_logger(__name__)

BOND_PROGRESS_RE = re.compile(r"(\d+)\s*/\s*(\d+)")
COUNTER_RE = re.compile(r"(\d+)\s*/\s*(\d+)")
NUMBER_RE = re.compile(r"(\d+)")


# ------------------------------------------------------------------ 纯解析

def parse_bond_progress(text: str | None) -> tuple[int, int] | None:
    """``"1550/12000"`` -> ``(1550, 12000)``; 解析不了返回 None."""
    match = BOND_PROGRESS_RE.search(text or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def parse_int(text: str | None) -> int | None:
    match = NUMBER_RE.search(text or "")
    return int(match.group(1)) if match else None


def parse_gift_counter(text: str | None) -> "DailyCountReading | None":
    """``"赠送3/3"`` -> ``remaining=3, limit=3, used=0``.

    按钮上的第一个数字是**今日剩余次数**(现有 GiftTask 也是这么用的), 不是已用次数。
    """
    match = COUNTER_RE.search(text or "")
    if not match:
        return None
    remaining, limit = int(match.group(1)), int(match.group(2))
    return DailyCountReading(used=max(0, limit - remaining), limit=limit, remaining=remaining)


def parse_global_remaining(text: str | None) -> int | None:
    """``"今日还能赠送10次礼物"`` -> ``10``; 解析不了返回 None."""
    return parse_int(text)


# ------------------------------------------------------------------ 数据结构

@dataclass(frozen=True)
class BondReading:
    level: int | None = None
    exp: int | None = None
    required_exp: int | None = None


@dataclass(frozen=True)
class DailyCountReading:
    used: int | None = None
    limit: int | None = None
    remaining: int | None = None


@dataclass(frozen=True)
class GiftSlotReading:
    slot_index: int
    # 最终身份: 由用户命名决定(见 GiftIdentity); None = 未命名
    gift_id: str | None = None
    # 图标模板给出的"建议继承"身份与分数(仅提示, 不自动生效)
    suggested_gift_id: str | None = None
    match_score: float = -1.0
    base_exp: int | None = None
    stock: int | None = None
    not_giftable: bool = False
    icon: np.ndarray | None = field(default=None, compare=False)
    # 放大后的图标裁剪, 只用于模板匹配(容忍几像素位移); 不参与比较/落盘
    search_icon: np.ndarray | None = field(default=None, compare=False)


@dataclass(frozen=True)
class GiftPageSnapshot:
    bond: BondReading = field(default_factory=BondReading)
    slots: tuple[GiftSlotReading, ...] = ()
    character_count: DailyCountReading = field(default_factory=DailyCountReading)
    global_count: DailyCountReading = field(default_factory=DailyCountReading)
    errors: tuple[str, ...] = ()

    def slot(self, index: int) -> GiftSlotReading | None:
        for reading in self.slots:
            if reading.slot_index == index:
                return reading
        return None


@dataclass(frozen=True)
class CatalogUpdate:
    gift_id: str
    exp: int


# ------------------------------------------------------------------ 纯几何

def gift_slot_boxes(width: int, height: int) -> list[tuple[float, float, float, float]]:
    """首屏 5x2 共 10 个礼物格的比例框(与 GiftTask.get_gift_boxes 一致)."""
    first_x, first_y, first_to_x, first_to_y = L.gift_first_ratio
    box_width = first_to_x - first_x
    box_height = first_to_y - first_y
    return [
        (
            first_x + column * L.gift_column_step,
            first_y + row * L.gift_row_step,
            first_x + column * L.gift_column_step + box_width,
            first_y + row * L.gift_row_step + box_height,
        )
        for row in range(L.gift_rows)
        for column in range(L.gift_columns)
    ]


def expand_box(box, x_expand: float, y_expand: float):
    x1, y1, x2, y2 = box
    return (x1 - x_expand, y1 - y_expand, x2 + x_expand, y2 + y_expand)


def gift_exp_box(slot_box):
    """礼物卡下方的心形经验数字框(默认视角第二排被底部横幅挡住, 需要下滚)."""
    x1, y1, x2, _ = slot_box
    return (
        x1 - L.gift_exp_x_expand,
        y1 + L.gift_exp_y_offset,
        x2 + L.gift_exp_x_expand,
        y1 + L.gift_exp_y_offset + L.gift_exp_height,
    )


def gift_badge_box(slot_box):
    """图标右下角的库存数量角标框."""
    x1, y1, _, _ = slot_box
    return (
        x1 + L.gift_badge_x_offset,
        y1 + L.gift_badge_y_offset,
        x1 + L.gift_badge_x_offset + L.gift_badge_width,
        y1 + L.gift_badge_y_offset + L.gift_badge_height,
    )


def special_badge_box(slot_box):
    """图标左上角的"特殊礼物(CD)"角标框."""
    width = slot_box[2] - slot_box[0]
    height = slot_box[3] - slot_box[1]
    return (
        slot_box[0] - width * L.unlimited_icon_x_offset_ratio,
        slot_box[1] - height * L.unlimited_icon_y_offset_ratio,
        slot_box[2] - width * L.unlimited_icon_width_reduction_ratio,
        slot_box[3],
    )


def crop_region(frame, box) -> np.ndarray | None:
    if frame is None:
        return None
    height, width = frame.shape[:2]
    x1 = max(0, min(width, int(round(width * box[0]))))
    y1 = max(0, min(height, int(round(height * box[1]))))
    x2 = max(0, min(width, int(round(width * box[2]))))
    y2 = max(0, min(height, int(round(height * box[3]))))
    if x2 <= x1 or y2 <= y1:
        return None
    return frame[y1:y2, x1:x2].copy()


# ------------------------------------------------------------------ 纯逻辑

def label_slots(snapshot: GiftPageSnapshot, slot_gift_ids) -> GiftPageSnapshot:
    """把用户标注的 ``{slot: gift_id}`` 应用到快照上."""
    mapping = {int(slot): gift_id for slot, gift_id in (slot_gift_ids or {}).items()}
    slots = [
        replace(reading, gift_id=mapping.get(reading.slot_index))
        if reading.slot_index in mapping
        else reading
        for reading in snapshot.slots
    ]
    return replace(snapshot, slots=tuple(slots))


def migrate_priority_gift_ids(selected_slots, slot_gift_ids) -> list[str] | None:
    """把旧的 ``selected_slots`` 按顺序换算成 ``priority_gift_ids``.

    顺序严格保持; 任何一个 slot 没有标注就返回 ``None``, 调用方应保留
    ``selected_slots``、**不要**写入不完整的 ``priority_gift_ids``。
    """
    mapping = {int(slot): gift_id for slot, gift_id in (slot_gift_ids or {}).items()}
    gift_ids: list[str] = []
    for slot in selected_slots:
        gift_id = mapping.get(int(slot))
        if not gift_id:
            return None
        gift_ids.append(gift_id)
    return gift_ids


def merge_snapshot_into_catalog(
    snapshot: GiftPageSnapshot, catalog
) -> tuple[list[CatalogUpdate], list[str]]:
    """把快照里读到的礼物合并进目录; 返回 ``(要写入的更新, 冲突说明)``.

    同一 ``gift_id`` 读到不同 ``exp`` 时不覆盖, 记成冲突返回(说明命名重复或 OCR 有问题)。
    """
    updates: dict[str, CatalogUpdate] = {}
    conflicts: list[str] = []
    for reading in snapshot.slots:
        if reading.not_giftable or not reading.gift_id or reading.base_exp is None:
            continue
        existing = (catalog or {}).get(reading.gift_id)
        existing_exp = existing.get("exp") if isinstance(existing, dict) else None
        if existing_exp is not None and int(existing_exp) != int(reading.base_exp):
            conflicts.append(
                f"{reading.gift_id}: existing exp={existing_exp} observed exp={reading.base_exp}"
            )
            continue
        updates[reading.gift_id] = CatalogUpdate(reading.gift_id, int(reading.base_exp))
    return list(updates.values()), conflicts


def gift_ids_for_names(names) -> dict[str, str]:
    """``{名字: gift_id}``(空名字忽略). 名字相同 -> id 相同, 这是身份的唯一依据."""
    result: dict[str, str] = {}
    for name in names or []:
        gift_id = gift_id_for_name(name)
        if gift_id:
            result[name] = gift_id
    return result


# ------------------------------------------------------------------ 读取器

class GiftPageReader:
    """从赠礼页读一帧(或两帧)得到标准化快照.

    - ``frame``: 默认视角(角色信息 / 库存角标 / 每日次数 / 特殊礼物)。
    - ``exp_frame``: 下滚后的视角(心形经验数字两排都可见); 不给就用 ``frame``。
    """

    def __init__(
        self,
        task,
        *,
        icon_store: GiftIconStore | None = None,
        threshold: float | None = None,
    ):
        self.task = task
        self.icon_store = icon_store or GiftIconStore()
        self.threshold = MATCH_THRESHOLD if threshold is None else threshold

    def _text(self, region, frame, match=None) -> str | None:
        try:
            results = self.task.ocr(*region, frame=frame, match=match) or []
        except Exception as error:
            logger.info(f"gift page ocr failed {region}: {error}")
            return None
        text = "".join(str(getattr(item, "name", "")) for item in results).strip()
        return text or None

    def _is_special(self, slot_box, frame) -> bool:
        """特殊礼物(角标 CD, 不可赠送)检测; 用的是图标左上角的小角标区."""
        badge = special_badge_box(slot_box)
        try:
            return bool(self.task.find_one(Labels.unlimit_gift, box=badge, frame=frame))
        except Exception as error:
            logger.info(f"gift page special check failed: {error}")
            return False

    def read_bond(self, frame=None) -> BondReading:
        frame = self.task.frame if frame is None else frame
        level = parse_int(self._text(L.bond_level_box, frame))
        progress = parse_bond_progress(self._text(L.bond_progress_box, frame))
        if progress is None:
            return BondReading(level=level)
        return BondReading(level=level, exp=progress[0], required_exp=progress[1])

    def read_counts(self, frame=None) -> tuple[DailyCountReading, DailyCountReading]:
        frame = self.task.frame if frame is None else frame
        character = parse_gift_counter(self._text(L.gift_counter_button_box, frame))
        global_remaining = parse_global_remaining(self._text(L.daily_banner_box, frame))
        global_count = DailyCountReading(remaining=global_remaining)
        return (character or DailyCountReading(), global_count)

    def read_slots(self, frame, exp_frame=None) -> tuple[GiftSlotReading, ...]:
        height, width = frame.shape[:2]
        exp_frame = frame if exp_frame is None else exp_frame
        readings: list[GiftSlotReading] = []
        for index, slot_box in enumerate(gift_slot_boxes(width, height)):
            readings.append(
                GiftSlotReading(
                    slot_index=index,
                    base_exp=parse_int(self._text(gift_exp_box(slot_box), exp_frame)),
                    stock=parse_int(self._text(gift_badge_box(slot_box), frame)),
                    not_giftable=self._is_special(slot_box, frame),
                    icon=crop_region(frame, slot_box),
                    search_icon=crop_region(
                        frame, expand_box(slot_box, SEARCH_EXPAND_X, SEARCH_EXPAND_Y)
                    ),
                )
            )
        return tuple(readings)

    def suggest_labels(
        self, slots, templates=None, threshold: float | None = None
    ) -> tuple[GiftSlotReading, ...]:
        """用已标注过的图标模板给每个格子一个"建议继承"的 gift_id(不自动生效)."""
        templates = self.icon_store.load_all() if templates is None else templates
        limit = self.threshold if threshold is None else threshold
        suggested: list[GiftSlotReading] = []
        for reading in slots:
            if reading.not_giftable or reading.search_icon is None:
                suggested.append(reading)
                continue
            gift_id, score = suggest_gift_id(reading.search_icon, templates, limit)
            suggested.append(replace(reading, suggested_gift_id=gift_id, match_score=score))
        return tuple(suggested)

    def learn_labels(self, slots, templates=None) -> dict[str, np.ndarray]:
        """把已标注格子的图标存成模板, 供下次采集建议继承; 返回合并后的模板表."""
        templates = dict(self.icon_store.load_all() if templates is None else templates)
        for reading in slots:
            if not reading.gift_id or reading.not_giftable or reading.icon is None:
                continue
            if reading.gift_id not in templates:
                self.icon_store.save(reading.gift_id, reading.icon)
                templates[reading.gift_id] = reading.icon
        return templates

    def read(self, frame=None, exp_frame=None, slot_gift_ids=None) -> GiftPageSnapshot:
        frame = self.task.frame if frame is None else frame
        if frame is None or not getattr(frame, "size", 0):
            return GiftPageSnapshot(errors=("没有可用的游戏画面",))
        errors: list[str] = []
        bond = self.read_bond(frame)
        if bond.level is None:
            errors.append("羁遇等级读取失败")
        if bond.exp is None or bond.required_exp is None:
            errors.append("羁遇经验读取失败")
        character_count, global_count = self.read_counts(frame)
        if character_count.limit is None:
            errors.append("每日赠送次数读取失败")
        slots = self.suggest_labels(self.read_slots(frame, exp_frame=exp_frame))
        missing_exp = [r.slot_index for r in slots if r.base_exp is None]
        if missing_exp:
            errors.append(f"礼物经验读取失败: slots {missing_exp}")
        snapshot = GiftPageSnapshot(
            bond=bond,
            slots=slots,
            character_count=character_count,
            global_count=global_count,
            errors=tuple(errors),
        )
        return label_slots(snapshot, slot_gift_ids)
