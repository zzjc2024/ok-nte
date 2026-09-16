"""记录真实键鼠操作的小工具.

热键:
    Ctrl+Shift+H  开始记录
    Ctrl+Shift+J  停止记录并生成日志
    Ctrl+C        退出

日志写入 logs/input_record_YYYYmmdd_HHMMSS.log, 用于复盘手动操作时序.
只监听本机输入事件, 不注入、不读取游戏内存.
"""

import os
import threading
import time
from datetime import datetime

from pynput import keyboard, mouse

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, "logs")

_MODIFIERS = {
    "ctrl", "ctrl_l", "ctrl_r",
    "shift", "shift_l", "shift_r",
    "alt", "alt_l", "alt_r", "alt_gr",
    "cmd", "cmd_l", "cmd_r",
}
_CTRL = {"ctrl", "ctrl_l", "ctrl_r"}
_SHIFT = {"shift", "shift_l", "shift_r"}


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


class InputRecorder:
    def __init__(self):
        self._lock = threading.Lock()
        self._recording = False
        self._events = []
        self._start_time = 0.0
        self._last_time = 0.0
        self._modifiers = set()
        self._suppress = set()

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

    def _handle_press(self, key):
        name = _format_key(key)
        with self._lock:
            if name in _MODIFIERS:
                self._modifiers.add(name)
            if self._hotkey(key, "h"):
                self._suppress.update({name} | _MODIFIERS)
                self._start_recording()
                return
            if self._hotkey(key, "j"):
                self._suppress.update({name} | _MODIFIERS)
                self._stop_recording()
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

    def _start_recording(self):
        if self._recording:
            print("[record] already recording")
            return
        self._events = []
        self._recording = True
        now = time.time()
        self._start_time = now
        self._last_time = now
        print("[record] START (Ctrl+Shift+J to stop)")

    def _stop_recording(self):
        if not self._recording:
            print("[record] not recording")
            return
        self._recording = False
        path = self._write_log()
        print(f"[record] STOP, {len(self._events)} events -> {path}")

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

    def stop_if_recording(self):
        with self._lock:
            if self._recording:
                self._stop_recording()


def main():
    recorder = InputRecorder()
    print("Input recorder ready.")
    print("  Ctrl+Shift+H = start, Ctrl+Shift+J = stop, Ctrl+C = quit")
    print(f"  logs -> {LOG_DIR}")
    keyboard_listener = keyboard.Listener(
        on_press=recorder.on_press, on_release=recorder.on_release
    )
    mouse_listener = mouse.Listener(
        on_click=recorder.on_click, on_scroll=recorder.on_scroll
    )
    keyboard_listener.start()
    mouse_listener.start()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[record] quit")
    finally:
        keyboard_listener.stop()
        mouse_listener.stop()
        recorder.stop_if_recording()


if __name__ == "__main__":
    main()
