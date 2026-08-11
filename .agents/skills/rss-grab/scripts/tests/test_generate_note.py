#!/usr/bin/env python3
"""generate_note 纯函数单测：format_info_json / sanitize_index_cell / register_rss_index。
不测 call_llm（真实 LLM 靠端到端验证）。"""
import sys, pathlib, tempfile, json
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))
import generate_note as g


def _sample_info():
    return {
        "feed": {"title": "独树不成林", "author": "鬼鬼祟祟的树",
                 "link": "https://xiaoyuzhoufm.com/podcast/xxx"},
        "item": {"title": "361-测试标题", "guid": "6a7410b31b5e24969ce9c46e",
                 "guid_hash8": "f35eb04d", "pub_date": "Thu, 06 Aug 2026 05:10:23 GMT",
                 "duration": "00:35:04", "duration_seconds": 2104,
                 "link": "https://xiaoyuzhoufm.com/episode/xxx"},
        "local": {"audio_path": "rss/audio/361-f35eb04d.m4a",
                  "fetched_at": "2026-08-08T19:05:30+0800"},
    }


def test_format_info_json_rss_structure():
    """rss info.json 是 feed/item/local 三层，format 后应含关键字段。"""
    text = g.format_info_json(_sample_info())
    assert "独树不成林" in text
    assert "鬼鬼祟祟的树" in text
    assert "361-测试标题" in text
    assert "f35eb04d" in text
    assert "2104" in text


def test_sanitize_index_cell_pipe():
    """| 替换成 / 避免破坏表格。"""
    assert g.sanitize_index_cell("标题|带竖线") == "标题/带竖线"


def test_sanitize_index_cell_newline():
    """换行替换成空格。"""
    assert g.sanitize_index_cell("第一行\n第二行") == "第一行 第二行"


def test_sanitize_index_cell_empty():
    assert g.sanitize_index_cell("") == ""
    assert g.sanitize_index_cell(None) == ""


def test_register_rss_index_new():
    """新笔记 -> INDEX.md 追加一行。"""
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        note_path = td / "361-测试-f35eb04d.md"
        note_path.write_text("笔记内容", encoding="utf-8")
        result = g.register_rss_index(_sample_info(), note_path, index_path=td / "INDEX.md")
        assert result == "added"
        index = (td / "INDEX.md").read_text(encoding="utf-8")
        assert "| 日期 |" in index or "| 日期|" in index
        assert "361-测试标题" in index
        assert "鬼鬼祟祟的树" in index
        assert "f35eb04d" in index
        assert "361-测试-f35eb04d.md" in index


def test_register_rss_index_dedup():
    """同一 guid_hash8 二次追加 -> 跳过。"""
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        note_path = td / "361-测试-f35eb04d.md"
        note_path.write_text("x", encoding="utf-8")
        index_path = td / "INDEX.md"
        g.register_rss_index(_sample_info(), note_path, index_path=index_path)
        result = g.register_rss_index(_sample_info(), note_path, index_path=index_path)
        assert result == "exists"


def test_register_rss_index_missing_hash8_skipped():
    """info 缺 guid_hash8 -> skipped。"""
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        note_path = td / "x.md"
        note_path.write_text("x", encoding="utf-8")
        info = _sample_info()
        info["item"]["guid_hash8"] = ""
        result = g.register_rss_index(info, note_path, index_path=td / "INDEX.md")
        assert result == "skipped"
