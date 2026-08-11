---
name: rss-grab
description: 抓取播客 RSS feed：解析 RSS XML -> 下载音频到 rss/audio/ -> 元数据落到 rss/raw/<guid_hash8>.info.json（增量去重，二次运行跳过已有）-> ASR 转写（阶段 2，mlx-whisper）-> 生成结构化中文笔记 + INDEX 索引（阶段 3，短播客 skill 模式 / 长播客 map-reduce 模式）。支持订阅模式（阶段 4）：Apple Podcasts 链接自动反推 feed URL -> 定时拉增量 -> 状态文件勾选 -> 抓取做笔记。当用户提供播客 RSS 链接（https://feed.xyzfm.space/xxx 或 https://xxx.com/feed.xml）或 Apple Podcasts 链接并要求"订阅 / 抓下来 / 下载播客 / 拉取期数 / 转写 / 写笔记"时使用。只支持播客音频类 RSS（RSS 2.0 + itunes + audio enclosure），文章/视频/Atom/JSON Feed 识别后提示不处理。依赖：yt-dlp、curl、Python 3.12+、mlx-whisper（Apple Silicon）、LLM_API_KEY。
---

# RSS Grab

## 触发条件

用户在 Codex 对话中发送 RSS 链接（含 `feed.xyzfm.space`、`/rss`、`/feed`、`.xml` 等）或 Apple Podcasts 链接（`podcasts.apple.com`），并要求订阅播客 / 下载播客 / 抓取期数 / 拉取音频。

## 阶段 4 工作流（订阅模式，推荐）

> RSS 的本质是订阅：作者持续更新。订阅模式 = 1 源 1 状态文件 + 定时拉增量 + 用户审阅勾选 + 抓取做笔记。

### 订阅新源

1. **订阅**：`python3 scripts/fetch_rss_feed.py --subscribe <apple_podcasts_url>`
   - Apple Podcasts 链接自动反推 feed URL（页面内 `"feedUrl":"..."` 提取）
   - 反推失败可用 `--feed-url <manual>` 手动指定
   - 生成 `rss/订阅/<节目名>.md` 状态文件（feed 元数据填好，三区为空）

### 拉取增量

2. **拉增量**：`python3 scripts/fetch_rss_feed.py --fetch-updates`
   - 遍历 `rss/订阅/subscriptions.json` 所有源
   - 对比状态文件已有 guid，只拉新增期
   - 新增期跑 AI 摘要（OpenAI 兼容 LLM，单期纯文本，20 并发 + 2s 间隔 + 429 熔断降级 + checkpoint 恢复）
   - 每期完成增量写状态文件"待确认"区
   - 可配定时任务（launchd/cron）每天自动跑 `--fetch-updates`，命令幂等、重复执行安全；日志 `rss/订阅/.logs/`
   - 示例（launchd plist 的 ProgramArguments，每天 22:30）：
     `/usr/bin/python3 <project-root>/scripts/fetch_rss_feed.py --fetch-updates`

### 审阅勾选

3. **审阅**：打开状态文件看"待确认"区（每期含 AI 摘要三段：一句话概括/内容概览/值得关注）
   - 改 checkbox：`[ ]` 待确认 / `[x]` 确认抓取 / `[~]` 确认不抓 / `[done]` 已转化
   - 保存后跑 `--sync-pick` 归区：`python3 scripts/fetch_rss_feed.py --sync-pick rss/订阅/<节目名>.md`
   - 支持反向流转（[x] 改回 [ ] 移回待确认）

### 抓取做笔记

4. **抓取**：`python3 scripts/fetch_rss_feed.py --pick-subscribe rss/订阅/<节目名>.md [--transcribe]`
   - 读 [x] 期的 guid（按 `<!-- guid:xxx -->` 注释），下载音频 + 转写
