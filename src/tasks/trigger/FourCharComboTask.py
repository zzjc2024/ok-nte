import logging
import os
import threading
import time
from contextlib import contextmanager
from enum import Enum

import cv2
import numpy as np
from ok import Logger, TriggerTask

from src.char.Daffodill import Daffodill
from src.char.Iroi import Iroi
from src.char.Sakiri import Sakiri
from src.char.Zankou import Zankou
from src.combat.BaseCombatTask import BaseCombatTask, NotInCombatException, cd_regex
from src.Labels import Labels
from src.sound_trigger.SoundCombatContext import SoundCombatContext
from src.utils import game_filters as gf

logger = Logger.get_logger(__name__)

COMBO_LOG_PATH = os.path.join("logs", "four_combo.log")


class ZankouComboAnomaly(Exception):
    """残虹长按在可控状态下 0.8~0.9s 没出金 E 且没掉血 -> 状态异常, 停任务排查."""


class HoldResult(Enum):
    """一次残虹长按的结局."""

    GOLD = "gold"  # 金 E 出现, 正常
    INTERRUPTED = "interrupted"  # 被新攻击警报打断, 上层负责重新闪避
    NO_GOLD = "no_gold"  # 到上限都没出金 E
    DODGE = "dodge"  # 长按期间闪避触发了(攻击被打断), 不是异常: 点左键 -> 等反击动画 -> 重打长按
    HANDLED = "handled"  # 成功闪避的反击路径已经自己打完二连, 上层不要再补

_COMBO_LOG_KEYWORDS = (
    "FourCharComboTask",
    "four_combo",
    "four char combo",
    "CombatCheck",
    "Dodge",
    "SoundCombatContext",
    "SoundListener",
)


class _ComboLogFilter(logging.Filter):
    """只放行四人连招排查相关的日志行.

    正文和 logger 名都看: 这样 FourCharComboTask / CombatCheck / SoundListener
    等模块自身的日志也能进来, 否则形如 "Zankou skill registered" 这种不含关键词
    的正文会被漏掉。
    """

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return False
        name = record.name or ""
        return any(keyword in message or keyword in name for keyword in _COMBO_LOG_KEYWORDS)


