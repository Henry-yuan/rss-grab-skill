#!/usr/bin/env python3
"""asr_podcast 单测：文件名派生 + 增量判断（不调真实模型）。"""
import sys
import pathlib
import tempfile
import json
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))
import asr_podcast as a


def test_transcript_filename_from_info():
    """info.json -> transcript 文件名。"""
    info = {
        "item": {"title": "361-测试标题", "guid_hash8": "f35eb04d"},
        "feed": {"title": "独树不成林"},
    }
    fname = a.transcript_filename(info)
    assert fname == "361-测试标题-f35eb04d.transcript.md"


def test_audio_path_from_info():
    """info.json -> 对应的音频路径。"""
    info = {"local": {"audio_path": "rss/audio/标题-f35eb04d.m4a"}}
    assert a.audio_path_from_info(info) == pathlib.Path("rss/audio/标题-f35eb04d.m4a")


def test_audio_path_missing_returns_none():
    """info.json 无 audio_path（下载失败）-> None。"""
    info = {"local": {"audio_path": None}}
    assert a.audio_path_from_info(info) is None


def test_transcript_exists_check():
    """transcripts/ 已有该 hash 的 transcript -> 跳过。"""
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "标题-f35eb04d.transcript.md").write_text("x", encoding="utf-8")
        assert a.transcript_exists(td, "f35eb04d") is True
        assert a.transcript_exists(td, "aaaaaaaa") is False


def test_transcript_exists_dir_missing():
    """目录不存在 -> False（不崩）。"""
    with tempfile.TemporaryDirectory() as td:
        assert a.transcript_exists(pathlib.Path(td) / "no_dir", "abc12345") is False


def test_find_info_jsons():
    """扫 raw/ 目录找所有 info.json。"""
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "标题A-aaaaaaaa.info.json").write_text("{}", encoding="utf-8")
        (td / "标题B-bbbbbbbb.info.json").write_text("{}", encoding="utf-8")
        (td / "不是info.md").write_text("x", encoding="utf-8")
        infos = a.find_info_jsons(td)
        assert len(infos) == 2


# ========== check_duration（转写完整性校验）==========

def test_duration_match():
    """ASR 时长 ≈ 音频时长 -> 通过。"""
    ok, reason = a.check_duration(2800, 2816)
    assert ok is True
    assert reason == "ok"


def test_duration_short_blocks():
    """ASR 只转一半（<85%）-> 拦截。"""
    ok, reason = a.check_duration(1400, 2816)
    assert ok is False
    assert "duration_mismatch" in reason


def test_duration_at_threshold_passes():
    """ASR 恰好在阈值 85% -> 通过（边界允许）。"""
    ok, _ = a.check_duration(2816 * 0.85, 2816)
    assert ok is True


def test_duration_just_below_threshold_blocks():
    """ASR 略低于阈值（84.9%）-> 拦截。"""
    ok, _ = a.check_duration(2816 * 0.849, 2816)
    assert ok is False


def test_duration_real_unknown_passes():
    """音频真实时长未知（ffprobe 失败）-> 放行（不误伤）。"""
    ok, reason = a.check_duration(100, None)
    assert ok is True
    assert reason == "unknown_real_duration"


def test_duration_asr_longer_passes():
    """ASR 时长 > 音频（whisper 多出一点）-> 通过（不误伤）。"""
    ok, _ = a.check_duration(3000, 2816)
    assert ok is True
