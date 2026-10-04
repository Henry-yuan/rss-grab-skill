#!/usr/bin/env python3
"""笔记 frontmatter 的**统一契约**（V1.0 C6 / AC-C2，2026-10-05 抽出）。

## 为什么要有这个模块

C6 把磁盘上 320 篇产物的 frontmatter 统一到 17 字段后，
**某一源的 builder 没跟上**——它还在吐 12 字段 + `asr_status`，
再跑一次 v3 就会写回旧格式，把磁盘上已有的 256 篇搞成两种格式并存。

根因不是"忘了改"，是**契约没有单一定义源**：每个源各写各的 `build()`，
字段集靠约定维持，约定一破就静默漂移。实测同一个项目里同时存在
12 字段版和 17 字段版两个 builder。

所以这里只放**契约**（字段表 + 序列化 + 自检），
各源的 `build()` 负责从自己的 info.json 取数，但**必须**产出 `FIELDS`
里的全部 key，顺序也按它来。`scripts/tests/test_frontmatter_contract.py`
会对三个源各跑一次 `build()` 并断言字段集完全一致。

## 字段表（顺序即落盘顺序）

`FIELDS` 是唯一权威。改这里等于改全部三个源的产物格式，
必须同步改测试与 `docs/versions/V1.0/spec.md` 的 AC-C2。
"""
from __future__ import annotations

import re

# ── AC-C2 的 17 字段，顺序即落盘顺序 ──────────────────────
FIELDS = (
    "note_id",
    "title",
    "uploader",
    "uploader_id",
    "speaker",
    "type",
    "duration",
    "published_at",
    "source_url",
    "subtitle_source",
    "transcript_status",
    "source_tags",
    "tags",
    "summary",
    "generated_at",
    "description",
    "show_name",
)

# 旧字段名 → 新字段名。迁移脚本与回读兼容用；
# **新代码一律写新名**，不要在 build() 里产出旧名。
LEGACY_FIELD_ALIASES = {
    "asr_status": "transcript_status",     # 拆分见 C6：status + subtitle_source
    "publish_time": "published_at",
    "source_type": None,                    # 已被 subtitle_source 取代，删除
    "asr_model": None,                      # 同上
}


def check(fm: dict) -> list[str]:
    """字段集自检。返回问题列表，空列表 = 合规。"""
    keys = set(fm)
    problems = []
    missing = [f for f in FIELDS if f not in keys]
    extra = sorted(keys - set(FIELDS))
    if missing:
        problems.append(f"缺字段 {missing}")
    if extra:
        problems.append(f"多字段 {extra}")
    legacy = sorted(k for k in extra if k in LEGACY_FIELD_ALIASES)
    if legacy:
        problems.append(
            f"含已废弃字段 {legacy}（应改用 "
            f"{[LEGACY_FIELD_ALIASES[k] for k in legacy]}）")
    return problems


def normalize(fm: dict) -> dict:
    """按 FIELDS 顺序重排；缺键补空串，多余键丢弃。

    补空串是刻意的：字段集稳定比"少写几个空字段"重要，
    否则跨源比对时会因为"这源没这个字段"而误判成不等价。
    """
    return {k: fm.get(k, "" if k not in ("tags", "source_tags") else [])
            for k in FIELDS}


# ── 序列化（从 rss-grab/build_frontmatter.py 搬来，三源共用）──
DUMP_SAFE = str.maketrans({":": "：", "#": "＃", '"': "＂", "'": "＇",
                           "[": "［", "]": "］", "{": "｛", "}": "｝"})


def _scalar(v) -> str:
    """标量 → YAML 字面量。

    ⚠️ **换行必须转义成 \\n**（2026-10-04 修）：值里若含真换行，写出来的
    YAML 会跨行 —— 一旦值里还含 `---`（实际踩过：某期条目的 description 里就有），
    读回时会把它误认成 frontmatter 的闭合标记，**正文被整段吞掉**。
    转义成单行后这个失败模式彻底消失。
    """
    s = str(v)
    if s == "":
        return '""'
    if "\n" in s or "\r" in s or s.translate(DUMP_SAFE) != s \
            or re.search(r"^\s|\s$|:\s", s):
        esc = (s.replace("\\", "\\\\").replace('"', '\\"')
                .replace("\r\n", "\\n").replace("\n", "\\n")
                .replace("\r", "\\n"))
        return '"' + esc + '"'
    return s


def dump(fm: dict) -> str:
    """frontmatter dict → YAML 文本（含首尾 `---`）。**按 FIELDS 顺序输出**。"""
    out = ["---"]
    for k, v in normalize(fm).items():
        if isinstance(v, list):
            if not v:
                out.append(f"{k}: []")
            else:
                out.append(f"{k}:")
                out += [f"  - {_scalar(x)}" for x in v]
        elif isinstance(v, bool):
            out.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            out.append(f"{k}: {v}")
        else:
            out.append(f"{k}: {_scalar(v)}")
    out.append("---")
    return "\n".join(out)


# 模块名刻意不叫 build_frontmatter：各源 skill 下已有同名模块，
# 同名会让 sys.path 的插入顺序决定 import 到哪一份（实测踩过）。
