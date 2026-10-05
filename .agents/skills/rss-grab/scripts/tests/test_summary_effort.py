#!/usr/bin/env python3
"""RSS 摘要的 LLM 调用参数（2026-10-05 补）。

起因：V1.0 spec 验收判据第 3 条要求「三源在用管线的每个
`chat.completions.create` 调用点都带 `reasoning_effort` +
`max_completion_tokens` + `timeout`」。本文件当时**漏改了**：
`max_completion_tokens` 有、timeout 挂在 client 上有，唯独 `reasoning_effort`
完全没设 —— 而官方「不设 = Max」，于是订阅拉取的短摘要一直跑在 Max 上。

讽刺的是同一段代码里就有注释讲「thinking token 计入 max_completion_tokens 预算」，
两个参数本是配套的，改了一个漏了另一个。

变异验证点：删掉 `reasoning_effort=` 那一行，本文件必须变红。
"""
import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parent.parent / "preview_podcast.py"
src = SRC.read_text(encoding="utf-8")


def _code_only(text: str) -> str:
    """剥掉注释行。

    ⚠️ 必须剥：本文件里那段「此前没设 reasoning_effort」的说明注释**本身就含
    `reasoning_effort` 这个词**，不剥的话断言会匹配到注释、永远为真 ——
    变异验证时实测踩过：删掉真的那行，只红了一条、另一条仍然绿。
    """
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def _summary_call() -> str:
    code = _code_only(src)
    m = re.search(r"chat\.completions\.create\((.*?)\n\s*\)\s*\n", code, re.S)
    assert m, "没找到摘要的 chat.completions.create 调用"
    return m.group(1)


def test_摘要调用必须带_reasoning_effort():
    call = _summary_call()
    assert "reasoning_effort" in call, \
        "摘要调用没带 reasoning_effort —— 官方默认是 Max，短摘要不该跑 Max"


def test_默认强度是_high_且可覆盖():
    call = _summary_call()
    assert 'os.environ.get("LLM_EFFORT", "high")' in call, \
        "默认强度应为 high，且允许用 LLM_EFFORT 覆盖"


def test_三个参数齐全():
    call = _summary_call()
    assert "max_completion_tokens" in call
    assert "reasoning_effort" in call
    # timeout 挂在 OpenAI client 上（150-175 行），不在调用点
    assert "timeout=600.0" in _code_only(src), "client 上应有 timeout"


def test_摘要预算保持_16000_不跟笔记的_100000_混用():
    """V1.0 spec 明确：摘要的值保留，只改参数名 —— 不因补 effort 就去动它。"""
    assert "SUMMARY_MAX_COMPLETION" in src
    assert 'LLM_SUMMARY_MAX_COMPLETION", "16000"' in src
    # 反例：若哪天有人「顺手统一成 100000」，这条会拦下
    assert '"16000"' in src and "MAX_COMPLETION" not in src.replace("SUMMARY_MAX_COMPLETION", "")
