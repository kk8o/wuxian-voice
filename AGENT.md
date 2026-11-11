# Voice

Local extension `voice` (声音), on this computer, through 无限工坊: it speaks Chinese lines aloud in the companion's
voice (Qwen3-TTS CustomVoice, the speaker Serena, on the GPU). It works without the game. It changes nothing in the
game and sends nothing to other players: only the player hears it, from this computer's speakers.

The voice is fixed (Serena); there is no option to change it. The player sets the volume on the extension's page in
无限工坊 (option 音量 / volume, 0-100).

## Tools

- `wk_voice_status` `{}`: `{ready, loading, muted, volume, speaking, queue, pools, recent, error?}`. The first call
  starts the extension and loads the model (10-30 s); if that call times out, call it again a little later. `error`:
  the model did not load (tell the player).
- `wk_voice_say` `{"text", "mood"?, "interrupt"?}`: makes the line now and speaks it (at most 80 characters; one or two
  short sentences are best). `mood`: 开心 撒娇 得意 紧张 心疼 温柔 (default 温柔). Answers `{seconds}` once the line is
  made. One line plays at a time; others queue, and a line still waiting after 20 s is dropped. `interrupt: true` cuts
  off what is playing and drops the queue: only for urgent lines.
- `wk_voice_prepare` `{"pool", "lines": [{text, mood?}], "replace"?}`: makes a pool of lines ahead (about 1-2 s a
  line), kept in `cache/pools/<pool>.jsonl` with the sound. Adds to the pool (repeats skipped, at most 60 lines, the
  oldest go); `replace: true` replaces it; `lines: []` only makes the sound of lines that have none yet.
- `wk_voice_play` `{"pool", "interrupt"?}`: speaks a line from the pool at once, one not played lately. For reactions.
- `wk_voice_pools` `{}`: `{pools: [{pool, lines, voiced}]}`. The old companion's line bank (`cache/bank/<scene>.jsonl`)
  shows here as pools of the same names (combat_start_elite, kill_rare, death, level ...); it is only read.
- `wk_voice_stop` `{}`: stops what is playing and drops the queue.
- `wk_voice_mute` `{"on"}`: `true` stops now and keeps every line silent (the reactions' too) until `false`.

While muted, `say` and `play` answer `{muted: true}` and say nothing.

## Source `said`

`voice:said` (item kind `voice.said`): a line finished playing or was cut off: `{text, pool?, seconds, cut?}`. A
reaction may hear it; `wk_voice_status` `recent` lists the last few with their time.

## Instant lines: pools and reactions

An Agent is too slow for a fight. Make pools of short lines ahead, then let the workshop's reactions play them the
moment the game says something:

    reaction_set {"id": "comp_kill_rare", "owner": "companion", "hear": "game:kill",
                  "when": {"data.unit.classification": ["rare", "rareelite"]},
                  "call": "voice.play", "args": {"pool": "kill_rare"}, "cooldown": 30}

The skill `companion` (`wk_docs` with `{"skill": "companion"}`) runs the whole companion: the app `companion`
(语音搭子), its pools, its reactions and what to say.
