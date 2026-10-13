"""长文/笔记生成管线（源无关的管线本体，本仓库的转写 → 笔记全流程走它）。

> 📦 与主项目共享同一套实现，**本文件是独立实体拷贝，不是软链**。
> 按项目规则必须**手动双改 + 两边各跑测试**，**禁止写脚本单向覆盖**。
>
> **信头归调用方**：本模块只管「怎么把源文变成笔记正文」，
> frontmatter 组装与索引写盘由各调用方自己做（`rss-grab` 侧用自己的字段表）。
> 理由：字段表因源而异，把信头焊死在本模块里会让别的源迁就。
>
> **与主项目那份的差异：仅 1 处** —— `SYSTEM_PROMPT` 里那句「机器转写存在同音字误识，
> **〔另一类素材〕字符误识**，请在行文中自然纠正」的后半句在本仓库已删除
> （本仓库只处理语音转写，不处理图像类素材）。已用 AST 比对确认：**除这 1 行外
> 两份逻辑完全一致**。其余模块的差异是既有的有意分化（`asr.py` / `env.py` /
> `batch_notes.py` 为 rss-only 裁剪版）。

## 本模块负责的四步

    清洗 → ①.5 错字纠正 → ② 分块扫描 → ③ 合并去重 → ④ 撰写 → 校验

净字数超过 `LONG_DOC_THRESHOLD` 时自动走分块路径（②③），
否则维持单次调用。

## 依赖

`chunk_scan` / `correct_typos` / `clean_source` / `validate_v3` 均在同目录 `_shared/` 下。
调用方需先把该目录放进 `sys.path`。

## 输出契约

`generate_note_body()` 返回 (正文, 校验报告, 统计)，**不写任何文件** ——
frontmatter 与落盘由调用方做。这样本模块完全不需要知道数据来自哪个源。
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# 喂给模型的元信息白名单：**由调用方设置**（信头归源，不归管线）。
# 本仓库的 rss 调用方设成自己的字段表；留空则不喂任何元信息。
PROMPT_INFO_FIELDS: tuple[str, ...] = ()


def info_for_prompt(info: dict) -> str:
    """按 PROMPT_INFO_FIELDS 白名单生成喂给 LLM 的元信息文本。

    白名单机制本身是安全设计：只取显式列出的键，杜绝把 token / 内部字段一起喂进提示词。
    """
    lines = []
    for k in PROMPT_INFO_FIELDS:
        v = info.get(k)
        if v is None or v == "":
            continue
        if isinstance(v, list):
            v = "、".join(str(x) for x in v)
        lines.append(f"- {k}: {v}")
    return "\n".join(lines)

# ── 配置 ──────────────────────────────────────────────
# 环境变量一律「通用名优先、专有名兜底」（2026-10-14）：
# 本仓 env.py 的 load_env **只认**通用名 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
# （开源用户可能用任何 OpenAI 兼容服务，不该被绑死在某一家），
# 但本文件此前**只认专有名** —— 用户照 .env.example 配好通用名后，
# 笔记这一步拿到的却是写死的默认模型与服务地址，必然连不上。
#   · 通用名 LLM_* 优先，专有名 MINIMAX_* 兜底（两边都认，不删任何一边）
#   · **不要用 `or`**：`or` 会把「显式设为空串」和「未设置」混为一谈。
#     嵌套 os.environ.get 能区分二者（未设置→None，显式设空→""），
#     effort 正是靠这个差异实现「显式设空 = 调用时不发该参数」。
MODEL = os.environ.get("LLM_MODEL",
                       os.environ.get("MINIMAX_MODEL", "MiniMax-M3.1-Flash-Preview"))
BASE_URL = os.environ.get("LLM_BASE_URL",
                           os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.cn/v1"))
# EFFORT == "" 表示用户显式关掉了该参数（LLM_EFFORT= ），见下方调用处
EFFORT = os.environ.get("LLM_EFFORT", os.environ.get("MINIMAX_EFFORT", "max"))
MAX_COMPLETION = int(os.environ.get(
    "LLM_MAX_COMPLETION", os.environ.get("MINIMAX_MAX_COMPLETION", "100000")))
TIMEOUT = int(os.environ.get(
    "LLM_TIMEOUT", os.environ.get("MINIMAX_TIMEOUT", "1800")))
TEMPERATURE = 0.2
MAX_RETRY = 2
HEARTBEAT = 30

ID_RE = re.compile(r"-([0-9a-f]{24})\.(transcript|images)\.md$")

# ── 提示词 ────────────────────────────────────────────
SYSTEM_PROMPT = """你是笔记生成助手。基于下方机器转写文本，生成结构化中文笔记。

# 输出结构（硬要求）

**不要输出 H1 标题**（# 开头的行）——标题由程序写入文件头。

必须且只能包含这三个二级章节（##）：

## TL;DR
核心观点 + 适用人群 + 主要话题清单（按序号换行，每个话题单独一行）

