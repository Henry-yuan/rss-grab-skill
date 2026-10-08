#!/usr/bin/env python3
"""generate_note_v3 薄壳单测：订阅状态标记接线 + 空源短路。

覆盖两处曾静默失效的接线（2026-10-05 修）：

1. **mark_done 调用形状**：真实签名是 `(state_path, guid, note_path)` 三个必填参数，
   且状态文件里存的是**完整 guid**。曾写成 `mark_done(a.guid_hash8)` —— 参数数量
   都不对，异常还被 `except: pass` 吞掉，整条接线从未通过过一次。
2. **空源短路**：共享层在源文过短时 `return "", {...}, {"status": "empty"}`，
   此时 stats 里**没有** net_chars / route。不短路就会先落空正文笔记、登记进生产
   INDEX，再在统计行 KeyError；占位信头又大于防重闸门 500 字节 → 重跑永远被跳过。

不调 LLM：`build_v3.generate_note_body` 与索引写入都被替换掉。
"""
from __future__ import annotations

import inspect
import json
import pathlib
import sys
import types

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent.parent / "_shared"))

import build_frontmatter as fm          # noqa: E402
import build_v3                        # noqa: E402
import append_index                      # noqa: E402
import generate_note_v3 as v3          # noqa: E402
import subscribe_manager as sm         # noqa: E402

FULL_GUID = "rss-episode-guid-0001"
HASH8 = "f35eb04d"
SHOW = "独树不成林"


def _info(guid: str = FULL_GUID) -> dict:
    """rss info.json 的最小形状（feed/item/local 三层）。"""
    return {
        "feed": {"title": SHOW, "author": "作者", "link": "https://example.com/p"},
        "item": {"title": "361-测试标题", "guid": guid, "guid_hash8": HASH8,
                 "pub_date": "Thu, 06 Aug 2026 05:10:23 GMT",
                 "duration_seconds": 2104, "link": "https://example.com/e/1"},
        "local": {"fetched_at": "2026-08-08T19:05:30+0800"},
    }


def _state_file(tmp_path: pathlib.Path, guid: str = FULL_GUID) -> pathlib.Path:
    """建一个含该期（待确认区）的状态文件，并让订阅表指过来。"""
    (tmp_path / "rss" / "订阅").mkdir(parents=True, exist_ok=True)
    state_path = tmp_path / "rss" / "订阅" / f"{SHOW}.md"
    state = sm._empty_state()
    state["frontmatter"] = {"source": SHOW, "feed_url": "https://example.com/feed",
                            "subscribed_at": "2026-08-11"}
    state["confirmed"] = [{"checkbox": "[x]", "seq": 361, "title": "测试标题",
                           "guid": guid, "fields": {}, "note_path": ""}]
    sm.save_state(state_path, state)

    (tmp_path / "subscriptions.json").write_text(
        json.dumps({"sources": [{"name": SHOW, "feed_url": "https://example.com/feed",
                                 "state_file": f"rss/订阅/{SHOW}.md"}]}),
        encoding="utf-8")
    return state_path


def _point_manager_at(tmp_path: pathlib.Path, monkeypatch) -> None:
    """把订阅表的两个根常量指向 tmp_path（模块级常量，patch 后立即生效）。"""
    monkeypatch.setattr(sm, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(sm, "SUBSCRIPTIONS_PATH", tmp_path / "subscriptions.json")


# ── 1. mark_done 调用接线 ────────────────────────────────────────

def test_mark_done_signature_is_three_required_args():
    """锁住真实签名：改坏它就是重演「只传 1 个参数」的 TypeError。"""
    params = list(inspect.signature(sm.mark_done).parameters)
    assert params == ["state_path", "guid", "note_path"]


def test_mark_subscribe_done_called_with_3_typed_args(monkeypatch, tmp_path):
    """mock 验证：3 个位置参数、类型正确、guid 用完整 guid 而非 hash8。"""
    calls: list[tuple] = []

    def _mark_done(*args, **kwargs):
        calls.append((args, kwargs))
        return True

    fake = types.SimpleNamespace(
        PROJECT_ROOT=tmp_path,
        load_subscriptions=lambda: {"sources": [
            {"name": SHOW, "state_file": f"rss/订阅/{SHOW}.md"}]},
        mark_done=_mark_done,
    )
    monkeypatch.setitem(sys.modules, "subscribe_manager", fake)

    note = tmp_path / "rss" / "notes" / SHOW / "361-测试标题.md"
    result = v3.mark_subscribe_done(_info(), note)

    assert len(calls) == 1, f"mark_done 应被调 1 次，实际 {len(calls)} 次"
    args, kwargs = calls[0]
    assert kwargs == {}, "应全部按位置传参"
    assert len(args) == 3, f"应传 3 个参数，实际 {len(args)} 个"

    state_path, guid, note_path = args
    assert isinstance(state_path, pathlib.Path)
    assert state_path == tmp_path / "rss" / "订阅" / f"{SHOW}.md"
    assert isinstance(guid, str) and guid == FULL_GUID
    assert guid != HASH8, "绝不能把 guid_hash8 当 guid 传（全等匹配必然落空）"
    assert isinstance(note_path, str) and note_path == str(note)
    assert "已标记" in result


def test_mark_subscribe_done_really_moves_item_to_done(monkeypatch, tmp_path):
    """不 mock：真状态文件 + 真 mark_done，确认完整 guid 匹配得上、条目被搬走。"""
    state_path = _state_file(tmp_path)
    _point_manager_at(tmp_path, monkeypatch)

    note = tmp_path / "rss" / "notes" / SHOW / "361-测试标题.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# 笔记\n", encoding="utf-8")

    result = v3.mark_subscribe_done(_info(), note)
    assert "已标记" in result, result

    state = sm.load_state(state_path)
    assert len(state["confirmed"]) == 0
    assert len(state["done"]) == 1
    assert state["done"][0]["checkbox"] == "[done]"
    assert state["done"][0]["note_path"].endswith("361-测试标题.md")


