"""correct_typos 共用逻辑回归测试（V1.0，2026-10-04）。

抓的是真实故障：某篇长文 2707 -> 2706 行，assert_structure_preserved 抛错，
整篇不生成。根因是模型报的 original 比原文长（带 ** 或多余标点），
`a0 + len(orig_raw)` 越过匹配点吞掉换行。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_SHARED = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SHARED))

import correct_typos as ct  # noqa: E402


TRANSCRIPT = (
    "**[00:00]** 今天我们聊 ClodFlare 这个项目\n"
    "**[00:15]** 用 ChatGPT\n"
    "和 Claude 都可以写代码\n"
    "**[00:30]** 结束\n"
)


@pytest.mark.parametrize("original", ["**ChatGPT**", "ChatGPT，", "ChatGPT。"])
def test_跨行替换被拒_行数不变(original):
    """original 比原文长时不得跨行——跨了会吞掉换行，整条断言作废。"""
    out, stat = ct.apply_judgments(
        TRANSCRIPT,
        [{"seg": 2, "decision": "replace", "original": original, "target": "GPT"}])
    assert len(out.splitlines()) == len(TRANSCRIPT.splitlines())
    assert stat.get("applied", 0) == 0
    assert stat.get("span_nl", 0) == 1
    ct.assert_structure_preserved(TRANSCRIPT, out)   # 不抛异常即通过


def test_单行错字仍然生效():
    """修的是跨行，不是把替换整体堵死：正常的错字纠正必须照旧落成。

    用 `ClodFlare → Cloudflare`（同长、只差一个换位字母），
    刻意避开会被 looks_like_rewrite 拦下的高改动比例用例。
    """
    out, stat = ct.apply_judgments(
        TRANSCRIPT,
        [{"seg": 1, "decision": "replace", "original": "ClodFlare", "target": "Cloudflare"}])
    assert "Cloudflare 这个项目" in out
    assert "ClodFlare" not in out
    assert stat.get("applied") == 1
    ct.assert_structure_preserved(TRANSCRIPT, out)
