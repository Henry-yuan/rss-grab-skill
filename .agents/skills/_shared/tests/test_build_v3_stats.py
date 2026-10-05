"""build_v3 统计回填的回归测试（2026-10-05）。

抓的是「报告口径与实际不符」这一类缺陷 —— 产物是对的，但统计字段说谎，
于是任何拿它做验收的判断都会得出错误结论。本轮已抓到两例：

1. 长文路径的纠错报告从不回填 stats，导致**所有长文笔记的统计行一律显示
   「纠错=0 处」**，哪怕实际落成了 160 处（实测某篇长文：判定 168 /
   落成 160 / 95.2%，统计行却写 0）。短文分支有回填，长文分支漏了。
2. detect_typos 静默截断，报告的 segments 却按全文记（见 test_correct_typos_shared）。

本文件只测**字段有没有被回填**，不测数值对不对（那属于 LLM 产物的验收，
要用真实数据跑，见 docs/版本 的成功指标）。
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

_SHARED = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SHARED))

import build_v3 as bv  # noqa: E402
import correct_typos as ct  # noqa: E402
import validate_v3 as v3  # noqa: E402


FAKE_TYPO_REPORT = {
    "judgments": 168,
    "applied_count": 160,
    "success_rate": 0.952,
    "coverage": 1.0,
}


def _long_source(n: int = 4000) -> str:
    """净字 > LONG_DOC_THRESHOLD 的一段源文，触发长文路径。"""
    return "\n".join(f"第 {i} 段讲的是 AI Agent 的工程化落地与成本结构" for i in range(n))


def _run_long(monkeypatch, typo_report):
    """把 ② ③ 与 ④ 全 mock 掉，只走真实的长文分支调度与 stats 累积。"""
    long_info = {
        "topics": [{"title": "主题一", "points": ["要点一"]}],
        "typo_report": typo_report,
        "clean_text": "清洗后的正文",
        "n_points": 1,
        "n_chunks": 1,
    }
    monkeypatch.setattr(bv, "run_long_path", lambda *a, **k: long_info)
    monkeypatch.setattr(
        bv, "call_llm_topics",
        lambda *a, **k: json.dumps({"summary": "摘要", "tags": ["标签"],
                                    "body": "## TL;DR\n\n- 要点\n\n"
                                            "## 要点总结\n\n正文\n\n"
                                            "## 关键引用\n\n- 引用\n"}))
    monkeypatch.setattr(
        v3, "validate",
        lambda body, src, info, llm: {"ok": True, "body": body, "report": {},
                                      "retries_needed": []})
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        src = td / "s.transcript.md"
        src.write_text(_long_source(), encoding="utf-8")
        return bv.generate_note_body(src, {"title": "t"}, object(),
                                     td / "work", td / "typos")


def test_长文的纠错结果必须回填到统计(monkeypatch):
    """2026-10-05 缺陷：长文统计行恒显「纠错=0 处」。

    本仓库这条路径漏了；另一源是独立实现且早就写对了。
    """
    _, _, stats = _run_long(monkeypatch, FAKE_TYPO_REPORT)
    assert stats["route"] == "chunked", "本用例必须走长文路径，否则测不到那个分支"
    assert stats["typo_applied"] == 160, \
        f"长文统计的纠错处数没回填，实得 {stats.get('typo_applied')}"
    assert stats["typo_judgments"] == 168
    assert stats["typo_success_rate"] == 0.952
    assert stats["typo_coverage"] == 1.0


def test_长文统计里不许残留零值默认值(monkeypatch):
    """守门：只要 run_long_path 返回了 typo_report，对应字段就都得有真值。

    比单点断言更耐用 —— 以后再加字段又忘了回填，这条会抓到。
    """
    _, _, stats = _run_long(monkeypatch, FAKE_TYPO_REPORT)
    for k, want in (("typo_applied", 160), ("typo_judgments", 168)):
        assert stats.get(k) == want, f"{k} 缺失或仍是默认值：{stats.get(k)}"


def test_短文路径的纠错统计不被这次改动破坏(monkeypatch):
    """同一份 stats 的短文分支本来就有回填，不能被长文的改法带歪。"""
    def _fake_run_file(src_path, out_path, typos_out, client=None, effort="high"):
        # 真实 run_file 会把清洗后的正文写到 out_path，后续流程要**读回来**。
        # mock 只返回 dict 的话，短文分支会在 read_text 处 FileNotFoundError。
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(src_path.read_text(encoding="utf-8"), encoding="utf-8")
        typos_out.parent.mkdir(parents=True, exist_ok=True)
        typos_out.write_text("{}", encoding="utf-8")
        return {"applied_count": 7, "success_rate": 0.9,
                "judgments": 9, "coverage": 1.0}

    monkeypatch.setattr(ct, "run_file", _fake_run_file)
    monkeypatch.setattr(
        bv, "call_llm",
        lambda *a, **k: json.dumps({"summary": "摘要", "tags": ["标签"],
                                    "body": "## TL;DR\n\n- 要点\n\n"
                                            "## 要点总结\n\n正文\n\n"
                                            "## 关键引用\n\n- 引用\n"}))
    monkeypatch.setattr(
        v3, "validate",
        lambda body, src, info, llm: {"ok": True, "body": body, "report": {},
                                      "retries_needed": []})
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        src = td / "s.transcript.md"
        # 要过 MIN_SOURCE_CHARS=200 的源文下限、又不超 LONG_DOC_THRESHOLD=15000，
        # 才真的落在短文分支上
        src.write_text("短文源文，讲的是 AI Agent 的工程化落地与成本结构。" * 12,
                       encoding="utf-8")
        _, _, stats = bv.generate_note_body(src, {"title": "t"}, object(),
                                            td / "work", td / "typos")
    assert stats["route"] == "single"
    assert stats["typo_applied"] == 7, f"短文回填被破坏：{stats.get('typo_applied')}"
