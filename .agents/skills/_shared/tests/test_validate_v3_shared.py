"""校验失败原因**携带 + 回灌**回归测试（2026-10-05）。

抓的是两个已上生产的缺陷，症状都是「校验失败后重试是盲赌」：

1. `validate()` 的 `retries_needed` **只从 `rep["C3_sections"]["error"]` 取值**，
   而「summary 为空」那条失败把原因写在了 `rep["C11_summary"]` 下
   → 真实失败原因丢失，编排层只拿到 `['unknown']`。
   附带问题：两条 C3 检查写同一个键，后一条会**覆盖**前一条。

2. `build_v3.generate_note_body()` 的重试循环**同参数盲重试**：
   `retries_needed` 只进了 print 字符串，从未回灌模型
   → 连着 3 次同样参数、同样失败。
   而 `validate()` 的 docstring 恰恰承诺「编排层应带错误信息重试」。

⚠️ 本文件在主项目与 opensource 各有一份，**全文不得出现任何具体源的名字或 ID**
（opensource 的守门测试会拦）。举例一律用中性的主题文本。
"""
from __future__ import annotations

import pathlib
import re
import sys
from types import SimpleNamespace

import pytest

_SHARED = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SHARED))

import build_v3  # noqa: E402
import validate_v3  # noqa: E402

INFO = {"tags": ["标签一", "标签二"]}

SOURCE = (
    "今天我们聊一个具体问题：如何把长篇转写整理成结构化笔记。\n"
    "第一个要点是保留原始信息，不要为了精简而合并不同的观点。\n"
    "第二个要点是数字精度必须原样保留，不能四舍五入。\n"
    "第三个要点是专有名词要照抄原文，不要自行改写。\n"
    "第四个要点是因果关系不能拆开来看，限定条件也不能丢。\n"
    "第五个要点是对比项要成对出现，缺一半就会误导读者。\n"
    "第六个要点是结论与前提要绑在一起写，不能只留结论。\n"
    "第七个要点是引用要能回源定位，否则读者无法核实。\n"
)

GOOD_BODY = (
    "## TL;DR\n\n"
    "1. 整理长篇转写的第一原则是信息量不得低于输入\n"
    "2. 数字精度、专有名词、因果关系三类信息必须原样保留\n\n"
    "## 关键引用\n\n"
    "- 信息量不得低于输入，这一条是所有其他规则的来源\n"
    "- 数字精度必须原样保留，不能四舍五入\n"
)

GOOD_JSON = '{"summary": "整理转写要保真不要精简", "tags": ["标签一", "标签二"]}'
GOOD_OUTPUT = f"{GOOD_BODY}\n---JSON---\n{GOOD_JSON}"


def _run(body: str, llm: dict | None = None) -> dict:
    """跑一次 validate()，llm 默认给一份能过的 JSON。"""
    payload = {"summary": "整理转写要保真不要精简",
               "tags": ["标签一", "标签二"]}
    if llm is not None:
        payload = llm
    return validate_v3.validate(body, SOURCE, INFO, payload)


# ── 缺陷 1：retries_needed 携带真实原因 ────────────────
def test_summary为空时原因不再退化成_unknown():
    """真实生产故障点：原因写在 C11_summary 下，取值时读的是 C3_sections。"""
    v = _run(GOOD_BODY, {"tags": ["标签一"], "summary": ""})
    assert v["ok"] is False
    assert v["retries_needed"] == ["summary 为空"]
    assert "unknown" not in v["retries_needed"]


@pytest.mark.parametrize("body,expect", [
    # 缺硬章节 → C3 第一条
    ("## 要点总结\n\n只有正文，没有两个硬章节。\n",
     "缺少 TL;DR 或 关键引用"),
    ("## TL;DR\n\n1. 只有 TL;DR\n",
     "缺少 TL;DR 或 关键引用"),
    ("## 关键引用\n\n- 只有引用\n",
     "缺少 TL;DR 或 关键引用"),
    # 结构齐全、只有 summary 空
    (GOOD_BODY, "summary 为空"),
])
def test_每条失败路径都拿得到自己的原因(body, expect):
    """逐一覆盖全部阻断分支，任何一条退回 'unknown' 都算回归。"""
    v = _run(body, {"summary": "", "tags": []})
    assert v["ok"] is False, "该分支本应判失败"
    assert expect in v["retries_needed"], \
        f"期望拿到「{expect}」，实得 {v['retries_needed']}"


