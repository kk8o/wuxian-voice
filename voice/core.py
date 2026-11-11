"""声音的本体：合成（speaker）+ 句子组（pools）+ 播放队列（player）。不认 SDK：ext.py 把它接成扩展的能力。

- say：现合成现说，第一个小句合成好就排进播放，整句合成完才回答（回 seconds）。
- prepare：把一组句子预先合成好存起来；play：从组里挑一句最近没放过的马上排进播放。
- 同时只放一句；interrupt 打断正在说的、清空队列。静音时 say、play 什么都不做（回 muted: true）。
- 一句话放完（或被打断）发来源 said {text, pool?, seconds, cut?}。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from typing import Any, Callable, Optional

from .config import Settings
from .player import Clip, Line, SoundPlayer
from .pools import CAP, Pools, key_of
from .speaker import DEFAULT_MOOD, MOODS, QwenSpeaker, read_wav, split

log = logging.getLogger("voice")

MAX_TEXT = 80                          # 一次 say 的上限（字，含标点）
STALE = 20.0                           # 排队超过这么多秒还没开始说就不说了
RECENT = 8                             # status 里列最近说过的几句


class VoiceError(Exception):
    """给 Agent 看的失败原因"""


_ASCII = {",": "，", "!": "！", "?": "？", ";": "；", ":": "："}


def clean(text: Any) -> str:
    """整理成能念的样子：去掉成对的外引号，波浪号、省略号统一，英文标点换中文"""
    t = str(text or "").strip()
    while len(t) >= 2 and t[0] in "\"'“「『" and t[-1] in "\"'”」』" and not re.search("[\"'“”「」『』]", t[1:-1]):
        t = t[1:-1].strip()
    t = re.sub(r"\.{2,}|。{2,}|…+", "……", t)
    t = re.sub(r"[~～]+(?=\s*$|[，。！？、；：…])", "", t)
    t = re.sub(r"[~～]+", "，", t)
    for a, b in _ASCII.items():
        t = t.replace(a, b)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _mood(mood: Any) -> str:
    return mood if mood in MOODS else DEFAULT_MOOD


class Voice:
    def __init__(self, settings: Optional[Settings] = None, *, speaker: Optional[QwenSpeaker] = None,
                 backend=None, pools: Optional[Pools] = None, emit: Optional[Callable[[dict], None]] = None,
                 stale: float = STALE):
        self.settings = settings or Settings()
        self.speaker = speaker or QwenSpeaker(self.settings)
        self.pools = pools or Pools(self.settings.pools_dir, self.settings.bank_dir)
        self.player = SoundPlayer(self.settings, backend=backend, on_end=self._ended)
        self.emit = emit
        self.stale = stale
        self.muted = False
        self.loading = False
        self.error = ""
        self._recent: deque[dict] = deque(maxlen=RECENT)
        self._lock = threading.Lock()
        self._prep = threading.Lock()          # prepare 一次一个（写组文件）

    # ------------------------------------------------------------------------------------------------- 生命周期

    def setup(self) -> None:
        """载入模型并热身（扩展 initialize 时调用）；失败只记下，status 里看得到，合成时再试"""
        self.loading = True
        try:
            self.speaker.warm()
            self.error = ""
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            log.exception("声音模型载入失败")
        finally:
            self.loading = False

    def close(self) -> None:
        self.player.close()

    def set_volume(self, value: Any) -> None:
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        self.settings.volume = max(0.0, min(100.0, v)) / 100.0

    # ------------------------------------------------------------------------------------------------- 能力

    def say(self, text: Any, mood: Any = None, interrupt: bool = False) -> dict:
        t = clean(text)
        if not t:
            raise VoiceError("text 是空的")
        if len(t) > MAX_TEXT:
            raise VoiceError(f"太长（{len(t)} 字，上限 {MAX_TEXT}）：分成几次说，或说短一点")
        if self.muted:
            return {"muted": True, "seconds": 0}
        m = _mood(mood)
        pieces = split(t)
        line = Line(text=t, mood=m, parts=len(pieces), deadline=time.monotonic() + self.stale)
        total = 0.0
        try:
            for i, (audio, sr) in enumerate(self.speaker.parts(t, m)):
                if line.cancelled or self.muted:
                    break
                clip = Clip(line=line, audio=audio, sr=sr, index=i)
                total += clip.seconds
                self.player.play(clip, interrupt=bool(interrupt) and i == 0)
        except Exception as e:
            line.cancelled = True
            raise VoiceError(self._synth_failed(e)) from e
        out = {"seconds": round(total, 2)}
        if line.cancelled:
            out["cut"] = True
        return out

    def prepare(self, pool: Any, lines: Any, replace: bool = False) -> dict:
        if not Pools.valid(pool):
            raise VoiceError("pool 只能用小写字母、数字和 _（字母或数字开头，最多 48 个）")
        if not isinstance(lines, (list, tuple)):
            raise VoiceError("lines 是句子的列表：[{text, mood?}]")
        new = []
        for x in lines:
            text, mood = (x.get("text"), x.get("mood")) if isinstance(x, dict) else (x, None)
            t = clean(text)
            if not t:
                continue
            if len(t) > MAX_TEXT:
                raise VoiceError(f"太长（{len(t)} 字，上限 {MAX_TEXT}）：{t}")
            new.append({"text": t, "mood": _mood(mood), "audio": ""})
        with self._prep:
            have = [] if replace else self.pools.lines(pool)
            seen = {key_of(x["text"]) for x in have}
            added = 0
            for x in new:
                if key_of(x["text"]) not in seen:
                    seen.add(key_of(x["text"]))
                    have.append(x)
                    added += 1
            if len(have) > CAP:
                del have[:len(have) - CAP]
            if not have:
                raise VoiceError(f"组「{pool}」里没有句子：给 lines")
            voiced = 0
            t0 = time.perf_counter()
            for x in have:
                if self.pools.voiced(x):
                    continue
                path = self.pools.audio_path(pool, x["text"], _mood(x["mood"]))
                try:
                    if not path.exists():
                        self.speaker.synth_to_file(x["text"], _mood(x["mood"]), path)
                        voiced += 1
                except Exception as e:
                    self.pools.save(pool, have)          # 已合成的留下
                    raise VoiceError(self._synth_failed(e)) from e
                x["audio"] = self.pools.rel(path)
            self.pools.save(pool, have)
        return {"pool": pool, "lines": len(have), "added": added, "voiced": voiced,
                "secs": round(time.perf_counter() - t0, 1)}

    def play(self, pool: Any, interrupt: bool = False) -> dict:
        if not Pools.valid(pool):
            raise VoiceError(f"没有组「{pool}」")
        if self.muted:
            return {"muted": True, "seconds": 0}
        x = self.pools.pick(pool)
        if x is None:
            raise VoiceError(f"组「{pool}」是空的或没有：先 prepare 它（pools 列出有哪些组）")
        mood = _mood(x["mood"])
        try:
            p = self.pools.abs(x["audio"])
            if p is not None and p.exists():
                audio, sr = read_wav(p)
            else:                                        # 旧句库的句子还没声音：现合成（多半命中合成缓存）
                audio, sr = self.speaker.say(x["text"], mood)
        except Exception as e:
            raise VoiceError(self._synth_failed(e)) from e
        line = Line(text=x["text"], mood=mood, pool=pool, deadline=time.monotonic() + self.stale)
        clip = Clip(line=line, audio=audio, sr=sr)
        self.player.play(clip, interrupt=bool(interrupt))
        return {"text": x["text"], "mood": mood, "seconds": round(clip.seconds, 2)}

    def list_pools(self) -> dict:
        out = []
        for name in self.pools.names():
            items = self.pools.lines(name)
            out.append({"pool": name, "lines": len(items), "voiced": sum(self.pools.voiced(x) for x in items)})
        return {"pools": out}

    def stop(self) -> dict:
        return {"stopped": self.player.stop()}

    def mute(self, on: Any) -> dict:
        self.muted = bool(on)
        if self.muted:
            self.player.stop()
        return {"muted": self.muted}

    def status(self) -> dict:
        cur = self.player.current
        out = {
            "ready": self.speaker.loaded,
            "loading": self.loading,
            "muted": self.muted,
            "volume": round(self.settings.volume * 100),
            "speaking": cur.line.text if cur is not None else None,
            "queue": [x.text for x in self.player.queued()],
            "pools": len(self.pools.names()),
            "recent": list(self._recent),
        }
        if self.error:
            out["error"] = self.error
        return out

    # ------------------------------------------------------------------------------------------------- 内部

    def _synth_failed(self, e: Exception) -> str:
        return f"合成失败：{type(e).__name__}: {e}"

    def _ended(self, clip: Clip, cut: bool) -> None:
        """播放线程：一句话的最后一个小句放完，或被打断，发 said（每句话一次）"""
        line = clip.line
        if line.reported or not (cut or clip.last):
            return
        line.reported = True
        data: dict = {"text": line.text, "seconds": round(line.seconds, 2)}
        if line.pool:
            data["pool"] = line.pool
        if cut:
            data["cut"] = True
        self._recent.append({**data, "at": int(time.time())})
        if self.emit is not None:
            try:
                self.emit(data)
            except Exception:
                log.exception("发 said 出错")


__all__ = ["Voice", "VoiceError", "clean", "MAX_TEXT"]
