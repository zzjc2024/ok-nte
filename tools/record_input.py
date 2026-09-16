"""记录真实键鼠操作的小工具(GUI).

用法:
    1. 双击 tools/record_input.cmd 以管理员权限启动.
    2. 点窗口按钮开始/停止记录; 也可用 F10 开始 / F12 停止 (Ctrl+Shift+H/J 可能被输入法占用).
    3. 记录期间实时显示事件; 停止时写入 logs/input_record_YYYYmmdd_HHMMSS.log.

同时后台监听游戏音频, 检测"闪避成功音"(assets/sounds/dodge_success.wav),
命中时在事件流里插入一条 `sound dodge_success score=...`, 便于测量闪避成功后
隔多久才长按/二连。

并轮询残虹 E 图标状态(assets/coco_annotations.json 里的 zankou_skill_gold /
zankou_skill_purple, 与 app 同一套模板和阈值), 状态变化时插入一条
`skill E purple -> gold (gold=0.831 purple=0.102)`, 便于测量"长按多久才变金 E"
以及"金 E 什么时候变回去"。

只监听本机输入事件, 不注入、不读取游戏内存; 音频走 WASAPI 进程回环, 图像走
屏幕截图模板匹配, 都不做进程注入.
"""

import os
import sys
import threading
import time
import tkinter as tk
from datetime import datetime

from pynput import keyboard, mouse

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, "logs")
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

DODGE_SUCCESS_SAMPLE = os.path.join(BASE_DIR, "assets", "sounds", "dodge_success.wav")
# Mirrors the app default for config option "Dodge Success Threshold".
DODGE_SUCCESS_THRESHOLD = 0.3
DEFAULT_GAME_EXE = "HTGame.exe"

# 残虹 E 图标的三个状态(与 app 的 Labels / assets/coco_annotations.json 一致):
#   white  = 初始白色(没有对应模板, 用"两个模板都没过阈值"表示)
#   gold   = 条件触发的金 E   -> 模板 zankou_skill_gold
#   purple = 金 E 放完后残虹没离场 -> 模板 zankou_skill_purple
#            (紫色 E 太弱, 实际不用, 只用来避免把紫色误报成白色)
SKILL_COCO_JSON = os.path.join(BASE_DIR, "assets", "coco_annotations.json")
SKILL_FEATURES = ("zankou_skill_gold", "zankou_skill_purple")
# Mirrors the app template_matching default_threshold / FourCharComboTask.GOLD_THRESHOLD.
SKILL_THRESHOLD = 0.7
SKILL_POLL_INTERVAL = 0.05
SKILL_HEARTBEAT_INTERVAL = 5.0
# 日志里用的短状态名, 便于阅读(原始模板名会一起打出来)
SKILL_STATE_LABELS = {
    "zankou_skill_gold": "gold",
    "zankou_skill_purple": "purple",
}
SKILL_STATE_WHITE = "white"
SKILL_STATE_NONE = "none"
# 诊断用: 模板框内"近白像素"占比, 用来分辨"真的白 E"还是"根本没图标".
# 实测(assets/images): 金 E 0.18, 紫 E 0.35, 纯黑空帧 0.00 -> 0.05 有较大余量, 未在游戏内校准.
SKILL_WHITE_PIXEL_MIN = 200
SKILL_WHITE_RATIO_MIN = 0.05

_MODIFIERS = {
    "ctrl", "ctrl_l", "ctrl_r",
    "shift", "shift_l", "shift_r",
    "alt", "alt_l", "alt_r", "alt_gr",
    "cmd", "cmd_l", "cmd_r",
}
_CTRL = {"ctrl", "ctrl_l", "ctrl_r"}
_SHIFT = {"shift", "shift_l", "shift_r"}
MAX_LINES = 500


def _format_key(key):
    char = getattr(key, "char", None)
    if char and char.isprintable():
        return char
    name = getattr(key, "name", None)
    if name:
        return name
    return str(key)


def _format_button(button):
    return str(button).rsplit(".", 1)[-1]


