import time
import unittest
from unittest.mock import Mock

from src.combat.BaseCombatTask import SleepCheckSkip
from src.tasks.trigger.FourCharComboTask import HoldResult
from src.tasks.trigger.ZankouComboHotkeyTask import ZankouComboHotkeyTask


def _make_task():
    """只替换 I/O 和继承来的重逻辑; 被测的 F12 方法保持真身."""
    task = object.__new__(ZankouComboHotkeyTask)
    task.sleep_check_skip = SleepCheckSkip()
    task.COMBO_RELEASE_GAP = 0.06
    task.COMBO_CLICK_GAP = 0.05
    task.DODGE_COUNTER_WAIT = 0.2
    task.SOUND_SUCCESS_CLICK_DOWN = 0.08
    task.ZANKOU_ENTRY_SKILL_WAIT = 1.1
    task.E_CD_WAIT_TIMEOUT = 0.0
    task.F12_MIN_INTERVAL = 1.5
    task.HOLD_RETRIES = 3
    task.IROI_E_WAIT_TIMEOUT = 0.0
    task._last_f12_at = 0.0
    task._last_combo_finished_at = 0.0
    task._abort_f12 = False
    task._holding = False
    task._alert_interrupt = Mock()
    task._dodge_success_heard = Mock()
    task._last_dodge_was_perfect = False
    task.chars = [Mock(index=index) for index in range(4)]
    # self.frame -> self.executor.frame
    task._executor = Mock()
    task.sleep = Mock()
    task.click = Mock()
    task.send_key = Mock()
    task.log_info = Mock()
    task.log_warning = Mock()
    task.log_error = Mock()
    task._set_action_phase = Mock()
    task.cycle_ratio = Mock(return_value=0.5)
    task._is_current = Mock(return_value=True)
    task._get_current_char_detection = Mock(
        return_value=Mock(accepted=True, index=0, reason="ok")
    )
    task._ultimate_available = Mock(return_value=True)
    task._raw_skill_cd = Mock(return_value=0.0)
    task.box_highlighted = Mock(return_value=1)
    task.get_skill_key = Mock(return_value="e")
    task.get_current_char = Mock(return_value=task.chars[0])
    task.refresh_cd = Mock()
    task.cds = {}
    # 继承来的重逻辑(已经单测过), F12 单测里替换掉
    task._hold_until_gold = Mock(return_value=(HoldResult.GOLD, False))
    task._zankou_combo_interruptible = Mock(return_value=False)
    task._iroi_q_funnel = Mock()
    task._sound_dodge_action = Mock()
    return task


def _switch_keys(task):
    return [call.args[0] for call in task.send_key.call_args_list]


