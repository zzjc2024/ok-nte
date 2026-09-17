import threading
import time
import unittest
from unittest.mock import Mock, patch

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
        task._alert_interrupt = threading.Event()
        task._last_zankou_combo_at = 0.0
        task._last_combo_finished_at = 0.0
        task._last_dodge_was_perfect = False
        task.send_key = Mock()
        task.COMBO_RECOVERY_MAX = 3
        task.COMBO_RECOVER_SWITCH_RETRIES = 3
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

    def test_no_gold_timeouts_recover_then_raise_after_max(self):
        # 长按超时(掉不掉血都一样) -> 切达芙蒂尔上场打一轮再回来; 超过预算才抛异常
        self.task._hold_until_gold = Mock(return_value=(HoldResult.NO_GOLD, True))
        self.task._recover_on_daffodill = Mock(return_value=True)

        with self.assertRaises(ZankouComboAnomaly):
            self.task._zankou_hold_with_recovery()

        self.assertEqual(self.task._recover_on_daffodill.call_count, 3)

    def test_no_gold_after_damage_recovers_then_gold(self):
        # 掉血不再触发普通闪避, 也不再立即异常: 走达芙蒂尔恢复后重打成功
        self.task._hold_until_gold = Mock(
            side_effect=[(HoldResult.NO_GOLD, True), (HoldResult.GOLD, False)]
        )
        self.task._recover_on_daffodill = Mock(return_value=True)

        self.assertIs(self.task._zankou_hold_with_recovery(), HoldResult.GOLD)
        self.task._recover_on_daffodill.assert_called_once()

    def test_hold_marks_normal_dodge_not_perfect(self):
        def find_one(*args, **kwargs):
            self.task._dodge_heard_at = time.time() + 1
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
        self.task.COMBO_RECOVERY_MAX = 2
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
        self.task.COMBO_RECOVERY_MAX = 2
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

    def test_no_gold_raises_only_after_recovery_budget_exhausted(self):
        # "没掉血 + 没金 E"不再立即异常: 先走达芙蒂尔恢复, 预算耗尽才停任务
        self.task.COMBO_RECOVERY_MAX = 1
        self.task._hold_until_gold = Mock(return_value=(HoldResult.NO_GOLD, False))
        self.task._recover_on_daffodill = Mock(return_value=True)

        with self.assertRaises(ZankouComboAnomaly):
            self.task._zankou_hold_with_recovery()

        self.task._recover_on_daffodill.assert_called_once()

    def test_recovery_resets_second_gold_context(self):
        # 切走又切回来, 闪避攻击/蓄力上下文已不在: 恢复后的长按回到等第一次金 E
        self.task._hold_until_gold = Mock(
            side_effect=[(HoldResult.NO_GOLD, False), (HoldResult.GOLD, False)]
        )
        self.task._recover_on_daffodill = Mock(return_value=True)

        self.assertIs(
            self.task._zankou_hold_with_recovery(require_second_gold=True), HoldResult.GOLD
        )

        calls = self.task._hold_until_gold.call_args_list
        self.assertTrue(calls[0].kwargs["require_second_gold"])
        self.assertFalse(calls[1].kwargs["require_second_gold"])

    def test_hold_waits_out_zankou_combo_linger(self):
        # 残虹二连(松开+单击)后攻击动作残留 1.5s: 期间开始新长按必被动画吃掉, 先补足等待
        self.task._last_combo_finished_at = time.time() - 0.5
        self.task.find_one = Mock(return_value=Mock(confidence=0.9))

        result, _damaged = self.task._hold_until_gold()

        self.assertIs(result, HoldResult.GOLD)
        linger_calls = [
            call
            for call in self.task.sleep.call_args_list
            if call.args and isinstance(call.args[0], float) and 0.9 <= call.args[0] <= 1.1
        ]
        self.assertEqual(len(linger_calls), 1)

    def test_hold_skips_linger_when_no_recent_combo(self):
        # 没有刚完成的二连(比如长按超时后的恢复路径): 不引入额外等待
        self.task.find_one = Mock(return_value=Mock(confidence=0.9))

        result, _damaged = self.task._hold_until_gold()

        self.assertIs(result, HoldResult.GOLD)
        self.task.sleep.assert_not_called()

    def test_zankou_combo_stamps_linger_time(self):
        self.task._zankou_hold_with_recovery = Mock(return_value=HoldResult.GOLD)

        self.task._zankou_combo()

        self.assertGreater(self.task._last_combo_finished_at, 0.0)

    def test_zankou_combo_interrupted_does_not_stamp_linger(self):
        # 被警报打断的长按没有完成二连: 不允许写残留时间戳, 否则重试节奏被拖慢
        self.task._zankou_hold_with_recovery = Mock(return_value=HoldResult.INTERRUPTED)

        self.assertTrue(self.task._zankou_combo_interruptible())

        self.assertEqual(self.task._last_combo_finished_at, 0.0)

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

    def test_recovery_switch_back_fails_gives_up(self):
        self.task._switch_to = Mock()
        self.task._verify_current = Mock(return_value=False)
        self.task._zankou_combo = Mock()
        self.task.sleep = Mock()

        self.assertFalse(self.task._recover_on_daffodill())

        self.task._zankou_combo.assert_not_called()

    def test_recovery_switches_back_fast_without_daffodill_window(self):
        # 达芙恢复登场不复用 _daffodill_window(至少1.5s): 普攻+连按切回键, 第一时间切回
        self.task._switch_to = Mock()
        self.task._verify_current = Mock(return_value=True)
        self.task._zankou_combo = Mock()
        self.task._cast_q = Mock()
        self.task.sleep = Mock()
        self.task.chars[1].ultimate_available = Mock(return_value=False)

        self.assertTrue(self.task._recover_on_daffodill())

        self.assertEqual(self.task._switch_to.call_count, 2)  # 达芙进 + 切回残虹
        self.task._switch_to.assert_called_with(self.task.zankou, attack_while_waiting=True)
        self.task._zankou_combo.assert_called_once()
        self.task._cast_q.assert_not_called()

    def test_recovery_takes_combo_before_daffodill_q(self):
        # 达芙Q时停: 先切回残虹吃一套二连, 再切回达芙放Q, 最后切回残虹
        self.task._switch_to = Mock()
        self.task._verify_current = Mock(return_value=True)
        self.task._zankou_combo = Mock()
        self.task._cast_q = Mock()
        self.task.sleep = Mock()
        self.task.chars[1].ultimate_available = Mock(return_value=True)

        self.assertTrue(self.task._recover_on_daffodill())

        self.assertEqual(self.task._switch_to.call_count, 4)  # 达芙进/切回/再进/再切回
        self.task._zankou_combo.assert_called_once()
        self.task._cast_q.assert_called_once_with(self.task.daffodill)

    def test_recovery_q_branch_skipped_when_switch_back_fails(self):
        # 切回残虹失败(被控): 不打二连也不放Q, 直接放弃本轮
        self.task._switch_to = Mock()
        self.task._verify_current = Mock(side_effect=[True, False, False, False])
        self.task._zankou_combo = Mock()
        self.task._cast_q = Mock()
        self.task.sleep = Mock()
        self.task.chars[1].ultimate_available = Mock(return_value=True)

        self.assertFalse(self.task._recover_on_daffodill())

        self.task._zankou_combo.assert_not_called()
        self.task._cast_q.assert_not_called()

    def test_switch_key_maps_zankou_to_named_constant(self):
        self.assertEqual(self.task.ZANKOU_SWITCH_KEY, 1)
        self.assertEqual(self.task._switch_key(self.task.zankou), self.task.ZANKOU_SWITCH_KEY)
        self.assertEqual(self.task._switch_key(self.task.chars[3]), 4)

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

    def test_confirm_switch_clicks_while_waiting(self):
        # attack_while_waiting: 达芙在场等待切回期间连点普攻
        task = self.task
        task.SWITCH_CONFIRM_TIMEOUT = 3.0
        task.SWITCH_CONFIRM_STABLE = 0.0
        task.click = Mock()
        det_pending = Mock(accepted=False, index=1, reason="pending")
        det_ok = Mock(accepted=True, index=1, reason="ok")
        task._get_current_char_detection = Mock(side_effect=[det_pending, det_ok])

        pressed_at = task._confirm_switch(task.chars[1], task.chars[0], attack_while_waiting=True)

        self.assertGreater(pressed_at, 0.0)
        task.click.assert_called_once()  # 确认前正好一次普攻

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
        task._confirm_switch.assert_called_once_with(
            task.chars[1], task.chars[0], attack_while_waiting=False
        )

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
