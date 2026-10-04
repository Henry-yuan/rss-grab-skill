"""ASR 转写的同音错字纠正（段内逐处判断 → 程序确定性替换）。

> ⚠️ **本文件是 rss-grab 仓库的一份实体拷贝，不是主项目的软链。**
> 另一份在主项目的 `_shared/`（三源共用完整版）。按项目规则必须**手动双改 + 两边各跑测试**，
> **禁止写脚本单向覆盖** —— 那会抹掉本仓库的裁剪。
>
> **本版已裁掉本项目不公开的那类素材的注释**：其举例不保留；
> 但**踩坑教训一律保留**（改写措辞，不删内容），否则后人重新实现时会踩同一个坑。
"""
from __future__ import annotations

import difflib
import json
import os
import re
import time
import unicodedata
from collections import Counter
from datetime import datetime
import pathlib
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# 输出预算：必须够 max 档的 reasoning 消耗
# （实测 58K 字符输入 + max 档时，20000 会被 thinking 吃光导致 content 返回空）
MAX_COMPLETION = int(os.environ.get("MINIMAX_MAX_COMPLETION", "100000"))
TIMEOUT = 1800

# max 档返回空 content 时，降级重试的顺序
EFFORT_FALLBACK = ["max", "xhigh", "high", "medium"]
# ── 已知听写混淆规律（提示，不是映射表）────────────────────────
# 2026-10-03 起替代旧的 22 条 `A → B` 映射表。三点变化：
#   1. 措辞从「X 就是 Y」改成「X 常被听成 Y」——给规律不给结论
#   2. 只留样本量 ≥7 的声学稳定项，低频个案（`C-Dance` 1 次、
#      `Cocoat` 3 次）一律删除——`C-Dance → Claude Code` 已被实测证伪
#      （模型结合 video frame / editing 上下文判为 Seedance 才对）
#   3. `OpenClaw → Orca`（22 次）也删除：OpenClaw 是另一个真实存在的
#      开源产品，源里在指称它就不是错拼
ACOUSTIC_HINTS = """【已知的听写混淆规律】
以下是**声学规律**，不是结论。规律只说明「哪些读音容易被听错」，
**具体该怎么改必须你自己结合上下文判断**——同样的拼写在不同语境下
可能指不同的东西。
- `Clocko` / `clock code` / `Cloud Code` / `cloudcode` / `Cocoat` /
  `clockhold` / `clockcode` 这几个读音常被听成 `Claude Code`
  （20 篇实测共 76 次，几乎全是）
- `Cloud Codex` 常被听成 `Codex`（12 次）
- `chain box` 常被听成 `chat box`（16 次）
- `clockflare` / `Clockflare` 常被听成 `Cloudflare`（7 次）
- `immunator` 常被听成 `emulator`（4 次）
- 逐字母读的产品名容易串位（例：`P-L-O-D` 可能是 `Plaud`）"""


DETECT_SYS = """你是一名 ASR（语音转文字）校对员。语音识别不可能 100% 准确，有些词被听错了。
你的工作是找出**被听错的词**并给出正确写法。

# 输入格式

内容被切成若干段，每段前面有编号 `L1` `L2` `L3`……
**编号只是定位用的，不属于正文内容。** 判断时要读段内的实际文字。

# 三条纪律

**一、不要凭读音相似就替换。**
同一段材料里可能同时存在多个读音相近的真实事物——一个开源工具和一个商业产品、
或者两种不同的产品名。只有当上下文证据明确指向某个真实事物、而当前写法在那个
语境下讲不通时，才算错字。

**二、拿不准就不改。**
漏改只是不好看，误改是内容失真，两者不对等。默认倾向保持原样。

**三、英文的语法功能词和常用词不是错拼对象。**
`should` / `could` / `would` / `the` / `of` / `and` 这类词在正常英文里反复出现，
**不要把它们改写**。特别地：**纠错绝不能增加或删除词语**——
把 `should` 改成 `shouldn't` 是改变句子意思，不是纠错。

**四、专有名词和普通词都要查。**
不只查英文音近错拼，**中文的音近错字同样要查**（如 `虎精`→`虎鲸`、
`化销`→`花销`、`有余`→`犹豫`、`线上级`→`现象级`），
以及中文专有名词的误识（如把产品名听成别的东西）。
凡是你能指出「上下文里明明应该是 X」的，都算候选。

# 判断方法

对每个可疑的词：
1. 看它在**上下文中讲的是什么**（这段在讨论什么、涉及哪些事物）
2. 问：这个写法在该语境下**讲得通吗**？有没有一个**真实存在**的事物，恰好更讲得通？
3. 有明确证据 → `replace`；本来就没错或没证据 → `keep`；拿不准 → `uncertain`

# 输出要求

只输出 JSON。**每一个** replace 判断都要给出段编号和依据。

```json
{
  "judgments": [
    {
      "seg": 23,
      "original": "该段里那个可疑的词（原样照抄）",
      "decision": "replace | keep | uncertain",
      "target": "replace 时填正确写法，否则填 null",
      "evidence": "上下文哪句支持这个判断（一句话）"
    }
  ]
}
```

- 同一处只能给一条判断；同一个词在多段出现时，**每段单独判断**，各自报自己的编号
- 段号写成整数（23），不要写 `L23`
- 只输出 JSON，不要任何其他文字

""" + ACOUSTIC_HINTS


