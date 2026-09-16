import os
import threading
import time
from contextlib import contextmanager

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
    COMBO_RELEASE_GAP = 0.06
    COMBO_CLICK_GAP = 0.05
    GOLD_THRESHOLD = 0.7
    DAFFODILL_FIELD_TIME = 1.5
    PAD_FIELD_TIME = 1.5
    IROI_FUNNEL_POST_SLEEP = 0.3
    Q_READY_TIMEOUT = 5.0
    Q_REGISTER_TIMEOUT = 3.0
    Q_DOUBLE_TIMEOUT = 8.0
    Q_PRESS_INTERVAL = 0.12
    ENTRY_SKILL_WAIT = 1.6
    SWITCH_VERIFY_ATTEMPTS = 2
    SKILL_REGISTER_TIMEOUT = 2.0
    DAFFODILL_SKILL_REGISTER_TIMEOUT = 0.5
    CONTROLLABLE_TIMEOUT = 10.0
    ZANKOU_Q_READY_WINDOW = 2.0
    SOUND_REACTION_DAFFODILL_TIME = 1.0
    SOUND_IMMEDIATE_SPAM_TIME = 1.2
    SCRIPT_TICK = 0.05

    ACTION_LOG_PATH = os.path.join("logs", "four_combo_actions.log")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.default_config.update({"_enabled": False})
        self.trigger_interval = 0.1
        self.name = "四人连招"
        self.description = "固定编队(残虹/达芙蒂尔/伊洛伊/早雾)的固定连招脚本"
        self._last_run_state = None
        self._entry_skill_until = 0.0
        self._opener_gold_e_done = False
        self._precombat_daffodill_q_done = False
        self._sound_counter_pending = False
        self._sound_counter_by_zankou = False
        self._in_sound_reaction = False
        self._action_phase = ""
        self._action_log_handle = None
        self._action_log_last = 0.0
        self._action_log_lock = threading.Lock()
        self._suppress_combat_check = False

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

    def enable(self):
        self._reset_precombat()
        super().enable()

    def _reset_precombat(self):
        self._opener_gold_e_done = False
        self._precombat_daffodill_q_done = False
        self._sound_counter_pending = False
        self._action_phase = ""
        self._suppress_combat_check = False

    def check_combat(self):
        """紧输入序列(开局)期间抑制战斗检测, 避免大招特写被误判脱战打断."""
        if self._suppress_combat_check:
            return
        super().check_combat()

    @contextmanager
    def _suspend_combat_check(self):
        old = self._suppress_combat_check
        self._suppress_combat_check = True
        try:
            yield
        finally:
            self._suppress_combat_check = old

    def combat_end(self):
        super().combat_end()
        self._reset_precombat()

    def _precombat_gold_e(self):
        """入战前只轮询金 E(不做任何键鼠操作); 检测到金 E 才按 E 切达芙蒂尔并放 Q.

        长按由玩家自己预判操作, 脚本不知道何时开始长按, 因此这里只做检测,
        绝不主动 mouse_down / click / 切人。
        """
        if self._opener_gold_e_done:
            self._precombat_daffodill_q()
            return
        if len(self.chars) < 4 and not self.load_chars():
            return
        if self.get_current_char(raise_exception=False) is not self.zankou:
            return
        box = self.find_one(Labels.zankou_skill_gold, threshold=0.0)
        conf = box.confidence if box else 0.0
        if conf < self.GOLD_THRESHOLD:
            return
        logger.info(f"precombat gold E detected, conf={conf:.3f}, press E and switch daffodill")
        self._set_action_phase("precombat_gold_e")
        self._opener_gold_e_done = True
        self._skill_until_registered(self.zankou)
        self._switch_to(self.daffodill)
        self._precombat_daffodill_q()

    def _precombat_daffodill_q(self):
        """入战前在达芙蒂尔身上等 Q(只检测), 可用即放, 不要求进入战斗."""
        if self._precombat_daffodill_q_done:
            return
        daffodill = self.daffodill
        if self.get_current_char(raise_exception=False) is not daffodill:
            return
        if not daffodill.ultimate_available():
            return
        self._set_action_phase("precombat_daffodill_q")
        self._cast_q(daffodill)
        self._precombat_daffodill_q_done = True

    def _log_run_state(self, state):
        if state != self._last_run_state:
            self._last_run_state = state
            logger.info(f"four char combo state: {state}")

    # ----------------------------------------------------------- action log

    def _action_log(self, text):
        """把一次真实的键鼠操作写入独立轻量日志, 便于复盘连招按键时序."""
        try:
            now = time.time()
            with self._action_log_lock:
                if self._action_log_handle is None:
                    os.makedirs(os.path.dirname(self.ACTION_LOG_PATH), exist_ok=True)
                    self._action_log_handle = open(
                        self.ACTION_LOG_PATH, "a", encoding="utf-8", buffering=1
                    )
                    self._action_log_handle.write(
                        f"\n==== session {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n"
                    )
                delta = now - self._action_log_last if self._action_log_last else 0.0
                self._action_log_last = now
                stamp = time.strftime("%H:%M:%S", time.localtime(now))
                millis = int((now % 1) * 1000)
                phase = self._action_phase or "-"
                self._action_log_handle.write(
                    f"{stamp}.{millis:03d} +{delta:6.3f}s [{phase}] {text}\n"
                )
        except Exception as e:
            logger.error("four combo action log write failed", e)

    def _set_action_phase(self, phase):
        if phase == self._action_phase:
            return
        self._action_phase = phase
        self._action_log(f"==== {phase} ====")

    def _close_action_log(self):
        handle = self._action_log_handle
        self._action_log_handle = None
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass

    # -------------------------------------------------- input log wrappers

    def click(self, *args, **kwargs):
        key = kwargs.get("key", "left")
        name = kwargs.get("name")
        self._action_log(f"click {key}" + (f" ({name})" if name else ""))
        return super().click(*args, **kwargs)

    def send_key(self, key, *args, **kwargs):
        self._action_log(f"key {key}")
        return super().send_key(key, *args, **kwargs)

    def send_key_down(self, key, *args, **kwargs):
        self._action_log(f"key_down {key}")
        return super().send_key_down(key, *args, **kwargs)

    def send_key_up(self, key, *args, **kwargs):
        self._action_log(f"key_up {key}")
        return super().send_key_up(key, *args, **kwargs)

    def mouse_down(self, *args, **kwargs):
        self._action_log(f"mouse_down {kwargs.get('key', 'left')}")
        return super().mouse_down(*args, **kwargs)

    def mouse_up(self, *args, **kwargs):
        self._action_log(f"mouse_up {kwargs.get('key', 'left')}")
        return super().mouse_up(*args, **kwargs)

    def on_destroy(self):
        self._close_action_log()
        super().on_destroy()

    def _run_rotation(self):
        logger.info(f"four char combo rotation start, chars={[c.ufn_name for c in self.chars]}")
        self._opener()
        while self.in_combat():
            self._loop_once()

    # --------------------------------------------------------------- opener

    def _opener(self):
        logger.info("four char combo opener start")
        self._set_action_phase("opener")
        with self._suspend_combat_check():
            if not self._opener_gold_e_done:
                self._ensure_current(self.zankou)
                self._zankou_gold_e()

            self._switch_to(self.daffodill)
            if not self._precombat_daffodill_q_done:
                self._cast_q(self.daffodill)

            self._switch_to(self.iroi)
            self._skill_until_registered(self.iroi)

            self._switch_to(self.sakiri)
            self._cast_q(self.sakiri)
            self._skill_until_registered(self.sakiri)

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
        self._set_action_phase("loop")
        with self._suspend_combat_check():
            self._maybe_handle_sound_counter()

            iroi = self.iroi
            self._switch_to(iroi)
            self._skill_until_registered(iroi)
            if not iroi.ultimate_available() and not self._pad_until_q(iroi):
                return
            self._iroi_q_funnel()

            sakiri = self.sakiri
            self._switch_to(sakiri)
            if not sakiri.ultimate_available() and not self._pad_until_q(sakiri):
                return
            self._cast_q(sakiri)
            self._skill_until_registered(sakiri)

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
        """达芙蒂尔在场窗口: E 能放就放(非阻塞), 到 DAFFODILL_FIELD_TIME 或 Q 可用后离开."""
        self._set_action_phase("daffodill_window")
        start = time.time()
        if daffodill.skill_available():
            self._skill_until_registered(daffodill, self.DAFFODILL_SKILL_REGISTER_TIMEOUT)
        while self.in_combat():
            self._maybe_handle_sound_counter()
            if daffodill.ultimate_available():
                self._cast_q(daffodill)
                return
            if time.time() - start >= self.DAFFODILL_FIELD_TIME:
                return
            if daffodill.skill_available():
                self._skill_until_registered(daffodill, self.DAFFODILL_SKILL_REGISTER_TIMEOUT)
            self.click()
            self.sleep(0.1)

    def _skill_until_registered(self, char, timeout=None):
        """连按 E 直到技能进入 CD(已释放); 观察到 CD 即可切人, 不等动画收尾.

        E 释放后不打断动作, 所以只要 CD 出现就认为放出去了, 立即返回便于切人。
        """
        if timeout is None:
            timeout = self.SKILL_REGISTER_TIMEOUT
        if char.has_cd("skill"):
            return True
        deadline = time.time() + timeout
        with self.skip_sleep_checks() as skip:
            skip.check_combat = True
            while time.time() < deadline:
                char.send_skill_key(down_time=0.05)
                self.sleep(0.05)
                if char.has_cd("skill"):
                    logger.info(f"{char} skill registered")
                    return True
        logger.warning(
            f"{char} skill not registered within {timeout}s "
            f"(available={char.skill_available()}, cd={self.get_cd('skill'):.2f}, "
            f"current={self.get_current_char(raise_exception=False)})"
        )
        return False

    # ------------------------------------------------------------ pad loops

    def _pad_until_q(self, target):
        """在 target 身上打 PAD_FIELD_TIME 秒 -> 切残虹二连 -> 切回, 直到 target Q 可放."""
        self._set_action_phase("pad_until_q")
        while self.in_combat() and not target.ultimate_available():
            start = time.time()
            while (
                self.in_combat()
                and not target.ultimate_available()
                and time.time() - start < self.PAD_FIELD_TIME
            ):
                self._maybe_handle_sound_counter()
                self.click()
                self.sleep(0.1)
            if not self.in_combat() or target.ultimate_available():
                break
            if self._zankou_combo_switch(target) is not target:
                return False
        return self.in_combat() and target.ultimate_available()

    # ------------------------------------------------------------- zankou

    def _zankou_gold_e(self):
        self._set_action_phase("zankou_gold_e")
        self._hold_until_gold()
        self._skill_until_registered(self.zankou)

    def _zankou_combo(self):
        """残虹二连: 长按轮询金E -> 松开 -> 等 0.1s -> 单击左键 -> 等 0.05s."""
        self._set_action_phase("zankou_combo")
        self._hold_until_gold()
        self.sleep(self.COMBO_RELEASE_GAP)
        self.click()
        self.sleep(self.COMBO_CLICK_GAP)

    def _zankou_combo_switch(self, default_target):
        self._set_action_phase("zankou_enter")
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
        self._set_action_phase("zankou_double_q")
        zankou = self.zankou
        logger.info(
            f"zankou double q start lit={self._q_button_lit()} "
            f"cd={self.get_cd('ultimate'):.2f} "
            f"current={self.get_current_char(raise_exception=False)}"
        )
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
        logger.warning(
            f"{char} ultimate not ready, skip cast (lit={self._q_button_lit()}, "
            f"cd={self.get_cd('ultimate'):.2f}, "
            f"current={self.get_current_char(raise_exception=False)})"
        )
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
        self._set_action_phase("iroi_funnel")
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
        """残虹触发声音反击: 切达芙蒂尔, Q/E 能放就放, SOUND_REACTION_DAFFODILL_TIME 后切回残虹."""
        self._set_action_phase("sound_zankou")
        self._switch_to(self.daffodill)
        daffodill = self.daffodill
        start = time.time()
        while self.in_combat() and time.time() - start < self.SOUND_REACTION_DAFFODILL_TIME:
            if daffodill.ultimate_available():
                self._cast_q(daffodill)
                break
            if daffodill.skill_available():
                self._skill_until_registered(daffodill, self.DAFFODILL_SKILL_REGISTER_TIMEOUT)
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
        for attempt in range(1, self.SWITCH_VERIFY_ATTEMPTS + 1):
            self._switch_to_char(
                char,
                current_char=current,
                has_intro=False,
                retry_intro=False,
                log_prefix="four_combo switch",
            )
            if self._verify_current_char(char):
                break
            detection = self._get_current_char_detection(
                frame=self.frame, char_count=self.team_size
            )
            logger.warning(
                f"four combo switch verify failed, want {char.index}, "
                f"got {detection.index} (reason={detection.reason}, attempt={attempt})"
            )
        self._entry_skill_until = time.time() + self.ENTRY_SKILL_WAIT if entry_skill else 0.0

    def _verify_current_char(self, char):
        """用图像检测确认目标角色确实在场, 避免 active health change 误判切换成功."""
        detection = self._get_current_char_detection(frame=self.frame, char_count=self.team_size)
        return detection.accepted and detection.index == char.index
