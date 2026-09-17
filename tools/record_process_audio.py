"""GUI 录音器: 用和 app 相同的 WASAPI **进程回环** 录游戏音频, 并实时打分.

用法:
    1. 双击 tools\\record_process_audio.cmd 启动 (仓库 .venv, 需要游戏在运行).
    2. 点"开始录制"或按 F11 开始; 点"停止"或按 F12 停止.
    3. 停止时写 logs/process_audio_output.wav, 峰值报告写
       logs/process_audio_peaks_<时间>.txt, 并显示在窗口里.
    4. 命令行 `--analyze <wav>` 可离线复算任意录音(不弹窗); `--seconds N`
       启动后自动录 N 秒, 适合无人值守。

录的是 `src/sound_trigger/SoundListener` 实际拿到的 **WASAPI 进程回环**(只含游戏进程),
不是默认输出设备回环(你耳朵听到的混音)。两者电平不同: 同一套模板在设备录音上 0.459,
在游戏内进程捕获上 0.14~0.23, 所以标定阈值必须用本工具, 否则会差大约一倍。

打分复用 app 的 SoundListener 滤波+匹配(每个 0.2s 窗口单独滤波+归一化),
只读取系统音频输出, 不做进程注入、不写游戏内存、不改游戏文件。
"""

import argparse
import ctypes
import os
import sys
import threading
import time
import tkinter as tk
import wave
from datetime import datetime

import numpy as np

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

DEFAULT_OUT = os.path.join("logs", "process_audio_output.wav")
PEAK_GAP = 0.3
SCORE_STEP = 0.025
# 阈值与 app 的 Sound Trigger Config 默认值一致, 便于直接对比。
SOUNDS = {
    "dodge": ("dodge.wav", 0.13),
    "counter": ("counter.wav", 0.12),
    "success": ("dodge_success.wav", 0.3),
    "motion": ("dodge_motion_1/2/3.wav", 0.2),
}


def _asset(*parts):
    return os.path.join(BASE_DIR, "assets", "sounds", *parts)


def _set_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


class Matcher:
    """app 同款打分: 每个 0.2s 窗口单独滤波+归一化后与模板做相关."""

    def __init__(self, process_name):
        from src.sound_trigger.SoundListener import SoundListener

        self.listener = SoundListener(
            sample_path=_asset("dodge.wav"),
            counter_attack_sample_path=_asset("counter.wav"),
            dodge_success_sample_path=_asset("dodge_success.wav"),
            dodge_motion_sample_paths=[_asset(f"dodge_motion_{i}.wav") for i in (1, 2, 3)],
            process_name=process_name,
        )
        self.window = int(self.listener.used_sr * self.listener.sample_len)
        self.step = int(self.listener.used_sr * SCORE_STEP)

    def score(self, window):
        norm = self.listener._prepare_stream_waveform(window)
        motion = 0.0
        for template in self.listener._dodge_motion_sample_waveforms:
            motion = max(motion, self.listener._match_normalized(norm, template))
        return {
            "dodge": self.listener._match_normalized(norm, self.listener._sample_waveform),
            "counter": self.listener._match_normalized(
                norm, self.listener._counter_sample_waveform
            ),
            "success": self.listener._match_normalized(
                norm, self.listener._dodge_success_sample_waveform
            ),
            "motion": motion,
        }


def _peaks(times, scores, floor):
    out = []
    for index in np.argsort(scores)[::-1]:
        if scores[index] < floor:
            break
        moment = float(times[index])
        if all(abs(moment - peak[0]) >= PEAK_GAP for peak in out):
            out.append((moment, float(scores[index])))
    return sorted(out)


def format_report(series, label):
    lines = [f"=== {label} ==="]
    for name, (path, threshold) in SOUNDS.items():
        times, scores = (np.asarray(values, dtype=float) for values in series[name])
        if len(scores) == 0:
            lines.append(f"{name:8s} (threshold {threshold}): no samples")
            continue
        peaks = _peaks(times, scores, max(threshold * 0.6, 0.05))
        top = ", ".join(f"{moment:.2f}s={score:.3f}" for moment, score in peaks[:12])
        lines.append(
            f"{name:8s} (threshold {threshold}): max={scores.max():.3f} "
            f"p90={np.percentile(scores, 90):.3f} peaks={len(peaks)}\n    {top}"
        )
    return "\n".join(lines)