def test_章节整段缺失时不再顺手报顺序错():
    """2026-10-05 顺序校验改严后同步的预期。

    旧实现用正则 `## TL;DR[\\s\\S]*?(## 关键引用|$)`，TL;DR 整段缺失时正则匹配不上，
    于是「顺序异常」也会跟着报一条。**那是坏正则的副产品，不是有意义的信息**：
    真正的问题是「章节缺失」，再补一句「顺序异常」只会误导重试（模型可能去调顺序，
    而把仍然缺着的章节原样留着）。现在顺序只在**都存在的**章节之间比对，
    缺失由上一条 C3 用一条清晰原因登记。
    """
    v = _run("## 关键引用\n\n- 只有引用\n", {"summary": "有摘要", "tags": []})
    assert "缺少 TL;DR 或 关键引用" in v["retries_needed"]
    assert "顺序异常" not in v["retries_needed"]


def test_两个硬章节顺序反了必须被拦下():
    r"""回归：2026-10-05 之前这条检查**永远绿**，顺序颠倒一条都抓不到。

    旧正则的 `$` 没有 re.MULTILINE，匹配的是串尾；只要 `## TL;DR` 存在，
    `[\s\S]*?` 一定能一路走到串尾 → 必然匹配成功 → 顺序反了照样通过。
    现在改成取实际 H2 序列逐位比对，本用例是它的判据：
    **在旧实现上 `ok` 为 True、在新实现上为 False**（见交付报告的对照实测）。
    """
    v = _run("## 关键引用\n\n- 先写引用\n\n## TL;DR\n\n1. 后写 TL;DR\n\n正文\n",
             {"summary": "有摘要", "tags": []})
    assert "缺少 TL;DR 或 关键引用" not in v["retries_needed"]
    assert v["ok"] is False, "顺序反了仍判通过 = 顺序检查又退化成恒真命题"
    assert any("顺序异常" in r for r in v["retries_needed"]), \
        f"应登记顺序原因，实得 {v['retries_needed']}"


def test_多条原因同时被收集而不是互相覆盖():
    """两条 C3 检查写同一个 report 键会覆盖，但原因清单必须都在。"""
    v = _run("## 要点总结\n\n只有正文。\n", {"summary": "", "tags": []})
    assert v["ok"] is False
    for r in ("缺少 TL;DR 或 关键引用", "summary 为空"):
        assert r in v["retries_needed"], f"「{r}」丢了，实得 {v['retries_needed']}"
    assert "unknown" not in v["retries_needed"]
    # 报告本身的既有行为不变（单键、后写覆盖），下游读 report 不受影响
    assert v["report"]["C3_sections"]["error"] == "缺少 TL;DR 或 关键引用"


def test_通过时原因清单为空():
    v = _run(GOOD_BODY)
    assert v["ok"] is True
    assert v["retries_needed"] == []


def test_置假只发生在_fail_里():
    """防漂移：新增阻断检查若绕过 fail()，原因又会从 retries_needed 丢失。"""
    src = (_SHARED / "validate_v3.py").read_text(encoding="utf-8")
    hits = [ln.strip() for ln in src.splitlines()
            if re.search(r"^\s*ok\s*=\s*False\s*$", ln)]
    assert len(hits) == 1, \
        f"validate() 内 ok=False 应只在 fail() 里出现 1 次，实得 {len(hits)}：{hits}"


# ── 缺陷 2：重试时把原因回灌给模型 ──────────────────────
def test_附加指令只含失败原因():
    assert build_v3.build_retry_hint([]) == ""
    assert build_v3.build_retry_hint(None) == ""
    assert build_v3.build_retry_hint(["  "]) == ""
    h = build_v3.build_retry_hint(["summary 为空", "缺少 TL;DR 或 关键引用"])
    assert "summary 为空" in h and "缺少 TL;DR 或 关键引用" in h


