"""voice/speaker.py：假模型测 分句、情绪后缀、缓存（键与旧版一致）、锁、release；VOICE_GPU_TEST=1 时跑真模型冒烟。"""
import hashlib
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np

from fakes import SR, FakeTTS

from voice.config import Settings
from voice.speaker import MOOD_SUFFIX, MOODS, QwenSpeaker, split


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(cache_dir=str(Path(self.tmp.name) / "voice"))
        self.models = []

    def speaker(self, delay=0.0):
        def factory(path, max_seq_len):
            self.assertEqual((path, max_seq_len), (self.settings.model, self.settings.max_seq_len))
            m = FakeTTS(delay)
            self.models.append(m)
            return m
        return QwenSpeaker(self.settings, factory=factory)

    def said(self):
        """热身那句以外的合成"""
        return [c for m in self.models for c in m.calls[1:]]


class Split(unittest.TestCase):
    def test_short_stays_whole(self):
        self.assertEqual(split("小心小心！好多怪在打你，快拉开呀！"), ["小心小心！好多怪在打你，快拉开呀！"])

    def test_long_splits_at_punctuation_and_merges_short_pieces(self):
        text = "叮！升到二十三级啦，你也太厉害了吧，奖励你一个抱抱好不好嘛～"
        parts = split(text)
        self.assertEqual(parts, ["叮！升到二十三级啦，你也太厉害了吧，", "奖励你一个抱抱好不好嘛～"])
        self.assertEqual("".join(parts), text)

    def test_runs_of_punctuation_stay_together(self):
        parts = split("呜呜你怎么倒下了嘛……没关系没关系，我陪你跑尸，我们一起回去好不好？")
        self.assertEqual(parts[0], "呜呜你怎么倒下了嘛……没关系没关系，")
        self.assertFalse(any(p[0] in "…，" for p in parts))

    def test_no_punctuation_is_left_alone(self):
        self.assertEqual(split("一" * 30), ["一" * 30])
        self.assertEqual(split("  "), [])


