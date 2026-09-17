import threading
import time

from ok import Logger, TriggerTask

from src.combat.BaseCombatTask import BaseCombatTask
from src.Labels import Labels

logger = Logger.get_logger(__name__)


class ZankouComboHotkeyTask(BaseCombatTask, TriggerTask):
    """按 F12 打一次残虹二连 (手动辅助, 不跑自动循环).

    序列:
    1. 按 1 切残虹(不管当前是谁, 按 1 没有负面效果), 等 0.1s;
       顺便查一下当前角色是不是残虹, 不是就再按一次 1 再等 0.1s。
    2. 长按左键轮询金 E -> 松开 -> 等 `COMBO_RELEASE_GAP`。
    3. 趁这个等待读环合值: >= `CYCLE_SWITCH_RATIO`(95%) 切 3 号伊洛伊,
       否则切 2 号达芙蒂尔。
    4. 单击左键完成二连; 切到达芙蒂尔时 0.5s 内再补 5 次左键(伊洛伊不补)。

    认不出当前角色 / 长按没出金 E 都只记日志直接结束: 不做恢复、不抛异常停任务,
    玩家再按一次 F12 即可。

    约定: 固定 4 人队(1 残虹 / 2 达芙蒂尔 / 3 伊洛伊), 与四人连招任务一致。
    不要和 `AutoCombatTask` 或四人连招任务同时开; app 全局 Start/Stop 热键也不要设成 F12。
    """

    ZANKOU_INDEX = 0
    DAFFODILL_INDEX = 1
    IROI_INDEX = 2
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
    # 只延长上限, 不影响"按完 1 号位 + 0.1s 就开始长按"。
    ENTRY_SKILL_EXTRA = 1.2
    # 两次 F12 至少隔这么久: 二连(松开+单击)后攻击动作还残留约 1.5s, 期间长按会被动画
    # 吃掉, 而且残留的金 E 会被误当成新的金 E 再打一套。
    COMBO_MIN_INTERVAL = 1.5
    # 按完切人键到开始长按的等待(用户实测: 0.1s)
    SWITCH_SETTLE_TIME = 0.1
    SWITCH_KEY_DOWN_TIME = 0.05
    # 二连后按环合值决定切谁: >= 95% 切伊洛伊(满环合, 触发连携登场技), 否则切达芙蒂尔
    CYCLE_SWITCH_RATIO = 0.95
    # 切到达芙蒂尔后自动补的普攻: 5 次, 在 0.5s 内点完(伊洛伊不补)
    DAFFODILL_PAD_CLICKS = 5
    DAFFODILL_PAD_WINDOW = 0.5

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.default_config.update({"_enabled": False})
        self.trigger_interval = 0.1
        self.name = "残虹二连(F12)"
        self.description = "按 F12: 切残虹 -> 打一次残虹二连 -> 按环合值切达芙蒂尔/伊洛伊"
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
        self.log_info(f"残虹二连: {self.HOTKEY.upper()} 已就绪")

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
        try:
            self._run_combo()
        except Exception as e:
            self.log_error("残虹二连出错", e)
        return True

    def _run_combo(self):
        elapsed = time.time() - self._last_combo_at
        if elapsed < self.COMBO_MIN_INTERVAL:
            self.log_info(f"残虹二连: 距上次二连只有 {elapsed:.2f}s, 忽略这次 F12")
            return
        self._switch_to_zankou()
        if not self._hold_until_gold(extra=self.ENTRY_SKILL_EXTRA):
            return
        self.sleep(self.COMBO_RELEASE_GAP)
        # 趁"松开 -> 单击"这个等待读环合值, 决定二连之后切谁
        ratio = self.cycle_ratio()
        self.click()
        self.sleep(self.COMBO_CLICK_GAP)
        self._last_combo_at = time.time()
        self.log_info(f"残虹二连: 已打出 (环合 {ratio:.2f})")
        target = self.IROI_INDEX if ratio >= self.CYCLE_SWITCH_RATIO else self.DAFFODILL_INDEX
        self._switch_after_combo(target)

    def _switch_to_zankou(self):
        """按 1 切残虹; 不等图像确认, 只等 SWITCH_SETTLE_TIME 就长按.

        顺便复查一次(用户要求): 等完发现居然不是残虹, 就再按一次 1 再等一轮;
        再不是也只是记日志继续长按(金 E 本身就是"残虹在场"的可靠信号)。
        """
        self._press_switch_key(self.ZANKOU_INDEX)
        self.sleep(self.SWITCH_SETTLE_TIME)
        if self._is_current(self.ZANKOU_INDEX):
            return
        self.log_info("残虹二连: 按 1 之后不是残虹, 再按一次")
        self._press_switch_key(self.ZANKOU_INDEX)
        self.sleep(self.SWITCH_SETTLE_TIME)
        if not self._is_current(self.ZANKOU_INDEX):
            self.log_warning("残虹二连: 再按一次 1 仍没确认是残虹, 继续长按")

    def _switch_after_combo(self, target):
        """二连后切人: 伊洛伊直接结束; 达芙蒂尔切过去后在 0.5s 内补 5 次普攻."""
        self._press_switch_key(target)
        self.log_info(f"残虹二连: 切 {target + 1} 号位")
        if target != self.DAFFODILL_INDEX:
            return
        gap = self.DAFFODILL_PAD_WINDOW / self.DAFFODILL_PAD_CLICKS
        for _ in range(self.DAFFODILL_PAD_CLICKS):
            self.sleep(gap)
            self.click()

    def _press_switch_key(self, index):
        self.send_key(index + 1, down_time=self.SWITCH_KEY_DOWN_TIME)

    # -------------------------------------------------------------- helpers

    def _is_current(self, index):
        """图像检测当前角色是不是 index 号位(不用 sticky tracker, 每次都是新帧)."""
        detection = self._get_current_char_detection(
            frame=self.frame, char_count=self.TEAM_SIZE
        )
        if not detection.accepted:
            logger.info(f"zankou combo hotkey: current char rejected ({detection.reason})")
        return bool(detection.accepted and detection.index == index)

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
