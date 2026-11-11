"""voice/core.py：替身合成（假 TTS）+ 假播放后端：say 的队列与打断、prepare / play 与旧句库、stop、mute、来源 said、status。"""
import json
import random
import tempfile
import time
import unittest
from pathlib import Path

from fakes import FakeBackend, FakeTTS

from voice.config import Settings
from voice.core import MAX_TEXT, Voice, VoiceError, clean
from voice.pools import Pools
from voice.speaker import QwenSpeaker

LONG = "一" * 40 + "。"          # 假 TTS 下约 0.8 秒


def failing_factory(*_):
    raise OSError("没有模型")


class Base(unittest.TestCase):
    per_char = 0.02          # 假 TTS：每字 0.02 秒音频

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.settings = Settings(cache_dir=str(self.root / "cache" / "voice"),
                                 pools_dir=str(self.root / "cache" / "pools"),
                                 bank_dir=str(self.root / "cache" / "bank"))
        self.models = []

        def factory(path, max_seq_len):
            m = FakeTTS(per_char=self.per_char)
            self.models.append(m)
            return m

        self.backend = FakeBackend()
        self.events = []
        pools = Pools(self.settings.pools_dir, self.settings.bank_dir, root=self.root, rng=random.Random(3))
        self.voice = Voice(self.settings, speaker=QwenSpeaker(self.settings, factory=factory), backend=self.backend,
                           pools=pools, emit=self.events.append)
        self.addCleanup(self.voice.close)

    def synths(self):
        """热身以外的合成"""
        return [c["text"] for m in self.models for c in m.calls[1:]]

    def wait(self, t=5):
        self.assertTrue(self.voice.player.wait(t))


class Setup(Base):
    def test_setup_loads_and_warms_the_model(self):
        self.assertFalse(self.voice.status()["ready"])
        self.voice.setup()
        st = self.voice.status()
        self.assertTrue(st["ready"])
        self.assertFalse(st["loading"])
        self.assertNotIn("error", st)
        self.assertEqual([c["text"] for c in self.models[0].calls], ["你好呀。"])

    def test_a_failed_load_is_kept_for_status_and_say_reports_it(self):
        v = Voice(self.settings, speaker=QwenSpeaker(self.settings, factory=failing_factory), backend=self.backend)
        self.addCleanup(v.close)
        with self.assertLogs("voice", "ERROR"):
            v.setup()
        self.assertIn("没有模型", v.status()["error"])
        with self.assertRaises(VoiceError) as e:
            v.say("你好呀。")
        self.assertIn("合成失败", str(e.exception))


class Saying(Base):
    def test_say_answers_the_seconds_and_said_follows_when_it_has_played(self):
        out = self.voice.say("你好呀，我在呢。", "温柔")
        self.assertAlmostEqual(out["seconds"], 8 * self.per_char, places=2)
        self.wait()
        self.assertEqual(self.events, [{"text": "你好呀，我在呢。", "seconds": out["seconds"]}])
        self.assertEqual(self.voice.status()["recent"][0]["text"], "你好呀，我在呢。")

    def test_a_long_line_goes_out_in_parts_but_said_comes_once(self):
        text = "叮！升到二十三级啦，你也太厉害了吧，奖励你一个抱抱好不好嘛"
        self.voice.say(text, "开心")
        self.wait()
        self.assertEqual(len(self.backend.log), 2)
        self.assertEqual([e["text"] for e in self.events], [text])

    def test_lines_queue_one_at_a_time(self):
        self.voice.say(LONG)
        self.voice.say("第二句话。")
        time.sleep(0.05)
        st = self.voice.status()
        self.assertEqual(st["speaking"], LONG)
        self.assertEqual(st["queue"], ["第二句话。"])
        self.wait()
        self.assertEqual([e["text"] for e in self.events], [LONG, "第二句话。"])
        log = self.backend.log
        self.assertGreaterEqual(log[1]["start"], log[0]["end"])

    def test_interrupt_cuts_off_and_drops_the_queue(self):
        self.voice.say(LONG)
        self.voice.say("排着的一句。")
        time.sleep(0.1)
        self.voice.say("快躲开！", "紧张", interrupt=True)
        self.wait()
        self.assertEqual([(e["text"], e.get("cut", False)) for e in self.events], [(LONG, True), ("快躲开！", False)])

    def test_text_is_cleaned_and_checked(self):
        self.assertEqual(clean("“好呀~”"), "好呀")
        self.assertEqual(clean("等等我...好嘛~我来啦"), "等等我……好嘛，我来啦")
        with self.assertRaises(VoiceError):
            self.voice.say("   ")
        with self.assertRaises(VoiceError):
            self.voice.say("一" * (MAX_TEXT + 1))

    def test_an_unknown_mood_is_gentle(self):
        self.voice.say("你好呀。", "生气")
        self.assertTrue(self.models[0].calls[1]["instruct"].endswith("放轻声音，慢慢地说。"))


