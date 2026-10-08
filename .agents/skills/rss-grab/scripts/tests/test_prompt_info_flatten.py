#!/usr/bin/env python3
"""喂给 LLM 的元信息必须非空（嵌套 info 拍平）。

## 这个测试拦的是什么

`_shared/build_v3.py` 的 `info_for_prompt()` 按白名单**直取 info 顶层**
（`info.get(k)`），而本源的 info.json 是 `{feed, item, local}` **三层嵌套**，
顶层没有 `title`/`description` 这些键 → 白名单全部落空，喂给模型的元信息区块
**恒为空**。同一模块的 `{desc}` 模板占位符同样直取顶层，一并失效。
信头正常是因为 `fm.build()` 会解嵌套 —— 缺的正是喂模型这一侧的适配。

实测全库 96/96 份真实 info.json 在拍平前元信息全空。

## 为什么用「真的嵌套形状」而不是扁平 dict 假装

用扁平 dict 造数据会让测试**恒绿**：白名单本来就能命中，测的只是
`dict.get` 能不能取到值，完全绕过了「顶层没有这些键」这个真缺陷。
所以这里一律用 `{feed, item, local}` 三层结构。

## 关键断言：`info_for_prompt()` 的输出非空

只测「拍平函数返回了 dict」是弱断言 —— 返回个空 dict 照样过。
必须走**共享层真实的 `info_for_prompt()`**，断言它吐出的文本里有值，
才能证明模型真的看得到元信息。

不调 LLM：`info_for_prompt` 与 `fm.prompt_info` 都是纯函数。
"""
from __future__ import annotations

import copy
import pathlib
import sys

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent.parent / "_shared"))

import build_frontmatter as fm          # noqa: E402
import build_v3                          # noqa: E402

SHOW = "独树不成林"
TITLE = "361-测试标题｜对谈于文渊：某云技术负责人"
DESC = "本期聊推理成本与模型选型，欢迎订阅。"


def _nested_info() -> dict:
    """rss info.json 的**真实嵌套形状**：顶层只有 feed/item/local 三层。

    刻意不含任何顶层 `title`/`description` —— 真实数据就是如此，
    这正是元信息区块全空的原因。
    """
    return {
        "feed": {"title": SHOW, "author": "作者",
                 "link": "https://example.com/feed",
                 "description": DESC, "language": "zh"},
        "item": {"title": TITLE, "guid": "rss-episode-guid-0001",
                 "guid_hash8": "f35eb04d",
                 "pub_date": "Thu, 06 Aug 2026 05:10:23 GMT",
                 "duration_seconds": 2104, "link": "https://example.com/e/1"},
        "local": {"audio_path": "/tmp/x.m4a", "fetched_at": "2026-08-08T19:05:30+0800"},
    }


def _prompt_text(info: dict) -> str:
    """走共享层真实的取数路径（白名单来自本源的 fm.PROMPT_INFO_FIELDS）。"""
    prev = build_v3.PROMPT_INFO_FIELDS
    build_v3.PROMPT_INFO_FIELDS = fm.PROMPT_INFO_FIELDS
    try:
        return build_v3.info_for_prompt(info)
    finally:
        build_v3.PROMPT_INFO_FIELDS = prev


# ── 1. 缺陷复现：不拍平时元信息确实是空的 ────────────────────────

def test_nested_info_without_flatten_yields_empty_prompt():
    """先确认缺陷真实存在：嵌套 info 直传共享层 → 元信息区块为空。

    这条是**对照基线**。若哪天共享层改成会解嵌套，它会变红，
    那时下面的拍平断言就失去意义了（说明问题已在别处解决）。
    """
    assert _prompt_text(_nested_info()) == "", \
        "嵌套 info 直传共享层本就该取不到值；若这里非空，说明共享层取数方式已变"


# ── 2. 拍平后：白名单字段必须真的取到值 ────────────────────────

def test_prompt_info_gets_title_and_description_from_nested():
    """拍平后至少能取到 title 与 description。"""
    flat = fm.prompt_info(_nested_info())
    assert flat["title"] == TITLE, flat.get("title")
    assert flat["description"] == DESC, flat.get("description")


def test_prompt_info_covers_entire_whitelist():
    """白名单里每个键都要有键存在（无数据给空串，不省略 key）。

    省略 key 与给空串在 `info_for_prompt` 里表现一样，但省略会让
    「这个字段本仓到底有没有映射」变得不可见 —— 跨源比对时要靠键集稳定。
    """
    flat = fm.prompt_info(_nested_info())
    for k in fm.PROMPT_INFO_FIELDS:
        assert k in flat, f"白名单字段 {k} 在拍平结果里没有键"
        assert isinstance(flat[k], str), f"{k} 应为 str，实际 {type(flat[k])}"


def test_prompt_text_is_not_empty_after_flatten():
    """**核心断言**：走共享层真实的 info_for_prompt，输出必须非空。

    只断言「返回了 dict」是弱断言（返回空 dict 照样过）。
    必须证明模型真的看得到元信息。
    """
    text = _prompt_text(fm.prompt_info(_nested_info()))
    assert text.strip(), "拍平后喂给 LLM 的元信息区块仍为空"
    assert TITLE in text, text
    assert DESC in text, text
    assert SHOW in text, f"节目名应在元信息里：{text}"


def test_desc_placeholder_no_longer_falls_back():
    """共享层 `{desc}` 占位符取 `info.get("description")` —— 也直取顶层。

    拍平前恒为 "(无)"，等于把简介整段丢掉。
    """
    flat = fm.prompt_info(_nested_info())
    desc = (flat.get("description") or "(无)").strip() or "(无)"
    assert desc == DESC, desc


