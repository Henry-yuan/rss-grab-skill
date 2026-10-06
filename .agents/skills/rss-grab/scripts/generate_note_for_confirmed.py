#!/usr/bin/env python3
"""阶段 3：为「确认抓取」区的条目补齐「音频 → 转写 → 笔记」整条链路（T3.3 / AC-A3）。

## 三步链路

`generate_note_v3.py` 靠 `rss/transcripts/*-<hash8>.transcript.md` 定位源文，缺源文直接退出。
但订阅模式下的定时任务只跑了阶段 1（`--fetch-updates`：拉摘要 + 写状态文件），
音频和 ASR 两步并不在链路里 —— 只勾了 `[x]` 而没单独跑过阶段 2 的期数，
到这里会因为找不到源文而失败。所以本脚本自己把三步串起来：

| 步骤 | 做什么 | 「已做过」的判据 | 复用 |
|---|---|---|---|
| 1 音频 | 音频没落盘就下载 | `local.audio_path` 指向的文件存在且非零字节 | `fetch_rss_feed.download_audio` |
| 2 转写 | ASR 转写 | `rss/transcripts/*-<hash8>.transcript.md` 存在 | `asr_podcast.transcribe_one` |
| 3 笔记 | v3 四步管线出笔记 | `generate_note_v3.py` 自带防重闸门 | 子进程调 `generate_note_v3.py` |

每步各自判重，重跑不会重复下载、不会重复烧 ASR、不会重复烧 LLM 额度。
步骤 2 直接调函数而不是起子进程：`asr_podcast.main()` 无论成败都返回 None（退出码恒为 0），
拿退出码判断会把转写失败当成成功；函数版直接给 (ok, 原因) 原因码。
转写时长校验与内容可信度校验都在 `transcribe_one` 内部，本脚本不再重复实现。

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

三层判重（见上表）叠加：转写稿在就跳过音频+转写，笔记产物在就由
`generate_note_v3.py` 的防重闸门跳过。所以重复调用本脚本不会重复下载、
不会重复烧 ASR、也不会重复消耗额度、不会重复追加 INDEX。

## 失败可定位

`run_one` 返回卡住的那一步（`音频` / `转写` / `笔记`），日志与汇总按步分开计数，
不用翻子进程原始输出才知道是下载挂了还是 ASR 挂了。

## 用法

    python3 generate_note_for_confirmed.py rss/订阅/<节目名>.md [--dry-run]
    python3 generate_note_for_confirmed.py --all           # 遍历订阅表全部源
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
# 本模块要 import fetch_rss_feed（取 download_audio），而那个模块在导入期会去
# 找 `_shared` 下的 note_worker。先把**真实**的 _shared 位置放上 sys.path，
# 本模块的导入就不依赖「谁先被 import」这种调用顺序。
sys.path.insert(0, str(SCRIPT_DIR.parent.parent / "_shared"))

import subscribe_manager as sm  # noqa: E402

GRAB = sm.CHECKBOX_GRAB          # "[x]"
DONE = sm.CHECKBOX_DONE         # "[done]"

PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent.parent
RAW_DIR = PROJECT_ROOT / "rss" / "raw"
TRANSCRIPTS_DIR = PROJECT_ROOT / "rss" / "transcripts"

# 本地步骤（音频下载 + ASR 转写）必须**全局串行**，与 AGENTS.md 的约定一致：
# 「本地一律串行（ASR/转码/下载），LLM 环节才允许并发」。
# 此前 --max-parallel 同时作用在 ASR 上，5 路 mlx-whisper 会把大模型各装一份
# 进内存，OOM 风险实在；且 Apple Silicon 的 GPU 本就被单个 ASR 占满，并发并不
# 更快。用 RLock 而非 Lock：转写内部若回调到下载路径也不会自锁。
_LOCAL_LOCK = threading.RLock()

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


def find_info_json(hash8: str) -> Path | None:
    """rss/raw/ 下这一期的 info.json（找不到返回 None）。"""
    return next(iter(sorted(RAW_DIR.glob(f"*-{hash8}.info.json"))), None)


def enclosure_url(info: dict) -> str:
    """从 info.json 取音频地址。

    `item.enclosure` 实际写出的是单个 dict，按 list 形态一并兼容。挑选顺序：
    ① type 以 audio/ 开头 ② type 不是 image/ ③ 任意有 url 的。
    ② 存在的意义：不少 feed 的 enclosure 不带 type，若直接取列表首个，
    拿到的是封面图地址 —— 把 jpg 当音频下，ASR 才会以看不懂的方式失败。
    """
    enc = (info.get("item") or {}).get("enclosure")
    entries = enc if isinstance(enc, list) else [enc]
    urls = [(str(e.get("url") or "").strip(), str(e.get("type") or ""))
            for e in entries if isinstance(e, dict)]
    urls = [(u, t) for u, t in urls if u]
    for pred in (lambda t: t.startswith("audio/"),
                 lambda t: not t.startswith("image/"),
                 lambda t: True):
        for u, t in urls:
            if pred(t):
                return u
    return ""


def audio_path_of(info: dict) -> Path | None:
    """info.json 里约定的音频落盘路径（`local.audio_path`）。"""
    p = (info.get("local") or {}).get("audio_path")
    return Path(p) if p else None


# ── 下面 4 个薄壳是「本脚本 ↔ 已有实现」的唯一接缝 ──
# 延迟 import（不让本模块的导入拖上 _shared/ASR 依赖），同时让测试能替换接缝，
# 不必真的下载或转写。转写这一步特意不按子进程调 `asr_podcast.py`：
# 它的 main() 无论成败都返回 None，退出码恒为 0，拿退出码判成败会把
# 转写失败读成成功。函数版直接给 (ok, 原因码)。

def _download(url: str, save_path: Path) -> tuple[bool, str]:
    from fetch_rss_feed import download_audio
    return download_audio(url, save_path)


def _transcribe(info_path: Path, transcripts_dir: Path) -> tuple[bool, str]:
    from asr_podcast import transcribe_one
    return transcribe_one(info_path, transcripts_dir, force=False)


def _transcript_exists(hash8: str) -> bool:
    from asr_podcast import transcript_exists
    return transcript_exists(TRANSCRIPTS_DIR, hash8)


def ensure_audio(info: dict) -> tuple[bool, str]:
    """步骤 1：保证音频已落盘。已存在且非零字节则跳过下载。"""
    url = enclosure_url(info)
    if not url:
        return False, "info.json 的 enclosure 里没有音频地址"
    audio = audio_path_of(info)
    if audio is None:
        return False, "info.json 的 local.audio_path 为空，无法确定落盘位置"
    if audio.is_file() and audio.stat().st_size > 0:
        return True, f"音频已就位（{audio.name}）"
    audio.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _LOCAL_LOCK:
            ok, err = _download(url, audio)
    except Exception as e:
        return False, f"下载调用异常：{type(e).__name__}: {e}"[:200]
    if not ok:
        return False, f"yt-dlp 下载失败：{err}"[:200]
    # yt-dlp 退出 0 不等于真的落盘了 —— 再验一次文件确实存在且非零字节。
    if not audio.is_file():
        return False, f"yt-dlp 退出 0 但目标文件不存在：{audio.name}"
    if audio.stat().st_size == 0:
        return False, f"下载到零字节文件：{audio.name}"
    return True, f"已下载 {audio.name}（{audio.stat().st_size // 1024 // 1024}MB）"


def ensure_transcript(hash8: str, info_path: Path) -> tuple[bool, str]:
    """步骤 2：调转写。时长校验、内容可信度校验都在 asr_podcast 内部。"""
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with _LOCAL_LOCK:
            ok, msg = _transcribe(info_path, TRANSCRIPTS_DIR)
    except Exception as e:
        return False, f"转写调用异常：{type(e).__name__}: {e}"[:200]
    if not ok:
        if msg == "skip_exists":
            return True, "转写稿已存在（并发下由其他进程写出）"
        return False, f"转写未落盘：{msg}"[:200]
    if not _transcript_exists(hash8):
        return False, "转写函数返回成功但产物不存在"
    return True, f"转写完成（{msg}）"


def generate_note(hash8: str) -> tuple[bool, str]:
    """步骤 3：v3 四步管线出笔记（自带防重闸门，重复调用不重复烧额度）。"""
    t0 = time.time()
    try:
        r = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "generate_note_v3.py"), hash8],
            capture_output=True, text=True, timeout=3600,
        )
    except subprocess.TimeoutExpired:
        return False, "笔记生成超时（1 小时）"
    except Exception as e:
        return False, f"调用异常：{type(e).__name__}: {e}"[:200]
    out = (r.stdout + r.stderr).strip()
    tail = out.splitlines()[-1] if out else ""
    ok = r.returncode == 0
    return ok, (f"{'ok' if ok else 'failed'} {int(time.time()-t0)}s {tail[:90]}")


def run_one(hash8: str, dry_run: bool) -> tuple[bool, str, str]:
    """跑完一期的完整链路。返回 (是否成功, 卡在哪一步, 日志行)。

    `stage` 取值：`完成` / `音频` / `转写` / `笔记` / `预演`，
    失败时一眼能看出断在哪一步，不用翻子进程原始输出。
    """
    if dry_run:
        return True, "预演", f"{hash8} [dry-run] 将依次补齐 音频 → 转写 → 笔记"
    steps: list[str] = []
    if _transcript_exists(hash8):
        steps.append("转写稿已存在")
    else:
        info_path = find_info_json(hash8)
        if info_path is None:
            return False, "音频", f"{hash8} 找不到 rss/raw/*-{hash8}.info.json"
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return False, "音频", f"{hash8} info.json 读取失败：{e}"[:200]
        ok, msg = ensure_audio(info)
        if not ok:
            return False, "音频", f"{hash8} {msg}"
        steps.append(msg)
        ok, msg = ensure_transcript(hash8, info_path)
        if not ok:
            return False, "转写", f"{hash8} {msg}"
        steps.append(msg)
    ok, msg = generate_note(hash8)
    if not ok:
        return False, "笔记", f"{hash8} {msg}"
    steps.append(f"笔记 {msg}")
    return True, "完成", f"{hash8} " + " ｜ ".join(steps)


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
        return {"picked": 0, "ok": 0, "fail": 0, "skip": 0}

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

    if dry_run:
        for h, title in todo:
            print(f"  [dry-run] {h}  {title}")
        return {"picked": len(items), "ok": 0, "fail": 0,
                "skip": len(items) - len(todo)}

    # 并发：LLM 环节（Henry 2026-10-05 要求 5 并发）
    from concurrent.futures import ThreadPoolExecutor
    ok = fail = 0
    by_stage: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as ex:
        futs = {ex.submit(run_one, h, dry_run): (h, title) for h, title in todo}
        for fu, (h, title) in futs.items():
            good, stage, msg = fu.result()
            print(f"  {'✅' if good else '❌'} {msg}")
            if good:
                ok += 1
            else:
                fail += 1
                by_stage[stage] = by_stage.get(stage, 0) + 1
    if by_stage:
        detail = "、".join(f"{k} {v} 期" for k, v in sorted(by_stage.items()))
        print(f"  ⚠️  失败分布：{detail}")
    return {"picked": len(items), "ok": ok, "fail": fail,
            "skip": len(items) - len(todo)}


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

    total = {"picked": 0, "ok": 0, "fail": 0, "skip": 0}
    for f in files:
        if not f.exists():
            print(f"⚠️ 状态文件不存在：{f}")
            continue
        r = process_state_file(f, a.dry_run)
        for k in total:
            total[k] += r[k]

    print(f"\n{'='*56}")
    print(f"确认抓取 {total['picked']} 期 ｜ ✅ 完成 {total['ok']} "
          f"｜ ❌ 失败 {total['fail']} ｜ ⏭️ 跳过 {total['skip']}")
    print("注：每期按 音频 → 转写 → 笔记 依次补齐；已有产物自动跳过对应步骤")
    print("注：已转化区不会被重复处理（防重闸门 + 已转化判定）")
    return 0 if total["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
