#!/usr/bin/env python3
"""批量 ASR 转写（串行，可暂停/续跑）。

用法：
  python3 batch_transcribe.py [--force]

特性：
  - 串行转写（mlx-whisper 本地 GPU 计算，并发无意义）
  - 已转写的自动跳过（transcript_exists），中断后重跑续跑
  - Ctrl+C 暂停：已完成的期保住了，下次续跑自动跳过

流程：
  扫 rss/raw/*.info.json -> 逐个转写到 rss/transcripts/
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import asr_podcast


def main():
    ap = argparse.ArgumentParser(description="批量 ASR 转写（串行，可暂停续跑）")
    ap.add_argument("--force", action="store_true", help="强制重转（默认跳过已有）")
    ap.add_argument("--max", type=int, default=0,
                    help="本次最多转写 N 期（默认全部待转写；已转写的始终跳过）")
    args = ap.parse_args()

    raw_dir = Path("rss/raw")
    transcripts_dir = Path("rss/transcripts")
    transcripts_dir.mkdir(parents=True, exist_ok=True)

    infos = asr_podcast.find_info_jsons(raw_dir)
    if not infos:
        print(f"📭 {raw_dir} 下没有 info.json")
        return

    # 过滤：已有 transcript 的跳过（除非 --force）
    todo = []
    for ip in infos:
        if not args.force and asr_podcast.transcript_exists(transcripts_dir,
                                                             _hash8(ip)):
            continue
        todo.append(ip)
    done = len(infos) - len(todo)
    # --max 限定本次最多跑 N 期（剩余留待下次）
    if args.max > 0 and len(todo) > args.max:
        todo, rest = todo[:args.max], todo[args.max:]
    else:
        rest = []
    print(f"=== 批量转写: 共 {len(infos)} 期, 已转写 {done}, 待转写 {len(todo)}"
          + (f", 本次限定 {args.max} 期, 下次续跑 {len(rest)}" if rest else "") + " ===")
    if not todo:
        print("✅ 全部已转写完成")
        return

    n_ok = n_fail = 0
    try:
        for i, ip in enumerate(todo, 1):
            print(f"\n--- [{i}/{len(todo)}] {ip.name} ---")
            ok, msg = asr_podcast.transcribe_one(ip, transcripts_dir, force=args.force)
            if ok:
                n_ok += 1
            elif msg == "skip_exists":
                print(f"  ⏭️  已有 transcript，跳过")
            elif msg == "no_audio":
                print(f"  ⏭️  无音频文件，跳过")
                n_fail += 1
            else:
                print(f"  ❌ {msg[:200]}")
                n_fail += 1
    except KeyboardInterrupt:
        print(f"\n\n⏸️  用户暂停（Ctrl+C）")
        print(f"   已转写 {n_ok} 期，待续跑 {len(todo) - (n_ok + n_fail)} 期")
        print(f"   下次重跑本脚本自动跳过已完成的")
        sys.exit(130)

    print(f"\n=== 转写完成：成功 {n_ok} / 失败 {n_fail} / 跳过 {done} ===")
    if n_fail:
        print("  失败的期下次重跑会重试（无 transcript 不算完成）")


def _hash8(info_path: Path) -> str:
    """从 info.json 文件名提取 guid_hash8（<title>-<hash8>.info.json）。"""
    return info_path.stem.rsplit("-", 1)[-1]


if __name__ == "__main__":
    main()
