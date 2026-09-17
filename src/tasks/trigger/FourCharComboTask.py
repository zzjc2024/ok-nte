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
    # 切回残虹的数字快捷键(键盘 1 = 1 号位)。达芙恢复登场期间连点普攻的同时连按它,
    # 切人 CD 一好(或解除被控)第一时间切回残虹, 避免魔法数字。
    ZANKOU_SWITCH_KEY = ZANKOU_INDEX + 1

    COMBO_HOLD_MIN = 0.67
    COMBO_HOLD_MAX = 0.9
    COMBO_POLL_INTERVAL = 0.05
    COMBO_RELEASE_GAP = 0.06
    COMBO_CLICK_GAP = 0.05
    # 长按超时(0.9s 无金 E)后走"切达芙蒂尔上场打一轮再切回"的恢复; 超过这个次数的
    # 超时仍无金 E 才算真状态异常停任务(保留 13:07 抓"环合检测坏了"那类真问题的兜底)。
    COMBO_RECOVERY_MAX = 3
    # 恢复时贴人(切达芙蒂尔/切回残虹)确认不了 = 角色被控或切人 CD 没好, 整体重试几次。
    COMBO_RECOVER_SWITCH_RETRIES = 3
    # 声音路径(闪避成功反击)刚打完残虹二连后, 主循环在这段时间内不要再打一套。
    # 实测撞车间隔 0.07~0.14s; 主循环最多再叠 SWITCH_SETTLE_TIME(0.1s) + 入场技预测(1.1s),
    # 所以取 2.5s。残虹二连的自然间隔由 E 冷却决定(~10s), 不会被这个窗口误伤。
    ZANKOU_COMBO_DEDUP_WINDOW = 2.5
    # 残虹二连(松开+单击)之后攻击动作仍持续约 1.5s; 期间切回残虹长按会被动画吃掉
    # (13:07 "切人后 0.13s 长按全落空"同机理)。任何一次新长按开始前必须距上次二连
    # 完成 >= 该值, 由 `_hold_until_gold` 开头显式补足(`_last_combo_finished_at`),
    # 不依赖"达芙 Q 施放刚好够长"这类各路径的时序巧合。
    ZANKOU_COMBO_LINGER_TIME = 1.5
    # 双 Q 二连的提前长按: 第 2 段动画一结束就开始按住(此时 CD 可能还凝固), 金 E 要等
    # 控制恢复后蓄力 0.67~0.9s 才出现, 实测 stage=3 -> CD 跳动 ≈ 3.08s, 上限按此延长。
    ZANKOU_DOUBLE_Q_HOLD_EXTRA = 3.0
    HEALTH_DROP_RATIO = 0.02
    HEALTH_DROP_MIN_PIXELS = 4
    # 金 E(真金) vs 白/蓄力: 录屏逐帧证据(logs/证据.mp4) —— 金 0.71~0.88, 白/蓄力 0.42~0.61,
    # 紫 <=0.41, 无图标 <=0.29。0.65 能干净区分"金"和"白"; 0.45 会把白/蓄力也算成金(已踩过)。
    GOLD_THRESHOLD = 0.65
    # 闪避反击后的长按: 等"第二次金 E"(金 -> 白/蓄力 -> 金)。录屏实测: 第一次金 ~0.5s,
    # 白/蓄力 ~0.6s, 之后第二次金; 手动松手在 1.10~1.80s(4 次录制: 1.372/1.804/1.239/1.235)。
    # 上限只作兜底, 必须盖住最长的 1.80s, 否则会在蓄力中间收手; 到点照常收手(不算异常)。
    DODGE_COMBO_HOLD = 1.9
    # 白/蓄力要持续这么久才算"第一次金 E 真的没了"(排除闪避命中的瞬时闪白, 实测只有 0.05s)
    SECOND_GOLD_CHARGE_MIN = 0.2
    DAFFODILL_FIELD_TIME = 1.5
    PAD_FIELD_TIME = 1.5
    IROI_FUNNEL_POST_SLEEP = 0.3
    Q_READY_TIMEOUT = 5.0
    Q_WAIT_ATTACK_INTERVAL = 0.2
    Q_REGISTER_TIMEOUT = 3.0
    Q_DOUBLE_TIMEOUT = 8.0
    Q_PRESS_INTERVAL = 0.12
    # 残虹入场技(连携登场技)实测时长: 切人键按下 -> 可以长按 = 1.09/1.12s。
    # 入场技时长因角色而异, 此值只用于残虹, 不要挪给其他角色的等待。
    ZANKOU_ENTRY_SKILL_WAIT = 1.1
    # 按下 Q 后等大招特写开始(is_in_team 变 False)的窗口, 与入场技时长无关。
    Q_CUTSCENE_START_WAIT = 1.1
    SWITCH_SETTLE_TIME = 0.1
    SUPPRESS_SWITCH_CLICK = True
    SWITCH_CONFIRM_TIMEOUT = 3.0
    # 切人确认要求目标高亮连续稳定这么久(过滤切换动画里的瞬态高亮)
    SWITCH_CONFIRM_STABLE = 0.15
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
    SOUND_SUCCESS_CLICK_DOWN = 0.08
    # 闪避反击动画时长: 点完左键要等它放完, 期间长按普攻不生效(会不出金 E 而被误判异常)。
    # 实测(2026-09-16, 7 次)反击左键 -> 开始长按 = 0.12~0.25s, 取 0.2s。
    DODGE_COUNTER_WAIT = 0.2
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
        # 声音路径(闪避成功反击)最近一次打完残虹二连的时刻: 主循环知道"刚打过, 跳过",
        # 否则主循环会紧接着再打一套, 二次长按只能看到上一套残留的金 E -> NO_GOLD ->
        # 走掉血/闪避重试 -> 角色正被连击闪不出来 -> 抛异常停任务(2026-09-16 21:14)。
        self._last_zankou_combo_at = 0.0
        # 最近一次残虹二连(松开+单击)完成的时刻: `_hold_until_gold` 开头用它补足
        # ZANKOU_COMBO_LINGER_TIME(1.5s)的攻击动作残留期, 期间开始新长按必被吃掉。
        self._last_combo_finished_at = 0.0
        self._alert_interrupt = threading.Event()
        self._dodge_success_heard = threading.Event()
        # 最近一次听到闪避(动作音/成功音)的时刻, 声音线程写, 主线程读:
        # 长按期间它变大 = 这次长按被闪避打断了, 不能按"没掉血"抛异常。
        self._dodge_heard_at = 0.0
        # 打断长按的那次闪避是不是完美闪避(听到成功音) -> 决定之后等第一次还是第二次金 E
        self._last_dodge_was_perfect = False
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
        self._entry_skill_until = 0.0
        self._action_phase = ""
        self._suppress_combat_check = False
        self._alert_interrupt.clear()
        self._dodge_success_heard.clear()
        self._dodge_heard_at = 0.0
        self._holding = False
        self._opener_lost_since = 0.0
        self._q_wait_attack_at = 0.0
        self._last_zankou_combo_at = 0.0
        self._last_combo_finished_at = 0.0

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
        绝不主动 mouse_down / click / 切人。不做 get_current_char 前置门:
        金 E 模板是残虹专属, 匹配到本身就说明她在场, 多一次检测只会拖慢响应
        (触发周期 0.1s, 每个周期里入战检测已经够重了)。
        """
        if self._opener_gold_e_done:
            self._precombat_daffodill_q()
            return
        if len(self.chars) < 4 and not self.load_chars():
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

        切人一律走 `_switch_confirmed`(整体重试 + 图像复查): 入场技动画期间当前角色
        检测可能整段失明(2026-09-17 18:00 实机: 切早雾已生效, 检测却连续 3s 报
        index=1), 单次 `_switch_to` 失败就裸继续会让后面每一步都在错的角色上空转。
        任一关键切人重试后仍失败 -> 落盘现场 + 停任务 + 抛异常:
        **开场流程不允许跳步**(用户明确要求), 宁可停下来人工看。
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

            if not aborted and not self._switch_confirmed(self.daffodill):
                self._raise_combo_anomaly("opener switch to daffodill failed after retries")
            if not aborted:
                if not self._precombat_daffodill_q_done:
                    self._cast_q(self.daffodill)
                aborted = self._opener_combat_lost("daffodill")

            if not aborted and not self._switch_confirmed(self.iroi):
                self._raise_combo_anomaly("opener switch to iroi failed after retries")
            if not aborted:
                self._skill_until_registered(self.iroi)
                aborted = self._opener_combat_lost("iroi")

            if not aborted and not self._switch_confirmed(self.sakiri):
                self._raise_combo_anomaly("opener switch to sakiri failed after retries")
            if not aborted:
                # 早雾只放 Q 不放 E(2026-09-17 改): 放完 Q 直接切残虹衔接双 Q
                self._cast_q(self.sakiri)
                aborted = self._opener_combat_lost("sakiri")

            if not aborted and not self._switch_confirmed(self.zankou):
                self._raise_combo_anomaly("opener switch to zankou failed after retries")
            if not aborted:
                self._zankou_double_q()
                self._zankou_combo(max_hold_extra=self.ZANKOU_DOUBLE_Q_HOLD_EXTRA)
                aborted = self._opener_combat_lost("zankou_double_q")

            if not aborted and not self._switch_confirmed(self.iroi):
                self._raise_combo_anomaly("opener switch to iroi (funnel) failed after retries")
            if not aborted:
                self._iroi_q_funnel()
                aborted = self._opener_combat_lost("iroi_funnel")

            if not aborted:
                target = self._zankou_combo_switch(self.daffodill)

        if aborted:
            logger.warning(
                "four char combo opener aborted (combat ended), reset precombat memory"
            )
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
        max_hold_extra = 0.0
        if q_available:
            self._zankou_double_q()
            max_hold_extra = self.ZANKOU_DOUBLE_Q_HOLD_EXTRA
        elif 0 < q_remaining < self.ZANKOU_Q_READY_WINDOW:
            self._stay_until_q_ready(zankou)
            self._zankou_double_q()
            max_hold_extra = self.ZANKOU_DOUBLE_Q_HOLD_EXTRA
        return self._zankou_combo_switch(self.daffodill, max_hold_extra=max_hold_extra)

    def _daffodill_until_cycle_full(self):
        daffodill = self.daffodill
        while self.in_combat():
            self._maybe_log_combat_state("daffodill_loop")
            self._switch_to(daffodill)
            self._daffodill_window(daffodill)
            if self._zankou_combo_switch(daffodill) is self.iroi:
                return

    def _daffodill_window(self, daffodill):
        """达芙蒂尔在场窗口.

        Q **进场就放**(2026-09-17 改): 她是从"残虹二连"刚切过来的, 正好符合
        "先打一套二连 -> 马上切该角色放 Q"(Q 长动画期间白赚二连的 1.5s 残留输出),
        不能等在场打了一阵才放。没有 Q 才放 E, 然后普攻到 DAFFODILL_FIELD_TIME;
        场上期间 Q 转好也立即放(放完同样马上切残虹)。
        """
        self._set_action_phase("daffodill_window")
        start = time.time()
        if daffodill.ultimate_available():
            self._cast_q(daffodill)
            return
        if daffodill.skill_available():
            self._skill_until_registered(daffodill, self.DAFFODILL_SKILL_REGISTER_TIMEOUT)
        while self.in_combat():
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
        大招/特写期间按键不生效(实测早雾放完 Q 后 `in_team=False`, E 连按 2s 全废),
        所以先等脱离特写、可控了再按 E。
        """
        if timeout is None:
            timeout = self.SKILL_REGISTER_TIMEOUT
        if char.has_cd("skill"):
            return True
        self._wait_entry_skill_if_any()
        if not self.is_in_team():
            self._wait_in_team(timeout=self.CONTROLLABLE_TIMEOUT)
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
                if self.is_in_team() and char.skill_available():
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
        """在 target 身上打 PAD_FIELD_TIME 秒 -> 切残虹二连 -> 切回, 直到 target Q 可放."""
        self._set_action_phase("pad_until_q")
        while self.in_combat() and not target.ultimate_available():
            start = time.time()
            while (
                self.in_combat()
                and not target.ultimate_available()
                and time.time() - start < self.PAD_FIELD_TIME
            ):
                self.click()
                self.sleep(0.1)
            if not self.in_combat() or target.ultimate_available():
                break
            if self._zankou_combo_switch(target) is not target:
                return False
        return self.in_combat() and target.ultimate_available()

    # ------------------------------------------------------------- zankou

    def _zankou_gold_e(self):
        """开局金 E: 长按轮询金E -> 点 E.

        min_hold=0: 开场金 E 由玩家手动长按蓄力, 脚本开始长按时金 E 可能早已亮起 ——
        不能套用 COMBO_HOLD_MIN(它忽略"长按开始时就已存在的金 E", 防的是上一套
        二连的残留金 E, 21:14 撞车), 否则玩家看到金 E 后还要白等 0.67s+ 才释放
        (2026-09-17 18:29 实机: 金 E 38.4s 已亮, 39.287 才接受)。开场时本场还没有
        打过任何二连, 不存在残留金 E, 立即接受是安全的。
        """
        self._set_action_phase("zankou_gold_e")
        if self._zankou_hold_with_recovery(min_hold=0.0) is HoldResult.HANDLED:
            return
        self._skill_until_registered(self.zankou)

    def _zankou_combo(self, max_hold_extra=0.0):
        """残虹二连: 长按轮询金E -> 松开 -> 单击左键 (间隔见 COMBO_* 常量).

        `max_hold_extra`: 双 Q 后提前长按时延长上限(动画尾 + CD 解冻前蓄力不开始)。
        """
        if self._zankou_combo_recently_done():
            logger.info(
                "zankou combo skipped: sound path already ran it "
                f"({time.time() - self._last_zankou_combo_at:.2f}s ago)"
            )
            return
        self._set_action_phase("zankou_combo")
        if (
            self._zankou_hold_with_recovery(max_hold_extra=max_hold_extra)
            is HoldResult.HANDLED
        ):
            return
        self.sleep(self.COMBO_RELEASE_GAP)
        self.click()
        self.sleep(self.COMBO_CLICK_GAP)
        self._last_combo_finished_at = time.time()

    def _zankou_combo_recently_done(self):
        """声音路径刚打完一套残虹二连(闪避成功反击) -> 主循环不要再打一遍.

        撞车现象: 主循环二次长按只能看到上一套残留的金 E, 而它落在
        `COMBO_HOLD_MIN` 之前被有意忽略 -> 报 NO_GOLD -> 走掉血/闪避重试 ->
        角色正被连击闪不出来 -> 抛异常停任务(2026-09-16 21:14)。
        只检查时间戳, 不额外看 E 冷却: 时间戳只在"确实点出了那一套"时才写。
        """
        elapsed = time.time() - self._last_zankou_combo_at
        return 0.0 <= elapsed < self.ZANKOU_COMBO_DEDUP_WINDOW

    def _zankou_combo_interruptible(self, require_second_gold=False):
        """残虹二连(长按期间可被新攻击警报打断); 返回 True 表示被打断.

        `require_second_gold=True` 时等的是"闪避攻击之后的第二次金 E"(见 §4.1 机制)。
        """
        self._set_action_phase("zankou_combo")
        result = self._zankou_hold_with_recovery(
            self._alert_interrupt, require_second_gold=require_second_gold
        )
        if result is HoldResult.INTERRUPTED:
            return True
        if result is HoldResult.HANDLED:
            return False
        self.sleep(self.COMBO_RELEASE_GAP)
        self.click()
        self.sleep(self.COMBO_CLICK_GAP)
        self._last_combo_finished_at = time.time()
        return False

    def _zankou_hold_with_recovery(
        self, interrupt_event=None, require_second_gold=False, min_hold=None,
        max_hold_extra=0.0,
    ):
        """长按轮询金 E; 一次超时就切达芙蒂尔上场打一轮, 再切回来重打.

        **不重按**(长按起点不再依赖入场技计时: 预测到入场技时直接按住左键穿过它,
        控制恢复后蓄力立刻开始, 上限按剩余入场技时间延长), 也**不再用"普通闪避重试"**
        (连按 shift + 动作音确认): 它的收益只是确认角色能动, 负担却是整套闪避状态机
        (2026-09-17 16:46 实机撞车: 成功反击在 `_dodge_until_triggered` 的 sleep 里重入
        执行 6s, 吃光 3s 重试窗口 -> 误报"闪避没确认"异常停任务)。现在:
        - 掉血与否都**不打断长按**, 血条只采样记日志;
        - 长按期间闪避触发 = 普攻被闪避动画打断, 不是异常: 点左键触发闪避反击 ->
          等反击动画 -> 重打长按(完美闪避之后等第二次金 E);
        - 长按**超时**(0.9s 无金 E, 掉不掉血都一样) -> `_recover_on_daffodill`:
          切达芙蒂尔上场打一轮, 切人 CD 一好(确认重试自动等)马上切回残虹重打;
          贴人确认不了 = 角色正被控, 整体重试几次;
        - 超时次数超过 COMBO_RECOVERY_MAX 才算真状态异常 -> 抛异常停任务
          (保留 13:07 抓"环合检测坏了"的兜底, 代价变成约 3 轮恢复时间)。
        """
        second_gold = require_second_gold
        timeouts = 0
        while True:
            result, _damaged = self._hold_until_gold(
                interrupt_event=interrupt_event,
                require_second_gold=second_gold,
                min_hold=min_hold,
                max_hold_extra=max_hold_extra,
            )
            if result is HoldResult.DODGE:
                if self._recover_from_dodge():
                    return HoldResult.HANDLED
                # "第二次金 E"是完美闪避(闪避攻击 -> 蓄力)特有的; 普通闪避之后
                # 等的是第一次金 E, 否则会白等一个蓄力周期再撞 1.9s 兜底。
                second_gold = self._last_dodge_was_perfect
                continue
            if result is not HoldResult.NO_GOLD:
                return result
            timeouts += 1
            if timeouts > self.COMBO_RECOVERY_MAX:
                self._raise_combo_anomaly(
                    f"zankou gold E still missing after "
                    f"{self.COMBO_RECOVERY_MAX} daffodill recoveries"
                )
            logger.warning(
                f"zankou gold E timeout ({timeouts}/{self.COMBO_RECOVERY_MAX}), "
                f"recover on daffodill then retry"
            )
            self._recover_on_daffodill()
            # 切走又切回来, 闪避攻击/蓄力的上下文已经不在了, 回到等第一次金 E。
            second_gold = False

    def _recover_on_daffodill(self):
        """长按超时后的恢复: 切达芙蒂尔短暂上场 -> 第一时间切回残虹重打二连.

        **不复用 `_daffodill_window`**(它至少待满 1.5s): 掉血被打断后达芙只需要把
        受击/被控的窗口熬过去, 应该在切人 CD 一好(或解除被控)的**第一时间**切回,
        实测她在场上不到 1s。她登场期间: 连点普攻占住输出 + 连按切回残虹的快捷键
        (`_confirm_switch(attack_while_waiting=True)`, 被控期间按键不生效由确认
        重试覆盖, 整体重试交给 `_switch_confirmed`)。

        达芙 Q 的时停用法: 她本次登场已经普攻过(等待切回期间连点普攻, 必然成立)
        且 Q 就绪时, **不在场上直接放**, 而是先切回残虹打一套二连, 再切回达芙放 Q
        —— 任何角色放 Q 期间关卡计时暂停而游戏继续, 相当于白赚这套二连。
        放完 Q 再切回残虹, 交回上层重打长按。
        """
        self._set_action_phase("combo_recover_daffodill")
        if not self._switch_confirmed(self.daffodill):
            logger.warning("recover switch to daffodill failed, retry the combo directly")
            return False
        daffodill_q_ready = self.daffodill.ultimate_available()
        if not self._switch_confirmed(self.zankou, attack_while_waiting=True):
            logger.warning("recover switch back to zankou failed, retry the combo directly")
            return False
        self.sleep(self.SWITCH_SETTLE_TIME)
        self._zankou_combo()
        if not daffodill_q_ready:
            return True
        logger.info(
            "daffodill ultimate ready: take the zankou combo first, then her Q "
            "(stage timer pauses during ult)"
        )
        if self._switch_confirmed(self.daffodill):
            self._cast_q(self.daffodill)
            self._switch_confirmed(self.zankou)
            self.sleep(self.SWITCH_SETTLE_TIME)
        return True

    def _switch_confirmed(self, char, retries=None, attack_while_waiting=False):
        """切人 + 图像复查; 确认不了(被控/切人 CD/高亮误报)就整体重试, 都失败返回 False.

        `attack_while_waiting=True` 时等待期间连点普攻(达芙恢复切回残虹用)。
        """
        if retries is None:
            retries = self.COMBO_RECOVER_SWITCH_RETRIES
        for attempt in range(1, retries + 1):
            self._switch_to(char, attack_while_waiting=attack_while_waiting)
            if self._verify_current(char):
                return True
            logger.warning(
                f"switch to {char} not confirmed ({attempt}/{retries}), retry"
            )
            self.sleep(0.2)
        return False

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

    def _zankou_combo_switch(self, default_target, max_hold_extra=0.0):
        """切残虹二连, 再按环合值决定去向.

        环合 >= CYCLE_STAY_RATIO 时不再和达芙蒂尔互切: 留在残虹身上连点左键直到环合满,
        然后切伊洛伊; 否则切 default_target。`max_hold_extra` 透传给二连长按
        (双 Q 后的提前长按延长上限用)。
        """
        self._set_action_phase("zankou_enter")
        self._switch_to(self.zankou)
        # 切人刚确认时角色还在切人动画里, 立刻长按普攻不生效 (实测确认后 0.001s 就长按, 金 E 全空)
        self.sleep(self.SWITCH_SETTLE_TIME)
        self._zankou_combo(max_hold_extra=max_hold_extra)
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
            self.next_frame()
            self.click()
            self.sleep(0.1)
        logger.info(f"four char combo cycle full ratio={self.cycle_ratio():.2f}")

    def _hold_until_gold(
        self, interrupt_event=None, require_second_gold=False, min_hold=None,
        max_hold_extra=0.0,
    ):
        """长按轮询金 E; 返回 (HoldResult, damaged).

        damaged 由长按期间的**多次**血条采样得出 (伊洛伊大招会在后台回血,
        只取首尾两次会出现"掉血-回血-采样"而看不到掉血)。

        `require_second_gold=True`(闪避反击后)等的是**第二次**金 E:
          闪避攻击带来的第一次金 E 不能用 -> 长按后闪避攻击动作结束会自动蓄力,
          蓄力一开始金 E 就消失 -> 蓄力约 0.7s 完成后再变金, 这一次点按才能稳定
          打出脱手攻击。所以这里按 gold -> not gold -> gold 三段等。
        """
        interrupted = False
        dodged = False
        damaged = False
        health_peak = None
        health_samples = 0
        # 残虹二连残留期(ZANKOU_COMBO_LINGER_TIME)内不允许开始新长按, 先显式补足,
        # 不依赖各路径(切人/达芙 Q 施放等)刚好消耗掉这 1.5s。
        linger_wait = self.ZANKOU_COMBO_LINGER_TIME - (
            time.time() - self._last_combo_finished_at
        )
        if linger_wait > 0:
            logger.info(f"zankou wait combo linger {linger_wait:.2f}s before hold")
            self.sleep(linger_wait)
        # 入场技不再前置等待: 直接按住左键穿过入场技(期间按键不生效但按住状态保留),
        # 控制一恢复蓄力立刻开始, 不用赌 1.1s 计时准不准。金 E 出现时间 =
        # 入场技结束 + 0.67~0.9s, 所以下限保持 COMBO_HOLD_MIN、上限按剩余入场技延长。
        entry_wait = max(0.0, self._entry_skill_until - time.time())
        if entry_wait > 0:
            logger.info(f"zankou hold through entry skill ({entry_wait:.2f}s left)")
        self._entry_skill_until = 0.0
        hold_start = time.time()
        self._holding = True
        self.mouse_down()
        gold = False
        best_conf = 0.0
        phase = 0  # 0=等第一次金 E, 1=等金 E 消失(蓄力开始), 2=等第二次金 E
        lost_since = 0.0
        try:
            with self.skip_sleep_checks() as skip:
                skip.check_combat = True
                start = time.time()
                min_until = start + (
                    0.0
                    if require_second_gold
                    else (self.COMBO_HOLD_MIN if min_hold is None else min_hold)
                )
                max_until = start + (
                    self.DODGE_COMBO_HOLD if require_second_gold else self.COMBO_HOLD_MAX
                ) + entry_wait + max_hold_extra
                while time.time() < max_until:
                    if interrupt_event is not None and interrupt_event.is_set():
                        interrupted = True
                        break
                    # 长按期间闪避触发(动作音/成功音) -> 普攻被打断, 不是异常
                    if self._dodge_heard_at > hold_start:
                        dodged = True
                        # 成功音 = 完美闪避(有闪避攻击/蓄力, 要等第二次金 E);
                        # 只有动作音 = 普通闪避(等第一次金 E 就行)。
                        self._last_dodge_was_perfect = self._dodge_success_heard.is_set()
                        break
                    box = self.find_one(Labels.zankou_skill_gold, threshold=0.0)
                    conf = box.confidence if box else 0.0
                    if conf > best_conf:
                        best_conf = conf
                    if require_second_gold:
                        if phase == 0 and conf >= self.GOLD_THRESHOLD:
                            phase = 1
                            logger.info(
                                f"second gold: first gold E seen (dodge attack), conf={conf:.3f}"
                            )
                        elif phase == 1:
                            if conf < self.GOLD_THRESHOLD:
                                if lost_since == 0.0:
                                    lost_since = time.time()
                                elif time.time() - lost_since >= self.SECOND_GOLD_CHARGE_MIN:
                                    phase = 2
                                    logger.info(
                                        f"second gold: gold E gone for "
                                        f"{time.time() - lost_since:.2f}s "
                                        f"(charge/white), conf={conf:.3f}"
                                    )
                            else:
                                lost_since = 0.0
                        elif phase == 2 and conf >= self.GOLD_THRESHOLD:
                            gold = True
                            logger.info(f"second gold: second gold E ready, conf={conf:.3f}")
                            break
                    elif time.time() >= min_until and conf >= self.GOLD_THRESHOLD:
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
        if require_second_gold:
            # 实测蓄力期间 E 模板一直是金, 看不到"变白再变金": 到点就按用户手动的方式收手点按
            logger.info(
                f"dodge combo hold deadline reached ({self.DODGE_COMBO_HOLD}s), "
                f"proceed with the follow-up attack (best conf={best_conf:.3f}, phase={phase})"
            )
            return HoldResult.GOLD, damaged
        logger.warning(
            f"zankou gold E not detected, best conf={best_conf:.3f} "
            f"(hold={self.COMBO_HOLD_MAX}s, second_gold={require_second_gold}, "
            f"phase={phase}, health_samples={health_samples}, "
            f"health_peak={health_peak}, damaged={damaged})"
        )
        if health_samples == 0:
            # 一帧血条都没取到, 只影响日志定位; 掉血与否不再改变恢复路径
            logger.warning("zankou health not sampled during hold")
        return HoldResult.NO_GOLD, damaged

    def _zankou_double_q(self):
        """残虹双 Q.

        连按 Q, 依次确认四个阶段: 第1段进特写 -> 第1段出特写 -> 第2段进特写 -> 第2段出特写。
        第 2 段动画结束(in_team 稳定回来)就可以**提前长按**, 不再等大招 CD 解冻——
        此时按住左键, 控制恢复后蓄力自动开始, 金 E 出现再松手点按完成二连
        (与入场技衔接二连同一套逻辑)。紧随其后的 `_zankou_combo` 必须带
        `max_hold_extra=ZANKOU_DOUBLE_Q_HOLD_EXTRA`(调用方负责), 覆盖"动画尾 +
        CD 解冻前蓄力不开始"的全程。
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
        self._wait_in_team(timeout=self.Q_CUTSCENE_START_WAIT)
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
            self.click()
            self.sleep(0.1)

    # ------------------------------------------------------------------ q

    def _wait_entry_skill_if_any(self):
        """刚切人的角色若预测到入场技(`_entry_skill_until`), 先等它结束再按键.

        入场技期间角色不可控、按键不生效, 且 ult_ready/skill 图标模板 conf 会掉到
        0.0x(2026-09-17 18:00 实机: 切早雾后立即轮询她的 Q, `conf=0.042` 挂满
        Q_READY_TIMEOUT=5s, 早雾全程发呆)。等待后清零, 每次入场技只消费一次。
        """
        wait = self._entry_skill_until - time.time()
        if wait > 0:
            logger.info(f"wait entry skill {wait:.2f}s before acting")
            self.sleep(wait)
        self._entry_skill_until = 0.0

    def _cast_q(self, char):
        self._wait_entry_skill_if_any()
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
        """可控信号: Q 冷却**原始数字**开始变小; 没有冷却数字时才退回"Q 按钮由亮变灭".

        必须用原始 OCR 数字: `get_cd()` 会减去"自快照以来的时间", 数字挂在屏幕上就一直变小,
        判断不出游戏里冷却是否真的开始跳(见 `_raw_ultimate_cd`)。
        **图标由亮变灭不能当主判据**: 实测大招一按下去图标就变灭, 但此时还在大招特写里
        (早雾放完 Q 后 `in_team=False`, E 连按 2s 全废), 所以只在读不到冷却数字时才用它兜底。
        """
        start = time.time()
        previous = None
        with self.skip_sleep_checks() as skip:
            skip.check_combat = True
            while time.time() - start < self.CONTROLLABLE_TIMEOUT:
                remaining = self._raw_ultimate_cd()
                if remaining > 0:
                    if previous is not None and remaining < previous - 0.001:
                        return True
                    previous = remaining
                elif was_lit and not self._q_button_lit():
                    return True
                self.sleep(self.SCRIPT_TICK)
        logger.warning(
            f"wait controllable timeout {char} "
            f"(raw cd={self._raw_ultimate_cd():.2f}, in_team={bool(self.is_in_team())})"
        )
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
        """等"第 2 段 Q 动画结束"就返回, 不再等大招 CD 解冻.

        1. **第 2 段** Q 的动画结束: 必须确认过 `enter2` (stage >= 3), 不能拿第 1 段的结束
           当数; 且 HUD (`is_in_team`) 要稳定回来, 排除特写期间的状态抖动。
        2. ~~Q 冷却原始数字变小~~ **已删(2026-09-17 晚)**: 二连改为提前长按——动画一结束
           就按住左键, 此时 CD 可能还凝固不变, 蓄力在控制恢复后自动开始, 金 E 出现再
           松手点按。长按上限按 `ZANKOU_DOUBLE_Q_HOLD_EXTRA` 延长, 覆盖"动画尾 +
           蓄力"的全程, 不用赌 CD 解冻时刻(实测 stage=3 -> cd 跳动 ≈ 3.08s)。
        """
        if stage < 3:
            logger.warning(
                f"zankou double q recovery: enter2 not confirmed (stage={stage}), "
                f"still waiting for in_team"
            )
        start = time.time()
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
                if now - last_log >= 1.0:
                    last_log = now
                    logger.info(
                        f"zankou double q recovery t={now - start:.2f}s "
                        f"in_team={bool(self.is_in_team())}"
                    )
                if (
                    in_team_since is not None
                    and now - in_team_since >= self.ANIMATION_STABLE_TIME
                ):
                    logger.info(
                        f"zankou double q animation done in {now - start:.2f}s "
                        f"(stage={stage}), start the hold early and wait for gold"
                    )
                    return True
                self.sleep(self.SCRIPT_TICK)
        self._dump_q_cd_state("combo_ready_timeout")
        logger.warning(
            f"wait zankou double q animation timeout (stage={stage}, "
            f"in_team={bool(self.is_in_team())})"
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
        其他角色: 只点按左键触发闪避反击, 然后直接返回 —— 声音动作是在主线程的
              sleep 里跑的, 返回后被打断的代码(放 E / 放 Q / 切人)会自然接着执行,
              不强行切人, 也不接管主循环的二连。
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
                # 只点这一下就返回: E/Q 都能打断闪避反击, 没必要等反击动画放完,
                # 被打断的代码(放 E / 放 Q / 切人)会立刻接着跑。
                self._set_action_phase("sound_success_other")
                self.click(down_time=self.SOUND_SUCCESS_CLICK_DOWN)
                return
            self._set_action_phase("sound_success")
            while True:
                self._alert_interrupt.clear()
                self.click(down_time=self.SOUND_SUCCESS_CLICK_DOWN)
                time.sleep(self.DODGE_COUNTER_WAIT)
                if not self._zankou_combo_interruptible(require_second_gold=True):
                    # 这一套(含点按左键)确实打出去了 -> 告诉主循环不要再打一遍
                    self._last_zankou_combo_at = time.time()
                    return
                logger.info("sound success combo interrupted, dodge then retry")
                self._set_action_phase("sound_success_interrupt")
                self._sound_dodge_action()

    def on_sound_alert(self):
        """攻击警报回调(在声音监听线程上): 只置标志, 供连招中途被打断."""
        self._alert_interrupt.set()

    def on_dodge_motion_sound(self):
        """闪避动作音回调(在声音监听线程上): 说明闪避真的触发了.

        只更新 `_dodge_heard_at`: 长按循环用它识别"这次长按被闪避打断了"。
        """
        self._dodge_heard_at = time.time()

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
                f"wait {self.ZANKOU_ENTRY_SKILL_WAIT}s"
            )
        return full and adjacent

    def _switch_to(self, char, attack_while_waiting=False):
        current = self.get_current_char(raise_exception=False)
        if current is char:
            return
        entry_skill = current is not None and self._switch_triggers_entry_skill(current, char)
        self._wait_in_team(timeout=self.CONTROLLABLE_TIMEOUT)
        pressed_at = self._confirm_switch(char, current, attack_while_waiting=attack_while_waiting)
        # 入场技计时锚点必须是 _confirm_switch 里**第一次按下切人键**的时刻
        # (实测以按键为基准: 切人键 -> 长按 = 1.09/1.12s)。不能用本函数入口时刻:
        # 切人前等特写(_wait_in_team 最长 10s)/确认重试都会把窗口整体推后,
        # 长按会落在入场技中间 -> 金 E 全空 -> 误报状态异常(实机踩过)。
        self._entry_skill_until = (
            pressed_at + self.ZANKOU_ENTRY_SKILL_WAIT if entry_skill and pressed_at else 0.0
        )

    def _verify_current(self, char, samples=2, gap=0.05):
        """关键节点复查当前角色: 重新做图像检测(绕过 sticky tracker), 连续 samples 次命中才算.

        切人高亮会在切换动画里先跳到目标角色上, 但游戏实际可能还留在原角色
        (实测 21:01 早雾 -> 残虹 被判 confirmed, 场上仍是早雾), 所以关键步骤前要复查。
        """
        with self.skip_sleep_checks() as skip:
            skip.check_combat = True
            for _ in range(max(1, samples)):
                detection = self._get_current_char_detection(
                    frame=self.frame, char_count=self.team_size
                )
                if not (detection.accepted and detection.index == char.index):
                    return False
                self.sleep(gap)
        return True

    def _confirm_switch(self, char, current, attack_while_waiting=False):
        """等脱离动画后重按切人键, 直到图像确认目标角色上场.

        不采信框架 `_switch_to_char` 的 active health change (会误报)。
        要求目标高亮**连续稳定 SWITCH_CONFIRM_STABLE 秒**才算确认, 过滤切换动画里的瞬态高亮。

        `attack_while_waiting=True` 时等待期间连点普攻(达芙恢复切回残虹用:
        她在场上的每一刻都在输出, 同时连按切回键, 切人 CD 一好立即切回)。

        返回**第一次按下切人键**的时刻(入场技计时锚点); 一次都没按或未确认返回 0.0。
        游戏接受的是第一次有效按压, 后续重按是空按, 所以锚点取首按。
        """
        start = time.time()
        deadline = start + self.SWITCH_CONFIRM_TIMEOUT
        detection = None
        stable_since = 0.0
        first_press_at = 0.0
        with self.skip_sleep_checks() as skip:
            skip.check_combat = True
            while time.time() < deadline:
                detection = self._get_current_char_detection(
                    frame=self.frame, char_count=self.team_size
                )
                if detection.accepted and detection.index == char.index:
                    if stable_since == 0.0:
                        stable_since = time.time()
                    if time.time() - stable_since >= self.SWITCH_CONFIRM_STABLE:
                        self._set_current_char(current, char, has_intro=False)
                        logger.info(
                            f"four combo switch confirmed -> {char} in {time.time() - start:.2f}s"
                        )
                        return first_press_at
                else:
                    stable_since = 0.0
                if first_press_at == 0.0:
                    first_press_at = time.time()
                if attack_while_waiting:
                    self.click()
                self.send_key(
                    self._switch_key(char),
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
        return 0.0

    def _switch_key(self, char):
        """切人数字快捷键: 键盘 1~4 对应 1~4 号位 (残虹 = `ZANKOU_SWITCH_KEY`)."""
        if char is self.zankou:
            return self.ZANKOU_SWITCH_KEY
        return char.index + 1