5. **做笔记**：`python3 scripts/regenerate_note.py <guid_hash8>`
   - 笔记落到 `rss/notes/<源名>/<stem>.md`（按源分目录）
   - 笔记生成成功后状态文件对应期自动标 `[done]` 移入已转化区（mark_done 衔接）

### 其他命令

- `--retry-summary <guid>`：单期重跑 AI 摘要（写回状态文件）
- `--unsubscribe <节目名>`：退订（状态文件标记"已退订"保留，可恢复）

## 阶段 1 工作流（一次性抓取）

1. **解析 URL**：识别 RSS 链接（http(s)://...xml 或 feed.xxx 域名）。
2. **抓取 RSS XML**：`curl -sSL` 拉 XML 全文。
3. **解析期数**：`python3 scripts/parse_rss.py <rss_url_or_xml_path>`
   - feed 级元数据：节目名、作者、描述、封面、link
   - per-item：title / pubDate / guid / itunes:duration / enclosure url+type+length / description / link
   - **类型识别**：检测 enclosure type 是否为 `audio/*`；不是则标记 `non_podcast` 并提示
4. **交互选择**（用户确认抓哪些）：
   - `python3 scripts/fetch_rss_feed.py <url> --list`：列出全部分期（序号/日期/时长/guid），**不下载**
   - 确认后按需下载：
     - `--pick "1,3,5-8"`：按序号/范围选（支持 `last` / `last:3` / 8 位 guid hash）
     - 无参数：默认下载最近 5 期（`--max N` 控制数量）
5. **下载音频**：对选中每期调 `yt-dlp` 下载到 `rss/audio/<sanitized_title>-<guid_hash8>.<ext>`
   - 扩展名按 enclosure type 推断（audio/mp4 -> m4a，audio/mpeg -> mp3）
6. **落 info.json**：`rss/raw/<sanitized_title>-<guid_hash8>.info.json`（feed 元数据 + item 元数据 + 本地路径）
7. **增量去重**：二次运行同一 feed 时，已有 info.json 的期数跳过（除非 `--force`）
8. **下载间隔**：每期间 sleep ≥ 5 秒（避免 CDN 压力）

> **注**：快照模式（`--pick-gen`/`--pick-file`/`选择下载/` 目录）已废弃（2026-08-11）。历史快照文件保留在 `rss/选择下载/`，新内容全走订阅模式。

## 阶段 2 工作流（ASR 转写）

下载音频后，转写成带时间戳的逐字稿：

1. **单期转写**：`python3 scripts/asr_podcast.py rss/raw/<...>.info.json`
2. **批量转写**：`python3 scripts/asr_podcast.py --all`（扫 rss/raw/ 所有 info.json）
3. **下载+转写一体**：`fetch_rss_feed.py ... --transcribe`（下载后自动转写本次新下的）
4. **增量**：已有 transcript 跳过（`--force` 强制重转）

产物：`rss/transcripts/<sanitized_title>-<guid_hash8>.transcript.md`
格式：`**[MM:SS]** 文本`（统一的分段时间戳格式，阶段 3 笔记生成兼容）

依赖：mlx-whisper（仅 Apple Silicon）+ tools/asr-poc/models/whisper-large-v3-turbo
共用模块：`_shared/asr.py`（transcribe_local + format_transcript_md）

## 阶段 3 工作流（笔记生成 + 索引 + 批量）

转写后，生成结构化中文笔记 + 索引登记：

1. **判断模式**：`python3 scripts/decide_mode.py rss/transcripts/<...>.transcript.md`
   - < 50K 字符 -> skill 模式；>= 50K 字符 -> map_reduce（rss feed 全部是对话/观点类播客，临界区一律 map-reduce，不做标题关键词判断）
2. **选模板**：`python3 ~/.agents/skills/rss-grab/scripts/adapt_template.py <transcript> --templates-dir ~/.agents/skills/rss-grab/templates`
   - 调 `scripts/adapt_template.py`（不硬编码路径），传 rss 的 templates-dir
   - 播客命中「访谈播客」模板