## 要点总结
正文主体。组织方式**按内容性质选**：
- **有序列表** `1. 2. 3.`：有先后顺序、需要逐条执行的内容
- **无序列表** `-`：并列要点、罗列清单、特征枚举
- **表格**：多项事物横向对比、带维度的分类、参数对照
- **段落**：观点阐释、因果论证、需要连贯行文才能讲清的内容
- **引用块** `>`：原话、金句、嘉宾或专家的原话
- **代码块**：命令、配置、伪代码、流程示意
- **加粗要点行** `- **要点**：说明`：一句话结论带解释

其中三级小标题（###）用于分段，**标题文字直接写内容，不要加数字或汉字序号**。

不得使用其他二级章节（##）。

## 关键引用
忠实于原文的引用，可轻微润色。标题**恰好是** `## 关键引用`，不要加括号后缀。

# 覆盖度

正文主体必须覆盖转写中的全部主要话题。
**不要给话题数量设下限**——数字会诱使你为凑数而补造不存在的话题。
宁可多保留，不要为精简而合并。寒暄、重复、过渡可略。

# 禁止输出以下内容

- 视频元信息（标题 / UP主 / 时长 / 链接 / 标签）——程序自动写入文件头
- 「信息可信度」章节——你没有联网能力，无法核验任何事实
- 「错字纠正」章节——纠错记录由程序单独输出
- 任何讲者 / 嘉宾 / 主持人的人名字段
- **推广与广告内容**（详见下方「广告」一节）

# 广告

推广与广告不构成笔记内容，遇到就跳过，不要为它们单独成段或单独成行。

判据不是关键词，而是这句话的**作用**：

- 它的作用是**说服读者去看、去���、去加入、去购买** → 广告，丢掉
- 它的作用是**传递一个事实** → 信息，保留

自检方法：**把这句删掉，读者对内容的理解会不会受影响？**
不受影响 → 丢掉；会漏掉一件事 → 保留。

常见的广告形态（**举例，不是穷举**，按上面的判据自行判断）：

- 社区/社群的规模宣传（成员数、关注者数、加入方式）
- 付费课程或产品的推广（价格、学员数、评分、课时数、退款政策、赠品）
- 试用邀请、优惠码、内测名额
- 平台自夸式宣传（「最好」「第一」「遥遥领先」「最强」）

判断的是**每一句**，不是每一段：同一段里既有推广又有事实时，
只丢推广那几句，事实那几句照常写进正文。

**创始人自己讲自家产品的能力和做法不是广告**——
只有「请你来看看/买买」性质的内容才是。

【反面案例】跳过「我们的课程已迭代 13 期、3000 多名学员、
在某某平台拿满分好评、14 天无理由退款、赠送一年期会员」

【正面案例】保留「产品每天发布一个 stable 版本，每个版本含上百个 PR」——
后者说的是迭代节奏，是事实

# 引用规则

- 引用应忠实于转写文本。可以**轻微润色让句子读起来顺**，
  但**不得扭曲原文观点**——数字、限定条件、因果方向、对比项保持原样
- **禁止加时间戳**：`[00:12]` `[01:04]` 一律不写
- 你的解释、评论一律放在引用块之外
- 引用条目格式：`- <引用>`（不加任何前缀修饰）

## 翻译

笔记正文一律用**中文**。源文是什么语言不影响这一点。

- 源文是外文 → 整篇**译成中文**再写笔记
- 源文是中文 → 直接写

以下**保留英文原样，不翻译**：
- 专业术语与技术名词：RAG / Agent / Transformer / prompt / token / harness /
  embedding / fine-tune / SOW / SaaS / API …
- 常见的普通英文词：should / could / the / of / and …（这类词译成中文
  反而会变成生硬直译）
- 人名、公司名、产品名、机构名的**通用写法**（如 `Anthropic`、`Orca`）

翻译时**不要顺手补充源文没有的内容**。译文里出现的每个数字、每个专名、
每条判断，都必须能在源文里找到对应。不要因为"读者可能不知道"就加背景说明
或量化数据——那是补充，不是翻译。

## 引用里的翻译

- 源以外文为主 → 每条引用**写成两行**：第一行中文译文（供阅读），
  第二行缩进 `  > <外文原句>`（供核对原视频）。第二行必须是源文里的原句
- 源为中文 → 单行即可

# 段落规则（下游按段落切片后独立检索）

- 每段必须自足：不以「它 / 这个 / 该方法 / 上述 / 前面提到」等指代开头
- 首次出现的专有名词写全称
- 不把一个完整论述拆碎

# 具名判据与具体数字

- 访谈中**具名嘉宾给出的可操作判据**（判断标准、提问方式、心法）必须保留，不能只留结论。
  写"要判断客户是否真的会付费"而不是"访谈提到要重视客户需求"。
- 源里出现的**具体比例、量级、金额**优先保留原数值，不要概括成"大量""很多""不少"。

