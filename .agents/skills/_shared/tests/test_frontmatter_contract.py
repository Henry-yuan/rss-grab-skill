"""frontmatter 序列化的回归测试（V1.0，2026-10-05）。

抓的是真实故障：某源 `info.json` 的 `desc` 字面就是一个短横线 `-`，
原 `_scalar` 只查「含不含 `: # " ' [ ] { }`」和首尾空白，**漏了行首指示符**，
写出 `description: -`，YAML 读回时把 `-` 当序列项起始符，
**整份 frontmatter 解析失败**——笔记正文再好也读不出来。

判据统一用「dump 出去再 yaml 读回来，必须等于原值」，
这比逐条断言转义写法更贴近真正的使用方式。
"""
from __future__ import annotations

import pathlib
import sys

import pytest
import yaml

_SHARED = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SHARED))

from frontmatter_contract import FIELDS, _scalar, check, dump, normalize  # noqa: E402
import frontmatter_contract  # noqa: E402  （测 PyYAML 缺失时的兜底）


@pytest.mark.parametrize("raw", [
    "-",                       # 真实踩坑：字面短横线
    "- 标题",
    "? key", "? 问号开头",
    "&anchor", "*alias", "!tag",
    "|block", ">fold", "%dir", "@at", "`tick",
    "#hash", "#FDE[话题]# #AI[话题]#",
    "[bracket", "{brace", ": colon",
    "true", "false", "null", "yes", "no", "on", "off", "~",
    "123", "1.5", "1e3", "-42",     # 纯数字串会被 YAML 读成 int/float
    "  首尾空格  ", "a: b", "a # 注释",
    "带\n换行", "带\r\n回车换行",
    "", "正常中文不带任何特殊字符",
    # ── YAML 1.1 隐式类型：逐个补类型是打地鼠，这里锁死「判据」而非清单 ──
    "2026-10-05",                      # date（真实踩过：94 篇 published_at）
    "2026-03-25",                      # date
    "2026-10-05T12:00:00+08:00",        # timestamp
    "0b1010",                          # 二进制 int
    "0x1f",                            # 十六进制 int
    "0o17",                            # 八进制（YAML 1.1 读成 str，仍锁住不回归）
    "1_000",                           # 下划线分隔 int
    "0.0_1",                           # 下划线分隔 float
    ".5",                              # 无前导 0 的 float
    "1.", "0.",                        # 尾部小数点
    "+12", "-0", "00", "007",           # 符号 / 前导零
    "1:08:29",                         # 六十进制 int —— 时长字段天然是这个形状
    "12:30",
    ".inf", "-.inf", ".nan", ".Inf",    # 特殊 float
    "TRUE", "False", "Yes", "No", "Null", "NULL",   # 大小写变体
    "0bfdc2dc",                        # 十六进制形态的 id，但 YAML 读回仍是 str
    "217a6492", "64843451",            # 真实撞过的全数字 hash8
    "1000000000",                      # ⚠️ 合成的全数字值（非真实数据）
    #   原先这里放的是另一个平台的**真实账号标识**（不复写，见 git 历史）；
    #   2026-10-06 换成明显假的合成值 —— 公开仓不放生产数据。
    "0001-01-01",                      # 极端 date
])
def test_标量往返不丢信息(raw):
    """dump 出去再 yaml 读回来，必须严格等于原值（含类型）。

    例外：`\r\n` / `\r` 会被归一成 `\n` —— 这是刻意的，换行必须压成单行，
    否则写出来的 YAML 会跨行（`---` 一旦出现在值里，正文会被整段吞掉）。
    """
    back = yaml.safe_load("k: " + _scalar(raw))["k"]
    expected = raw.replace("\r\n", "\n").replace("\r", "\n")
    assert back == expected, f"{raw!r} 往返后变成 {back!r}"
    assert type(back) is type(expected), (
        f"{raw!r} 往返后类型从 {type(expected).__name__} 变成 {type(back).__name__}")