DETECT_USER = """以下是待校对的转写内容（每段前面有 L编号）：

{numbered}

## 输出格式
{{
  "judgments": [
    {{
      "seg": 段号整数,
      "original": "该段里那个可疑的词",
      "decision": "replace | keep | uncertain",
      "target": "正确写法，否则 null",
      "evidence": "依据"
    }}
  ]
}}"""


# ── 段编号：按源文**已有的**切分给每段编号 ───────────────────
# 不新造切分规则：ASR 源文本身按 `**[mm:ss]**` 分段
# 分段，无标记时按空行分段。编号由程序生成，模型只需抄数字——
# 它抄不错，而它抄不错才是这套方案的全部意义
# （2026-10-03 实测：让模型复述原文上下文当定位，21 条里 16 条匹配失败，
#   因为它会把 `**[16:57]** ghosty,` 读通成「你之前好像还提到了一个词ghosty」，
#   原文里根本没有这种连续文本）。
SEG_HEAD = re.compile(r"^\*\*\[\d{1,2}:\d{2}\]\*\*|^#{1,3}\s")
# 段首标记（比 SEG_HEAD 多吃掉标记本身和它后面的空白）
SEG_LEAD = re.compile(r"^\s*(?:\*\*\[\d{1,2}:\d{2}\]\*\*|\#{1,3}\s+)[ \t]*")
TS_MARK = re.compile(r"^\*\*\[\d{1,2}:\d{2}\]\*\*", re.M)


def split_segments(text: str) -> list[str]:
    """切成可寻址段。优先用源文自带的标记，没有则按空行切。"""
    has_ts = bool(TS_MARK.search(text))
    segs: list[str] = []
    cur: list[str] = []
    for ln in text.split("\n"):
        if has_ts and SEG_HEAD.match(ln):
            if cur:
                segs.append("\n".join(cur))
            cur = [ln]
        elif not has_ts and not ln.strip():
            if cur:
                segs.append("\n".join(cur))
            cur = []
        else:
            if cur or ln.strip():
                cur.append(ln)
    if cur:
        segs.append("\n".join(cur))
    return segs


def _norm_seg(s: str) -> str:
    """段归一化：剥段标记、去标点空白、NFKC、小写。

    归一化是必要的——模型给的 `original` 常带多一个字
    （实测有把「进步」报成「进步的」的情况），精确匹配会漏。
    """
    s = re.sub(r"^\s*\*\*\[\d{1,2}:\d{2}\]\*\*\s*", "", s)
    s = re.sub(r"^\s*#{1,3}\s+", "", s)
    return re.sub(r"[^\w\u4e00-\u9fff]", "",
                  unicodedata.normalize("NFKC", s).lower())


def build_index(text: str) -> tuple[list[str], list[list[str]], list[list[int]]]:
    """→ (段列表, 每段归一化串, 每段归一化字符 → 原文下标)。

    第三项是把「归一化位置」映射回「原文绝对位置」的桥，
    没有它就无法在不改动原文结构的前提下精确替换。
    """
    segs = split_segments(text)
    norms, idxmaps = [], []
    pos = 0
    for sg in segs:
        start = text.index(sg, pos)
        pos = start + len(sg)
        # 段首的 `**[mm:ss]**` / `## ` 标记**不是正文**，
        # 必须整体跳过。跳过它不是洁癖——不跳过的话归一化串会带上
        # "0000" 这类前缀，且 idxmaps[0] 指向 `*` 而不是正文首字，
        # 替换会切到时间戳上（实测 test_索引_归一化位置映射回原文位置 抓到）。
        m = SEG_LEAD.match(sg)
        body_off = m.end() if m else 0
        n_buf, i_buf = [], []
        for j in range(body_off, len(sg)):
            c = unicodedata.normalize("NFKC", sg[j]).lower()
            if re.match(r"[\w\u4e00-\u9fff]", c):
                n_buf.append(c)
                i_buf.append(start + j)
        norms.append("".join(n_buf))
        idxmaps.append(i_buf)
    return segs, norms, idxmaps


