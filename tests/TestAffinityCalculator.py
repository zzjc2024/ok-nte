import unittest

from src.gifts import AffinityCalculator as Calc
from src.gifts.GiftDb import GIFT_TIERS, MAX_LEVEL


def _profile(
    name="角色",
    level=9,
    exp=0,
    target=10,
    priority=("A",),
    extra=0,
    extra_enabled=False,
):
    return {
        "display_name": name,
        "bond_level": level,
        "bond_exp": exp,
        "target_level": target,
        "priority_gift_ids": list(priority),
        "daily_extra_exp": extra,
        "daily_extra_enabled": extra_enabled,
    }


CATALOG = {"A": {"exp": 400}, "B": {"exp": 200}, "C": {"exp": 100}}
SETTINGS = {
    "protagonist_skill_level": 1,
    "daily_gift_limit_per_char": 3,
    "daily_gift_limit_global": 10,
}


class TestExpTable(unittest.TestCase):
    def test_exp_table_matches_confirmed_values(self):
        self.assertEqual(
            Calc.EXP_TABLE,
            {1: 500, 2: 1000, 3: 2000, 4: 3500, 5: 5000, 6: 7000, 7: 9000, 8: 12000, 9: 16000},
        )
        self.assertEqual(MAX_LEVEL, 10)
        self.assertEqual(GIFT_TIERS, (100, 200, 400))

    def test_required_exp_for_level(self):
        self.assertEqual(Calc.required_exp_for_level(1), 500)
        self.assertEqual(Calc.required_exp_for_level(9), 16000)
        self.assertEqual(Calc.required_exp_for_level(10), 0)

    def test_required_exp_for_level_rejects_invalid(self):
        for bad in (0, -1, 11):
            with self.assertRaises(ValueError):
                Calc.required_exp_for_level(bad)

    def test_required_exp_to_target(self):
        self.assertEqual(Calc.required_exp_to_target(1, 0, 2), 500)
        self.assertEqual(Calc.required_exp_to_target(9, 0, 10), 16000)
        self.assertEqual(Calc.required_exp_to_target(1, 0, 10), 56000)
        self.assertEqual(Calc.required_exp_to_target(8, 3000, 9), 9000)

    def test_required_exp_to_target_no_negative_need(self):
        self.assertEqual(Calc.required_exp_to_target(5, 100, 5), 0)
        self.assertEqual(Calc.required_exp_to_target(5, 100, 3), 0)
        self.assertEqual(Calc.required_exp_to_target(10, 0, 10), 0)
        self.assertEqual(Calc.required_exp_to_target(10, 5000, 10), 0)

    def test_required_exp_to_target_clamps_over_max(self):
        self.assertEqual(Calc.required_exp_to_target(9, 0, 99), 16000)

    def test_required_exp_to_target_rejects_invalid(self):
        with self.assertRaises(ValueError):
            Calc.required_exp_to_target(0, 0, 5)
        with self.assertRaises(ValueError):
            Calc.required_exp_to_target(11, 0, 5)
        with self.assertRaises(ValueError):
            Calc.required_exp_to_target(5, -1, 6)


class TestBonus(unittest.TestCase):
    def test_bonus_threshold_by_skill_level(self):
        self.assertEqual([Calc.bonus_threshold(s) for s in range(1, 6)], [4, 5, 6, 7, 8])

    def test_bonus_threshold_rejects_invalid(self):
        for bad in (0, 6):
            with self.assertRaises(ValueError):
                Calc.bonus_threshold(bad)

    def test_gift_exp_applies_bonus_at_or_below_threshold(self):
        self.assertEqual(Calc.gift_exp(400, 4, 1), 420)
        self.assertEqual(Calc.gift_exp(400, 5, 1), 400)
        self.assertEqual(Calc.gift_exp(100, 8, 5), 105)
        self.assertEqual(Calc.gift_exp(100, 9, 5), 100)
        self.assertEqual(Calc.gift_exp(200, 8, 5), 210)

    def test_gift_exp_bonus_does_not_stack(self):
        self.assertEqual(Calc.gift_exp(400, 4, 5), 420)

    def test_gift_exp_truncates_toward_zero(self):
        # 150 * 105 // 100 = 157 (截断), 游戏实际取整方式尚未确认
        self.assertEqual(Calc.gift_exp(150, 1, 1), 157)
        self.assertEqual(Calc.gift_exp(0, 1, 1), 0)