class Recorder:
    """后台线程: 进程回环 -> wav + 实时打分."""

    def __init__(self, matcher, process_name, out_path):
        self.matcher = matcher
        self.process_name = process_name
        self.out_path = out_path
        self.error = ""
        self.report_text = ""
        self._thread = None
        self._stop = threading.Event()
        self._state_lock = threading.Lock()
        self._series = {name: ([], []) for name in SOUNDS}
        self._live = {name: 0.0 for name in SOUNDS}
        self._peak = {name: 0.0 for name in SOUNDS}
        self._seconds = 0.0
        self._running = False

    @property
    def running(self):
        return self._running

    def start(self):
        if self._thread is not None:
            return
        self.error = ""
        self.report_text = ""
        self._series = {name: ([], []) for name in SOUNDS}
        self._seconds = 0.0
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ProcessRecorder", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=3.0)

    def snapshot(self):
        with self._state_lock:
            return {
                "seconds": self._seconds,
                "live": dict(self._live),
                "peak": dict(self._peak),
                "error": self.error,
            }

    def _publish(self, name, value):
        with self._state_lock:
            self._live[name] = value
            self._peak[name] = max(self._peak[name], value)

    def _run(self):
        from src.sound_trigger.capture import MODE_PROCESS, create_capture_source

        capture = create_capture_source(MODE_PROCESS, process_name=self.process_name)
        if not capture.start():
            self.error = (
                f"进程回环不可用 ({self.process_name}): {capture.error}. "
                "确认游戏在运行; 游戏是管理员启动的话本工具也要提权。"
            )
            print(f"[record] {self.error}")
            return
        print(f"[record] capturing {capture.name} -> {self.out_path}")

        tail = np.zeros(0, dtype=np.float32)
        total = 0
        since_score = 0
        next_tick = time.time() + 1.0
        os.makedirs(os.path.dirname(self.out_path) or ".", exist_ok=True)
        self._running = True
        try:
            with wave.open(self.out_path, "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(self.matcher.listener.used_sr)
                while not self._stop.is_set():
                    chunk = capture.read(timeout=0.2)
                    if chunk is None or chunk.size == 0:
                        continue
                    handle.writeframes(
                        (np.clip(chunk, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
                    )
                    total += chunk.size
                    tail = np.concatenate([tail, chunk])[-self.matcher.window :]
                    since_score += chunk.size
                    if tail.size < self.matcher.window or since_score < self.matcher.step:
                        continue
                    since_score = 0
                    moment = total / self.matcher.listener.used_sr
                    for name, value in self.matcher.score(tail).items():
                        self._publish(name, value)
                        self._series[name][0].append(moment)
                        self._series[name][1].append(value)
                    self._seconds = moment
                    if time.time() >= next_tick:
                        next_tick = time.time() + 1.0
                        with self._state_lock:
                            print(
                                f"[{moment:6.1f}s] "
                                + "  ".join(
                                    f"{name} {self._live[name]:.3f}"
                                    f"/peak {self._peak[name]:.3f}"
                                    for name in SOUNDS
                                )
                            )
        except Exception as error:
            # 录到一半失败也要让用户看到原因, 而不是静默丢文件
            self.error = f"录制失败: {error}"
            print(f"[record] {self.error}")
        finally:
            self._running = False
            capture.stop()

        self.report_text = format_report(self._series, os.path.basename(self.out_path))
        print(f"[record] saved {self.out_path} ({self._seconds:.1f}s)")
        print(self.report_text)
        return


class RecorderApp:
    def __init__(self, root, recorder, auto_seconds=0.0):
        self.root = root
        self.recorder = recorder
        self.auto_seconds = auto_seconds
        self._auto_stop_at = 0.0
        root.title("进程回环录音 (SoundListener 同一条轨)")
        root.geometry("760x420")
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        tk.Label(
            root,
            text=f"目标进程: {recorder.process_name}   输出: {recorder.out_path}",
            font=("Microsoft YaHei", 10),
        ).pack(pady=(12, 2), anchor="w", padx=12)
        self.status = tk.Label(
            root, text="未开始 (F11 开始 / F12 停止)", font=("Microsoft YaHei", 12), fg="gray"
        )
        self.status.pack(pady=4, anchor="w", padx=12)

        buttons = tk.Frame(root)
        buttons.pack(anchor="w", padx=12)
        self.toggle_button = tk.Button(
            buttons,
            text="开始录制 (F11)",
            font=("Microsoft YaHei", 11),
            bg="#4CAF50",
            fg="white",
            width=18,
            height=2,
            command=self._toggle,
        )
        self.toggle_button.pack(side="left")

        self.score_labels = {}
        scores = tk.Frame(root)
        scores.pack(anchor="w", padx=12, pady=8)
        for name, (path, threshold) in SOUNDS.items():
            label = tk.Label(
                scores,
                text=f"{name:8s} 0.000 / peak 0.000  (threshold {threshold}, {path})",
                font=("Consolas", 10),
                anchor="w",
            )
            label.pack(anchor="w")
            self.score_labels[name] = label

        tk.Label(root, text="峰值报告", font=("Microsoft YaHei", 10)).pack(
            anchor="w", padx=12
        )
        self.report = tk.Text(root, height=9, font=("Consolas", 9))
        self.report.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        root.after(200, self._poll)
        if auto_seconds > 0:
            root.after(300, self._start)

    def _toggle(self):
        if self.recorder.running:
            self._stop()
        else:
            self._start()

    def _start(self):
        if self.recorder.running:
            return
        self.status.config(text="录制中...", fg="red")
        self.toggle_button.config(text="停止 (F12)", bg="#F44336")
        self.report.delete("1.0", "end")
        if self.auto_seconds > 0:
            self._auto_stop_at = time.time() + self.auto_seconds
        self.recorder.start()

    def _stop(self):
        self.recorder.stop()
        self._auto_stop_at = 0.0
        self.status.config(text="已停止", fg="gray")
        self.toggle_button.config(text="开始录制 (F11)", bg="#4CAF50")
        self.report.delete("1.0", "end")
        self.report.insert("1.0", self.recorder.report_text or "(没有数据)")
        self._write_report()

    def _write_report(self):
        if not self.recorder.report_text:
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join("logs", f"process_audio_peaks_{stamp}.txt")
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.recorder.report_text + "\n")
            print(f"[record] peak report saved to {path}")
        except OSError as error:
            print(f"[record] failed to save peak report: {error}")

    def _poll(self):
        snapshot = self.recorder.snapshot()
        if snapshot["error"]:
            self.status.config(text=snapshot["error"], fg="red")
        elif self.recorder.running:
            self.status.config(text=f"录制中 {snapshot['seconds']:.1f}s", fg="red")
        for name, label in self.score_labels.items():
            _, threshold = SOUNDS[name]
            label.config(
                text=(
                    f"{name:8s} {snapshot['live'][name]:.3f} / "
                    f"peak {snapshot['peak'][name]:.3f}  (threshold {threshold})"
                )
            )
        if self._auto_stop_at and time.time() >= self._auto_stop_at:
            self._stop()
        self.root.after(200, self._poll)

    def _on_close(self):
        self.recorder.stop()
        self.root.destroy()


def analyze(path, process_name):
    from src.sound_trigger.capture.base import CAPTURE_SAMPLE_RATE

    matcher = Matcher(process_name)
    with wave.open(path, "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    if width != 2:
        print(f"[analyze] only 16-bit PCM supported, got {width * 8}-bit")
        return 1
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    if rate != CAPTURE_SAMPLE_RATE:
        print(f"[analyze] warning: file is {rate}Hz, app captures at {CAPTURE_SAMPLE_RATE}Hz")

    series = {name: ([], []) for name in SOUNDS}
    window = matcher.window
    for start in range(0, max(0, len(data) - window), matcher.step):
        moment = start / rate
        for name, value in matcher.score(data[start : start + window]).items():
            series[name][0].append(moment)
            series[name][1].append(value)
    scored = len(series["dodge"][0])
    print(f"[analyze] {path}: {len(data) / rate:.1f}s, scored {scored} windows")
    print(format_report(series, os.path.basename(path)))
    return 0


def _hotkey_listener(root, on_start, on_stop):
    from pynput import keyboard

    def on_press(key):
        if key == keyboard.Key.f11:
            root.after(0, on_start)
        elif key == keyboard.Key.f12:
            root.after(0, on_stop)

    listener = keyboard.Listener(on_press=on_press)
    listener.daemon = True
    listener.start()
    return listener


def main():
    parser = argparse.ArgumentParser(description="Process-loopback game audio recorder (GUI)")
    parser.add_argument("--process", default="HTGame.exe", help="target process name")
    parser.add_argument("--out", default=DEFAULT_OUT, help="output wav path")
    parser.add_argument(
        "--seconds", type=float, default=0.0, help="auto start and stop after N seconds"
    )
    parser.add_argument("--analyze", default="", help="score an existing wav instead of GUI")
    args = parser.parse_args()

    if args.analyze:
        return analyze(args.analyze, args.process)

    _set_dpi_awareness()
    try:
        matcher = Matcher(args.process)
    except Exception as error:
        print(f"[record] failed to load sound samples: {error}")
        return 1

    root = tk.Tk()
    recorder = Recorder(matcher, args.process, args.out)
    app = RecorderApp(root, recorder, auto_seconds=args.seconds)
    listener = _hotkey_listener(root, app._start, app._stop)
    try:
        root.mainloop()
    finally:
        recorder.stop()
        listener.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