@pytest.mark.parametrize("raw", [
    "2026-10-05", "0b1010", "1_000", ".5", "1:08:29",
    "64843451", "1000000000", "2026-10-05T12:00:00+08:00",
])
def test_隐式类型必须加引号(raw):
    """裸写会被读成非 str 的值，一律加引号 —— 类型被篡改比多两个引号严重得多。

    注意 `0bfdc2dc` / `217a6492` 这类**含字母**的十六进制不在此列：
    YAML 把它们读回成 str，裸写是安全的（由 test_标量往返不丢信息 覆盖）。
    真正会出事的是**全数字**的 id —— 实测撞过 3 篇 note_id、11 篇 uploader_id。
    """
    assert _scalar(raw).startswith('"'), f"{raw!r} 没加引号，会被读成非字符串"


def test_正常中文不被过度引号():
    """反过来也要锁：普通标题不该变成 `"普通标题"`（可读性与体积）。"""
    for raw in ("正常中文标题", "OpenClaw 之后，谁将定义主动式 AI 的新战场？",
                "AI 产品经理的成长路径", "v1.2.3", "第一章"):
        assert _scalar(raw) == raw, f"{raw!r} 被过度加引号了"


def test_无_pyyaml_时兜底仍保守():
    """PyYAML 缺失时（dump 能在无 yaml 的解释器上跑）不得放宽。"""
    saved, frontmatter_contract._yaml = frontmatter_contract._yaml, None
    try:
        assert frontmatter_contract._resolves_to_str("2026-10-05") is False
        assert frontmatter_contract._resolves_to_str("0b1010") is False
        assert frontmatter_contract._resolves_to_str("正常中文标题") is True
    finally:
        frontmatter_contract._yaml = saved


def test_真实故障复现():
    """source 的 desc 就是 '-' 这件事本身要能复现。"""
    fm = normalize({"description": "-", "title": "正常标题"})
    text = dump(fm)
    parsed = yaml.safe_load(text.strip().strip("-"))
    assert parsed["description"] == "-"
    assert parsed["title"] == "正常标题"
    assert check(fm) == []


def test_列表字段统一为_list():
    """空值也要写 []，不能是 ''，否则 yaml 读回类型不一致。"""
    fm = normalize({"tags": "", "source_tags": None})
    assert fm["tags"] == [] and fm["source_tags"] == []
    parsed = yaml.safe_load(dump(fm).strip().strip("-"))
    assert parsed["tags"] == [] and parsed["source_tags"] == []


def test_列表字段收非空字符串不碎成字符():
    """真实隐患：`list("AI")` == ['A','I']，一个标签会变成两个。"""
    assert normalize({"source_tags": "AI"})["source_tags"] == ["AI"]
    assert normalize({"tags": "AI, 技术"})["tags"] == ["AI, 技术"]
    # 且写盘后读回仍是同一个 list
    parsed = yaml.safe_load(dump(normalize({"tags": "AI, 技术"})).strip().strip("-"))
    assert parsed["tags"] == ["AI, 技术"]


def test_整份_frontmatter_往返_类型全对():
    """最终判据：整份 dump → yaml 读回，17 个字段的类型与值都等于原值。

    真实踩过的 94 篇 `published_at`（date）、4 篇 `note_id`（int）、
    11 篇 `uploader_id`（int）都是这个断言能拦住的。
    """
    fm = {
        "note_id": "0bfdc2dc", "title": "正常中文标题",
        "uploader_id": "1000000000", "type": "podcast",
        "duration": "1:08:29", "published_at": "2026-10-05",
        "source_tags": [], "tags": ["AI", "2026-10-05"],
        "summary": "摘要", "generated_at": "2026-10-05T09:00:00+08:00",
        "description": "描述",
    }
    parsed = yaml.safe_load(dump(fm).strip().strip("-"))
    for k in FIELDS:
        want = normalize(fm)[k]
        assert type(parsed[k]) is type(want), (
            f"{k}: 读回是 {type(parsed[k]).__name__}，原值是 {type(want).__name__}")
        assert parsed[k] == want, f"{k}: {parsed[k]!r} != {want!r}"


def test_字段顺序按契约():
    keys = [l.split(":", 1)[0] for l in dump(normalize({})).split("\n")[1:-1]]
    assert keys == list(FIELDS)
    assert len(FIELDS) == 17


def test_废弃字段被_check_拦下():
    assert check({"asr_status": "ok"}) != []
    assert check({"title": "x"}) != []          # 缺 16 个
    assert check({k: "" for k in FIELDS}) == []