class TestCharacterPlan(unittest.TestCase):
    def test_reaches_target_with_bonus(self):
        plan = Calc.calculate_character_plan(
            current_level=1,
            current_exp=0,
            target_level=2,
            priority_gift_ids=["A"],
            gift_catalog=CATALOG,
            inventory={"A": 2},
            protagonist_skill_level=1,
        )
        self.assertEqual(plan.required_exp_before_extra, 500)
        self.assertEqual(plan.required_gift_exp, 500)
        self.assertTrue(plan.reached_target)
        self.assertEqual(plan.gifts_used, 2)
        self.assertEqual(plan.gained_exp, 840)
        self.assertEqual(plan.final_level, 2)
        self.assertEqual(plan.final_exp, 340)
        self.assertEqual(plan.overflow_exp, 340)
        self.assertEqual(plan.shortage_exp, 0)
        self.assertEqual(plan.gift_plan, (Calc.GiftUse("A", 2, 400, 840),))

    def test_priority_order_is_preserved(self):
        plan = Calc.calculate_character_plan(
            current_level=1,
            current_exp=0,
            target_level=10,
            priority_gift_ids=["C", "A", "B"],
            gift_catalog=CATALOG,
            inventory={"A": 2, "B": 1, "C": 1},
            protagonist_skill_level=1,
        )
        self.assertEqual([use.gift_id for use in plan.gift_plan], ["C", "A", "B"])
        self.assertEqual([use.quantity for use in plan.gift_plan], [1, 2, 1])
        self.assertFalse(plan.reached_target)

    def test_bonus_recomputed_after_level_crosses_threshold(self):
        catalog = {"A": {"exp": 400}, "B": {"exp": 400}}
        plan = Calc.calculate_character_plan(
            current_level=4,
            current_exp=3450,
            target_level=6,
            priority_gift_ids=["A", "B"],
            gift_catalog=catalog,
            inventory={"A": 1, "B": 1},
            protagonist_skill_level=1,
        )
        # 第 1 件在 4 级(<=4)吃 5%, 升级到 5 级后第 2 件不再吃加成
        self.assertEqual(
            plan.gift_plan, (Calc.GiftUse("A", 1, 400, 420), Calc.GiftUse("B", 1, 400, 400))
        )
        self.assertEqual(plan.final_level, 5)
        self.assertFalse(plan.reached_target)

    def test_no_stock_keeps_full_shortage(self):
        plan = Calc.calculate_character_plan(
            current_level=1,
            current_exp=0,
            target_level=2,
            priority_gift_ids=["A"],
            gift_catalog=CATALOG,
            inventory={},
            protagonist_skill_level=1,
        )
        self.assertEqual(plan.gifts_used, 0)
        self.assertFalse(plan.reached_target)
        self.assertTrue(plan.limited_by_stock)
        self.assertEqual(plan.shortage_exp, 500)
        self.assertEqual(plan.inventory_after, {})

    def test_exact_stock_reaches_target_without_overflow(self):
        plan = Calc.calculate_character_plan(
            current_level=9,
            current_exp=0,
            target_level=10,
            priority_gift_ids=["A"],
            gift_catalog=CATALOG,
            inventory={"A": 40},
            protagonist_skill_level=1,
        )
        self.assertTrue(plan.reached_target)
        self.assertEqual(plan.gifts_used, 40)
        self.assertEqual(plan.overflow_exp, 0)
        self.assertEqual(plan.inventory_after, {"A": 0})

    def test_surplus_stock_is_left_untouched(self):
        plan = Calc.calculate_character_plan(
            current_level=9,
            current_exp=0,
            target_level=10,
            priority_gift_ids=["A"],
            gift_catalog=CATALOG,
            inventory={"A": 45},
            protagonist_skill_level=1,
        )
        self.assertTrue(plan.reached_target)
        self.assertEqual(plan.gifts_used, 40)
        self.assertEqual(plan.overflow_exp, 0)
        self.assertEqual(plan.inventory_after, {"A": 5})

    def test_unknown_or_zero_exp_gifts_are_ignored(self):
        plan = Calc.calculate_character_plan(
            current_level=9,
            current_exp=0,
            target_level=10,
            priority_gift_ids=["missing", "zero", "A"],
            gift_catalog={"zero": {"exp": 0}, "A": {"exp": 400}},
            inventory={"missing": 5, "zero": 5, "A": 40},
            protagonist_skill_level=1,
        )
        self.assertEqual([use.gift_id for use in plan.gift_plan], ["A"])
        self.assertEqual(plan.inventory_after, {"missing": 5, "zero": 5, "A": 0})