def _ensure_combo_log_handler():
    """确保 ok logger 上挂着写 logs/four_combo.log 的过滤 handler."""
    ok_logger = logging.getLogger("ok")
    for handler in ok_logger.handlers:
        if getattr(handler, "_four_combo_handler", False):
            return
    try:
        os.makedirs(os.path.dirname(COMBO_LOG_PATH), exist_ok=True)
        handler = logging.FileHandler(COMBO_LOG_PATH, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        handler.addFilter(_ComboLogFilter())
        handler._four_combo_handler = True
        ok_logger.addHandler(handler)
    except Exception as e:
        logger.error(f"four combo log handler setup failed {e}")


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

    COMBO_HOLD_MIN = 0.67
    COMBO_HOLD_MAX = 0.9
    COMBO_POLL_INTERVAL = 0.05
    COMBO_RELEASE_GAP = 0.06
    COMBO_CLICK_GAP = 0.05
    COMBO_DODGE_RETRY_MAX = 3
    HEALTH_DROP_RATIO = 0.02
    HEALTH_DROP_MIN_PIXELS = 4
    DODGE_RETRY_TIMEOUT = 3.0
    DODGE_RETRY_INTERVAL = 0.15
    GOLD_THRESHOLD = 0.7
    DAFFODILL_FIELD_TIME = 1.5
    PAD_FIELD_TIME = 1.5
    IROI_FUNNEL_POST_SLEEP = 0.3
    Q_READY_TIMEOUT = 5.0
    Q_WAIT_ATTACK_INTERVAL = 0.2
    Q_REGISTER_TIMEOUT = 3.0
    Q_DOUBLE_TIMEOUT = 8.0
    Q_PRESS_INTERVAL = 0.12
    ENTRY_SKILL_WAIT = 1.1
    SWITCH_SETTLE_TIME = 0.1
    SUPPRESS_SWITCH_CLICK = True
    SWITCH_CONFIRM_TIMEOUT = 3.0
    CYCLE_BAR_VISIBLE_MIN_PIXELS = 20
    IROI_FUNNEL_ANIMATION_TIMEOUT = 5.0
    SKILL_REGISTER_TIMEOUT = 2.0
    DAFFODILL_SKILL_REGISTER_TIMEOUT = 0.5
    CONTROLLABLE_TIMEOUT = 10.0
    ZANKOU_Q_READY_WINDOW = 2.0
    ANIMATION_STABLE_TIME = 0.3
    CYCLE_STAY_RATIO = 0.9
    COMBAT_STATE_LOG_INTERVAL = 2.0
    OPENER_COMBAT_LOST_GRACE = 1.5
    SOUND_IMMEDIATE_SPAM_TIME = 1.2
    SOUND_SUCCESS_CLICK_DOWN = 0.08
    # 闪避反击动画时长: 点完左键要等它放完, 期间长按普攻不生效(会不出金 E 而被误判异常)。
    # 手动实测"闪避成功音 -> 开始长按"中位 0.46s, 减去点按 0.08s 约 0.38s。未实测校准。
    DODGE_COUNTER_WAIT = 0.35
    SCRIPT_TICK = 0.05

    # 必须与 BaseCombatTask.refresh_cd() 里的 OCR 区域保持一致
    CD_OCR_BOX = (0.8594, 0.8847, 0.9578, 0.9139)

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
        self._in_sound_reaction = False
        self._pad_target = None
        self._alert_interrupt = threading.Event()
        self._dodge_motion_heard = threading.Event()
        self._dodge_success_heard = threading.Event()
        # 最近一次听到闪避(动作音/成功音)的时刻, 声音线程写, 主线程读:
        # 长按期间它变大 = 这次长按被闪避打断了, 不能按"没掉血"抛异常。
        self._dodge_heard_at = 0.0
        self._holding = False
        self._combat_state_logged_at = 0.0
        self._q_wait_attack_at = 0.0
        self._opener_lost_since = 0.0
        self._action_phase = ""
        self._action_log_handle = None
        self._action_log_last = 0.0
        self._action_log_lock = threading.Lock()
        self._suppress_combat_check = False
        _ensure_combo_log_handler()

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
            dodge_success_action=self._sound_dodge_success_action,
        )
        logger.info(f"four char combo loaded, current index {current_index}")
        return True

    def run(self):
        _ensure_combo_log_handler()
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
        self._alert_interrupt.clear()
        self._dodge_motion_heard.clear()
        self._dodge_success_heard.clear()
        self._dodge_heard_at = 0.0
        self._holding = False
        self._opener_lost_since = 0.0
        self._q_wait_attack_at = 0.0

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

    def _maybe_log_combat_state(self, tag):
        """低频打印各脱战信号, 用于定位"敌人已死但 in_combat 仍为 True".

        写进 logs/four_combo.log, 打死敌人后如果卡在战斗状态, 直接看这行就知道是
        哪个信号(scene 缓存 / boss / lv / target / 红血条 / uncertain)把状态按住了。
        """
        now = time.time()
        if now - self._combat_state_logged_at < self.COMBAT_STATE_LOG_INTERVAL:
            return
        self._combat_state_logged_at = now
        self.next_frame()
        try:
            state = (
                f"in_combat={bool(self._in_combat)} "
                f"scene_cache={self.scene.in_combat()} "
                f"uncertain={self.combat_detect_uncertain} "
                f"miss={self.combat_detect_state.miss_count} "
                f"boss_flag={self._boss_fight} "
                f"is_boss={bool(self.is_boss())} "
                f"lv={bool(self.find_lv())} "
                f"target={bool(self.find_target())} "
                f"health_bar={self.has_health_bar()}"
            )
        except Exception as e:
            state = f"error={e}"
        logger.info(f"four char combo combat state [{tag}] {state}")

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
        if self.SUPPRESS_SWITCH_CLICK and kwargs.get("action_name") == "switch_char_click":
            return False
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
        """开局固定序列.

        序列期间 check_combat 被抑制(避免大招特写被误判脱战), 所以每一步之间自己做一次
        脱战判断 `_opener_combat_lost()`; 一旦确认敌人没了就中止, 并清掉开场记忆,
        让下一场战斗重新从金 E 开始。
        """
        logger.info("four char combo opener start")
        self._set_action_phase("opener")
        aborted = False
        target = None
        with self._suspend_combat_check():
            if not self._opener_gold_e_done:
                self._ensure_current(self.zankou)
                self._zankou_gold_e()
            aborted = self._opener_combat_lost("gold_e")

            if not aborted:
                self._switch_to(self.daffodill)
                if not self._precombat_daffodill_q_done:
                    self._cast_q(self.daffodill)
                aborted = self._opener_combat_lost("daffodill")

            if not aborted:
                self._switch_to(self.iroi)
                self._skill_until_registered(self.iroi)
                aborted = self._opener_combat_lost("iroi")

            if not aborted:
                self._switch_to(self.sakiri)
                self._cast_q(self.sakiri)
                self._skill_until_registered(self.sakiri)
                aborted = self._opener_combat_lost("sakiri")

            if not aborted:
                self._switch_to(self.zankou)
                aborted = self._opener_combat_lost("zankou_before_double_q")
            if not aborted:
                self._zankou_double_q()
                self._zankou_combo()
                aborted = self._opener_combat_lost("zankou_double_q")

            if not aborted:
                self._switch_to(self.iroi)
                self._iroi_q_funnel()
                aborted = self._opener_combat_lost("iroi_funnel")

            if not aborted:
                target = self._zankou_combo_switch(self.daffodill)

        if aborted:
            logger.info("four char combo opener aborted (combat ended), reset precombat memory")
            self._opener_gold_e_done = False
            self._precombat_daffodill_q_done = False
            return
        if target is self.daffodill:
            self._daffodill_until_cycle_full()

    def _enemy_present(self):
        """便宜的"敌人还在"信号: boss / Lv / 目标 / 红血条.

        故意不用 `in_combat()`: 后者在关卡切换或敌人刚死时会因为异步检测 pending、
        或"重新索敌成功"而继续返回 True; 原始信号才是真值。也不会有 middle_click 副作用。
        """
        self.next_frame()
        return bool(
            self.is_boss() or self.find_lv() or self.find_target() or self.has_health_bar()
        )

    def _opener_combat_lost(self, tag):
        """开场序列中途的脱战判断.

        连续 OPENER_COMBAT_LOST_GRACE 秒看不到任何敌人信号才确认脱战。
        """
        self._maybe_log_combat_state(f"opener_{tag}")
        if self._enemy_present():
            self._opener_lost_since = 0.0
            return False
        now = time.time()
        if not self._opener_lost_since:
            self._opener_lost_since = now
            return False
        if now - self._opener_lost_since < self.OPENER_COMBAT_LOST_GRACE:
            return False
        logger.info(f"four char combo opener combat lost at [{tag}]")
        self._opener_lost_since = 0.0
        return True

    # ------------------------------------------------------------ main loop

    def _loop_once(self):
        self._set_action_phase("loop")
        with self._suspend_combat_check():
            self._maybe_log_combat_state("loop")
            self._maybe_handle_sound_counter()

            iroi = self.iroi
            self._switch_to(iroi)
            self._skill_until_registered(iroi)
            if not iroi.ultimate_available() and not self._pad_until_q(iroi):
                return
            self._iroi_q_funnel()

            if self._zankou_combo_switch(self.sakiri) is self.iroi:
                return

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
        q_available = zankou.ultimate_available()
        q_remaining = self._zankou_q_remaining()
        logger.info(
            f"zankou fixed step q_available={q_available} q_cd={q_remaining:.2f} "
            f"lit={self._q_button_lit()} current={self.get_current_char(raise_exception=False)}"
        )
        if q_available:
            self._zankou_double_q()
        elif 0 < q_remaining < self.ZANKOU_Q_READY_WINDOW:
            self._stay_until_q_ready(zankou)
            self._zankou_double_q()
        return self._zankou_combo_switch(self.daffodill)

    def _daffodill_until_cycle_full(self):
        daffodill = self.daffodill
        while self.in_combat():
            self._maybe_log_combat_state("daffodill_loop")
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
        start = time.time()
        deadline = start + timeout
        last_log = -1.0
        with self.skip_sleep_checks() as skip:
            skip.check_combat = True
            while time.time() < deadline:
                elapsed = time.time() - start
                if elapsed - last_log >= 0.4:
                    last_log = elapsed
                    logger.info(
                        f"{char} skill wait t={elapsed:.2f}s "
                        f"lit={self.box_highlighted('skill')} "
                        f"cd={self.get_cd('skill'):.2f} "
                        f"in_team={bool(self.is_in_team())}"
                    )
                if char.skill_available():
                    char.send_skill_key(down_time=0.05)
                self.sleep(0.05)
                if char.has_cd("skill"):
                    logger.info(f"{char} skill registered")
                    return True
        logger.warning(
            f"{char} skill not registered within {timeout}s "
            f"(lit={self.box_highlighted('skill')}, cd={self.get_cd('skill'):.2f}, "
            f"in_team={bool(self.is_in_team())}, "
            f"current={self.get_current_char(raise_exception=False)})"
        )
        return False

    # ------------------------------------------------------------ pad loops

    def _pad_until_q(self, target):
        """在 target 身上打 PAD_FIELD_TIME 秒 -> 切残虹二连 -> 切回, 直到 target Q 可放.

        期间记录 `_pad_target`, 供声音反击把切人目标对准当前垫刀对象。
        """
        self._set_action_phase("pad_until_q")
        self._pad_target = target
        try:
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
        finally:
            self._pad_target = None

    # ------------------------------------------------------------- zankou

    def _zankou_gold_e(self):
        """开局金 E: 长按轮询金E -> 点 E."""
        self._set_action_phase("zankou_gold_e")
        if self._zankou_hold_with_recovery() is HoldResult.HANDLED:
            return
        self._skill_until_registered(self.zankou)

    def _zankou_combo(self):
        """残虹二连: 长按轮询金E -> 松开 -> 单击左键 (间隔见 COMBO_* 常量)."""
        self._set_action_phase("zankou_combo")
        if self._zankou_hold_with_recovery() is HoldResult.HANDLED:
            return
        self.sleep(self.COMBO_RELEASE_GAP)
        self.click()
        self.sleep(self.COMBO_CLICK_GAP)

    def _zankou_combo_interruptible(self):
        """残虹二连(长按期间可被新攻击警报打断); 返回 True 表示被打断."""
        self._set_action_phase("zankou_combo")
        result = self._zankou_hold_with_recovery(self._alert_interrupt)
        if result is HoldResult.INTERRUPTED:
            return True
        if result is HoldResult.HANDLED:
            return False
        self.sleep(self.COMBO_RELEASE_GAP)
        self.click()
        self.sleep(self.COMBO_CLICK_GAP)
        return False

    def _zankou_hold_with_recovery(self, interrupt_event=None):
        """长按轮询金 E; 没出金 E 时按"是否掉血"分流, 返回最终结果.

        **不重按**: 长按起点由 `_switch_to` 的入场技预测 (`_entry_skill_until`) 保证;
        重按会白等 1~2s, 限时关卡里等于直接失败。

        - 掉血 = 大概率被打断 -> 连按 shift 直到闪避真的触发, 然后重打长按;
        - 长按期间闪避触发 = 普攻被闪避动画打断, **不是异常** ->
          点左键触发闪避反击 -> 等反击动画 -> 重打长按;
        - 没掉血也没闪避 = 状态异常 -> 抛异常停任务。
        """
        damaged = False
        for dodges in range(1, self.COMBO_DODGE_RETRY_MAX + 1):
            result, press_damaged = self._hold_until_gold(interrupt_event=interrupt_event)
            damaged = damaged or press_damaged
            if result is HoldResult.DODGE:
                if self._recover_from_dodge():
                    return HoldResult.HANDLED
                damaged = False
                continue
            if result is not HoldResult.NO_GOLD:
                return result
            if not damaged:
                self._raise_combo_anomaly(
                    f"zankou gold E missing while not damaged (hold {self.COMBO_HOLD_MAX}s)"
                )
            logger.warning(
                f"zankou gold E missing but damaged, dodge then retry "
                f"({dodges}/{self.COMBO_DODGE_RETRY_MAX})"
            )
            if not self._dodge_until_triggered():
                return HoldResult.HANDLED
            damaged = False
        self._raise_combo_anomaly(
            f"zankou gold E still missing after {self.COMBO_DODGE_RETRY_MAX} dodges"
        )

    def _recover_from_dodge(self):
        """长按期间闪避触发后的恢复: 点左键触发闪避反击 -> 等反击动画 -> 重打长按.

        返回 True = 等待期间"闪避成功音"的反击路径已经接管并自己打完二连, 上层不要再补。
        """
        self._set_action_phase("dodge_retry")
        self._dodge_success_heard.clear()
        logger.info("zankou hold interrupted by dodge, counter attack then retry combo")
        self.click(down_time=self.SOUND_SUCCESS_CLICK_DOWN)
        self.sleep(self.DODGE_COUNTER_WAIT)
        if self._dodge_success_heard.is_set():
            logger.info("dodge success reaction took over during the counter wait")
            return True
        return False

    def _dodge_until_triggered(self):
        """连按闪避(shift)直到听到"闪避动作音", 确认闪避真的触发了.

        角色处于不可控状态时按 shift 不会产生闪避动作音, 所以这个音效就是
        "闪避是否生效"的判据。返回 True = 普通闪避触发, 上层重打长按;
        返回 False = 触发的是"成功闪避", 现成的成功闪避反击路径已经打完二连。
        """
        self._set_action_phase("dodge_retry")
        self._dodge_motion_heard.clear()
        self._dodge_success_heard.clear()
        deadline = time.time() + self.DODGE_RETRY_TIMEOUT
        with self.skip_sleep_checks() as skip:
            skip.all = True
            while time.time() < deadline:
                self._press_dodge()
                if self._dodge_success_heard.is_set():
                    logger.info(
                        "dodge retry: perfect dodge heard, run the existing dodge-success reaction"
                    )
                    SoundCombatContext().discard_pending_action()
                    self._sound_dodge_success_action()
                    return False
                if self._dodge_motion_heard.is_set():
                    logger.info("dodge retry: dodge motion heard, dodge triggered")
                    return True
                self.sleep(self.DODGE_RETRY_INTERVAL)
        self._raise_combo_anomaly(
            f"dodge never triggered within {self.DODGE_RETRY_TIMEOUT}s while pressing shift"
        )

    def _press_dodge(self):
        """按一次闪避(shift)."""
        self.send_key("lshift")

    def _health_pixels(self):
        """当前角色血条的红条像素数; None 表示这一帧没取到."""
        snapshot = self._get_health_snapshot(self.frame)
        if snapshot is None:
            return None
        return int(np.count_nonzero(snapshot))

    def _health_drop_margin(self, peak):
        return max(self.HEALTH_DROP_MIN_PIXELS, peak * self.HEALTH_DROP_RATIO)

    def _raise_combo_anomaly(self, reason):
        """状态异常: 落盘现场 -> 停任务 -> 抛异常让 executor 报错."""
        message = f"four char combo anomaly: {reason}"
        logger.error(message)
        self._dump_q_cd_state("combo_anomaly")
        self.disable()
        raise ZankouComboAnomaly(message)

    def _zankou_combo_switch(self, default_target):
        """切残虹二连, 再按环合值决定去向.

        环合 >= CYCLE_STAY_RATIO 时不再和达芙蒂尔互切: 留在残虹身上连点左键直到环合满,
        然后切伊洛伊; 否则切 default_target。
        """
        self._set_action_phase("zankou_enter")
        self._switch_to(self.zankou)
        # 切人刚确认时角色还在切人动画里, 立刻长按普攻不生效 (实测确认后 0.001s 就长按, 金 E 全空)
        self.sleep(self.SWITCH_SETTLE_TIME)
        self._zankou_combo()
        ratio = self.cycle_ratio()
        if ratio >= self.CYCLE_STAY_RATIO:
            logger.info(
                f"four char combo cycle ratio={ratio:.2f} >= {self.CYCLE_STAY_RATIO}, "
                f"stay on zankou until full"
            )
            self._stay_until_cycle_full()
            self._switch_to(self.iroi)
            return self.iroi
        self._switch_to(default_target)
        return default_target

    def _stay_until_cycle_full(self):
        """留在残虹身上连点左键, 直到环合满或脱战."""
        self._set_action_phase("zankou_cycle_full")
        while self.in_combat() and not self.is_cycle_full():
            self._maybe_handle_sound_counter()
            self.next_frame()
            self.click()
            self.sleep(0.1)
        logger.info(f"four char combo cycle full ratio={self.cycle_ratio():.2f}")

    def _hold_until_gold(self, interrupt_event=None):
        """长按轮询金 E; 返回 (HoldResult, damaged).

        damaged 由长按期间的**多次**血条采样得出 (伊洛伊大招会在后台回血,
        只取首尾两次会出现"掉血-回血-采样"而看不到掉血)。
        """
        interrupted = False
        dodged = False
        damaged = False
        health_peak = None
        health_samples = 0
        wait = self._entry_skill_until - time.time()
        if wait > 0:
            logger.info(f"zankou wait entry skill {wait:.2f}s before combo")
            self.sleep(wait)
        self._entry_skill_until = 0.0
        hold_start = time.time()
        self._holding = True
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
                    if interrupt_event is not None and interrupt_event.is_set():
                        interrupted = True
                        break
                    # 长按期间闪避触发(动作音/成功音) -> 普攻被打断, 不是异常
                    if self._dodge_heard_at > hold_start:
                        dodged = True
                        break
                    box = self.find_one(Labels.zankou_skill_gold, threshold=0.0)
                    conf = box.confidence if box else 0.0
                    if conf > best_conf:
                        best_conf = conf
                    if time.time() >= min_until and conf >= self.GOLD_THRESHOLD:
                        gold = True
                        break
                    pixels = self._health_pixels()
                    if pixels is not None:
                        health_samples += 1
                        if health_peak is None or pixels > health_peak:
                            health_peak = pixels
                        elif pixels < health_peak - self._health_drop_margin(health_peak):
                            damaged = True
                    self.sleep(self.COMBO_POLL_INTERVAL)
        finally:
            self._holding = False
            self.mouse_up()
        if interrupted:
            logger.info("zankou combo hold interrupted by new attack alert")
            return HoldResult.INTERRUPTED, damaged
        if dodged:
            logger.info("zankou combo hold interrupted by dodge")
            return HoldResult.DODGE, damaged
        if gold:
            logger.info(f"zankou gold E detected, conf={best_conf:.3f}")
            return HoldResult.GOLD, damaged
        logger.warning(
            f"zankou gold E not detected, best conf={best_conf:.3f} "
            f"(hold={self.COMBO_HOLD_MAX}s, health_samples={health_samples}, "
            f"health_peak={health_peak}, damaged={damaged})"
        )
        if health_samples == 0:
            # 一帧血条都没取到 -> 无法证明"没掉血", 走更安全的闪避重试分支
            logger.warning("zankou health not sampled during hold, assume interrupted")
            damaged = True
        return HoldResult.NO_GOLD, damaged

    def _zankou_double_q(self):
        """残虹双 Q.

        连按 Q, 依次确认四个阶段: 第1段进特写 -> 第1段出特写 -> 第2段进特写 -> 第2段出特写。
        之后**不能立刻二连**: 必须等到"第 2 段 Q 动画结束"且"Q 冷却数字真正开始变小"
        两个条件同时成立 (见 `_wait_double_q_recovery`), 否则长按会落在动画/收招里,
        普攻不生效, E 不会变金, 二连接不上。
        """
        self._set_action_phase("zankou_double_q")
        zankou = self.zankou
        logger.info(
            f"zankou double q start lit={self._q_button_lit()} "
            f"cd={self.get_cd('ultimate'):.2f} "
            f"current={self.get_current_char(raise_exception=False)}"
        )
        if not self._enemy_present():
            logger.warning("zankou double q skipped: no enemy signal, keep the ultimate")
            return
        if not self._press_q_ready(zankou):
            return
        self._wait_in_team(timeout=self.ENTRY_SKILL_WAIT)
        deadline = time.time() + self.Q_DOUBLE_TIMEOUT
        with self.skip_sleep_checks() as skip:
            skip.check_combat = True
            stage = self._press_q_through_animations(zankou, deadline)
        if stage >= 4:
            logger.info("zankou double q done (2 animations)")
        else:
            logger.warning(f"zankou double q incomplete, stage={stage}/4")
        self._wait_double_q_recovery(stage)

    def _press_q_through_animations(self, zankou, deadline):
        """连按 Q 并等待四个阶段确认; 返回已确认的阶段数(0~4).

        阶段: enter1(第1段特写进入) / exit1 / enter2 / exit2。
        每个阶段都要求 is_in_team 稳定保持 ANIMATION_STABLE_TIME 才算确认,
        防止特写期间的状态抖动把动画数错。
        """
        stages = ("enter1", "exit1", "enter2", "exit2")
        stage = 0
        stable_since = None
        last_press = 0.0
        while stage < len(stages) and time.time() < deadline:
            now = time.time()
            if now - last_press >= self.Q_PRESS_INTERVAL:
                zankou.send_ultimate_key(
                    action_name="four_combo_q", interval=0.1, down_time=0.05
                )
                last_press = now
            want_in_team = stages[stage].startswith("exit")
            if bool(self.is_in_team()) == want_in_team:
                if stable_since is None:
                    stable_since = now
                elif now - stable_since >= self.ANIMATION_STABLE_TIME:
                    logger.info(f"zankou q {stages[stage]} confirmed")
                    stage += 1
                    stable_since = None
            else:
                stable_since = None
            self.sleep(self.SCRIPT_TICK)
        return stage

    def _wait_in_team(self, timeout=2.0):
        """等脱离大招动画(is_in_team 恢复)."""
        start = time.time()
        while not self.is_in_team() and time.time() - start < timeout:
            self.sleep(0.05)

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
        self._q_wait_attack_at = time.time()
        if self.wait_until(
            char.ultimate_available,
            time_out=self.Q_READY_TIMEOUT,
            pre_action=self._q_wait_attack,
            raise_if_not_found=False,
        ):
            return True
        self._dump_q_cd_state("q_not_ready")
        logger.warning(
            f"{char} ultimate not ready, skip cast (lit={self._q_button_lit()}, "
            f"cd={self.get_cd('ultimate'):.2f}, "
            f"current={self.get_current_char(raise_exception=False)})"
        )
        return False

    def _dump_q_cd_state(self, tag):
        """CD 判定异常时把整帧 + 技能区 OCR 明细落盘, 用于确认是不是误读.

        截图写 logs/(本地生成物, 不提交); 日志里带 OCR 原始文本和坐标,
        这样能判断读到的数字到底来自 E 图标还是 Q 图标。
        """
        try:
            os.makedirs("logs", exist_ok=True)
            path = os.path.join(
                "logs", f"four_combo_{tag}_{time.strftime('%Y%m%d_%H%M%S')}.png"
            )
            frame = self.frame
            if frame is not None:
                cv2.imwrite(path, frame)
                bar = self.box_of_screen(0.78, 0.84, 1.0, 0.96).crop_frame(frame)
                if bar is not None and bar.size:
                    cv2.imwrite(path[:-4] + "_cdbar.png", bar)
            texts = self.ocr(
                *self.CD_OCR_BOX,
                frame_processor=gf.isolate_text_to_black,
                match=cd_regex,
            )
            detail = [f"{t.name}@({t.x},{t.y})" for t in (texts or [])]
            logger.warning(
                f"four combo q cd dump [{tag}] saved={path} "
                f"ocr={detail} cds={self.cds} "
                f"current={self.get_current_char(raise_exception=False)}"
            )
        except Exception as e:
            logger.error(f"four combo q cd dump failed {e}")

    def _q_wait_attack(self):
        """等 Q 就绪期间继续普攻, 避免角色在场上站着发呆.

        `wait_until` 的轮询循环不带 sleep, 所以这里自己做节流; `_press_q_ready`
        开始时重置时间戳, 因此 Q 若很快可用则一次都不会多点。
        """
        now = time.time()
        if now - self._q_wait_attack_at < self.Q_WAIT_ATTACK_INTERVAL:
            return
        self._q_wait_attack_at = now
        self.click()

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
        """伊洛伊浮游炮: 长按部分对齐原版出招表.

        原版 Iroi._wait_ultimate_unfreeze 内部自己 mouse_down, 等待信号是
        box_ultimate 图标变化 / Q 不可用; 这里沿用, 松手后按本脚本规范补
        sleep IROI_FUNNEL_POST_SLEEP(0.3s) + 单击左键。
        """
        self._set_action_phase("iroi_funnel")
        iroi = self.iroi
        if not self._press_q_ready(iroi):
            return
        was_lit = self._q_button_lit()
        self._press_q_until_registered(iroi, was_lit, self.Q_REGISTER_TIMEOUT)
        self._wait_iroi_cutscene()
        try:
            iroi._wait_ultimate_unfreeze(time.time())
        finally:
            if iroi._mouse_pressed:
                self.mouse_up()
                iroi._mouse_pressed = False
        self.sleep(self.IROI_FUNNEL_POST_SLEEP)
        self.click()

    def _cycle_bar_white_pixels(self):
        """环合条环形区域的白像素数; 特写期间环合条不可见(≈0)."""
        img = self.box_of_screen_scaled(
            2560, 1440, 944, 1316, width_original=66, height_original=66
        ).crop_frame(self.frame)
        h, w = img.shape[:2]
        side = h
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        _, thresh = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY)
        mask = np.zeros((h, w), dtype=np.uint8)
        center = (w // 2, h // 2)
        outer_r = side // 2
        inner_r = int(outer_r * 0.85)
        cv2.circle(mask, center, outer_r, 255, -1)
        cv2.circle(mask, center, inner_r, 0, -1)
        ring = cv2.bitwise_and(thresh, thresh, mask=mask)
        return int(np.count_nonzero(ring))

    def _is_cycle_bar_visible(self):
        return self._cycle_bar_white_pixels() >= self.CYCLE_BAR_VISIBLE_MIN_PIXELS

    def _wait_iroi_cutscene(self):
        """等伊洛伊 Q 大招动画结束 (is_in_team 恢复); 环合条像素仅作对照采样."""
        start = time.time()
        last_log = -1.0
        while (
            not self.is_in_team()
            and time.time() - start < self.IROI_FUNNEL_ANIMATION_TIMEOUT
        ):
            elapsed = time.time() - start
            if elapsed - last_log >= 0.2:
                last_log = elapsed
                logger.info(
                    f"iroi cutscene t={elapsed:.2f}s "
                    f"cycle_bar_pixels={self._cycle_bar_white_pixels()}"
                )
            self.sleep(0.05)
        logger.info(
            f"iroi cutscene wait done {time.time() - start:.2f}s "
            f"in_team={bool(self.is_in_team())} "
            f"cycle_bar_pixels={self._cycle_bar_white_pixels()}"
        )

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

    def _raw_ultimate_cd(self):
        """当前角色 Q 冷却的**原始 OCR 数字** (不扣时间); 0 表示没读到数字.

        必须用原始值: `get_cd()` 会减去"自 OCR 快照以来的时间", 所以只要冷却数字还挂在
        屏幕上, 即使游戏里冷却被冻结, `get_cd()` 也会一路变小, 不能用来判断
        "冷却是否真的开始计时"。
        """
        char = self.get_current_char(raise_exception=False)
        if char is None:
            return 0.0
        self.refresh_cd()
        cds = self.cds.get(char.index)
        return cds["ultimate"] if cds else 0.0

    def _wait_double_q_recovery(self, stage):
        """等二连可以长按的时机; 两个条件必须同时成立.

        1. **第 2 段** Q 的动画结束: 必须确认过 `enter2` (stage >= 3), 不能拿第 1 段的结束
           当数; 且 HUD (`is_in_team`) 要稳定回来, 排除特写期间的状态抖动。
        2. Q 冷却**原始数字**真正变小: 游戏在 Q 动画期间把冷却数字冻结在满值,
           所以数字开始变小 = 动画结束、冷却开始计时。

        实测依据: 长按比正确时机早约 2s 时, 2.0s 长按全程落在动画/收招里,
        普攻完全不生效, 金 E 检测 `best conf=0.000`, 二连接不上。
        """
        if stage < 3:
            logger.warning(
                f"zankou double q recovery: enter2 not confirmed (stage={stage}), "
                f"still waiting for in_team + cd"
            )
        start = time.time()
        baseline = None
        in_team_since = None
        last_log = 0.0
        with self.skip_sleep_checks() as skip:
            skip.check_combat = True
            while time.time() - start < self.CONTROLLABLE_TIMEOUT:
                now = time.time()
                if self.is_in_team():
                    if in_team_since is None:
                        in_team_since = now
                else:
                    in_team_since = None
                raw = self._raw_ultimate_cd()
                if raw > 0 and baseline is None:
                    baseline = raw
                if now - last_log >= 1.0:
                    last_log = now
                    logger.info(
                        f"zankou double q recovery t={now - start:.2f}s "
                        f"in_team={bool(self.is_in_team())} cd_raw={raw:.1f} "
                        f"baseline={baseline}"
                    )
                in_team_ok = (
                    in_team_since is not None
                    and now - in_team_since >= self.ANIMATION_STABLE_TIME
                )
                cd_ok = raw > 0 and baseline is not None and raw < baseline - 0.001
                if in_team_ok and cd_ok:
                    logger.info(
                        f"zankou combo ready in {now - start:.2f}s "
                        f"(stage={stage}, cd_raw {baseline:.1f} -> {raw:.1f})"
                    )
                    return True
                self.sleep(self.SCRIPT_TICK)
        self._dump_q_cd_state("combo_ready_timeout")
        logger.warning(
            f"wait zankou combo ready timeout (stage={stage}, "
            f"in_team={bool(self.is_in_team())}, "
            f"cd_raw={self._raw_ultimate_cd():.1f}, baseline={baseline})"
        )
        return False

    def _q_button_lit(self):
        return bool(self.box_highlighted("ultimate"))

    # --------------------------------------------------------- sound react

    def _sound_dodge_action(self):
        """听到攻击警报: 只按闪避; 反击连招改由"闪避成功音"触发."""
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

    def _sound_counter_action(self):
        """听到可反击警报: 只补一次左键; 反击连招改由"闪避成功音"触发."""
        self.click()
        time.sleep(0.02)

    def _sound_dodge_success_action(self):
        """听到"闪避成功音"后的反击.

        残虹: 点按左键 -> 等 DODGE_COUNTER_WAIT(闪避反击动画) -> 残虹二连.
              二连长按期间若又听到攻击警报, 立即打断: 闪避 -> 点按左键 -> 再等 -> 再打二连。
        其他角色: 保持原逻辑(连点左键+连点切人键), 随后主循环切残虹打二连。
        """
        self._dodge_success_heard.set()
        self._dodge_heard_at = time.time()
        if self._holding:
            # 二连长按正在进行: 交给长按自己收尾(打断 -> 点左键 -> 等反击动画 -> 重打),
            # 这里再点一次左键会和它抢鼠标, 把长按按废。
            logger.info("dodge success during combo hold, leave the reaction to the hold")
            return
        current = self.get_current_char(raise_exception=False)
        with self.skip_sleep_checks() as skip:
            skip.all = True
            if current is not self.zankou:
                self._sound_immediate_reaction()
                return
            self._set_action_phase("sound_success")
            while True:
                self._alert_interrupt.clear()
                self.click(down_time=self.SOUND_SUCCESS_CLICK_DOWN)
                time.sleep(self.DODGE_COUNTER_WAIT)
                if not self._zankou_combo_interruptible():
                    return
                logger.info("sound success combo interrupted, dodge then retry")
                self._set_action_phase("sound_success_interrupt")
                self._sound_dodge_action()

    def on_sound_alert(self):
        """攻击警报回调(在声音监听线程上): 只置标志, 供连招中途被打断."""
        self._alert_interrupt.set()

    def on_dodge_motion_sound(self):
        """闪避动作音回调(在声音监听线程上): 说明闪避真的触发了."""
        self._dodge_heard_at = time.time()
        self._dodge_motion_heard.set()

    def _sound_immediate_reaction(self):
        """触发闪避反击后第一时间连点左键并连点切人键."""
        current = self.get_current_char(raise_exception=False)
        by_zankou = current is self.zankou
        if by_zankou and self._pad_target is not None:
            target = self._pad_target
        else:
            target = self.daffodill if by_zankou else self.zankou
        deadline = time.time() + self.SOUND_IMMEDIATE_SPAM_TIME
        while time.time() < deadline:
            self.send_key(
                target.index + 1, action_name="sound_switch", interval=0.1, down_time=0.05
            )
            self.click()
            time.sleep(0.06)
        self._sound_counter_pending = True

    def _maybe_handle_sound_counter(self):
        if not self._sound_counter_pending or self._in_sound_reaction:
            return
        self._sound_counter_pending = False
        self._in_sound_reaction = True
        try:
            self._switch_to(self.zankou)
            self._zankou_combo()
        except (NotInCombatException, ZankouComboAnomaly):
            raise
        except Exception as e:
            logger.error("sound counter reaction error", e)
        finally:
            self._in_sound_reaction = False

    # ---------------------------------------------------------------- switch

    def _ensure_current(self, char):
        if self.get_current_char(raise_exception=False) is not char:
            self._switch_to(char)

    def _is_adjacent_element(self, char_a, char_b):
        """两个角色的属性在六边形环上是否相邻 (`element_ring` 的邻居, 含首尾环绕).

        游戏机制: **满环合**的角色切到**环上相邻属性**的角色时, 新上场的角色会触发入场技,
        期间无法控制。例如残虹/早雾=咒(Red)、伊洛伊=灵(Green)、达芙蒂尔=暗(Purple),
        咒与灵、暗相邻, 所以满环合的达芙蒂尔/伊洛伊切残虹会触发残虹的入场技。
        """
        index = self.element_ring_index
        a = getattr(char_a, "element", None)
        b = getattr(char_b, "element", None)
        if a is None or b is None or a == b or a not in index or b not in index:
            return False
        delta = abs(index[a] - index[b])
        return delta == 1 or delta == len(self.element_ring) - 1

    def _switch_triggers_entry_skill(self, current, char):
        """上一任满环合 + 属性相邻 -> 新角色会放入场技."""
        full = bool(current.is_cycle_full())
        adjacent = self._is_adjacent_element(current, char)
        if full and adjacent:
            logger.info(
                f"four combo entry skill expected: {current}({current.element}) -> "
                f"{char}({char.element}), cycle_ratio={self.cycle_ratio():.2f}, "
                f"wait {self.ENTRY_SKILL_WAIT}s"
            )
        return full and adjacent

    def _switch_to(self, char):
        current = self.get_current_char(raise_exception=False)
        if current is char:
            return
        entry_skill = current is not None and self._switch_triggers_entry_skill(current, char)
        # 入场技计时从"按下切人键"算起 (手动实测: 切人键 -> 长按 = 1.09/1.12s)
        started = time.time()
        self._wait_in_team(timeout=self.CONTROLLABLE_TIMEOUT)
        self._confirm_switch(char, current)
        self._entry_skill_until = started + self.ENTRY_SKILL_WAIT if entry_skill else 0.0

    def _confirm_switch(self, char, current):
        """等脱离动画后重按切人键, 直到图像确认目标角色上场; 不做额外点击.

        不采信框架 `_switch_to_char` 的 active health change (会误报)。
        """
        start = time.time()
        deadline = start + self.SWITCH_CONFIRM_TIMEOUT
        detection = None
        with self.skip_sleep_checks() as skip:
            skip.check_combat = True
            while time.time() < deadline:
                detection = self._get_current_char_detection(
                    frame=self.frame, char_count=self.team_size
                )
                if detection.accepted and detection.index == char.index:
                    self._set_current_char(current, char, has_intro=False)
                    logger.info(
                        f"four combo switch confirmed -> {char} in {time.time() - start:.2f}s"
                    )
                    return True
                self.send_key(
                    char.index + 1,
                    action_name="four_combo_switch",
                    interval=0.2,
                    down_time=0.05,
                )
                self.sleep(0.05)
        logger.warning(
            f"four combo switch not confirmed, want {char.index}, "
            f"got {detection.index if detection else None} "
            f"(reason={detection.reason if detection else None})"
        )
        return False