def test_mark_subscribe_done_reports_failure_instead_of_swallowing(monkeypatch, tmp_path):
    """原实现 `except: pass` 让接线失败完全无声；现在必须把原因带出来。"""
    _state_file(tmp_path)
    _point_manager_at(tmp_path, monkeypatch)
    monkeypatch.setattr(sm, "mark_done",
                        lambda *a, **k: (_ for _ in ()).throw(TypeError("参数不对")))

    result = v3.mark_subscribe_done(_info(), tmp_path / "n.md")
    assert "失败" in result and "TypeError" in result, result


def test_mark_subscribe_done_skips_when_no_full_guid(monkeypatch, tmp_path):
    """只有 hash8 没有完整 guid 时必须明确说跳过，而不是拿 hash8 去撞。"""
    _state_file(tmp_path)
    _point_manager_at(tmp_path, monkeypatch)

    called: list = []
    monkeypatch.setattr(sm, "mark_done", lambda *a, **k: called.append(a))

    info = _info()
    info["item"].pop("guid")
    result = v3.mark_subscribe_done(info, tmp_path / "n.md")
    assert "跳过" in result and "item.guid" in result
    assert called == [], "缺完整 guid 时不该调 mark_done"


def test_mark_subscribe_done_skips_show_not_in_subscription_table(monkeypatch, tmp_path):
    """没订阅就不是订阅流程的事，正常跳过。"""
    _point_manager_at(tmp_path, monkeypatch)
    (tmp_path / "subscriptions.json").write_text(
        json.dumps({"sources": [{"name": "别的节目", "state_file": "rss/订阅/x.md"}]}),
        encoding="utf-8")

    called: list = []
    monkeypatch.setattr(sm, "mark_done", lambda *a, **k: called.append(a))
    result = v3.mark_subscribe_done(_info(), tmp_path / "n.md")
    assert "不在订阅表里" in result
    assert called == []


# ── 2. 空源短路 ─────────────────────────────────────────────────

def _empty_body(*_args, **_kwargs):
    """共享层在源文过短时的真实返回形状：**没有** net_chars / route。"""
    return "", {"C0_empty_source": {"chars": 44}}, {
        "stem": "x", "route": "", "attempts": 0,
        "source_chars": 44, "status": "empty",
    }


def _run_main(monkeypatch, tmp_path, argv, empty: bool = True):
    """跑一次薄壳 main()：LLM 客户端不联网，索引写入被换成探针。"""
    monkeypatch.setenv("LLM_API_KEY", "test-key-not-a-real-one")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    for var in ("MINIMAX_API_KEY", "MINIMAX_BASE_URL"):
        monkeypatch.delenv(var, raising=False)

    indexed: list = []
    # T2.3 起薄壳从 _shared/append_index 取索引入口（不再是 v2 的 generate_note），
    # 而那行 import 在 main() 内部、每次调用时才执行 —— 所以探针必须打在
    # append_index 上，打在 generate_note 上会「改了但没生效」且测试照样绿。
    monkeypatch.setattr(append_index, "register_rss_index",
                        lambda info, out: indexed.append(out) or "ok")
    marked: list = []
    # raising=False：探针装不上也不该让测试报 AttributeError ——
    # 那样测的就只是「函数在不在」，不是「空源时到底发生了什么」。
    monkeypatch.setattr(v3, "mark_subscribe_done",
                        lambda info, out: marked.append(out) or "已标记 x.md",
                        raising=False)
    if empty:
        monkeypatch.setattr(build_v3, "generate_note_body", _empty_body)
    monkeypatch.setattr(sys, "argv", ["generate_note_v3.py"] + argv)
    return v3.main(), indexed, marked


