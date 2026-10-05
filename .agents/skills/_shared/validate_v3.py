#!/usr/bin/env python3
"""v3 Step ④：落盘前校验（C1~C13）。

原则（方案决策 #11）：
- 机械问题用脚本修，不触发重试
- 观测类指标只记录，不阻断
- 只有「内容缺失/结构错乱」才判失败交由编排层重试
"""
from __future__ import annotations

import difflib
import re

# ── 常量 ──────────────────────────────────────────────
H2 = re.compile(r"^##\s+(.+?)\s*$", re.M)
H3 = re.compile(r"^###\s+(.+?)\s*$", re.M)
TIME_STAMP = re.compile(r"\[\d{1,2}:\d{2}(?::\d{2})?(?:\s*[-–~]\s*\d{1,2}:\d{2}(?::\d{2})?)?\]")
TRANSLATE_MARK = re.compile(r"（译）|\(译\)|\(翻译\)|（翻译）")
PUNCT = re.compile(r"[\s，。！？、；：\"'（）《》…—\-·,.!?;:()\[\]>#>*`|~]")
PRONOUN_START = re.compile(r"^\s*(它|他|她|这(?:个|些|种|类|里|边)?|那(?:个|些|种|类|里|边)?|该|此|上述|前述|前面(?:提到|说过)|以上)")
CODE_FENCE = re.compile(r"```.*?```", re.S)
CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def normalize(s: str) -> str:
    """回源比对用的归一：剥时间戳标记、去标点空白、全半角统一。"""
    s = re.sub(r"\*\*\[\d{1,2}:\d{2}\]\*\*", "", s)
    s = TIME_STAMP.sub("", s)
    s = s.translate(str.maketrans("：；，。（）％－", ":;,.%()-")).lower()
    return PUNCT.sub("", s)


# ── C3 章节归一 ──────────────────────────────────────
def normalize_quotes(body: str) -> tuple[str, int]:
    """把 `## 关键引用` 章里的引用统一成 `- 引用` 列表项。

    历史上有三种写法并存：`- 引用`（存量主流 288 篇）、`- > 引用`（v3 提示词
    规定的，实测 43/44 篇）、裸 `> 引用`。格式是机械问题，归代码收口——
    提示词只负责告知，不承担保证责任（实测提示词也只做到 43/44）。

    只处理 `## 关键引用` 章内部，不动其他章节的引用块。

    Returns: (归一后正文, 改动行数)
    """
    m = re.search(r"^##\s*关键引用\s*$", body, re.M)
    if not m:
        return body, 0
    head, sec = body[:m.end()], body[m.end():]
    nxt = re.search(r"^##\s+", sec, re.M)
    if nxt:
        sec, tail = sec[:nxt.start()], sec[nxt.start():]
    else:
        tail = ""
    fixed = 0
    out = []
    for ln in sec.split("\n"):
        # `- > xxx` / `* > xxx` → `- xxx`
        m2 = re.match(r"^(\s*)[-*]\s*>\s*(.+?)\s*$", ln)
        # 裸 `> xxx` → `- xxx`
        if not m2:
            m2 = re.match(r"^(\s*)>\s*(.+?)\s*$", ln)
        if m2:
            out.append(f"{m2.group(1)}- {m2.group(2)}")
            fixed += 1
        else:
            out.append(ln)
    return head + "\n".join(out) + tail, fixed


def normalize_sections(body: str):
    """把章节名收敛到三个硬章节：TL;DR + 要点总结 + 关键引用。多余 H2 降为 H3。

    Returns: (归一后正文, 是否含 TL;DR, 是否含 关键引用, 是否含 要点总结)
    """
    lines = body.split("\n")
    out, seen = [], {"tldr": False, "quote": False, "body": False}
    for ln in lines:
        m = H2.match(ln)
        if not m:
            out.append(ln)
            continue
        name = m.group(1).strip()
        key = re.sub(r"[\s（）()·]", "", name)
        if key.upper().startswith("TL;DR") or key.upper() == "TLD R":
            out.append("## TL;DR")
            seen["tldr"] = True
        elif key.startswith("关键引用"):
            out.append("## 关键引用")          # 去掉「（可直接复用）」后缀
            seen["quote"] = True
        elif key.startswith("要点总结"):
            out.append("## 要点总结")
            seen["body"] = True
        else:
            out.append("### " + name)            # 多余 H2 降级
    return "\n".join(out), seen["tldr"], seen["quote"], seen["body"]


