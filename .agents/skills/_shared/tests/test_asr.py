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


# ========== is_asr_failed 转写内容可信度（2026-10-05 补入） ==========
#
# 本仓库此前**没有** ASR 失败检测，幻觉完全裸奔：
#   - asr_podcast.check_duration 只比时长 → 幻觉时间戳铺满全程照样通过
#   - build_v3 的 200 字符下限 → 「字幕志愿者」重复 40 分钟 = 2000 字符，放行
# 下面每条断言都做过变异验证：把幻觉模式表清空，幻觉两条必须变红。

def _segs(n, text):
    return [{"start": float(i), "end": float(i) + 1, "text": text} for i in range(n)]


def test_段数过少算失败():
    failed, reason = asr.is_asr_failed(_segs(1, "any content at all"))
    assert failed is True
    assert "segments" in reason


def test_幻觉输出算失败():
    """whisper 在无语音时的固定输出：必须拦下，不能拿去生成笔记。"""
    for text in ("字幕志愿者", "字幕君", "字幕组", "字幕字幕字幕",
                 "Thanks for watching!", "Subtitles by the Amara.org community"):
        failed, reason = asr.is_asr_failed(_segs(6, text))
        assert failed is True, f"幻觉「{text}」应判失败"
        assert "幻觉" in reason


def test_任何语种的正常转写都不算失败():
    """这是本函数存在的意义：**与语种无关**。

    判据只看「有没有内容」，绝不能把非中文内容误判成失败 ——
    主项目曾用「中文字符 < 50」当判据，实测 5 份真实英文访谈笔记中招，
    转写稿顶部还挂了假的「未识别到有效内容」横幅。
    """
    for text in ("Welcome back to the show, today we talk about AI.",
                 "こんにちは、今日の番組です",
                 "OK so 这个 topic 很有意思"):
        failed, reason = asr.is_asr_failed(_segs(8, text))
        assert failed is False, f"「{text[:20]}…」应判成功，实际：{reason}"


def test_签名里没有语种旋钮():
    """`min_chinese_chars` 不得以任何形式回来 —— 那是被删掉的错误判据。"""
    import inspect
    assert "min_chinese_chars" not in inspect.signature(asr.is_asr_failed).parameters
    src = inspect.getsource(asr.is_asr_failed)
    assert "chinese" not in src.lower() and "\\u4e00" not in src


def test_asr_podcast_接上了内容校验():
    """守门：光有函数不够，asr_podcast 必须真的调它。

    变异验证点：把 asr_podcast 里的 `is_asr_failed(...)` 调用删掉，这条必须变红。
    """
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "rss-grab" / "scripts" / "asr_podcast.py").read_text(encoding="utf-8")
    assert "is_asr_failed" in src, "asr_podcast 没有调用 is_asr_failed —— 函数是死的"
    assert "asr_untrustworthy" in src, "没有把不可信转写拦下来落盘"
