#!/usr/bin/env python3
"""跨 skill 共享：ASR 转写本地媒体文件 + 格式化 transcript md。

复用 mlx-whisper large-v3-turbo 模型（tools/asr-poc/models/）。

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
        RuntimeError: 模型不存在 / 转写失败
    """
    if not WHISPER_MODEL_PATH.exists():
        raise RuntimeError(
            f"Whisper 模型不存在: {WHISPER_MODEL_PATH}\n"
            f"请确认 tools/asr-poc/models/whisper-large-v3-turbo/ 软链接是否就绪")

    import mlx_whisper
    result = mlx_whisper.transcribe(
        str(media_path),
        path_or_hf_repo=str(WHISPER_MODEL_PATH),
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
