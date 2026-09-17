import threading
import time

from ok import Logger, TriggerTask

from src.combat.BaseCombatTask import BaseCombatTask
from src.Labels import Labels

logger = Logger.get_logger(__name__)


class ZankouComboHotkeyTask(BaseCombatTask, TriggerTask):
    """按 F12 打一次残虹二连 (手动辅助, 不跑自动循环).

    序列: 切残虹(需要时) -> 长按左键等金 E -> 松开 -> 单击左键 -> 切回按下 F12 时的角色。

    任何一步失败(认不出当前角色 / 切人没确认 / 长按没出金 E)都只记日志直接结束:
    不做恢复、不抛异常停任务, 玩家再按一次 F12 即可。

    约定: 固定 4 人队(1 号位 = 残虹), 与四人连招任务一致。不要和 `AutoCombatTask`
    或四人连招任务同时开; app 全局 Start/Stop 热键也不要设成 F12。
    """

    ZANKOU_INDEX = 0
    TEAM_SIZE = 4
    HOTKEY = "f12"

    # 长按参数与四人连招任务一致(录屏逐帧校准): 金 E 0.71~0.88 / 白(蓄力) 0.42~0.61。
    COMBO_HOLD_MIN = 0.67
    COMBO_HOLD_MAX = 0.9
    COMBO_POLL_INTERVAL = 0.05
    COMBO_RELEASE_GAP = 0.06
    COMBO_CLICK_GAP = 0.05
    GOLD_THRESHOLD = 0.65
    # 切到残虹时按住左键穿过她的入场技(满环合的相邻角色切过来会触发, 期间按键不生效
    # 但按住状态保留), 控制一恢复蓄力立刻开始; 上限按入场技实测时长(1.1s)延长。
    ENTRY_SKILL_EXTRA = 1.2
    # 两次 F12 至少隔这么久: 二连(松开+单击)后攻击动作还残留约 1.5s, 期间长按会被动画
    # 吃掉, 而且残留的金 E 会被误当成新的金 E 再打一套。
    COMBO_MIN_INTERVAL = 1.5
    SWITCH_SETTLE_TIME = 0.1
    SWITCH_CONFIRM_TIMEOUT = 3.0
    # 切人确认要求目标高亮连续稳定这么久(过滤切换动画里的瞬态高亮)
    SWITCH_CONFIRM_STABLE = 0.15
    SWITCH_KEY_INTERVAL = 0.2
    SWITCH_KEY_DOWN_TIME = 0.05
    SCRIPT_TICK = 0.05

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.default_config.update({"_enabled": False})
        self.trigger_interval = 0.1
        self.name = "残虹二连(F12)"
        self.description = "按 F12: 切残虹 -> 打一次残虹二连 -> 切回原角色"
        self._request = threading.Event()
        self._hotkey_listener = None
        self._hotkey_lock = threading.Lock()
        self._hotkey_warned = False
        self._last_combo_at = 0.0

    # -------------------------------------------------------------- hotkey

    def enable(self):
        super().enable()
        self._ensure_hotkey_listener()

    def disable(self):
        self._stop_hotkey_listener()
        super().disable()

    def on_destroy(self):
        self._stop_hotkey_listener()
        super().on_destroy()

    def _ensure_hotkey_listener(self):
        """注册 F12 监听(幂等). run() 在 executor 线程、enable() 在 UI 线程都会调, 加锁."""
        with self._hotkey_lock:
            if self._hotkey_listener is not None:
                return
            try:
                from pynput import keyboard
            except Exception as e:
                self.log_error("残虹二连: pynput 不可用, F12 无法注册", e)
                return
            self._warn_hotkey_conflict()
            key = getattr(keyboard.Key, self.HOTKEY, None)
            if key is None:
                self.log_error(f"残虹二连: pynput 不认识按键 {self.HOTKEY}")
                return

            def on_release(released):
                try:
                    if released == key:
                        self._request.set()
                except Exception as e:
                    logger.error(f"zankou combo hotkey callback failed: {e}")

            listener = keyboard.Listener(on_release=on_release)
            listener.daemon = True
            listener.start()
            self._hotkey_listener = listener
        self.log_info(f"残虹二连: {self.HOTKEY.upper()} 已就绪 (切残虹 -> 二连 -> 切回)")

    def _warn_hotkey_conflict(self):
        """app 全局 Start/Stop 热键如果也是 F12, 按一下会同时开关任务, 提醒一次."""
        if self._hotkey_warned:
            return
        self._hotkey_warned = True
        try:
            start_stop = (self.executor.basic_options or {}).get("Start/Stop")
        except Exception:
            return
        if str(start_stop).strip().upper() == self.HOTKEY.upper():
            self.log_warning(
                f"残虹二连: 全局 Start/Stop 热键也是 {self.HOTKEY.upper()}, "
                f"会同时开关任务, 请在设置里改成别的键"
            )

    def _stop_hotkey_listener(self):
        with self._hotkey_lock:
            listener = self._hotkey_listener
            self._hotkey_listener = None
        if listener is None:
            return
        try:
            listener.stop()
        except Exception as e:
            logger.error(f"zankou combo hotkey stop failed: {e}")

    # ---------------------------------------------------------------- run

    def run(self):
        self._ensure_hotkey_listener()
        if not self._request.is_set():
            return False
        self._request.clear()
        if not self.scene.is_in_team(self.is_in_team):
            self.log_info("残虹二连: 不在队伍界面, 忽略这次 F12")
            return False
        try:
            self._run_combo()
        except Exception as e:
            self.log_error("残虹二连出错", e)
        return True

    def _run_combo(self):
        now = time.time()
        elapsed = now - self._last_combo_at
        if elapsed < self.COMBO_MIN_INTERVAL:
            self.log_info(
                f"残虹二连: 距上次二连只有 {elapsed:.2f}s, 忽略这次 F12"
            )
            return
        current = self._current_index()
        if current < 0:
            self.log_warning("残虹二连: 认不出当前角色, 结束")
            return
        switched = current != self.ZANKOU_INDEX
        if switched and not self._switch_to_index(self.ZANKOU_INDEX):
            self.log_warning("残虹二连: 切残虹没确认, 结束")
            return
        self.sleep(self.SWITCH_SETTLE_TIME)
        if not self._hold_until_gold(extra=self.ENTRY_SKILL_EXTRA if switched else 0.0):
            return
        self.sleep(self.COMBO_RELEASE_GAP)
        self.click()
        self.sleep(self.COMBO_CLICK_GAP)
        self._last_combo_at = time.time()
        self.log_info("残虹二连: 已打出")
        if switched and not self._switch_to_index(current):
            self.log_warning(f"残虹二连: 切回 {current + 1} 号位没确认")

    # -------------------------------------------------------------- helpers

    def _current_index(self):
        """当前角色号位(0~3); -1 = 这一帧认不出来."""
        detection = self._get_current_char_detection(
            frame=self.frame, char_count=self.TEAM_SIZE
        )
        if not detection.accepted:
            logger.info(f"zankou combo hotkey: current char rejected ({detection.reason})")
            return -1
        return detection.index

    def _switch_to_index(self, index):
        """按数字键切到 index 号位, 用头像高亮确认(不采信框架的 active health change).

        返回 False = 超时没确认(被控 / 切人 CD / 检测失明), 调用方只记日志。
        """
        key = index + 1
        deadline = time.time() + self.SWITCH_CONFIRM_TIMEOUT
        stable_since = 0.0
        detection = None
        with self.skip_sleep_checks() as skip:
            skip.check_combat = True
            while time.time() < deadline:
                detection = self._get_current_char_detection(
                    frame=self.frame, char_count=self.TEAM_SIZE
                )
                if detection.accepted and detection.index == index:
                    if stable_since == 0.0:
                        stable_since = time.time()
                    if time.time() - stable_since >= self.SWITCH_CONFIRM_STABLE:
                        logger.info(f"zankou combo switch confirmed -> slot {key}")
                        return True
                else:
                    stable_since = 0.0
                self.send_key(
                    key,
                    action_name="zankou_hotkey_switch",
                    interval=self.SWITCH_KEY_INTERVAL,
                    down_time=self.SWITCH_KEY_DOWN_TIME,
                )
                self.sleep(self.SCRIPT_TICK)
        logger.warning(
            f"zankou combo switch to slot {key} not confirmed, "
            f"got {detection.index if detection else None} "
            f"(reason={detection.reason if detection else None})"
        )
        return False

    def _hold_until_gold(self, extra=0.0):
        """长按左键轮询金 E; 出现返回 True(调用方负责松开后的点按), 超时返回 False.

        下限 `COMBO_HOLD_MIN` 用来忽略上一套二连的残留金 E; 上限是
        `COMBO_HOLD_MAX` + `extra`(穿过入场技时为入场技时长)。
        """
        start = time.time()
        min_until = start + self.COMBO_HOLD_MIN
        max_until = start + self.COMBO_HOLD_MAX + extra
        best_conf = 0.0
        gold = False
        self.mouse_down()
        try:
            with self.skip_sleep_checks() as skip:
                skip.check_combat = True
                while time.time() < max_until:
                    box = self.find_one(Labels.zankou_skill_gold, threshold=0.0)
                    conf = box.confidence if box else 0.0
                    if conf > best_conf:
                        best_conf = conf
                    if time.time() >= min_until and conf >= self.GOLD_THRESHOLD:
                        gold = True
                        break
                    self.sleep(self.COMBO_POLL_INTERVAL)
        finally:
            self.mouse_up()
        if gold:
            logger.info(f"zankou combo gold E detected, conf={best_conf:.3f}")
            return True
        logger.warning(
            f"zankou combo no gold E in {self.COMBO_HOLD_MAX + extra:.2f}s "
            f"(best conf={best_conf:.3f})"
        )
        return False
