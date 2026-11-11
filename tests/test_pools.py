"""voice/pools.py：组的读写、旧句库（cache/bank）当成同名的组只读、挑最近没放过的。"""
import json
import random
import tempfile
import unittest
from collections import Counter
from pathlib import Path

import fakes  # noqa: F401  （把扩展文件夹放进 sys.path）

from voice.pools import Pools


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bank = self.root / "cache" / "bank"
        self.pools = Pools(self.root / "cache" / "pools", self.bank, root=self.root, rng=random.Random(7))


class Legacy(Base):
    def setUp(self):
        super().setUp()
        write_jsonl(self.bank / "combat_start_elite.jsonl", [
            {"text": "来了，我盯着呢。", "mood": "紧张", "audio": "", "uses": 2},
            {"text": "小心呀，别大意。", "mood": "紧张", "audio": "", "uses": 2},
            {"text": "小心呀，别大意！", "mood": "紧张", "audio": "", "uses": 0},      # 查重键相同：丢
        ])
        (self.bank / "broken.jsonl").write_text("不是 JSON\n{\"text\": \"\"}\n", encoding="utf-8")
        (self.bank / "Bad-Name.jsonl").write_text("", encoding="utf-8")
        self.before = {p.name: p.read_bytes() for p in self.bank.iterdir()}

    def test_old_bank_scenes_are_pools_of_the_same_name(self):
        self.assertEqual(self.pools.names(), ["broken", "combat_start_elite"])
        lines = self.pools.lines("combat_start_elite")
        self.assertEqual([x["text"] for x in lines], ["来了，我盯着呢。", "小心呀，别大意。"])
        self.assertEqual(lines[0], {"text": "来了，我盯着呢。", "mood": "紧张", "audio": ""})
        self.assertEqual(self.pools.lines("broken"), [])
        self.assertEqual(self.pools.lines("missing"), [])

    def test_a_saved_pool_wins_over_the_old_bank_which_is_never_written(self):
        self.pools.save("combat_start_elite", [{"text": "新的一句。", "mood": "开心", "audio": "cache/pools/x.wav"}])
        self.assertEqual([x["text"] for x in self.pools.lines("combat_start_elite")], ["新的一句。"])
        self.assertEqual({p.name: p.read_bytes() for p in self.bank.iterdir()}, self.before)
        self.pools.pick("combat_start_elite")
        self.assertEqual({p.name: p.read_bytes() for p in self.bank.iterdir()}, self.before)


class Files(Base):
    def test_save_writes_one_json_object_per_line(self):
        self.pools.save("kill_rare", [{"text": "拿下啦。", "mood": "得意", "audio": "cache/pools/kill_rare/a.wav", "x": 1}])
        raw = (self.root / "cache" / "pools" / "kill_rare.jsonl").read_text(encoding="utf-8")
        self.assertEqual(raw, '{"text": "拿下啦。", "mood": "得意", "audio": "cache/pools/kill_rare/a.wav"}\n')
        self.assertEqual(self.pools.names(), ["kill_rare"])

    def test_names_must_be_plain(self):
        for bad in ("", "Kill", "a-b", "../x", "_x", "x" * 49, None, 3):
            self.assertFalse(Pools.valid(bad), bad)
        self.assertTrue(Pools.valid("combat_start_elite"))
        self.assertEqual(self.pools.lines("../bank/x"), [])

    def test_audio_paths_are_relative_to_the_extension_folder(self):
        p = self.pools.audio_path("kill_rare", "拿下啦。", "得意")
        self.assertEqual(p.parent, self.root / "cache" / "pools" / "kill_rare")
        self.assertEqual(self.pools.rel(p), "cache/pools/kill_rare/" + p.name)
        self.assertEqual(self.pools.abs(self.pools.rel(p)), p)
        self.assertFalse(self.pools.voiced({"audio": self.pools.rel(p)}))
        p.parent.mkdir(parents=True)
        p.write_bytes(b"RIFF")
        self.assertTrue(self.pools.voiced({"audio": self.pools.rel(p)}))


class Picking(Base):
    def test_picks_spread_over_the_pool_and_skip_the_last_few(self):
        self.pools.save("idle", [{"text": f"第{i}句。", "mood": "撒娇"} for i in range(8)])
        got = [self.pools.pick("idle")["text"] for _ in range(24)]
        self.assertEqual(set(Counter(got).values()), {3})                 # 放得最少的先
        for i in range(len(got) - 5):
            self.assertNotIn(got[i], got[i + 1:i + 6])                     # 最近五次放过的不挑

    def test_a_small_pool_still_varies_and_a_single_line_repeats(self):
        self.pools.save("two", [{"text": "甲。"}, {"text": "乙。"}])
        got = [self.pools.pick("two")["text"] for _ in range(6)]
        self.assertTrue(all(got[i] != got[i + 1] for i in range(5)))
        self.pools.save("one", [{"text": "只有我。"}])
        self.assertEqual([self.pools.pick("one")["text"] for _ in range(3)], ["只有我。"] * 3)

    def test_an_empty_or_missing_pool_gives_none(self):
        self.assertIsNone(self.pools.pick("nothing"))
        self.pools.save("empty", [])
        self.assertIsNone(self.pools.pick("empty"))


if __name__ == "__main__":
    unittest.main()
