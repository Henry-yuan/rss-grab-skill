#!/usr/bin/env python3
"""护栏：提示词模板里不得再出现重复的「## 笔记描述」区块（2026-10-08 删）。

## 背景

`description`（简介）曾经被**喂两次**，内容一字不差：
- 一次在 `## 笔记元信息` 里（由各源 `PROMPT_INFO_FIELDS` 白名单控制）
- 一次在独立的 `## 笔记描述` 里（模板的 `{desc}` 占位符）

实测某源 348/356 篇的 description 都重复出现。Henry 拍板删掉独立区块，
保留元信息里的那份。

## 为什么需要断言

删掉重复区块后，简介的**唯一投递路径就是白名单**。若有人后来「顺手补回」
这个区块，简介会立刻重新变成两遍（纯浪费 token，且模型可能被同一段文字
带偏重复表述），而**没有任何现有测试会报警**。这条断言专门拦这个。
"""
from __future__ import annotations

import pathlib
import sys

SHARED_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))

import build_v3 as shared  # noqa: E402


def test_短文模板无重复描述区块():
    assert "## 笔记描述" not in shared.USER_TPL, \
        "USER_TPL 里不该再有独立的「笔记描述」区块（简介已在元信息区）"
    assert "{desc}" not in shared.USER_TPL, \
        "USER_TPL 里不该再有 {desc} 占位符（简介已在元信息区）"


def test_长文模板无重复描述区块():
    assert "## 笔记描述" not in shared.USER_TPL_TOPICS, \
        "USER_TPL_TOPICS 里不该再有独立的「笔记描述」区块（简介已在元信息区）"
    assert "{desc}" not in shared.USER_TPL_TOPICS, \
        "USER_TPL_TOPICS 里不该再有 {desc} 占位符（简介已在元信息区）"


def test_两模板都仍带元信息区():
    """反向护栏：确认删的是描述区、不是把元信息区一起误删。"""
    for name, tpl in (("USER_TPL", shared.USER_TPL),
                      ("USER_TPL_TOPICS", shared.USER_TPL_TOPICS)):
        assert "## 笔记元信息" in tpl, f"{name} 的「笔记元信息」区被误删了"
        assert "{info}" in tpl, f"{name} 的 {{info}} 占位符被误删了"


def test_格式后不含未替换占位符():
    """模板能被 .format() 正常渲染，不残留 {desc} 之类的占位。

    这是「删干净了」的端到端判据：若 {desc} 还留在模板里而不再传参，
    渲染结果里就会留着字面量 `{desc}` 混进提示词。
    """
    info = {"title": "标题", "description": "简介内容"}
    for name, tpl in (("USER_TPL", shared.USER_TPL),
                      ("USER_TPL_TOPICS", shared.USER_TPL_TOPICS)):
        rendered = tpl.format(info="元信息行", src="正文", topics="主题", n=3)
        assert "{desc}" not in rendered, f"{name} 渲染结果里残留 {{desc}} 占位符"
        assert "简介内容" in rendered or "元信息行" in rendered, \
            f"{name} 渲染结果异常"