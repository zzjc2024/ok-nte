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
    task._switch_to_index = Mock(return_value=True)
    if mock_hold:
        task._hold_until_gold = Mock(return_value=True)
    return task


class TestZankouComboHotkey(unittest.TestCase):
    """F12 手动辅助: 切残虹 -> 二连 -> 切回原角色, 失败只记日志."""

    def test_combo_switches_to_zankou_then_back(self):
        task = _make_task()
        task._current_index = Mock(return_value=1)

        task._run_combo()

        self.assertEqual(
            [call.args[0] for call in task._switch_to_index.call_args_list], [0, 1]
        )
        task.click.assert_called_once()
        self.assertGreater(task._last_combo_at, 0.0)

    def test_combo_on_zankou_does_not_switch(self):
        task = _make_task()
        task._current_index = Mock(return_value=0)

        task._run_combo()

        task._switch_to_index.assert_not_called()
        task.click.assert_called_once()

    def test_combo_skipped_when_previous_combo_too_recent(self):
        task = _make_task()
        task._last_combo_at = time.time()

        task._run_combo()

        task._switch_to_index.assert_not_called()
        task._hold_until_gold.assert_not_called()
        task.click.assert_not_called()

    def test_combo_aborts_when_current_char_unknown(self):
        task = _make_task()
        task._current_index = Mock(return_value=-1)

        task._run_combo()

        task._switch_to_index.assert_not_called()
        task._hold_until_gold.assert_not_called()

    def test_combo_aborts_when_switch_not_confirmed(self):
        task = _make_task()
        task._switch_to_index = Mock(return_value=False)

        task._run_combo()

        task._hold_until_gold.assert_not_called()
        task.click.assert_not_called()

    def test_combo_does_not_click_when_gold_never_shows(self):
        task = _make_task()
        task._hold_until_gold = Mock(return_value=False)

        task._run_combo()

        task.click.assert_not_called()
        self.assertEqual(task._last_combo_at, 0.0)

    def test_combo_passes_entry_skill_extra_only_when_switching(self):
        task = _make_task()
        task._current_index = Mock(return_value=2)

        task._run_combo()

        task._hold_until_gold.assert_called_once_with(extra=task.ENTRY_SKILL_EXTRA)

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
