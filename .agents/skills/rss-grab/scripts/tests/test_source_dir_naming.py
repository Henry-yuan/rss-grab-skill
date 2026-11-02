#!/usr/bin/env python3
"""源目录命名守门：笔记源目录名必须 == 订阅状态文件名（去 .md）。

为什么要有这个（2026-11-02 登记）
--------------------------------
订阅状态文件名（`rss/订阅/<节目名>.md`）走 `subscribe_manager._sanitize_filename()`；
笔记源目录名（`rss/notes/<节目名>/`）原先走 `feed.title` **原值**。
两套命名规则从未对齐过 —— 只是此前没有节目名同时触发两种 sanitize 才没暴露：

    订阅：Some-Show.md      （连字符，sanitize 结果）
    笔记：Some Show/        （带空格，feed.title 原值）

后果：功能不受影响（索引按 glob 找得到），但「按节目名找目录」的人工操作别扭，
且两处名字对不上会让人怀疑是不是两个不同的源。

已统一到 sanitize（`generate_note_v3.source_dir_for()`），本测试从**代码侧**与
**数据侧**双向锁死这个约定 —— 任何一侧退回旧规则都会变红：

- 方向一（数据 → 订阅）：`rss/notes/` 下每个源目录名都能在订阅状态文件名里找到
- 方向二（代码 → 订阅）：每份 `info.json` 派生出的源目录名都能在订阅状态文件名里找到
- 方向三（订阅 → 数据）：有原料的订阅必须有对应的源目录

⚠️ **本文件刻意不写任何真实节目名**（用合成标题代替）—— 它要进公开仓库，
真实订阅清单等于公开「这个人订阅了哪些节目」。数据侧的断言全部动态读取，
不依赖任何具体名字。

环境缺失（换机器 / 没有 rss 数据目录）→ skip 而非 fail：本测试是守门，不是准入。
"""
from __future__ import annotations

import json
import pathlib
import sys

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent.parent / "_shared"))

import generate_note_v3 as v3  # noqa: E402

# <repo>/.agents/skills/rss-grab/scripts/tests/this.py → parents[5] = repo 根
REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]
RSS_DIR = REPO_ROOT / "rss"
SUB_DIR = RSS_DIR / "订阅"
NOTES_DIR = RSS_DIR / "notes"
RAW_DIR = RSS_DIR / "raw"

# 合成节目名：**不得换成真实订阅名**（本文件进公开仓库）
SAMPLE_TITLE = "Some Show"
SAMPLE_SAFE = "Some-Show"


def _require_data():
    """数据不在就 skip（守门不是准入）。"""
    if not SUB_DIR.is_dir() or not NOTES_DIR.is_dir():
        import pytest
        pytest.skip(f"rss 数据目录不在 {REPO_ROOT}（纯主项目环境），跳过")
    return True


def _state_file_stems() -> set[str]:
    """订阅状态文件名的 stem 集合（如 {'Some-Show'}）。"""
    return {p.stem for p in SUB_DIR.glob("*.md")}


def _source_dirs_on_disk() -> set[str]:
    """磁盘上实际存在的笔记源目录名（排除 INDEX.md 那个文件）。"""
    return {p.name for p in NOTES_DIR.iterdir() if p.is_dir()}


# ── 代码侧 ────────────────────────────────────────────────────────────
def test_source_dir_for_sanitizes_feed_title():
    """派生规则：feed.title 原值 → sanitize 结果，不是原样透传。"""
    info = {"feed": {"title": SAMPLE_TITLE}}
    got = v3.source_dir_for(info)
    assert got == SAMPLE_SAFE, f"期望 sanitize 后的 {SAMPLE_SAFE!r}，实际 {got!r}"
    assert got != info["feed"]["title"], "派生结果等于原值 → 说明没做 sanitize"


def test_source_dir_for_handles_missing_title():
    """没有 feed.title 时兜底成 'Unknown'，不能抛异常。"""
    assert v3.source_dir_for({}) == "Unknown"
    assert v3.source_dir_for({"feed": {}}) == "Unknown"


def test_source_dir_for_matches_subscribe_manager_rule():
    """与订阅侧的 `_sanitize_filename()` 必须同规则 —— 两边声明过「规则一致」。"""
    import subscribe_manager
    for title in (SAMPLE_TITLE, "另 一个 节目", "A/B 测试",
                  "  前后空格  ", "带.点的名字"):
        assert v3.source_dir_for({"feed": {"title": title}}) == \
            subscribe_manager._sanitize_filename(title), f"标题 {title!r} 两侧规则不一致"


# ── 数据侧 ────────────────────────────────────────────────────────────
def test_existing_source_dirs_all_match_a_state_file():
    """方向一：磁盘上的每个源目录名都能对上某个订阅状态文件名。"""
    _require_data()
    stems = _state_file_stems()
    assert stems, "订阅目录里没有状态文件 —— 测试数据异常"
    stray = _source_dirs_on_disk() - stems
    assert not stray, (
        f"笔记源目录与订阅状态文件名对不上：{sorted(stray)}。"
        f"（曾出现 'Some Show' vs 'Some-Show.md' 这类带空格的目录名）"
    )


def test_source_dirs_derived_from_info_match_state_files():
    """方向二：每份 info.json 派生出的源目录名都能对上订阅状态文件名。"""
    _require_data()
    if not RAW_DIR.is_dir():
        import pytest
        pytest.skip("没有 raw 原料目录，跳过")
    stems = _state_file_stems()
    seen = {}
    for p in sorted(RAW_DIR.glob("*.info.json")):
        try:
            info = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        d = v3.source_dir_for(info)
        seen.setdefault(d, 0)
        seen[d] += 1
    assert seen, "没有可用的 info.json —— 测试数据异常"
    stray = set(seen) - stems
    assert not stray, (
        f"从 info.json 派生的源目录名对不上订阅状态文件名：{sorted(stray)}"
        f"（订阅状态文件名 stem 集合：{sorted(stems)}）"
    )


def test_sources_with_raw_material_have_a_notes_dir():
    """方向三：有原料的订阅必须有对应的源目录。"""
    _require_data()
    if not RAW_DIR.is_dir():
        import pytest
        pytest.skip("没有 raw 原料目录，跳过")
    stems = _state_file_stems()
    dirs = _source_dirs_on_disk()
    for p in sorted(RAW_DIR.glob("*.info.json")):
        try:
            info = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        d = v3.source_dir_for(info)
        assert d in stems, f"源目录名 {d!r} 不在订阅状态文件名里"
        assert d in dirs, f"订阅 {d!r} 已有原料，但缺笔记源目录 rss/notes/{d}/"


def test_source_dirs_have_no_spaces():
    """sanitize 规则的直接后果：源目录名不含空格（路径带空格是长期隐患）。"""
    _require_data()
    spaced = [d for d in _source_dirs_on_disk() if " " in d]
    assert not spaced, f"源目录名含空格：{spaced} —— 应统一到 sanitize 规则"
