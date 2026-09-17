import threading
import time
import unittest
from unittest.mock import Mock, patch

from src.combat.BaseCombatTask import SleepCheckSkip
from src.tasks.trigger.FourCharComboTask import (
    DodgeRetry,
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
        task._dodge_motion_heard = threading.Event()
        task._alert_interrupt = threading.Event()
        task._last_zankou_combo_at = 0.0
        task._last_dodge_was_perfect = False
        task.send_key = Mock()
        task.COMBO_DODGE_RETRY_MAX = 3
        task.sleep = Mock()
        task.mouse_down = Mock()
        task.mouse_up = Mock()
        task._set_action_phase = Mock()
        task.click = Mock()
        task.find_one = Mock(return_value=None)
        task._health_pixels = Mock(return_value=300)
        task._recover_from_dodge = Mock(return_value=False)
        task._raise_combo_anomaly = Mock(side_effect=ZankouComboAnomaly("anomaly"))
        task.chars = [Mock(index=index) for index in range(4)]
        self.task = task

    def test_zankou_combo_skipped_after_sound_path_combo(self):
        # 声音路径(闪避成功反击)刚打完 -> 主循环不要再打一套(21:14 异常的根因)
        self.task._zankou_hold_with_recovery = Mock(return_value=HoldResult.GOLD)
        self.task._last_zankou_combo_at = time.time()

        self.task._zankou_combo()

        self.task._zankou_hold_with_recovery.assert_not_called()

    def test_zankou_combo_runs_when_stamp_is_old(self):
        self.task._zankou_hold_with_recovery = Mock(return_value=HoldResult.GOLD)
        self.task._last_zankou_combo_at = time.time() - 10

        self.task._zankou_combo()

        self.task._zankou_hold_with_recovery.assert_called_once()

    def test_sound_success_zankou_combo_stamps_timestamp(self):
        self.task.get_current_char = Mock(return_value=self.task.zankou)
        self.task._zankou_combo_interruptible = Mock(return_value=False)

        self.task._sound_dodge_success_action()

        self.assertGreater(self.task._last_zankou_combo_at, 0.0)

    def test_sound_success_on_other_char_only_clicks(self):
        # 非残虹: 只点左键触发闪避反击, 不强行切人, 也不接管主循环的二连
        self.task.get_current_char = Mock(return_value="Sakiri")
        self.task._switch_to = Mock()

        self.task._sound_dodge_success_action()

        self.task.click.assert_called_once()
        self.task.send_key.assert_not_called()
        self.task._switch_to.assert_not_called()
        self.assertEqual(self.task._last_zankou_combo_at, 0.0)

    def test_dodge_not_confirmed_retries_then_raises(self):
        # 角色正被连击时闪不出来: 本轮只记日志继续再试, 几轮都不行才抛异常
        self.task.COMBO_DODGE_RETRY_MAX = 3
        self.task._hold_until_gold = Mock(return_value=(HoldResult.NO_GOLD, True))
        self.task._dodge_until_triggered = Mock(return_value=DodgeRetry.NOT_TRIGGERED)

        with self.assertRaises(ZankouComboAnomaly):
            self.task._zankou_hold_with_recovery()

        self.assertEqual(self.task._dodge_until_triggered.call_count, 3)

    def test_dodge_triggered_after_damage_returns_gold(self):
        self.task._hold_until_gold = Mock(
            side_effect=[(HoldResult.NO_GOLD, True), (HoldResult.GOLD, False)]
        )
        self.task._dodge_until_triggered = Mock(return_value=DodgeRetry.TRIGGERED)

        self.assertIs(self.task._zankou_hold_with_recovery(), HoldResult.GOLD)
        self.assertEqual(self.task._dodge_until_triggered.call_count, 1)

    def test_hold_marks_normal_dodge_not_perfect(self):
        def find_one(*args, **kwargs):
            self.task._dodge_heard_at = time.time() + 1
            self.task._dodge_motion_heard.set()
            return None

        self.task.find_one = Mock(side_effect=find_one)

        self.task._hold_until_gold()

        self.assertFalse(self.task._last_dodge_was_perfect)

    def test_hold_marks_perfect_dodge(self):
        def find_one(*args, **kwargs):
            self.task._dodge_heard_at = time.time() + 1
            self.task._dodge_success_heard.set()
            return None

        self.task.find_one = Mock(side_effect=find_one)

        self.task._hold_until_gold()

        self.assertTrue(self.task._last_dodge_was_perfect)

    def test_dodge_retry_waits_for_first_gold_after_normal_dodge(self):
        self.task.COMBO_DODGE_RETRY_MAX = 2
        self.task._hold_until_gold = Mock(
            side_effect=[(HoldResult.DODGE, False), (HoldResult.GOLD, False)]
        )
        self.task._recover_from_dodge = Mock(return_value=False)
        self.task._last_dodge_was_perfect = False

        result = self.task._zankou_hold_with_recovery()

        self.assertIs(result, HoldResult.GOLD)
        calls = self.task._hold_until_gold.call_args_list
        self.assertFalse(calls[0].kwargs["require_second_gold"])
        self.assertFalse(calls[1].kwargs["require_second_gold"])

    def test_dodge_retry_waits_for_second_gold_after_perfect_dodge(self):
        self.task.COMBO_DODGE_RETRY_MAX = 2
        self.task._hold_until_gold = Mock(
            side_effect=[(HoldResult.DODGE, False), (HoldResult.GOLD, False)]
        )
        self.task._recover_from_dodge = Mock(return_value=False)
        self.task._last_dodge_was_perfect = True

        self.task._zankou_hold_with_recovery()

        calls = self.task._hold_until_gold.call_args_list
        self.assertFalse(calls[0].kwargs["require_second_gold"])
        self.assertTrue(calls[1].kwargs["require_second_gold"])

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
        self.task._verify_current = Mock(return_value=True)

        with self.assertRaises(ZankouComboAnomaly):
            self.task._zankou_hold_with_recovery()

        self.task._raise_combo_anomaly.assert_called_once()

    def test_wait_controllable_needs_raw_cd_to_tick(self):
        # 实测 bug: 大招一按下去 Q 图标就变灭, 但此时还在特写里(in_team=False),
        # 早雾的 E 连按 2s 全废。有冷却数字时必须等原始数字真的变小。
        self.task.CONTROLLABLE_TIMEOUT = 0.4
        self.task._q_button_lit = Mock(return_value=False)
        self.task._raw_ultimate_cd = Mock(return_value=20.0)
        self.task.is_in_team = Mock(return_value=False)

        self.assertFalse(self.task._wait_controllable(self.task.zankou, was_lit=True))

    def test_wait_controllable_returns_when_raw_cd_ticks(self):
        start = time.time()

        def raw_cd():
            return 20.0 if time.time() - start < 0.15 else 19.8

        self.task.CONTROLLABLE_TIMEOUT = 1.0
        self.task._q_button_lit = Mock(return_value=False)
        self.task._raw_ultimate_cd = Mock(side_effect=raw_cd)

        self.assertTrue(self.task._wait_controllable(self.task.zankou, was_lit=True))

    def test_skill_waits_for_team_before_pressing_e(self):
        # 特写期间(in_team=False)不能按 E
        char = Mock()
        char.has_cd = Mock(side_effect=[False, True])
        char.skill_available = Mock(return_value=True)
        char.send_skill_key = Mock()
        self.task.is_in_team = Mock(return_value=False)
        self.task._wait_in_team = Mock()
        self.task.box_highlighted = Mock(return_value=0)
        self.task.get_cd = Mock(return_value=0.0)

        self.assertTrue(self.task._skill_until_registered(char))

        char.send_skill_key.assert_not_called()
        self.task._wait_in_team.assert_called_once()

    def test_no_gold_but_wrong_char_reswitches_instead_of_anomaly(self):
        # 实测 bug: 切人高亮误报 confirmed, 场上其实还是早雾 -> 不该抛异常停任务
        self.task._hold_until_gold = Mock(
            side_effect=[(HoldResult.NO_GOLD, False), (HoldResult.GOLD, False)]
        )
        self.task._verify_current = Mock(side_effect=[False, True])
        self.task._switch_to = Mock()
        self.task.get_current_char = Mock(return_value="Sakiri")

        result = self.task._zankou_hold_with_recovery()

        self.assertIs(result, HoldResult.GOLD)
        self.task._switch_to.assert_called_once()
        self.task._raise_combo_anomaly.assert_not_called()

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


class TestSwitchEntrySkillAnchor(unittest.TestCase):
    """回归: 入场技计时锚点必须是第一次按下切人键, 不是 _switch_to 入口时刻.

    实机踩过: 切人前等特写(_wait_in_team 最长 10s)/确认重试把窗口整体推后,
    长按落在入场技中间 -> 金 E 全空 -> 误报状态异常停任务。
    """

    def setUp(self):
        task = object.__new__(FourCharComboTask)
        task.sleep_check_skip = SleepCheckSkip()
        task._entry_skill_until = 0.0
        task.send_key = Mock()
        task.sleep = Mock()
        task.chars = [Mock(index=index) for index in range(4)]
        task._set_current_char = Mock()
        # frame/team_size 是只读 property; bare 实例没有截图栈, 类级别替换成普通值
        for target, value in ((FourCharComboTask, "frame"), (FourCharComboTask, "team_size")):
            patcher = patch.object(target, value, None if value == "frame" else 4)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.task = task

    def test_confirm_switch_returns_first_press_time(self):
        task = self.task
        task.SWITCH_CONFIRM_TIMEOUT = 3.0
        task.SWITCH_CONFIRM_STABLE = 0.0
        det_pending = Mock(accepted=False, index=-1, reason="pending")
        det_ok = Mock(accepted=True, index=1, reason="ok")
        task._get_current_char_detection = Mock(side_effect=[det_pending, det_ok])
        before = time.time()

        pressed_at = task._confirm_switch(task.chars[1], task.chars[0])

        after = time.time()
        self.assertGreater(pressed_at, 0.0)
        self.assertGreaterEqual(pressed_at, before)
        self.assertLessEqual(pressed_at, after)
        task.send_key.assert_called_once()  # 一次按压后确认, 锚点就是这一按

    def test_confirm_switch_without_press_returns_zero(self):
        # 检测在按压前就稳定确认(切人早已发生) -> 无锚点, 返回 0
        task = self.task
        task.SWITCH_CONFIRM_TIMEOUT = 3.0
        task.SWITCH_CONFIRM_STABLE = 0.0
        task._get_current_char_detection = Mock(
            return_value=Mock(accepted=True, index=1, reason="ok")
        )

        self.assertEqual(task._confirm_switch(task.chars[1], task.chars[0]), 0.0)
        task.send_key.assert_not_called()

    def test_confirm_switch_timeout_returns_zero(self):
        task = self.task
        task.SWITCH_CONFIRM_TIMEOUT = 0.0
        task._get_current_char_detection = Mock(
            return_value=Mock(accepted=False, index=-1, reason="pending")
        )

        self.assertEqual(task._confirm_switch(task.chars[1], task.chars[0]), 0.0)

    def test_entry_skill_window_anchored_at_first_press(self):
        task = self.task
        pressed_at = time.time() - 0.5
        task.get_current_char = Mock(return_value=task.chars[0])
        task._switch_triggers_entry_skill = Mock(return_value=True)
        task._wait_in_team = Mock()
        task._confirm_switch = Mock(return_value=pressed_at)

        task._switch_to(task.chars[1])

        self.assertAlmostEqual(
            task._entry_skill_until,
            pressed_at + task.ZANKOU_ENTRY_SKILL_WAIT,
            places=6,
        )
        task._confirm_switch.assert_called_once_with(task.chars[1], task.chars[0])

    def test_entry_skill_window_cleared_without_prediction(self):
        task = self.task
        task.get_current_char = Mock(return_value=task.chars[0])
        task._switch_triggers_entry_skill = Mock(return_value=False)
        task._wait_in_team = Mock()
        task._confirm_switch = Mock(return_value=time.time())

        task._switch_to(task.chars[1])

        self.assertEqual(task._entry_skill_until, 0.0)

    def test_entry_skill_window_cleared_when_switch_not_confirmed(self):
        task = self.task
        task.get_current_char = Mock(return_value=task.chars[0])
        task._switch_triggers_entry_skill = Mock(return_value=True)
        task._wait_in_team = Mock()
        task._confirm_switch = Mock(return_value=0.0)

        task._switch_to(task.chars[1])

        self.assertEqual(task._entry_skill_until, 0.0)

    def test_reset_precombat_clears_entry_skill_window(self):
        task = self.task
        task._opener_gold_e_done = True
        task._precombat_daffodill_q_done = True
        task._entry_skill_until = time.time() + 5
        task._alert_interrupt = threading.Event()
        task._dodge_motion_heard = threading.Event()
        task._dodge_success_heard = threading.Event()

        task._reset_precombat()

        self.assertEqual(task._entry_skill_until, 0.0)
        self.assertFalse(task._opener_gold_e_done)


if __name__ == "__main__":
    unittest.main()