class TestDailyExtraExp(unittest.TestCase):
    def test_extra_is_applied_before_gifts(self):
        plan = Calc.calculate_character_plan(
            current_level=1,
            current_exp=0,
            target_level=2,
            priority_gift_ids=["A"],
            gift_catalog=CATALOG,
            inventory={"A": 1},
            protagonist_skill_level=1,
            daily_extra_exp=200,
            daily_extra_enabled=True,
        )
        self.assertEqual(plan.required_exp_before_extra, 500)
        self.assertEqual(plan.daily_extra_exp, 200)
        self.assertEqual(plan.required_gift_exp, 300)
        self.assertEqual(plan.gained_exp, 420)
        self.assertTrue(plan.reached_target)

    def test_extra_does_not_get_bonus(self):
        plan = Calc.calculate_character_plan(
            current_level=1,
            current_exp=0,
            target_level=2,
            gift_catalog=CATALOG,
            inventory={},
            protagonist_skill_level=1,
            daily_extra_exp=100,
            daily_extra_enabled=True,
        )
        # 100 而不是 105, 所以礼物还需要 400
        self.assertEqual(plan.final_exp, 100)
        self.assertEqual(plan.required_gift_exp, 400)

    def test_extra_can_finish_alone_and_consumes_no_stock(self):
        plan = Calc.calculate_character_plan(
            current_level=1,
            current_exp=0,
            target_level=2,
            priority_gift_ids=["A"],
            gift_catalog=CATALOG,
            inventory={"A": 3},
            protagonist_skill_level=1,
            daily_extra_exp=500,
            daily_extra_enabled=True,
        )
        self.assertTrue(plan.reached_target)
        self.assertEqual(plan.gifts_used, 0)
        self.assertEqual(plan.required_gift_exp, 0)
        self.assertEqual(plan.inventory_after, {"A": 3})

    def test_extra_overflow_is_counted(self):
        plan = Calc.calculate_character_plan(
            current_level=1,
            current_exp=0,
            target_level=2,
            gift_catalog=CATALOG,
            inventory={},
            protagonist_skill_level=1,
            daily_extra_exp=600,
            daily_extra_enabled=True,
        )
        self.assertEqual(plan.final_level, 2)
        self.assertEqual(plan.final_exp, 100)
        self.assertEqual(plan.overflow_exp, 100)

    def test_disabled_extra_is_ignored(self):
        plan = Calc.calculate_character_plan(
            current_level=1,
            current_exp=0,
            target_level=2,
            gift_catalog=CATALOG,
            inventory={},
            protagonist_skill_level=1,
            daily_extra_exp=200,
            daily_extra_enabled=False,
        )
        self.assertEqual(plan.daily_extra_exp, 0)
        self.assertEqual(plan.required_gift_exp, 500)

    def test_already_at_target_ignores_extra(self):
        plan = Calc.calculate_character_plan(
            current_level=10,
            current_exp=220,
            target_level=10,
            gift_catalog=CATALOG,
            inventory={"A": 5},
            protagonist_skill_level=5,
            daily_extra_exp=200,
            daily_extra_enabled=True,
        )
        self.assertTrue(plan.reached_target)
        self.assertEqual(plan.gifts_used, 0)
        self.assertEqual(plan.daily_extra_exp, 0)
        self.assertEqual(plan.inventory_after, {"A": 5})