def _fixture_paths(tmp_path: pathlib.Path):
    transcript = tmp_path / "361-测试标题.md.transcript.md"
    transcript.write_text("# 转写\n\n[00:00] 极短。\n", encoding="utf-8")
    info_json = tmp_path / "361-测试标题.md.info.json"
    info_json.write_text(json.dumps(_info(), ensure_ascii=False), encoding="utf-8")
    return transcript, info_json


def test_empty_source_writes_placeholder_and_does_not_crash(monkeypatch, tmp_path):
    """空源：落占位笔记、退出码 0、不 KeyError。"""
    transcript, info_json = _fixture_paths(tmp_path)
    out = tmp_path / "out" / "note.md"

    rc, indexed, marked = _run_main(monkeypatch, tmp_path, [
        "--transcript", str(transcript), "--info", str(info_json),
        "--output", str(out), "--work-root", str(tmp_path / "work")])

    assert rc == 0
    assert out.exists(), "应落占位笔记"
    text = out.read_text(encoding="utf-8")
    assert text.startswith("---"), "占位笔记仍要有信头"
    assert "transcript_status: empty" in text
    assert "未生成正文" in text
    # 真实缺陷的形状：空笔记不该进生产索引，也不该标订阅「已转化」
    assert indexed == [], "占位笔记不是产物，不进索引"
    assert marked == [], "占位笔记不是产物，不标订阅状态"
    # 也没有残留临时文件
    assert [p.name for p in out.parent.iterdir()] == ["note.md"]


def test_empty_source_with_no_index_flag(monkeypatch, tmp_path):
    """--no-index（暂存批次）下同样落占位、不崩、不进索引。"""
    transcript, info_json = _fixture_paths(tmp_path)
    out = tmp_path / "out" / "note.md"

    rc, indexed, _ = _run_main(monkeypatch, tmp_path, [
        "--transcript", str(transcript), "--info", str(info_json),
        "--output", str(out), "--work-root", str(tmp_path / "work"),
        "--no-index"])

    assert rc == 0
    assert out.exists()
    assert "transcript_status: empty" in out.read_text(encoding="utf-8")
    assert indexed == []


def test_placeholder_note_respects_frontmatter_contract(monkeypatch, tmp_path):
    """占位笔记的信头也要过 17 字段契约（否则下游按字段取值会踩空）。"""
    transcript, info_json = _fixture_paths(tmp_path)
    out = tmp_path / "out" / "note.md"

    _run_main(monkeypatch, tmp_path, [
        "--transcript", str(transcript), "--info", str(info_json),
        "--output", str(out), "--work-root", str(tmp_path / "work")])

    fmv = fm.build(_info(), transcript_status="empty")
    assert fm.check(fmv) == [], f"占位信头应满足字段契约：{fm.check(fmv)}"


def test_empty_short_circuit_precedes_dedup_gate_report(monkeypatch, tmp_path, capsys):
    """占位信头大小随 info 内容变：真实简介够长时会超 500 闸门，提示要说清要加 --force。

    最小 info 的占位只有 480 字节（不超闸门），所以这里用贴近真实播客的
    标题 + 简介复现「被闸门拦下、必须 --force」的那一档。
    """
    transcript, info_json = _fixture_paths(tmp_path)
    info = _info()
    info["item"]["title"] = "361-为什么我们总是高估自己的能力边界，以及如何校准判断"
    info["feed"]["description"] = (
        "本期聊聊判断校准：为什么人在信息不足时倾向于高估自己的能力，"
        "以及有哪些可操作的方法能把判断拉回基准线。")
    info_json.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "out" / "note.md"

    _run_main(monkeypatch, tmp_path, [
        "--transcript", str(transcript), "--info", str(info_json),
        "--output", str(out), "--work-root", str(tmp_path / "work")])

    assert out.stat().st_size > 500, "真实长度的信头确实超闸门（重跑会被拦下）"
    assert "--force" in capsys.readouterr().out


def test_placeholder_below_gate_says_rewrite_is_unaffected(monkeypatch, tmp_path, capsys):
    """信头够短没过闸门时，别谎报「须加 --force」。"""
    transcript, info_json = _fixture_paths(tmp_path)
    out = tmp_path / "out" / "note.md"

    _run_main(monkeypatch, tmp_path, [
        "--transcript", str(transcript), "--info", str(info_json),
        "--output", str(out), "--work-root", str(tmp_path / "work")])

    assert out.stat().st_size <= 500
    printed = capsys.readouterr().out
    assert "未超防重闸门" in printed and "--force" not in printed
