#!/usr/bin/env python3
"""fetch_rss_feed 纯函数单测：sanitize_title / ext_from_enclosure / build_info_json。

不测 fetch_xml / download_audio / main（涉及网络 + yt-dlp 子进程，靠真实数据
端到端验证，对齐 AGENTS.md 测试规则）。
"""
import sys
import pathlib
import time

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))
import fetch_rss_feed as f


# ========== sanitize_title ==========

def test_sanitize_keeps_cjk_and_alnum():
    """中英文 + 数字保留，其他替换成 -。"""
    assert f.sanitize_title("独树不成林 361-测试") == "独树不成林-361-测试"


def test_sanitize_collapses_dashes():
    """连续 - 合并、首尾 - 去除。"""
    assert f.sanitize_title("  标题！！！???  ") == "标题"


def test_sanitize_truncates_to_max():
    """超过 60 字截断。"""
    long_title = "标题" + "a" * 80
    result = f.sanitize_title(long_title)
    assert len(result) <= 60


def test_sanitize_all_punctuation_returns_empty():
    """全标点 -> 空串（sanitize 兜底行为）。"""
    assert f.sanitize_title("！！！？？？") == ""


def test_sanitize_keeps_cjk_alnum():
    """sanitize 规则：保留 CJK + 字母数字，其他替换为 -。"""
    # 含 emoji、特殊符号
    assert f.sanitize_title("播客🎵EP.12 | 嘉宾：张三") == "播客-EP-12-嘉宾-张三"


# ========== ext_from_enclosure ==========

def test_ext_audio_mp4():
    assert f.ext_from_enclosure("audio/mp4") == "m4a"


def test_ext_audio_mpeg():
    assert f.ext_from_enclosure("audio/mpeg") == "mp3"


def test_ext_unknown_defaults_m4a():
    """未知 type 默认 m4a（播客最常见）。"""
    assert f.ext_from_enclosure("audio/unknown") == "m4a"


def test_ext_empty_defaults_m4a():
    assert f.ext_from_enclosure("") == "m4a"


# ========== build_info_json ==========

def _sample_feed():
    return {
        "title": "测试节目",
        "author": "测试作者",
        "link": "https://example.com/podcast",
        "description": "节目简介",
        "language": "zh-cn",
        "itunes_image": "https://example.com/cover.jpg",
    }


def _sample_item():
    return {
        "title": "第1期-测试",
        "guid": "abc123",
        "pub_date": "Thu, 06 Aug 2026 05:10:23 GMT",
        "duration": "00:35:04",
        "duration_seconds": 2104,
        "enclosure": {"url": "https://example.com/ep1.m4a", "type": "audio/mp4", "length": "28800031"},
        "link": "https://example.com/ep1",
        "itunes_image": "https://example.com/ep1.png",
    }


def test_build_info_json_success_path():
    """成功路径：audio_path 非 None。"""
    info = f.build_info_json(_sample_feed(), _sample_item(), "f35eb04d",
                             pathlib.Path("rss/audio/ep1-f35eb04d.m4a"))
    assert info["feed"]["title"] == "测试节目"
    assert info["feed"]["author"] == "测试作者"
    assert info["feed"]["image"] == "https://example.com/cover.jpg"
    assert info["item"]["title"] == "第1期-测试"
    assert info["item"]["guid"] == "abc123"
    assert info["item"]["guid_hash8"] == "f35eb04d"
    assert info["item"]["duration_seconds"] == 2104
    assert info["item"]["enclosure"]["type"] == "audio/mp4"
    assert info["local"]["audio_path"] == "rss/audio/ep1-f35eb04d.m4a"
    assert "fetched_at" in info["local"]
    assert "audio_download_error" not in info["local"]


def test_build_info_json_failed_path():
    """失败路径：audio_path=None（下载失败时）。"""
    info = f.build_info_json(_sample_feed(), _sample_item(), "f35eb04d", None)
    assert info["local"]["audio_path"] is None
    # audio_download_error 由调用方追加，build_info_json 本身不加
    assert "audio_download_error" not in info["local"]


def test_build_info_json_fetched_at_is_iso():
    """fetched_at 是 ISO 8601 格式（带时区）。"""
    info = f.build_info_json(_sample_feed(), _sample_item(), "abc12345", None)
    ts = info["local"]["fetched_at"]
    # 形如 2026-08-08T19:05:30+0800
    assert "T" in ts
    assert len(ts) >= 20
