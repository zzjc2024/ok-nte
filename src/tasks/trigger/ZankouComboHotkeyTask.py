import threading
import time

from ok import Logger

from src.combat.BaseCombatTask import cd_regex, convert_cd
from src.sound_trigger.SoundCombatContext import SoundCombatContext
from src.tasks.trigger.FourCharComboTask import FourCharComboTask, HoldResult
from src.utils import game_filters as gf

logger = Logger.get_logger(__name__)


class ZankouComboHotkeyTask(FourCharComboTask):
    """按 F12 打一次残虹二连 (手动辅助, 不跑自动循环).

    继承四人连招任务的全部 helper(长按轮询金 E、伊洛伊 Q/浮游炮、声音接线),
    只覆写 `run()` 和 F12 的流程, 不跑 `_run_rotation`。

    流程:
    1. 按 1 切残虹(不管当前是谁, 按 1 没有负面效果), 等 `SWITCH_SETTLE_TIME`; 顺便复查,
       发现不是残虹就再按一次 1 再等一轮(再不是只记日志, 继续长按)。
    2. 长按左键轮询金 E -> 松开 -> 等 `COMBO_RELEASE_GAP` -> **趁这个等待读环合值** ->
       单击左键完成二连。
    3. 环合 >= `CYCLE_SWITCH_RATIO`(0.97) 走**伊洛伊流程**, 否则切 2 号达芙蒂尔 +
       0.5s 内补 5 次左键。

    伊洛伊流程(满环合, 切过去几乎必然吃她的入场技):
    - 按 3 切伊洛伊 -> **从切过去就开始连按 E**(入场技期间按键不生效, 连按到注册为止,
      这样入场技一结束就能第一时间把 E 放出来)。
    - E 注册后等它的冷却**原始数字**掉到 `E_CD_AFTER_CAST`(11.6s, E 满冷 12s, 即放出去
      约 0.4s) 再判 Q: Q 可用 -> `_iroi_q_funnel()`(Q + 浮游炮); Q 不可用 ->
      **本次 F12 停**(什么都不做)。
    - E 没注册(本来就在 CD)时: Q 可用 -> Q + 浮游炮; E/Q 都不可用 -> 直接切达芙。
    - Q + 浮游炮之后: 切残虹打第二套二连 -> 切达芙 + 5 次左键。

    声音(继承四人连招任务的接线; 动作跑在 `sleep_check` 里, 不会被上面的流程挡住):
    - **非残虹**触发闪避成功音: 照常点一下左键触发闪避反击, 然后停掉本次 F12 流程;
    - **残虹**触发闪避成功音: 点左键 -> 长按等第二次金 E(期间可再被警报/闪避打断) ->
      二连打完 -> 按环合规则切人。

    任何失败都只记日志直接结束(不切达芙救场、不抛异常停任务), 玩家再按一次 F12 即可。
    约定: 固定 4 人队(1 残虹 / 2 达芙蒂尔 / 3 伊洛伊), 与四人连招任务一致。
    不要和 `AutoCombatTask` 或四人连招任务同时开; app 全局 Start/Stop 热键也不要设成 F12。
    """

    HOTKEY = "f12"

    DAFFODILL_INDEX = 1
    IROI_INDEX = 2
    SWITCH_KEY_DOWN_TIME = 0.05
    # 按完切人键到开始长按的等待(用户实测: 0.05s)
    SWITCH_SETTLE_TIME = 0.05
    # 二连后按环合值决定切谁: >= 97% 切伊洛伊(满环合, 触发连携登场技), 否则切达芙蒂尔
    CYCLE_SWITCH_RATIO = 0.97
    # 两次 F12 至少隔这么久: 二连后攻击动作还残留约 1.5s, 期间长按会被动画吃掉,
    # 而且残留的金 E 会被误当成新的金 E 再打一套。
    F12_MIN_INTERVAL = 1.5
    # 长按被打断(警报/闪避)时最多重打几次
    HOLD_RETRIES = 3
    # 切到达芙蒂尔后自动补的普攻: 5 次, 在 0.5s 内点完(伊洛伊不补)
    DAFFODILL_PAD_CLICKS = 5
    DAFFODILL_PAD_WINDOW = 0.5
    # 连按 E 到注册的总窗口: 要盖住伊洛伊的入场技(期间按键不生效、图标也会失明)
    IROI_E_WAIT_TIMEOUT = 3.0
    IROI_E_PRESS_INTERVAL = 0.12
    # E 满冷 12s: 冷却原始数字掉到 11.6 表示 E 真的放出去了(约 0.4s), 这时才接 Q
    E_CD_AFTER_CAST = 11.6
    # 等 E 冷却数字的兜底上限(读不到数字时不能一直等)
    E_CD_WAIT_TIMEOUT = 0.6

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = "残虹二连(F12)"
        self.description = (
            "按 F12: 切残虹 -> 二连 -> 环合满切伊洛伊(放E/Q+浮游炮)否则切达芙蒂尔"
        )
        self._request = threading.Event()
        self._hotkey_listener = None
        self._hotkey_lock = threading.Lock()
        self._hotkey_warned = False
        self._last_f12_at = 0.0
        # 声音路径要求"停掉本次 F12 流程"时置位; 每轮 F12 开头清掉
        self._abort_f12 = False

    # -------------------------------------------------------------- hotkey

    def enable(self):
        super().enable()
        self._bind_sound()
        self._ensure_hotkey_listener()

    def disable(self):
        self._stop_hotkey_listener()
        SoundCombatContext().clear_task_if(self)
        super().disable()

    def on_destroy(self):
        self._stop_hotkey_listener()
        super().on_destroy()

    def _bind_sound(self):
        """把声音回调绑到本任务(动作跑在 sleep_check 里, 不占任务节流)."""
        self._apply_sound_config(
            dodge_action=self._sound_dodge_action,
            counter_action=self._sound_counter_action,
            dodge_success_action=self._sound_dodge_success_action,
        )

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
        if not self.scene.is_in_team(self.is_in_team):
            self.log_info("残虹二连: 不在队伍界面, 忽略这次 F12")
            return False
        if len(self.chars) < 4 and not self.load_chars():
            self.log_warning("残虹二连: 读不到队伍, 忽略这次 F12")
            return False
        try:
            self._run_combo()
        except Exception as e:
            self.log_error("残虹二连出错", e)
        return True

    def _run_combo(self):
        elapsed = time.time() - self._last_f12_at
        if elapsed < self.F12_MIN_INTERVAL:
            self.log_info(f"残虹二连: 距上次只有 {elapsed:.2f}s, 忽略这次 F12")
            return
        self._abort_f12 = False
        self._set_action_phase("f12_combo")
        self._switch_to_zankou()
        if self._abort_f12:
            return
        ratio = self._zankou_combo_once(extra=self.ZANKOU_ENTRY_SKILL_WAIT)
        if ratio is None:
            return
        self._last_f12_at = time.time()
        self.log_info(f"残虹二连: 已打出 (环合 {ratio:.2f})")
        self._route_after_combo(ratio)

    def _route_after_combo(self, ratio):
        """二连之后按环合值分流: 满环合走伊洛伊流程, 否则切达芙蒂尔 + 5 次普攻."""
        if ratio >= self.CYCLE_SWITCH_RATIO:
            self._iroi_flow()
        else:
            self._switch_after_combo(self.DAFFODILL_INDEX)

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

    def _zankou_combo_once(self, extra=0.0):
        """打一套残虹二连; 返回打完后读到的环合值, None = 没打出来.

        长按期间被攻击警报/闪避打断会重打(和四人脚本同源), 但**不做达芙恢复、
        不抛异常**: 试满 `HOLD_RETRIES` 轮还是没金 E 就记日志结束。
        """
        second_gold = False
        for _ in range(self.HOLD_RETRIES):
            self._alert_interrupt.clear()
            result, _damaged = self._hold_until_gold(
                interrupt_event=self._alert_interrupt,
                require_second_gold=second_gold,
                max_hold_extra=extra,
            )
            if result is HoldResult.GOLD:
                self.sleep(self.COMBO_RELEASE_GAP)
                # 趁"松开 -> 单击"的等待读环合值(用户要求), 决定二连之后切谁
                ratio = self.cycle_ratio()
                self.click()
                self.sleep(self.COMBO_CLICK_GAP)
                self._last_combo_finished_at = time.time()
                return ratio
            if result is HoldResult.DODGE:
                # 长按被闪避打断: 点左键触发闪避反击 -> 等反击动画 -> 重打
                # (完美闪避之后等的是第二次金 E)
                second_gold = self._last_dodge_was_perfect
                self.click(down_time=self.SOUND_SUCCESS_CLICK_DOWN)
                self.sleep(self.DODGE_COUNTER_WAIT)
                continue
            if result is HoldResult.INTERRUPTED:
                self.log_info("残虹二连: 长按被攻击警报打断, 重打")
                continue
            break
        self.log_warning("残虹二连: 长按没出金 E, 结束")
        return None

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

    # ---------------------------------------------------------- iroi flow

    def _iroi_flow(self):
        """满环合切伊洛伊: E -> Q + 浮游炮 -> 残虹二连 -> 达芙(+5次普攻)."""
        self._set_action_phase("f12_iroi")
        self._press_switch_key(self.IROI_INDEX)
        e_registered = self._press_e_until_registered()
        if self._abort_f12:
            return
        if e_registered:
            self._wait_e_cd_after_cast()
            if self._abort_f12:
                return
        # 之后的 Q 判定 / 浮游炮都按"当前角色 = 伊洛伊"读冷却
        self._sync_current_char()
        if not self._ultimate_available():
            if e_registered:
                self.log_info("残虹二连: 伊洛伊 Q 还没好, 本次 F12 停")
            else:
                self.log_info("残虹二连: 伊洛伊 E/Q 都不可用, 直接切达芙")
                self._switch_after_combo(self.DAFFODILL_INDEX)
            return
        self._iroi_q_funnel()
        if self._abort_f12:
            return
        self._zankou_second_combo()

    def _zankou_second_combo(self):
        """浮游炮之后: 切残虹打第二套二连, 再切达芙蒂尔 + 5 次普攻."""
        self._switch_to_zankou()
        if self._abort_f12:
            return
        ratio = self._zankou_combo_once(extra=self.ZANKOU_ENTRY_SKILL_WAIT)
        if ratio is None:
            return
        self._last_f12_at = time.time()
        self.log_info(f"残虹二连: 第二套已打出 (环合 {ratio:.2f})")
        self._switch_after_combo(self.DAFFODILL_INDEX)

    def _press_e_until_registered(self):
        """从切过去就开始连按 E, 直到它进 CD(注册); False = E 一直没放出来.

        入场技期间按键不生效、图标模板也会失明(实测能持续 3s), 所以**不预判可用性**:
        连按到"看到图标亮过之后 CD 数字出现"算注册; 一直只看到 CD 数字(或什么都没有)
        就是 E 本来就在 CD / 不可控。返回 True/False 决定走哪条分支。
        """
        seen_available = False
        deadline = time.time() + self.IROI_E_WAIT_TIMEOUT
        with self.skip_sleep_checks() as skip:
            skip.check_combat = True
            while time.time() < deadline:
                if self._raw_skill_cd() > 0.0:
                    registered = seen_available
                    logger.info(f"f12 iroi: E cd appeared, registered={registered}")
                    return registered
                if self.box_highlighted("skill"):
                    seen_available = True
                self.send_key(
                    self.get_skill_key(),
                    action_name="f12_iroi_e",
                    interval=0.1,
                    down_time=0.05,
                )
                self.sleep(self.IROI_E_PRESS_INTERVAL)
        logger.warning(f"f12 iroi: E not registered (seen_available={seen_available})")
        return False

    def _wait_e_cd_after_cast(self):
        """等 E 冷却原始数字 <= `E_CD_AFTER_CAST`, 然后才接 Q.

        满冷 12s, 掉到 11.6 说明 E 真的放出去了(约 0.4s), 这时按 Q 才不会被 E 的前摇
        吃掉。必须用**原始 OCR 数字**(`get_cd()` 会随时间漂移); 读不到数字时按
        `E_CD_WAIT_TIMEOUT` 兜底, 不能一直等。
        """
        deadline = time.time() + self.E_CD_WAIT_TIMEOUT
        while time.time() < deadline:
            cd = self._raw_skill_cd()
            if 0.0 < cd <= self.E_CD_AFTER_CAST:
                logger.info(f"f12 iroi: E cd {cd:.1f}s, cast Q")
                return
            self.sleep(0.05)
        logger.warning(f"f12 iroi: E cd not read, cast Q anyway (last={self._raw_skill_cd():.1f})")

    # ------------------------------------------------------- cooldown reads

    def _cd_texts(self):
        return (
            self.ocr(
                *self.CD_OCR_BOX,
                frame_processor=gf.isolate_text_to_black,
                match=cd_regex,
            )
            or []
        )

    def _raw_skill_cd(self):
        """E 冷却原始数字; 按 OCR 位置取, **不依赖当前角色标记**.

        `refresh_cd()` 把数字存在 `get_current_char().index` 下, 而切人/入场技期间
        那个标记会失明(实测 3s), 会读到上一个人的冷却, 所以这里自己读。
        """
        for text in self._cd_texts():
            if text.x < self.width_of_screen(0.89):
                return convert_cd(text)
        return 0.0

    def _raw_ultimate_cd(self):
        """Q 冷却原始数字; 同上, 不依赖当前角色标记."""
        for text in self._cd_texts():
            if text.x > self.width_of_screen(0.925):
                return convert_cd(text)
        return 0.0

    def _ultimate_available(self):
        """Q 可用 = 图标亮 且 读不到冷却数字(不依赖当前角色标记)."""
        return bool(self._q_button_lit()) and self._raw_ultimate_cd() <= 0.0

    # ------------------------------------------------------------- helpers

    def _press_switch_key(self, index):
        self.send_key(index + 1, down_time=self.SWITCH_KEY_DOWN_TIME)

    def _is_current(self, index):
        """图像检测当前角色是不是 index 号位(不用 sticky tracker, 每次都是新帧)."""
        detection = self._get_current_char_detection(
            frame=self.frame, char_count=self.team_size
        )
        if detection.accepted:
            self._set_current_flags(detection.index)
        else:
            logger.info(f"f12: current char rejected ({detection.reason})")
        return bool(detection.accepted and detection.index == index)

    def _sync_current_char(self):
        """把检测到的当前角色同步给 `chars[].is_current_char`.

        浮游炮里的 `has_cd("ultimate")` / `_wait_ultimate_unfreeze` 都按
        `get_current_char().index` 取冷却, 不更新会读到上一个角色的冷却。
        """
        detection = self._get_current_char_detection(
            frame=self.frame, char_count=self.team_size
        )
        if detection.accepted:
            self._set_current_flags(detection.index)
        else:
            logger.info(f"f12: current char rejected ({detection.reason})")

    def _set_current_flags(self, index):
        for char in self.chars:
            char.is_current_char = char.index == index

    # --------------------------------------------------------------- sound

    def _sound_dodge_success_action(self):
        """听到闪避成功音 -> 闪避反击 (用户规格 2026-09-17).

        非残虹: 照常点一下左键触发闪避反击, 然后停掉本次 F12 流程(原地不动)。
        残虹: 点左键 -> 长按等第二次金 E(期间可再被攻击警报打断) -> 二连打完 ->
              按环合规则切人。
        """
        self._dodge_success_heard.set()
        self._dodge_heard_at = time.time()
        if self._holding:
            # 长按正在进行: 交给长按自己收尾(打断 -> 点左键 -> 等反击动画 -> 重打),
            # 这里再点一次左键会和它抢鼠标。
            logger.info("dodge success during hold, leave the reaction to the hold")
            return
        is_zankou = self._is_current(self.ZANKOU_INDEX)
        with self.skip_sleep_checks() as skip:
            skip.all = True
            if not is_zankou:
                self.log_info("残虹二连: 非残虹触发闪避反击, 停掉本次 F12")
                self.click(down_time=self.SOUND_SUCCESS_CLICK_DOWN)
                self._abort_f12 = True
                return
            self._set_action_phase("f12_sound_success")
            while True:
                self._alert_interrupt.clear()
                self.click(down_time=self.SOUND_SUCCESS_CLICK_DOWN)
                time.sleep(self.DODGE_COUNTER_WAIT)
                if not self._zankou_combo_interruptible(require_second_gold=True):
                    break
                logger.info("f12 sound success combo interrupted, dodge then retry")
                self._sound_dodge_action()
            ratio = self.cycle_ratio()
            self.log_info(f"残虹二连: 闪避反击二连打完 (环合 {ratio:.2f})")
            self._route_after_combo(ratio)
            self._abort_f12 = True
