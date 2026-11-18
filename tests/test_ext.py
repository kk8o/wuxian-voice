"""voice/ext.py + 清单：经 SDK 替身（tests/fakes.py 的 FakeExtension，按 contract3.md 第五节的 API）登记的能力、选项、来源；
按名字调用、错误变成 ExtensionError、选项改音量、来源 said。SDK 的 Extension 写好后照样能用 build(ext, voice)。
清单过无限工坊自己的检查（wuxianworkshop.local.manifest，工坊源码在 PYTHONPATH 上时）。"""
import json
import random
import re
import shutil
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
POOLS = {"combat_start_elite", "combat_start_rare", "combat_start_boss", "kill_elite", "kill_rare", "kill_boss",
         "danger_adds", "cast_at_me", "death", "alive", "level"}
QUIET_OFF = {"combat_start_elite", "kill_elite", "cast_at_me", "alive"}    # 安静时不放的四条（约定第 5 节）
CORE_PACE = {"quiet": {"gather": 6, "every": False, "idle": False, "most": 30},     # WuxianKit 核心的三档（Decl.lua）
             "normal": {"gather": 5, "every": 60, "idle": 1200, "most": 30},
             "lively": {"gather": 3, "every": 20, "idle": 480, "most": 30}}


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

    def test_the_skill_names_only_capabilities_there_are(self):
        text = (ROOT / "skills" / "companion.md").read_text(encoding="utf-8")
        named = set(re.findall(r"wk_voice_([a-z_]+)", text)) | set(re.findall(r"`voice\.([a-z_]+)", text))
        self.assertTrue(named)
        self.assertLessEqual(named, set(self.ext.caps))

    def test_the_manifest_calls_capabilities_there_are(self):
        m = json.loads((ROOT / "wuxian-extension.json").read_text(encoding="utf-8"))
        [app] = m["apps"]
        for r in app["reactions"]:
            ext, name = r["call"].split(".", 1)
            self.assertEqual(ext, m["id"])
            self.assertIn(name, self.ext.caps)
            self.assertIn(r["args"]["pool"], POOLS)
        for c in app["controls"]:
            if isinstance(c, dict) and "call" in c:
                self.assertIn(c["call"], self.ext.caps)

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
        self.assertEqual(app["controls"], [{"cmd": "mute", "call": "mute", "args": {"on": True}},
                                           {"cmd": "unmute", "call": "mute", "args": {"on": False}},
                                           "less", "more", "test", {"cmd": "brief", "label": "任务播报"}, "stop"])
        self.assertEqual((app["say"], app["minutes"], app["link"]), (True, 720, "live"))
        self.assertEqual(self.m["requires"], {"kit": "0.7.0", "workshop": "0.10.0"})
        for gone in ("temp", "driver", "launch"):
            self.assertNotIn(gone, app)

    def test_the_pace(self):
        """kit 的写法（Decl.lua 的 Pace）：三档各 {gather, every, idle, most, wake?}；适中、活跃同核心，安静不饿死"""
        pace = self.m["apps"][0]["pace"]
        self.assertEqual(set(pace), {"quiet", "normal", "lively"})
        for name, p in pace.items():
            self.assertLessEqual(set(p), {"gather", "every", "idle", "most", "wake"}, name)
            self.assertGreaterEqual(p["gather"], 0)
            self.assertGreaterEqual(p["most"], 1)
            for k in ("every", "idle"):
                self.assertTrue(p[k] is False or (not isinstance(p[k], bool) and p[k] >= 1), (name, k))
        self.assertEqual(pace["normal"], CORE_PACE["normal"])
        self.assertEqual(pace["lively"], CORE_PACE["lively"])
        self.assertIsNot(pace["quiet"]["every"], False)                     # 游戏条目没有 mention：别只靠攒满

    def test_the_reactions(self):
        rs = self.m["apps"][0]["reactions"]
        self.assertEqual(len(rs), 11)
        self.assertEqual({r["args"]["pool"] for r in rs}, POOLS)
        for r in rs:
            self.assertNotIn("id", r)
            self.assertNotIn("owner", r)
            self.assertEqual(r["call"], "voice.play")
            pool = r["args"]["pool"]
            self.assertEqual(r.get("activity"), ["normal", "lively"] if pool in QUIET_OFF else None, pool)
            self.assertNotIn("pace", r)                    # 0.10.0：反射的字段叫 activity（应用的 pace 是节奏表）
            self.assertEqual(r["args"].get("interrupt"), True if pool in ("danger_adds", "death") else None, pool)

    def test_the_skill_front_matter(self):
        text = (ROOT / "skills" / "companion.md").read_text(encoding="utf-8")
        head = re.match(r"---\n(.*?)\n---\n", text, re.S)
        self.assertIsNotNone(head)
        fields = dict(line.split(": ", 1) for line in head.group(1).splitlines())
        self.assertEqual(fields["name"], "companion")
        self.assertEqual(fields["needs"], "shell")
        for k in ("title", "title-zh", "description", "description-zh"):
            self.assertTrue(fields.get(k), k)
        for tool in ("wk_voice_status", "wk_voice_say", "wk_voice_prepare", "wk_voice_pools", "wk_voice_stop",
                     "wk_kit_app_open", "wk_kit_app_stop", "wuxian listen companion --once"):
            self.assertIn(tool, text)
        for gone in ("reaction_set", "reaction_clear", "wk_voice_mute"):     # 清单装反射、工坊调静音
            self.assertNotIn(gone, text)


class WorkshopCheck(unittest.TestCase):
    """清单过无限工坊的清单检查（local/manifest.py 的 check_manifest）：没有错，也没有警告"""

    def setUp(self):
        try:
            from wuxianworkshop.local import manifest
        except ImportError:
            self.skipTest("无限工坊的源码不在 PYTHONPATH 上（PYTHONPATH=<工坊>/src）")
        self.M = manifest
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name) / "companion"         # 清单与它指到的文件；虚拟环境不在工作区里时放个替身
        (self.folder / ".venv" / "Scripts").mkdir(parents=True)
        shutil.copy(ROOT / "wuxian-extension.json", self.folder)
        shutil.copy(ROOT / "AGENT.md", self.folder)
        shutil.copytree(ROOT / "skills", self.folder / "skills")
        (self.folder / ".venv" / "Scripts" / "python.exe").write_bytes(b"")

    def test_the_workshop_takes_the_manifest(self):
        m, problems = self.M.check_manifest(self.folder)
        self.assertEqual(problems, [])
        [app] = m["apps"]
        self.assertEqual(app["link"], "live")
        self.assertEqual(len(app["reactions"]), 11)
        paced = [r["args"]["pool"] for r in app["reactions"] if r.get("activity")]
        self.assertEqual(set(paced), QUIET_OFF)
        controls = {c["cmd"]: c for c in app["controls"] if isinstance(c, dict)}
        self.assertEqual((controls["mute"]["call"], controls["mute"]["args"]), ("mute", {"on": True}))
        self.assertEqual((controls["unmute"]["call"], controls["unmute"]["args"]), ("mute", {"on": False}))


if __name__ == "__main__":
    unittest.main()