# 人物规则

- 正文任何位置**不得出现人名元字段**（如"嘉宾：""讲者：""主持人："）。
- 人名只允许出现在 `## 关键引用` 内，或作为原文内容自然出现；
  具名判据的表述里可保留说话人身份（"这位嘉宾给出的判据是…"），但不设独立的元字段行。

# 错字处理

- 机器转写存在同音字误识，请在行文中自然纠正
- 同时把每次纠正记录进末尾 JSON 的 typos 数组
- 只记录明确无误的；不确定的不记
- 同一处有多种可能错法时不要合并成一条，直接跳过

# 末尾输出

笔记 Markdown 正文结束后，空一行，写一行 `---JSON---`，再写一个 JSON 对象。
全文只输出这三部分，不要任何额外说明。"""

USER_TPL = """## 笔记元信息
{info}

## 机器转写文本（全文）
{src}

## 末尾 JSON 格式
{{
  "summary": "不超过60字的一句话概括",
  "typos": [
    {{"original": "原样抄写", "target": "纠正后", "reason": "依据"}}
  ]
}}"""

# ── 长文路径：源太长（净字数 > 15000），一次调用装不下 ──
# 源文清洗后的字符数下限。低于此值不调 LLM（见 generate_note_body 内的说明）。
# T2.7: source-file suffixes stripped from the stem. Data-driven so that
# adding a type is one line and cannot half-apply again.
# NOTE: this rss-only repo only ever sees '.transcript.md'. The main project
# keeps a second entry for its other source type -- that literal is exactly
# what the open-source boundary forbids here, so it is not carried over.
_SOURCE_SUFFIXES = ('.transcript.md',)

MIN_SOURCE_CHARS = 200
CHUNK_SIZE = 10000
CHUNK_OVERLAP = 1
SCAN_EFFORT = "xhigh"
MERGE_EFFORT = "low"
# 单篇内部的分块扫描并发。**可热调**：A8 每篇是独立 python 子进程，
# 改这里只对之后启动的篇目生效，**不会影响正在跑的篇目，也不用中断任何任务**。
# 2026-10-04 由 5 提到 8：扫描是纯网络等待（实测本机 CPU 全空闲），
# 峰值 LLM 并发 = 篇目并发 × 本值。
SCAN_CONCURRENCY = 8

# ⚠️ 2026-10-05 T1.14.2：净字口径与长文阈值此前在本文件里**另抄了一份**
# （`_TS_STRIP` + `net_len` + `LONG_DOC_THRESHOLD`），而 test_long_doc.py 只测
# long_doc 那份 —— 副本从未被验证，且两处各改各的必然漂移。
# 现在统一从 long_doc 引。口径只有一处定义 = 只可能被测到一次。
from long_doc import LONG_DOC_THRESHOLD, net_len  # noqa: E402,F401

# 长文路径喂给 ④ 的是「主题清单 + 原文」：
#   主题清单 = 覆盖全文的大纲，防遗忘
#   原文     = 一手事实，④ 可用它核对、纠正清单里被压缩掉的细节
# 两者都给的理由：只喂清单，④ 手上一手材料为零，主题被压平的部分无法回补；
# 只喂原文，长转写容易注意力分散。两者结合最稳。
# ── 长文路径 ④ 系统提示词（2026-10-01 定稿）────────────────
# 设计依据：③ 主题层实测 89.0%，而旧 ④ 提示词产出只有 68.7%，
# 丢的 20 个点几乎全是「把 detail 改写成更顺的话」造成的。
# 所以把 ④ 从「作者」降级为「排版员」：可重新分节、可轻微润色，
# 但信息量不得低于输入。
# 注：本提示词**不含任何数量约束**（H3 个数 / 引用条数 / 句数）——
#     数量一旦写死，模型会为凑数强行合并或拆分，反而制造丢失。
SYSTEM_PROMPT_TOPICS = """你是一个笔记撰写者。下方是一篇长转写经分块扫描 + 合并去重后得到的**主题清单**。

主题清单已经覆盖了全文主干。你的任务：把它排版成一篇笔记。

## 核心原则

**输出的信息量不得低于输入的主题清单。**

可以做的事：
- 按内容逻辑重新分节、调整顺序
- 为读起来顺畅做轻微润色、理顺句子

不可以做的事：
- 删掉任何主题
- 丢掉细节、数字、出处、限定条件
- 用一句"总之""类似地"代替原本具体的内容

**写完自检**：主题清单里有的东西，是不是都在笔记里了？少了就补回去。

## 必须原样保留的内容

润色句子可以，但下面这些一旦改动就是信息损失：

- **具体数字与单位** —— `400 多个 worktree`、`200 美元`、`90%`、`十一分之一`。
  数字不能约数化（`49000 颗星` 写成 `接近 5 万` 是损失）
