"""记录真实键鼠操作的小工具(GUI).

用法:
    1. 双击 tools/record_input.cmd 以管理员权限启动.
    2. 点窗口按钮开始/停止记录; 也可用 F10 开始 / F12 停止 (Ctrl+Shift+H/J 可能被输入法占用).
    3. 记录期间实时显示事件; 停止时写入 logs/input_record_YYYYmmdd_HHMMSS.log.

同时后台监听游戏音频, 检测"闪避成功音"(assets/sounds/dodge_success.wav),
命中时在事件流里插入一条 `sound dodge_success score=...`, 便于测量闪避成功后
隔多久才长按/二连。

只监听本机输入事件, 不注入、不读取游戏内存; 音频走 WASAPI 进程回环, 不做进程注入.
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
DODGE_SUCCESS_THRESHOLD = 0.45
DEFAULT_GAME_EXE = "HTGame.exe"

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
            def _check_triggers(self, dodge_score, counter_score):
                monitor._handle_score(dodge_score)
                super()._check_triggers(dodge_score, counter_score)

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
    def __init__(self, root, recorder, monitor=None):
        self.root = root
        self.recorder = recorder
        self.monitor = monitor
        self._shown = 0
        self._last_state = None

        root.title("键鼠录制")
        root.geometry("560x500")
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
        self.root.destroy()


def _resolve_process_name():
    try:
        from src import GAME_EXE

        return GAME_EXE
    except Exception:
        return DEFAULT_GAME_EXE


def main():
    recorder = InputRecorder()
    monitor = SoundMonitor(
        sample_path=DODGE_SUCCESS_SAMPLE,
        threshold=DODGE_SUCCESS_THRESHOLD,
        process_name=_resolve_process_name(),
        on_event=recorder.on_sound_event,
        on_score=recorder.on_sound_score,
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
    RecorderApp(root, recorder, monitor)
    monitor.start()
    try:
        root.mainloop()
    finally:
        recorder.stop()
        monitor.stop()
        keyboard_listener.stop()
        mouse_listener.stop()


if __name__ == "__main__":
    main()
