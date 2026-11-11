# 语音搭子

《魔兽世界：无限》的语音搭子：你玩的时候，她在耳边说话——进战斗、击杀稀有、升级时立刻来一句，平时结合你的角色和任务进度聊几句，任务播报读出下一步要做什么。

这是[无限工坊](https://wuxianwow.com/workshop)的一个**本机扩展**「声音」（id `voice`），带一个应用「语音搭子」（id `companion`）。案例介绍见[扩展框架](https://wuxianwow.com/workshop/extensions#case-voice)。

- **说什么**由你本机的 AI Agent（Claude Code、Codex 等）决定：它按技能 `companion` 听游戏（战斗、光环、成长、任务、旅行、任务指引），自己决定说不说、说什么。
- **怎么说**是这个扩展：本地语音合成（Qwen3-TTS 0.6B，音色 Serena）把 Agent 写的话念出来，也预先合成一组组句子。
- **即时反应**（进战斗、击杀、死亡、升级……）由无限工坊的「反射」直接从句子组里放一句，不等 Agent。

```
游戏  WuxianKit · 游戏动态、任务指引
  │
  ▼
无限工坊 ── 应用 companion 的唤醒 ──→ Agent（技能 companion）── wk_voice_say ──┐
   │                                                                          ▼
   └── 反射：击杀稀有 → voice.play {pool: kill_rare} ────────────────→ 扩展 voice（本文件夹）→ 扬声器
```

声音只在你这台电脑上播放，不改游戏里的任何东西，也不发给别的玩家。

## 系统要求

- 64 位 Windows 10 或 11。
- NVIDIA 显卡，驱动支持 CUDA 12.8。模型峰值约 2.2 GB 显存；游戏同时也占显存，建议显卡 8 GB 起。
- 硬盘约 5 GB（PyTorch 约 2.5 GB，模型约 2.4 GB）。
- Python 3.12（[python.org](https://www.python.org/downloads/)）。
- [无限工坊](https://wuxianwow.com/workshop) 0.9.13 或更高版本；游戏里装好扩展框架 WuxianKit 0.6.0 或更高版本，「游戏动态」「任务指引」开着。
- 一个支持 MCP、已接入无限工坊的 AI Agent。

游戏帧数越高，合成越慢：实测前台 60 帧时合成一句约为音频时长的 0.6–0.8 倍，建议在游戏设置里把前台帧数上限设为 60。

## 安装

1. 下载这个仓库（`git clone`，或在 GitHub 上下载 zip 解压），放在一个固定的位置。
2. 在文件夹里运行：

   ```
   powershell -ExecutionPolicy Bypass -File setup.ps1
   ```

   它会建虚拟环境 `.venv`、安装依赖、从 ModelScope 下载语音模型到 `models\`。中断了再运行一次，会接着做。
3. 在无限工坊 App 的「扩展」页点「添加」，选这个文件夹。App 列出它的命令、文件和能力，看过后点「批准」。

## 启动

- 游戏里输入 `/wk`，在「语音搭子」的卡片上点「交给 AI」，把复制的那句话发给你的 Agent；
- 或直接对 Agent 说：「用技能 companion 陪我玩三个小时」。

第一次启动要载入模型（10–30 秒）。开起来后，在卡片上或聊天框里：

```
/wk companion 静音 | 开声 | 少说 | 多说 | 试音 | 任务播报 | 停止
/wk companion <想对她说的话>
```

音量在 App「扩展」页的「声音」里调。日志：`wuxian ext logs voice`，或让 Agent 看 `extension_logs`。

## 能力（工具 `wk_voice_*`）

| 能力 | 类 | 参数 | 说明 |
|---|---|---|---|
| `say` | 指引 | `text, mood?, interrupt?` | 现合成现说（80 字以内），回 `{seconds}` |
| `prepare` | 存储 | `pool, lines: [{text, mood?}], replace?` | 预先合成一组句子；`lines: []` 只补合成 |
| `play` | 指引 | `pool, interrupt?` | 从组里放一句最近没放过的（反射用） |
| `pools` | 读取 | | 各组几句、几句已合成 |
| `stop` | 指引 | | 停下、清空队列 |
| `mute` | 指引 | `on` | 静音（反射也不出声） |
| `status` | 读取 | | 模型是否载入、在说什么、队列、最近说过的几句 |

同时只放一句；`interrupt` 打断正在说的；排队 20 秒还没轮到的不说了。来源 `said`：说完一句（`{text, pool?, seconds, cut?}`）。

## 本机设置

可选文件 `voice.local.toml`（不进仓库）：

```toml
[voice]
device = "耳机"       # 播放设备名的一部分；空 = 系统默认
loudness = 0.9        # 每句拉到的峰值；0 = 不拉平
```

其余项见 `voice/config.py` 的 `Settings`。

## 自己做一套

不想用这份代码，也可以让你的 Agent 用无限工坊从头做一个：提示词见[扩展框架页的案例四](https://wuxianwow.com/workshop/extensions#case-voice)。

## 测试

```
.venv\Scripts\python.exe -m unittest discover -s tests
```

不载入真模型、不出声音（假模型、假播放后端）。设 `VOICE_GPU_TEST=1` 时多跑一个真模型的冒烟测试。

## 许可与致谢

本项目代码以 MIT 许可证发布，见 `LICENSE`。

- 语音模型 [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS)（Apache-2.0），由 setup.ps1 从 ModelScope 下载，不随仓库分发。
- 加速 [faster-qwen3-tts](https://github.com/andimarafioti/faster-qwen3-tts)（MIT）。

玩家自制的非官方项目，与暴雪娱乐、网易无关。