- **专有名词** —— 产品名、公司名、人名、技术术语，**照抄清单里的写法**
- **因果关系** —— 因为 / 所以 / 因此 / 导致
- **限定条件** —— 如果 / 只有…才 / 不能保证 / 仅限 / 前提下
- **对比项** —— `不是 AWS` 里的 `AWS` 不能省

## 只还原，不补充

你写的是一篇**可信原料层**笔记，下游会拿它当事实依据。

**可以用你的领域知识判断「这个写法到底指什么」**（那是纠错），
**不能用领域知识往里填源文没有的东西**（那是编造）。

这两种情形最要警惕，是实测里反复出问题的地方：

- **源文只给了定性表述**（「更好」「明显优于」「much better」）
  → **不要补出具体的比赛名、榜单名、指标名、百分比**
  实测踩坑：源文只说某网络「明显优于传统计算机视觉系统」，
  笔记写成「在 ImageNet 竞赛的错误率远低于既有系统」——
  源文里既没有 ImageNet 这个名称，也没有任何错误率数字。

- **源文给了举例或类比**（「一万个人各学一门课再交流」）
  → **不要把类比的量级转成结论性数据**
  实测踩坑：源文说某模型像「一万个人共享知识」那样学习，
  笔记补出「因此比任何人知道多成千上万倍、只使用了约 2% 的权重」——
  这两个数字源文里完全不存在。

- **源文说「某主体认为/相信/主张 X」，而现实是 Y**
  → **笔记必须保留 X，并写明这是该主体的看法。不得把 X 改写成 Y。**
  实测踩坑：源文说机器人在棱镜后仍认为「物体在它指的位置」
  （真实位置是正前方，两者背离），讲者正是用这个背离论证
  机器人拥有「主观体验」；笔记改成「物体在正前方」——
  机器人和现实一致了，**那个背离正是论证的立足点**，被抹掉了。

  一句话：**主体的错误信念本身就是信息，不要"帮"它纠正。**

- **列举同类项时，模型会按"这一类还该有谁"的常识补名单**
  → **只写主题清单里出现过的那些，一个都不要加。**
  实测踩坑（2026-10-04，4 篇超长文对照）：源文在讲视频生成模型时只提了
  某几个，笔记自行补上了 `Lovart`、`Google Veo`、`Sora 时代`；
  讲 AI 编程工具时补了 `Dify`。**源文里这些名字 0 命中**。
  短文里没出现这个毛病，**长文分块后才集中爆发** ——
  分块让模型失去全文语境，于是退化成"按类别回忆"。

  典型现场：源文列举竞品时只给了「传统自动化 / 云平台 / 通用 Agent」三个
  泛称，笔记却补上了三个具体公司名。**泛称就该保持泛称**，
  除非主题清单里真的有具体名字。

  ⚠️ **本条只管「名单」，不管「论证」。**
  （2026-10-04 修正：初版没写清这句，验证时把**论证链也削掉了** ——
  实测两篇笔记的主题未覆盖率升到 9.1% / 13.0%，被删的包括
  「Informer → Google Times FM → LagLama → 清华 Timer XL」这条学术脉络、
  「空调设 24℃ 从 26→25→24」这个论证例证。它们源文里 100% 存在，
  是支撑结论的依据，不是编造。）

  下面这几类**不受本条约束，照写不误**：
  - **因果论证链**：因为 / 所以 / 推论 / 依据 —— 源文给了几条就该写几条
  - **发展脉络**：从 A 到 B 到 C 的演进、师承、引用关系
  - **举例佐证**：源文为说明某个结论所举的具体例子（哪怕只有一个）
  - **单条的泛称清单**：源文说「几个大厂」就写「几个大厂」，
    **不要为了凑数编具体名字**

**共同点**：这四种编造模式都不是"忘了核对"，而是**在补全**——
模型觉得自己知道得更全，就把知道的东西填了进去。补的东西单看都合理，
合起来就是编造。

判据：**笔记里的每一个数字、每一个专有名词、每一项指标，
以及每一个"同类项名单"里的每一项，
都必须在主题清单里找到对应。找不到就不要写。**
**但这条判据只管「来源」，不管「详略」** ——
只要能在主题清单里找到，就该写全，不要因为怕出错就省略。


## 翻译

笔记正文一律用**中文**，源文是什么语言不影响这一点。源文是外文就**整篇译成中文**。

以下**保留英文原样，不翻译**：
- 专业术语与技术名词：RAG / Agent / Transformer / prompt / token / harness /
  embedding / fine-tune / SOW / SaaS / API …
- 常见的普通英文词：should / could / would / the / of / and …（译成中文反成生硬直译）
- 人名、公司名、产品名、机构名的通用写法（如 `Anthropic`、`Orca`）

译文同样受上一节约束：**不得出现源文没有的事实。**

## 结构

