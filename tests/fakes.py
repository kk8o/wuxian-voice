"""测试用的替身：假 TTS 模型、假播放后端、按约定第五节 API 写的最小 SDK 替身（不起进程、不出声音、不载入真模型）。"""
import sys
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SR = 24000


class FakeTTS:
    """每字 0.2 秒（按 per_char 改）正弦波；记下调用与同时在合成的最大数"""

    def __init__(self, delay=0.0, per_char=0.2):
        self.calls = []
        self.delay = delay
        self.per_char = per_char
        self.inside = 0
        self.most = 0
        self._lock = threading.Lock()

    def generate_custom_voice(self, text, speaker, language, instruct=None, max_new_tokens=2048):
        with self._lock:
            self.inside += 1
            self.most = max(self.most, self.inside)
        time.sleep(self.delay)
        self.calls.append(dict(text=text, speaker=speaker, language=language, instruct=instruct,
                               max_new_tokens=max_new_tokens))
        n = int(len(text) * self.per_char * SR)
        wav = (0.3 * np.sin(np.arange(n) * 2 * np.pi * 440 / SR)).astype(np.float32)
        with self._lock:
            self.inside -= 1
        return [wav], SR


class FakeBackend:
    """按时长「放」（speed 倍速）：cancel 设上就提前返回；记下每段的样本数、开始、结束、是否被打断、峰值"""

    def __init__(self, speed=1.0):
        self.log = []
        self.speed = speed
        self.lock = threading.Lock()

    def play(self, audio, sr, cancel):
        start = time.monotonic()
        cut = cancel.wait(len(audio) / sr / self.speed)
        with self.lock:
            self.log.append(dict(n=len(audio), start=start, end=time.monotonic(), cut=cut,
                                 peak=float(np.max(np.abs(audio))) if len(audio) else 0.0))

    def names(self):
        with self.lock:
            return [e["n"] for e in self.log]


# ------------------------------------------------------------------------------------------------ SDK 替身
# 无限工坊的 sdk/python/wuxiankit.py（Extension）正在另一个工作包里写；这里按 contract3.md 第五节的 API 写一个最小替身，
# 只够登记与按名字调用。


class _Option:
    def __init__(self, spec):
        self.spec = spec
        self.value = spec["default"]

    def get(self):
        return self.value


class _Source:
    def __init__(self, ext, spec):
        self.ext = ext
        self.spec = spec

    def emit(self, data):
        self.ext.events.append({"source": self.spec["name"], "data": data})


class FakeExtension:
    def __init__(self):
        self.options, self.sources, self.caps = {}, {}, {}
        self.events = []
        self._on_options = []
        self.ran = False

    def option(self, name, *, kind, default, title, **more):
        opt = _Option({"name": name, "kind": kind, "default": default, "title": title, **more})
        self.options[name] = opt
        return opt

    def on_options(self, fn):
        self._on_options.append(fn)

    def source(self, name, *, title, doc=""):
        src = _Source(self, {"name": name, "title": title, "doc": doc})
        self.sources[name] = src
        return src

    def _cap(self, kind, name, *, title, doc, args, timeout=None):
        def deco(fn):
            self.caps[name] = {"name": name, "kind": kind, "title": title, "doc": doc, "args": args,
                               "timeout": timeout, "fn": fn}
            return fn
        return deco

    def see(self, name, **kw):
        return self._cap("see", name, **kw)

    def keep(self, name, **kw):
        return self._cap("keep", name, **kw)

    def point(self, name, **kw):
        return self._cap("point", name, **kw)

    def run(self, setup=None):
        if setup is not None:
            setup()
        self.ran = True

    # 工坊那一侧
    def set_options(self, values):
        for k, v in values.items():
            self.options[k].value = v
        for fn in self._on_options:
            fn({k: o.value for k, o in self.options.items()})

    def call(self, name, args=None):
        return self.caps[name]["fn"](**(args or {}))
