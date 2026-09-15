import time

from ok import Logger, TriggerTask

from src.char.Daffodill import Daffodill
from src.char.Iroi import Iroi
from src.char.Sakiri import Sakiri
from src.char.Zankou import Zankou
from src.combat.BaseCombatTask import BaseCombatTask, NotInCombatException
from src.Labels import Labels

logger = Logger.get_logger(__name__)


class FourCharComboTask(BaseCombatTask, TriggerTask):
    """固定编队连招脚本.

    位置约定: 1=残虹 2=达芙蒂尔 3=伊洛伊 4=早雾.
    完全绕开 planner, 由本任务驱动固定连招循环.
    只应在固定编队下手动启用, 不要和 AutoCombatTask 同时开.
    """

    ZANKOU_INDEX = 0
    DAFFODILL_INDEX = 1
    IROI_INDEX = 2
    SAKIRI_INDEX = 3

    COMBO_HOLD_MIN = 0.7
    COMBO_HOLD_MAX = 2.0
    COMBO_POLL_INTERVAL = 0.05
    COMBO_CLICK_GAP = 0.05
    GOLD_THRESHOLD = 0.7
    DAFFODILL_FIELD_TIME = 2.0
    IROI_FUNNEL_POST_SLEEP = 0.3
    Q_READY_TIMEOUT = 5.0
    Q_REGISTER_TIMEOUT = 3.0
    Q_DOUBLE_TIMEOUT = 8.0
    Q_PRESS_INTERVAL = 0.12
    ENTRY_SKILL_WAIT = 1.6
    CONTROLLABLE_TIMEOUT = 10.0
    ZANKOU_Q_READY_WINDOW = 2.0
    SOUND_REACTION_DAFFODILL_TIME = 1.0
    SOUND_IMMEDIATE_SPAM_TIME = 1.2
    SCRIPT_TICK = 0.05

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.default_config.update({"_enabled": False})
        self.trigger_interval = 0.1
        self.name = "四人连招"
        self.description = "固定编队(残虹/达芙蒂尔/伊洛伊/早雾)的固定连招脚本"
        self._last_run_state = None
        self._entry_skill_until = 0.0
        self._opener_gold_e_done = False
        self._sound_counter_pending = False
        self._sound_counter_by_zankou = False
        self._in_sound_reaction = False

    # ---------------------------------------------------------------- chars

    @property
    def zankou(self):
        return self.chars[self.ZANKOU_INDEX]

    @property
    def daffodill(self):
        return self.chars[self.DAFFODILL_INDEX]

    @property
    def iroi(self):
        return self.chars[self.IROI_INDEX]

    @property
    def sakiri(self):
        return self.chars[self.SAKIRI_INDEX]

    # ------------------------------------------------------------ lifecycle

    def load_chars(self) -> bool:
        in_team, current_index, count = self.in_team()
        if not in_team or current_index == -1:
            self._log_run_state(
                f"load_failed in_team={in_team} current={current_index} count={count}"
            )
            return False
        classes = [Zankou, Daffodill, Iroi, Sakiri]
        chars = [cls(self, index, confidence=1) for index, cls in enumerate(classes)]
        for char in chars:
            char.is_current_char = char.index == current_index
        self.chars = chars
        self._apply_sound_config(
            dodge_action=self._sound_dodge_action,
            counter_action=self._sound_counter_action,
        )
        logger.info(f"four char combo loaded, current index {current_index}")
        return True

    def run(self):
        if not self.scene.is_in_team(self.is_in_team):
            self._log_run_state("not_in_team")
            return
        if not self.in_combat():
            self._log_run_state("not_in_combat")
            self._precombat_gold_e()
            return
        if len(self.chars) < 4:
            self._log_run_state(f"chars_{len(self.chars)}")
            return
        self._log_run_state("running")
        try:
            self.combat_session.use_ultimate = True
            self._run_rotation()
        except NotInCombatException as e:
            logger.info(f"four char combo out of combat {e}")
        finally:
            self.combat_end()

    def combat_end(self):
        super().combat_end()
        self._opener_gold_e_done = False

    def _precombat_gold_e(self):
        """入战检测之前, 残虹在场且检测到金 E 时, 直接按 E 并切达芙蒂尔."""
        if self._opener_gold_e_done:
            return
        if len(self.chars) < 4 and not self.load_chars():
            return
        zankou = self.zankou
        if self.get_current_char(raise_exception=False) is not zankou:
            return
        if not self.find_one(Labels.zankou_skill_gold):
            return
        logger.info("precombat gold E detected, press E and switch daffodill")
        self._opener_gold_e_done = True
        zankou.click_skill()
        self._switch_to(self.daffodill)

    def _log_run_state(self, state):
        if state != self._last_run_state:
            self._last_run_state = state
            logger.info(f"four char combo state: {state}")

    def _run_rotation(self):
        logger.info(f"four char combo rotation start, chars={[c.ufn_name for c in self.chars]}")
        self._ensure_current(self.zankou)
        self._opener()
        while self.in_combat():
            self._loop_once()

    # --------------------------------------------------------------- opener

    def _opener(self):
        logger.info("four char combo opener start")
        if not self._opener_gold_e_done:
            self._ensure_current(self.zankou)
            self._zankou_gold_e()

        self._switch_to(self.daffodill)
        self._cast_q(self.daffodill)

        self._switch_to(self.iroi)
        self.iroi.click_skill()

        self._switch_to(self.sakiri)
        self._cast_q(self.sakiri)
        self.sakiri.click_skill()

        self._switch_to(self.zankou)
        self._zankou_double_q()
        self._zankou_combo()
        self._switch_to(self.iroi)

        self._iroi_q_funnel()

        target = self._zankou_combo_switch(self.daffodill)
        if target is self.daffodill:
            self._daffodill_until_cycle_full()

    # ------------------------------------------------------------ main loop

    def _loop_once(self):
        self._maybe_handle_sound_counter()

        iroi = self.iroi
        self._switch_to(iroi)
        if not iroi.ultimate_available() and not self._pad_until_q(iroi):
            return
        self._iroi_q_funnel()

        sakiri = self.sakiri
        self._switch_to(sakiri)
        if not sakiri.ultimate_available() and not self._pad_until_q(sakiri):
            return
        self._cast_q(sakiri)
        sakiri.click_skill()

        self._switch_to(self.zankou)
        target = self._zankou_fixed_step()
        if target is self.iroi:
            return
        self._daffodill_until_cycle_full()

    def _zankou_fixed_step(self):
        zankou = self.zankou
        if zankou.ultimate_available():
            self._zankou_double_q()
        elif 0 < self._zankou_q_remaining() < self.ZANKOU_Q_READY_WINDOW:
            self._stay_until_q_ready(zankou)
            self._zankou_double_q()
        return self._zankou_combo_switch(self.daffodill)

    def _daffodill_until_cycle_full(self):
        daffodill = self.daffodill
        while self.in_combat():
            self._switch_to(daffodill)
            self._daffodill_window(daffodill)
            if self._zankou_combo_switch(daffodill) is self.iroi:
                return

    def _daffodill_window(self, daffodill):
        start = time.time()
        if daffodill.skill_available():
            daffodill.click_skill()
        while self.in_combat():
            self._maybe_handle_sound_counter()
            if daffodill.ultimate_available():
                self._cast_q(daffodill)
                return
            if time.time() - start >= self.DAFFODILL_FIELD_TIME:
                return
            if daffodill.skill_available():
                daffodill.click_skill()
            self.click()
            self.sleep(0.1)

    # ------------------------------------------------------------ pad loops

    def _pad_until_q(self, target):
        """点一次左键 -> 切残虹二连 -> 切回, 直到 target Q 可放."""
        while self.in_combat() and not target.ultimate_available():
            self._maybe_handle_sound_counter()
            self.click()
            if self._zankou_combo_switch(target) is not target:
                return False
        return True

    # ------------------------------------------------------------- zankou

    def _zankou_gold_e(self):
        self._hold_until_gold()
        self.zankou.click_skill()

    def _zankou_combo(self):
        """残虹二连: 长按轮询金E -> 松开 -> 单击左键 -> 等 0.05s."""
        self._hold_until_gold()
        self.click()
        self.sleep(self.COMBO_CLICK_GAP)

    def _zankou_combo_switch(self, default_target):
        self._switch_to(self.zankou)
        self._zankou_combo()
        if self.is_cycle_full():
            self._switch_to(self.iroi)
            return self.iroi
        self._switch_to(default_target)
        return default_target

    def _hold_until_gold(self):
        wait = self._entry_skill_until - time.time()
        if wait > 0:
            logger.info(f"zankou wait entry skill {wait:.2f}s before combo")
            self.sleep(wait)
        self._entry_skill_until = 0.0
        self.mouse_down()
        gold = False
        best_conf = 0.0
        try:
            with self.skip_sleep_checks() as skip:
                skip.check_combat = True
                start = time.time()
                min_until = start + self.COMBO_HOLD_MIN
                max_until = start + self.COMBO_HOLD_MAX
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
            logger.info(f"zankou gold E detected, conf={best_conf:.3f}")
        else:
            logger.warning(f"zankou gold E not detected, best conf={best_conf:.3f}")

    def _zankou_double_q(self):
        zankou = self.zankou
        if not self._press_q_ready(zankou):
            return
        self._press_q_until_registered(zankou, self._q_button_lit(), self.Q_REGISTER_TIMEOUT)
        if not self.wait_until(
            zankou.ultimate_available, time_out=self.Q_DOUBLE_TIMEOUT, raise_if_not_found=False
        ):
            logger.warning("zankou second Q not ready after first")
            return
        second_lit = self._q_button_lit()
        self._press_q_until_registered(zankou, second_lit, self.Q_REGISTER_TIMEOUT)
        self._wait_controllable(zankou, second_lit)

    def _zankou_q_remaining(self):
        if self.has_cd("ultimate"):
            return self.get_cd("ultimate")
        return 0.0

    def _stay_until_q_ready(self, zankou):
        while self.in_combat() and not zankou.ultimate_available():
            self._maybe_handle_sound_counter()
            self.click()
            self.sleep(0.1)

    # ------------------------------------------------------------------ q

    def _cast_q(self, char):
        if not self._press_q_ready(char):
            return False
        was_lit = self._q_button_lit()
        self._press_q_until_registered(char, was_lit, self.Q_REGISTER_TIMEOUT)
        self._wait_controllable(char, was_lit)
        return True

    def _press_q_ready(self, char):
        if self.wait_until(
            char.ultimate_available, time_out=self.Q_READY_TIMEOUT, raise_if_not_found=False
        ):
            return True
        logger.warning(f"{char} ultimate not ready, skip cast")
        return False

    def _press_q_until_registered(self, char, was_lit, timeout, stop_on_cd=True):
        """连按 Q 直到注册(CD出现或按钮变灭), 覆盖入场技/切人动画吃掉按键的情况."""
        deadline = time.time() + timeout
        with self.skip_sleep_checks() as skip:
            skip.check_combat = True
            while time.time() < deadline:
                char.send_ultimate_key(action_name="four_combo_q", interval=0.1, down_time=0.05)
                self.sleep(self.Q_PRESS_INTERVAL)
                if self._q_registered(was_lit, stop_on_cd):
                    logger.info(f"{char} Q registered")
                    return True
        logger.warning(f"{char} Q not registered within {timeout}s")
        return False

    def _q_registered(self, was_lit, stop_on_cd=True):
        if stop_on_cd and self.has_cd("ultimate"):
            return True
        if was_lit and not self._q_button_lit():
            return True
        return False

    def _iroi_q_funnel(self):
        iroi = self.iroi
        if not self._press_q_ready(iroi):
            return
        was_lit = self._q_button_lit()
        self._press_q_until_registered(iroi, was_lit, self.Q_REGISTER_TIMEOUT)
        self.mouse_down()
        try:
            self._wait_cd_ticking()
        finally:
            self.mouse_up()
        self.sleep(self.IROI_FUNNEL_POST_SLEEP)
        self.click()

    def _wait_controllable(self, char, was_lit):
        """可控信号: 冷却数字开始跳 或 Q 按钮由亮变灭."""
        start = time.time()
        previous = None
        with self.skip_sleep_checks() as skip:
            skip.check_combat = True
            while time.time() - start < self.CONTROLLABLE_TIMEOUT:
                if was_lit and not self._q_button_lit():
                    return True
                remaining = self.get_cd("ultimate")
                if remaining > 0 and previous is not None and remaining < previous - 0.001:
                    return True
                if remaining > 0:
                    previous = remaining
                self.sleep(self.SCRIPT_TICK)
        logger.warning(f"wait controllable timeout {char}")
        return False

    def _wait_cd_ticking(self):
        start = time.time()
        previous = None
        with self.skip_sleep_checks() as skip:
            skip.check_combat = True
            while time.time() - start < self.CONTROLLABLE_TIMEOUT:
                remaining = self.get_cd("ultimate")
                if remaining > 0 and previous is not None and remaining < previous - 0.001:
                    return True
                if remaining > 0:
                    previous = remaining
                self.sleep(self.SCRIPT_TICK)
        logger.warning("wait cd ticking timeout")
        return False

    def _q_button_lit(self):
        return bool(self.box_highlighted("ultimate"))

    # --------------------------------------------------------- sound react

    def _sound_dodge_action(self):
        try:
            self.send_key_down("d")
            time.sleep(0.02)
            self.send_key("lshift")
            time.sleep(0.02)
        finally:
            self.send_key_up("d")
            time.sleep(0.02)
        self.send_key("lshift")
        time.sleep(0.02)
        self.click()
        time.sleep(0.02)
        self._sound_immediate_reaction()

    def _sound_counter_action(self):
        self.click()
        time.sleep(0.02)
        self._sound_immediate_reaction()

    def _sound_immediate_reaction(self):
        """触发闪避反击后第一时间连点左键并连点切人键."""
        current = self.get_current_char(raise_exception=False)
        by_zankou = current is self.zankou
        target = self.daffodill if by_zankou else self.zankou
        deadline = time.time() + self.SOUND_IMMEDIATE_SPAM_TIME
        while time.time() < deadline:
            self.send_key(
                target.index + 1, action_name="sound_switch", interval=0.1, down_time=0.05
            )
            self.click()
            time.sleep(0.06)
        self._sound_counter_by_zankou = by_zankou
        self._sound_counter_pending = True

    def _maybe_handle_sound_counter(self):
        if not self._sound_counter_pending or self._in_sound_reaction:
            return
        self._sound_counter_pending = False
        self._in_sound_reaction = True
        try:
            if self._sound_counter_by_zankou:
                self._sound_reaction_zankou()
            else:
                self._switch_to(self.zankou)
                self._zankou_combo()
        except NotInCombatException:
            raise
        except Exception as e:
            logger.error("sound counter reaction error", e)
        finally:
            self._in_sound_reaction = False

    def _sound_reaction_zankou(self):
        self._switch_to(self.daffodill)
        daffodill = self.daffodill
        start = time.time()
        while self.in_combat() and time.time() - start < self.SOUND_REACTION_DAFFODILL_TIME:
            if daffodill.ultimate_available():
                self._cast_q(daffodill)
                break
            if daffodill.skill_available():
                daffodill.click_skill()
            self.click()
            self.sleep(0.1)
        self._switch_to(self.zankou)

    # ---------------------------------------------------------------- switch

    def _ensure_current(self, char):
        if self.get_current_char(raise_exception=False) is not char:
            self._switch_to(char)

    def _switch_to(self, char):
        current = self.get_current_char(raise_exception=False)
        if current is char:
            return
        entry_skill = current is not None and bool(self.is_cycle_full())
        self._switch_to_char(
            char,
            current_char=current,
            has_intro=False,
            retry_intro=False,
            log_prefix="four_combo switch",
        )
        self._entry_skill_until = time.time() + self.ENTRY_SKILL_WAIT if entry_skill else 0.0