- 三个二级标题，顺序固定：`## TL;DR` → `## 要点总结` → `## 关键引用`
- `## 要点总结` 下面用三级标题分节，**分几节你自己定，按内容自然边界划分**
- 三级标题不加数字或汉字序号
- 不要出现空节

## 段落形态

三级标题下面的内容，**组织方式按内容性质选**：

- **有序列表** `1. 2. 3.`：有先后顺序、需要逐条执行的内容
- **无序列表** `-`：并列要点、罗列清单、特征枚举
- **表格**：多项事物横向对比、带维度的分类、参数对照
- **段落**：观点阐释、因果论证、需要连贯行文才能讲清的内容
- **引用块** `>`：原话、金句
- **代码块**：命令、配置、伪代码、流程示意
- **加粗要点行** `- **要点**：说明`：一句话结论带解释

同一篇里不同节可以用不同形态，按这节讲的是什么来定。
**不要整篇只用一种形态**——全部写成散文是最难读的一种排版。
也不需要为了凑形态把内容拆碎：形态服务于可读性，不改变信息量。

## TL;DR

- 说清核心结论和适用人群
- 这里不承载细节，细节由要点总结负责

## 关键引用

- 从主题 detail 里挑最有代表性的句子
- **可以轻微润色让句子读起来顺**，但**不得扭曲原文观点**：
  数字、限定条件、因果方向、对比项都要保持原样
- 覆盖全文不同部分，数量你判断
- 格式：`- 引用`（普通列表项，句首**不要**加 `>` 引用符）

## 广告

社区推广、试用邀请、付费课程（价格 / 学员人数 / 评分 / 课时数）不要单独成段。

## 输出格式

正文 markdown，空一行，写 `---JSON---`，再写一个 JSON 对象。
全文只输出这三部分，不要额外说明。"""


USER_TPL_TOPICS = """## 笔记元信息
{info}

## 主题清单（共 {n} 个主题）

> 这是全文的主干覆盖清单。
> 排版时可以重新分节和轻微润色，但**信息量不能低于这份清单**。

{topics}

## 末尾 JSON 格式
{{
  "summary": "不超过60字的一句话概括",
  "typos": [
    {{"original": "原样抄写", "target": "纠正后", "reason": "依据"}}
  ]
}}"""


def topics_block(topics: list[dict]) -> str:
    """主题清单 → 喂给 ④ 的文本。"""
    lines = []
    for i, t in enumerate(topics, 1):
        d = (t.get("detail") or "").strip()
        fb = []
        if t.get("has_number"):
            fb.append("含数字")
        if t.get("has_person"):
            fb.append("含人名")
        mark = f" [{','.join(fb)}]" if fb else ""
        lines.append(f"{i}. {t.get('title', '').strip()}{mark}"
                     + (f"\n   {d}" if d else ""))
    return "\n".join(lines)

JSON_SEP = "---JSON---"


# ── LLM 调用 ──────────────────────────────────────────
def strip_thinking(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def parse_output(raw: str) -> tuple[str, dict]:
    """三层兜底解析：剥 think → 剥 ```json 围栏 → 取首个完整 JSON 对象。"""
    text = strip_thinking(raw)
    if JSON_SEP in text:
        body, tail = text.split(JSON_SEP, 1)
    else:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            raise ValueError("输出中找不到 JSON")
        body, tail = text[: m.start()], m.group(0)
    tail = tail.strip()
    tail = re.sub(r"^```(?:json)?\s*", "", tail)
    tail = re.sub(r"```\s*$", "", tail).strip()
    try:
        return body.strip(), json.loads(tail)
    except json.JSONDecodeError:
        s = tail.find("{")
        e = tail.rfind("}")
        if s < 0 or e <= s:
            raise ValueError("JSON 解析失败")
        return body.strip(), json.loads(tail[s: e + 1])


# ── 校验失败回灌 ──────────────────────────────────────
# 此前重试是**同参数盲重试**：retries_needed 只进了 print 字符串，从未回灌模型，
# 于是连着 3 次用同样参数、同样失败（validate() 的 docstring 却承诺
# 「编排层应带错误信息重试」）。
# 刻意克制：只把失败原因拼成一句附加指令附在 user 消息末尾，
# 不重发提示词、不复述规则 —— 提示词本身已是几万字符，再加只会稀释约束。
RETRY_HINT_HEAD = '【上一次输出未通过校验，请修正后重出】'


def build_retry_hint(reasons) -> str:
    """把 validate() 的 retries_needed 压成一句附加指令；无原因则返回空串。

    返回空串是**正常路径**（首次生成），调用方不必特判。
    """
    rs = [str(r).strip() for r in (reasons or []) if str(r).strip()]
    if not rs:
        return ''
    return RETRY_HINT_HEAD + '：' + '；'.join(rs) + '。'


