#!/usr/bin/env python3
"""跨 skill 共享：ASR 转写本地媒体文件 + 格式化 transcript md。

复用 mlx-whisper large-v3-turbo 模型（tools/asr-poc/models/）。
该目录被 .gitignore 排除，clone 后通常不存在 —— 此时自动回退到 HF 仓库名下载。

用法：
  from asr import transcribe_local, format_transcript_md, audio_duration
  result = transcribe_local(Path("audio.m4a"))
  md = format_transcript_md(result["segments"], title=..., source=...,
                            language=result["language"],
                            duration=result["duration"],
                            model_name="mlx-whisper large-v3-turbo")
  # 校验转写完整性（asr 时长 vs 音频真实时长）
  real = audio_duration(Path("audio.m4a"))
"""
from __future__ import annotations
import subprocess
from pathlib import Path

# 复用 _shared.paths 找项目根（定位 Whisper 模型）
import sys
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from paths import find_project_root

PROJECT_ROOT = find_project_root()
WHISPER_MODEL_PATH = PROJECT_ROOT / "tools" / "asr-poc" / "models" / "whisper-large-v3-turbo"
# 本地模型缺失时的兜底：把 HuggingFace 仓库名交给 mlx-whisper，让它自己下载。
# 依据 mlx_whisper.load_models.load_model 的实现 —— 它先试 `Path(x).exists()`，
# 为假就直接 `snapshot_download(repo_id=x)`。即 path_or_hf_repo 这个参数
# **本来就同时接受「本地路径」与「仓库名」**，不是只认本地路径。
WHISPER_HF_REPO = "mlx-community/whisper-large-v3-turbo"
MODEL_NAME = "mlx-whisper large-v3-turbo"


def audio_duration(media_path: Path) -> float | None:
    """用 ffprobe 读媒体文件真实时长（秒）。失败返回 None。

    用于 ASR 转写完整性校验：转写时长应接近音频真实时长，
    若差太多说明 whisper 没跑完（截断/超时），转写结果不可信。
    """
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(media_path)],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return None
        return float(r.stdout.strip())
    except (ValueError, subprocess.TimeoutExpired, OSError):
        return None


def transcribe_local(media_path: Path, language: str = "zh") -> dict:
    """用 mlx-whisper 转写本地媒体文件。

    Args:
        media_path: 本地音频/视频文件路径
        language: 语言代码（默认 zh）

    Returns:
        {"segments": [{"start", "end", "text", ...}],
         "language": str, "duration": float, "text": str}

    Raises:
        RuntimeError: 转写失败
    """
    # ⚠️ 2026-10-06：此前本地模型不存在时直接 raise，等于「ASR 对所有 clone 用户
    # 必然失败」——.gitignore 排除了 tools/，仓库里不可能带得动这个模型目录。
    # 改为回退到 HF 仓库名，由 mlx-whisper 自动下载（缓存在 ~/.cache/huggingface）。
    if WHISPER_MODEL_PATH.exists():
        model_ref = str(WHISPER_MODEL_PATH)
    else:
        print(f"  本地模型未找到（{WHISPER_MODEL_PATH}），"
              f"将从 HuggingFace 自动下载 {WHISPER_HF_REPO}，首次运行较慢…")
        model_ref = WHISPER_HF_REPO

    import mlx_whisper
    result = mlx_whisper.transcribe(
        str(media_path),
        path_or_hf_repo=model_ref,
        language=language,
        initial_prompt="以下是简体中文的转写。",
    )
    segments = result.get("segments", [])
    duration = segments[-1]["end"] if segments else 0.0
    return {
        "segments": segments,
        "language": result.get("language", language),
        "duration": duration,
        "text": result.get("text", ""),
    }


def format_transcript_md(segments: list, *, title: str, source: str,
                         language: str, duration: float,
                         model_name: str = MODEL_NAME) -> str:
    """把 segments 格式化成 transcript md 文本（统一的分段时间戳格式）。

    格式：
        # {title}
        > 来源：{source}
        > 时长：{duration}s · 语言：{language} · 模型：{model_name}

        **[MM:SS]** {text}
        ...
    """
    lines = [
        f"# {title}",
        "",
        f"> 来源：{source}",
        f"> 时长：{duration:.1f}s · 语言：{language} · 模型：{model_name}",
        "",
    ]
    for seg in segments:
        start = seg.get("start", 0.0)
        m, s = int(start) // 60, int(start) % 60
        text = seg.get("text", "").strip()
        lines.append(f"**[{m:02d}:{s:02d}]** {text}")
    return "\n\n".join(lines)


