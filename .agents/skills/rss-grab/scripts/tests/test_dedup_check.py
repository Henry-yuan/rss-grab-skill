#!/usr/bin/env python3
"""dedup_check 单测：检查 rss/raw/ 是否已有某 guid_hash8 的 info.json。"""
import sys, pathlib, tempfile
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))
import dedup_check as d


def test_not_exists_empty_dir():
    """空目录：不存在。"""
    with tempfile.TemporaryDirectory() as td:
        exists, fname = d.check_rss_raw_exists(pathlib.Path(td), "abcdef12")
        assert exists is False
        assert fname is None


def test_not_exists_dir_not_exist():
    """目录不存在：不存在（不崩）。"""
    with tempfile.TemporaryDirectory() as td:
        exists, fname = d.check_rss_raw_exists(
            pathlib.Path(td) / "no_such_dir", "abcdef12")
        assert exists is False
        assert fname is None


def test_exists_with_matching_hash():
    """raw/ 下有 <title>-<hash8>.info.json：命中。"""
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "独树不成林-361-abcdef12.info.json").write_text("{}", encoding="utf-8")
        exists, fname = d.check_rss_raw_exists(td, "abcdef12")
        assert exists is True
        assert fname == "独树不成林-361-abcdef12.info.json"


def test_not_exists_different_hash():
    """hash 不匹配：不命中。"""
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "标题-aaaaaaaa.info.json").write_text("{}", encoding="utf-8")
        exists, fname = d.check_rss_raw_exists(td, "bbbbbbbb")
        assert exists is False
        assert fname is None


def test_ignores_non_info_json():
    """非 .info.json 文件不参与匹配。"""
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "标题-abcdef12.md").write_text("笔记", encoding="utf-8")
        exists, fname = d.check_rss_raw_exists(td, "abcdef12")
        assert exists is False
