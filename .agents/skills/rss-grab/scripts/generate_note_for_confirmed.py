#!/usr/bin/env python3
"""阶段 3：为「确认抓取」区的条目补齐「音频 → 转写 → 笔记」整条链路（T3.3 / AC-A3）。

## 三步链路

`generate_note_v3.py` 靠 `rss/transcripts/*-<hash8>.transcript.md` 定位源文，缺源文直接退出。
但订阅模式下的定时任务只跑了阶段 1（`--fetch-updates`：拉摘要 + 写状态文件），
音频和 ASR 两步并不在链路里 —— 只勾了 `[x]` 而没单独跑过阶段 2 的期数，
到这里会因为找不到源文而失败。所以本脚本自己把三步串起来：

| 步骤 | 做什么 | 「已做过」的判据 | 复用 |
|---|---|---|---|
| 0 补拉 | `rss/raw/` 没有 info.json 时，从 feed 补这一期的元数据 | `rss/raw/*-<hash8>.info.json` 存在 | `fetch_rss_feed.build_info_json` |
| 1 音频 | 音频没落盘就下载 | `local.audio_path` 指向的文件存在且非零字节 | `fetch_rss_feed.download_audio` |
| 2 转写 | ASR 转写 | `rss/transcripts/*-<hash8>.transcript.md` 存在 | `asr_podcast.transcribe_one` |
| 3 笔记 | v3 四步管线出笔记 | `generate_note_v3.py` 自带防重闸门 | 子进程调 `generate_note_v3.py` |

**步骤 0 是必需项，不是可选优化**：阶段 1 只拉简介做摘要、从不碰音频，
所以订阅清单里绝大多数期数从来没有过 `info.json`。缺了这一步，
后面三步对它们全部无从下手（详见 `ensure_info_json` 的说明）。

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
import derive_rss_id  # noqa: E402

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

    三条路，按「最可靠」排序：
      1. `fields` 里的 guid_hash8 —— **状态文件渲染时落盘**（subscribe_manager
         `_render_item`），这是主路径，不用算、也不用反查；
      2. 由 guid 现算 sha256 前 8 位 —— 兜底。派生是纯函数且已与 info.json
         逐条核对一致，所以老文件/手改过的文件也能算出来；
      3. 扫 `rss/raw/*.info.json` 按 guid 反查 —— 最后的兜底（慢，580 条要扫
         几百个文件），保留是为了兼容字段名不是 guid_hash8 的历史文件。

    ⚠️ 原实现在第 1、3 条都拿不到时返回 None，调用方直接跳过。于是
    「md 里没落这个字段 且 raw/ 里没这期」= **静默丢弃**：秒返回、退出码 0、
    日志一行「找不到 guid_hash8，跳过」。实测 580 条里 528 条会走到这里，
    而当时 stage 3 还是新接进定时任务的 —— 看起来一切正常，一条笔记没生成。
    """
    for key, val in (item.get("fields") or {}).items():
        if "hash8" in key.lower() and val:
            return str(val).strip()
    guid = item.get("guid") or ""
    if not guid:
        return None
    # 兜底 2：guid 就在条目里，sha256 前 8 位是纯函数，直接算。
    calc = derive_rss_id.guid_to_hash8(guid)
    # 兜底 3：反查 info.json（字段名不是 guid_hash8 的历史文件）
    root = SCRIPT_DIR.parent.parent.parent.parent      # → 项目根
    for p in (root / "rss" / "raw").glob("*.info.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        it = d.get("item") or {}
        if it.get("guid") == guid:
            return it.get("guid_hash8") or calc
    return calc


def already_done(state: dict, hash8: str) -> bool:
    """已转化区里是否已有这一期（判据是笔记链接里带 hash8）。

    ⚠️ 判据要读 **note_path**，不能只读 fields["笔记"]。
    `subscribe_manager` 解析状态文件时会把 `- 笔记：…` 还原进
    `item["note_path"]` 并**明确不进 fields**（见其「笔记字段还原到
    note_path（不进 fields）」那段）。而这里原来读的是 fields["笔记"]，
    于是对**全部 51 个真实已转化条目都返回 False** —— 这道防重闸门
    从上线起就是空的。
    之所以没暴露：pick_confirmed 本来就只扫 confirmed 区里的 `[x]`，
    已转化条目在另一个区，两者不重叠，这道闸门一直是多余的冗余保险。
    但一旦有人拿它当「这期到底做完没」的唯一判据（就像标 done 的逻辑），
    空闸门就变成静默重复处理。
    """
    if not hash8:
        return False
    for item in state.get("done", []):
        blob = " ".join((
            str(item.get("note_path") or ""),
            str((item.get("fields") or {}).get("笔记", "")),
        ))
        if hash8 in blob:
            return True
    return False


# 「产物够格才算完成」的字节下限。
# 依据：本仓现有 52 篇笔记实测，最小 11845 / 中位 28434 / 最大 56407 字节，
# 无一低于 1000。取 1000 是**远低于**真实下限的保守值 —— 只为拦空文件、
# 半截写入、异常退出的残骸，不会误伤任何正常笔记。
# ⚠️ 这个数不是随手拍的：调高了会把正常笔记判成未完成（于是无限重试、
# 永远标不上 done），调低了会让垃圾产物混进已转化区。
NOTE_MIN_BYTES = 1000


def find_note_path(hash8: str) -> Path | None:
    """按 hash8 找已落盘的笔记：rss/notes/<节目>/<标题>-<hash8>.md。"""
    if not hash8:
        return None
    root = SCRIPT_DIR.parent.parent.parent.parent      # → 项目根
    notes = root / "rss" / "notes"
    # ⚠️ 两个坑都在这里踩过：
    #   ① glob 模式不能以 / 开头（绝对路径会抛 NotImplementedError）；
    #   ② `notes.glob("*-xxx.md")` **不递归子目录** —— 笔记都在
    #      `rss/notes/<节目名>/` 下，所以目录层必须写 `*/`；
    #   ③ 文件名是 `<标题>-<hash8>.md`，不是裸 `<hash8>.md`，
    #      所以文件名层要用前缀通配 `*` 而不是精确名。
    for pattern in (f"*/*{hash8}.md", f"*{hash8}.md"):
        for p in notes.glob(pattern):
            return p
    return None


def should_mark_done(hash8: str) -> tuple[bool, str]:
    """产物是否够格把这一期标成已转化。

    🔴 **判据是「文件真的躺在盘上且够大」，不是「生成脚本退出码是 0」**。
    退出码只说明进程没崩；文件被写空、写半截、或写到别处，退出码照样是 0。
    Henry 2026-10-06 明确要求：生成失败绝不能标 done。
    """
    p = find_note_path(hash8)
    if p is None:
        return False, "笔记文件未落盘"
    try:
        n = p.stat().st_size
    except OSError as e:
        return False, f"笔记文件读不到：{e}"
    if n < NOTE_MIN_BYTES:
        return False, f"笔记只有 {n} 字节（< {NOTE_MIN_BYTES}），判为未完成"
    return True, f"笔记已落盘 {n} 字节"


def mark_done(state_path: Path, hash8: str, note_rel: str) -> bool:
    """确保这一期在 md 里处于 `[done]`，并挂上笔记链接。返回目标状态是否达成。

    只在 `should_mark_done` 通过后调用。**不改分区** —— 归区由
    `fetch_rss_feed.py --sync-pick` 统一做（别处已有实现，不重复造）。
    走 sm.load_state/save_state，与 --sync-pick 同一套读写约定
    （临时文件 + replace 原子替换）。

    🔴 **必须连「已转化」区一起找，且已达成时不要重写文件。**
    真实跑一轮才暴露的问题：这一期**通常已经被子进程标好了** ——
    `generate_note_v3.py` 内部有自己的 `mark_subscribe_done`，跑完就把条目
    移进「已转化」区（它会打一行 `订阅状态=已标记 <文件名>`）。
    而本函数原来只扫 `confirmed` / `pending` 两个区，于是：
      ① 条目已不在这两个区 → 找不到 → 返回 False → 汇总报「本轮标 done：0 期」，
         而实际状态文件是对的 —— **日志在说谎**，看日志的人会以为标记功能坏了；
      ② 更实质的是，「先验证产物够格、再标 done」这道保险被打散了：
         子进程已经标完了，外层这道复核来得太晚，挡不住任何东西。
    改成三区都扫 + 幂等之后，本函数退化为「兜底 + 复核」：
    子进程没标（`--no-index`、状态文件找不到等）时由它补上，
    已经标好了就只确认、不重复写盘。
    """
    try:
        state = sm.load_state(state_path)
    except Exception as e:
        print(f"  ⚠️  {hash8} 读状态文件失败，未标 done：{e}"[:200])
        return False
    hit = False
    changed = False
    for zone in ("confirmed", "pending", "done"):
        for item in state.get(zone, []):
            if find_hash8(item) != hash8:
                continue
            hit = True
            if item.get("checkbox") == DONE:
                continue          # 已是目标状态，不重复写
            item["checkbox"] = DONE
            # 🔴 笔记链接要写进 **item["note_path"]**，不是 fields["笔记"]。
            # 渲染器 `_render_item` 是从 note_path 出「- 笔记：…」这一行的；
            # 写进 fields 会被**静默丢掉**（渲染时只按固定字段表取值），
            # 于是状态是「标了 done、却查不到笔记」——而防重判重
            # already_done 正是靠这个链接里的 hash8，下一轮就会重复处理。
            # （读回来时 FIELD_RE 才会把它解析进 fields["笔记"]，所以只读侧用 fields。）
            item["note_path"] = note_rel
            fields = item.setdefault("fields", {})
            fields["笔记"] = note_rel
            changed = True
    if not hit:
        return False
    if not changed:
        return True               # 已达成，别把状态文件白重写一遍
    try:
        sm.save_state(state_path, state)
    except Exception as e:
        print(f"  ⚠️  {hash8} 写状态文件失败，未标 done：{e}"[:200])
        return False
    return True


def find_info_json(hash8: str) -> Path | None:
    """rss/raw/ 下这一期的 info.json（找不到返回 None）。"""
    return next(iter(sorted(RAW_DIR.glob(f"*-{hash8}.info.json"))), None)


# ── 步骤 0：从 feed 补拉 info.json ──
# 🔴 这一步是阶段 3 能不能落地的前提，不是可选优化。
# 580 期订阅清单里 **528 期从未下载过音频**（`rss/raw/` 只有 52 份 info.json），
# 因为阶段 1（--fetch-updates）只拉简介做摘要、从不碰音频。
# 于是 `find_info_json` 对绝大多数条目返回 None，原实现在 `run_one` 里直接
# `return False, "音频", "找不到 info.json"` —— 定时任务每晚跑、每晚失败、
# 退出码非 0，但一篇笔记也没产出过。**这就是为什么必须补这一步，而不是
# 把失败当成"这期有问题"。**

# feed 缓存：一次运行内同一个源只拉一次 XML。
# 不用的话，勾 78 期 `[x]` 就会打 78 次 feed（小xyzfm 站也扛不住这种礼貌）。
_FEED_CACHE: dict[str, dict] = {}


def _load_feed(feed_url: str) -> dict:
    """取 feed（含缓存）。失败抛异常，由调用方转成失败原因。"""
    with _LOCAL_LOCK:          # 同一个源只拉一次：并发线程别重复打 feed
        if feed_url not in _FEED_CACHE:
            from fetch_rss_feed import fetch_xml
            import parse_rss
            _FEED_CACHE[feed_url] = parse_rss.parse_text(fetch_xml(feed_url))
    return _FEED_CACHE[feed_url]


def ensure_info_json(hash8: str, guid: str, feed_url: str) -> tuple[Path | None, str]:
    """保证 `rss/raw/<标题>-<hash8>.info.json` 存在，缺则从 feed 补这一期。

    **只负责落 info.json，不下载音频** —— 下载仍由 `ensure_audio` 负责。
    这样拆的好处：下载失败时 info.json 已经在盘上，下一轮
    `find_info_json` 直接命中、只重试下载，不会重新去 feed 里捞这一期。

    `build_info_json` / `sanitize_title` / `ext_from_enclosure` / `TMP_AUDIO_DIR`
    全部复用 `fetch_rss_feed` 的实现 —— 命名规则必须和阶段 1 逐字节一致，
    否则 `find_info_json` 的 glob、ASR 的产物名、笔记文件名三处会对不上。
    """
    exist = find_info_json(hash8)
    if exist is not None:
        return exist, "info.json 已存在"
    if not guid:
        return None, "条目里没有 guid，无法从 feed 反查"
    if not feed_url:
        return None, "状态文件 frontmatter 没有 feed_url，无法补拉"
    try:
        feed = _load_feed(feed_url)
    except Exception as e:
        return None, f"拉 feed 失败：{type(e).__name__}: {e}"[:200]
    item = next((it for it in feed.get("items", []) if it.get("guid") == guid), None)
    if item is None:
        # feed 通常只保留最近若干期，清单里的老期可能已被源站丢弃。
        # 这是**真限制**，必须如实报出来，不能伪装成"这期有问题"。
        return None, (f"feed 里已没有这一期（{len(feed.get('items', []))} 期里找不到）"
                      f"—— 源站可能已过保留期")
    if not (item.get("enclosure") or {}).get("url"):
        return None, "feed 里这一期没有音频 enclosure"
    from fetch_rss_feed import (TMP_AUDIO_DIR, build_info_json,
                                ext_from_enclosure, sanitize_title)
    safe = sanitize_title(item["title"])
    ext = ext_from_enclosure(item["enclosure"].get("type", ""))
    audio_path = TMP_AUDIO_DIR / f"{safe}-{hash8}.{ext}"
    info = build_info_json(feed, item, hash8, audio_path)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    out = RAW_DIR / f"{safe}-{hash8}.info.json"
    try:
        out.write_text(json.dumps(info, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    except OSError as e:
        return None, f"info.json 落盘失败：{e}"[:200]
    return out, f"已从 feed 补 info.json（{out.name}）"


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


def run_one(hash8: str, dry_run: bool, guid: str = "",
            feed_url: str = "") -> tuple[bool, str, str]:
    """跑完一期的完整链路。返回 (是否成功, 卡在哪一步, 日志行)。

    `stage` 取值：`完成` / `补拉` / `音频` / `转写` / `笔记` / `预演`，
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
            # 没有 info.json 不是终局：从 feed 把这一期补出来再继续。
            info_path, msg = ensure_info_json(hash8, guid, feed_url)
            if info_path is None:
                return False, "补拉", f"{hash8} {msg}"
            steps.append(msg)
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
        return {"picked": 0, "ok": 0, "fail": 0, "marked": 0, "skip": 0}

    todo = []
    need_fetch = 0
    for it in items:
        h = find_hash8(it)
        if not h:
            print(f"  ⏭️  guid={str(it.get('guid'))[:12]}… 找不到 guid_hash8，跳过")
            continue
        if already_done(state, h):
            print(f"  ⏭️  {h} 已在已转化区，跳过")
            continue
        if find_info_json(h) is None:
            need_fetch += 1
        todo.append((h, it.get("title", "")[:40], it.get("guid") or ""))
    print(f"  待生成：{len(todo)} 期"
          + (f"（其中 {need_fetch} 期需先从 feed 补拉音频）" if need_fetch else ""))

    if dry_run:
        for h, title, _g in todo:
            print(f"  [dry-run] {h}  {title}")
        return {"picked": len(items), "ok": 0, "fail": 0, "marked": 0,
                "skip": len(items) - len(todo)}

    # 并发：LLM 环节（Henry 2026-10-05 要求 5 并发）
    from concurrent.futures import ThreadPoolExecutor
    ok = fail = marked = 0
    by_stage: dict[str, int] = {}
    feed_url = str((state.get("frontmatter") or {}).get("feed_url") or "")
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as ex:
        futs = {ex.submit(run_one, h, dry_run, g, feed_url): (h, title)
                for h, title, g in todo}
        results = []
        for fu, (h, title) in futs.items():
            good, stage, msg = fu.result()
            results.append((h, good, stage, msg))
            print(f"  {'✅' if good else '❌'} {msg}")
            if good:
                ok += 1
            else:
                fail += 1
                by_stage[stage] = by_stage.get(stage, 0) + 1

    # 成功之后**再验一次产物**，够格才标 done。
    # 为什么不直接信 run_one 的 good：它判的是「子进程退出码为 0」，
    # 而 v3 脚本崩在最后一刻、或文件写空，退出码都可能是 0。
    # 这一步独立复核磁盘上的真东西 —— 判据是文件存在且够大（见 should_mark_done）。
    if not dry_run:
        for h, good, _stage, _msg in results:
            if not good:
                continue
            passed, why = should_mark_done(h)
            if not passed:
                print(f"  ⚠️  {h} 生成返回成功但{why} → **不标 done**，下次重试")
                continue
            rel = Path(find_note_path(h)).relative_to(
                SCRIPT_DIR.parent.parent.parent.parent).as_posix()
            if mark_done(state_path, h, rel):
                marked += 1
                print(f"  🏷️  {h} 产物达标（{why}）→ 已转化")

    if by_stage:
        detail = "、".join(f"{k} {v} 期" for k, v in sorted(by_stage.items()))
        print(f"  ⚠️  失败分布：{detail}")
    print(f"  🏷️  本轮确认已转化：{marked} 期")
    return {"picked": len(items), "ok": ok, "fail": fail, "marked": marked,
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

    total = {"picked": 0, "ok": 0, "fail": 0, "marked": 0, "skip": 0}
    for f in files:
        if not f.exists():
            print(f"⚠️ 状态文件不存在：{f}")
            continue
        r = process_state_file(f, a.dry_run)
        for k in total:
            total[k] += r[k]

    print(f"\n{'='*56}")
    print(f"确认抓取 {total['picked']} 期 ｜ ✅ 完成 {total['ok']} "
          f"｜ ❌ 失败 {total['fail']} ｜ 🏷️ 已转化 {total['marked']}"
          f" ｜ ⏭️ 跳过 {total['skip']}")
    print("注：每期按 音频 → 转写 → 笔记 依次补齐；已有产物自动跳过对应步骤")
    print("注：已转化区不会被重复处理（防重闸门 + 已转化判定）")
    return 0 if total["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