# ── judgment → 精确替换 ─────────────────────────────────────
# 三条设计（全部来自 2026-10-03 的四组实测，见
# docs/实验/错字纠正方案实测-20261003.md）：
#
# 1. **按段定位，不做全文替换**。旧实现对每条规则做全文
#    `pat.sub(target, out)`，把「这一处该改」压成了「这个词到处都是」，
#    于是同一段里 `OpenClaw` / `OpenCode` 并存时会连坐误伤。
#    四组实测改成逐处定位后，误伤稳定为 0。
#
# 2. **邻域 ±3 段兜底**。模型报段号偶尔偏一位（实测 43 处），
#    在前后各 3 段内再找一次即可救回。
#
# 3. **倒序应用**。按绝对位置从后往前替换，否则前面的改动会移动
#    后面的偏移量——顺序应用比倒序少改 6 处。
def _rewrite_ratio(original: str, target: str) -> float:
    """target 相对 original 的改动比例（0=点改，1=整段重写）。

    用最长公共子序列算保留率：先做大小写不敏感的字面比对，
    再退化到归一化比对（去标点空白），这样 `Al Coding` 与 `AL  CODING`
    都被认作「同一个词的大小写/空格差异」而非重写。
    """
    if not original:
        return 1.0
    a = original.strip()
    b = target.strip()
    if a.lower() == b.lower():
        return 0.0
    sm = difflib.SequenceMatcher(None, a.lower(), b.lower())
    kept = sum(bl.size for bl in sm.get_matching_blocks())
    # 归一化必须走 NFKC：全角 ｃｌｏｃｋｏ 与半角 clocko 折叠成同一个词，
    # 否则「全角→半角」这种纯粹的字形纠正会被误判成整行重写而拒掉
    # （build_index 里的归一化同样走 NFKC，两处必须一致）。
    na = re.sub(r"[^\w\u4e00-\u9fff]", "",
                unicodedata.normalize("NFKC", a.lower()))
    nb = re.sub(r"[^\w\u4e00-\u9fff]", "",
                unicodedata.normalize("NFKC", b.lower()))
    if na and na == nb:
        return 0.0            # 只是标点/空格/全半角差异
    if not na:
        return 1.0
    kept2 = sum(bl.size for bl in
                difflib.SequenceMatcher(None, na, nb).get_matching_blocks())
    return max(0.0, 1.0 - kept2 / len(na))


NEIGHBOR = 3

# 单次替换的 original 长度上限。
# ASR 场景 original 是 5-10 字的音近词；非 ASR 场景模型可能给出**一整行**读不通的
# 文本当作 original（实测有 20+ 字的），那样替换就不是「纠错」而是「整行重写」——
# 模型会借机把读错的行润色成通顺句，替换范围远超一个词。
# 超过这个长度一律拒绝，宁可漏改（不改只是难看）。
# 「点改」还是「重写」——用差异比例判定，不用长度。
# 长度闸门（曾用 24）在纯外文 ASR 上误伤：音近错误发生在**词内**，模型会把
# 整个短语当 original，长度天然 25-46。实测 7 条全是合法纠错被误挡：
#   `Microsoft was the last operating system`(39字) → `Microsoft Windows was…`
#   `the evidence of narrow markets`(30字)          → `the abundance of…`
#   `that are these businesses`(25字)               → `that there are…`
# 真正的「整行重写」特征是**改动比例高**，不是长度高：
#   低质量文本被模型当 original 时，target 与 original 几乎全不一样。
# 重写判据的两个参数，缺一不可：
#
#   重写风险与 original 的**长度正相关**——把 40 个字符整行润色成一整段，
#   风险远高于把 2 个字换个说法。
#   但**短词不能用比例判定**：2-3 个字符的词，公共字符数随机在 0-2 之间跳，
#   比例在 0%~100% 剧烈抖动。实测误伤一整批正确的音近纠错：
#     采购→adoption (100%)、有余→犹豫 (100%)、疫情→其实 (100%)、
#     线上级→现象级 (67%)、clockhold→Claude Code (56%)
#   ——全是把「音近汉字→英文术语」判成了「整行重写」。
#
# 所以：**只有 original 长度 ≥8、且差异比例 >0.6，才判重写。**
# 8 与 0.6 这两个数是照真实数据定的：
#   乱码重写  `消点与异點历史紧粥`(9字) → 77.8%  拦
#   音近错字      `clockhold`(9字)         → 55.6%  放
#   音近错字      `采购`/`有余`/`疫情`(2字)  → 100% 但太短，放
# 更短的词一律放行——短词做不出「整行重写」，漏放风险实测为 0
# （老板拍板那篇实际跑下来 23 处 replace 全部落成，无一处重写）。
MIN_REWRITE_LEN = 8
MAX_REWRITE_RATIO = 0.6

