#!/usr/bin/env python3
"""T2.6 · 「残留率」指标的语义回归测试（2026-10-05）。

## 为什么要有这个指标

`correct_typos` 的报告只有一个 `success_rate`（M1），它答的是
「**改了的里面有多少改对**」（精确率），完全不答「**该改的改没改**」（召回）。

V1.0 实测：clean 文本里仍残留约 12 处「能修而没修」的错字，而真正改坏的
只有 3 处 —— **M1 高达 98.31%，完全掩盖了「召回≈0」**。98.31% 读起来像
「纠错几乎完美」，实际含义是「几乎没改，所以没什么可改坏」。

## 本文件锁住的是**语义**，不是「字段存在」

下面每条断言都对应一个「这个指标会怎么骗人」的具体场景：

| 场景 | 会怎么骗人 | 对应用例 |
|---|---|---|
| 没调过 LLM | 报 `0` = 「一处残留都没有」 | 用例 4a |
| LLM 全挂（零窗成功） | 同上，且看起来是「跑过了」 | 用例 4b |
| 拿全文段数当分母 | 把「没送检」算成「改干净了」 | 用例 5 |
| 模型判过 keep | 把「模型的决定」算成「模型的漏改」 | 用例 3 |
| 邻域容差用太宽 | 全文别处提过一次就把残留全抹掉 | 用例 3b |
| 候选扫描恒返回 0 | 指标看着在跑，实际永远报干净 | 用例 6 |

## 判据本身的诚实边界

`scan_residual_candidates` 只认 `ACOUSTIC_HINTS` 里那 9 条硬编码音近写法，
所以它是**下界**：中文音近错字一处也扫不出来，且 `chain box` / `immunator`
这类词在别的语境里可能是真词（误报风险）。
因此报告同时给出 `residual_items` 逐条证据 —— **数字必须配证据一起看**。
用例 7 把这份局限也锁住：词表与提示词必须对得上。

⚠️ 本文件在主项目与 opensource 各有一份，**不得出现任何具体平台的名字或 ID**
（opensource 的守门测试会拦）。举例一律用声学规律里已有的音近词本身。
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
import tempfile

import pytest

_SHARED = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SHARED))

import correct_typos as ct  # noqa: E402


# ── 造数用的假 client：不发网络请求，但真的走完 _call → _parse_json ──
#
# ⚠️ 关键：必须真的返回 **JSON 字符串**（而不是直接返回 dict），
# 否则走不到 `_parse_json`，这条链路就不是生产链路了。

class _FakeResp:
    def __init__(self, content):
        msg = type("M", (), {"content": content})()
        self.choices = [type("C", (), {"message": msg})()]


class _ScriptedClient:
    """按窗回预置 judgments；`fail_from_seg` 判「窗内**首段**号 ≥ 它」的窗失败。

    ⚠️ 为什么按**首段**判失败而不是「有任意一段 ≥ 它」：分窗是连续切段的，
    按后者判的话连首窗都会被误杀（全灭 → 测不出「部分失败」）。
    `fail_from_seg=1` 表示全灭。
    """

    def __init__(self, judgments=(), fail_from_seg: int | None = None):
        self.judgments = list(judgments)
        self.fail_from_seg = fail_from_seg
        self.seen: list[int] = []
        self.chat = self
        self.completions = self

    def create(self, **kw):
        user = kw["messages"][1]["content"]
        ids = [int(m) for m in re.findall(r"^L(\d+):", user, re.M)]
        self.seen.extend(ids)
        if self.fail_from_seg is not None and ids and min(ids) >= self.fail_from_seg:
            # 模拟「这一窗彻底挂掉」：降档重试也失败 → 该窗不计入进度
            raise RuntimeError("这一窗的 LLM 彻底挂了")
        js = [it for it in self.judgments if it.get("seg") in ids]
        return _FakeResp(json.dumps({"judgments": js}))


def _ts(m: int) -> str:
    return f"**[{m:02d}:00]**"


def _run(td: pathlib.Path, text: str, client):
    src = td / "t.transcript.md"
    src.write_text(text, encoding="utf-8")
    return ct.run_file(src, td / "t.clean.md", td / "t.typos.json", client=client)


# 声学规律里有的音近写法。`Cloud Codex` / `chain box` 带空格，
# `clocko` / `cocoat` / `immunator` 是单 token —— 两种形态都要能扫到。
CLEAN_WITH_RESIDUAL = (
    f"{_ts(1)} 今天我们用 Cloud Codex 跑了一遍\n"
    f"{_ts(2)} 又试了 clocko 这个工具\n"
    f"{_ts(3)} 整体体验还不错\n"
)
CLEAN_CLEAN = (
    f"{_ts(1)} 今天我们用 Codex 跑了一遍\n"
    f"{_ts(2)} 又试了 Claude Code 这个工具\n"
    f"{_ts(3)} 整体体验还不错\n"
)


def _long_text(n_seg: int, pad: int = 2100) -> str:
    """造一份 > 120,000 字符的源文（必然多窗），前 3 段埋上残留候选。

    单窗上限 120000 是 `run_file` 走 `detect_typos` 的默认值，压不动 ——
    想验「部分窗失败」就必须造到真实规模那一档。
    """
    assert n_seg >= 4
    body = "这是正常内容没有音近候选。" * (pad // 12)
    return CLEAN_WITH_RESIDUAL + "".join(
        f"{_ts(m)} 第 {m} 段 {body}\n" for m in range(4, n_seg + 1))


# ── 1. 正例：clean 文本里确实有「能修而没修」的错字 ────────────

def test_模型没报的音近错字必须被算成残留():
    """**核心判据**：模型一处都没报，clean 文本里那 2 处候选就是残留。

    这条断言在「残留判定被短路成恒返回 0」时会红（见用例 6），
    所以它是整套指标里唯一证明「这个数字真的在动」的锚点。
    """
    with tempfile.TemporaryDirectory() as td:
        rep = _run(pathlib.Path(td), CLEAN_WITH_RESIDUAL, _ScriptedClient())
    assert rep["residual_count"] == 2, \
        f"应查出 2 处残留（Cloud Codex / clocko），实际 {rep['residual_count']}"
    assert rep["residual_rate"] > 0, "有残留却报 0 率 —— 又是「报 0 即说谎」"
    toks = [i["token"].lower() for i in rep["residual_items"]]
    assert any("codex" in t for t in toks), f"漏掉了 Cloud Codex，实得 {toks}"
    assert any("clocko" in t for t in toks), f"漏掉了 clocko，实得 {toks}"


# ── 2. 反例：干净文本里一处残留都没有 ────────────────────────

def test_干净文本不得被误报成有残留():
    """`Codex` / `Claude Code` 是**正确写法**，不能被音近扫描当成残留。

    反向证明这条扫描不是「见词就报」：判据必须真的在做匹配。
    """
    with tempfile.TemporaryDirectory() as td:
        rep = _run(pathlib.Path(td), CLEAN_CLEAN, _ScriptedClient())
    assert rep["residual_count"] == 0, \
        f"干净文本被误报 {rep['residual_count']} 处残留：{rep['residual_items']}"
    assert rep["residual_rate"] == 0.0


def test_词内多出的字母不算命中():
    """`clockout` 里含 `clocko` —— 不加词边界就会被误判成残留。"""
    text = (f"{_ts(1)} 这个功能是 clockout 模式\n"
            f"{_ts(2)} 和 Cloudflare 无关\n")
    with tempfile.TemporaryDirectory() as td:
        rep = _run(pathlib.Path(td), text, _ScriptedClient())
    assert rep["residual_count"] == 0, \
        f"`clockout` 被当成 `clocko` 误报了：{rep['residual_items']}"


# ── 3. 模型判过的位置不算残留 ──────────────────────────────────

@pytest.mark.parametrize("decision", ["keep", "uncertain"])
def test_模型判过keep或uncertain的位置不算残留(decision):
    """模型「看过并决定不改」是**它的判断**，不是漏改，不该记成残留。

    反过来算，等于把模型的正常决策全算成漏改 —— 数字会虚高一倍不止。
    """
    judgments = [{"seg": 1, "original": "Cloud Codex", "decision": decision,
                  "target": None, "evidence": "在讲另一个产品"},
                 {"seg": 2, "original": "clocko", "decision": decision,
                  "target": None, "evidence": "拿不准"}]
    with tempfile.TemporaryDirectory() as td:
        rep = _run(pathlib.Path(td), CLEAN_WITH_RESIDUAL,
                   _ScriptedClient(judgments))
    assert rep["residual_count"] == 0, \
        (f"模型已判 {decision} 的位置被算成残留了：{rep['residual_items']}"
         " —— keep/uncertain 是模型的决定，不是漏改")
    assert rep["decision_counts"].get(decision) == 2, \
        "前置造数有问题：这两条判断没真的送进 apply_judgments"


def test_邻域之外提过同一个词不算这一处被看过():
    """`NEIGHBOR` 段之外提过同一个词**不算**「这一处模型看过」。

    同一个词在全文里可能��现十几次，模型在第 30 段提过一次不代表第 2 段那处
    也被看过 —— 否则残留率会被系统性低估到接近 0，而这正是这个指标
    要抓的病（把「漏改」藏起来）。
    """
    judgments = [{"seg": 30, "original": "clocko", "decision": "keep",
                  "target": None, "evidence": "别处提过"}]
    text = CLEAN_WITH_RESIDUAL + "".join(
        f"{_ts(m)} 无关内容\n" for m in range(4, 31))
    with tempfile.TemporaryDirectory() as td:
        rep = _run(pathlib.Path(td), text, _ScriptedClient(judgments))
    assert rep["residual_count"] == 2, \
        (f"第 30 段的 keep 把第 1/2 段的残留也抹掉了，实得 {rep['residual_count']}"
         " —— 邻域容差（±%d）用得太宽，把别的段也算成「模型看过」" % ct.NEIGHBOR)


# ── 4. 没检测过就必须报「未检测」，不能报 0 ────────────────────

def test_没传client时残留率必须是None():
    """**不调 LLM** → 该字段报 None，**不是 0**。

    0 会被读成「一处残留都没有、全部改完」，而真实情况是「压根没查过」。
    这与 T1.14.2 修掉的 coverage / success_rate 兜底说谎是同一个坑。
    """
    with tempfile.TemporaryDirectory() as td:
        rep = _run(pathlib.Path(td), CLEAN_WITH_RESIDUAL, client=None)
    assert rep["residual_rate"] is None, \
        f"没调 LLM 却报了 {rep['residual_rate']} —— 0 会被读成「全部改完」"
    assert rep["residual_count"] is None, "residual_count 同样必须报 None"
    assert rep["residual_items"] is None, "residual_items 同样必须报 None"
    # ⚠️ 造数自证：这份文本里确实有 2 处候选，不是「本来就没东西可报」
    n, _ = ct.scan_residual_candidates(CLEAN_WITH_RESIDUAL, [], {1, 2, 3})
    assert n == 2, "前置造数有问题：文本里本该有 2 处候选"


def test_LLM全挂时残留率必须是None而不是0():
    """零窗成功 → 分母为 0，且 clean 文本原样保留（残留必然存在）。

    此时报 0 等于对着**一份完全没被检查过的文本**宣称「全部改完」——
    比没传 client 那条更严重，因为它看起来是「跑过了」。
    """
    with tempfile.TemporaryDirectory() as td:
        rep = _run(pathlib.Path(td), CLEAN_WITH_RESIDUAL,
                   _ScriptedClient(fail_from_seg=1))
    assert rep["detected_windows"] == 0, "前置：应一个窗都没跑成"
    assert rep["coverage"] == 0.0, "前置：覆盖率为 0"
    assert rep["residual_rate"] is None, \
        f"零窗成功却报了 {rep['residual_rate']} —— 对没检查过的文本宣称改完了"
    assert rep["residual_count"] is None


# ── 5. 分母必须是「已检测段数」，不是全文段数 ──────────────────

def test_分母必须是已检测段数():
    """后段检测失败 → 分母必须缩到**已检测段数**，不能按全文段数算。

    拿全文段数当分母等于把「压根没送检」当成「模型漏改了」，
    残留率会被系统性高估 —— 与 coverage / success_rate 当初的病同源。
    """
    with tempfile.TemporaryDirectory() as td:
        rep = _run(pathlib.Path(td), _long_text(60),
                   _ScriptedClient(fail_from_seg=10))
    assert rep["detected_windows_total"] > 1, "前置：需要多窗才验得到分母"
    assert 0 < rep["detected_segments"] < rep["segments"], \
        (f"前置：需要「部分窗失败」，实际 已检测 {rep['detected_segments']} / "
         f"全文 {rep['segments']}")
    assert rep["residual_count"] == 2, "前置：前 3 段埋了 2 处候选"
    # abs 容差：字段按 6 位小数落盘，比较时不能用默认的相对容差
    assert rep["residual_rate"] == pytest.approx(
        rep["residual_count"] / rep["detected_segments"], abs=5e-7), \
        (f"residual_rate={rep['residual_rate']} 不是 "
         f"residual_count/detected_segments={rep['detected_segments']}")
    assert rep["residual_rate"] != pytest.approx(
        rep["residual_count"] / rep["segments"], abs=5e-7), \
        (f"分母用的是全文段数（{rep['segments']}）—— "
         "没送检的段被算成「模型漏改」了")


def test_未送检段里的候选不算残留():
    """分子与分母必须落在**同一批段**上。

    候选出现在没送检的段里 → 那属于覆盖问题（看 `coverage`），
    不是残留问题。混进来会让这个指标在部分失败时凭空暴涨。
    """
    text = (f"{_ts(1)} 正常内容\n"
            f"{_ts(2)} 正常内容\n"
            f"{_ts(3)} 正常内容\n"
            f"{_ts(4)} 这里提到了一个 Cloud Codex\n")
    n_all, _ = ct.scan_residual_candidates(text, [], {1, 2, 3, 4})
    n_det, _ = ct.scan_residual_candidates(text, [], {1, 2, 3})
    assert n_all == 1, "前置：全扫应得 1 处"
    assert n_det == 0, \
        "只扫已检测段时不该数到未送检段里的候选 —— 分子分母口径不一致"


# ── 6. 反向断言：候选扫描被短路时，用例 1 一定会红 ─────────────

def test_把残留判定短路成恒零后报告会跟着说零(monkeypatch):
    """**反向断言**：证明用例 1 真的在检查候选扫描，而不是恒真。

    把扫描换成恒返回 0（等价于「残留判定被短路」），
    同一份输入的 `residual_count` 必须从 2 掉到 0 ——
    即用例 1 的 `== 2` 会红。
    """
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        good = _run(td, CLEAN_WITH_RESIDUAL, _ScriptedClient())
    assert good["residual_count"] == 2, "前置：候选扫描本该扫出 2 处"

    monkeypatch.setattr(ct, "scan_residual_candidates",
                        lambda *a, **k: (0, []))
    with tempfile.TemporaryDirectory() as td:
        shorted = _run(pathlib.Path(td), CLEAN_WITH_RESIDUAL, _ScriptedClient())
    assert shorted["residual_count"] == 0, \
        "扫描短路后报告仍报 2 —— 报告字段没接在扫描上，这套断言是假绿"
    assert shorted["residual_rate"] == 0.0
    assert shorted["residual_items"] == []
    # 明确写下这条不变式：两个分支结果必须不同，否则用例 1 恒真。
    assert good["residual_count"] != shorted["residual_count"], \
        "开/关扫描结果相同 —— 说明用例 1 证明不了任何东西"


# ── 7. 词表与提示词必须对得上（两份会漂移的地方） ─────────────

def test_候选词表必须逐条出自声学规律提示词():
    """`ACOUSTIC_SUSPECTS` 是 `ACOUSTIC_HINTS` 的可执行副本。

    提示词里删了/加了一条规律而这里没跟着改，两个来源就此漂移 ——
    指标会悄悄少扫一类错字，而**没有任何报错**。
    对拍：把每个候选的字母重新拼回去，必须能在提示词里找到。
    """
    hints = re.sub(r"[\s`]+", "", ct.ACOUSTIC_HINTS).lower()
    for parts in ct.ACOUSTIC_SUSPECTS:
        spelled = "".join(parts)
        assert spelled in hints, \
            (f"候选 {spelled!r} 在 ACOUSTIC_HINTS 里找不到 —— "
             "词表与提示词漂移了，指标会悄悄漏扫这一类")


def test_报告必须带证据不能只给数字():
    """有残留时 `residual_items` 必须能定位到段号与原文词。

    这是本指标「宁可报不出也不能乱报」的对价：**数字必须配证据**，
    人要能逐条核，否则误报（`chain box` / `immunator` 可能是真词）
    就没法证伪。
    """
    with tempfile.TemporaryDirectory() as td:
        rep = _run(pathlib.Path(td), CLEAN_WITH_RESIDUAL, _ScriptedClient())
    assert rep["residual_items"], "有残留却没给证据，无法人工复核"
    for it in rep["residual_items"]:
        assert it["seg"] >= 1, f"证据缺段号：{it}"
        assert it["token"].strip(), f"证据缺原文：{it}"
    assert rep["residual_count"] >= len(rep["residual_items"]), \
        "证据条数不应多于残留处数"


# ── 8. 正常路径不被这次改动弄坏 ───────────────────────────────

def test_正常纠错时残留为零():
    """模型报的错字被真的改掉 → clean 文本里不该再扫出候选。

    防止「替换没落成但报告说落成」那类回归：文本没真改，指标就该抓到。
    """
    text = f"{_ts(1)} 讲的是 Cloud Codex 这个东西\n{_ts(2)} 收尾\n"
    judgments = [{"seg": 1, "original": "Cloud Codex", "decision": "replace",
                  "target": "Codex", "evidence": "上下文在讲 Codex"}]
    with tempfile.TemporaryDirectory() as td:
        rep = _run(pathlib.Path(td), text, _ScriptedClient(judgments))
    assert rep["applied_count"] == 1, "前置：替换必须真的落上"
    assert rep["residual_count"] == 0, \
        f"已改掉的词又被算成残留：{rep['residual_items']}"
    assert rep["residual_rate"] == 0.0