# ── C6 编号重排 ──────────────────────────────────────
CN_ORDER = "一二三四五六七八九十"

# H3 标题前缀的编号：汉字序号 / 阿拉伯序号（模型不听话时兜底抹掉）
H3_NUMBER = re.compile(r"^(#{3,4}\s*)(?:[一二三四五六七八九十]+|\d{1,2})[.、]\s*(\S.*)$")


def strip_h3_numbers(body: str) -> tuple[str, int]:
    """去掉 H3/H4 标题开头的数字或汉字序号（提示词已要求不加，此处兜底）。"""
    lines, fixed = body.split("\n"), 0
    for i, ln in enumerate(lines):
        m = H3_NUMBER.match(ln)
        if m:
            new = f"{m.group(1)}{m.group(2)}"
            if new != ln:
                lines[i] = new
                fixed += 1
    return "\n".join(lines), fixed


# 打断编号序列的行：标题 / 引用 / 表格 / 散文（不缩进的正文）
_BREAK = re.compile(r"^(#{1,6}\s|>|\||-{3,}|={3,})")
_BLANK = re.compile(r"^\s*$")
_NUM_ITEM = re.compile(r"^(\s*)(\d{1,2})[.、]\s+(\S.*)$")


def renumber(body: str) -> tuple[str, int]:
    """重排**有序列表**编号，使每个块内连续为 1..N。

    H3/H4 标题不参与编号（提示词已要求不加序号，由 strip_h3_numbers 兜底清理）。
    块划分：遇标题 / 散文 / 表格 / 引用断块；空行与缩进续行不断块。
    跳过代码围栏。
    """
    lines, in_code = body.split("\n"), False
    seq, fixed = 0, 0

    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("```"):
            in_code = not in_code
            if in_code:
                seq = 0
            continue
        if in_code:
            continue

        mn = _NUM_ITEM.match(ln)
        if mn:
            seq += 1
            new = f"{mn.group(1)}{seq}. {mn.group(3)}"
            if new != ln:
                lines[i] = new
                fixed += 1
            continue

        if _BLANK.match(ln):
            continue                       # 空行不断块
        if _BREAK.match(ln):
            seq = 0                        # 标题 / 表格 / 引用断块
            continue
        if ln[:1] in (" ", "\t", "*", "-", "+"):
            continue                       # 缩进或符号 = 续行 / 子项
        seq = 0                            # 无缩进散文断块

    return "\n".join(lines), fixed


# ── C4 引用回源（观测） ───────────────────────────────
# 2026-10-02 放宽：不再要求「逐字回源」。
#   - 相似阈值 0.85 → 0.60（允许轻微润色与断句调整）
#   - 指标从「回源率」降级为「可溯率」= (逐字 + 高相似) / 总数
#   - 理由：长文路径的 ④ 从 ③ 主题层摘引用，而 ③ 已改写源文，
#     拿源文当唯一对照会让长文篇恒为 0 —— 那是口径错配，不是引用不实。
#   - exact / ratio 字段保留，仅为兼容既有报表。
SIM_THRESHOLD = 0.60
CN_RATIO = re.compile(r"[\u4e00-\u9fff]")


def is_foreign_source(source: str, threshold: float = 0.05) -> bool:
    """判定源是否以外文为主（中文占比极低）。"""
    body = TIME_STAMP.sub("", source)
    sample = body[:20000]
    if not sample:
        return False
    return len(CN_RATIO.findall(sample)) / len(sample) < threshold


