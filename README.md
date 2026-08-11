# rss-grab

订阅 RSS 播客 → 自动拉取增量 → AI 摘要 → 审阅勾选 → 下载 → ASR 转写 → 生成结构化中文笔记。

完整流水线：**订阅 → 增量 → 摘要 → 勾选 → 下载 → 转写 → 笔记**。

> **这是什么**：项目主体是一个 **Agent skill 包**（`.agents/skills/rss-grab/`），供 **Codex / Claude Code 等 AI 助手**编排使用——在对话里发一个播客 RSS 链接（或 Apple Podcasts 链接），说"订阅这个播客、拉取新期数、把感兴趣的转写生成笔记"，Agent 会按 skill 指令自动完成整条流水线。所有脚本同时是完整 CLI，也可以纯命令行操作（见「快速开始」）。

> ⚠️ **平台要求：仅支持 macOS（Apple Silicon，M1/M2/M3/M4）**
> ASR 转写依赖 mlx-whisper（Apple 的 MLX 框架，仅 Apple Silicon 可用）。
> **Windows / Linux / Intel Mac 用户**：请改用本地 [faster-whisper](https://github.com/SYSTRAN/faster-whisper) 或 [whisper.cpp](https://github.com/ggerganov/whisper.cpp) 做 ASR，或使用其他在线 ASR 服务，然后按 rss/transcripts/ 的格式输出分段时间戳内容（后续流水线依赖时间戳分段落盘）。
> 跨平台支持已在 Roadmap 中（见文末）。

## 为什么选 rss-grab

**它解决什么问题**：播客信息密度高、一期 35-60 分钟，靠"听"做知识管理效率低——你听完就忘，回找时只能凭记忆翻播放器历史。rss-grab 把**"听播客"变成"读笔记"**：自动拉取你订阅播客的新期数、生成摘要供你快速判断值不值得听、把选中的转写成文字稿、再生成结构化中文笔记沉淀下来——整个流程不靠你在播放器里手动操作，订阅后基本自动化。

**一句话定位**：rss-grab 是**开源的本地播客笔记工具**——订阅、下载、ASR 转写都在你机器上完成（Apple Silicon 本地推理），AI 摘要与笔记生成调用 OpenAI 兼容 LLM API。

**适合你，如果你**：
- 不想把播客笔记数据交给云端 SaaS，希望数据留在本机
- 主要听中文播客，想用中文模板生成结构化笔记
- 已经有 RSS 订阅习惯，想要"自动收件箱"式增量工作流
- 在 macOS Apple Silicon 上跑，介意离线可用

**它做了什么**：
- **完整流水线**：订阅 → 增量 → AI 摘要 → 你审阅勾选 → 下载 → ASR 转写 → 结构化笔记
- **本地抓取 + 本地转写**：抓取与 ASR 转写（mlx-whisper，Apple Silicon 本地推理）都在你机器上完成；原始音频不离开设备，转写文本仅在与 LLM API 交互时上传（见「LLM 配置」）
- **三区状态机**：每源一份 Markdown 状态文件（待确认 / 确认 / 已转化），跟你用邮箱一样自然
- **Apple Podcasts 反推**：粘一个 Apple 链接，自动反推 RSS feed URL 订阅
- **长度自适应**：短播客用 skill 模式（快），长播客自动切 map-reduce（细）

**诚实承认的限制**（按重要性排序）：
- **仅 macOS Apple Silicon**（mlx-whisper 硬性要求）— Windows/Linux 用户需自己改用 faster-whisper
- **CLI 优先**（没有 GUI）— 非技术用户上手有门槛
- **LLM 可配**：OpenAI 兼容接口，未配置时回退 MiniMax M3，可在 .env 指定任意兼容服务（DeepSeek/OpenAI/Ollama 等）

## 核心能力

| 能力 | 说明 |
|---|---|
| 订阅模式 | 订阅表 + 每源一个状态文件（待确认 / 确认 / 已转化 三区） |
| 定时增量拉取 | 增量命令幂等、可重复执行；可配合 launchd/cron 定时自动拉新期数 + AI 摘要 |
| AI 摘要 | 每期生成"一句话概括"，用户无需打开音频即可判断是否值得听 |
| 批量下载 | yt-dlp 下载音频（串行 + CDN 礼仪间隔） |
| ASR 转写 | mlx-whisper large-v3-turbo（仅 Apple Silicon），带**时长完整性校验** |
| 笔记生成 | 模板自适应 + 长度档位（<50K skill 模式 / ≥50K map-reduce），20 并发批量 |
| Apple Podcasts 反向解析 | 网页链接 → RSS feed URL 自动提取 |
| LLM 兼容 | OpenAI 兼容接口（未配置时回退 MiniMax M3，可换任意兼容服务） |

## 跟同类项目对比

| 维度 | rss-grab | Podwise（闭源） | MrRSS | RSS-GPT | Meetily |
|---|---|---|---|---|---|
| **播客专门** | ✅ | ✅ | ❌ 通用 RSS | ❌ 通用 RSS | ❌ 会议录音 |
| **本地 ASR** | ✅ mlx-whisper | ❌ 云端 | ❌ | ❌ | ✅ Whisper |
| **增量订阅模式** | ✅ 三区状态机 | ✅ 自动 | ✅ | ❌ 静态 | ❌ |
| **Apple Podcasts 反推** | ✅ | ❌ | ❌ | ❌ | ❌ |
| **中文模板自适应** | ✅ | ⚠️ 英文优先 | ⚠️ 通用 | ⚠️ 通用 | ⚠️ 通用 |
| **离线运行** | ⚠️ 转写离线/摘要需联网 | ❌ | ⚠️ | ❌ | ✅ |
| **跨平台** | ❌ macOS only | ✅ Web/App | ✅ Docker | ✅ Actions | ✅ Tauri |
| **开源协议** | ✅ MIT | ❌ | ✅ | ✅ | ✅ MIT |
| **价格** | 免费 | $5.9/月起 | 免费 | 免费 | 免费 |
| **数据隐私** | ⚠️ 本地转写/LLM 见上 | ❌ 云端 | ⚠️ 自托管 | ❌ 云端 | ✅ |

**结论**：
- 与**云端播客笔记服务**相比，rss-grab 强调**开源 + 离线 + 中文化**
- 跟 **MrRSS / RSS-GPT** 偏通用 RSS 不同，rss-grab 专注**音频类 RSS** + **本地 ASR** + **长笔记生成**
- 跟 **Meetily** 思路接近（本地优先），但定位不同：rss-grab 处理"播客"，Meetily 处理"会议录音"

## LLM 配置（OpenAI 兼容）

- 代码通过 OpenAI 兼容接口调用 LLM。**未配置时回退 MiniMax M3**（`api.minimaxi.com`）；在 `.env` 配置后即切换到你的服务
- **可换任意 OpenAI 兼容服务**（DeepSeek / OpenAI / 本地 Ollama 等），只需在 `.agents/skills/rss-grab/scripts/.env` 配置（模板见根目录 `.env.example`）：
  ```bash
  LLM_API_KEY=your-key                    # 你的服务 API key
  LLM_BASE_URL=https://api.minimaxi.com/v1   # 换成你的服务 base_url
  LLM_MODEL=MiniMax-M3                    # 换成你的模型名
  ```
- 从 https://platform.minimaxi.com/user-center/payment/token-plan 获取 MiniMax key

## 两种使用方式

### 方式 A：Agent 驱动（推荐，面向 Codex / Claude Code 等 AI 助手）

把 `.agents/skills/` 目录放进 AI 助手能访问的项目目录（Codex 的项目目录、或 Claude Code 的工作区），然后在对话中直接用自然语言驱动：

```
"订阅这个播客：https://podcasts.apple.com/xxx
 拉取新期数，把待确认区里感兴趣的几期转写并生成笔记"
```

Agent 会读取 `.agents/skills/rss-grab/SKILL.md` 的指令，自动完成：订阅反推 → 拉增量 → AI 摘要 → 三区状态文件 → 勾选抓取 → ASR 转写 → 模板笔记。**无需记住任何命令**——skill 会把每一步要执行什么、产物落在哪都告诉你。

> 💡 把 `.agents/skills/` 当作"技能库"：除了 `rss-grab` 主 skill，共享模块 `_shared/`（ASR、env 加载、路径定位、批量笔记）会被 skill 自动引用，一并放入即可。

### 方式 B：CLI 直接使用

不依赖 Agent，纯命令行跑整条流水线（见下方「快速开始」）。适合想精确控制每一步、或做自动化脚本的用户。

## 安装

```bash
# 依赖（macOS）
brew install yt-dlp ffmpeg
pip3 install -r requirements.txt   # openai + mlx-whisper（模型见下）

# MLX-Whisper 模型（仅 Apple Silicon）
# 1. 下载模型权重（约 1.6GB，只需一次）
huggingface-cli download mlx-community/whisper-large-v3-turbo
# 2. 建软链指向模型
mkdir -p tools/asr-poc/models
ln -s ~/.cache/huggingface/hub/models--mlx-community--whisper-large-v3-turbo \
  tools/asr-poc/models/whisper-large-v3-turbo
# （若尚未安装 huggingface-cli：brew install huggingface-cli 或 pip install "huggingface_hub[cli]"）

# API Key（AI 摘要 + 笔记生成）
# 模板见根目录 .env.example；生效位置是 scripts/ 目录（脚本从这里读）
cp .env.example .agents/skills/rss-grab/scripts/.env
# 编辑 .agents/skills/rss-grab/scripts/.env，填入你的 LLM_API_KEY（OpenAI 兼容接口）
```

## 快速开始

```bash
# 1. 订阅一个播客（RSS feed URL 或 Apple Podcasts 链接）
python3 .agents/skills/rss-grab/scripts/fetch_rss_feed.py --subscribe "https://feed.xyzfm.space/xxx"

# 2. 拉取增量（新期数 + AI 摘要）
python3 .agents/skills/rss-grab/scripts/fetch_rss_feed.py --fetch-updates

# 3. 审阅：打开 rss/订阅/<节目名>.md，把想听的勾为 [x]（确认抓取）

# 4. 批量下载 + 转写 + 笔记
python3 .agents/skills/rss-grab/scripts/fetch_rss_feed.py --pick-subscribe rss/订阅/<节目名>.md
python3 .agents/skills/rss-grab/scripts/batch_transcribe.py
python3 .agents/skills/rss-grab/scripts/batch_notes.py
```

**定时拉取（可选）**：`--fetch-updates` 幂等、重复执行安全。可配 launchd/cron 每日自动跑，例如 launchd plist 的 ProgramArguments：

```xml
<key>ProgramArguments</key>
<array>
  <string>/usr/bin/python3</string>
  <string>/绝对路径/.agents/skills/rss-grab/scripts/fetch_rss_feed.py</string>
  <string>--fetch-updates</string>
</array>
<key>StartCalendarInterval</key>
<dict>
  <key>Hour</key><integer>22</integer>
  <key>Minute</key><integer>30</integer>
</dict>
```

（cron 用户等价的 crontab 行：`30 22 * * * /usr/bin/python3 /绝对路径/.../fetch_rss_feed.py --fetch-updates`）

## 目录结构

```
.agents/skills/
├── rss-grab/                  # 主 skill（SKILL.md = 给 Agent 的指令文档）
│   ├── scripts/               # 流水线脚本（CLI 可直接调用）
│   ├── templates/             # 笔记模板（当前含「访谈播客」中文模板）
│   └── SKILL.md               # Agent 指令：触发条件 + 阶段 1-4 工作流 + 路径约定
└── _shared/                   # 共享模块（ASR 转写、.env 加载、路径定位、批量笔记）

rss/                           # 运行数据（安装后由脚本生成，不入 git）
├── 订阅/                    # 订阅表 + 每源状态文件（待确认/确认/已转化 三区）
├── raw/                     # 期数元数据（info.json，含 enclosure url）
├── transcripts/             # ASR 转写稿（可入 git 防丢失）
└── notes/                   # 结构化笔记（按源分目录）
```

音频临时存 `/tmp/rss-grab-audio/`（转写后可用 `--cleanup-audio` 清理；info.json 里存了源 url，需要时可重下）。

## 测试

```bash
python3 -m pytest .agents/skills/rss-grab/scripts/tests/ .agents/skills/_shared/tests/ -v
```

## 免责声明

本项目仅供个人学习与内容管理使用。抓取内容（RSS feed、音频）版权归原作者/平台所有；请遵守各平台服务条款与内容许可，勿将本工具用于任何商业或侵权用途。RSS 是公开标准，但请尊重播客作者的下载礼仪（脚本已内置请求间隔）。

## Roadmap

- [ ] **跨平台 ASR**：Windows / Linux / Intel Mac 支持（faster-whisper / whisper.cpp，不再绑 Apple Silicon）
- [ ] **桌面 GUI**：基于 Tauri 包装 CLI，降低非技术用户门槛
- [ ] **更多 LLM 适配**：OpenAI / DeepSeek / Gemini / 本地 Ollama 一键切换（当前需手动配 base_url）
- [ ] **知识库联动**：Obsidian / Notion / Logseq 笔记自动同步
- [ ] **英文播客模板**：当前仅「访谈播客」中文模板，补英文场景 2-3 个
- [ ] **GitHub Actions CI**：单测自动跑，发布 release tag

## Acknowledgments

rss-grab 站在巨人的肩膀上，感谢以下开源项目：

- **[openai-python](https://github.com/openai/openai-python)** (Apache 2.0) — OpenAI 兼容 API 客户端
- **[mlx-whisper](https://github.com/ml-explore/mlx-examples)** (MIT) — Apple Silicon 上的 Whisper 推理
- **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** (Unlicense) — 播客音频下载
- **[FFmpeg](https://ffmpeg.org/)** (LGPL 2.1+) — 音频元数据探测
- **[curl](https://curl.se/)** (MIT) — RSS XML 抓取

## License

MIT
