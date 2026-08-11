#!/usr/bin/env python3
"""fetch_rss_feed --list / --pick / --pick-file 的纯函数单测：parse_pick_spec。"""
import sys, pathlib, tempfile
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))
import fetch_rss_feed as f


def test_pick_single():
    """单个序号 -> [下标]。"""
    assert f.parse_pick_spec("3", 10) == [2]


def test_pick_multi():
    """逗号分隔多序号 -> 下标列表。"""
    assert f.parse_pick_spec("1,3,5", 10) == [0, 2, 4]


def test_pick_range():
    """范围 5-8 -> [4,5,6,7]。"""
    assert f.parse_pick_spec("5-8", 10) == [4, 5, 6, 7]


def test_pick_mixed():
    """混合：1,3,5-8 -> [0,2,4,5,6,7]。"""
    assert f.parse_pick_spec("1,3,5-8", 10) == [0, 2, 4, 5, 6, 7]


def test_pick_guid():
    """guid 匹配（末尾 8 位 hash）。"""
    assert f.parse_pick_spec("abcdef12", 10) == []
    # guid 匹配需要真实 guid 列表，走 _guid_to_idx


def test_pick_out_of_range():
    """超出范围的序号 -> 忽略。"""
    assert f.parse_pick_spec("1,99", 10) == [0]


def test_pick_invalid():
    """非法输入 -> 忽略。"""
    assert f.parse_pick_spec("abc", 10) == []


def test_pick_last():
    """'last' 关键字 -> 最近 N 期（默认 5）。"""
    assert f.parse_pick_spec("last", 10) == [5, 6, 7, 8, 9]


def test_pick_last_n():
    """'last:3' -> 最近 3 期。"""
    assert f.parse_pick_spec("last:3", 10) == [7, 8, 9]


def test_guid_to_idx():
    """guid 末尾 8 位匹配 -> 下标。"""
    items = [
        {"title": "a", "guid": "6a7410b31b5e24969ce9c46e", "pub_date": "2026-01-01"},
        {"title": "b", "guid": "6831ea4adce640bfdda20be4", "pub_date": "2026-01-02"},
    ]
    # 真实 guid 的 hash8（与 derive_rss_id 一致）
    h1 = f.derive_rss_id.derive_id(guid="6a7410b31b5e24969ce9c46e")
    h2 = f.derive_rss_id.derive_id(guid="6831ea4adce640bfdda20be4")
    assert f.guid_to_idx(items, h2) == 1
    assert f.guid_to_idx(items, h1) == 0
    assert f.guid_to_idx(items, "00000000") is None