class SoundMonitor:
    """后台监听游戏音频里的"闪避成功音".

    复用 src.sound_trigger.SoundListener 的滤波与匹配逻辑, 只额外暴露实时分数,
    这样录制时能直接看到"现在有没有听到", 而不是盲录。
    """

    def __init__(self, sample_path, threshold, process_name, on_event, on_score=None):
        self.sample_path = sample_path
        self.threshold = threshold
        self.process_name = process_name
        self.on_event = on_event
        self.on_score = on_score
        self.last_score = 0.0
        self.event_count = 0
        self.status = "未启动"
        self._listener = None
        self._started = False

    def start(self):
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._run, name="SoundMonitor", daemon=True).start()

    def stop(self):
        self._started = False
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.stop()
            except Exception as error:
                print(f"[record] sound stop failed: {error}")

    def _handle_score(self, score):
        self.last_score = score
        if self.on_score is not None:
            self.on_score(score)

    def _run(self):
        try:
            from src.sound_trigger.SoundListener import SoundListener
        except Exception as error:
            self.status = f"不可用({error.__class__.__name__})"
            print(f"[record] sound monitor unavailable: {error}")
            return

        if not os.path.exists(self.sample_path):
            self.status = "缺模板"
            print(f"[record] dodge success sample missing: {self.sample_path}")
            return

        monitor = self

        class _ScoreListener(SoundListener):
            def _check_triggers(self, dodge_score, counter_score, dodge_success_score=0.0):
                monitor._handle_score(dodge_score)
                super()._check_triggers(dodge_score, counter_score, dodge_success_score)

        try:
            listener = _ScoreListener(
                sample_path=self.sample_path,
                counter_attack_sample_path="",
                threshold=self.threshold,
                process_name=self.process_name,
            )
        except Exception as error:
            self.status = "加载失败"
            print(f"[record] dodge success sample load failed: {error}")
            return

        listener.on_dodge_triggered = self._on_trigger
        self._listener = listener
        self.status = "监听中"
        print(f"[record] sound monitor listening for {self.process_name} @ {self.threshold}")
        listener.start()

    def _on_trigger(self):
        score = self.last_score
        self.event_count += 1
        print(f"[record] dodge success score={score:.3f}")
        self.on_event(score)


