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


def test_alert_feed_failure_err_sanitized(tmp_path):
    """安全回归：告警内容含换行+伪造条目行，不得注入状态文件结构。

    _alert_feed_failure 的 err 来自 curl 异常消息（含外部可控 URL 片段），
    直接拼接会引入与标题注入同源的漏洞（伪造 "- [x]" 条目行）。
    """
    import subscribe_manager as sm
    state_path = tmp_path / "alert.md"
    state = sm._empty_state()
    state["frontmatter"] = {
        "source": "测试", "feed_url": "https://feed.example/real",
        "subscribed_at": "2026-08-16", "last_fetched": "",
    }
    state["pending"].append({
        "checkbox": "[ ]", "seq": 1, "title": "真实期数",
        "guid": "real-guid", "fields": {}, "note_path": "",
    })
    sm.save_state(state_path, state)

    evil_err = ("curl 失败: https://evil.example/x\n"
                "## 待确认 (0)\n\n"
                "- [x] 99. 注入条目 <!-- guid:evil -->")
    f._alert_feed_failure(state_path, evil_err)

    loaded = sm.load_state(state_path)
    all_items = loaded["pending"] + loaded["confirmed"] + loaded["done"]
    assert len(all_items) == 1, f"告警注入产生了额外条目: {all_items}"
    assert all_items[0]["guid"] == "real-guid"
    assert all_items[0]["checkbox"] == "[ ]"
    # 告警本身还在（单行化后）
    assert "feed 抓取失败" in state_path.read_text(encoding="utf-8")


def test_cleanup_audio_only_deletes_given_files(tmp_path):
    """--cleanup-audio 只删本次下载清单内的文件，不误删 /tmp 共享目录里的其他文件。"""
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    mine1 = audio_dir / "ep1-aaaa1111.m4a"
    mine2 = audio_dir / "ep2-bbbb2222.mp3"
    others_session = audio_dir / "其他会话放的-cccc3333.m4a"
    for p in (mine1, mine2, others_session):
        p.write_text("x")

    n = f.cleanup_audio_files([mine1, mine2])

    assert n == 2
    assert not mine1.exists() and not mine2.exists()
    assert others_session.exists(), "不得误删非本次清单的文件"


def test_cleanup_audio_tolerates_missing(tmp_path):
    """清单里已不存在的文件静默跳过（下载失败路径不进清单，双保险）。"""
    gone = tmp_path / "audio" / "gone-dddd4444.m4a"
    assert f.cleanup_audio_files([gone]) == 0