3. **生成笔记**：
   - skill 模式（短播客）：`python3 scripts/generate_note.py <transcript> <plan.json> --info-json <info.json> --output <note.md>`
   - map-reduce 模式（长播客 >50K）：`python3 scripts/regenerate_note.py <guid_hash8>`（自动跑 map_reduce_note.py -> final.json -> generate_note --source summary）
   - 自动追加 rss/notes/INDEX.md（按 guid_hash8 dedup）
4. **重生成**：`python3 scripts/regenerate_note.py <guid_hash8> [--mode auto|skill|map_reduce] [--keep-v1]`
5. **批量抓取**：`python3 scripts/fetch_rss_pending.py [待抓取URL/RSS.md]`
   - 读待抓取文件，批量调 fetch_rss_feed.py 落原料（不生成笔记）

产物：`rss/notes/<源名>/<sanitized_title>-<guid_hash8>.md` + 全局 `rss/notes/INDEX.md`
INDEX 列：`| 日期 | 标题 | 作者 | 时长 | 期号 | 源 | 笔记 |`

依赖：LLM_API_KEY（读 scripts/.env，模板见 .env.example）

## 路径约定

| 用途 | 路径 |
|---|---|
| 项目根 | `<project-root>/` |
| 订阅表 | `./rss/订阅/subscriptions.json` |
| 订阅状态文件 | `./rss/订阅/<节目名>.md` |
| 运行日志（定时任务时） | `./rss/订阅/.logs/` |
| 期数元数据 | `./rss/raw/<sanitized_title>-<guid_hash8>.info.json` |
| 音频文件 | `./rss/audio/<sanitized_title>-<guid_hash8>.<ext>` |
| 转写稿（阶段 2） | `./rss/transcripts/<sanitized_title>-<guid_hash8>.transcript.md` |
| 笔记（阶段 3） | `./rss/notes/<源名>/<sanitized_title>-<guid_hash8>.md` |
| 索引（阶段 3） | `./rss/notes/INDEX.md` |
| 历史快照（已废弃） | `./rss/选择下载/`（只增不删） |

## 主键与命名

- **主键**：优先 RSS `<guid>`（稳定）；无 guid 时用 `pubDate + 标题` 的 sha256 前 8 位派生
- **文件名**：`<sanitized_title>-<guid_hash8>.<ext>`（sanitized 规则：保留中英文/数字，其他替换 `-`，标题最长 60 字）
- 标题变化不丢归属（主键是 guid，不是标题）

## 依赖

- **yt-dlp**（已装，macOS：`brew install yt-dlp`）
- **curl**（已装）
- **Python 3.12+**（标准库 `xml.etree.ElementTree`，不引入 feedparser）

## 合规边界

- ✅ 只抓公开 RSS + enclosure 直链
- ❌ 不碰 DRM / 付费墙 / 会员专享
- ✅ 控制下载频率（间隔 ≥ 5 秒），个人自用不二次传播

## 已知限制（阶段 1-3）

- 只支持播客音频类 RSS（RSS 2.0 + itunes + audio enclosure）
- 文章/视频/Atom/JSON Feed：识别后提示"该 RSS 不是播客音频 feed"，不报错崩溃
- 无 `<guid>` 的 feed 用 pubDate+标题 hash 派生主键
- 畸形 XML（如爱范儿）解析失败时明确报错（阶段 1 不处理，留待后续按需启用 feedparser）
- ASR 转写仅 Apple Silicon（mlx-whisper）；长音频（35-60 分钟）转写约 2-3 分钟
- 超长播客（>2h）转写约 10-15 分钟，笔记走 map-reduce（多次 API 调用）
- 订阅模式反推依赖 Apple Podcasts 页面结构（`"feedUrl":"..."` 字段），页面改版时需更新 `resolve_apple_podcast.py`