class TestZankouComboHotkey(unittest.TestCase):
    """F12 手动辅助: 二连 -> 按环合切伊洛伊(放E/Q+浮游炮)或达芙(+5次普攻)."""

    # ------------------------------------------------------------ 主流程

    def test_combo_presses_1_first(self):
        task = _make_task()
        task._zankou_combo_once = Mock(return_value=0.5)

        task._run_combo()

        self.assertEqual(_switch_keys(task)[0], 1)

    def test_combo_routes_to_daffodill_when_cycle_low(self):
        task = _make_task()
        task._zankou_combo_once = Mock(return_value=task.CYCLE_SWITCH_RATIO - 0.01)
        task._switch_after_combo = Mock()
        task._iroi_flow = Mock()

        task._run_combo()

        task._switch_after_combo.assert_called_once_with(task.DAFFODILL_INDEX)
        task._iroi_flow.assert_not_called()

    def test_combo_routes_to_iroi_when_cycle_full(self):
        task = _make_task()
        task._zankou_combo_once = Mock(return_value=task.CYCLE_SWITCH_RATIO)
        task._switch_after_combo = Mock()
        task._iroi_flow = Mock()

        task._run_combo()

        task._iroi_flow.assert_called_once()
        task._switch_after_combo.assert_not_called()

    def test_combo_stops_when_gold_never_shows(self):
        task = _make_task()
        task._zankou_combo_once = Mock(return_value=None)
        task._switch_after_combo = Mock()
        task._iroi_flow = Mock()

        task._run_combo()

        task._iroi_flow.assert_not_called()
        task._switch_after_combo.assert_not_called()
        self.assertEqual(task._last_f12_at, 0.0)

    def test_combo_skipped_when_previous_too_recent(self):
        task = _make_task()
        task._last_f12_at = time.time()

        task._run_combo()

        task.send_key.assert_not_called()

    # ------------------------------------------------------------ 切残虹

    def test_switch_to_zankou_presses_1_again_when_not_zankou(self):
        task = _make_task()
        task._is_current = Mock(side_effect=[False, True])

        task._switch_to_zankou()

        self.assertEqual(_switch_keys(task), [1, 1])
        settle = [c.args[0] for c in task.sleep.call_args_list[:2]]
        self.assertEqual(settle, [task.SWITCH_SETTLE_TIME] * 2)

    def test_switch_to_zankou_presses_once_when_already_zankou(self):
        task = _make_task()
        task._is_current = Mock(return_value=True)

        task._switch_to_zankou()

        self.assertEqual(_switch_keys(task), [1])

    def test_switch_after_daffodill_adds_five_clicks(self):
        task = _make_task()

        task._switch_after_combo(task.DAFFODILL_INDEX)

        self.assertEqual(_switch_keys(task), [task.DAFFODILL_INDEX + 1])
        self.assertEqual(task.click.call_count, task.DAFFODILL_PAD_CLICKS)

    def test_switch_after_iroi_adds_no_clicks(self):
        task = _make_task()

        task._switch_after_combo(task.IROI_INDEX)

        self.assertEqual(_switch_keys(task), [task.IROI_INDEX + 1])
        task.click.assert_not_called()

    # ------------------------------------------------------------ 二连

    def test_combo_once_reads_cycle_between_release_and_click(self):
        task = _make_task()
        order = []
        task.cycle_ratio = Mock(side_effect=lambda: order.append("ratio") or 0.5)
        task.click = Mock(side_effect=lambda *a, **k: order.append("click"))

        ratio = task._zankou_combo_once()

        self.assertEqual(ratio, 0.5)
        self.assertEqual(order[0], "ratio")
        self.assertEqual(order[1], "click")

    def test_combo_once_retries_after_dodge_interrupt(self):
        task = _make_task()
        task._hold_until_gold = Mock(
            side_effect=[(HoldResult.DODGE, False), (HoldResult.GOLD, False)]
        )

        ratio = task._zankou_combo_once()

        self.assertEqual(ratio, 0.5)
        self.assertEqual(task._hold_until_gold.call_count, 2)
        # 闪避打断后要补点一次左键触发闪避反击
        self.assertGreaterEqual(task.click.call_count, 2)

    def test_combo_once_retries_after_alert_interrupt(self):
        task = _make_task()
        task._hold_until_gold = Mock(
            side_effect=[(HoldResult.INTERRUPTED, False), (HoldResult.GOLD, False)]
        )

        ratio = task._zankou_combo_once()

        self.assertEqual(ratio, 0.5)
        self.assertEqual(task._hold_until_gold.call_count, 2)

    def test_combo_once_gives_up_after_retries(self):
        # 没出金 E 且没被打断: 不重试(重试只针对警报/闪避打断), 直接结束
        task = _make_task()
        task._hold_until_gold = Mock(return_value=(HoldResult.NO_GOLD, False))

        ratio = task._zankou_combo_once()

        self.assertIsNone(ratio)
        self.assertEqual(task._hold_until_gold.call_count, 1)

    def test_combo_once_gives_up_after_interrupt_retries(self):
        task = _make_task()
        task._hold_until_gold = Mock(return_value=(HoldResult.INTERRUPTED, False))

        ratio = task._zankou_combo_once()

        self.assertIsNone(ratio)
        self.assertEqual(task._hold_until_gold.call_count, task.HOLD_RETRIES)

    # ------------------------------------------------------------ 伊洛伊流程

    def test_iroi_flow_e_registered_and_q_ready(self):
        task = _make_task()
        task._wait_e_cd_after_cast = Mock()
        task._zankou_second_combo = Mock()
        task._switch_after_combo = Mock()
        task._press_e_until_registered = Mock(return_value=True)
        task._ultimate_available = Mock(return_value=True)

        task._iroi_flow()

        task._press_e_until_registered.assert_called_once()
        task._wait_e_cd_after_cast.assert_called_once()
        task._iroi_q_funnel.assert_called_once()
        task._zankou_second_combo.assert_called_once()
        task._switch_after_combo.assert_not_called()

    def test_iroi_flow_stops_when_q_not_ready_after_e(self):
        task = _make_task()
        task._wait_e_cd_after_cast = Mock()
        task._zankou_second_combo = Mock()
        task._switch_after_combo = Mock()
        task._press_e_until_registered = Mock(return_value=True)
        task._ultimate_available = Mock(return_value=False)

        task._iroi_flow()

        task._iroi_q_funnel.assert_not_called()
        task._zankou_second_combo.assert_not_called()
        task._switch_after_combo.assert_not_called()

    def test_iroi_flow_goes_daffodill_when_e_and_q_unavailable(self):
        task = _make_task()
        task._wait_e_cd_after_cast = Mock()
        task._zankou_second_combo = Mock()
        task._switch_after_combo = Mock()
        task._press_e_until_registered = Mock(return_value=False)
        task._ultimate_available = Mock(return_value=False)

        task._iroi_flow()

        task._iroi_q_funnel.assert_not_called()
        task._switch_after_combo.assert_called_once_with(task.DAFFODILL_INDEX)

    def test_iroi_flow_q_only_when_e_on_cooldown(self):
        task = _make_task()
        task._wait_e_cd_after_cast = Mock()
        task._zankou_second_combo = Mock()
        task._switch_after_combo = Mock()
        task._press_e_until_registered = Mock(return_value=False)
        task._ultimate_available = Mock(return_value=True)

        task._iroi_flow()

        task._wait_e_cd_after_cast.assert_not_called()
        task._iroi_q_funnel.assert_called_once()
        task._zankou_second_combo.assert_called_once()

    def test_iroi_flow_stops_when_aborted(self):
        task = _make_task()
        task._wait_e_cd_after_cast = Mock()
        task._zankou_second_combo = Mock()
        task._switch_after_combo = Mock()
        task._abort_f12 = True

        task._iroi_flow()

        task._iroi_q_funnel.assert_not_called()
        task._zankou_second_combo.assert_not_called()

    def test_iroi_flow_switches_to_iroi_slot(self):
        task = _make_task()
        task._wait_e_cd_after_cast = Mock()
        task._zankou_second_combo = Mock()
        task._switch_after_combo = Mock()

        task._iroi_flow()

        self.assertEqual(_switch_keys(task)[0], task.IROI_INDEX + 1)

    def test_second_combo_then_daffodill(self):
        task = _make_task()
        task._zankou_combo_once = Mock(return_value=0.99)
        task._switch_after_combo = Mock()

        task._zankou_second_combo()

        task._switch_after_combo.assert_called_once_with(task.DAFFODILL_INDEX)

    # ------------------------------------------------------------ 伊洛伊 E

    def test_press_e_returns_false_when_already_on_cd(self):
        task = _make_task()
        task._raw_skill_cd = Mock(return_value=12.0)

        self.assertFalse(task._press_e_until_registered())
        task.send_key.assert_not_called()

    def test_press_e_registers_when_cd_appears(self):
        task = _make_task()
        task.IROI_E_WAIT_TIMEOUT = 0.05
        task._raw_skill_cd = Mock(side_effect=[0.0, 12.0])

        self.assertTrue(task._press_e_until_registered())
        self.assertGreaterEqual(task.send_key.call_count, 1)

    def test_press_e_gives_up_after_timeout(self):
        task = _make_task()
        
        self.assertFalse(task._press_e_until_registered())

    # ------------------------------------------------------------ 声音

    def test_sound_non_zankou_clicks_then_aborts(self):
        task = _make_task()
        task._is_current = Mock(return_value=False)

        task._sound_dodge_success_action()

        task.click.assert_called_once()
        task._zankou_combo_interruptible.assert_not_called()
        self.assertTrue(task._abort_f12)

    def test_sound_zankou_combos_then_routes_to_daffodill(self):
        task = _make_task()
        task._is_current = Mock(return_value=True)
        task.cycle_ratio = Mock(return_value=0.5)
        task._switch_after_combo = Mock()

        task._sound_dodge_success_action()

        task._zankou_combo_interruptible.assert_called_once_with(require_second_gold=True)
        task._switch_after_combo.assert_called_once_with(task.DAFFODILL_INDEX)
        self.assertTrue(task._abort_f12)

    def test_sound_zankou_routes_to_iroi_when_cycle_full(self):
        task = _make_task()
        task._is_current = Mock(return_value=True)
        task.cycle_ratio = Mock(return_value=0.99)
        task._iroi_flow = Mock()

        task._sound_dodge_success_action()

        task._iroi_flow.assert_called_once()

    def test_sound_leaves_hold_alone_when_holding(self):
        task = _make_task()
        task._holding = True

        task._sound_dodge_success_action()

        task.click.assert_not_called()
        task._zankou_combo_interruptible.assert_not_called()

    def test_sound_retries_when_combo_interrupted(self):
        task = _make_task()
        task._is_current = Mock(return_value=True)
        task._zankou_combo_interruptible = Mock(side_effect=[True, False])
        task._switch_after_combo = Mock()

        task._sound_dodge_success_action()

        self.assertEqual(task._zankou_combo_interruptible.call_count, 2)
        task._sound_dodge_action.assert_called_once()

    # ------------------------------------------------------------ run

    def test_run_runs_combo_when_requested(self):
        task = _make_task()
        task._ensure_hotkey_listener = Mock()
        task._request = Mock()
        task._request.is_set = Mock(return_value=True)
        task.scene = Mock()
        task.scene.is_in_team = Mock(return_value=True)
        task._run_combo = Mock()

        task.run()

        task._run_combo.assert_called_once()

    def test_run_does_nothing_without_request(self):
        task = _make_task()
        task._ensure_hotkey_listener = Mock()
        task._request = Mock()
        task._request.is_set = Mock(return_value=False)
        task._run_combo = Mock()

        task.run()

        task._run_combo.assert_not_called()

    def test_run_ignores_when_not_in_team(self):
        task = _make_task()
        task._ensure_hotkey_listener = Mock()
        task._request = Mock()
        task._request.is_set = Mock(return_value=True)
        task.scene = Mock()
        task.scene.is_in_team = Mock(return_value=False)
        task._run_combo = Mock()

        task.run()

        task._run_combo.assert_not_called()

    def test_run_ignores_when_team_not_loaded(self):
        task = _make_task()
        task._ensure_hotkey_listener = Mock()
        task._request = Mock()
        task._request.is_set = Mock(return_value=True)
        task.scene = Mock()
        task.scene.is_in_team = Mock(return_value=True)
        task.chars = []
        task.load_chars = Mock(return_value=False)
        task._run_combo = Mock()

        task.run()

        task._run_combo.assert_not_called()


if __name__ == "__main__":
    unittest.main()