# 英文语法功能词与高频常用词：**永远不是 ASR 错拼的对象**。
# 真实踩坑：84K 的 Hinton 讲座里 `should` 出现 8 次（正常英文），模型却判其中
# 一处 `should → shouldn't`——纠错**加了一个否定词**，把语义反转了。
# 「出现多次就是正常词」这条判据不成立（真错拼 `ghosty` 也出现 12 次），
# 所以只能用停用词黑名单：纯机械、可测、且不伤真错拼。
EN_FUNCTION_WORDS = {
    # 情态 / 助动词
    "should", "could", "would", "might", "must", "can", "will", "shall",
    "may", "do", "does", "did", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "am", "shall",
    # 高频虚词
    "the", "a", "an", "to", "of", "in", "on", "at", "for", "with", "and",
    "or", "but", "if", "then", "than", "that", "this", "these", "those",
    "it", "its", "as", "by", "from", "not", "no", "so", "up", "out", "we",
    "you", "they", "he", "she", "i", "his", "her", "our", "your", "their",
    "what", "which", "who", "when", "where", "how", "why", "all", "some",
    "one", "two", "very", "just", "also", "more", "most", "much", "many",
}


def apply_judgments(text: str, judgments: list[dict]) -> tuple[str, dict]:
    """把模型的逐处判断落成实际替换。返回 (新文本, 统计)。"""
    segs, norms, idxmaps = build_index(text)
    plan: list[tuple[int, int, str, str, int, int]] = []
    rejected: list[dict] = []
    stat = Counter()

    for it in judgments:
        dec = str(it.get("decision") or "").strip().lower()
        stat[f"decision_{dec or 'blank'}"] += 1
        if dec != "replace":
            continue
        if not str(it.get("target") or "").strip():
            # 判了改却给不出正确写法——拒绝并**记账**。
            # 曾经在这里直接 continue，导致这类判断既不落成、也不进 rejected，
            # 报告里查不到，「判定 10 / 落成 6」的差值对不上账
            # （真实踩坑：某篇把 `1` 整段删成 `''`，原文有 7 处 `1`，删错就出事）。
            rejected.append({**it, "why": "null_target"}); stat["null_target"] += 1
            continue
        try:
            si = int(str(it.get("seg", "")).strip().lstrip("Ll"))
        except (TypeError, ValueError):
            rejected.append({**it, "why": "seg_parse_fail"}); stat["seg_parse_fail"] += 1
            continue
        if not (1 <= si <= len(segs)):
            rejected.append({**it, "why": f"seg_out_of_range({si})"}); stat["seg_oob"] += 1
            continue
        orig_raw = str(it.get("original") or "").strip()
        if "\n" in orig_raw or "\r" in orig_raw:
            # 段编号方案下 original 必须落在**单段**内；跨行的 original 会让
            # 替换区间横跨段落，触发 assert_structure_preserved 的行数校验失败
            # （真实踩坑：行数 105 → 103，纠错整条作废回退原文）。
            rejected.append({**it, "why": "original_has_newline"})
            stat["orig_newline"] += 1
            continue
        if orig_raw.lower().strip(" .!?,'\"") in EN_FUNCTION_WORDS:
            rejected.append({**it, "why": "en_function_word"})
            stat["en_function_word"] += 1
            continue
        tgt_raw = str(it.get("target") or "").strip()
        if len(orig_raw) >= MIN_REWRITE_LEN:
            ratio = _rewrite_ratio(orig_raw, tgt_raw)
            if ratio > MAX_REWRITE_RATIO:
                rejected.append({**it, "why": f"looks_like_rewrite({ratio:.0%})"})
                stat["looks_like_rewrite"] += 1
                continue
        on = _norm_seg(orig_raw)
        if not on:
            rejected.append({**it, "why": "original_empty"}); stat["orig_empty"] += 1
            continue
        hit = None
        # 候选段序：**先试报告的那一段**，再按 0, +1, -1, +2, -2… 向外扩。
        # 绝不能从最左的段顺序扫——那样只要左边某段恰好也有同形词，
        # 就会改错段（test_按段定位_只改指定那一处 抓到的就是这个）。
        for off in (0, -1, 1, -2, 2, -3, 3, -4, 4):
            k = si - 1 + off
            if not (0 <= k < len(segs)) or abs(off) > NEIGHBOR:
                continue
            ns = norms[k]
            at = ns.find(on)
            if at < 0 or ns.find(on, at + 1) >= 0:
                # 找不到，或该段内出现多次 → 跳过（宁可不动）
                continue
            hit = (k, at)
            break
        if hit is None:
            rejected.append({**it, "why": f"seg_miss(±{NEIGHBOR})"}); stat["seg_miss"] += 1
            continue
        k, at = hit
        im = idxmaps[k]
        a0 = im[at]
        # a1 必须按**原文长度**算，不能用归一化串反查。
        # 归一化会删掉空格/标点（`Al Coding` 9 字符 → `alcoding` 7），
        # 按归一化坐标反查只覆盖到 `AlCodin`，尾部漏一个字符——
        # 实测某篇 `Al Coding → AI Coding` 就是这样把 `g` 留在外面，
        # 报告里 plan 记为落成、文本却没改对。
        # 两种边界取更靠后的那个：
        #   a0 + len(orig)          —— 原文里 original 连续出现的情况
        #   im[at+len(on)-1] + 1    —— 归一化串末尾对应的原文位置，
        #                              覆盖「原文里夹着零宽字符/异体字、
        #                              归一化后才对得上」的情况（低质量文本常见）
        a1 = max(a0 + len(orig_raw),
                 im[min(at + len(on) - 1, len(im) - 1)] + 1)
        # 替换区间**不得跨行**。
        # `a0 + len(orig_raw)` 假定 original 在原文里逐字连续出现，可匹配是在
        # **归一化串**上做的（空白/标点/星号全被剥掉）。模型报的 original 常比原文长
        # （`**ChatGPT**`、`ChatGPT，`、`ChatGPT。`），a1 于是越过匹配点、吞掉紧随其后
        # 的换行 → 行数 -1 → assert_structure_preserved 整条作废，该篇长文直接不生成。
        # 真实踩坑：NotebookLM(636941a1) 2707 -> 2706。
        # 与本模块「宁可不动」一致：跨行就放弃这一处，不拿结构冒险。
        if "\n" in text[a0:a1] or "\r" in text[a0:a1]:
            rejected.append({**it, "why": "span_crosses_newline"})
            stat["span_nl"] += 1
            continue
        stat["applied"] += 1
        if k != si - 1:
            stat["neighbor_rescued"] += 1
        plan.append((a0, a1, str(it["original"]).strip(), str(it["target"]).strip(),
                     si, k - si + 1))

    plan.sort(key=lambda x: -x[0])          # 倒序
    out = text
    for a0, a1, _o, t, _si, _d in plan:
        out = out[:a0] + t + out[a1:]

    stat["plan"] = len(plan)
    stat["rejected"] = len(rejected)
    n_repl = stat.get("decision_replace", 0)
    stat["success_rate"] = (len(plan) / n_repl) if n_repl else 1.0
    stat["rejected_list"] = rejected
    return out, dict(stat)