class TestDailyLimits(unittest.TestCase):
    def test_character_limit_caps_gifts(self):
        plan = Calc.calculate_character_plan(
            current_level=9,
            current_exp=0,
            target_level=10,
            priority_gift_ids=["A"],
            gift_catalog=CATALOG,
            inventory={"A": 100},
            protagonist_skill_level=1,
            daily_gift_limit=3,
        )
        self.assertEqual(plan.gifts_used, 3)
        self.assertFalse(plan.reached_target)
        self.assertTrue(plan.limited_by_daily)
        self.assertFalse(plan.limited_by_stock)
        self.assertEqual(plan.shortage_exp, 16000 - 1200)

    def test_unlimited_plan_uses_everything(self):
        plan = Calc.calculate_character_plan(
            current_level=9,
            current_exp=0,
            target_level=10,
            priority_gift_ids=["A"],
            gift_catalog=CATALOG,
            inventory={"A": 100},
            protagonist_skill_level=1,
        )
        self.assertEqual(plan.gifts_used, 40)
        self.assertTrue(plan.reached_target)


class TestMultiCharacterPlan(unittest.TestCase):
    def test_shared_inventory_is_not_double_counted(self):
        plan = Calc.calculate_multi_character_plan(
            profiles=[_profile("A角"), _profile("B角")],
            gift_catalog=CATALOG,
            inventory={"A": 10},
            settings={**SETTINGS, "daily_gift_limit_per_char": 5},
        )
        first, second = plan.day_plans
        self.assertEqual(first.gifts_used, 5)
        self.assertEqual(second.gifts_used, 5)
        self.assertEqual(plan.total_gifts_used, 10)
        self.assertEqual(plan.inventory_after, {"A": 0})

    def test_global_limit_is_shared_across_characters(self):
        plan = Calc.calculate_multi_character_plan(
            profiles=[_profile(f"角色{i}") for i in range(5)],
            gift_catalog=CATALOG,
            inventory={"A": 100},
            settings={**SETTINGS, "daily_gift_limit_per_char": 3, "daily_gift_limit_global": 10},
        )
        self.assertEqual([p.gifts_used for p in plan.day_plans], [3, 3, 3, 1, 0])
        self.assertEqual(plan.total_gifts_used, 10)
        self.assertEqual(plan.global_remaining, 0)
        self.assertEqual(plan.inventory_after, {"A": 90})

    def test_per_character_limit_when_global_is_plenty(self):
        plan = Calc.calculate_multi_character_plan(
            profiles=[_profile("A角")],
            gift_catalog=CATALOG,
            inventory={"A": 100},
            settings={**SETTINGS, "daily_gift_limit_per_char": 3, "daily_gift_limit_global": 100},
        )
        self.assertEqual(plan.day_plans[0].gifts_used, 3)

    def test_profiles_without_level_are_skipped(self):
        plan = Calc.calculate_multi_character_plan(
            profiles=[_profile("未设置", level=0), _profile("已设置")],
            gift_catalog=CATALOG,
            inventory={"A": 100},
            settings=SETTINGS,
        )
        self.assertEqual([p.display_name for p in plan.day_plans], ["已设置"])

    def test_order_follows_profile_list(self):
        plan = Calc.calculate_multi_character_plan(
            profiles=[_profile("第一"), _profile("第二")],
            gift_catalog=CATALOG,
            inventory={"A": 4},
            settings={**SETTINGS, "daily_gift_limit_per_char": 3},
        )
        self.assertEqual([p.display_name for p in plan.day_plans], ["第一", "第二"])
        self.assertEqual([p.gifts_used for p in plan.day_plans], [3, 1])

    def test_uses_settings_skill_level_for_bonus(self):
        profiles = [_profile("A角", level=8, exp=0, target=9, priority=("A",))]
        with_bonus = Calc.calculate_multi_character_plan(
            profiles=profiles,
            gift_catalog=CATALOG,
            inventory={"A": 1},
            settings={**SETTINGS, "protagonist_skill_level": 5},
        )
        # 特技 5 级 -> 阈值 8 级, 8 级吃加成
        self.assertEqual(with_bonus.day_plans[0].gift_plan[0].actual_exp, 420)
        without_bonus = Calc.calculate_multi_character_plan(
            profiles=profiles,
            gift_catalog=CATALOG,
            inventory={"A": 1},
            settings={**SETTINGS, "protagonist_skill_level": 4},
        )
        # 特技 4 级 -> 阈值 7 级, 8 级不吃加成
        self.assertEqual(without_bonus.day_plans[0].gift_plan[0].actual_exp, 400)


