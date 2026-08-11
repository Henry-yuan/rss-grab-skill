#!/usr/bin/env python3
"""rss-grab 阶段 2：音频 -> transcript.md（ASR 转写）。

读 rss/raw/<...>.info.json -> 找对应音频 -> 调 _shared.asr 转写 ->
写 rss/transcripts/<sanitized_title>-<guid_hash8>.transcript.md。
增量：已有 transcript 跳过（--force 强制重转）。

用法：
  # 转写单期（给 info.json 路径）
  python3 asr_podcast.py <info_json_path> [--force]
  # 批量转写 rss/raw/ 下所有（扫 *.info.json）
  python3 asr_podcast.py --all [--force] [--out-dir rss]

依赖：mlx-whisper（仅 Apple Silicon）、tools/asr-poc/models/whisper-large-v3-turbo
"""
from __future__ import annotations
import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
# 引入 _shared.asr
SHARED_DIR = SCRIPT_DIR.parent.parent / "_shared"
sys.path.insert(0, str(SHARED_DIR))
import asr as shared_asr

DEFAULT_OUT_DIR = Path("rss")
DURATION_THRESHOLD = 0.85  # ASR 时长 < 音频真实时长 85% 判不完整


def check_duration(asr_duration: float, real_duration: float | None,
                   threshold: float = DURATION_THRESHOLD) -> tuple[bool, str]:
    """转写完整性校验。返回 (ok, reason)。

    规则：real_duration 未知时放行（无 ffprobe）；否则 ASR 时长 >= 阈值视为完整。
    """
    if not real_duration:
        return True, "unknown_real_duration"
    if asr_duration < real_duration * threshold:
        return False, f"duration_mismatch: asr {asr_duration:.0f}s vs audio {real_duration:.0f}s"
    return True, "ok"


def transcript_filename(info: dict) -> str:
    """info.json -> transcript 文件名（与音频同名，换 .transcript.md 后缀）。"""
    title = info["item"]["title"]
    hash8 = info["item"]["guid_hash8"]
    # sanitize 复用 fetch_rss_feed 的规则（import 避免重复）
    from fetch_rss_feed import sanitize_title
    return f"{sanitize_title(title)}-{hash8}.transcript.md"


def audio_path_from_info(info: dict) -> Path | None:
    """info.json -> 对应音频路径（无 audio_path 返回 None）。"""
    p = info.get("local", {}).get("audio_path")
    return Path(p) if p else None


def transcript_exists(transcripts_dir: Path, guid_hash8: str) -> bool:
    """transcripts/ 是否已有该 hash8 的 transcript。"""
    transcripts_dir = Path(transcripts_dir)
    if not transcripts_dir.exists():
        return False
    return any(transcripts_dir.glob(f"*-{guid_hash8}.transcript.md"))


def find_info_jsons(raw_dir: Path) -> list[Path]:
    """扫 raw/ 目录找所有 *.info.json。"""
    raw_dir = Path(raw_dir)
    if not raw_dir.exists():
        return []
    return sorted(raw_dir.glob("*.info.json"))


def transcribe_one(info_path: Path, transcripts_dir: Path,
                   force: bool = False) -> tuple[bool, str]:
    """转写一期。返回 (ok, message)。

    跳过条件：无音频路径 / 已有 transcript（非 force）
    """
    info = json.loads(info_path.read_text(encoding="utf-8"))
    hash8 = info["item"]["guid_hash8"]
    title = info["item"]["title"]

    # 增量：已有 transcript 跳过
    if not force and transcript_exists(transcripts_dir, hash8):
        return False, "skip_exists"

    audio = audio_path_from_info(info)
    if audio is None or not audio.exists():
        return False, "no_audio"

    out_path = transcripts_dir / transcript_filename(info)
    print(f"  ⏺️  转写: {title}")
    print(f"     音频: {audio.name} ({audio.stat().st_size // 1024 // 1024}MB)")

    t0 = time.time()
    try:
        result = shared_asr.transcribe_local(audio)
    except RuntimeError as e:
        return False, f"asr_error: {e}"
    elapsed = time.time() - t0

    # 转写完整性校验：ASR 时长应接近音频真实时长（防 whisper 截断/只转一半）
    asr_duration = result["duration"]
    real_duration = shared_asr.audio_duration(audio)
    ok, reason = check_duration(asr_duration, real_duration)
    if not ok:
        print(f"  ⚠️  时长不匹配：{reason}（转写可能不完整），跳过落盘")
        return False, reason
    if real_duration:
        print(f"     校验: ASR {asr_duration:.0f}s ≈ 音频 {real_duration:.0f}s ✅")

    source = info.get("item", {}).get("link") or info.get("feed", {}).get("link") or ""
    md = shared_asr.format_transcript_md(
        result["segments"], title=title, source=source,
        language=result["language"], duration=result["duration"])
    out_path.write_text(md, encoding="utf-8")
    print(f"     ✅ {out_path.name}（{elapsed:.0f}s, {len(result['segments'])} 段）")
    return True, "ok"


def main():
    import argparse
    ap = argparse.ArgumentParser(description="rss 播客 ASR 转写")
    ap.add_argument("info_path", nargs="?", help="单期 info.json 路径（或用 --all 批量）")
    ap.add_argument("--all", action="store_true", help="批量转写 rss/raw/ 下所有 info.json")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="输出根目录（默认 rss）")
    ap.add_argument("--force", action="store_true", help="跳过增量，强制重转")
    args = ap.parse_args()

    if not args.all and not args.info_path:
        ap.error("需要 info_path 或 --all")

    transcripts_dir = args.out_dir / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)

    if args.all:
        raw_dir = args.out_dir / "raw"
        infos = find_info_jsons(raw_dir)
        if not infos:
            print(f"📭 {raw_dir} 下没有 info.json")
            return
        print(f"=== 批量转写 {len(infos)} 期 ===")
    else:
        infos = [Path(args.info_path)]
        print(f"=== 转写单期 ===")

    n_ok = n_skip = n_fail = 0
    for i, ip in enumerate(infos, 1):
        print(f"\n--- [{i}/{len(infos)}] {ip.name} ---")
        ok, msg = transcribe_one(ip, transcripts_dir, force=args.force)
        if ok:
            n_ok += 1
        elif msg == "skip_exists":
            print(f"  ⏭️  已有 transcript，跳过（--force 重转）")
            n_skip += 1
        elif msg == "no_audio":
            print(f"  ⏭️  无音频文件，跳过")
            n_fail += 1
        else:
            print(f"  ❌ {msg[:200]}")
            n_fail += 1

    print(f"\n=== 完成：✅ {n_ok} | ⏭️ {n_skip} | ❌ {n_fail} ===")
    if n_ok:
        print(f"💡 阶段 2 完成。阶段 3（笔记生成）后续实现。")


if __name__ == "__main__":
    main()