def call_llm(client, source: str, info: dict, heartbeat: bool = True,
             retry_hint: str = "") -> str:
    """调 LLM，带心跳线程（长稿可能跑 5 分钟+）。

    retry_hint：校验失败原因（可选），非空时附在 user 消息末尾回灌给模型。
    """
    return _call_with_template(client, USER_TPL, source, info, heartbeat,
                               retry_hint=retry_hint)


def call_llm_topics(client, topics_text: str, info: dict,
                    n_topics: int = 0, heartbeat: bool = True,
                    retry_hint: str = "") -> str:
    """长文路径 ④：只喂主题清单，用排版员提示词。

    实测加原文会让保真率从 68.7% 掉到 57.7%（76K 上下文稀释大纲的约束力），
    因此**不传 src**。

    retry_hint：同上，两条路径一致。
    """
    return _call_with_template(client, USER_TPL_TOPICS, topics_text, info,
                               heartbeat, sys_p=SYSTEM_PROMPT_TOPICS,
                               n_topics=n_topics, retry_hint=retry_hint)


def _call_with_template(client, tpl: str, payload: str, info: dict,
                        heartbeat: bool, sys_p: str | None = None,
                        n_topics: int = 0, retry_hint: str = "") -> str:
    user = tpl.format(
        info=info_for_prompt(info),
        # 2026-10-08：曾有独立的「## 笔记描述」区块（占位符 {desc}），与
        # 「## 笔记元信息」里的 description 完全重复（实测两处一字不差），
        # 已连区块一起删除。简介现在**只经白名单**走 info_for_prompt() 一条路，
        # 故不再传 desc=。对应护栏见 tests/test_no_duplicate_desc_block.py。
        src=payload,   # 长文模板不用 {src}（实测加原文掉保真率），保留占位以兼容短文模板
        topics=payload,
        n=n_topics,
    )
    if retry_hint:
        user = user.rstrip() + "\n\n" + retry_hint
    stop = threading.Event()

    def _beat():
        t0 = time.time()
        while not stop.wait(HEARTBEAT):
            print(f"    … 仍在生成 {int(time.time() - t0)}s", flush=True)

    th = None
    if heartbeat:
        th = threading.Thread(target=_beat, daemon=True)
        th.start()
    try:
        # EFFORT 被显式设成空串（LLM_EFFORT=）→ **不发** reasoning_effort。
        # 部分 OpenAI 兼容服务不接受这个参数，无条件传会直接 400。
        effort_kwargs = {} if EFFORT == "" else {"reasoning_effort": EFFORT}
        r = client.chat.completions.create(
            model=MODEL,
            **effort_kwargs,
            max_completion_tokens=MAX_COMPLETION,
            temperature=TEMPERATURE,
            timeout=TIMEOUT,
            messages=[{"role": "system",
                       "content": sys_p or SYSTEM_PROMPT},
                      {"role": "user", "content": user}],
        )
        return r.choices[0].message.content or ""
    finally:
        stop.set()


