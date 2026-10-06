"""「原料 → 笔记」管道：队列 + 收割（rss-only 裁剪版）。

与主项目 `_shared/note_worker.py` 同源，**本仓库只保留 rss**（双仓库约定：
`_shared` 是两份独立拷贝、各改各的，不做单向覆盖）。

## 为什么要有这个
抓原料要串行 + 随机抖动（反爬），写笔记是纯 LLM、不碰平台、可并发。
两者不抢资源，所以不该被拆成「必须先后手动跑两步」。

## 用法
```bash
python3 fetch_rss_pending.py 待抓取URL/RSS.md rss --auto-notes
python3 _shared/note_worker.py --source rss --concurrency 10
```

## 队列即重试清单
成功条目从队列删除、失败条目留下 —— worker 崩了/限流了，重跑即断点续跑，
**不需要重新抓原料**。这是选队列而非「每条 fork 子进程」的主因：
fork 出去的子进程失败不留痕，而笔记生成恰恰最易失败。
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait

# ⚠️ 层级：_shared → skills → .agents → <项目根>，是 parents[3]。
PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
QUEUE_DIR = PROJECT_ROOT / ".notes-queue"

# 2026-10-06（Henry）：LLM 侧并发上限 10。抓取端不受此限 —— 它必须串行。
MAX_CONCURRENCY_CEILING = 10

_SOURCES = ("rss",)

# 各源的笔记生成入口（与 _note_cmd 同一批文件）。rss-only 裁剪版只留 rss。
_SKILL_DIR = {"rss": "rss-grab"}
_ENTRY_SCRIPT = {"rss": "generate_note_v3.py"}

def queue_file(source: str) -> pathlib.Path:
    return QUEUE_DIR / f"{source}.jsonl"


def running_marker(source: str) -> pathlib.Path:
    return QUEUE_DIR / f"{source}.running"


def mark_running(source: str) -> pathlib.Path:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    p = running_marker(source)
    p.write_text(str(os.getpid()), encoding="utf-8")
    return p


def clear_running(source: str) -> None:
    try:
        running_marker(source).unlink(missing_ok=True)
    except OSError:
        pass


def enqueue(source: str, item_id: str) -> None:
    if source not in _SOURCES:
        raise ValueError(f"未知源 {source!r}，可选 {_SOURCES}")
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    if item_id in read_queue(source):
        return
    with queue_file(source).open("a", encoding="utf-8") as f:
        f.write(json.dumps({"id": item_id, "at": time.time()}, ensure_ascii=False) + "\n")


def read_queue(source: str) -> list[str]:
    p = queue_file(source)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line)["id"])
        except (json.JSONDecodeError, KeyError):
            continue        # 坏行忽略，别让一行垃圾卡住整条管道
    return out


def write_queue(source: str, ids: list[str]) -> None:
    if not ids:
        queue_file(source).unlink(missing_ok=True)
        return
    tmp = queue_file(source).with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for i in ids:
            f.write(json.dumps({"id": i, "at": time.time()}, ensure_ascii=False) + "\n")
    os.replace(tmp, queue_file(source))


def note_exists(source: str, item_id: str) -> bool:
    d = PROJECT_ROOT / "rss"
    for pat in (f"notes/**/*-{item_id}.md", f"notes/*-{item_id}.md"):
        if next(d.glob(pat), None):
            return True
    return False


def _note_cmd(source: str, item_id: str) -> list[str]:
    return [sys.executable,
            str(PROJECT_ROOT / ".agents/skills/rss-grab/scripts/generate_note_v3.py"),
            item_id, "--no-index"]


def _one(source: str, item_id: str, timeout: int = 3600) -> tuple[str, bool, str]:
    # 先判产物在不在：省一次子进程，也把状态说清楚。
    # 别把跳过报成「成功」—— 那会让「成功 N」里混着「其实啥也没做」。
    if note_exists(source, item_id):
        return item_id, True, "已有产物，跳过"
    cmd = _note_cmd(source, item_id)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, cwd=str(PROJECT_ROOT))
    except subprocess.TimeoutExpired:
        return item_id, False, "超时"
    if r.returncode != 0:
        tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
        return item_id, False, (tail[-1][:120] if tail else f"exit {r.returncode}")
    return item_id, True, "ok"


def _backfill_index(source: str, ids: list[str]) -> tuple[bool, str]:
    """收割成功后补 INDEX 登记。

    为什么需要这一步：子进程一律传 `--no-index`（并发写同一个索引会互相覆盖），
    代价就是跑完索引缺登记。没有这一步的话每批跑完都得人工补 —— 已经因此手工
    补过两次（21 篇、30 篇），同一个洞踩两次就该治。

    各源入口自治（各自有 `--backfill-index`），worker 只负责调度，不碰各源的
    索引格式与 id 匹配细节。
    """
    if not ids:
        return True, "无成功条目，跳过"
    entry = _ENTRY_SCRIPT.get(source)
    skill = _SKILL_DIR.get(source)
    if not entry or not skill:
        return False, f"未知源 {source}，无补登记入口"
    s = PROJECT_ROOT / ".agents" / "skills" / skill / "scripts" / entry
    if not s.exists():
        return False, f"入口不存在：{s.name}"
    cmd = [sys.executable, str(s), "--backfill-index", ",".join(ids)]
    # rss 入口的 find_project_root() 从 __file__ 反推，而该文件物理上在
    # opensource 仓（主项目里是软链），不显式指定就会补到**另一个仓**的索引。
    if source == "rss":
        cmd += ["--project-root", str(PROJECT_ROOT)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                           cwd=str(PROJECT_ROOT))
    except subprocess.TimeoutExpired:
        return False, "补登记超时（不影响已生成的笔记）"
    if r.returncode != 0:
        return False, f"补登记退出码 {r.returncode}（不影响已生成的笔记）"
    for line in (r.stdout or "").splitlines():
        if "补" in line and "条" in line:
            return True, line.strip()
    return True, "已执行"


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="收割「原料→笔记」队列")
    ap.add_argument("--source", default="rss", choices=_SOURCES)
    ap.add_argument("--concurrency", type=int, default=5,
                    help=f"LLM 并发（硬上限 {MAX_CONCURRENCY_CEILING}）")
    ap.add_argument("--follow", action="store_true",
                    help="持续轮询；队列空且抓取端已退出才结束")
    ap.add_argument("--poll", type=float, default=15.0)
    a = ap.parse_args()

    conc = max(1, min(a.concurrency, MAX_CONCURRENCY_CEILING))
    if conc != a.concurrency:
        print(f"⚠️ 并发被硬上限压到 {MAX_CONCURRENCY_CEILING}（你传了 {a.concurrency}）")

    total_ok = total_fail = 0
    while True:
        ids = read_queue(a.source)
        if not ids:
            if not a.follow:
                break
            if not running_marker(a.source).exists():
                print("队列空且抓取端已退出 → 收割结束")
                break
            time.sleep(a.poll)
            continue

        print(f"[{time.strftime('%H:%M:%S')}] 队列 {len(ids)} 条，并发 {conc} 收割中…")
        done_ok, lock = [], threading.Lock()
        with ThreadPoolExecutor(max_workers=conc) as ex:
            futs = {ex.submit(_one, a.source, i): i for i in ids}
            pending = set(futs)
            while pending:
                fin, pending = wait(pending, return_when=FIRST_COMPLETED,
                                    timeout=a.poll if a.follow else None)
                for f in fin:
                    iid, ok, msg = f.result()
                    with lock:
                        if ok:
                            done_ok.append(iid)
                            print(f"  ✅ {iid[:14]}… {msg}")
                        else:
                            total_fail += 1
                            print(f"  ❌ {iid[:14]}… {msg}")
        total_ok += len(done_ok)
        if done_ok:
            write_queue(a.source, [i for i in read_queue(a.source) if i not in set(done_ok)])
            # 补 INDEX 登记（单进程，此刻无并发冲突）。失败只提示，不影响退出码 ——
            # 笔记已经生成并落盘，索引可以事后补。
            bf_ok, bf_msg = _backfill_index(a.source, done_ok)
            print(f"  📇 索引：{bf_msg}" if bf_ok else f"  ⚠️  索引：{bf_msg}")
        if not a.follow:
            break

    left = len(read_queue(a.source))
    print(f"\n=== 收割完成：成功 {total_ok} / 失败 {total_fail} / 队列剩余 {left} ===")
    if left:
        print(f"剩余条目仍在 {queue_file(a.source)}，重跑本命令即可从断点继续。")
    return 0 if not left else 1


if __name__ == "__main__":
    sys.exit(main())
