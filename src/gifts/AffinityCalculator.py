"""纯计算层: 羁遇(好感)等级规划、礼物消耗、共享库存与购买量.

本模块只依赖普通 Python 数据结构, **不**依赖 UI / OCR / 截图 / GiftTask /
GiftManager 的 I/O / 游戏进程, 所有函数都是纯计算, 便于单测。

数据语义(与 GiftDb schema v2 对齐):

- ``bond_level``: 当前羁遇等级, 1..10; ``0`` 表示"未设置"(由调用方决定跳过或取默认)。
- ``bond_exp``: **当前等级内已经获得的经验**(不是累计经验)。例如 8 级 bond_exp=3000
  表示离 9 级还差 ``required_exp_for_level(8) - 3000 = 9000``。
- ``target_level``: 目标羁遇等级, 1..10; 低于当前等级时需求为 0(不产生负需求)。
- ``priority_gift_ids``: **有序**列表, 顺序有实际意义(先 A 再 B 再 C), 不会被转成 set。
- 普通礼物档位 ``100/200/400``; 特殊礼物不参与计算。

取整规则: 5% 加成用整数运算 ``base * 105 // 100``(向零截断)。当前档位
100/200/400 乘 1.05 都是整数, 所以截断与四舍五入结果相同; 出现非整除档位时按截断处理
(**游戏实际取整方式尚未确认**, 后续如有实测再调整)。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.gifts.GiftDb import GIFT_TIERS, MAX_LEVEL

MIN_LEVEL = 1
MAX_SKILL_LEVEL = 5
DEFAULT_DAILY_GIFT_LIMIT_PER_CHAR = 3
DEFAULT_DAILY_GIFT_LIMIT_GLOBAL = 10
# 5% 加成生效的最高羁遇等级: 特技 5 级 -> 8 级
MAX_BONUS_LEVEL = 8
# 实测截图 + 用户确认: n -> n+1 需要的经验
EXP_TABLE = {
    1: 500,
    2: 1000,
    3: 2000,
    4: 3500,
    5: 5000,
    6: 7000,
    7: 9000,
    8: 12000,
    9: 16000,
}


def required_exp_for_level(level: int) -> int:
    """羁遇等级 ``level`` 升到 ``level + 1`` 需要的经验; 满级(10)返回 0."""
    level = int(level)
    if not MIN_LEVEL <= level <= MAX_LEVEL:
        raise ValueError(f"level must be in {MIN_LEVEL}..{MAX_LEVEL}, got {level}")
    return EXP_TABLE.get(level, 0)


def required_exp_to_target(current_level: int, current_exp: int, target_level: int) -> int:
    """从 ``(current_level, current_exp)`` 升到 ``target_level`` 还差多少经验.

    ``target_level`` 超过 10 会被夹到 10; 不高于当前等级时返回 0(不会出现负需求)。
    """
    current_level = int(current_level)
    current_exp = int(current_exp)
    target_level = int(target_level)
    if not MIN_LEVEL <= current_level <= MAX_LEVEL:
        raise ValueError(f"current_level must be in {MIN_LEVEL}..{MAX_LEVEL}, got {current_level}")
    if current_exp < 0:
        raise ValueError(f"current_exp must not be negative, got {current_exp}")
    if target_level > MAX_LEVEL:
        target_level = MAX_LEVEL
    if target_level <= current_level:
        return 0
    needed = -current_exp
    for level in range(current_level, target_level):
        needed += required_exp_for_level(level)
    return max(0, needed)


def bonus_threshold(skill_level: int) -> int:
    """5% 加成生效的最高羁遇等级 = 主角特技等级 + 3 (特技 5 级 -> 8)."""
    skill_level = int(skill_level)
    if not MIN_LEVEL <= skill_level <= MAX_SKILL_LEVEL:
        raise ValueError(
            f"skill_level must be in {MIN_LEVEL}..{MAX_SKILL_LEVEL}, got {skill_level}"
        )
    return min(MAX_BONUS_LEVEL, skill_level + 3)


def gift_exp(base_exp: int, level: int, skill_level: int) -> int:
    """角色处于羁遇 ``level`` 级时, 一件基础经验 ``base_exp`` 的礼物实际提供的经验.

    等级 <= ``bonus_threshold(skill_level)`` 时额外 +5%(不叠加, 单件独立判定)。
    """
    base_exp = int(base_exp)
    if base_exp <= 0:
        return 0
    if int(level) <= bonus_threshold(skill_level):
        return base_exp * 105 // 100
    return base_exp


def _apply_exp(level: int, exp: int, amount: int, target_level: int) -> tuple[int, int, int]:
    """加经验并升级; 返回 ``(level, exp, overflow)``.

    ``overflow`` 是越过 ``target_level`` 之后多出来的经验(未达标时为 0)。
    """
    level = int(level)
    exp = int(exp)
    amount = int(amount)
    if amount <= 0:
        return level, exp, 0
    if level >= target_level:
        return level, exp + amount, amount
    exp += amount
    while level < target_level and exp >= required_exp_for_level(level):
        exp -= required_exp_for_level(level)
        level += 1
    if level >= target_level:
        return level, exp, exp
    return level, exp, 0


@dataclass(frozen=True)
class GiftUse:
    """一种礼物被用掉多少个, 以及它提供的经验."""

    gift_id: str
    quantity: int
    base_exp: int
    actual_exp: int


@dataclass(frozen=True)
class CharacterPlan:
    """单个角色某一次规划的结果."""

    display_name: str
    start_level: int
    start_exp: int
    target_level: int
    required_exp_before_extra: int
    daily_extra_exp: int
    required_gift_exp: int
    gift_plan: tuple[GiftUse, ...]
    gifts_used: int
    gained_exp: int
    final_level: int
    final_exp: int
    reached_target: bool
    shortage_exp: int
    overflow_exp: int
    inventory_after: dict[str, int]
    limited_by_daily: bool
    limited_by_stock: bool


@dataclass(frozen=True)
class MultiCharacterPlan:
    """一天内多个角色共享库存的规划结果."""

    day_plans: tuple[CharacterPlan, ...]
    inventory_after: dict[str, int]
    total_gifts_used: int
    global_remaining: int


@dataclass(frozen=True)
class DaysPlan:
    """多天规划结果."""

    days: int
    all_reached: bool
    stock_empty_day: int | None
    day_plans: tuple[MultiCharacterPlan, ...]


@dataclass(frozen=True)
class PurchaseNeed:
    """需要购买的礼物数量与它带来的经验."""

    quantity: int
    supplied_exp: int
    overflow_exp: int


def _group_uses(uses: list[tuple[str, int, int]]) -> tuple[GiftUse, ...]:
    grouped: list[GiftUse] = []
    for gift_id, base_exp, actual_exp in uses:
        if grouped and grouped[-1].gift_id == gift_id:
            last = grouped[-1]
            grouped[-1] = GiftUse(
                gift_id, last.quantity + 1, base_exp, last.actual_exp + actual_exp
            )
        else:
            grouped.append(GiftUse(gift_id, 1, base_exp, actual_exp))
    return tuple(grouped)


def _build_queue(priority_gift_ids, gift_catalog, inventory) -> list[tuple[str, int]]:
    queue: list[tuple[str, int]] = []
    for gift_id in priority_gift_ids:
        entry = gift_catalog.get(gift_id)
        if not entry:
            continue
        base_exp = int(entry.get("exp", 0))
        if base_exp <= 0:
            continue
        for _ in range(max(0, int(inventory.get(gift_id, 0)))):
            queue.append((gift_id, base_exp))
    return queue


def calculate_character_plan(
    *,
    current_level: int,
    current_exp: int,
    target_level: int,
    priority_gift_ids=(),
    gift_catalog=None,
    inventory=None,
    protagonist_skill_level: int = 1,
    daily_extra_exp: int = 0,
    daily_extra_enabled: bool = False,
    daily_gift_limit: int | None = None,
    display_name: str = "",
) -> CharacterPlan:
    """规划一个角色的礼物消耗.

    - 先结算"每日额外经验"(约会, 不享受 5% 加成, 不消耗库存)。
    - 再按 ``priority_gift_ids`` 顺序消耗共享库存, 顺序不会为了减少溢出而调整。
    - ``daily_gift_limit`` 为 ``None`` 表示不限次数(算"一共需要多少礼物");
      给数字时表示当天最多能送几次, 送不完的部分留在 ``shortage_exp``。
    - 输出里的 ``daily_extra_exp`` 是**实际生效**的额外经验(角色已达标时为 0)。
    """
    gift_catalog = gift_catalog or {}
    inventory = {key: int(value) for key, value in (inventory or {}).items()}
    target_level = min(int(target_level), MAX_LEVEL)

    required_before_extra = required_exp_to_target(current_level, current_exp, target_level)

    level = int(current_level)
    exp = int(current_exp)
    overflow_exp = 0
    extra = max(0, int(daily_extra_exp)) if daily_extra_enabled else 0
    if required_before_extra <= 0:
        extra = 0
    if extra:
        level, exp, overflow_exp = _apply_exp(level, exp, extra, target_level)

    required_gift_exp = required_exp_to_target(level, exp, target_level)

    queue = _build_queue(priority_gift_ids, gift_catalog, inventory)
    uses: list[tuple[str, int, int]] = []
    gained_exp = 0
    reached = level >= target_level
    for gift_id, base_exp in queue:
        if reached:
            break
        if daily_gift_limit is not None and len(uses) >= daily_gift_limit:
            break
        actual_exp = gift_exp(base_exp, level, protagonist_skill_level)
        level, exp, gift_overflow = _apply_exp(level, exp, actual_exp, target_level)
        overflow_exp += gift_overflow
        gained_exp += actual_exp
        inventory[gift_id] = inventory.get(gift_id, 0) - 1
        uses.append((gift_id, base_exp, actual_exp))
        if level >= target_level:
            reached = True

    limited_by_daily = (
        not reached and daily_gift_limit is not None and len(uses) >= daily_gift_limit
    )
    limited_by_stock = not reached and len(uses) >= len(queue)
    return CharacterPlan(
        display_name=str(display_name),
        start_level=int(current_level),
        start_exp=int(current_exp),
        target_level=target_level,
        required_exp_before_extra=required_before_extra,
        daily_extra_exp=extra,
        required_gift_exp=required_gift_exp,
        gift_plan=_group_uses(uses),
        gifts_used=len(uses),
        gained_exp=gained_exp,
        final_level=level,
        final_exp=exp,
        reached_target=reached,
        shortage_exp=required_exp_to_target(level, exp, target_level),
        overflow_exp=overflow_exp,
        inventory_after=inventory,
        limited_by_daily=limited_by_daily,
        limited_by_stock=limited_by_stock,
    )


def calculate_multi_character_plan(
    *,
    profiles,
    gift_catalog=None,
    inventory=None,
    settings=None,
) -> MultiCharacterPlan:
    """规划**一天**内多个角色共享库存的消耗.

    角色顺序 = ``profiles`` 的传入顺序(与 GiftTask 的执行顺序一致), 不按缺口重排。
    每个角色最多 ``daily_gift_limit_per_char`` 次, 全部角色合计最多
    ``daily_gift_limit_global`` 次; 库存被前面的角色消耗后, 后面的角色只能用剩下的。
    未设置等级(``bond_level < 1``)的角色会被跳过。
    """
    settings = settings or {}
    per_char = int(settings.get("daily_gift_limit_per_char", DEFAULT_DAILY_GIFT_LIMIT_PER_CHAR))
    global_limit = int(settings.get("daily_gift_limit_global", DEFAULT_DAILY_GIFT_LIMIT_GLOBAL))
    skill_level = int(settings.get("protagonist_skill_level", 1))

    remaining_inventory = {key: int(value) for key, value in (inventory or {}).items()}
    global_remaining = max(0, global_limit)
    plans: list[CharacterPlan] = []
    for profile in profiles:
        level = int(profile.get("bond_level", 0))
        if level < MIN_LEVEL:
            continue
        limit = max(0, min(per_char, global_remaining))
        plan = calculate_character_plan(
            display_name=str(profile.get("display_name", "")),
            current_level=level,
            current_exp=int(profile.get("bond_exp", 0)),
            target_level=int(profile.get("target_level", MAX_LEVEL)),
            priority_gift_ids=profile.get("priority_gift_ids", []),
            gift_catalog=gift_catalog,
            inventory=remaining_inventory,
            protagonist_skill_level=skill_level,
            daily_extra_exp=int(profile.get("daily_extra_exp", 0)),
            daily_extra_enabled=bool(profile.get("daily_extra_enabled", False)),
            daily_gift_limit=limit,
        )
        plans.append(plan)
        remaining_inventory = plan.inventory_after
        global_remaining -= plan.gifts_used
    return MultiCharacterPlan(
        day_plans=tuple(plans),
        inventory_after=remaining_inventory,
        total_gifts_used=sum(plan.gifts_used for plan in plans),
        global_remaining=max(0, global_remaining),
    )


def calculate_days_to_target(
    *,
    profiles,
    gift_catalog=None,
    inventory=None,
    settings=None,
    max_days: int = 365,
) -> DaysPlan:
    """按天模拟直到所有角色达标 / 库存耗尽 / 再无任何经验来源.

    ``stock_empty_day`` 是库存首次耗尽的第几天(1-based), 未耗尽为 ``None``。
    """
    active = [
        dict(profile) for profile in profiles if int(profile.get("bond_level", 0)) >= MIN_LEVEL
    ]
    if not active:
        return DaysPlan(days=0, all_reached=False, stock_empty_day=None, day_plans=())

    targets = [min(int(p.get("target_level", MAX_LEVEL)), MAX_LEVEL) for p in active]
    states = [(int(p["bond_level"]), int(p.get("bond_exp", 0))) for p in active]
    remaining_inventory = {key: int(value) for key, value in (inventory or {}).items()}
    has_stock = any(value > 0 for value in remaining_inventory.values())
    day_plans: list[MultiCharacterPlan] = []
    stock_empty_day = None

    while len(day_plans) < max_days:
        if all(level >= target for (level, _), target in zip(states, targets)):
            return DaysPlan(
                days=len(day_plans),
                all_reached=True,
                stock_empty_day=stock_empty_day,
                day_plans=tuple(day_plans),
            )
        day_profiles = [
            {**profile, "bond_level": level, "bond_exp": exp}
            for profile, (level, exp) in zip(active, states)
        ]
        plan = calculate_multi_character_plan(
            profiles=day_profiles,
            gift_catalog=gift_catalog,
            inventory=remaining_inventory,
            settings=settings,
        )
        if plan.total_gifts_used == 0 and all(p.daily_extra_exp == 0 for p in plan.day_plans):
            break
        day_plans.append(plan)
        remaining_inventory = plan.inventory_after
        for index, character_plan in enumerate(plan.day_plans):
            states[index] = (character_plan.final_level, character_plan.final_exp)
        if has_stock and stock_empty_day is None and not any(
            value > 0 for value in remaining_inventory.values()
        ):
            stock_empty_day = len(day_plans)

    return DaysPlan(
        days=len(day_plans),
        all_reached=False,
        stock_empty_day=stock_empty_day,
        day_plans=tuple(day_plans),
    )


def calculate_purchase_need(
    required_exp: int, buy_tier: int, available_exp: int = 0
) -> PurchaseNeed:
    """按 ``buy_tier`` 档位计算还需要买几个礼物.

    库存与购买分开: ``available_exp`` 是现有库存能提供的经验, 先扣除再算购买量。
    只按礼物基础经验估算(不含 5% 加成), 属于保守上界。
    """
    buy_tier = int(buy_tier)
    if buy_tier not in GIFT_TIERS:
        raise ValueError(f"buy_tier must be one of {GIFT_TIERS}, got {buy_tier}")
    remaining = max(0, int(required_exp)) - max(0, int(available_exp))
    if remaining <= 0:
        return PurchaseNeed(0, 0, 0)
    quantity = -(-remaining // buy_tier)
    supplied_exp = quantity * buy_tier
    return PurchaseNeed(quantity, supplied_exp, supplied_exp - remaining)
