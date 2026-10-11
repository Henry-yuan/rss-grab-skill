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


def test_正文净字数过少算失败():
    failed, reason = asr.is_asr_failed(_segs(1, "any content at all"))
    assert failed is True
    assert "净字数" in reason


def test_段数少但内容足不算失败():
    """🔴 2026-10-12 修的真 bug 的回归锁。

    旧判据 `segments < 3` 把「段数」当「内容量」用。whisper 遇到连续密集口播
    会合并成大段，少段数不等于没内容 —— 实测有笔记 48 秒只出 2 段、
    336 字完整正文，却被判「无语音」，内容直接丢失。
    """
    real = ("要做好需求管理呢,一定要掌握三层管理法。第一层,战略层需求,"
            "对应的是咱们年度季度的战略目标。数量少,但是每个都决定着项目的生死。"
            "核心管理逻辑是,高层研讨确定,季度规划,且定了就基本不动。"
            "第二层是常规的业务需求,来自月度周度的数据分析,用户反馈和市场变化。")
    for n in (1, 2):
        assert asr.is_asr_failed(_segs(n, real))[0] is False, \
            f"{n} 段但长正文应判成功，段数少不是内容少的证据"


def test_长篇循环幻觉仍算失败():
    """字数判据兜不住的那一半：whisper 会把同一句幻觉循环重复几十遍。

    这种样本字数轻松过阈值，只能靠模式识别 —— 所以模式表不许被「反正有字数
    判据了」的理由删掉。
    """
    for loop in ("字幕志愿者 " * 80, "Thanks for watching! " * 60,
                 "Subtitles by the Amara.org community " * 40):
        failed, reason = asr.is_asr_failed(_segs(9, loop))
        assert failed is True, "长篇循环幻觉必须判失败"
        assert "幻觉" in reason


def test_短幻觉一律被拦():
    """whisper 在无语音时的固定输出：必须拦下，不能拿去生成笔记。

    ⚠️ 这里**不断言 reason 含「幻觉」**：这些样本都很短，先被字数阈值拦下，
    报的是「正文净字数不足」。模式判据那条路径由
    `test_长篇循环幻觉仍算失败` 单独覆盖 —— 两层各测各的，别混在一起断言。
    """
    for text in ("字幕志愿者", "字幕君", "字幕组", "字幕字幕字幕",
                 "Thanks for watching!", "Subtitles by the Amara.org community"):
        failed, reason = asr.is_asr_failed(_segs(6, text))
        assert failed is True, f"幻觉「{text}」应判失败，实际报：{reason}"


def test_任何语种的正常转写都不算失败():
    """这是本函数存在的意义：**与语种无关**。

    判据只看「有没有内容」，绝不能把非中文内容误判成失败 ——
    主项目曾用「中文字符 < 50」当判据，实测 5 份真实英文访谈笔记中招，
    转写稿顶部还挂了假的「未识别到有效内容」横幅。

    ⚠️ 样本必须够长（>80 字）才有意义：判据问的是**内容量**不是语种，
    用一句十几字的日文证明「不判失败」，测的其实是字数够不够 —— 两件事别混。
    """
    for text in ("Welcome back to the show. Today we discuss how podcast hosts "
                 "prepare for interviews and why most episodes lose listeners "
                 "in the first ten minutes of the conversation.",
                 "こんにちは、今日はこの番組についてお話しします。"
                 "ポッドキャストのホストがインタビュー准备的をどう整えるか、"
                 "詳しく見ていきましょう。実例を挙げながら説明します。",
                 "OK so 这个 topic 真的很有意思 we should talk about it "
                 "in more detail with the guests and the listeners"):
        failed, reason = asr.is_asr_failed(_segs(8, text))
        assert failed is False, f"「{text[:20]}…」应判成功，实际：{reason}"


def test_签名里没有已删旋钮():
    """被删掉的旋钮不得以任何形式回来 —— 那是被证伪的判据。

    min_chinese_chars：语种 ≠ 内容量（误伤真英文访谈）
    min_segments：段数 ≠ 内容量（误杀密集口播）
    """
    import inspect
    params = inspect.signature(asr.is_asr_failed).parameters
    assert "min_chinese_chars" not in params
    assert "min_segments" not in params, "旧段数旋钮已删，签名里不该还留着"
    assert "min_body_chars" in params
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
