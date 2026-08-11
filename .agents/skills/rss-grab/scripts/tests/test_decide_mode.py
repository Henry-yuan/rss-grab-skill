#!/usr/bin/env python3
"""decide_mode 单测：长度档位判断（rss 版：>= 50K 一律 map_reduce）。"""
import sys, pathlib, tempfile
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))
import decide_mode as d


def test_short_transcript_skill_mode():
    """< 50K 字符 -> skill 模式。"""
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        t = td / "测试-f35eb04d.transcript.md"
        t.write_text("x" * 10000, encoding="utf-8")
        mode, reason = d.decide(t)
        assert mode == "skill"
        assert "10000" in reason


def test_50k_boundary_map_reduce():
    """正好 50K 字符 -> map_reduce（边界）。"""
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        t = td / "测试-f35eb04d.transcript.md"
        t.write_text("x" * 50000, encoding="utf-8")
        mode, reason = d.decide(t)
        assert mode == "map_reduce"
        assert "50000" in reason


def test_long_transcript_map_reduce_mode():
    """> 50K 字符 -> map_reduce 模式（无论标题是否含播客关键词）。"""
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        t = td / "测试-f35eb04d.transcript.md"
        t.write_text("x" * 93000, encoding="utf-8")
        mode, reason = d.decide(t)
        assert mode == "map_reduce"
        assert "93000" in reason


def test_extract_hash8_from_path():
    """从 transcript 文件名提取 guid_hash8。"""
    p = pathlib.Path("rss/transcripts/独树不成林-361-f35eb04d.transcript.md")
    assert d.extract_hash8_from_path(p) == "f35eb04d"
