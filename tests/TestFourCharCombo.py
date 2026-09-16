import threading
import time
import unittest
from unittest.mock import Mock

from src.combat.BaseCombatTask import SleepCheckSkip
from src.tasks.trigger.FourCharComboTask import (
    FourCharComboTask,
    HoldResult,
    ZankouComboAnomaly,
)


class TestFourCharCombo(unittest.TestCase):
    """回归: 长按期间闪避触发不能被当成"没掉血 + 没金 E"的异常."""

    def setUp(self):
        task = object.__new__(FourCharComboTask)
        task.sleep_check_skip = SleepCheckSkip()
        task.COMBO_HOLD_MIN = 0.0
        task.COMBO_HOLD_MAX = 0.05
        task._entry_skill_until = 0.0
        task._dodge_heard_at = 0.0
        task._holding = False
        task._dodge_success_heard = threading.Event()
        task.sleep = Mock()
        task.mouse_down = Mock()
        task.mouse_up = Mock()
        task._set_action_phase = Mock()
        task.click = Mock()
        task.find_one = Mock(return_value=None)
        task._health_pixels = Mock(return_value=300)
        task._recover_from_dodge = Mock(return_value=False)
        task._raise_combo_anomaly = Mock(side_effect=ZankouComboAnomaly("anomaly"))
        self.task = task

    def test_hold_returns_dodge_when_dodge_heard_during_hold(self):
        def find_one(*args, **kwargs):
            self.task._dodge_heard_at = time.time() + 1
            return None

        self.task.find_one = Mock(side_effect=find_one)

        result, damaged = self.task._hold_until_gold()

        self.assertIs(result, HoldResult.DODGE)
        self.assertFalse(damaged)
        self.assertFalse(self.task._holding)

    def test_hold_without_gold_or_dodge_returns_no_gold(self):
        result, damaged = self.task._hold_until_gold()

        self.assertIs(result, HoldResult.NO_GOLD)
        self.assertFalse(damaged)
        self.assertFalse(self.task._holding)

    def test_dodge_hold_recovers_instead_of_raising_anomaly(self):
        self.task._hold_until_gold = Mock(return_value=(HoldResult.DODGE, False))
        self.task._recover_from_dodge = Mock(return_value=True)

        self.assertIs(self.task._zankou_hold_with_recovery(), HoldResult.HANDLED)

        self.task._recover_from_dodge.assert_called_once_with()
        self.task._raise_combo_anomaly.assert_not_called()

    def test_no_gold_and_no_damage_still_raises_anomaly(self):
        self.task._hold_until_gold = Mock(return_value=(HoldResult.NO_GOLD, False))

        with self.assertRaises(ZankouComboAnomaly):
            self.task._zankou_hold_with_recovery()

        self.task._raise_combo_anomaly.assert_called_once()

    def test_dodge_success_reaction_defers_to_active_hold(self):
        self.task._holding = True
        self.task.get_current_char = Mock()

        self.task._sound_dodge_success_action()

        self.assertTrue(self.task._dodge_success_heard.is_set())
        self.task.click.assert_not_called()
        self.task.get_current_char.assert_not_called()

    def _conf_sequence(self, values):
        remaining = list(values)

        def find_one(*args, **kwargs):
            if not remaining:
                return None
            return Mock(confidence=remaining.pop(0))

        return Mock(side_effect=find_one)

    def test_second_gold_exits_early_when_charge_seen(self):
        start = time.time()

        def find_one(*args, **kwargs):
            elapsed = time.time() - start
            if elapsed < 0.05:
                return Mock(confidence=0.85)  # 第一次金 E(闪避攻击)
            if elapsed < 0.35:
                return Mock(confidence=0.10)  # 蓄力/白: 金 E 消失
            return Mock(confidence=0.80)  # 第二次金 E

        self.task.DODGE_COMBO_HOLD = 1.0
        self.task.find_one = Mock(side_effect=find_one)

        result, damaged = self.task._hold_until_gold(require_second_gold=True)

        self.assertIs(result, HoldResult.GOLD)
        self.assertFalse(damaged)
        self.assertLess(time.time() - start, 1.0)  # 提前收手, 没等到上限

    def test_charge_white_score_is_not_treated_as_gold(self):
        # 录屏实测: 蓄力/白阶段 gold 模板 0.42~0.61, 真金 0.71~0.88
        # 0.55 必须算"金 E 没了", 否则又回到"全程 gold"的老bug
        start = time.time()

        def find_one(*args, **kwargs):
            elapsed = time.time() - start
            if elapsed < 0.05:
                return Mock(confidence=0.85)  # 第一次金 E
            if elapsed < 0.35:
                return Mock(confidence=0.55)  # 蓄力/白
            return Mock(confidence=0.80)  # 第二次金 E

        self.task.DODGE_COMBO_HOLD = 1.0
        self.task.find_one = Mock(side_effect=find_one)

        result, _damaged = self.task._hold_until_gold(require_second_gold=True)

        self.assertIs(result, HoldResult.GOLD)
        self.assertLess(time.time() - start, 1.0)

    def test_dodge_hold_proceeds_at_deadline_when_charge_never_seen(self):
        # 万一没看到蓄力(比如被特效糊住), 到点也必须照常收手点按, 不能抛异常
        self.task.DODGE_COMBO_HOLD = 0.3
        self.task.find_one = self._conf_sequence([0.85, 0.85, 0.85, 0.85])

        result, damaged = self.task._hold_until_gold(require_second_gold=True)

        self.assertIs(result, HoldResult.GOLD)
        self.assertFalse(damaged)


if __name__ == "__main__":
    unittest.main()