# ── ASR 失败检测 ─────────────────────────────────────────────
# 2026-10-05 补入（与主项目对齐）。此前本文件**没有**失败检测，
# rss 侧仅有的两道防线都拦不住幻觉：
#   ① asr_podcast.check_duration 只比时长 —— 幻觉转写时间戳铺满全程照样通过
#   ② build_v3 的 200 字符下限 —— 「字幕志愿者」重复 40 分钟 = 2000 字符，放行
# 结果：静音/幻觉的英文播客会产出一份「看着完整」的垃圾转写，
# 并被标成 transcript_status: ok，然后拿它去生成笔记。
#
# ⚠️ 两条判据都**与语种无关**（这正是主项目删掉「中文字符数 < 50」的教训：
#     那个判据把「语种」当「内容量」用，会把真英文访谈误判成失败）。
#     本仓库只开源 rss，但判据本身对中英文播客一视同仁。
#
# ⚠️ 2026-10-12 第二个错判据也删了：`segments 数 < 3` 与上面是**同一类错误** ——
#     **段数不是内容量的度量**。whisper 遇到连续密集口播会把整段合并成大段，
#     少段数不等于没内容。主项目实测有笔记 48 秒只出 2 段、正文 336 字完整内容，
#     却因段数不够被判「无语音」，内容直接丢失；同批 7 条真幻觉的正文只有 8~20 字
#     —— 段数把这两种完全分不开。
#     阈值取自实测分布：幻觉 8~20 字，最短真实内容 185 字，中间是空的。
#     80 字往下 4 倍余量、往上 2.3 倍余量，两边都不贴边。
_DEFAULT_MIN_BODY_CHARS = 80

# whisper 在无语音输入时倾向重复的固定文本（幻觉输出）
# ⚠️ 这是**字数判据兜不住的那一半**：字数少靠阈值就能拦，但 whisper 在长音频上
#     会把同一句幻觉**循环重复几十遍**，字数轻松过阈值 —— 只能靠模式识别。
_HALLUCINATION_PATTERNS = (
    r"^字幕[\u4e00-\u9fff\s]*$",   # "字幕志愿者"/"字幕君"/"字幕组" 等
    r"^字幕字幕.*$",               # 重复"字幕"开头
    r"^Thanks for watching.*$",    # 英文播客的同类幻觉
    r"^Subtitles by.*$",           # 同上
    r"^Amara\.org.*$",             # 同上
)


def is_asr_failed(
    segments: list,
    text: str = "",
    *,
    min_body_chars: int = _DEFAULT_MIN_BODY_CHARS,
) -> tuple[bool, str]:
    """检测 ASR 转写**是否产出了内容**（不是「是不是中文」，也不是「切了几段」）。

    失败判定（任一条件触发）：
    - 正文净字数 < min_body_chars（默认 80）—— 几乎什么都没转出来
    - 文本命中已知幻觉模式 —— whisper 在无语音时的固定输出

    ⚠️ **不要在这里判断语种**。「中文字符 0」不是失败信号：ASR 转出全英文的
    真英文访谈是正常产出。主项目曾用「中文字符 < 50」当失败判据，
    实测 5 份真实英文访谈笔记中招。
    ⚠️ 同理**不要用段数当内容量**：段数只反映 whisper 怎么切分音频，
    不反映说了多少话（见上方 2026-10-12 的说明）。

    Args:
        segments: ASR 返回的 segments 列表
        text: ASR 返回的完整文本（可选，segments 拼接亦可）
        min_body_chars: 正文净字数下限（可调）

    Returns:
        (is_failed, reason)：
          - is_failed=True 时，reason 是人类可读的失败原因
          - is_failed=False 时，reason 为空字符串
    """
    import re
    full_text = text or " ".join(seg.get("text", "") for seg in segments)

    body_chars = len(re.sub(r"\s+", "", full_text))
    if body_chars < min_body_chars:
        return True, f"正文净字数 {body_chars} < {min_body_chars}（音频无有效语音）"
    for pattern in _HALLUCINATION_PATTERNS:
        if re.match(pattern, full_text.strip()):
            return True, "命中已知 ASR 幻觉模式（无语音时的固定输出）"
    return False, ""
