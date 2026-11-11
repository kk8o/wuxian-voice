"""播放：后台一个线程按顺序放 Clip，同时只放一句；interrupt=True 停掉正在说的、清空队列先放它。

- 一句话（Line）可以分成几个小句（Clip，speaker.parts 逐句交出）：小句之间只留 part_gap，句与句之间留 gap。
- 打断、stop()：正在放的和排着的 Line 都记成 cancelled，它们还没排进来的小句也不放了。
- 排得太久的（过了 Line.deadline）不放了：话说晚了就不对了。
- on_start(clip)、on_end(clip, cut) 在播放线程里调用（每个小句各一次；cut = 被打断或停掉）。
- 后端可换：默认 sounddevice，测试用假后端。后端只要 play(audio, sr, cancel) 阻塞到放完，或 cancel（threading.Event）
  被设上就尽快停（设在开始之前也算）。
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

import numpy as np

from .config import Settings

log = logging.getLogger("voice.player")
MAX_GAIN = 4.0                         # 响度拉平最多放大几倍（静音、气声别被拉成噪音）


@dataclass(eq=False)
class Line:
    text: str
    mood: str = ""
    pool: Optional[str] = None         # 从哪个组来的（play）；say 的为 None
    parts: int = 1                     # 分成几个小句
    deadline: float = math.inf         # time.monotonic()：过了还没开始放就不放
    cancelled: bool = False
    seconds: float = 0.0               # 已放出的小句的时长合计（on_end 时累加）
    started: bool = False
    reported: bool = False             # said 发过了


@dataclass(eq=False)
class Clip:
    line: Line
    audio: np.ndarray                  # float32 单声道
    sr: int
    index: int = 0                     # 第几个小句（从 0）

    @property
    def seconds(self) -> float:
        return len(self.audio) / self.sr if self.sr else 0.0

    @property
    def last(self) -> bool:
        return self.index >= self.line.parts - 1


class Backend(Protocol):
    def play(self, audio: np.ndarray, sr: int, cancel: threading.Event) -> None:
        """放一段（float32 单声道），阻塞到放完；cancel 设上就停"""


def find_device(name: str) -> Optional[int]:
    """按名字子串（不分大小写）找输出设备，优先默认的音频接口（Windows 上一般是 MME）；空或找不到 = None（系统默认）"""
    if not name:
        return None
    import sounddevice as sd
    want = name.lower()
    hits = [i for i, d in enumerate(sd.query_devices()) if d["max_output_channels"] > 0 and want in d["name"].lower()]
    if not hits:
        log.warning("没有名字含「%s」的输出设备，用系统默认", name)
        return None
    try:
        default_api = sd.default.hostapi
    except Exception:
        default_api = None
    for i in hits:
        if sd.query_devices(i)["hostapi"] == default_api:
            return i
    return hits[0]


def _resample(audio: np.ndarray, sr: int, to: int) -> np.ndarray:
    if sr == to or not len(audio):
        return audio
    n = int(round(len(audio) * to / sr))
    return np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)


class SoundDeviceBackend:
    """sounddevice 输出流：每段开一个流，cancel 设上就 abort"""

    def __init__(self, device: str = ""):
        self.device = find_device(device)

    def play(self, audio: np.ndarray, sr: int, cancel: threading.Event) -> None:
        import sounddevice as sd
        try:
            self._play(sd, audio, sr, cancel)
        except sd.PortAudioError as e:                                 # 设备不收这个采样率：重采样再试
            to = int(sd.query_devices(self.device, "output")["default_samplerate"])
            if to == sr:
                raise
            log.info("设备不收 %d Hz（%s），重采样到 %d Hz", sr, e, to)
            self._play(sd, _resample(audio, sr, to), to, cancel)

    def _play(self, sd, audio: np.ndarray, sr: int, cancel: threading.Event) -> None:
        if cancel.is_set():
            return
        data = np.ascontiguousarray(audio, dtype=np.float32).reshape(-1, 1)
        pos = 0
        done = threading.Event()

        def callback(out, frames, _time, _status):
            nonlocal pos
            n = min(frames, len(data) - pos)
            out[:n] = data[pos:pos + n]
            out[n:] = 0
            pos += n
            if pos >= len(data):
                raise sd.CallbackStop

        stream = sd.OutputStream(samplerate=sr, channels=1, dtype="float32", device=self.device, callback=callback,
                                 finished_callback=done.set)
        end = time.monotonic() + len(data) / sr + 2.0
        with stream:
            while not done.wait(0.02):
                if cancel.is_set() or time.monotonic() > end:
                    stream.abort()
                    break


class SoundPlayer:
    def __init__(self, settings: Optional[Settings] = None, backend: Optional[Backend] = None,
                 on_start: Optional[Callable[[Clip], None]] = None,
                 on_end: Optional[Callable[[Clip, bool], None]] = None, gap: float = 0.15, part_gap: float = 0.05):
        self.settings = settings or Settings()
        self.backend = backend if backend is not None else SoundDeviceBackend(self.settings.device)
        self.on_start = on_start
        self.on_end = on_end
        self.gap = gap
        self.part_gap = part_gap
        self._queue: deque[Clip] = deque()
        self._cond = threading.Condition()
        self._current: Optional[Clip] = None
        self._cancel = threading.Event()           # 当前一句的停止信号（每句一个）
        self._last: Optional[Clip] = None          # 上一句（算间隔用）
        self._last_end = 0.0
        self._rush = False                         # 打断来的：不等间隔
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="voice-player", daemon=True)
        self._thread.start()

    def play(self, clip: Clip, interrupt: bool = False) -> bool:
        """排队；interrupt 先停掉正在说的、清空队列。这句话已被打断（cancelled）就不收，返回 False"""
        with self._cond:
            if interrupt:
                self._cut()
                self._rush = True
            if clip.line.cancelled:
                return False
            self._queue.append(clip)
            self._cond.notify_all()
            return True

    def _cut(self) -> int:
        """持锁调用：正在放的、排着的都作废；返回作废了几句话"""
        lines = {id(c.line): c.line for c in self._queue}
        if self._current is not None:
            lines[id(self._current.line)] = self._current.line
        for ln in lines.values():
            ln.cancelled = True
        self._queue.clear()
        self._cancel.set()
        self._cond.notify_all()
        return len(lines)

    def stop(self) -> int:
        with self._cond:
            return self._cut()

    def busy(self) -> bool:
        with self._cond:
            return self._current is not None or bool(self._queue)

    @property
    def current(self) -> Optional[Clip]:
        with self._cond:
            return self._current

    def queued(self) -> list[Line]:
        """排着的话（不含正在说的），每句话一次"""
        with self._cond:
            out: list[Line] = []
            for c in self._queue:
                if not out or out[-1] is not c.line:
                    out.append(c.line)
            cur = self._current.line if self._current is not None else None
            return [x for x in out if x is not cur]

    def wait(self, timeout: Optional[float] = None) -> bool:
        """等到放完（队列空、没在放）；超时返回 False"""
        end = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while self._current is not None or self._queue:
                left = None if end is None else end - time.monotonic()
                if left is not None and left <= 0:
                    return False
                self._cond.wait(left)
            return True

    def close(self) -> None:
        self.stop()
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        self._thread.join(2.0)

    # -------------------------------------------------------------------------------------------------- 播放线程

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                nxt = self._queue[0]
                if nxt.index == 0 and not nxt.line.started and time.monotonic() > nxt.line.deadline:
                    nxt.line.cancelled = True                    # 排太久了：整句话不说了
                    log.info("排太久，不说了：%s", nxt.line.text)
                    while self._queue and self._queue[0].line is nxt.line:
                        self._queue.popleft()
                    self._cond.notify_all()
                    continue
                if not self._rush and self._last is not None:
                    gap = self.part_gap if nxt.line is self._last.line else self.gap
                    left = self._last_end + gap - time.monotonic()
                    if left > 0:
                        self._cond.wait(left)
                        continue                       # 等的时候可能被打断、清空：重新看
                clip = self._queue.popleft()
                self._rush = False
                self._current = clip
                clip.line.started = True
                self._cancel = cancel = threading.Event()
            try:
                if self.on_start is not None and not cancel.is_set():
                    self.on_start(clip)
            except Exception:
                log.exception("on_start 出错")
            try:
                audio = np.asarray(clip.audio, dtype=np.float32).reshape(-1)
                target = float(self.settings.loudness or 0)
                peak = float(np.max(np.abs(audio))) if audio.size else 0.0
                gain = min(MAX_GAIN, target / peak) if target and peak > 1e-4 else 1.0   # 每句拉到同一峰值
                gain *= float(self.settings.volume)
                if gain != 1.0:
                    audio = np.clip(audio * gain, -1.0, 1.0)
                if not cancel.is_set():
                    self.backend.play(audio, clip.sr, cancel)
            except Exception:
                log.exception("播放出错")
            cut = cancel.is_set()
            if not cut:
                clip.line.seconds += clip.seconds
            try:
                if self.on_end is not None:
                    self.on_end(clip, cut)
            except Exception:
                log.exception("on_end 出错")
            with self._cond:
                self._current = None
                self._last = clip
                self._last_end = time.monotonic()
                self._cond.notify_all()


__all__ = ["SoundPlayer", "SoundDeviceBackend", "Backend", "Line", "Clip", "find_device"]
