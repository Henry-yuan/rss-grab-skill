#!/usr/bin/env python3
"""_shared/asr 单测：format_transcript_md 纯函数 + transcribe_local 集成（短样本）。"""
import sys
import pathlib
SHARED_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))
import asr


# ========== format_transcript_md 纯函数 ==========

def test_format_basic():
    """基本格式：标题 + 来源 + 时长 + segments。"""
    segments = [
        {"start": 0.0, "end": 2.5, "text": "你好世界"},
        {"start": 2.5, "end": 5.0, "text": "测试转写"},
    ]
    md = asr.format_transcript_md(
        segments, title="测试节目", source="https://example.com/ep1",
        language="zh", duration=5.0, model_name="mlx-whisper large-v3-turbo")
    assert "# 测试节目" in md
    assert "https://example.com/ep1" in md
    assert "时长：5.0s" in md
    assert "语言：zh" in md
    assert "**[00:00]** 你好世界" in md
    assert "**[00:02]** 测试转写" in md


def test_format_empty_segments():
    """空 segments 不崩，duration=0。"""
    md = asr.format_transcript_md(
        [], title="空", source="x", language="zh",
        duration=0.0, model_name="mlx-whisper large-v3-turbo")
    assert "# 空" in md
    assert "时长：0.0s" in md


def test_format_timestamp_minutes_overflow():
    """超过 60 秒的时间戳显示 MM:SS（不显示 HH）。"""
    segments = [{"start": 125.0, "end": 130.0, "text": "两分五秒"}]
    md = asr.format_transcript_md(
        segments, title="t", source="s", language="zh",
        duration=130.0, model_name="m")
    assert "**[02:05]** 两分五秒" in md


def test_format_strips_whitespace_in_text():
    """segment text 前后空白被 strip。"""
    segments = [{"start": 0.0, "end": 1.0, "text": "  带空格  "}]
    md = asr.format_transcript_md(
        segments, title="t", source="s", language="zh",
        duration=1.0, model_name="m")
    assert "**[00:00]** 带空格" in md


# ========== transcribe_local 集成（真实模型，慢）==========
# 用 asr-poc/samples 里 29s 短样本，转写约 10-30s
# 样本文件不入库：本地放 tools/asr-poc/samples/sample.mp3 即可启用这 2 个测试

SAMPLE = pathlib.Path(__file__).resolve().parent.parent.parent.parent.parent / "tools" / "asr-poc" / "samples" / "sample.mp3"


def test_transcribe_local_short_sample():
    """29s 短样本能转写，返回 segments + language + duration。"""
    if not SAMPLE.exists():
        import pytest
        pytest.skip(f"样本不存在: {SAMPLE}")
    result = asr.transcribe_local(SAMPLE)
    assert "segments" in result
    assert "language" in result
    segs = result["segments"]
    assert len(segs) > 0, "应有至少 1 个 segment"
    assert "text" in segs[0]
    assert "start" in segs[0]
    # 转写内容非空（29s 样本应有文字）
    full_text = "".join(s.get("text", "") for s in segs).strip()
    assert len(full_text) > 0, "转写文本不应为空"


# ========== audio_duration（ffprobe 读真实时长）==========

def test_audio_duration_real_short_sample():
    """29s 短样本：ffprobe 读到 ~29s（真实调用，验证 ffprobe 可用）。"""
    if not SAMPLE.exists():
        import pytest
        pytest.skip(f"样本不存在: {SAMPLE}")
    d = asr.audio_duration(SAMPLE)
    assert d is not None, "ffprobe 应返回时长"
    assert 25 < d < 35, f"样本应 ~29s，实际 {d}s"


def test_audio_duration_missing_file():
    """文件不存在 -> None（不崩）。"""
    assert asr.audio_duration(pathlib.Path("/nonexistent/audio.m4a")) is None


def test_audio_duration_invalid_file():
    """存在但不是媒体文件（文本文件）-> None（ffprobe 报错不崩）。"""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".txt") as f:
        f.write(b"not audio")
        f.flush()
        assert asr.audio_duration(pathlib.Path(f.name)) is None