def _call(client, sys_p: str, user_p: str, effort: str = "medium",
          timeout: int = 1800) -> str:
    r = client.chat.completions.create(
        model=_model(), reasoning_effort=effort,
        max_completion_tokens=MAX_COMPLETION, temperature=0.1, timeout=timeout,
        messages=[{"role": "system", "content": sys_p},
                  {"role": "user", "content": user_p}])
    return r.choices[0].message.content or ""


def _model() -> str:
    import os
    return os.environ.get("MINIMAX_MODEL", "MiniMax-M3.1-Flash-Preview")


def _base_url() -> str:
    import os
    return os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.cn/v1")


def _parse_json(raw: str) -> dict:
    t = re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"```\s*$", "", t).strip()
    s, e = t.find("{"), t.rfind("}")
    if s < 0 or e < 0:
        raise ValueError(f"无法从返回中解析 JSON: {raw[:200]}")
    return json.loads(t[s:e + 1])


def _find_env_dir() -> pathlib.Path | None:
    """向上找各 skill 的 `scripts/.env`。

    本文件在 `_shared/`，而 `.env` 仍按 skill 分别放在
    `<skills>/<skill>/scripts/.env`（脚本只读自己同目录的 .env）。
    故从 `_shared/` 的同级目录里逐个找 `*/scripts/.env`。
    找不到返回 None —— 由调用方决定是报错还是降级，不静默用错源的凭证。
    """
    skills_dir = SCRIPT_DIR.parent
    if not skills_dir.is_dir():
        return None
    for cand in sorted(skills_dir.glob("*/scripts/.env")):
        return cand.parent
    return None


def load_global(path: Path | None = None) -> list[dict]:
    """读全局种子词表。"""
    p = path or (SCRIPT_DIR / "typos_global.json")
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8")).get("items", [])