def quote_coverage(body: str, source: str) -> dict:
    """返回 {total, exact, similar, unverified, traceable, ratio, translated}。

    仅观测，不阻断（v3 决策 #13）。「引用能否在源文里找到对应片段」是**线索**，
    不是质量判据：允许轻微润色，故以 SIM_THRESHOLD（0.60）作高相似档。

    **长文路径的口径限制**（2026-10-02 记录）：分块路径的 ④ 收到的是 ③ 主题
    清单，引用从主题 detail 摘取；而 ③ 本身已对源文做过改写。因此长文篇的
    traceable 天然偏低，不能与短文篇同口径横比，更不能当「引用不实」的证据。
    要真正校验长文引用需把 ③ 主题文本作为第二对照传入，本函数暂未接。

    引用有两种形态：`> 原句`（块引用）与 `- 原句`（列表）。
    只取「关键引用」章节内的条目，避免把正文列表误当引用。

    外文源：引用为译文，逐字回源必然不成立——记 translated=True，不参与比率。
    """
    src_n = normalize(source)
    foreign = is_foreign_source(source)
    quotes: list[str] = []
    m = re.search(r"^##\s*关键引用\s*$(.*?)(?=^##\s|\Z)", body, re.S | re.M)
    scope = m.group(1) if m else body
    for ln in scope.split("\n"):
        s = ln.strip()
        if s.startswith(">"):
            quotes.append(s.lstrip(">").strip())
        elif re.match(r"^[-*]\s+\S", s):
            quotes.append(re.sub(r"^[-*]\s+", "", s))
    quotes = [q for q in quotes if len(normalize(q)) >= 6]
    total = len(quotes)
    if not total:
        return {"total": 0, "exact": 0, "similar": 0, "similar_085": 0,
                "unverified": 0, "traceable": 1.0, "ratio": 1.0,
                "translated": foreign}
    if foreign:
        # 外文源引用为译文，不做逐字回源
        return {"total": total, "exact": 0, "similar": 0, "similar_085": 0,
                "unverified": 0, "traceable": None, "ratio": None,
                "translated": True, "note": "外文源，引用为译文，跳过校验"}
    exact = sim = unver = 0
    for q in quotes:
        qn = normalize(q)
        if qn in src_n:
            exact += 1
            continue
        r = difflib.SequenceMatcher(None, qn, src_n).find_longest_match(
            0, len(qn), 0, len(src_n)).size / max(1, len(qn))
        if r >= SIM_THRESHOLD:
            sim += 1
        else:
            unver += 1
    return {"total": total, "exact": exact, "similar": sim, "similar_085": sim,
            "unverified": unver, "traceable": (exact + sim) / total,
            "ratio": exact / total, "translated": False}


# ── C11 段落自足（观测） ──────────────────────────────
def pronoun_ratio(body: str) -> float:
    """段首指代词占比。按 H2/H3 切段后逐段判，标题行本身不计。"""
    # 先按标题切成块，每块去掉首行标题
    blocks = re.split(r"^#{1,3}\s+.*$", body, flags=re.M)
    paras = []
    for blk in blocks:
        for p in re.split(r"\n\s*\n", blk):
            p = p.strip()
            if not p:
                continue
            if p.startswith((">", "|", "-", "*", "---", "`")):
                continue
            paras.append(p)
    if not paras:
        return 0.0
    bad = sum(1 for p in paras if PRONOUN_START.match(p))
    return bad / len(paras)


# ── 4.1 未翻译英文残留 ────────────────────────────────
EN_RUN = re.compile(r"\b[A-Za-z][A-Za-z'\-]*(?:\s+[A-Za-z][A-Za-z'\-]*){5,}")
TRANS_MARK = re.compile(r"（译）|\(译\)|（翻译）|\(翻译\)")


# ── 正文块类型统计（纯观测）───────────────────────────
def block_kinds(body: str) -> set:
    """统计正文（排除标题行）用到的块类型，仅用于观测。"""
    m = re.search(r"^##\s*关键引用\s*$(.*?)(?=^##\s|\Z)", body, re.S | re.M)
    scope = body[: m.start()] if m else body
    kinds = set()
    for ln in scope.split("\n"):
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("|"):
            kinds.add("表格")
        elif re.match(r"^\d+[.、]", s):
            kinds.add("有序列表")
        elif re.match(r"^[-*]\s", s):
            kinds.add("无序列表")
        elif s.startswith(">"):
            kinds.add("引用块")
        else:
            kinds.add("段落")
    return kinds


def find_untranslated(body: str, exclude_quote: bool = True) -> list[str]:
    """检测未翻译的连续英文（≥6 词）。

    扫描范围排除 `## 关键引用` 段（3.1 正在往那里加英文原句）与代码块。
    仅记录，不阻断。
    """
    scope = body
    if exclude_quote:
        m = re.search(r"^##\s*关键引用\s*$(.*?)(?=^##\s|\Z)", body, re.S | re.M)
        if m:
            scope = body[: m.start()] + body[m.end():]
    scope = CODE_FENCE.sub("", scope)
    hits = [m.group(0).strip() for m in EN_RUN.finditer(scope)]
    hits += [m.group(0) for m in TRANS_MARK.finditer(scope)]
    return hits


