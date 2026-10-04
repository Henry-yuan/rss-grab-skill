#!/usr/bin/env python3
"""rss 侧 v3 四步管线入口 —— **薄壳**（V1.0 tasks A10 / C7，2026-10-04）。

管线本体在 `_shared/build_v3.py`（三源共用）。本文件只做 rss 专属的四件事：

1. **信头**：`_shared/build_frontmatter.py` 的 17 字段（AC-C2）
2. **落盘**：正文 + frontmatter 合成 `rss/notes/<节目名>/<标题>-<hash8>.md`
3. **索引**：`generate_note.register_index()` 追加到 `rss/notes/INDEX.md`
4. **进度日志**：订阅状态文件按 `guid_hash8` 标记完成

**D6 已拍板（2026-10-04）：长文走本管线，不再走 map-reduce。**
依据见 `docs/versions/V1.0/spec.md` 决策记录 #21/#44：4 篇同篇对照 4/4 同向，
完全覆盖口径 Δ +37.7~+79.4pp；map-reduce 的问题是「提到但讲不透」
（严格 Δ +37.7pp / 宽松仅 +11.4pp），且有结构性硬失败（见 spec #13/#19）。

用法：
    python3 scripts/generate_note_v3.py --transcript <path> --info <info.json> \\
        --output <note.md> [--work-root <dir>] [--no-typos]
    python3 scripts/generate_note_v3.py <guid_hash8>       # 自动定位
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
_SHARED = SCRIPT_DIR.parent.parent / "_shared"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(_SHARED))

from env import load_env  # noqa: E402

SCRIPTS_ENV = SCRIPT_DIR          # .env 在本目录（rss 的键名是 LLM_*）


def locate(guid_hash8: str) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path | None]:
    """按 hash8 定位 源文 / info.json / 现有笔记。"""
    root = _SHARED.parent.parent.parent      # 项目根
    t = next(iter(sorted(root.glob(f"rss/transcripts/*-{guid_hash8}.transcript.md"))), None)
    if t is None:
        raise SystemExit(f"❌ 找不到源文 *-{guid_hash8}.transcript.md")
    i = root / "rss" / "raw" / t.name.replace(".transcript.md", ".info.json")
    if not i.exists():
        raise SystemExit(f"❌ 找不到 info.json: {i}")
    n = next(iter(sorted(root.glob(f"rss/notes/**/*-{guid_hash8}.md"))), None)
    return t, i, n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("guid_hash8", nargs="?")
    ap.add_argument("--transcript", type=pathlib.Path)
    ap.add_argument("--info", type=pathlib.Path)
    ap.add_argument("--output", type=pathlib.Path)
    ap.add_argument("--work-root", type=pathlib.Path,
                    default=SCRIPT_DIR / "scan_work")
    ap.add_argument("--no-typos", action="store_true",
                    help="跳过错字纠正（仅排障用）")
    ap.add_argument("--no-index", action="store_true",
                    help="不追加到生产 rss/notes/INDEX.md（批量重生成到暂存目录时必加，"
                         "避免污染生产索引；正式切换时再统一重建）")
    ap.add_argument("--force", action="store_true",
                    help="目标笔记已存在也重新生成（默认跳过，防止白烧 LLM 调用）")
    a = ap.parse_args()

    if a.transcript and a.info:
        t, i = a.transcript, a.info
        existing = None
    elif a.guid_hash8:
        t, i, existing = locate(a.guid_hash8)
    else:
        ap.error("需要 <guid_hash8> 或 --transcript + --info")

    if not a.output:
        a.output = existing or (SCRIPT_DIR / f"d3out-{t.stem}.md")

    # 断点续跑闸门下推到脚本最前面（V1.0 A8，2026-10-04）。
    # 批量驱动两个进程从列表两端往中间跑，但任务在启动时就全排进了线程池，
    # 驱动侧的 skip 判定发生在 worker **领任务那一刻**——两侧都判"还没有"，
    # 同一篇被跑两遍、白烧两轮 LLM。只有在这里判才拦得住后到的一方。
    if not a.force and a.output.exists() and a.output.stat().st_size > 500:
        print(f"⏭️ 已有产物，跳过：{a.output.name} ({a.output.stat().st_size:,} bytes)")
        return 0

    load_env(SCRIPTS_ENV)          # .env 在本目录；缺 MINIMAX/LLM 键会报错
    if not os.environ.get("LLM_API_KEY") and not os.environ.get("MINIMAX_API_KEY"):
        print("❌ 未配置 API key（scripts/.env）", file=sys.stderr)
        return 1

    import build_v3 as shared
    import build_frontmatter as fm
    from generate_note import register_rss_index
    from openai import OpenAI

    # 信头交给共享模块（info_for_prompt 按此白名单取）
    shared.PROMPT_INFO_FIELDS = fm.PROMPT_INFO_FIELDS

    key = os.environ.get("MINIMAX_API_KEY") or os.environ["LLM_API_KEY"]
    base = os.environ.get("MINIMAX_BASE_URL") or os.environ.get("LLM_BASE_URL") \
        or shared.BASE_URL
    client = OpenAI(api_key=key, base_url=base, timeout=shared.TIMEOUT)

    info = json.loads(i.read_text(encoding="utf-8"))
    t0 = time.time()

    print(f"▶ {t.name[:50]}  净字 {shared.net_len(t.read_text(encoding='utf-8')):,}")
    body, report, stats = shared.generate_note_body(
        t, info, client, a.work_root, SCRIPT_DIR / "typos")

    # 落盘：信头 + 正文
    llm = stats.get("llm") or {}          # tags / summary 在 LLM 的 JSON 里
    fmv = fm.build(info, summary_llm=llm.get("summary", ""),
                   tags_llm=llm.get("tags", []) or [])
    a.output.parent.mkdir(parents=True, exist_ok=True)
    # 原子落盘：先写同目录临时文件再 os.replace。
    # write_text() 是 open('w')——先截断再逐块写；两进程同时写同一路径会各自截断、
    # 交错落盘，产出「半截 A + 半截 B」的撕裂文件且**不报错**。
    # os.replace 在同一文件系统内是原子的，读者只可能看到完整旧版或完整新版。
    tmp = a.output.with_name(f"{a.output.name}.tmp{os.getpid()}")
    try:
        tmp.write_text(fm.dump(fmv) + "\n" + body + "\n", encoding="utf-8")
        os.replace(tmp, a.output)
    finally:
        if tmp.exists():
            tmp.unlink()

    # 索引（--no-index 时跳过：批量重生成到暂存目录不该碰生产 INDEX.md）
    src = "skipped"
    if not a.no_index:
        try:
            src = register_rss_index(info, a.output)
        except Exception as e:  # noqa: BLE001
            print(f"   ⚠️ 索引追加失败（不影响笔记）: {type(e).__name__}: {e}")

    print(f"✅ {a.output}")
    print(f"   路由={stats['route']} 净字={stats['net_chars']:,} "
          f"正文={len(body):,} 纠错={stats.get('typo_applied', 0)} 处 "
          f"用时={time.time() - t0:.0f}s 索引={src}")

    # 进度日志：按 guid_hash8 标记完成（与订阅流程对齐）
    if a.guid_hash8 and not a.no_index:      # 暂存批次不标记订阅状态
        try:
            from subscribe_manager import mark_done
            mark_done(a.guid_hash8)
        except Exception:
            pass          # 状态标记失败不该让生成失败
    return 0


if __name__ == "__main__":
    sys.exit(main())
