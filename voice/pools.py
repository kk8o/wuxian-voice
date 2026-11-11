"""句子组：预先合成好的一组句子，反射（或 Agent）要即时说一句时从组里挑。

- 存储：cache/pools/<组>.jsonl，每行 {text, mood, audio}；audio 是相对扩展文件夹的路径（cache/pools/<组>/<哈希>.wav），
  空 = 还没合成。
- 旧版陪玩的句库 cache/bank/<场景>.jsonl（{text, mood, audio, uses}）当成同名的组读：只读，不改、不删；
  这个组第一次 prepare 时连同旧句子一起写成 cache/pools/<组>.jsonl，之后以它为准。
- pick：挑最近几次没放过、放得最少的一句（放过几次只记在内存里）。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import threading
from collections import deque
from pathlib import Path
from typing import Iterable, Optional

from .config import ROOT

NAME = re.compile(r"^[a-z0-9][a-z0-9_]{0,47}$")
CAP = 60                               # 一组最多留几句（多了丢最早的）
RECENT = 5                             # 最近几次放过的不再挑（组里句子少时按句数减一）


def key_of(text: str) -> str:
    """查重用：去掉标点和空白"""
    return re.sub(r"[\s，。！？、；：…—「」『』“”《》,.!?]", "", text)


class Pools:
    def __init__(self, dir, legacy=None, *, root=ROOT, rng: Optional[random.Random] = None, recent: int = RECENT):
        self.dir = Path(dir)
        self.legacy = Path(legacy) if legacy else None
        self.root = Path(root)
        self.rng = rng or random.Random()
        self._recent_n = recent
        self._recent: dict[str, deque[str]] = {}
        self._plays: dict[str, dict[str, int]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def valid(name: str) -> bool:
        return isinstance(name, str) and bool(NAME.match(name))

    def file(self, name: str) -> Path:
        return self.dir / (name + ".jsonl")

    def _source(self, name: str) -> Optional[Path]:
        p = self.file(name)
        if p.exists():
            return p
        if self.legacy is not None:
            q = self.legacy / (name + ".jsonl")
            if q.exists():
                return q
        return None

    def names(self) -> list[str]:
        out = set()
        for d in (self.dir, self.legacy):
            if d is not None and d.is_dir():
                out.update(p.stem for p in d.glob("*.jsonl") if self.valid(p.stem))
        return sorted(out)

    def lines(self, name: str) -> list[dict]:
        """组里的句子 [{text, mood, audio}]；没有这个组 = []"""
        if not self.valid(name):
            return []
        p = self._source(name)
        if p is None:
            return []
        items, seen = [], set()
        with self._lock:
            for raw in p.read_text(encoding="utf-8").splitlines():
                try:
                    x = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(x, dict) or not isinstance(x.get("text"), str) or not x["text"].strip():
                    continue
                if key_of(x["text"]) in seen:
                    continue
                seen.add(key_of(x["text"]))
                items.append({"text": x["text"].strip(), "mood": str(x.get("mood") or ""),
                              "audio": str(x.get("audio") or "")})
        return items

    def save(self, name: str, items: Iterable[dict]) -> None:
        p = self.file(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        with self._lock:
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                for x in items:
                    f.write(json.dumps({"text": x["text"], "mood": x.get("mood") or "", "audio": x.get("audio") or ""},
                                       ensure_ascii=False) + "\n")
            os.replace(tmp, p)

    # --------------------------------------------------------------------------------------------------- 声音

    def audio_path(self, name: str, text: str, mood: str) -> Path:
        h = hashlib.sha1(f"{mood}\n{text}".encode("utf-8")).hexdigest()[:16]
        return self.dir / name / (h + ".wav")

    def rel(self, p: Path) -> str:
        try:
            return Path(p).resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return str(p)

    def abs(self, audio: str) -> Optional[Path]:
        if not audio:
            return None
        p = Path(audio)
        return p if p.is_absolute() else self.root / p

    def voiced(self, item: dict) -> bool:
        p = self.abs(item.get("audio") or "")
        return p is not None and p.exists()

    # --------------------------------------------------------------------------------------------------- 挑

    def pick(self, name: str) -> Optional[dict]:
        """挑一句并记下放过；组空或没有 = None"""
        items = self.lines(name)
        if not items:
            return None
        with self._lock:
            recent = self._recent.setdefault(name, deque(maxlen=self._recent_n))
            plays = self._plays.setdefault(name, {})
            keep = min(len(recent), max(0, len(items) - 1))
            avoid = set(list(recent)[len(recent) - keep:]) if keep else set()
            cands = [x for x in items if key_of(x["text"]) not in avoid] or items
            low = min(plays.get(key_of(x["text"]), 0) for x in cands)
            x = self.rng.choice([c for c in cands if plays.get(key_of(c["text"]), 0) == low])
            k = key_of(x["text"])
            plays[k] = plays.get(k, 0) + 1
            recent.append(k)
            return dict(x)


__all__ = ["Pools", "key_of", "CAP", "NAME"]
