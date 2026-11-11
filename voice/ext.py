"""把 core.Voice 接成无限工坊的本机扩展 voice（SDK 的 Extension）：能力、选项 volume、来源 said。"""
from __future__ import annotations

from functools import wraps

from .core import MAX_TEXT, Voice, VoiceError
from .pools import CAP

try:
    from wuxiankit import ExtensionError
except ImportError:                    # 不经工坊跑测试时没有 SDK
    class ExtensionError(Exception):
        pass

MOOD_ARG = "开心|撒娇|得意|紧张|心疼|温柔?"
MOOD_DOC = "mood: one of 开心 撒娇 得意 紧张 心疼 温柔, the line's tone (default 温柔). "


def _errors(fn):
    @wraps(fn)
    def run(*a, **kw):
        try:
            return fn(*a, **kw)
        except VoiceError as e:
            raise ExtensionError(str(e)) from e
    return run


def build(ext, voice: Voice) -> None:
    """在 ext 上登记选项、来源、能力（ext 是 wuxiankit.Extension，或测试里按同样接口写的替身）"""
    volume = ext.option("volume", kind="number", title="音量", default=100, min=0, max=100, step=5, unit="%",
                        hint="说话的音量（0 = 无声）")
    voice.set_volume(volume.get())
    ext.on_options(lambda *_: voice.set_volume(volume.get()))

    said = ext.source("said", title="说完一句",
                      doc="a line finished playing, or was cut off: {text, pool? (the pool it came from), seconds (played), "
                          "cut? (interrupted or stopped)}. Lines from say and play, the reactions' too.")
    voice.emit = said.emit

    @ext.point("say", title="说一句", timeout=60, args={"text": "string", "mood": MOOD_ARG, "interrupt": "boolean?"},
               doc=f"speak one line of Chinese aloud in the companion's voice (Serena), made now: at most {MAX_TEXT} characters "
                   f"(one or two short sentences are best). {MOOD_DOC}interrupt true: cut off what is playing and drop "
                   "the queue (urgent lines only); else it queues: one line plays at a time, and a line still waiting "
                   "after 20 s is dropped. Answers once the line is made: {seconds}; while muted it says nothing and "
                   "answers {muted: true}. Runs on this computer, works without the game.")
    @_errors
    def say(text, mood=None, interrupt=False):
        return voice.say(text, mood, bool(interrupt))

    @ext.keep("prepare", title="准备一组句子", timeout=600,
              args={"pool": "string", "lines": "table", "replace": "boolean?"},
              doc="make a pool of lines ahead and keep it on this computer, so play can speak one at once: pool, its name "
                  "(lowercase letters, digits, _, such as kill_rare); lines [{text, mood?}], each at most "
                  f"{MAX_TEXT} characters, meant to be said again and again (no names, no numbers). Adds to the pool "
                  f"(repeats skipped; at most {CAP} lines, the oldest go); replace true replaces it. Empty lines: only "
                  "make the sound of lines that have none yet. About 1-2 s a line; answers {pool, lines, added, "
                  "voiced, secs}.")
    @_errors
    def prepare(pool, lines, replace=False):
        return voice.prepare(pool, lines, bool(replace))

    @ext.point("play", title="从组里放一句", args={"pool": "string", "interrupt": "boolean?"},
               doc="speak a line from the pool at once: one not played lately; interrupt as in say. Answers {text, mood, "
                   "seconds}; fails when the pool is empty or missing; {muted: true} while muted. For reactions.")
    @_errors
    def play(pool, interrupt=False):
        return voice.play(pool, bool(interrupt))

    @ext.see("pools", title="句子组", args={},
             doc="the pools: {pools: [{pool, lines, voiced (lines with their sound made)}]}, the old companion's line bank "
                 "among them by scene (combat_start_elite, kill_rare, death, level ...).")
    @_errors
    def pools():
        return voice.list_pools()

    @ext.point("stop", title="停下", args={}, doc="stop what is playing and drop the queue: {stopped: lines}. Later lines play as usual.")
    @_errors
    def stop():
        return voice.stop()

    @ext.point("mute", title="静音", args={"on": "boolean"},
               doc="on true: stop now and stay silent (say and play, the reactions' too) until on false. Answers {muted}.")
    @_errors
    def mute(on):
        return voice.mute(on)

    @ext.see("status", title="状态", args={},
             doc="{ready (the voice model is loaded), loading, muted, volume, speaking (the line playing), queue (lines "
                 "waiting), pools (how many), recent (the last lines said: {text, pool?, seconds, cut?, at}), error? "
                 "(why the model did not load)}. The first call starts the extension and loads the model (10-30 s).")
    @_errors
    def status():
        return voice.status()


__all__ = ["build", "ExtensionError"]
