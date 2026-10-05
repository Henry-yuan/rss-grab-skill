"""correct_typos 共用逻辑回归测试（V1.0，2026-10-04）。

抓的是真实故障：某篇长文 2707 -> 2706 行，assert_structure_preserved 抛错，
整篇不生成。根因是模型报的 original 比原文长（带 ** 或多余标点），
`a0 + len(orig_raw)` 越过匹配点吞掉换行。
"""
from __future__ import annotations

import json
import pathlib
import re
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


"""超长视频（>99 分钟）的时间戳分段回归测试（V1.0，2026-10-05）。

真实故障：5 篇超长视频（时长 2h24m ~ 5h25m）的分钟数超过 99 后，
`correct_typos` 里的时间戳正则 `\\d{1,2}:\\d{2}` **匹配不上**
`[144:23]` / `[227:41]` / `[325:41]`，于是 `has_ts` 判定失败、
`split_segments` 退化成按空行切，段结构与模型预期完全错位。

后果不是报错，是**静默丢弃**：模型把时间戳数字当成段号报回来
（如把 `[152:00]` 报成「第 15200 段」），程序判越界拒绝，
**76 条本来能修的纠错被扔掉**。最严重的一篇（3h47m）段数从正确的
5,623 退化成 2,490，纠错成功率只剩 59.6%。

⚠️ 本文件在主项目与 opensource 各有一份，**全文不得出现任何具体源的名字或 ID**
（opensource 的守门测试会拦）。举例一律用时间戳本身。
"""

import pathlib
import sys

import pytest

_SHARED = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SHARED))

import correct_typos as ct  # noqa: E402


def _ts(minutes: int) -> str:
    return f"**[{minutes}:{minutes % 60:02d}]**"


@pytest.mark.parametrize("minutes", [1, 9, 59, 99, 100, 144, 227, 325])
def test_超长视频时间戳仍被识别为段首(minutes):
    """分钟数 1~3 位都要认。少了 3 位就是上面那场静默丢弃事故。"""
    text = "\n".join(f"{_ts(m)} 这是第 {m} 分钟的内容 ClodFlare 写错了" for m in range(1, 5))
    assert ct.TS_MARK.search(text) is not None, f"[{minutes}:..] 没被认成时间戳"
    segs = ct.split_segments(text)
    assert len(segs) == 4, f"应切成 4 段，实得 {len(segs)}"


def test_超长视频按时间戳切段而不是退化成按空行():
    a, b, c = _ts(144), _ts(227), _ts(325)
    text = f"{a} 第一段 ClodFlare\n{b} 第二段 ChatGPT\n{c} 第三段 Anthropic"
    segs = ct.split_segments(text)
    assert len(segs) == 3
    # 段首标记必须被 SEG_LEAD 吃掉，不能混进正文（否则归一化串会带上时间戳数字）
    for s in segs:
        assert "[" not in s.split("**")[0]


def test_三位分钟的时间戳不进归一化正文():
    """时间戳剥干净后，段内不该残留 14423 这类数字。"""
    text = f"{_ts(144)} ClodFlare 很好用"
    segs, norms, _ = ct.build_index(text)
    assert "144" not in norms[0]
    assert "clodflare" in norms[0]


def test_结构校验能数到超长视频的时间戳():
    """TS_LINE 同样要认 3 位分钟，否则 assert 比的是空对空，恒过。"""
    before = f"{_ts(144)} 甲\n{_ts(325)} 乙"
    after = f"{_ts(144)} 甲改\n{_ts(325)} 乙"
    ct.assert_structure_preserved(before, after)   # 不抛即通过
    # 真的少了一个时间戳时必须报错（证明它不是空对空）
    with pytest.raises(AssertionError):
        ct.assert_structure_preserved(before, f"{_ts(144)} 甲改\n乙")


# ── 缺陷：detect_typos 静默截断（2026-10-05）────────────────────────
#
# 旧实现是 `split_segments(text[:max_chars])`：超出 max_chars 的尾部**直接丢掉
# 且不报错**，而 run_file 的报告 `segments` 仍按全文记 —— 看上去像全篇都跑过。
# 实测 5 小时播客（285,809 字符 / 12,150 段）只有前 120,000 字符进过纠错，
# **58% 的内容从未被检查**。现在按段边界分窗覆盖全文。


