#!/usr/bin/env python3
"""derive_rss_id 单测：guid 派生 + 无 guid 兜底。"""
import sys, pathlib
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))
import derive_rss_id as d


def test_guid_to_hash8_basic():
    """guid -> sha256 前 8 位。"""
    h = d.guid_to_hash8("6a7410b31b5e24969ce9c46e")
    assert len(h) == 8, f"应为 8 位，实际 {len(h)}"
    assert all(c in "0123456789abcdef" for c in h), "应为十六进制"


def test_guid_to_hash8_stable():
    """同一 guid 多次调用结果一致。"""
    h1 = d.guid_to_hash8("abc123")
    h2 = d.guid_to_hash8("abc123")
    assert h1 == h2


def test_guid_to_hash8_different_input():
    """不同 guid 结果不同。"""
    assert d.guid_to_hash8("aaa") != d.guid_to_hash8("bbb")


def test_derive_id_fallback_no_guid():
    """无 guid 时用 pubDate + 标题 hash。"""
    h = d.derive_id(guid=None, pub_date="Thu, 06 Aug 2026 05:10:23 GMT",
                    title="361-测试标题")
    assert len(h) == 8
    assert all(c in "0123456789abcdef" for c in h)


def test_derive_id_prefers_guid():
    """有 guid 时优先用 guid。"""
    h_guid = d.derive_id(guid="6a7410b31b5e24969ce9c46e",
                         pub_date="Thu, 06 Aug 2026 05:10:23 GMT",
                         title="361-测试")
    h_fallback = d.derive_id(guid=None, pub_date="Thu, 06 Aug 2026 05:10:23 GMT",
                             title="361-测试")
    # guid 和 fallback 不应相同（极小概率碰撞，这里 guid 已是 hex 字符串）
    assert h_guid != h_fallback


def test_derive_id_no_guid_no_date():
    """既无 guid 也无 pubDate：只用标题 hash（不崩）。"""
    h = d.derive_id(guid=None, pub_date=None, title="只有标题")
    assert len(h) == 8


def test_derive_id_no_guid_no_date_no_title():
    """啥都没有：返回固定占位 hash（不崩，但会告警）。"""
    h = d.derive_id(guid=None, pub_date=None, title=None)
    assert len(h) == 8