# ── 4.2 TL;DR 话题清单相邻去重 ────────────────────────
def dedup_tldr_topics(body: str) -> tuple[str, int]:
    """删除 TL;DR 里与前一条归一化后完全相同的编号条目。

    须在 renumber 之前调用（否则删条目会留下跳号）。
    """
    m = re.search(r"^##\s*TL;DR\s*$(.*?)(?=^##\s|\Z)", body, re.S | re.M)
    if not m:
        return body, 0
    lines = m.group(1).split("\n")
    out, prev, removed = [], None, 0
    for ln in lines:
        mm = _NUM_ITEM.match(ln)
        if mm:
            key = PUNCT.sub("", mm.group(3).lower())
            if key and key == prev:
                removed += 1
                continue
            prev = key
        out.append(ln)
    if removed == 0:
        return body, 0
    new_block = "\n".join(out)
    # m.start(1)/m.end(1) 是正则分组边界，与 out 的行数一一对应，可直接拼接
    return body[: m.start(1)] + new_block + body[m.end(1):], removed



# ── 主入口 ───────────────────────────────────────────
def validate(body: str, source: str, info: dict, llm: dict) -> dict:
    """返回 {ok, body, report, retries_needed}。

    ok=False 表示内容级失败，编排层应带错误信息重试。
    `retries_needed` 是**失败原因的完整清单**（多条 = 同时踩了多条），
    编排层应把它回灌给模型；ok=True 时恒为空列表。
    """
    rep, ok = {}, True
    # 所有「内容级失败」的原因统一收口到这里。
    # 此前每处 ok=False 各自把原因写进 report 的某个子键，而 retries_needed
    # 只读 C3_sections.error —— 于是 summary 为空这类失败取到的是 'unknown'，
    # 编排层拿不到真原因，只能原参数盲重试。fail() 是**唯一**的置假入口：
    # 新增阻断检查时必须走 fail()，否则原因会再次从 retries_needed 丢失。
    reasons: list[str] = []

    def fail(reason: str) -> None:
        nonlocal ok
        ok = False
        reasons.append(reason)

    body2, has_tldr, has_quote, has_body = normalize_sections(body)
    rep["C3_sections"] = {"tldr": has_tldr, "quote": has_quote, "body": has_body}

    # C1/C2 typos 真实性
    src_n = normalize(source)
    kept, dropped = [], []
    for t in (llm.get("typos") or []):
        orig, tgt = str(t.get("original", "")), str(t.get("target", ""))
        if not orig or orig not in source:
            dropped.append({**t, "status": "dropped_not_in_source"}); continue
        if not tgt or PUNCT.sub("", tgt) == "":
            dropped.append({**t, "status": "dropped_empty_target"}); continue
        kept.append({**t, "occurrences": source.count(orig),
                     "in_note": orig in body2, "status": "kept"})
    rep["C1_C2_typos"] = {"kept": len(kept), "dropped": len(dropped), "items": kept + dropped}

    # C3 硬章节
    if not (has_tldr and has_quote):
        rep["C3_sections"]["error"] = "缺少 TL;DR 或 关键引用"
        fail(rep["C3_sections"]["error"])
    # C4 判定重试：章节顺序
    #
    # ⚠️ 2026-10-05 重写。此前是
    #   `re.search(r"##\s*TL;DR[\s\S]*?(##\s*关键引用|$)", body2)`
    # **永远绿**：正则里的 `$` 没有 re.MULTILINE，匹配的是字符串末尾；只要
    # `## TL;DR` 存在，`[\s\S]*?` 一定能一路走到串尾 → 必然匹配成功。
    # 于是这条「检查顺序」的校验**只在 TL;DR 整个缺失时才报**（而那种情况已被
    # 上一条抓到），真正的顺序颠倒（`关键引用` 跑到 `要点总结` 前面）一条都抓不到。
    # 这与 2026-10-04 守门测试「扫描根错一层、上线起就在漏扫」是同一类毛病：
    # **看起来在检查，实际不检查**。
    #
    # 现在改成：取实际 H2 序列，与期望顺序逐位比对。
    order_seen = [h.strip() for h in re.findall(r"^##\s+(.+?)\s*$", body2, re.M)]
    order_want = ["TL;DR", "要点总结", "关键引用"]
    pos = [order_seen.index(s) for s in order_want if s in order_seen]
    if pos != sorted(pos):
        got = " → ".join(s for s in order_seen if s in order_want)
        rep["C3_sections"]["error"] = f"章节顺序异常：{got}"
        fail(rep["C3_sections"]["error"])
    rep["C3_sections"]["order"] = " → ".join(s for s in order_seen if s in order_want)

    body_d, dupfix = dedup_tldr_topics(body2)
    rep["C4_tldr_dedup"] = {"removed": dupfix}
    body2 = body_d

    body3, h3fix = strip_h3_numbers(body2)
    rep["C6_h3_strip"] = {"fixed": h3fix}
    body4, nfix = renumber(body3)
    rep["C6_renumber"] = {"fixed": nfix}
    body = body4

    # C10 剥离时间戳与「（译）」
    ts_removed = len(TIME_STAMP.findall(body))
    tr_removed = len(TRANSLATE_MARK.findall(body))
    body = TIME_STAMP.sub("", body)
    body = TRANSLATE_MARK.sub("", body)
    body = re.sub(r"[ \t]{2,}", " ", body)
    rep["C10_strip"] = {"timestamps": ts_removed, "translate_marks": tr_removed}

    # C4 引用回源（观测）
    rep["C4_quote_coverage"] = quote_coverage(body, source)

    # C8 tags
    tags = llm.get("tags") or []
    clean = [str(x).strip() for x in tags
             if str(x).strip() and not re.fullmatch(r"[\d:]+", str(x).strip())]
    src_tags = set(info.get("tags") or [])
    overlap = sum(1 for t in clean if t.lstrip("#") in src_tags) / max(1, len(clean))
    rep["C8_tags"] = {"n": len(clean), "overlap_with_source": round(overlap, 2)}
    if not 3 <= len(clean) <= 8:
        rep["C8_tags"]["warn"] = "建议 3-8 个"

    # C11 summary
    s = str(llm.get("summary") or "").strip()
    truncated = False
    if len(s) > 60:
        s = s[:60]; truncated = True
    tldr_head = ""
    m = re.search(r"##\s*TL;DR\s*\n(.*?)(?=\n##|\Z)", body, re.S)
    if m:
        tldr_head = PUNCT.sub("", normalize(m.group(1)))[:60]
    dup = bool(s) and tldr_head and PUNCT.sub("", normalize(s))[:60] == tldr_head
    rep["C11_summary"] = {"len": len(s), "truncated": truncated, "dup_with_tldr": dup}
    if not s:
        rep["C11_summary"]["error"] = "summary 为空"
        fail(rep["C11_summary"]["error"])

    # C13 压缩比（观测）
    ratio = len(body) / max(1, len(source))
    rep["C13_compression"] = {"body_chars": len(body), "source_chars": len(source),
                              "ratio": round(ratio, 2),
                              "low_coverage": len(source) > 20000 and ratio < 0.2}

    # C11 段落自足（观测）
    rep["C11_pronoun"] = {"ratio": round(pronoun_ratio(body), 3)}

    # 4.1 未翻译英文（观测，不阻断）
    untr = find_untranslated(body)
    rep["C14_untranslated"] = {"count": len(untr), "samples": untr[:3]}

    # C16 关键引用格式统一（只改格式，不改内容）
    body, qfix = normalize_quotes(body)
    rep["C16_quote_format"] = {"fixed": qfix}

    # 形态种类（纯观测，不设目标）
    rep["C15_block_kinds"] = {"kinds": sorted(block_kinds(body))}

    rep["h3_count"] = len(H3.findall(body))
    if not ok and not reasons:
        # 兜底：ok=False 却一条原因都没有，只可能是有人绕过 fail() 直接置假。
        # 宁可回 'unknown' 也不能给编排层一个空列表（空列表会被当成「无需重试」）。
        reasons.append("unknown")
    return {"ok": ok, "body": body, "report": rep,
            "retries_needed": reasons}