def detect_typos(text: str, client, effort: str = "high",
                 max_chars: int = 120000, retries: int = 2,
                 seed: list[dict] | None = None) -> list[dict]:
    """调 LLM 产出单篇的**逐处判断**（段编号 + 原文 + 决定 + 正确写法）。

    与旧版的区别：旧版要模型输出「A → B 映射表」，程序拿去做全文替换；
    新版要模型对**每一处**表态（replace / keep / uncertain）并报段号，
    程序按段精确替换。位置信息不再丢失，这是零误伤的前提
    （2026-10-03 四组实测，见 docs/实验/错字纠正方案实测-20261003.md）。

    `seed` 保留参数只为兼容旧调用；新提示词里声学规律是写死的
    `ACOUSTIC_HINTS`，不再从种子表渲染——那张表已从「执行指令」
    降级为「提示」，且删到只剩高频项。
    """
    segs = split_segments(text[:max_chars])
    numbered = "\n".join(f"L{i}: {s.strip()}" for i, s in enumerate(segs, 1))
    user = DETECT_USER.format(numbered=numbered)
    order = [effort] + [e for e in EFFORT_FALLBACK if e != effort][:retries]
    last = None
    for i, eff in enumerate(order):
        try:
            raw = _call(client, DETECT_SYS, user, eff)
            items = _parse_json(raw).get("judgments", [])
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {str(e)[:120]}"
            print(f"    ↻ 检测失败（{eff}）：{last}", flush=True)
            continue
        if not items:
            # 0 条判断是**正常结果**——这篇本来就没错字。
            # 曾把它当失败，白白降档重试 3 次（短文批量时尤其明显）。
            return []
        return items
    raise RuntimeError(f"专名检测失败，已降档重试 {len(order)} 次：{last}")


def _seed_block(seed: list[dict], source_text: str) -> str:
    """把种子词表渲染成给 LLM 的参考块。

    关键：每条都标注它在**当前这篇**里到底出现没有。
    种子表是历史积累的，`original` 拼法常常对不上具体某篇的实际形态
    （真实踩坑：表里写 Anthropical，源里其实是 Athropical），
    标注出来能让 LLM 自己判断该不该用、怎么改。
    """
    if not seed:
        return ""
    lines = ["", "## 已知错拼参考表（仅供参考，以本转写上下文为准）", ""]
    hit, miss = [], []
    for it in seed:
        o = (it.get("original") or "").strip()
        t = (it.get("target") or "（无 target）")
        c = (it.get("confidence") or "?").lower()
        row = f"- `{o}` → `{t}`（{c}）"
        if not o:
            continue
        (hit if re.search(re.escape(o), source_text, re.IGNORECASE)
         else miss).append(row)
    if hit:
        lines.append("### 在本转写中出现的条目")
        lines += hit
        lines.append("")
    if miss:
        lines.append("### ⚠️ 表里有、但本转写中未出现（请忽略，"
                     "除非你在文中找到它的其他形态）")
        lines += miss
        lines.append("")
    lines.append("表未覆盖的新错拼照常要抽出来。")
    lines.append("")
    return "\n".join(lines)


def _normalize_items(items: list) -> list[dict]:
    """只保留 original 非空、且带 target 或显式 null 的。"""
    out = []
    for it in items:
        o = (it.get("original") or "").strip()
        if not o:
            continue
        out.append({
            "original": o,
            "target": (it.get("target") or None),
            "reason": (it.get("reason") or "").strip(),
            "confidence": (it.get("confidence") or "low").strip().lower(),
        })
    return out


def build_rules(seed: list[dict], detected: list[dict] | None = None,
                min_confidence: str = "high") -> list[dict]:
    """合并种子表与单篇检测表。detected 优先（更贴合当前这篇）。

    过滤规则：
      - target 为空 → 丢弃（无法确定正确名，不该替换）
      - confidence 低于 min_confidence → 保留在报告里，但**不作为替换规则**
      - 同一 original 出现多次，取 confidence 最高的那条
    """
    order = {"low": 0, "medium": 1, "high": 2}
    floor = order.get(min_confidence, 2)
    merged: dict[str, dict] = {}

    for it in list(seed) + list(detected or []):
        o = (it.get("original") or "").strip()
        if not o:
            continue
        t = (it.get("target") or "").strip()
        c = (it.get("confidence") or "low").strip().lower()
        prev = merged.get(o)
        # 同置信度时**后者胜出**（detected 在后，更贴合当前这篇）；
        # 只有先前的置信度严格更高时才保留它。
        if prev and order.get(prev["confidence"], 0) > order.get(c, 0):
            continue
        merged[o] = {
            "original": o, "target": t or None,
            "reason": (it.get("reason") or "").strip(),
            "confidence": c, "occurrences": 0, "applied": False,
        }

    applied = [v for v in merged.values()
               if v["target"] and order.get(v["confidence"], 0) >= floor]
    for v in merged.values():
        v["applied"] = v in applied
    return list(merged.values())