class _FakeMsg:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMsg(content)


class _FakeResp:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _RecordingClient:
    """记录每次调用收到的段列表；对 `judge_seg` 号那一段回一条 replace。

    不发真实网络请求，但**真的走完** `_call -> _parse_json -> judgments` 全链路，
    因此能证明分窗逻辑确实把每一段都送进了模型。
    """

    def __init__(self, judge_seg: int = 1):
        self.seen: list[list[int]] = []
        self.judge_seg = judge_seg
        self.chat = self
        self.completions = self

    def create(self, **kw):
        user = kw["messages"][1]["content"]
        ids = [int(m) for m in re.findall(r"^L(\d+):", user, re.M)]
        self.seen.append(ids)
        js = [{"seg": i, "decision": "replace",
               "original": "ClodFlare", "target": "Cloudflare"}
              for i in ids if i == self.judge_seg]
        return _FakeResp(json.dumps({"judgments": js}))

    @property
    def covered(self) -> set[int]:
        return {i for ids in self.seen for i in ids}


def _long_transcript(n_seg: int = 40) -> str:
    return "".join(f"{_ts(m)} 第 {m} 段 ClodFlare 讨论\n" for m in range(1, n_seg + 1))


def test_超长源文分窗覆盖全文而不是静默截断():
    """每一段都必须进过模型。旧实现只送前 max_chars 字符，这一条会红。"""
    text = _long_transcript(40)
    segs = ct.split_segments(text)
    assert len(segs) == 40
    # 把单窗上限压到很小，模拟 5 小时播客那种「装不下」的情形
    c = _RecordingClient(judge_seg=37)          # 37 落在**尾部**
    items = ct.detect_typos(text, c, max_chars=200)

    assert c.covered == set(range(1, 41)), \
        f"只有 {len(c.covered)}/40 段进过检测，尾部被静默丢弃了"
    assert len(c.seen) > 1, "源文超单窗上限却只发了一次调用 = 没分窗"
    # 尾部那一段的判断必须能原样取回（段号是全局编号，合并时不得重映射）
    assert any(it["seg"] == 37 for it in items), \
        f"尾部第 37 段的判断丢了，实得 {[it['seg'] for it in items]}"


def test_未超单窗上限时只发一次调用():
    """短文（绝大多数）不该被无谓地拆成多窗。"""
    c = _RecordingClient(judge_seg=2)
    ct.detect_typos(_long_transcript(4), c, max_chars=200)
    assert len(c.seen) == 1, f"短文被拆成 {len(c.seen)} 窗，白白多花 {len(c.seen)-1} 次调用"


def test_run_file_有client时不再NameError():
    """2026-10-05：run_file 的 seed 参数已在清死代码时删除，调用处却还在传
    `seed=seed` → 有 client 时必然 NameError。之所以没被发现：现有单测全部走
    `client=None` 分支，这行永远不执行。"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        src = td / "t.transcript.md"
        src.write_text(_long_transcript(4), encoding="utf-8")
        rep = ct.run_file(src, td / "t.clean.md", td / "t.typos.json",
                          client=_RecordingClient(judge_seg=2))
    assert rep["segments"] == 4
    assert rep["applied_count"] == 1, "替换没落上"


def test_报告如实记录覆盖率():
    """报告必须能看出实际送检了多少段，不能只报全文段数。

    用**真实规模**的文本（> 12 万字符，即 5 小时播客那一档），
    因为 run_file 走的是 detect_typos 的默认单窗上限，压不动。
    """
    import tempfile
    n = 5000                       # ≈15 万字符，必然超过 120,000 单窗上限
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        src = td / "t.transcript.md"
        src.write_text(_long_transcript(n), encoding="utf-8")
        rep = ct.run_file(src, td / "t.clean.md", td / "t.typos.json",
                          client=_RecordingClient())
    assert rep["detected_windows"] > 1, "这么长的文本却只跑了一窗，分窗没生效"
    assert rep["detected_segments"] == rep["segments"], \
        f"送检 {rep['detected_segments']} 段 / 全文 {rep['segments']} 段，报告口径不一致"
    assert rep["coverage"] == 1.0, \
        f"覆盖率 {rep['coverage']} —— 仍有内容没进过纠错"
