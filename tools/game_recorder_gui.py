"""GUI recorder for the *default output device* loopback (what you hear).

Note: this is NOT the same track SoundListener hears. SoundListener uses WASAPI
*process* loopback (HTGame.exe only), whose level/mix differs (measured: the same
template scores 0.459 on a device recording vs 0.14~0.23 in the live app), so
thresholds calibrated from these recordings are ~2x too high. Use
tools/record_process_audio.cmd when calibrating sound trigger thresholds.
"""

import sys
import os
import ctypes
import wave
import threading
import pyaudiowpatch as pyaudio
import keyboard
import tkinter as tk
from tkinter import messagebox


def is_admin():
    """检查当前脚本是否以管理员身份运行"""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False


def run_as_admin():
    """请求 Windows 弹出 UAC 提权窗口并重新运行当前脚本"""
    script_path = os.path.abspath(sys.argv[0]) if '__file__' in globals() else sys.executable
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, f'"{script_path}"', None, 1)


class GameAudioRecorderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("游戏音频录制工具")
        self.root.geometry("350x220")
        self.root.resizable(False, False)

        self.is_recording = False
        self.record_thread = None
        self.stop_event = threading.Event()

        # UI 界面布局
        self.label_status = tk.Label(root, text="状态: 未录制", font=("Microsoft YaHei", 12))
        self.label_status.pack(pady=20)

        self.btn_toggle = tk.Button(root, text="开始录制 (F11)", font=("Microsoft YaHei", 11), bg="#4CAF50", fg="white",
                                    width=20, height=2, command=self.toggle_recording)
        self.btn_toggle.pack(pady=5)

        self.label_tip = tk.Label(root, text="快捷键: [F11] 开始录制 | [F12] 停止录制", font=("Microsoft YaHei", 9),
                                  fg="gray")
        self.label_tip.pack(pady=15)

        # 注册全局快捷键
        try:
            keyboard.add_hotkey('f11', self.start_recording_from_hotkey)
            keyboard.add_hotkey('f12', self.stop_recording_from_hotkey)
        except Exception as e:
            print(f"注册快捷键失败: {e}")

        # 监听窗口关闭事件
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def toggle_recording(self):
        """按钮点击切换状态"""
        if not self.is_recording:
            self.start_recording()
        else:
            self.stop_recording()

    def start_recording_from_hotkey(self):
        """快捷键触发开始（线程安全）"""
        if not self.is_recording:
            # 使用 root.after 确保在主线程更新 GUI
            self.root.after(0, self.start_recording)

    def stop_recording_from_hotkey(self):
        """快捷键触发停止（线程安全）"""
        if self.is_recording:
            self.root.after(0, self.stop_recording)

    def start_recording(self):
        """开始录制逻辑"""
        if self.is_recording:
            return

        self.is_recording = True
        self.stop_event.clear()

        # 更新 UI
        self.label_status.config(text="状态: 正在录制...", fg="red")
        self.btn_toggle.config(text="停止录制 (F12)", bg="#F44336")

        # 启动后台录音线程
        self.record_thread = threading.Thread(target=self.record_loop)
        self.record_thread.daemon = True
        self.record_thread.start()

    def stop_recording(self):
        """停止录制逻辑"""
        if not self.is_recording:
            return

        self.is_recording = False
        self.stop_event.set()  # 触发事件通知后台线程退出

        # 更新 UI
        self.label_status.config(text="状态: 已停止并保存", fg="green")
        self.btn_toggle.config(text="开始录制 (F11)", bg="#4CAF50")

    def record_loop(self):
        """后台录音核心循环"""
        p = pyaudio.PyAudio()
        try:
            wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_speakers = p.get_device_info_by_index(wasapi_info['defaultOutputDevice'])

            loopback_device = None
            for loopback in p.get_loopback_device_info_generator():
                if default_speakers['name'] in loopback['name']:
                    loopback_device = loopback
                    break

            if not loopback_device:
                print("未找到可用的内录设备！")
                return

            CHANNELS = loopback_device['maxInputChannels']
            RATE = int(loopback_device['defaultSampleRate'])
            CHUNK = 1024
            WAVE_OUTPUT_FILENAME = "game_audio_output.wav"

            stream = p.open(
                format=pyaudio.paInt16,
                channels=CHANNELS,
                rate=RATE,
                input=True,
                input_device_index=loopback_device['index'],
                frames_per_buffer=CHUNK
            )

            frames = []
            # 只要没有收到停止信号，就持续循环读取音频
            while not self.stop_event.is_set():
                try:
                    data = stream.read(CHUNK, exception_on_overflow=False)
                    frames.append(data)
                except Exception:
                    break

            stream.stop_stream()
            stream.close()
            p.terminate()

            # 保存 WAV 文件
            wf = wave.open(WAVE_OUTPUT_FILENAME, 'wb')
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(p.get_sample_size(pyaudio.paInt16))
            wf.setframerate(RATE)
            wf.writeframes(b''.join(frames))
            wf.close()
            print(f"音频已成功保存至: {WAVE_OUTPUT_FILENAME}")

        except Exception as e:
            print(f"录音过程出错: {e}")

    def on_closing(self):
        """关闭窗口时清理快捷键和线程"""
        if self.is_recording:
            self.stop_recording()
        keyboard.unhook_all()
        self.root.destroy()


if __name__ == "__main__":
    if not is_admin():
        print("当前没有管理员权限，正在请求提权以支持全局快捷键...")
        run_as_admin()
        sys.exit()

    root = tk.Tk()
    app = GameAudioRecorderApp(root)
    root.mainloop()