def _pat(original: str) -> re.Pattern:
    """错拼字样 → 大小写不敏感的字面匹配。

    纯字面匹配，不做词边界推断：专名错拼常与标点/中文直接相连，
    加 \\b 反而会漏（中文与英文之间不存在词边界）。
    """
    return re.compile(re.escape(original), re.IGNORECASE)


def apply_typos(text: str, items: list[dict]) -> tuple[str, list[dict]]:
    """按规则替换。返回 (新文本, 命中的规则列表)。

    不改结构：只做字面替换，`**[mm:ss]**` 等标记原样保留。

    **单遍 + 保护区**（2026-10-02 修）：此前是逐条链式替换——每条规则都在
    上���条的输出上再跑一遍。两个后果：

    1. 链式污染：`ghosty → Ghostty` 先跑产出 `Ghostty`，紧接着
       `ghost → Ghostty` 又去吃这个 `Ghostty`（`Ghost` 是 `Ghostty` 的
       真子串），结果变成 `Ghostty` + 残留的 `ty` = `Ghosttyty`。
       实测在 Orca 篇把 11 处正确的 `Ghostty` 全改坏。
    2. 链式放大会把一条规则的产物喂给下一条，错误逐级放大。

    修法两条一起上：
      · 单遍：只读原文、只写输出，替换产物永不被再次扫描
      · 保护区：原文中已经写对的 target 先标出来，落在保护区内的
        匹配一律跳过——**已经正确的词不该被纠错规则再碰**
    """
    rules = [it for it in items if it.get("target") and it.get("applied")]
    # 长字样优先：交替式里先注册的先匹配
    rules.sort(key=lambda x: -len(x["original"]))
    if not rules:
        return text, []

    # ── 保护区：原文中所有 target 的出现区间 ──
    protected: list[tuple[int, int]] = []
    for t in {it["target"] for it in rules if len(it["target"]) >= 3}:
        for m in re.finditer(re.escape(t), text, re.IGNORECASE):
            protected.append((m.start(), m.end()))

    def guarded(s: int, e: int) -> bool:
        return any(s >= ps and e <= pe for ps, pe in protected)

    # ── 单遍：合并成一条交替式，一次遍历 ──
    groups = {f"g{i}": it for i, it in enumerate(rules)}
    combined = re.compile(
        "|".join(f"(?P<g{i}>{re.escape(it['original'])})" for i, it in enumerate(rules)),
        re.IGNORECASE,
    )

    out: list[str] = []
    hits: list[dict] = []
    pos = 0
    for m in combined.finditer(text):
        it = groups[m.lastgroup]
        if guarded(m.start(), m.end()):
            continue
        out.append(text[pos:m.start()])
        out.append(it["target"])
        pos = m.end()
        it["occurrences"] = it.get("occurrences", 0) + 1
        if it not in hits:
            hits.append(it)
    out.append(text[pos:])
    return "".join(out), hits


def correct_text(text: str, seed: list[dict], detected: list[dict] | None = None,
                 min_confidence: str = "high") -> tuple[str, dict]:
    """一步到位：合并规则 → 替换 → 统计。"""
    items = build_rules(seed, detected, min_confidence)
    out, hits = apply_typos(text, items)
    skipped = [i for i in items if i["target"] and not i["applied"]]
    unknown = [i for i in items if not i["target"]]
    return out, {
        "chars_in": len(text), "chars_out": len(out),
        "applied_count": len(hits),
        "occurrences": sum(i["occurrences"] for i in hits),
        "skipped_low_confidence": [
            {"original": i["original"], "target": i["target"],
             "confidence": i["confidence"]} for i in skipped],
        "unknown_target": [i["original"] for i in unknown],
        "items": items,
    }


# ── 时间戳标记的完整性校验 ────────────────────────────────
TS_LINE = re.compile(r"^\*\*\[\d{1,2}:\d{2}\]\*\*", re.M)


def assert_structure_preserved(before: str, after: str) -> None:
    """确认只换了字、没动结构。

    校验：时间戳标记数量一致 + 行数一致。
    """
    b_ts, a_ts = TS_LINE.findall(before), TS_LINE.findall(after)
    if len(b_ts) != len(a_ts):
        raise AssertionError(
            f"时间戳标记数不一致：{len(b_ts)} -> {len(a_ts)}，替换破坏了分块结构")
    if len(before.splitlines()) != len(after.splitlines()):
        raise AssertionError(
            f"行数不一致：{len(before.splitlines())} -> {len(after.splitlines())}")
    if b_ts != a_ts:
        raise AssertionError("时间戳内容或顺序被改动")


