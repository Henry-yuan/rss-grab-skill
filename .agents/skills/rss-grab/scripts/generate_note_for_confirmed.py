#!/usr/bin/env python3
"""阶段 3：转写完成后，为「确认抓取」区的条目生成 v3 笔记（T3.3 / AC-A3）。

## 策略 A（Henry 2026-10-05 拍板）：**只处理 `[x]`**

三区的 checkbox 语义（见 SKILL.md）：

| 标记 | 含义 | 阶段 3 是否处理 |
|---|---|---|
| `[ ]` | 待确认（用户还没看过） | ❌ **跳过** |
| `[x]` | **确认抓取** | ✅ 处理 |
| `[~]` | **确认不抓** | ❌ **跳过** |
| `[done]` | 已转化 | ❌ 跳过（已完成） |

**「白烧钱防护」就在这里**：待确认区的条目用户还没审阅过，
自动给它们生成笔记 = 为用户没看过的内容烧 API 额度。
这个脚本的第一职责是**不碰不该碰的**。

## 幂等（AC-A5）

`generate_note_v3.py` 自带防重闸门（已有产物即跳过，`--force` 才覆盖），
所以重复调用本脚本不会重复消耗额度、不会重复追加 INDEX。

## 用法

    python3 generate_note_for_confirmed.py rss/订阅/<节目名>.md [--dry-run]
    python3 generate_note_for_confirmed.py --all           # 遍历订阅表全部源
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import subscribe_manager as sm  # noqa: E402

GRAB = sm.CHECKBOX_GRAB          # "[x]"
DONE = sm.CHECKBOX_DONE         # "[done]"

MAX_PARALLEL = int(
    __import__("os").environ.get("NOTE_MAX_PARALLEL", "5"))


def pick_confirmed(state: dict) -> list[dict]:
    """只取「确认抓取」区的 `[x]` 条目。

    ⚠️ 这里**不**按分区名过滤，而是按 checkbox 逐条判定 ——
    分区标题可能被用户手改，而 checkbox 是权威。
    另：已转化（`[done]`）不再处理，避免重复。
    """
    out = []
    for item in state.get("confirmed", []):
        cb = (item.get("checkbox") or "").strip()
        if cb == DONE:
            continue
        if cb != GRAB:
            continue
        if not item.get("guid"):
            continue
        out.append(item)
    return out


def find_hash8(item: dict) -> str | None:
    """从条目里拿 guid_hash8 —— 生成笔记的必需主键。

    优先从 `fields` 里找（`--sync-pick` 归区时写进去的），
    找不到就退回用 `rss/raw/` 的 info.json 按 guid 反查。
    """
    for key, val in (item.get("fields") or {}).items():
        if "hash8" in key.lower() and val:
            return str(val).strip()
    guid = item.get("guid") or ""
    if not guid:
        return None
    # 按 guid 前缀找 info.json
    root = SCRIPT_DIR.parent.parent.parent.parent      # → 项目根
    for p in (root / "rss" / "raw").glob("*.info.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        it = d.get("item") or {}
        if it.get("guid") == guid:
            return it.get("guid_hash8")
    return None


def already_done(state: dict, hash8: str) -> bool:
    """已转化区里是否已有这一期（判据是 note 链接里带 hash8）。"""
    for item in state.get("done", []):
        note = str((item.get("fields") or {}).get("笔记", ""))
        if hash8 and hash8 in note:
            return True
    return False


def run_one(hash8: str, dry_run: bool) -> tuple[bool, str]:
    if dry_run:
        return True, f"[dry-run] 将生成 {hash8}"
    t0 = time.time()
    r = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "generate_note_v3.py"), hash8],
        capture_output=True, text=True, timeout=3600,
    )
    out = (r.stdout + r.stderr).strip()
    tail = out.splitlines()[-1] if out else ""
    ok = r.returncode == 0
    mark = "ok" if ok else "done"     # exit 0 = 已有产物被防重跳过
    return ok, f"{hash8} [{mark}] {int(time.time()-t0)}s {tail[:90]}"


def process_state_file(state_path: Path, dry_run: bool) -> dict:
    state = sm.load_state(state_path)
    items = pick_confirmed(state)
    print(f"\n=== {state_path.name} ===")
    print(f"  确认抓取区 `[x]`：{len(items)} 期"
          f"（待确认 {len(state.get('pending', []))} / "
          f"确认不抓 {len(state.get('confirmed', [])) - len(items)} / "
          f"已转化 {len(state.get('done', []))}）")
    if not items:
        print("  ⏭️  无可处理条目，跳过（策略 A：不碰未审阅 / 不碰已排除）")
        return {"picked": 0, "ok": 0, "skip": 0}

    todo = []
    for it in items:
        h = find_hash8(it)
        if not h:
            print(f"  ⏭️  guid={str(it.get('guid'))[:12]}… 找不到 guid_hash8，跳过")
            continue
        if already_done(state, h):
            print(f"  ⏭️  {h} 已在已转化区，跳过")
            continue
        todo.append((h, it.get("title", "")[:40]))
    print(f"  待生成：{len(todo)} 期")

    ok = skip = 0
    if dry_run:
        for h, title in todo:
            print(f"  [dry-run] {h}  {title}")
        return {"picked": len(items), "ok": 0, "skip": len(items) - len(todo)}

    # 并发：LLM 环节（Henry 2026-10-05 要求 5 并发）
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as ex:
        futs = {ex.submit(run_one, h, dry_run): (h, title) for h, title in todo}
        for fu, (h, title) in futs.items():
            good, msg = fu.result()
            print(f"  {'✅' if good else '❌'} {msg}")
            ok += 1 if good else 0
            skip += 0 if good else 1
    return {"picked": len(items), "ok": ok, "skip": skip}


def main() -> int:
    global MAX_PARALLEL          # 必须在任何使用之前声明
    ap = argparse.ArgumentParser(description="阶段 3：为「确认抓取」区生成笔记")
    ap.add_argument("state_file", nargs="?", help="状态文件路径")
    ap.add_argument("--all", action="store_true", help="遍历订阅表全部源")
    ap.add_argument("--dry-run", action="store_true",
                    help="只列出将要处理的条目，不真的调 LLM")
    ap.add_argument("--max-parallel", type=int, default=MAX_PARALLEL)
    a = ap.parse_args()
    MAX_PARALLEL = a.max_parallel

    if a.all:
        root = SCRIPT_DIR.parent.parent.parent.parent
        subs = json.loads((root / "rss" / "订阅" / "subscriptions.json")
                          .read_text(encoding="utf-8"))
        files = [root / src["state_file"] for src in subs["sources"]]
    elif a.state_file:
        files = [Path(a.state_file)]
    else:
        ap.error("需要 <状态文件> 或 --all")

    print(f"策略 A：只处理「确认抓取」区 `[x]`；待确认 / 确认不抓 / 已转化 一律跳过")
    print(f"并发：{MAX_PARALLEL}（LLM 环节）{'｜dry-run，不花钱' if a.dry_run else ''}")

    total = {"picked": 0, "ok": 0, "skip": 0}
    for f in files:
        if not f.exists():
            print(f"⚠️ 状态文件不存在：{f}")
            continue
        r = process_state_file(f, a.dry_run)
        for k in total:
            total[k] += r[k]

    print(f"\n{'='*56}")
    print(f"确认抓取 {total['picked']} 期 ｜ 成功 {total['ok']} ｜ 失败 {total['skip']}")
    print("注：已转化区不会被重复处理（防重闸门 + 已转化判定）")
    return 0 if total["skip"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
