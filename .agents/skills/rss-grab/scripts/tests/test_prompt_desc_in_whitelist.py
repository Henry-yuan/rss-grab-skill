#!/usr/bin/env python3
"""护栏：简介字段必须留在喂给 LLM 的白名单里。

## 为什么需要这条断言

2026-10-08 之前，提示词模板里有**两个**简介区块：
`## 笔记元信息` 里的 description（由白名单控制）+ 独立的 `## 笔记描述`（`{desc}`）。
同日删掉了重复的那个。此后 **简介只剩白名单这一条投递路径**。

危险在于这条路径**静默失败**：共享层 `info_for_prompt()` 逐个 `info.get(k)`，
取不到就 `continue`。白名单少了简介 → 简介彻底消失，但不报错、不留痕、
测试也不报警，笔记只会悄悄变得信息更少。

本仓库侧的调用方在 `generate_note_v3.py` 里把本模块的白名单推给共享层
（`shared.PROMPT_INFO_FIELDS = fm.PROMPT_INFO_FIELDS`），本测试走同一条路。
"""
from __future__ import annotations

import pathlib
import sys

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent.parent / "_shared"))

import build_frontmatter as fm  # noqa: E402

DESC_SENTINEL = "这是一段用于判定的简介样例内容，必须出现在模型输入里"


def _fed_to_model(info: dict) -> str:
    """复刻调用方的接线方式，返回真正喂给模型的那段元信息文本。"""
    import build_v3 as shared
    shared.PROMPT_INFO_FIELDS = fm.PROMPT_INFO_FIELDS
    return shared.info_for_prompt(info)


def test_白名单含简介():
    assert "description" in fm.PROMPT_INFO_FIELDS, (
        "白名单缺少 description：重复的「笔记描述」区块已删，"
        "简介只剩 info_for_prompt() 一条路，删掉后模型就再也看不到简介且不报错")


def test_简介真的进了模型输入():
    """不只看常量 —— 经共享层取值路径实测一遍，确保真值没被吃掉。"""
    out = _fed_to_model({"title": "单集标题", "show_name": "节目名",
                         "speaker": "讲述人", "type": "podcast",
                         "duration": 1800, "description": DESC_SENTINEL})
    assert DESC_SENTINEL in out, "简介没有进入喂给模型的文本（白名单形同虚设）"