def run_file(src: Path, out_path: Path, typos_out: Path,
             seed: list[dict] | None = None, client=None,
             effort: str = "high") -> dict:
    """完整流程：读源 → LLM 逐处判断 → 按段精确替换 → 写 .clean.md + typos JSON。

    `seed` 只为兼容旧调用方保留，新流程不使用（声学规律见 ACOUSTIC_HINTS）。
    无 client 时退化为「只做结构切分、不替换」，用于离线跑批。
    """
    text = src.read_text(encoding="utf-8")
    segs = split_segments(text)

    judgments = detect_typos(text, client, effort=effort, seed=seed) if client else []
    out, stats = apply_judgments(text, judgments)

    assert_structure_preserved(text, out)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(out, encoding="utf-8")

    typos_out.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "source": src.name,
        "clean": out_path.name,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "effort": effort,
        "segments": len(segs),
        "judgments": len(judgments),
        "applied_count": stats.get("plan", 0),
        "success_rate": round(stats.get("success_rate", 1.0), 4),
        "neighbor_rescued": stats.get("neighbor_rescued", 0),
        "rejected": stats.get("rejected_list", []),
        "decision_counts": {k[len("decision_"):]: v for k, v in stats.items()
                            if k.startswith("decision_")},
        "chars_in": len(text),
        "chars_out": len(out),
        "typos": judgments,
    }
    typos_out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    return report


if __name__ == "__main__":
    import argparse
    import os
    import sys

    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="原始 transcript/images 路径")
    ap.add_argument("--out", required=True, help="输出 .clean.md 路径")
    ap.add_argument("--typos-out", required=True, help="输出 typos JSON 路径")
    ap.add_argument("--seed", default=None, help="全局种子词表路径")
    ap.add_argument("--no-llm", action="store_true", help="只用种子表，不调 LLM")
    ap.add_argument("--env-dir", default=None,
                    help="含 .env 的目录（默认在 _shared 同级里找 */scripts/.env）")
    ap.add_argument("--effort", default="medium", help="LLM 检测的 effort")
    ap.add_argument("--min-confidence", default="high",
                    choices=["low", "medium", "high"],
                    help="低于此 confidence 的条目只记录不替换")
    a = ap.parse_args()

    # 本文件现在位于 `_shared/`，`SCRIPT_DIR.parent.parent / "_shared"` 会指回自身、
    # 上一行则会指到 `skills/` —— 两条都失去了原意。改为：`_shared/env.py` 就在
    # 同目录，直接 import；`.env` 仍在各 skill 的 `scripts/` 下，用 --env-dir 传入
    # 或按已知 skill 名向上找。
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        from env import load_env
        env_dir = pathlib.Path(a.env_dir) if a.env_dir else _find_env_dir()
        if env_dir is not None:
            load_env(env_dir)
        else:
            print("ℹ️ 未找到 .env（用 --env-dir 指定）；若缺 MINIMAX_API_KEY 将直接报错退出")
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ load_env 失败: {e}", file=sys.stderr)

    client = None
    if not a.no_llm:
        if not os.environ.get("MINIMAX_API_KEY"):
            print("❌ 未配置 MINIMAX_API_KEY，加 --no-llm 可只用种子表",
                  file=sys.stderr)
            raise SystemExit(1)
        from openai import OpenAI
        client = OpenAI(api_key=os.environ["MINIMAX_API_KEY"],
                        base_url=_base_url(), timeout=1800)

    t0 = time.time()
    rep = run_file(Path(a.source), Path(a.out), Path(a.typos_out),
                   seed=load_global(Path(a.seed) if a.seed else None),
                   client=client, effort=a.effort,
                   min_confidence=a.min_confidence)
    print(f"[correct-typos] {rep['applied_count']} 条规则生效 / "
          f"{rep['occurrences']} 处替换  {time.time() - t0:.1f}s")
    print(f"  种子 {rep['seed_count']} 条，LLM 检测 {rep['llm_detected_count']} 条")
    if rep["skipped_low_confidence"]:
        print(f"  低置信只记录不替换: {len(rep['skipped_low_confidence'])} 条")
        for s in rep["skipped_low_confidence"]:
            print(f"    - {s['original']} → {s['target']} ({s['confidence']})")
    if rep["unknown_target"]:
        print(f"  无法确定正确名: {rep['unknown_target']}")
    print(f"  -> {a.out}")
    print(f"  -> {a.typos_out}")
