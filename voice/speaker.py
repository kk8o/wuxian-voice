"""合成：文字 → 音频，Qwen3-TTS 加速版（faster-qwen3-tts，CUDA 图），CustomVoice 的 Serena。

- 情绪：instruct = 设置的 instruct + MOOD_SUFFIX[情绪]（0.6B CustomVoice 会丢掉 instruct，但它进缓存键）。
- 分句：长于 SPLIT_AT 字的按中文标点切成小句逐句合成；parts() 合成好一句交一句，say() 是拼起来的整句。
- 缓存：按 (模型名, speaker, instruct 全文, 文本) 的哈希存 wav（24 kHz 单声道 PCM16），命中不碰显卡；
  键和旧版陪玩一样，cache/voice 里的旧缓存照样命中。
- 模型只在一把锁下用；没 warm() 就合成会先载入。
"""
from __future__ import annotations

import gc
import hashlib
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Iterator, Optional

import numpy as np

from .config import Settings

log = logging.getLogger("voice.speaker")

MOODS: tuple[str, ...] = ("开心", "撒娇", "得意", "紧张", "心疼", "温柔")
MOOD_SUFFIX: dict[str, str] = {
    "开心": "语气轻快，带着笑意。",
    "撒娇": "拖长尾音，软软地撒娇。",
    "得意": "有点小骄傲，语调上扬。",
    "紧张": "此刻很着急，语速加快。",
    "心疼": "带一点委屈的哭腔。",
    "温柔": "放轻声音，慢慢地说。",
}
DEFAULT_MOOD = "温柔"
PUNCT = set("，。！？～…；、,.!?;~")
SPLIT_AT = 20                          # 长于这么多字才分句
LANGUAGE = "Chinese"
TOKENS_PER_SECOND = 12.0               # 12 Hz 编码
PART_SILENCE = 0.05                    # say() 拼句时小句之间的静音（秒）
WARM_TEXT = "你好呀。"

_process_ready = False                 # 阻塞同步、优先级每个进程只设一次


def split(text: str, limit: int = SPLIT_AT) -> list[str]:
    """按中文标点分句（标点跟着前一句），再把短的并起来，每句尽量 ≤ limit 字；不长于 limit 就整句"""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    pieces, buf = [], ""
    for i, ch in enumerate(text):
        buf += ch
        if ch in PUNCT and (i + 1 == len(text) or text[i + 1] not in PUNCT):
            pieces.append(buf)
            buf = ""
    if buf:
        pieces.append(buf)
    out, cur = [], ""
    for p in pieces:
        if cur and len(cur) + len(p) > limit:
            out.append(cur)
            cur = p
        else:
            cur += p
    if cur:
        out.append(cur)
    return [p.strip() for p in out if p.strip()]


def instruct_for(settings: Settings, mood: str) -> str:
    return settings.instruct + MOOD_SUFFIX.get(mood, "")


def _prepare_process(settings: Settings) -> None:
    """阻塞式同步（必须在 CUDA 初始化之前）、低优先级、CPU 线程数"""
    global _process_ready
    if _process_ready:
        return
    _process_ready = True
    import torch
    if settings.blocking_sync and sys.platform == "win32":
        if torch.cuda.is_initialized():
            log.warning("CUDA 已初始化，阻塞式同步没设上")
        else:
            import ctypes
            dll = Path(torch.__file__).parent / "lib" / "cudart64_12.dll"
            try:
                rc = ctypes.WinDLL(str(dll)).cudaSetDeviceFlags(0x04)      # cudaDeviceScheduleBlockingSync
                if rc != 0:
                    log.warning("cudaSetDeviceFlags 返回 %s", rc)
            except OSError as e:
                log.warning("阻塞式同步没设上：%s", e)
    if settings.low_priority and sys.platform == "win32":
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x4000)          # BELOW_NORMAL_PRIORITY_CLASS
    if settings.threads:
        torch.set_num_threads(settings.threads)


def _load_fast(path: str, max_seq_len: int):
    from faster_qwen3_tts import FasterQwen3TTS
    return FasterQwen3TTS.from_pretrained(path, max_seq_len=max_seq_len)


def read_wav(path) -> tuple[np.ndarray, int]:
    import soundfile as sf
    audio, sr = sf.read(str(path), dtype="float32", always_2d=False)
    return np.asarray(audio, dtype=np.float32).reshape(-1), int(sr)


def write_wav(path, audio: np.ndarray, sr: int) -> None:
    """PCM16，先写临时文件再换名"""
    import soundfile as sf
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + f".{os.getpid()}.{threading.get_ident()}.tmp.wav")
    sf.write(str(tmp), audio, sr, subtype="PCM_16")
    os.replace(tmp, path)