class SkillMonitor:
    """后台轮询残虹 E 图标状态, 状态变化时插一条事件.

    复用 app 的模板资源(assets/coco_annotations.json + ok 的 FeatureSet), 与 app 里
    `find_one(Labels.zankou_skill_gold)` 同一套模板、同一阈值, 所以这里的分数可以直接
    和流程日志里的 conf 对比。

    E 有三个状态: white(初始) -> gold(条件触发) -> purple(金 E 放完且残虹没离场, 实际不用)。
    每次轮询截全屏(物理像素) -> 在模板标注框附近做匹配:
      - 哪个模板分数最高且 >= 阈值 -> 就是那个状态;
      - 两个都没过阈值 -> white(初始状态, 没有对应模板)。
    white 时同时记录模板框内近白像素占比(white_ratio)作为诊断: 占比接近 0 说明那一帧
    残虹不在场/没有图标, 不是真的白 E。
    """

    def __init__(self, coco_json, features, threshold, interval, on_event, on_state=None):
        self.coco_json = coco_json
        self.features = tuple(features)
        self.threshold = threshold
        self.interval = interval
        self.on_event = on_event
        self.on_state = on_state
        self.state = None
        self.scores = {}
        self.white_ratio = 0.0
        self.event_count = 0
        self.status = "未启动"
        self._started = False
        self._thread = None
        self._feature_set = None

    def start(self):
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(target=self._run, name="SkillMonitor", daemon=True)
        self._thread.start()

    def stop(self):
        self._started = False

    def _run(self):
        try:
            import cv2
            import numpy as np
            from ok.feature.FeatureSet import FeatureSet
            from PIL import ImageGrab
        except Exception as error:
            self.status = f"不可用({error.__class__.__name__})"
            print(f"[record] skill monitor unavailable: {error}")
            return

        if not os.path.exists(self.coco_json):
            self.status = "缺模板"
            print(f"[record] skill coco json missing: {self.coco_json}")
            return

        try:
            self._feature_set = FeatureSet(
                debug=False,
                coco_json=self.coco_json,
                default_horizontal_variance=0.002,
                default_vertical_variance=0.002,
                default_threshold=self.threshold,
            )
            self._grab = lambda: cv2.cvtColor(np.array(ImageGrab.grab()), cv2.COLOR_RGB2BGR)
            # 先跑一次: 加载模板 + 确认模板名存在, 免得之后每 0.05s 抛一次异常
            state, scores = self._poll()
        except Exception as error:
            self.status = f"加载失败({error.__class__.__name__})"
            print(f"[record] skill monitor load failed: {error}")
            return

        self.state = state
        self.scores = scores
        self.status = "监听中"
        print(f"[record] skill monitor listening for E state @ {self.interval}s")
        last_beat = time.time()
        while self._started:
            start = time.time()
            try:
                state, scores = self._poll()
            except Exception as error:
                self.status = f"读取失败({error.__class__.__name__})"
                print(f"[record] skill poll failed: {error}")
                time.sleep(0.5)
                continue
            self.scores = scores
            if state != self.state:
                previous = self.state
                self.state = state
                self.event_count += 1
                print(f"[record] skill E {previous} -> {state} {self.detail()}")
                self.on_event(previous, state, scores, self.white_ratio)
            if self.on_state is not None:
                self.on_state(state, scores)
            if time.time() - last_beat >= SKILL_HEARTBEAT_INTERVAL:
                last_beat = time.time()
                print(f"[record] skill E state={state} {self.detail()}")
            time.sleep(max(0.0, self.interval - (time.time() - start)))

    def _poll(self):
        frame = self._grab()
        scores = {}
        for name in self.features:
            boxes = self._feature_set.find_one_feature(frame, name, threshold=0.001)
            scores[name] = max((box.confidence for box in boxes), default=0.0)
        self.scores = scores
        self.white_ratio = self._white_ratio(frame)
        best = max(scores, key=scores.get) if scores else None
        if best is not None and scores[best] >= self.threshold:
            state = SKILL_STATE_LABELS.get(best, best)
        elif self.white_ratio >= SKILL_WHITE_RATIO_MIN:
            state = SKILL_STATE_WHITE
        else:
            state = SKILL_STATE_NONE
        return state, scores

    def _white_ratio(self, frame):
        """模板框内近白像素占比(诊断用): 分辨"白 E"和"没有图标"."""
        try:
            feature = self._feature_set.get_feature_by_name(frame, self.features[0])
            if feature is None:
                return 0.0
            region = frame[
                feature.y : feature.y + feature.height,
                feature.x : feature.x + feature.width,
            ]
            if region.size == 0:
                return 0.0
            pixels = region.reshape(-1, region.shape[-1])[:, :3]
            return float((pixels.min(axis=1) > SKILL_WHITE_PIXEL_MIN).mean())
        except Exception:
            return 0.0

    def detail(self):
        parts = [f"{name}={value:.3f}" for name, value in self.scores.items()]
        parts.append(f"white_ratio={self.white_ratio:.2f}")
        return " ".join(parts)