class Muting(Base):
    def test_mute_stops_now_and_keeps_say_and_play_silent_until_unmuted(self):
        self.voice.prepare("idle", [{"text": "陪我一会儿嘛。", "mood": "撒娇"}])
        self.voice.say(LONG)
        time.sleep(0.05)
        self.assertEqual(self.voice.mute(True), {"muted": True})
        self.assertEqual(self.voice.say("还说吗？"), {"muted": True, "seconds": 0})
        self.assertEqual(self.voice.play("idle"), {"muted": True, "seconds": 0})
        self.wait()
        self.assertEqual(len(self.backend.log), 1)
        self.assertTrue(self.backend.log[0]["cut"])
        self.assertNotIn("还说吗？", self.synths())
        self.assertTrue(self.voice.status()["muted"])
        self.voice.mute(False)
        self.voice.play("idle")
        self.wait()
        self.assertEqual(len(self.backend.log), 2)

    def test_stop_cuts_and_clears_but_later_lines_play(self):
        self.voice.say(LONG)
        self.voice.say("排着的。")
        time.sleep(0.05)
        self.assertEqual(self.voice.stop(), {"stopped": 2})
        self.wait()
        self.voice.say("又来啦。")
        self.wait()
        self.assertEqual([e["text"] for e in self.events], [LONG, "又来啦。"])

    def test_volume_option_maps_0_100(self):
        self.voice.set_volume(40)
        self.assertAlmostEqual(self.settings.volume, 0.4)
        self.voice.set_volume(250)
        self.assertEqual(self.voice.status()["volume"], 100)
        self.voice.set_volume("x")
        self.assertEqual(self.voice.status()["volume"], 100)


class PoolsAndPlay(Base):
    def test_prepare_makes_the_sound_ahead_and_play_does_not_synthesize(self):
        out = self.voice.prepare("kill_rare", [{"text": "拿下啦！", "mood": "得意"}, "稀有的呀。", {"text": " "}])
        self.assertEqual({k: out[k] for k in ("pool", "lines", "added", "voiced")},
                         {"pool": "kill_rare", "lines": 2, "added": 2, "voiced": 2})
        raw = (self.root / "cache/pools/kill_rare.jsonl").read_text(encoding="utf-8")
        rows = [json.loads(x) for x in raw.splitlines()]
        self.assertEqual([(r["text"], r["mood"]) for r in rows], [("拿下啦！", "得意"), ("稀有的呀。", "温柔")])
        self.assertTrue(all((self.root / r["audio"]).exists() for r in rows))
        self.assertTrue(rows[0]["audio"].startswith("cache/pools/kill_rare/"))
        n = len(self.synths())
        got = self.voice.play("kill_rare")
        self.assertIn(got["text"], ("拿下啦！", "稀有的呀。"))
        self.assertGreater(got["seconds"], 0)
        self.wait()
        self.assertEqual(len(self.synths()), n)
        self.assertEqual(self.events, [{"text": got["text"], "pool": "kill_rare", "seconds": got["seconds"]}])

    def test_prepare_adds_skips_repeats_and_replace_replaces(self):
        self.voice.prepare("idle", ["陪我一会儿嘛。", "你在想什么呀？"])
        out = self.voice.prepare("idle", ["陪我一会儿嘛！", "今天好开心。"])
        self.assertEqual((out["lines"], out["added"], out["voiced"]), (3, 1, 1))
        out = self.voice.prepare("idle", ["只剩这句。"], replace=True)
        self.assertEqual((out["lines"], out["added"]), (1, 1))
        self.assertEqual(self.voice.list_pools(), {"pools": [{"pool": "idle", "lines": 1, "voiced": 1}]})

    def test_prepare_checks_its_arguments(self):
        for pool, lines in (("Bad Name", ["a"]), ("ok", "不是列表"), ("ok", []), ("ok", ["一" * (MAX_TEXT + 1)])):
            with self.assertRaises(VoiceError):
                self.voice.prepare(pool, lines)

    def test_play_an_empty_or_missing_pool_fails(self):
        with self.assertRaises(VoiceError) as e:
            self.voice.play("nothing")
        self.assertIn("prepare", str(e.exception))
        with self.assertRaises(VoiceError):
            self.voice.play("../bank")

    def test_play_interrupts_when_asked(self):
        self.voice.prepare("death", ["没事的，我等你。"])
        self.voice.say(LONG)
        time.sleep(0.05)
        self.voice.play("death", interrupt=True)
        self.wait()
        self.assertEqual([e.get("cut", False) for e in self.events], [True, False])


class OldBank(Base):
    def setUp(self):
        super().setUp()
        bank = self.root / "cache" / "bank"
        bank.mkdir(parents=True)
        rows = [{"text": "来了，我盯着呢。", "mood": "紧张", "audio": "", "uses": 2},
                {"text": "小心呀，别大意。", "mood": "紧张", "audio": "", "uses": 2}]
        self.old_file = bank / "combat_start_elite.jsonl"
        self.old_file.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        self.old = self.old_file.read_bytes()

    def test_old_scenes_are_listed_and_played_with_the_synthesis_cache(self):
        self.assertEqual(self.voice.list_pools(), {"pools": [{"pool": "combat_start_elite", "lines": 2, "voiced": 0}]})
        a = self.voice.play("combat_start_elite")
        b = self.voice.play("combat_start_elite")
        self.assertNotEqual(a["text"], b["text"])
        self.wait()
        self.assertEqual(sorted(self.synths()), sorted([a["text"], b["text"]]))
        self.voice.play("combat_start_elite")                     # 第三次：合成缓存命中
        self.assertEqual(len(self.synths()), 2)
        self.assertEqual(self.old_file.read_bytes(), self.old)

    def test_prepare_with_no_lines_voices_the_old_scene_into_a_pool_of_its_own(self):
        out = self.voice.prepare("combat_start_elite", [])
        self.assertEqual((out["lines"], out["added"], out["voiced"]), (2, 0, 2))
        self.assertTrue((self.root / "cache/pools/combat_start_elite.jsonl").exists())
        self.assertEqual(self.voice.list_pools()["pools"][0]["voiced"], 2)
        self.assertEqual(self.old_file.read_bytes(), self.old)
        self.voice.prepare("combat_start_elite", ["稳住，我在呢。"])
        self.assertEqual(self.voice.list_pools()["pools"][0]["lines"], 3)


if __name__ == "__main__":
    unittest.main()
