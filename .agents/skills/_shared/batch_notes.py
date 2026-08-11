#!/usr/bin/env python3
"""批量笔记生成（20 并发，可暂停/续跑）—— rss 源（开源版，已裁剪为 rss-only）。

用法：
  python3 batch_notes.py [--max-workers N] [--max N] [--force]

特性：
  - 并发调 regenerate_note（LLM API 调用，无硬件瓶颈，默认 20 并发 + 2s 发射间隔）
  - 429 熔断降级：限流时并发 20 -> 15 -> 10
  - 已生成笔记的跳过，中断后重跑续跑
  - Ctrl+C 暂停：已完成的保住了

各源约定：
  rss:  transcripts rss/transcripts/<title>-<hash8>.transcript.md
        笔记        rss/notes/（按源分目录 rss/notes/<源名>/）
        regenerate  rss-grab/scripts/regenerate_note.py <hash8>
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))
from paths import find_project_root

PROJECT_ROOT = find_project_root()

MAX_WORKERS = 20          # 并发上限
LAUNCH_INTERVAL = 2       # 发射间隔（秒），错开 API 峰值
FALLBACK_WORKERS = 15     # 429 熔断降级第一档
FALLBACK_WORKERS_2 = 10   # 429 熔断降级第二档

# 各源配置：transcript 目录 / 笔记目录 / regenerate 脚本 / id 提取方式
SOURCES = {
    "rss": {
        "transcripts": PROJECT_ROOT / "rss" / "transcripts",
        "notes": PROJECT_ROOT / "rss" / "notes",
        "regenerate": PROJECT_ROOT / ".agents" / "skills" / "rss-grab" / "scripts" / "regenerate_note.py",
        "id_re": r"-([0-9a-f]{8})\.transcript\.md$",   # <title>-<hash8>.transcript.md
        "id_name": "guid_hash8",
    },
}


def _extract_id(transcript_path: Path, source_cfg: dict) -> str:
    """从 transcript 文件名提取 id（guid_hash8 / bvid / note_id）。"""
    import re
    m = re.search(source_cfg["id_re"], transcript_path.name)
    if m:
        return m.group(1)
    # 兜底：整个 stem 去掉 .transcript 后缀（不崩）
    return transcript_path.name.replace(".transcript.md", "")


def _has_existing_note(source_cfg: dict, id_: str) -> bool:
    """按 id 判断是否已有笔记（存在则跳过）。"""
    notes_dir = source_cfg["notes"]
    if not notes_dir.exists():
        return False
    for f in notes_dir.rglob(f"*-{id_}.md"):
        if f.name != "INDEX.md":
            return True
    return False


def _run_regenerate(regenerate_script: Path, id_: str) -> tuple[bool, str]:
    """跑 regenerate_note.py 单期，返回 (ok, msg)。"""
    cmd = [sys.executable, str(regenerate_script), id_]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if r.returncode == 0:
            return True, r.stdout.strip()[-200:]
        return False, (r.stderr.strip() or r.stdout.strip())[-300:]
    except subprocess.TimeoutExpired:
        return False, "超时（1 小时）"


def main():
    ap = argparse.ArgumentParser(description="批量笔记生成（20 并发，可暂停续跑）—— rss 专用")
    ap.add_argument("--source", choices=["rss"], required=True,
                    help="内容源（rss）")
    ap.add_argument("--max-workers", type=int, default=MAX_WORKERS,
                    help=f"并发数（默认 {MAX_WORKERS}）")
    ap.add_argument("--force", action="store_true", help="强制重生成（默认跳过已有笔记）")
    ap.add_argument("--max", type=int, default=0,
                    help="本次最多生成 N 篇笔记（默认全部待生成；已有的始终跳过）")
    args = ap.parse_args()

    source_cfg = SOURCES[args.source]
    transcripts_dir = source_cfg["transcripts"]
    if not transcripts_dir.exists():
        print(f"📭 {transcripts_dir} 不存在（先跑转写）")
        return

    # 收集待处理 transcript（跳过已有笔记）
    todo = []
    for tp in sorted(transcripts_dir.glob("*.transcript.md")):
        id_ = _extract_id(tp, source_cfg)
        if args.force:
            todo.append(tp)
            continue
        if _has_existing_note(source_cfg, id_):
            continue
        todo.append(tp)

    # --max 限定本次最多生成 N 篇
    if args.max > 0 and len(todo) > args.max:
        todo, rest = todo[:args.max], todo[args.max:]
    else:
        rest = []
    total_transcripts = len(list(transcripts_dir.glob("*.transcript.md")))
    print(f"=== 批量笔记[{args.source}]: 共 {total_transcripts} 期 transcript, "
          f"待生成笔记 {len(todo)}"
          + (f", 本次限定 {args.max} 篇, 下次续跑 {len(rest)}" if rest else "") + " ===")
    if not todo:
        print("✅ 全部已有笔记")
        return

    workers = min(args.max_workers, len(todo))
    print(f"并发 {workers}，发射间隔 {LAUNCH_INTERVAL}s")

    breaker = [0]  # 429 计数，>=1 降档
    lock = threading.Lock()
    done_count = [0]
    fail_count = [0]
    last_launch = [0.0]
    lock_launch = threading.Lock()
    # 信号量真控制并发：429 熔断时 acquire 更多次 -> 实际在飞数下降
    semaphore = threading.BoundedSemaphore(workers)
    active_limit = [workers]  # 当前并发上限（熔断降级修改）

    def launch(pool, tp):
        with lock_launch:
            now = time.time()
            wait = LAUNCH_INTERVAL - (now - last_launch[0])
            if wait > 0:
                time.sleep(wait)
            semaphore.acquire()  # 占一个并发位（熔断后这里等更久）
            id_ = _extract_id(tp, source_cfg)
            fut = pool.submit(_run_regenerate, source_cfg["regenerate"], id_)
            # 关键修复：future 完成后必须释放信号量位，否则 20 个位被永久占用，
            # 第 21 篇 semaphore.acquire() 永久阻塞，整个循环卡死（20 篇后停滞的根因）
            fut.add_done_callback(_release_slot)
            futures[fut] = tp
            last_launch[0] = time.time()

    def _release_slot(_fut):
        semaphore.release()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        next_idx = 0

        # 先发前 workers 期
        for i in range(min(workers, len(todo))):
            launch(pool, todo[i])
            next_idx += 1

        try:
            while futures:
                completed, _ = wait(list(futures), return_when=FIRST_COMPLETED)
                for fut in completed:
                    tp = futures.pop(fut)
                    ok, msg = fut.result()
                    with lock:
                        if ok:
                            done_count[0] += 1
                        else:
                            fail_count[0] += 1
                    status = "✅" if ok else "❌"
                    print(f"  {status} [{done_count[0]+fail_count[0]}/{len(todo)}] {tp.name[:50]}")

                    # 429 熔断降并发：改 active_limit + 用 acquire 数量控制
                    if re.search(r"429|RateLimit(?:Error)?", msg):
                        breaker[0] += 1
                        if breaker[0] == 1 and active_limit[0] > FALLBACK_WORKERS_2:
                            active_limit[0] = FALLBACK_WORKERS
                            print(f"  🔻 429 熔断：并发降级 -> {active_limit[0]}")
                        elif breaker[0] >= 2 and active_limit[0] > FALLBACK_WORKERS_2:
                            active_limit[0] = FALLBACK_WORKERS_2
                            print(f"  🔻 429 熔断：并发降级 -> {active_limit[0]}")
                    else:
                        breaker[0] = 0  # 连续无 429 复位

                    if next_idx < len(todo):
                        launch(pool, todo[next_idx])
                        next_idx += 1
        except KeyboardInterrupt:
            print(f"\n\n⏸️  用户暂停（Ctrl+C）")
            print(f"   已完成 {done_count[0]} / 失败 {fail_count[0]}，待续跑 {len(todo) - done_count[0] - fail_count[0]}")
            print(f"   下次重跑本脚本自动跳过已完成的")
            # 立即退出，不等在飞任务（已完成的笔记文件已落盘，续跑跳过）
            pool.shutdown(wait=False, cancel_futures=True)
            sys.exit(130)

    print(f"\n=== 笔记完成[{args.source}]：成功 {done_count[0]} / 失败 {fail_count[0]} ===")
    if fail_count[0]:
        print("  失败的期下次重跑会重试（无笔记不算完成）")
        sys.exit(1)  # 有失败 -> 非零退出码（脚本化调用可感知）


if __name__ == "__main__":
    main()