class InputRecorder:
    def __init__(self):
        self._lock = threading.Lock()
        self._recording = False
        self._events = []
        self._start_time = 0.0
        self._last_time = 0.0
        self._modifiers = set()
        self._suppress = set()
        self.last_log_path = None
        self.sound_score = 0.0
        self.sound_events = 0
        self.skill_events = 0

    @property
    def recording(self):
        with self._lock:
            return self._recording

    def events_since(self, index):
        with self._lock:
            return list(self._events[index:]), len(self._events)

    def start(self):
        with self._lock:
            if self._recording:
                return
            self._events = []
            self._recording = True
            now = time.time()
            self._start_time = now
            self._last_time = now
        print("[record] START")

    def stop(self):
        with self._lock:
            if not self._recording:
                return None
            self._recording = False
            path = self._write_log()
            self.last_log_path = path
        print(f"[record] STOP -> {path}")
        return path

    def on_press(self, key):
        try:
            self._handle_press(key)
        except Exception as e:
            print(f"[record] press error: {e}")

    def on_release(self, key):
        try:
            self._handle_release(key)
        except Exception as e:
            print(f"[record] release error: {e}")

    def on_click(self, x, y, button, pressed):
        try:
            with self._lock:
                if not self._recording:
                    return
                action = "mouse_down" if pressed else "mouse_up"
                self._record(action, f"{_format_button(button)} @ ({x}, {y})")
        except Exception as e:
            print(f"[record] click error: {e}")

    def on_scroll(self, x, y, dx, dy):
        try:
            with self._lock:
                if not self._recording:
                    return
                self._record("scroll", f"({dx}, {dy}) @ ({x}, {y})")
        except Exception as e:
            print(f"[record] scroll error: {e}")

    def on_sound_score(self, score):
        self.sound_score = score

    def on_sound_event(self, score):
        """声音线程回调: 命中闪避成功音时插一条事件(仅记录期间写入日志)."""
        try:
            with self._lock:
                self.sound_events += 1
                if not self._recording:
                    return
                self._record("sound", f"dodge_success score={score:.3f}")
        except Exception as e:
            print(f"[record] sound event error: {e}")

    def on_skill_event(self, previous, state, scores, white_ratio=0.0):
        """技能图标线程回调: E 状态变化时插一条事件(仅记录期间写入日志)."""
        try:
            with self._lock:
                self.skill_events += 1
                if not self._recording:
                    return
                detail = f"E {previous} -> {state} " + " ".join(
                    f"{name}={value:.3f}" for name, value in scores.items()
                )
                self._record("skill", f"{detail} white_ratio={white_ratio:.2f}")
        except Exception as e:
            print(f"[record] skill event error: {e}")

    def _handle_press(self, key):
        name = _format_key(key)
        with self._lock:
            if name in _MODIFIERS:
                self._modifiers.add(name)
            if self._hotkey(key, "h") or key == keyboard.Key.f10:
                self._suppress.update({name} | _MODIFIERS)
                self._start_locked()
                return
            if self._hotkey(key, "j") or key == keyboard.Key.f12:
                self._suppress.update({name} | _MODIFIERS)
                self._stop_locked()
                return
            if self._recording:
                self._record("key_down", name)

    def _handle_release(self, key):
        name = _format_key(key)
        with self._lock:
            if name in _MODIFIERS:
                self._modifiers.discard(name)
            if name in self._suppress:
                self._suppress.discard(name)
                return
            if self._recording:
                self._record("key_up", name)

    def _hotkey(self, key, letter):
        char = getattr(key, "char", None)
        if not char or char.lower() != letter:
            return False
        return bool(self._modifiers & _CTRL) and bool(self._modifiers & _SHIFT)

    def _record(self, action, detail):
        now = time.time()
        delta = now - self._last_time if self._last_time else 0.0
        self._last_time = now
        self._events.append((now, delta, action, detail))

    def _start_locked(self):
        if self._recording:
            return
        self._events = []
        self._recording = True
        now = time.time()
        self._start_time = now
        self._last_time = now
        print("[record] START")

    def _stop_locked(self):
        if not self._recording:
            return
        self._recording = False
        path = self._write_log()
        self.last_log_path = path
        print(f"[record] STOP -> {path}")

    def _write_log(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        start_str = datetime.fromtimestamp(self._start_time).strftime("%Y%m%d_%H%M%S")
        path = os.path.join(LOG_DIR, f"input_record_{start_str}.log")
        header = datetime.fromtimestamp(self._start_time).strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(f"==== input record {header} ====\n")
            for now, delta, action, detail in self._events:
                stamp = datetime.fromtimestamp(now).strftime("%H:%M:%S")
                millis = int((now % 1) * 1000)
                handle.write(f"{stamp}.{millis:03d} +{delta:6.3f}s {action} {detail}\n")
        return path


class RecorderApp:
    def __init__(self, root, recorder, monitor=None, skill_monitor=None):
        self.root = root
        self.recorder = recorder
        self.monitor = monitor
        self.skill_monitor = skill_monitor
        self._shown = 0
        self._last_state = None

        root.title("键鼠录制")
        root.geometry("560x560")
        root.attributes("-topmost", True)

        top = tk.Frame(root)
        top.pack(fill="x", padx=8, pady=6)
        self.button = tk.Button(top, text="开始记录", width=12, command=self._toggle)
        self.button.pack(side="left")
        self.status = tk.Label(top, text="未记录", anchor="w")
        self.status.pack(side="left", padx=10)

        tk.Label(
            root,
            text="按钮切换; 热键 F10 开始 / F12 停止 (Ctrl+Shift+H/J 可能被输入法占用)",
            anchor="w",
        ).pack(fill="x", padx=8)

        sound_frame = tk.Frame(root)
        sound_frame.pack(fill="x", padx=8, pady=2)
        self.sound_var = tk.BooleanVar(value=monitor is not None)
        self.sound_check = tk.Checkbutton(
            sound_frame,
            text="监听闪避成功音",
            variable=self.sound_var,
            command=self._toggle_sound,
            state="normal" if monitor is not None else "disabled",
        )
        self.sound_check.pack(side="left")
        self.sound_label = tk.Label(sound_frame, text="声音: 未启动", anchor="w")
        self.sound_label.pack(side="left", padx=10)

        skill_frame = tk.Frame(root)
        skill_frame.pack(fill="x", padx=8, pady=2)
        self.skill_var = tk.BooleanVar(value=skill_monitor is not None)
        self.skill_check = tk.Checkbutton(
            skill_frame,
            text="监听 E 状态(金/紫)",
            variable=self.skill_var,
            command=self._toggle_skill,
            state="normal" if skill_monitor is not None else "disabled",
        )
        self.skill_check.pack(side="left")
        self.skill_label = tk.Label(skill_frame, text="E: 未启动", anchor="w")
        self.skill_label.pack(side="left", padx=10)

        self.path_var = tk.StringVar(value="日志: (未生成)")
        tk.Label(root, textvariable=self.path_var, anchor="w", wraplength=540).pack(
            fill="x", padx=8
        )

        text_frame = tk.Frame(root)
        text_frame.pack(fill="both", expand=True, padx=8, pady=6)
        scroll = tk.Scrollbar(text_frame)
        scroll.pack(side="right", fill="y")
        self.text = tk.Text(
            text_frame, height=16, yscrollcommand=scroll.set, state="disabled"
        )
        self.text.pack(side="left", fill="both", expand=True)
        scroll.config(command=self.text.yview)

        bottom = tk.Frame(root)
        bottom.pack(fill="x", padx=8, pady=4)
        tk.Button(bottom, text="清空显示", command=self._clear).pack(side="left")
        tk.Button(bottom, text="打开日志目录", command=self._open_log_dir).pack(
            side="left", padx=6
        )

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll()

    def _toggle(self):
        if self.recorder.recording:
            self.recorder.stop()
        else:
            self._clear()
            self.recorder.start()

    def _toggle_sound(self):
        if self.monitor is None:
            return
        if self.sound_var.get():
            self.monitor.start()
        else:
            self.monitor.stop()

    def _toggle_skill(self):
        if self.skill_monitor is None:
            return
        if self.skill_var.get():
            self.skill_monitor.start()
        else:
            self.skill_monitor.stop()

    def _clear(self):
        self._shown = 0
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.config(state="disabled")

    def _append(self, event):
        now, delta, action, detail = event
        stamp = datetime.fromtimestamp(now).strftime("%H:%M:%S")
        millis = int((now % 1) * 1000)
        line = f"{stamp}.{millis:03d} +{delta:6.3f}s {action} {detail}\n"
        self.text.config(state="normal")
        self.text.insert("end", line)
        line_count = int(self.text.index("end-1c").split(".")[0])
        if line_count > MAX_LINES:
            self.text.delete("1.0", f"{line_count - MAX_LINES}.0")
        self.text.see("end")
        self.text.config(state="disabled")

    def _poll(self):
        events, total = self.recorder.events_since(self._shown)
        if total < self._shown:
            self._clear()
            events, total = self.recorder.events_since(0)
        for event in events:
            self._append(event)
        self._shown = total

        recording = self.recorder.recording
        if recording != self._last_state:
            self._last_state = recording
            self.button.config(text="停止记录" if recording else "开始记录")
        if recording:
            self.status.config(text=f"记录中 ({total} 事件)")
        else:
            self.status.config(text=f"未记录 (共 {total} 事件)")
        if self.recorder.last_log_path:
            self.path_var.set(f"日志: {self.recorder.last_log_path}")
        self._poll_sound()
        self._poll_skill()
        self.root.after(150, self._poll)

    def _poll_sound(self):
        if self.monitor is None:
            return
        score = self.recorder.sound_score
        hit = "  [命中]" if score >= self.monitor.threshold else ""
        self.sound_label.config(
            text=(
                f"声音: {self.monitor.status} | 分数 {score:.3f} "
                f"(阈值 {self.monitor.threshold}) | 命中 {self.monitor.event_count} 次{hit}"
            )
        )

    def _poll_skill(self):
        if self.skill_monitor is None:
            return
        self.skill_label.config(
            text=(
                f"E: {self.skill_monitor.state} {self.skill_monitor.detail()} "
                f"| 变化 {self.skill_monitor.event_count} 次 | {self.skill_monitor.status}"
            )
        )

    def _open_log_dir(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        try:
            os.startfile(LOG_DIR)
        except Exception as e:
            print(f"[record] open dir failed: {e}")

    def _on_close(self):
        self.recorder.stop()
        if self.monitor is not None:
            self.monitor.stop()
        if self.skill_monitor is not None:
            self.skill_monitor.stop()
        self.root.destroy()


def _resolve_process_name():
    try:
        from src import GAME_EXE

        return GAME_EXE
    except Exception:
        return DEFAULT_GAME_EXE


def _set_dpi_awareness():
    """按物理像素截图: 否则显示缩放不是 100% 时 E 图标会截错位置/被缩放糊掉."""
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception as error:
        print(f"[record] dpi awareness failed: {error}")


def main():
    _set_dpi_awareness()
    recorder = InputRecorder()
    monitor = SoundMonitor(
        sample_path=DODGE_SUCCESS_SAMPLE,
        threshold=DODGE_SUCCESS_THRESHOLD,
        process_name=_resolve_process_name(),
        on_event=recorder.on_sound_event,
        on_score=recorder.on_sound_score,
    )
    skill_monitor = SkillMonitor(
        coco_json=SKILL_COCO_JSON,
        features=SKILL_FEATURES,
        threshold=SKILL_THRESHOLD,
        interval=SKILL_POLL_INTERVAL,
        on_event=recorder.on_skill_event,
    )

    keyboard_listener = keyboard.Listener(
        on_press=recorder.on_press, on_release=recorder.on_release
    )
    mouse_listener = mouse.Listener(
        on_click=recorder.on_click, on_scroll=recorder.on_scroll
    )
    keyboard_listener.start()
    mouse_listener.start()

    root = tk.Tk()
    RecorderApp(root, recorder, monitor, skill_monitor)
    monitor.start()
    skill_monitor.start()
    try:
        root.mainloop()
    finally:
        recorder.stop()
        monitor.stop()
        skill_monitor.stop()
        keyboard_listener.stop()
        mouse_listener.stop()


if __name__ == "__main__":
    main()