def test_两条调用路径都能接收附加指令():
    """长文路径与短文路径必须同口径，否则长文篇仍在盲赌。"""
    import inspect
    for fn in (build_v3.call_llm, build_v3.call_llm_topics):
        assert "retry_hint" in inspect.signature(fn).parameters, fn.__name__


class _FakeClient:
    """记录每次 create() 的入参，回放固定输出。"""

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kw):
        self.calls.append(kw)
        idx = min(len(self.calls) - 1, len(self._outputs) - 1)
        msg = SimpleNamespace(content=self._outputs[idx])
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    def user_text(self, i: int = 0) -> str:
        return self.calls[i]["messages"][1]["content"]


def test_重试时prompt确实带上了失败原因(monkeypatch, tmp_path):
    """核心验收：第二次调用的 user 消息里必须出现第一次的真实失败原因。"""
    import clean_source
    import correct_typos

    src_path = tmp_path / "demo.transcript.md"
    src_path.write_text(SOURCE, encoding="utf-8")
    work_dir = tmp_path / "work"
    typos_dir = tmp_path / "typos"
    work_dir.mkdir()
    typos_dir.mkdir()

    monkeypatch.setattr(clean_source, "load_and_clean",
                        lambda p: (SOURCE, {"chars": len(SOURCE)}))

    def _fake_run_file(_src, clean_out, _typos_json, client=None):
        pathlib.Path(clean_out).write_text(SOURCE, encoding="utf-8")
        return {"applied_count": 0, "success_rate": 1.0}

    monkeypatch.setattr(correct_typos, "run_file", _fake_run_file)

    real_validate = validate_v3.validate
    seen = {"n": 0}

    def _flaky(*a, **kw):
        """第 1 次模拟「summary 为空」失败，第 2 次放行。"""
        seen["n"] += 1
        v = real_validate(*a, **kw)
        if seen["n"] == 1:
            v["ok"] = False
            v["retries_needed"] = ["summary 为空"]
        return v

    monkeypatch.setattr(validate_v3, "validate", _flaky)

    client = _FakeClient([GOOD_OUTPUT, GOOD_OUTPUT])
    body, _report, stats = build_v3.generate_note_body(
        src_path, {"description": "示例"}, client, work_dir, typos_dir)

    assert len(client.calls) == 2, f"应重试一次，实调 {len(client.calls)} 次"
    assert stats["attempts"] == 2
    # 首轮没有附加指令（正常路径不应多此一举）
    assert "summary 为空" not in client.user_text(0)
    # 关键断言：第二轮带上了真实原因
    assert "summary 为空" in client.user_text(1)
    assert stats["last_retry_hint"] == client.user_text(1).rsplit("\n\n", 1)[-1]
    assert body.startswith("## TL;DR")


def test_附加指令不撑大prompt():
    """实测长度影响：按生产量级的源文算，prompt 只该多出附加指令本身那点字数。

    用短源文测会把占比放大到失真（模板才 200 字），故这里用长文量级的源文。
    """
    long_source = "这是一段用于测量的转写正文，保留原始信息不被精简。" * 400  # ~11.9K
    plain_client = _FakeClient([GOOD_OUTPUT])
    build_v3.call_llm(plain_client, long_source, {"description": "示例"},
                      heartbeat=False)
    plain = len(plain_client.user_text(0))

    hint = build_v3.build_retry_hint(["summary 为空", "缺少 TL;DR 或 关键引用"])
    fed_client = _FakeClient([GOOD_OUTPUT])
    build_v3.call_llm(fed_client, long_source, {"description": "示例"},
                      heartbeat=False, retry_hint=hint)
    fed = len(fed_client.user_text(0))

    assert fed - plain == len(hint) + 2, "除了附加指令和两个换行，prompt 不得有别的变化"
    assert len(hint) <= 120, "附加指令本身也要克制"
    assert (fed - plain) / plain < 0.01, "附加指令占比须 < 1%，避免稀释约束"
    print(f"\n[长度实测] 附加指令 {len(hint)} 字符 / prompt {plain} 字符 "
          f"= {(fed - plain) / plain:.3%}")
