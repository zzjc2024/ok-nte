"""用和 app 完全相同的 WASAPI **进程回环** 录制游戏音频, 供声音阈值标定.

用法(仓库 .venv, 通过 tools\\record_process_audio.cmd 启动):
    tools\\record_process_audio.cmd                     # 录到 Ctrl+C
    tools\\record_process_audio.cmd --seconds 60         # 录 60 秒
    tools\\record_process_audio.cmd --process HTGame.exe
    tools\\record_process_audio.cmd --analyze logs\\process_audio_output.wav

和 tools\\game_recorder_gui.py 的区别: 那个录的是**默认输出设备回环**(你耳朵听到的混音),
本工具录的是**进程回环**(`src/sound_trigger/SoundListener` 实际拿到的那条轨)。
两者电平/混音并不相同(同一套模板实测: 设备录音 0.459 vs 游戏内 0.14~0.23),
所以标定阈值必须用本工具, 否则会差大约一倍。

录制时复用 app 的 `SoundListener` 滤波+匹配, 每 25ms 打分并实时打印峰值;
结束后打印各音效的峰值列表(时间 + 分数), 直接回答"这个音在游戏里到底有多强"。
只读取系统音频输出, 不做进程注入、不写游戏内存、不改游戏文件。
"""

import argparse
import os
import sys
import time
import wave

import numpy as np

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

DEFAULT_OUT = os.path.join("logs", "process_audio_output.wav")
SOUND_FILES = {
    "dodge": ("dodge.wav", 0.13),
    "counter": ("counter.wav", 0.12),
    "success": ("dodge_success.wav", 0.3),
    "motion": ("dodge_motion_{1,2,3}.wav", 0.2),
}
PEAK_GAP = 0.3
SCORE_STEP = 0.025


def _asset(*parts):
    return os.path.join(BASE_DIR, "assets", "sounds", *parts)


class Matcher:
    """app 同款打分: 每个 0.2s 窗口单独滤波+归一化后与模板相关."""

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


def _report(series, label):
    print(f"\n=== {label} ===")
    for name, (path, threshold) in SOUND_FILES.items():
        times, scores = (np.asarray(values, dtype=float) for values in series[name])
        if len(scores) == 0:
            print(f"{name:8s} (threshold {threshold}): no samples")
            continue
        peaks = _peaks(times, scores, max(threshold * 0.6, 0.05))
        top = ", ".join(f"{moment:.2f}s={score:.3f}" for moment, score in peaks[:12])
        print(
            f"{name:8s} (threshold {threshold}): max={scores.max():.3f} "
            f"p90={np.percentile(scores, 90):.3f} peaks={len(peaks)}\n    {top}"
        )


def _store(series, name, moment, value):
    series[name][0].append(moment)
    series[name][1].append(value)


def record(args, matcher):
    from src.sound_trigger.capture import MODE_PROCESS, create_capture_source

    capture = create_capture_source(MODE_PROCESS, process_name=args.process)
    if not capture.start():
        print(f"[record] process loopback not ready for {args.process}: {capture.error}")
        return 1
    print(f"[record] capturing {capture.name} -> {args.out} (Ctrl+C to stop)")
    print(f"[record] thresholds: {', '.join(f'{k}={v[1]}' for k, v in SOUND_FILES.items())}")

    series = {name: ([], []) for name in SOUND_FILES}
    tail = np.zeros(0, dtype=np.float32)
    total = 0
    since_score = 0
    step = int(matcher.listener.used_sr * SCORE_STEP)
    live = {name: 0.0 for name in SOUND_FILES}
    live_peak = {name: 0.0 for name in SOUND_FILES}
    next_print = time.time() + 1.0
    start = time.time()

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with wave.open(args.out, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(matcher.listener.used_sr)
        try:
            while True:
                if args.seconds and time.time() - start >= args.seconds:
                    break
                chunk = capture.read(timeout=0.2)
                if chunk is None or chunk.size == 0:
                    continue
                handle.writeframes(
                    (np.clip(chunk, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
                )
                total += chunk.size
                tail = np.concatenate([tail, chunk])[-matcher.window :]
                since_score += chunk.size
                if tail.size < matcher.window or since_score < step:
                    continue
                since_score = 0
                moment = total / matcher.listener.used_sr
                for name, value in matcher.score(tail).items():
                    live[name] = value
                    live_peak[name] = max(live_peak[name], value)
                    _store(series, name, moment, value)
                if time.time() >= next_print:
                    next_print = time.time() + 1.0
                    print(
                        f"[{moment:6.1f}s] "
                        + "  ".join(
                            f"{name} {live[name]:.3f}/peak {live_peak[name]:.3f}"
                            for name in SOUND_FILES
                        )
                    )
                    for name in live_peak:
                        live_peak[name] = 0.0
        except KeyboardInterrupt:
            print("\n[record] stopped by user")
        finally:
            capture.stop()

    duration = total / matcher.listener.used_sr
    print(f"[record] saved {args.out} ({duration:.1f}s)")
    _report(series, f"live peaks while recording ({args.process})")
    return 0


def analyze(args):
    from src.sound_trigger.capture.base import CAPTURE_SAMPLE_RATE

    matcher = Matcher(args.process)
    step = int(CAPTURE_SAMPLE_RATE * SCORE_STEP)
    with wave.open(args.analyze, "rb") as handle:
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

    series = {name: ([], []) for name in SOUND_FILES}
    window = matcher.window
    for start in range(0, max(0, len(data) - window), step):
        moment = start / rate
        for name, value in matcher.score(data[start : start + window]).items():
            _store(series, name, moment, value)
    scored = len(series["dodge"][0])
    print(f"[analyze] {args.analyze}: {len(data) / rate:.1f}s, scored {scored} windows")
    _report(series, os.path.basename(args.analyze))
    return 0


def main():
    parser = argparse.ArgumentParser(description="Process-loopback game audio recorder")
    parser.add_argument("--process", default="HTGame.exe", help="target process name")
    parser.add_argument("--out", default=DEFAULT_OUT, help="output wav path")
    parser.add_argument("--seconds", type=float, default=0.0, help="0 = until Ctrl+C")
    parser.add_argument("--analyze", default="", help="score an existing wav instead of recording")
    args = parser.parse_args()
    if args.analyze:
        return analyze(args)
    return record(args, Matcher(args.process))


if __name__ == "__main__":
    sys.exit(main())