class TestDaysToTarget(unittest.TestCase):
    def test_stops_when_stock_runs_out(self):
        plan = Calc.calculate_days_to_target(
            profiles=[_profile("A角")],
            gift_catalog=CATALOG,
            inventory={"A": 10},
            settings=SETTINGS,
        )
        self.assertEqual(plan.days, 4)
        self.assertFalse(plan.all_reached)
        self.assertEqual(plan.stock_empty_day, 4)
        self.assertEqual(plan.day_plans[-1].inventory_after, {"A": 0})

    def test_reaches_target_with_daily_extra(self):
        profile = _profile("A角", level=9, exp=15600, target=10, extra=400, extra_enabled=True)
        plan = Calc.calculate_days_to_target(
            profiles=[profile],
            gift_catalog=CATALOG,
            inventory={},
            settings=SETTINGS,
        )
        self.assertEqual(plan.days, 1)
        self.assertTrue(plan.all_reached)
        self.assertIsNone(plan.stock_empty_day)

    def test_no_progress_stops_immediately(self):
        plan = Calc.calculate_days_to_target(
            profiles=[_profile("A角")],
            gift_catalog=CATALOG,
            inventory={},
            settings=SETTINGS,
        )
        self.assertEqual(plan.days, 0)
        self.assertFalse(plan.all_reached)

    def test_no_active_profiles(self):
        plan = Calc.calculate_days_to_target(
            profiles=[_profile("未设置", level=0)],
            gift_catalog=CATALOG,
            inventory={"A": 10},
            settings=SETTINGS,
        )
        self.assertEqual(plan.days, 0)
        self.assertEqual(plan.day_plans, ())


class TestPurchaseNeed(unittest.TestCase):
    def test_rounds_up_to_whole_gifts(self):
        need = Calc.calculate_purchase_need(250, 200)
        self.assertEqual((need.quantity, need.supplied_exp, need.overflow_exp), (2, 400, 150))

    def test_exact_tier_multiple(self):
        need = Calc.calculate_purchase_need(400, 400)
        self.assertEqual((need.quantity, need.supplied_exp, need.overflow_exp), (1, 400, 0))

    def test_zero_need(self):
        need = Calc.calculate_purchase_need(0, 200)
        self.assertEqual((need.quantity, need.supplied_exp, need.overflow_exp), (0, 0, 0))

    def test_stock_is_deducted_before_purchase(self):
        need = Calc.calculate_purchase_need(250, 200, available_exp=200)
        self.assertEqual((need.quantity, need.supplied_exp, need.overflow_exp), (1, 200, 150))

    def test_stock_covers_need(self):
        need = Calc.calculate_purchase_need(100, 200, available_exp=200)
        self.assertEqual((need.quantity, need.supplied_exp, need.overflow_exp), (0, 0, 0))

    def test_invalid_tier(self):
        with self.assertRaises(ValueError):
            Calc.calculate_purchase_need(100, 150)


if __name__ == "__main__":
    unittest.main()
