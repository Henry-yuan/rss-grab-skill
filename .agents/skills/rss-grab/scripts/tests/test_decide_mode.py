#!/usr/bin/env python3
"""decide_mode 单测：长文判定（rss 版，口径 2026-10-04 改为净字 > 15,000）。

改口径前的测试用的是「原始字符 ≥ 50,000」，且测试数据没有时间戳 ——
于是 net_len == len(text)，旧断言在新阈值下**碰巧仍然通过**，
等于没有测到口径变化本身。故这里显式引入带时间戳的样本。
"""
import pathlib
import sys
import tempfile

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))
import decide_mode as d  # noqa: E402


def _write(td: pathlib.Path, text: str) -> pathlib.Path:
    t = td / "测试-f35eb04d.transcript.md"
    t.write_text(text, encoding="utf-8")
    return t


def test_short_transcript_skill_mode():
    """净字 10,000 ≤ 15,000 → skill。"""
    with tempfile.TemporaryDirectory() as td:
        t = _write(pathlib.Path(td), "字" * 10000)
        mode, reason = d.decide(t)
        assert mode == "skill"
        assert "10000" in reason


def test_boundary_exactly_15000_is_short():
    """净字恰为 15,000 → 判短文（严格大于，与 build_v3.py 的 > THRESHOLD 对齐）。"""
    with tempfile.TemporaryDirectory() as td:
        t = _write(pathlib.Path(td), "字" * 15000)
        mode, reason = d.decide(t)
        assert mode == "skill"
        assert "15000" in reason


def test_boundary_15001_is_long():
    """净字 15,001 → 判长文。"""
    with tempfile.TemporaryDirectory() as td:
        t = _write(pathlib.Path(td), "字" * 15001)
        mode, reason = d.decide(t)
        assert mode == "map_reduce"
        assert "15001" in reason


def test_timestamps_are_stripped_before_judging():
    """核心回归：原始字符远超阈值、但净字不到 → 必须判短文。

    这是旧口径判错的方式 —— 2026-10-04 实测 48 篇 rss 里有 24 篇
    （净字 15,000–25,000、原始字符 29,732–49,433）被卡在 50,000 线下方。
    """
    with tempfile.TemporaryDirectory() as td:
        # 6000 段 × 每段 1 个正文字：原始字符约 8.4 万，净字仅 6000
        # 注意 `% 1000`：分钟超过 999 会变成 4 位，_TS_STRIP 按设计不匹配
        # （4 位分钟不是合法 ASR 标记），造数据时必须取模，否则净字会虚高。
        text = "".join(f"**[{i % 1000:03d}:00]** 短\n" for i in range(6000))
        t = _write(pathlib.Path(td), text)
        assert len(text) > 50000          # 旧口径会判 map_reduce
        mode, reason = d.decide(t)
        assert mode == "skill"
        assert "6000" in reason


def test_real_regime_flip():
    """复现 A6 预审里被误判的那一篇：净字 > 15,000、原始字符 < 50,000 → 现在判长文。"""
    with tempfile.TemporaryDirectory() as td:
        # 18000 段 × 1 字 = 净字 18000；每段标记 12 字符 → 原始约 23 万
        text = "".join(f"**[{i % 1000:03d}:00]** 字\n" for i in range(18000))
        t = _write(pathlib.Path(td), text)
        mode, _ = d.decide(t)
        assert mode == "map_reduce"


def test_extract_hash8_from_path():
    """从 transcript 文件名提取 guid_hash8。"""
    p = pathlib.Path("rss/transcripts/独树不成林-361-f35eb04d.transcript.md")
    assert d.extract_hash8_from_path(p) == "f35eb04d"


def test_cli_exit_code_contract():
    """exit code: 0 = skill, 1 = map_reduce —— regenerate_note.py 依赖这个约定。"""
    import subprocess

    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        short = td / "短-f35eb04d.transcript.md"
        short.write_text("字" * 100, encoding="utf-8")
        r = subprocess.run([sys.executable, str(SCRIPT_DIR / "decide_mode.py"), str(short)],
                           capture_output=True, text=True)
        assert r.returncode == 0 and r.stdout.startswith("skill")

        long_ = td / "长-f35eb04d.transcript.md"
        long_.write_text("字" * 20000, encoding="utf-8")
        r = subprocess.run([sys.executable, str(SCRIPT_DIR / "decide_mode.py"), str(long_)],
                           capture_output=True, text=True)
        assert r.returncode == 1 and r.stdout.startswith("map_reduce")
