"""声音的设置：代码里的默认值，可选的 voice.local.toml（不进仓库）的 [voice] 覆盖；音量由工坊的选项 volume 管。

声音是 Serena：官方 CustomVoice 0.6B 的 speaker。0.6B 会忽略 instruct，念的是 Serena 原声；
instruct 与情绪后缀仍进缓存键，改了 instruct，已合成的缓存（cache/voice）就不再命中。
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Settings:
    model: str = str(ROOT / "models" / "Qwen3-TTS-12Hz-0.6B-CustomVoice")
    speaker: str = "Serena"
    instruct: str = "年轻女声，语气亲切活泼，音调自然有起伏。"
    # 实测（开着游戏）：max_seq_len 256 比 1024 少 3 GB 显存、不慢；一句最多约 200 个 token
    # （提示 ~100 + 音频 12 Hz × 8 秒），超出会截断，所以长句按标点分句合成。
    # 阻塞式同步让 CPU 从每秒音频 2 秒降到 0.1 秒，不变慢；低优先级无害。
    max_seq_len: int = 256
    max_new_tokens: int = 150          # 12 Hz：约 12 秒音频，留余量
    blocking_sync: bool = True         # cudaDeviceScheduleBlockingSync：等显卡时睡眠，不空转一个 CPU 核
    low_priority: bool = True          # 进程低于正常优先级，让游戏先用 CPU
    threads: int = 0                   # torch CPU 线程，0 = 默认
    cache_dir: str = str(ROOT / "cache" / "voice")      # 合成缓存（按模型、speaker、instruct、文字的哈希）
    pools_dir: str = str(ROOT / "cache" / "pools")      # 句子组：<组>.jsonl + <组>/*.wav
    bank_dir: str = str(ROOT / "cache" / "bank")        # 旧版句库（只读）：<场景>.jsonl 当成同名的组
    volume: float = 1.0                # 0–1；工坊选项 volume（0–100）
    loudness: float = 0.9              # 每句拉到这个峰值（0 = 不拉平）；实测合成的峰值中位只有 0.34
    device: str = ""                   # 播放设备名的一部分，空 = 系统默认


def load(root: Path = ROOT) -> Settings:
    s = Settings()
    p = root / "voice.local.toml"
    if p.exists():
        for k, v in (tomllib.loads(p.read_text(encoding="utf-8")).get("voice") or {}).items():
            if hasattr(s, k):
                setattr(s, k, v)
    return s


__all__ = ["Settings", "load", "ROOT"]