# ── 3. 同源：与 frontmatter 取值一致 ──────────────────────────

def test_flattened_values_match_frontmatter():
    """拍平值必须与信头取值逐字段一致（同一来源，不是两套逻辑）。

    若哪天两边分叉，本条会红 —— 那说明模型看到的元信息与笔记信头对不上。
    """
    info = _nested_info()
    fmv = fm.build(info)
    flat = fm.prompt_info(info)
    for k in fm.PROMPT_INFO_FIELDS:
        assert flat[k] == fmv[k], \
            f"{k} 拍平值 {flat[k]!r} 与信头值 {fmv[k]!r} 不一致"


def test_speaker_and_duration_survive_flatten():
    """嘉宾与时长也走白名单：确认解析结果没在拍平时丢掉。"""
    flat = fm.prompt_info(_nested_info())
    assert flat["speaker"] == "于文渊", flat["speaker"]
    assert flat["duration"] == "35:04", flat["duration"]   # 2104 秒
    assert flat["type"] == "podcast"


# ── 4. 纯附加：不破坏嵌套读法、不改入参、不碰落盘 ───────────────

def test_nested_keys_are_preserved():
    """拍平是**纯附加**：feed/item/local 必须还在。

    薄壳后续还用原始 info 做信头、索引与状态标记；
    共享层若将来读嵌套键，也不能因为这次拍平拿到 None。
    """
    flat = fm.prompt_info(_nested_info())
    assert flat["feed"]["title"] == SHOW
    assert flat["item"]["guid_hash8"] == "f35eb04d"
    assert flat["local"]["fetched_at"] == "2026-08-08T19:05:30+0800"


def test_input_info_is_not_mutated():
    """不改入参：调用方的 info 还要拿去 build 信头、注册索引。"""
    info = _nested_info()
    before = copy.deepcopy(info)
    fm.prompt_info(info)
    assert info == before, "prompt_info 不得就地修改传入的 info"
    assert "title" not in info, "不得往入参顶层塞字段"


def test_missing_layers_do_not_crash():
    """缺层 / 空层不炸：无数据就是空串（不猜、不填占位值）。"""
    # `type` 是常量（本仓只产 podcast），不由数据推导，故不参与「必须为空」判断。
    data_fields = tuple(k for k in fm.PROMPT_INFO_FIELDS if k != "type")
    for bad in ({}, {"feed": None, "item": None, "local": None},
                {"feed": {}, "item": {}}):
        flat = fm.prompt_info(bad)
        for k in fm.PROMPT_INFO_FIELDS:
            assert k in flat and isinstance(flat[k], str), (bad, k)
        assert all(flat[k] == "" for k in data_fields), \
            f"无数据时必须留空串而非占位值：{flat}"
        assert flat["type"] == "podcast"      # 常量字段恒为 podcast

    # 只有 feed.title 时，唯一有值的是节目名
    flat = fm.prompt_info({"feed": {"title": SHOW}})
    assert flat["show_name"] == SHOW
    assert all(flat[k] == "" for k in data_fields if k != "show_name"), flat


# ── 5. 接线：薄壳喂给共享层的必须是拍平后的 dict ────────────────

def test_thin_shell_passes_flattened_info_to_pipeline(monkeypatch, tmp_path):
    """锁住调用点：`generate_note_body` 收到的必须是拍平后的 dict。

    拍平函数写得再对，调用点忘了用照样全空 —— 这条直接看传参。
    """
    import json
    import generate_note_v3 as v3

    # ⚠️ 三个键**都要**给：`_shared/env.py` 的 `load_env()` 缺任一键都会
    # 会 `sys.exit`（这是有意的防呆，不是 bug），而 `generate_note_v3.main()`
    # 在流程开头就调它。此前这里只设了 API_KEY / BASE_URL，漏了 LLM_MODEL ——
    # 开发机上有 `scripts/.env` 兜底所以全绿，干净克隆（无 .env）直接 exit。
    # 值也不能用含 `your-api` 的占位串：`load_env` 会把它当未配置再 exit 一次。
    monkeypatch.setenv("LLM_API_KEY", "test-key-not-a-real-one")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model-name")
    for var in ("MINIMAX_API_KEY", "MINIMAX_BASE_URL"):
        monkeypatch.delenv(var, raising=False)

    transcript = tmp_path / "361-测试标题.md.transcript.md"
    transcript.write_text("# 转写\n\n[00:00] 内容。\n", encoding="utf-8")
    info_json = tmp_path / "361-测试标题.md.info.json"
    info_json.write_text(json.dumps(_nested_info(), ensure_ascii=False),
                         encoding="utf-8")
    out = tmp_path / "out" / "note.md"

    seen: list[dict] = []

    def _capture(_src, info, *_a, **_k):
        seen.append(info)
        return "", {"C0_empty_source": {"chars": 1}}, {
            "stem": "x", "route": "", "attempts": 0,
            "source_chars": 1, "status": "empty",
        }

    monkeypatch.setattr(build_v3, "generate_note_body", _capture)
    monkeypatch.setattr(sys, "argv", [
        "generate_note_v3.py", "--transcript", str(transcript),
        "--info", str(info_json), "--output", str(out), "--no-index",
    ])

    rc = v3.main()
    assert rc == 0
    assert len(seen) == 1, f"共享层应被调 1 次，实际 {len(seen)} 次"

    got = seen[0]
    assert got.get("title") == TITLE, "喂给共享层的 dict 没有顶层 title"
    assert got.get("description") == DESC, "喂给共享层的 dict 没有顶层 description"
    # 原始嵌套结构同时保留（纯附加）
    assert got["item"]["guid_hash8"] == "f35eb04d"