class Speaking(Base):
    def test_every_mood_has_a_suffix_after_the_base_instruct_and_the_voice_is_serena(self):
        self.assertEqual(set(MOOD_SUFFIX), set(MOODS))
        sp = self.speaker()
        for mood in MOODS:
            sp.say(f"{mood}的一句话。", mood)
        for call, mood in zip(self.said(), MOODS):
            self.assertEqual(call["instruct"], self.settings.instruct + MOOD_SUFFIX[mood])
            self.assertEqual((call["speaker"], call["language"], call["max_new_tokens"]), ("Serena", "Chinese", 150))

    def test_cache_key_is_the_old_companions(self):
        """旧版陪玩（custom 模式）的键：模型名、Serena、instruct + 情绪后缀、文字；cache/voice 里的旧缓存照样命中"""
        sp = self.speaker()
        text, mood = "来了，我盯着呢。", "紧张"
        key = "\n".join(("Qwen3-TTS-12Hz-0.6B-CustomVoice", "Serena", self.settings.instruct + MOOD_SUFFIX[mood], text))
        self.assertEqual(sp.cache_path(text, mood).name, hashlib.sha1(key.encode("utf-8")).hexdigest() + ".wav")

    def test_parts_yield_one_piece_per_short_sentence_and_say_joins_them(self):
        sp = self.speaker()
        text = "叮！升到二十三级啦，你也太厉害了吧，奖励你一个抱抱好不好嘛～"
        parts = list(sp.parts(text, "开心"))
        self.assertEqual(len(parts), 2)
        self.assertAlmostEqual(len(parts[0][0]) / SR, 18 * 0.2, places=2)
        audio, sr = sp.say(text, "开心")
        self.assertAlmostEqual(len(audio) / sr, 30 * 0.2 + 0.05, places=2)
        self.assertEqual(audio.dtype, np.float32)

    def test_parts_hand_over_the_first_before_the_second_is_made(self):
        sp = self.speaker()
        it = sp.parts("叮！升到二十三级啦，你也太厉害了吧，奖励你一个抱抱好不好嘛～")
        next(it)
        self.assertEqual([c["text"] for c in self.said()], ["叮！升到二十三级啦，你也太厉害了吧，"])

    def test_cache_hit_skips_the_model_even_in_a_new_speaker(self):
        sp = self.speaker()
        a, _ = sp.say("你好呀，我在呢。")
        b, _ = self.speaker().say("你好呀，我在呢。")          # 新的 Speaker：缓存命中不载入模型
        self.assertEqual(len(self.models), 1)
        self.assertLess(np.max(np.abs(a - b)), 1e-3)          # PCM16
        sp.say("你好呀，我在呢。", "心疼")                      # 换情绪 = 换 instruct：另算
        self.assertEqual(len(self.said()), 2)
        self.assertEqual(len(list(Path(self.settings.cache_dir).glob("*.wav"))), 2)

    def test_synth_to_file_writes_the_whole_line_as_pcm16(self):
        import soundfile as sf
        sp = self.speaker()
        out = Path(self.tmp.name) / "pools" / "level" / "x.wav"
        secs = sp.synth_to_file("叮！升到二十三级啦，你也太厉害了吧，奖励你一个抱抱好不好嘛～", "得意", out)
        self.assertAlmostEqual(secs, 30 * 0.2 + 0.05, places=2)
        info = sf.info(str(out))
        self.assertEqual((info.samplerate, info.channels, info.subtype), (SR, 1, "PCM_16"))
        self.assertAlmostEqual(info.duration, secs, places=2)

    def test_too_long_audio_warns_about_truncation(self):
        sp = self.speaker()
        with self.assertLogs("voice.speaker", "WARNING") as logs:
            sp.say("一" * 60)                                   # 12 秒
        self.assertIn("截断", "".join(logs.output))

    def test_one_thread_at_a_time_in_the_model(self):
        sp = self.speaker(delay=0.05)
        texts = [f"第{i}句话。" for i in range(6)]
        threads = [threading.Thread(target=sp.say, args=(t,)) for t in texts]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.models), 1)
        self.assertEqual(self.models[0].most, 1)
        self.assertEqual(sorted(c["text"] for c in self.said()), sorted(texts))


class Lifecycle(Base):
    def test_warm_loads_once_and_says_a_short_line(self):
        sp = self.speaker()
        self.assertFalse(sp.loaded)
        sp.warm()
        sp.warm()
        self.assertTrue(sp.loaded)
        self.assertEqual([c["text"] for c in self.models[0].calls], ["你好呀。"])

    def test_release_drops_the_model_and_the_next_line_loads_again(self):
        sp = self.speaker()
        sp.warm()
        sp.release()
        self.assertFalse(sp.loaded)
        sp.say("又见面啦。")
        self.assertEqual(len(self.models), 2)


@unittest.skipUnless(os.environ.get("VOICE_GPU_TEST") == "1", "真模型：设 VOICE_GPU_TEST=1")
class RealModel(unittest.TestCase):
    def test_smoke(self):
        with tempfile.TemporaryDirectory() as tmp:
            sp = QwenSpeaker(Settings(cache_dir=tmp))
            t = time.perf_counter()
            sp.warm()
            warm = time.perf_counter() - t
            t = time.perf_counter()
            audio, sr = sp.say("你好呀，我在呢。", "温柔")
            secs = time.perf_counter() - t
            sp.release()
        print(f"\n真模型：载入 + 热身 {warm:.1f} 秒，合成 {secs:.2f} 秒，音频 {len(audio) / sr:.2f} 秒", flush=True)
        self.assertEqual(sr, SR)
        self.assertTrue(1.0 <= len(audio) / sr <= 5.0)


if __name__ == "__main__":
    unittest.main()