class QwenSpeaker:
    """factory(模型路径, max_seq_len) → 模型；默认是 FasterQwen3TTS（测试换成假模型）"""

    def __init__(self, settings: Optional[Settings] = None, factory: Optional[Callable] = None):
        self.settings = settings or Settings()
        self._factory = factory
        self._model = None
        self._lock = threading.Lock()
        self.cache_dir = Path(self.settings.cache_dir)
        self.model_name = Path(self.settings.model).name

    # ----------------------------------------------------------------------------------------------------- 模型

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def warm(self) -> None:
        with self._lock:
            self._ensure_model()

    def _ensure_model(self):
        """持锁调用：载入并热身（第一次合成抓 CUDA 图，约 10 秒）"""
        if self._model is not None:
            return self._model
        t = time.perf_counter()
        if self._factory is None:
            _prepare_process(self.settings)
            model = _load_fast(self.settings.model, self.settings.max_seq_len)
        else:
            model = self._factory(self.settings.model, self.settings.max_seq_len)
        loaded = time.perf_counter() - t
        self._generate(model, WARM_TEXT, self.settings.instruct)
        self._model = model
        log.info("声音模型就绪：载入 %.1f 秒，热身 %.1f 秒", loaded, time.perf_counter() - t - loaded)
        return model

    def _generate(self, model, text: str, instruct: str):
        return model.generate_custom_voice(text=text, speaker=self.settings.speaker, language=LANGUAGE,
                                           instruct=instruct, max_new_tokens=self.settings.max_new_tokens)

    def release(self) -> None:
        with self._lock:
            if self._model is None:
                return
            self._model = None
            gc.collect()
            torch = sys.modules.get("torch")
            if torch is not None and torch.cuda.is_available():
                torch.cuda.empty_cache()
            log.info("声音模型已释放")

    # ----------------------------------------------------------------------------------------------------- 缓存

    def cache_path(self, text: str, mood: str) -> Path:
        key = "\n".join((self.model_name, self.settings.speaker, instruct_for(self.settings, mood), text))
        return self.cache_dir / (hashlib.sha1(key.encode("utf-8")).hexdigest() + ".wav")

    def _synth(self, text: str, mood: str) -> tuple[np.ndarray, int]:
        """一小句：先查缓存，没有就合成并存下"""
        path = self.cache_path(text, mood)
        if path.exists():
            try:
                return read_wav(path)
            except Exception as e:                                      # 坏文件：重合成
                log.warning("缓存读不了，重合成：%s（%s）", path.name, e)
        with self._lock:
            model = self._ensure_model()
            t = time.perf_counter()
            wavs, sr = self._generate(model, text, instruct_for(self.settings, mood))
            secs = time.perf_counter() - t
        audio = np.asarray(wavs[0], dtype=np.float32).reshape(-1)
        dur = len(audio) / sr if sr else 0.0
        limit = self.settings.max_new_tokens / TOKENS_PER_SECOND
        if dur >= limit - 0.5:
            log.warning("合成 %.1f 秒，到了上限 %.1f 秒，可能被截断：%s", dur, limit, text)
        log.debug("合成 %.2f 秒音频用 %.2f 秒：%s", dur, secs, text)
        write_wav(path, audio, int(sr))
        return audio, int(sr)

    # ----------------------------------------------------------------------------------------------------- 对外

    def parts(self, text: str, mood: str = DEFAULT_MOOD) -> Iterator[tuple[np.ndarray, int]]:
        """逐小句交出 (音频, 采样率)，第一句合成好就交"""
        for piece in split(text):
            yield self._synth(piece, mood)

    def say(self, text: str, mood: str = DEFAULT_MOOD) -> tuple[np.ndarray, int]:
        """整句（小句之间留一点静音）"""
        return self.join(list(self.parts(text, mood)))

    def synth_to_file(self, text: str, mood: str, path) -> float:
        """整句写到 path（PCM16），返回秒数"""
        audio, sr = self.say(text, mood)
        write_wav(path, audio, sr)
        return len(audio) / sr if sr else 0.0

    @staticmethod
    def join(chunks: list[tuple[np.ndarray, int]]) -> tuple[np.ndarray, int]:
        if not chunks:
            return np.zeros(0, dtype=np.float32), 24000
        sr = chunks[0][1]
        gap = np.zeros(int(sr * PART_SILENCE), dtype=np.float32)
        out = []
        for i, (a, _) in enumerate(chunks):
            if i:
                out.append(gap)
            out.append(a)
        return np.concatenate(out).astype(np.float32, copy=False), sr


__all__ = ["QwenSpeaker", "MOODS", "MOOD_SUFFIX", "DEFAULT_MOOD", "split", "instruct_for", "read_wav", "write_wav"]
