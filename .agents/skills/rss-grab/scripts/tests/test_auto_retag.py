#!/usr/bin/env python3
"""生成脚本「落盘后自动打标」的接线单测（不调 LLM、不起真实打标进程）。

`retag_hook` 自身的行为由 `scripts/retag.py` 旁边的单测覆盖；本文件断言的是
**生成脚本那一侧真的接线了**：正文写完之后调了它，且参数传对。

为什么要单独测接线：「工具本身没问题，只是没人记得调」是最难发现的一类失效 ——
工具测试全绿，功能却是关着的。

覆盖：
1. 单篇落盘后打标被调用，且带上这篇的期号
2. 打标失败时正文文件仍在、内容完整（打标是加分项，不是前置条件）
3. 产物落在生产 notes 目录之外时（暂存目录 / 对照实验）不打标

运行：
    python3 -m pytest .agents/skills/rss-grab/scripts/tests/test_auto_retag.py -v
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent.parent.parent.parent   # tests→scripts→skill→skills→repo
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent.parent / "_shared"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))   # retag_hook 在仓根 scripts/ 下

spec = importlib.util.spec_from_file_location("shell_under_test",
                                              SCRIPTS_DIR / "generate_note_v3.py")
v3 = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = v3
spec.loader.exec_module(v3)

HASH8 = "f35eb04d"
GUID = "rss-episode-guid-0001"


@pytest.fixture
def spy(monkeypatch):
    """把 auto_retag 换成记录器：不调 LLM、不起进程，只记被调了几次。"""
    calls = []

    def fake(source, ids, apply=True):
        calls.append({"source": source, "ids": list(ids)})
        return True

    monkeypatch.setattr(v3, "auto_retag", fake)
    return calls


@pytest.fixture
def staged(tmp_path, monkeypatch):
    """造出「源文 + info.json」，并把生成管线换成假实现。

    重点：本测试**不依赖真实仓库布局**。产物路径与「生产 notes 目录」的
    相对关系由 `_prod_notes` 决定，故这里把仓库根换到 tmp_path。
    """
    root = tmp_path / "repo"
    (root / "rss" / "transcripts").mkdir(parents=True, exist_ok=True)
    (root / "rss" / "raw").mkdir(parents=True, exist_ok=True)
    (root / "rss" / "notes" / "某节目").mkdir(parents=True, exist_ok=True)

    t = root / "rss" / "transcripts" / f"测试标题-{HASH8}.transcript.md"
    t.write_text("# 转写\n**[00:12]** 内容。\n", encoding="utf-8")
    info_path = root / "rss" / "raw" / f"测试标题-{HASH8}.info.json"
    info_path.write_text(
        '{"feed": {"title": "某节目"},'
        ' "item": {"title": "测试标题", "guid": "%s", "guid_hash8": "%s"},'
        ' "local": {}}' % (GUID, HASH8), encoding="utf-8")

    # 打标闸门要判断「产物是否落在生产目录」，单测把这个基准换到 tmp 下。
    # 不用 monkeypatch _SHARED：locate() 等路径推导都依赖它，改了会让本测试
    # 测的是「另一套路径」而不是生产代码本身。改为显式传 --transcript/--info。
    monkeypatch.setattr(v3, "_prod_notes_dir", lambda: root / "rss" / "notes")
    return t, info_path, root


def test_prod_notes_dir_not_offset_by_one_level():
    """断言 4：生产态的 `_prod_notes_dir()` 必须真的指向 `<repo>/rss/notes`。

    **这条是补 2026-10-08 那个静默 bug 的回归防护。**

    当时它写的是 `_SHARED.parent.parent / "rss" / "notes"`，而 `_SHARED`
    = `<repo>/.agents/skills/_shared`，往上两层是 `<repo>/.agents/skills`
    —— 拼出 `<repo>/.agents/rss/notes`，**这个目录不存在**。闸门是
    `prod in output.resolve().parents`，基准不存在时恒为 False，
    于是 **rss 侧自动打标永不触发，且不报任何错**。

    为什么其它测试抓不到：它们都经 `staged` fixture 把路径相关常量
    monkeypatch 到 tmp 下，测的是「另一套路径」，永远碰不到生产推导本身。
    故本测试**不依赖任何 fixture**，直接加载生产模块验证推导。

    实现说明：模块用 `spec_from_file_location` 加载（不在 sys.modules 标准
    名下），`importlib.reload` 会抛 ModuleNotFoundError，所以直接调函数。
    """
    # 按生产源码的原表达式重算，作为「正确答案」
    shared_dir = v3.SCRIPT_DIR.parent.parent / "_shared"
    expected = v3._REPO_ROOT / "rss" / "notes"
    assert expected == shared_dir.parent.parent.parent / "rss" / "notes", (
        f"参考推导本身不一致：{expected} vs "
        f"{shared_dir.parent.parent.parent / 'rss' / 'notes'}")

    actual = v3._prod_notes_dir()

    assert actual.name == "notes" and actual.parent.name == "rss", actual
    # 核心判据：上上级是仓库根，**不是 .agents** —— 这正是原 bug 的形态
    assert actual.parent.parent.name != ".agents", (
        f"_prod_notes_dir() 错位到 {actual}（落在 .agents 下），"
        "闸门基准不存在 → 自动打标永不触发且不报错")
    assert actual == expected, f"{actual} != {expected}"
    assert actual.parent.parent == v3._REPO_ROOT, actual.parent.parent


def test_main_tags_after_write(tmp_path, monkeypatch, spy, staged):
    """断言 1：正文写完后打标被调用一次，且带上这篇期号 + 正确的 source。"""
    t, info_path, root = staged
    out = root / "rss" / "notes" / "某节目" / f"测试标题-{HASH8}.md"

    import build_v3 as shared
    import build_frontmatter as fm

    monkeypatch.setattr(shared, "generate_note_body", lambda *a, **k: (
        "## TL;DR\n\n甲。\n\n## 要点总结\n\n1. 甲\n",
        {"C1_C2_typos": {"kept": 1, "dropped": 0, "items": []},
         "C4_quote_coverage": {"ratio": 0.5},
         "C13_compression": {"low_coverage": False},
         "C11_pronoun": {"ratio": 0.1}},
        {"route": "short", "net_chars": 100, "source_chars": 120,
         "attempts": 1, "llm": {"summary": "摘要"}}))
    monkeypatch.setattr(shared, "net_len", lambda *_: 100)
    monkeypatch.setattr(v3, "mark_subscribe_done", lambda *a, **k: "跳过")

    class _FakeClient:
        pass

    import openai
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: _FakeClient())
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model")

    import append_index
    monkeypatch.setattr(append_index, "register_rss_index",
                        lambda *a, **k: "ok")

    argv = ["generate_note_v3.py", "--transcript", str(t), "--info",
            str(info_path), "--output", str(out), "--no-index", "--force"]
    monkeypatch.setattr(sys, "argv", argv)
    assert v3.main() == 0

    assert out.exists(), "正文应已落盘"
    assert len(spy) == 1, f"落盘后应调一次打标，实际 {len(spy)} 次"
    assert spy[0]["ids"] == [HASH8]
    assert spy[0]["source"] == "rss"


def test_note_intact_when_tagging_fails(tmp_path, monkeypatch, staged):
    """断言 2：打标抛异常时，正文文件仍在、内容完整。

    这是整条接线最关键的安全性质：正文先落盘，打标是之后的加分项。
    """
    t, info_path, root = staged
    out = root / "rss" / "notes" / "某节目" / f"测试标题-{HASH8}.md"

    import build_v3 as shared
    import append_index

    body = "## TL;DR\n\n甲。\n\n## 要点总结\n\n1. 甲\n"
    monkeypatch.setattr(shared, "generate_note_body", lambda *a, **k: (
        body,
        {"C1_C2_typos": {"kept": 1, "dropped": 0, "items": []},
         "C4_quote_coverage": {"ratio": 0.5},
         "C13_compression": {"low_coverage": False},
         "C11_pronoun": {"ratio": 0.1}},
        {"route": "short", "net_chars": 100, "source_chars": 120,
         "attempts": 1, "llm": {"summary": "摘要"}}))
    monkeypatch.setattr(shared, "net_len", lambda *_: 100)
    monkeypatch.setattr(v3, "mark_subscribe_done", lambda *a, **k: "跳过")
    monkeypatch.setattr(append_index, "register_rss_index",
                        lambda *a, **k: "ok")

    import openai
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: object())
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model")

    # 打标抛异常：正文产物必须仍在
    monkeypatch.setattr(v3, "auto_retag",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("tagging exploded")))
    monkeypatch.setattr(sys, "argv",
                        ["generate_note_v3.py", "--transcript", str(t),
                         "--info", str(info_path), "--output", str(out),
                         "--no-index", "--force"])
    try:
        v3.main()
    except RuntimeError:
        pass
    assert out.exists(), "打标失败不该删掉正文产物"
    assert "## 要点总结" in out.read_text(encoding="utf-8")


def test_staging_output_not_tagged(tmp_path, monkeypatch, spy, staged):
    """断言 3：产物落在生产 notes 目录之外时（暂存 / 对照实验）不打标。"""
    t, info_path, root = staged
    elsewhere = tmp_path / "staging" / "reregen"
    elsewhere.mkdir(parents=True, exist_ok=True)
    out = elsewhere / f"测试标题-{HASH8}.md"

    import build_v3 as shared
    import append_index
    monkeypatch.setattr(shared, "generate_note_body", lambda *a, **k: (
        "## TL;DR\n\n甲。\n\n## 要点总结\n\n1. 甲\n",
        {"C1_C2_typos": {"kept": 1, "dropped": 0, "items": []},
         "C4_quote_coverage": {"ratio": 0.5},
         "C13_compression": {"low_coverage": False},
         "C11_pronoun": {"ratio": 0.1}},
        {"route": "short", "net_chars": 100, "source_chars": 120,
         "attempts": 1, "llm": {"summary": "摘要"}}))
    monkeypatch.setattr(shared, "net_len", lambda *_: 100)
    monkeypatch.setattr(v3, "mark_subscribe_done", lambda *a, **k: "跳过")
    monkeypatch.setattr(append_index, "register_rss_index",
                        lambda *a, **k: "ok")

    import openai
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: object())
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model")

    monkeypatch.setattr(sys, "argv",
                        ["generate_note_v3.py", "--transcript", str(t),
                         "--info", str(info_path), "--output", str(out),
                         "--no-index", "--force"])
    v3.main()
    assert out.exists()
    assert spy == [], "非生产目录的产物不该消耗打标额度"