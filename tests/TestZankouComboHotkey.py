import time
import unittest
from unittest.mock import Mock

from src.combat.BaseCombatTask import SleepCheckSkip
from src.tasks.trigger.ZankouComboHotkeyTask import ZankouComboHotkeyTask


def _make_task(mock_hold=True):
    task = object.__new__(ZankouComboHotkeyTask)
    task.sleep_check_skip = SleepCheckSkip()
    task.COMBO_HOLD_MIN = 0.0
    task.COMBO_HOLD_MAX = 0.05
    task.COMBO_POLL_INTERVAL = 0.0
    task.COMBO_MIN_INTERVAL = 1.5
    task._last_combo_at = 0.0
    task._request = Mock()
    task.sleep = Mock()
    task.click = Mock()
    task.mouse_down = Mock()
    task.mouse_up = Mock()
    task.send_key = Mock()
    task.find_one = Mock(return_value=None)
    task.log_info = Mock()
    task.log_warning = Mock()
    task.log_error = Mock()
    task._current_index = Mock(return_value=1)
    if mock_hold:
        task._hold_until_gold = Mock(return_value=True)
    return task


def _switch_keys(task):
    return [call.args[0] for call in task.send_key.call_args_list]


class TestZankouComboHotkey(unittest.TestCase):
    """F12 手动辅助: 切残虹(不等确认) -> 二连 -> 切回原角色 + 补 5 次普攻."""

    def test_combo_switches_to_zankou_then_back(self):
        task = _make_task()
        task._current_index = Mock(return_value=1)

        task._run_combo()

        # 1 号位(残虹) -> 2 号位(原角色)
        self.assertEqual(_switch_keys(task), [1, 2])
        # 二连的单击 + 切回后的 5 次普攻
        self.assertEqual(task.click.call_count, 1 + task.SWITCH_BACK_CLICKS)
        self.assertGreater(task._last_combo_at, 0.0)

    def test_combo_on_zankou_does_not_switch_or_pad(self):
        # 已经在残虹身上: 不切人, 也不补那 5 次普攻
        task = _make_task()
        task._current_index = Mock(return_value=0)

        task._run_combo()

        task.send_key.assert_not_called()
        task.click.assert_called_once()

    def test_combo_does_not_wait_for_switch_confirmation(self):
        task = _make_task()
        task._current_index = Mock(return_value=3)

        task._run_combo()

        # 按完切人键只等 SWITCH_SETTLE_TIME 就长按, 不做图像确认
        task._hold_until_gold.assert_called_once_with(extra=task.ENTRY_SKILL_EXTRA)
        self.assertEqual(task.sleep.call_args_list[0].args[0], task.SWITCH_SETTLE_TIME)

    def test_switch_back_clicks_are_spread_over_the_window(self):
        task = _make_task()
        task._current_index = Mock(return_value=1)

        task._run_combo()

        gaps = [call.args[0] for call in task.sleep.call_args_list]
        self.assertIn(task.SWITCH_BACK_CLICK_WINDOW / task.SWITCH_BACK_CLICKS, gaps)

    def test_combo_skipped_when_previous_combo_too_recent(self):
        task = _make_task()
        task._last_combo_at = time.time()

        task._run_combo()

        task.send_key.assert_not_called()
        task._hold_until_gold.assert_not_called()
        task.click.assert_not_called()

    def test_combo_aborts_when_current_char_unknown(self):
        task = _make_task()
        task._current_index = Mock(return_value=-1)

        task._run_combo()

        task.send_key.assert_not_called()
        task._hold_until_gold.assert_not_called()

    def test_combo_does_not_click_when_gold_never_shows(self):
        task = _make_task()
        task._hold_until_gold = Mock(return_value=False)

        task._run_combo()

        task.click.assert_not_called()
        self.assertEqual(task._last_combo_at, 0.0)

    def test_run_ignores_request_outside_team(self):
        task = _make_task()
        task._ensure_hotkey_listener = Mock()
        task._request.is_set = Mock(return_value=True)
        task._run_combo = Mock()
        task.scene = Mock()
        task.scene.is_in_team = Mock(return_value=False)

        task.run()

        task._run_combo.assert_not_called()
        task._request.clear.assert_called_once()

    def test_run_runs_combo_when_requested(self):
        task = _make_task()
        task._ensure_hotkey_listener = Mock()
        task._request.is_set = Mock(return_value=True)
        task._run_combo = Mock()
        task.scene = Mock()
        task.scene.is_in_team = Mock(return_value=True)

        task.run()

        task._run_combo.assert_called_once()

    def test_hold_accepts_gold_e_after_min_hold(self):
        task = _make_task(mock_hold=False)
        task.COMBO_HOLD_MIN = 0.0
        task.COMBO_HOLD_MAX = 0.2
        task.find_one = Mock(return_value=Mock(confidence=0.8))

        self.assertTrue(task._hold_until_gold())
        task.mouse_down.assert_called_once()
        task.mouse_up.assert_called_once()

    def test_hold_rejects_gold_e_before_min_hold(self):
        # 上一套二连的残留金 E: 出现在下限之前, 不能接
        task = _make_task(mock_hold=False)
        task.COMBO_HOLD_MIN = 0.2
        task.COMBO_HOLD_MAX = 0.05
        task.find_one = Mock(return_value=Mock(confidence=0.8))

        self.assertFalse(task._hold_until_gold())

    def test_hold_times_out_without_gold_e(self):
        task = _make_task(mock_hold=False)
        task.COMBO_HOLD_MIN = 0.0
        task.COMBO_HOLD_MAX = 0.05
        task.find_one = Mock(return_value=None)

        self.assertFalse(task._hold_until_gold())
        task.mouse_down.assert_called_once()
        task.mouse_up.assert_called_once()

    def test_hold_releases_mouse_even_when_polling_raises(self):
        task = _make_task(mock_hold=False)
        task.COMBO_HOLD_MIN = 0.0
        task.COMBO_HOLD_MAX = 0.05
        task.find_one = Mock(side_effect=RuntimeError("frame gone"))

        with self.assertRaises(RuntimeError):
            task._hold_until_gold()

        task.mouse_up.assert_called_once()


if __name__ == "__main__":
    unittest.main()
