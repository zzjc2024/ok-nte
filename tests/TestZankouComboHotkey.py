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
    task.cycle_ratio = Mock(return_value=0.5)
    task._is_current = Mock(return_value=True)
    if mock_hold:
        task._hold_until_gold = Mock(return_value=True)
    return task


def _switch_keys(task):
    return [call.args[0] for call in task.send_key.call_args_list]


class TestZankouComboHotkey(unittest.TestCase):
    """F12 手动辅助: 按1切残虹 -> 二连 -> 按环合值切达芙/伊洛伊."""

    def test_combo_always_presses_1_first(self):
        # 不管当前是谁都先按 1(在残虹身上按 1 没有负面效果)
        task = _make_task()

        task._run_combo()

        self.assertEqual(_switch_keys(task)[0], 1)

    def test_combo_presses_1_again_when_not_zankou(self):
        # 等完 0.1s 发现不是残虹 -> 再按一次 1 再等一轮
        task = _make_task()
        task._is_current = Mock(side_effect=[False, True])

        task._run_combo()

        # 两次切残虹 + 最后按环合值切 2 号达芙
        self.assertEqual(_switch_keys(task), [1, 1, 2])
        settle = [c.args[0] for c in task.sleep.call_args_list[:2]]
        self.assertEqual(settle, [task.SWITCH_SETTLE_TIME] * 2)

    def test_combo_does_not_press_1_again_when_already_zankou(self):
        task = _make_task()
        task._is_current = Mock(return_value=True)

        task._run_combo()

        self.assertEqual(_switch_keys(task)[0], 1)
        self.assertEqual(len([key for key in _switch_keys(task) if key == 1]), 1)

    def test_combo_still_holds_when_zankou_never_confirmed(self):
        task = _make_task()
        task._is_current = Mock(return_value=False)

        task._run_combo()

        task._hold_until_gold.assert_called_once()
        self.assertEqual(_switch_keys(task), [1, 1, 2])

    def test_combo_switches_to_daffodill_when_cycle_low(self):
        task = _make_task()
        task.cycle_ratio = Mock(return_value=0.5)

        task._run_combo()

        self.assertEqual(_switch_keys(task), [1, 2])

    def test_combo_switches_to_iroi_when_cycle_full(self):
        task = _make_task()
        task.cycle_ratio = Mock(return_value=0.95)

        task._run_combo()

        self.assertEqual(_switch_keys(task), [1, 3])

    def test_daffodill_gets_five_pad_clicks(self):
        task = _make_task()
        task.cycle_ratio = Mock(return_value=0.5)

        task._run_combo()

        # 二连的单击 + 达芙的 5 次普攻
        self.assertEqual(task.click.call_count, 1 + task.DAFFODILL_PAD_CLICKS)

    def test_iroi_gets_no_pad_clicks(self):
        task = _make_task()
        task.cycle_ratio = Mock(return_value=0.99)

        task._run_combo()

        task.click.assert_called_once()

    def test_cycle_is_read_between_release_and_click(self):
        task = _make_task()
        order = []
        task.cycle_ratio = Mock(side_effect=lambda: order.append("ratio") or 0.5)
        task.click = Mock(side_effect=lambda *a, **k: order.append("click"))

        task._run_combo()

        self.assertEqual(order[0], "ratio")
        self.assertEqual(order[1], "click")

    def test_combo_skipped_when_previous_combo_too_recent(self):
        task = _make_task()
        task._last_combo_at = time.time()

        task._run_combo()

        task.send_key.assert_not_called()
        task._hold_until_gold.assert_not_called()
        task.click.assert_not_called()

    def test_combo_does_not_click_when_gold_never_shows(self):
        task = _make_task()
        task._hold_until_gold = Mock(return_value=False)

        task._run_combo()

        task.click.assert_not_called()
        task.send_key.assert_called_once()
        self.assertEqual(task._last_combo_at, 0.0)

    def test_run_runs_combo_when_requested(self):
        task = _make_task()
        task._ensure_hotkey_listener = Mock()
        task._request.is_set = Mock(return_value=True)
        task._run_combo = Mock()

        task.run()

        task._run_combo.assert_called_once()

    def test_run_does_nothing_without_request(self):
        task = _make_task()
        task._ensure_hotkey_listener = Mock()
        task._request.is_set = Mock(return_value=False)
        task._run_combo = Mock()

        task.run()

        task._run_combo.assert_not_called()

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