# ── 单篇 ──────────────────────────────────────────────
def run_long_path(src_path: Path, source: str, client,
                  work_dir: Path, no_typos: bool = False) -> dict:
    """长文路径：①.5 错字纠正 → ② 分块扫描 → ③ 合并去重。

    返回 {"topics": [...], "typo_report": {...}, "scan_stats": {...}}
    """
    from chunk_scan import run_merge, run_scan, split_chunks
    from correct_typos import run_file

    work_dir.mkdir(parents=True, exist_ok=True)
    clean_path = work_dir / (src_path.name.replace(".md", ".clean.md"))
    typos_path = work_dir / (src_path.name.replace(".md", ".typos.json"))

    # ①.5 错字纠正：**全流程唯一一次替换**。
    # 2026-10-03 起模型输出的是「逐处判断（段号 + 原文 + 决定）」，
    # 程序按段精确定位——不再输出映射表做全文替换。
    # 四组实测误伤均为 0，见 docs/实验/错字纠正方案实测-20261003.md
    if no_typos:
        # 对照实验用：跳过 ①.5，直接把原文复制成 clean
        import shutil as _sh
        _sh.copyfile(src_path, clean_path)
        print("    [长文] --no-typos：跳过 ①.5 错字纠正（对照实验）", flush=True)
    else:
        typo_report = run_file(src_path, clean_path, typos_path, client=client)
        print(f"    [长文] 错字纠正 判定 {typo_report['judgments']} 处 / "
              f"落成 {typo_report['applied_count']} 处 / "
              f"成功率 {typo_report['success_rate']:.1%}", flush=True)

    clean_text = clean_path.read_text(encoding="utf-8")

    # ② 分块扫描：**只消费 clean 原文，不再二次套错字规则**
    # 2026-10-02 移除：此前 ② 前与 ③ 后各再套一次同一套规则，等于把
    # ①.5 的替换结果又过一遍替换器。两次替换的时序本身就是 bug 温床——
    # 实测 `ghosty→Ghostty` 在 ①.5 已经产出正确的 Ghostty，② 阶段
    # `ghost→Ghostty` 又去吃这个 Ghostty（Ghost 是 Ghostty 的真子串），
    # 11 处全被改成 Ghosttyty 并一路传到终稿。替换只该发生一次。
    chunks = split_chunks(clean_text, CHUNK_SIZE, CHUNK_OVERLAP)
    pts = run_scan(chunks, work_dir, client, SCAN_EFFORT,
                   concurrency=SCAN_CONCURRENCY, stagger=2.0,
                   empty_retries=1)
    (work_dir / "points.json").write_text(
        json.dumps(pts, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"    [长文] ② 扫描 {len(chunks)} 块 -> {len(pts)} 要点", flush=True)

    # ③ 合并去重（同样不再套错字规则）
    obj = run_merge(pts, work_dir, client, MERGE_EFFORT, "topics")
    topics = obj.get("topics", [])
    print(f"    [长文] ③ 合并 -> {len(topics)} 主题", flush=True)
    if not topics:
        raise RuntimeError("合并去重返回 0 主题，长文路径无法继续")
    return {"topics": topics, "typo_report": typo_report,
            "clean_text": clean_text,
            "n_points": len(pts), "n_chunks": len(chunks)}


# ── 编排：产出正文 ─────────────────────────────────────────
def generate_note_body(src_path: Path, info: dict, client, work_dir: Path, typos_dir: Path,
                      no_typos: bool = False) -> tuple[str, dict, dict]:
    """跑完四步，返回 **(正文, 校验报告, 统计)**。

    **本函数不写任何文件** —— frontmatter 组装与落盘由调用方做。
    这样本模块完全不需要知道数据来自哪个源（AC-C2 的「信头归调用方」）。

    流程：
        净字数 > LONG_DOC_THRESHOLD → ①.5 纠错 → ② 分块扫描 → ③ 合并去重
                                        → ④ 撰写 → 校验
        否则                      → ①.5 纠错 → 单次调用 → 校验

    Args:
        src_path: 源文路径（`<title>-<id>.transcript.md`）
        info:     元信息 dict；喂给 LLM 的部分按模块级 `PROMPT_INFO_FIELDS` 白名单取
        client:   OpenAI 兼容客户端
        work_dir: 中间产物目录（纠错后的 clean.md / points / topics）
        typos_dir: 纠错报告落盘目录（`*.typos.json`，供溯源）

    Returns:
        (body, report, stats)。`report` 是 `validate()` 的报告 dict；
        `stats` 含 route / net_chars / typo_applied / attempts 等，
        以及 `stats['llm']` —— LLM 产出的 `summary`（**信头要用**，
        它不在 `report` 里）。`tags` **不在**其中：v3 不产出标签，
        `stats['llm']` 经 `llm.pop("tags", None)` 硬过滤保证不含该键。

    Raises:
        RuntimeError: 校验连续 MAX_RETRY+1 次未过，或分块路径失败。
    """
    from clean_source import load_and_clean
    from validate_v3 import validate
    # T2.7: strip every known source-file suffix, not just one.
    # A source whose suffix was not stripped got its report named
    # '<title>-<id><suffix>.typos.json' (suffix left in the middle).
    for _sfx in _SOURCE_SUFFIXES:
        if src_path.name.endswith(_sfx):
            stem = src_path.name[: -len(_sfx)]
            break
    else:
        stem = src_path.name
    stats: dict = {'stem': stem, 'route': '', 'attempts': 0}
    source, cstat = load_and_clean(src_path)
    stats['source_chars'] = len(source)
    stats['clean'] = cstat

    # ── 源文下限闸门（三源共享）──
    # 源文几乎没有内容时，纠错、分块、撰写全是白烧 LLM 调用，产出必然是空壳。
    # 阈值来自实测分布：无内容的源文清洗后 44-73 字符；下一档（确实有内容）
    # 从 298 字符起。200 落在两者中间，两侧都留了余量。
    # 全库 320 篇实测只有 4 篇命中。
    #
    # 只依赖长度，不依赖 asr_status —— 那是某一源独有的 info 字段，
    # 换成 transcript_status 后两边都取不到，留着就是只对一部分生效的闸门。
    if len(source) < MIN_SOURCE_CHARS:
        stats['status'] = 'empty'
        print(f"    ⏭️ 源文仅 {len(source)} 字符（< {MIN_SOURCE_CHARS}）→ 跳过正文生成",
              flush=True)
        return '', {'C0_empty_source': {'chars': len(source)}}, stats

    n_chars = net_len(source)
    is_long = n_chars > LONG_DOC_THRESHOLD
    stats['net_chars'] = n_chars
    stats['route'] = 'chunked' if is_long else 'single'
    print(f'    净字数 {n_chars} → {('长文分块路径' if is_long else '单次调用路径')}', flush=True)
    long_info = None
    try:
        from correct_typos import run_file
        if is_long:
            pass
        elif no_typos:
            # 对照实验用：跳过 ①.5，source 保持原文。
            # stats 显式标出「纠错被跳过」，避免产出看起来像跑过纠错的笔记。
            print("    [短文] --no-typos：跳过 ①.5 错字纠正（对照实验）", flush=True)
            stats['typo_skipped'] = True
        else:
            wr = work_dir / stem
            typo_report = run_file(src_path, wr / f'{stem}.clean.md', typos_dir / f'{stem}.typos.json', client=client)
            n = typo_report['applied_count']
            if n:
                print(f'    [短文] 错字纠正 落成 {n} 处 / 成功率 {typo_report['success_rate']:.1%}', flush=True)
            source = (wr / f'{stem}.clean.md').read_text(encoding='utf-8')
            stats['typo_applied'] = n
            stats['typo_success_rate'] = typo_report['success_rate']
    except Exception as e:
        print(f'    ⚠️ 短文错字纠正失败，沿用原文：{type(e).__name__}: {str(e)[:110]}', flush=True)
        stats['typo_error'] = f'{type(e).__name__}: {str(e)[:160]}'
    if is_long:
        wr = work_dir / stem
        try:
            long_info = run_long_path(src_path, source, client, wr, no_typos=no_typos)
        except Exception as e:
            print(f'    ✗ 长文路径失败: {type(e).__name__}: {str(e)[:160]}', flush=True)
            raise RuntimeError(f'长文路径失败: {type(e).__name__}: {e}') from e
        stats['n_points'] = long_info['n_points']
        stats['n_chunks'] = long_info['n_chunks']
        stats['n_topics'] = len(long_info['topics'])
        # 2026-10-05 补：长文的纠错在 run_long_path 内部完成，其报告原先**只返回
        # 给调用方打印，从未回填 stats**。而 stats['typo_applied'] 只有短文分支
        # 会赋值 → 于是**所有长文笔记的统计行一律显示「纠错=0 处」**，哪怕实际
        # 落成了 160 处。不影响产物质量（clean 文本确实被 ② 消费了），
        # 但会让任何拿这个字段做验收的判断得出错误结论。
        tr = long_info.get('typo_report') or {}
        stats['typo_applied'] = tr.get('applied_count', 0)
        stats['typo_success_rate'] = tr.get('success_rate', 0.0)
        stats['typo_judgments'] = tr.get('judgments', 0)
        stats['typo_coverage'] = tr.get('coverage', 0.0)
    last_err = None
    retry_hint = ""          # 首轮为空；校验不过后带上真原因回灌
    for attempt in range(1, MAX_RETRY + 2):
        stats['attempts'] = attempt
        try:
            if is_long:
                raw = call_llm_topics(client, topics_block(long_info['topics']), info, n_topics=len(long_info['topics']), retry_hint=retry_hint)
                body, llm = parse_output(raw)
                v = validate(body, long_info['clean_text'], info, llm)
            else:
                raw = call_llm(client, source, info, retry_hint=retry_hint)
                body, llm = parse_output(raw)
                v = validate(body, source, info, llm)
            if not v['ok']:
                last_err = f'校验未过: {v['retries_needed']}'
                # 关键：把真原因带上，否则下一轮还是同参数盲赌
                retry_hint = build_retry_hint(v['retries_needed'])
                stats['last_retry_hint'] = retry_hint
                print(f'    ⚠️ 第 {attempt} 次校验未过: {last_err}', flush=True)
                continue
            # LLM 产出的 summary 在 llm 里，不在校验报告里 ——
            # 2026-10-04 漏掉这一步，导致 rss 薄壳写出的 frontmatter
            # `summary` 恒为空。放进 stats 一起返回。
            # tags 不再走这里：v3 不产出标签，打标由本仓库外的独立步骤负责，
            # 两个模板的 JSON 格式里已删掉 tags —— 但那只保证「模型大概率不吐」，
            # 下面这行是整块透传，模型若仍返回 tags 照样会进 stats。
            # 所以透传前硬过滤，不给这个已废弃的字段留缝。
            llm.pop("tags", None)
            stats['llm'] = llm
            # ⚠️ 必须返回 v['body']，不能返回原始 body。
            # validate() 里那一整条归一化链（H3 去编号、重新编号、剥时间戳、
            # TL;DR 去重、引用格式统一）全都作用在 v['body'] 上；返回原始 body
            # 等于把归一化结果直接丢弃——H3 编号 `### 1.` 会原样落盘。
            # 2026-10-04 实测：ok=True 的生产路径上 validate 归一化了 2 个 H3，
            # 文件里仍留着编号。归一化器是死代码，从未生效过。
            return (v['body'], v['report'], stats)
        except Exception as e:
            last_err = f'{type(e).__name__}: {e}'
            print(f'    ✗ 第 {attempt} 次生成异常: {last_err}', flush=True)
    raise RuntimeError(f'撰写连续 {MAX_RETRY + 1} 次失败，最后一次: {last_err}')
