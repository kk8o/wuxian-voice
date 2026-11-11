"""voice/ext.py + 清单：经 SDK 替身（tests/fakes.py 的 FakeExtension，按 contract3.md 第五节的 API）登记的能力、选项、来源；
按名字调用、错误变成 ExtensionError、选项改音量、来源 said。SDK 的 Extension 写好后照样能用 build(ext, voice)。"""
import json
import random
import re
import tempfile
import unittest
from pathlib import Path

from fakes import ROOT, FakeBackend, FakeExtension, FakeTTS

from voice.config import Settings
from voice.core import Voice
from voice.ext import ExtensionError, build
from voice.pools import Pools
from voice.speaker import QwenSpeaker

TYPES = {"string", "number", "boolean", "table", "any"}


class Wiring(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.settings = Settings(cache_dir=str(root / "voice"), pools_dir=str(root / "pools"),
                                 bank_dir=str(root / "bank"))
        self.voice = Voice(self.settings, speaker=QwenSpeaker(self.settings, factory=lambda *a: FakeTTS(per_char=0.01)),
                           backend=FakeBackend(), pools=Pools(root / "pools", root / "bank", root=root,
                                                              rng=random.Random(1)))
        self.addCleanup(self.voice.close)
        self.ext = FakeExtension()
        build(self.ext, self.voice)

    def test_capabilities_and_their_kinds(self):
        kinds = {n: c["kind"] for n, c in self.ext.caps.items()}
        self.assertEqual(kinds, {"say": "point", "prepare": "keep", "play": "point", "pools": "see", "stop": "point",
                                 "mute": "point", "status": "see"})
        for name, cap in self.ext.caps.items():
            self.assertRegex(name, r"^[a-z][a-z0-9_.]*$")
            self.assertTrue(cap["title"] and cap["doc"], name)
            self.assertLessEqual(cap["timeout"] or 30, 600)
            for arg, spec in cap["args"].items():           # kit 的 args 写法：类型名或枚举，| 联合，? 可选
                for part in spec.rstrip("?").split("|"):
                    self.assertTrue(part in TYPES or not part.isascii(), (name, arg, spec))
        self.assertEqual(self.ext.caps["say"]["args"]["text"], "string")
        self.assertEqual(self.ext.caps["prepare"]["args"]["lines"], "table")

    def test_options_and_sources(self):
        self.assertEqual(list(self.ext.options), ["volume"])
        spec = self.ext.options["volume"].spec
        self.assertEqual((spec["kind"], spec["default"], spec["min"], spec["max"]), ("number", 100, 0, 100))
        self.ext.set_options({"volume": 30})
        self.assertAlmostEqual(self.settings.volume, 0.3)
        self.assertEqual(list(self.ext.sources), ["said"])

    def test_calls_by_name_and_said_goes_out_as_an_event(self):
        self.ext.run(setup=self.voice.setup)
        self.assertTrue(self.ext.call("status")["ready"])
        self.ext.call("prepare", {"pool": "level", "lines": [{"text": "又升级啦！", "mood": "开心"}]})
        self.assertEqual(self.ext.call("pools"), {"pools": [{"pool": "level", "lines": 1, "voiced": 1}]})
        got = self.ext.call("play", {"pool": "level"})
        self.ext.call("say", {"text": "我在呢。"})
        self.assertTrue(self.voice.player.wait(5))
        self.assertEqual([e["source"] for e in self.ext.events], ["said", "said"])
        self.assertEqual(self.ext.events[0]["data"], {"text": "又升级啦！", "pool": "level", "seconds": got["seconds"]})
        self.assertEqual(self.ext.call("mute", {"on": True}), {"muted": True})
        self.assertEqual(self.ext.call("say", {"text": "嘘。"})["muted"], True)
        self.assertEqual(self.ext.call("stop"), {"stopped": 0})

    def test_failures_come_back_as_extension_errors(self):
        with self.assertRaises(ExtensionError) as e:
            self.ext.call("play", {"pool": "nothing"})
        self.assertIn("prepare", str(e.exception))
        with self.assertRaises(ExtensionError):
            self.ext.call("say", {"text": ""})


class Manifest(unittest.TestCase):
    def setUp(self):
        self.m = json.loads((ROOT / "wuxian-extension.json").read_text(encoding="utf-8"))

    def test_the_manifest_follows_the_contract(self):
        m = self.m
        self.assertEqual((m["id"], m["title"], m["start"]), ("voice", "声音", "use"))
        self.assertRegex(m["id"], r"^[a-z][a-z0-9_]{0,23}$")
        self.assertLessEqual(len(m["title"].encode("utf-8")), 48)
        self.assertEqual(m["run"], ["python", "-m", "voice"])
        self.assertEqual(m["python"], ".venv/Scripts/python.exe")
        self.assertTrue((ROOT / m["docs"]).is_file())
        self.assertTrue((ROOT / m["skills"] / "companion.md").is_file())

    def test_the_companion_app(self):
        [app] = self.m["apps"]
        self.assertEqual((app["app"], app["title"], app["prompt"]), ("companion", "语音搭子", "companion"))
        self.assertEqual(app["hear"], ["game:combat", "game:aura", "game:growth", "game:quest", "game:travel", "route:*"])
        self.assertEqual(app["see"], ["me", "route", "today"])
        self.assertEqual(app["controls"], ["mute", "unmute", "less", "more", "test", {"cmd": "brief", "label": "任务播报"}, "stop"])
        self.assertEqual((app["say"], app["pace"], app["minutes"]), (True, True, 720))
        for gone in ("temp", "driver", "launch"):
            self.assertNotIn(gone, app)

    def test_the_skill_front_matter(self):
        text = (ROOT / "skills" / "companion.md").read_text(encoding="utf-8")
        head = re.match(r"---\n(.*?)\n---\n", text, re.S)
        self.assertIsNotNone(head)
        fields = dict(line.split(": ", 1) for line in head.group(1).splitlines())
        self.assertEqual(fields["name"], "companion")
        self.assertEqual(fields["needs"], "shell")
        for k in ("title", "title-zh", "description", "description-zh"):
            self.assertTrue(fields.get(k), k)
        for tool in ("wk_voice_status", "wk_voice_say", "wk_voice_prepare", "wk_voice_mute", "wk_voice_stop",
                     "wk_kit_app_open", "reaction_set", "reaction_clear", "wuxian listen companion --once"):
            self.assertIn(tool, text)


if __name__ == "__main__":
    unittest.main()
