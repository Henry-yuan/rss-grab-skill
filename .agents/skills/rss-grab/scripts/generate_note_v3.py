#!/usr/bin/env python3
"""rss 侧 v3 四步管线入口 —— **薄壳**（V1.0 tasks A10 / C7，2026-10-04）。

管线本体在 `_shared/build_v3.py`（三源共用）。本文件只做 rss 专属的四件事：

1. **信头**：`_shared/build_frontmatter.py` 的 17 字段（AC-C2）
2. **落盘**：正文 + frontmatter 合成 `rss/notes/<节目名>/<标题>-<hash8>.md`
3. **索引**：`_shared/append_index.register_rss_index()` 追加到 `rss/notes/INDEX.md`
   （V1.1 T2.3 从 v2 的 `generate_note.py` 抽到共享模块，v2 侧只留 re-export）
4. **进度日志**：订阅状态文件里对应期标 `[done]` 移入「已转化」区
   （`state_path` 与完整 guid 都从 info.json 反查，见 `mark_subscribe_done`）

**D6 已拍板（2026-10-04）：长文走本管线，不再走 map-reduce。**
依据见 `docs/versions/V1.0/spec.md` 决策记录 #21/#44：4 篇同篇对照 4/4 同向，
完全覆盖口径 Δ +37.7~+79.4pp；map-reduce 的问题是「提到但讲不透」
（严格 Δ +37.7pp / 宽松仅 +11.4pp），且有结构性硬失败（见 spec #13/#19）。

用法：
    python3 scripts/generate_note_v3.py --transcript <path> --info <info.json> \\
        --output <note.md> [--work-root <dir>] [--no-typos]
    python3 scripts/generate_note_v3.py <guid_hash8>       # 自动定位
    python3 scripts/generate_note_v3.py --backfill-index <hash8,...>   # 只补 INDEX 登记
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import re
import sys
import time

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
_SHARED = SCRIPT_DIR.parent.parent / "_shared"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(_SHARED))

from env import load_env  # noqa: E402

# 打标接线：笔记落盘后自动补跑 scripts/retag.py。
# import 本模块只做 subprocess 调脚本，不把 retag / openai 拖进本进程，
# 因此不会给正文生成引入额外的 import 期副作用。
#
# ⚠️ 仓根必须按 `.agents` 这个目录名反推，不能用 `_SHARED.parent.parent`
# —— 后者会因本脚本被软链到别处（~/.agents/skills/…）而指向错误的目录，
# 于是 `scripts/` 找不到、import 期就 ModuleNotFoundError，
# 连 `--help` 都跑不起来。
_REPO_ROOT = next((p for p in [SCRIPT_DIR, *SCRIPT_DIR.parents]
                   if p.name == ".agents"), None)
_REPO_ROOT = (_REPO_ROOT.parent if _REPO_ROOT else
              SCRIPT_DIR.parent.parent.parent.parent)
if str(_REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))
try:
    from retag_hook import auto_retag
except ImportError:      # noqa: BLE001
    # 打标接线缺失**不能**让正文生成整个不可用（本脚本还得能被 --help 探测、
    # 被批量入口当子进程调用）。降级成「不自动打标」，由既有的体检告警兜。
    def auto_retag(source, ids, apply=True):       # noqa: ANN001, ANN201
        print("⚠️ 未找到 retag_hook，自动打标已跳过（不影响正文产出）；"
              "标签留空，下次体检会被 G1 告警抓出来。", flush=True)
        return False

RETAG_SOURCE = "rss"


def _prod_notes_dir() -> pathlib.Path:
    """生产笔记目录 `rss/notes`。

    抽成函数而不是内联表达式：打标闸门要判断「产物是否落在生产目录」，
    单测需要把这个基准换到 tmp 下。内联的话单测只能改模块级常量，
    与生产代码的实际推导脱节，等于没测到闸门本身。
    """
    return _SHARED.parent.parent / "rss" / "notes"

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


# ── 索引补登记（--backfill-index）───────────────────────────────
# 存在的理由：批量生成时用 --no-index 绕开「多进程并发写同一个 INDEX.md」的
# 互相覆盖，代价是跑完索引缺一堆登记，得人工补。这个入口只做这一件事：
# 读**已存在**的笔记 + info.json → 把缺登记的行补进 INDEX.md。
# 不调 LLM、不跑 ASR、不下载、不改正文、不建无用的空 INDEX。
BACKFILL_HASH_RE = re.compile(r"^[0-9a-f]{8}$")


def _project_root(explicit: str | None = None) -> pathlib.Path:
    """项目根：显式参数 > 环境变量 > 本仓 checkout 路径（find_project_root 的口径）。"""
    if explicit:
        return pathlib.Path(explicit)
    from paths import find_project_root
    return find_project_root()


@contextlib.contextmanager
def _project_root_env(root: pathlib.Path):
    """把项目根同步给「按环境变量定位」的代码，写完立刻还原。

    索引写入函数内部就是按环境变量（或本仓路径）查抓取时间的：显式给了项目根却
    不同步，日期列会取到另一份数据的抓取时间（取不到则退化为当前时间）。
    写完必须还原 —— 留着不动等于给整个进程留了个隐式全局副作用。
    """
    prev = os.environ.get("CODEX_PROJECT_ROOT")
    os.environ["CODEX_PROJECT_ROOT"] = str(root)
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("CODEX_PROJECT_ROOT", None)
        else:
            os.environ["CODEX_PROJECT_ROOT"] = prev


def find_note_by_hash8(notes_dir: pathlib.Path, hash8: str) -> pathlib.Path | None:
    """按 hash8 精确找已有笔记：文件名必须以 `-<hash8>.md` 结尾。

    只接受精确后缀匹配，找不到就返回 None。**不允许**退化成「取目录里第一个
    .md」——那会把 A 期的笔记登记到 B 期名下（期号与链接对不上，事后也分不出
    是哪一期登记错了）。
    """
    hits = sorted(p for p in notes_dir.rglob(f"*-{hash8}.md") if p.is_file())
    return hits[0] if hits else None


def find_info_by_hash8(raw_dir: pathlib.Path, hash8: str) -> dict | None:
    """按 hash8 找 info.json，并核对内容里的期号与文件名一致。"""
    for p in sorted(raw_dir.glob(f"*-{hash8}.info.json")):
        try:
            info = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        # 文件名对而内容里的期号不符，说明这两个文件不是同一期，
        # 登记出去等于把 A 期的信息写到 B 期行上。
        if (info.get("item") or {}).get("guid_hash8") == hash8:
            return info
    return None


def backfill_index(ids: list[str], project_root: str | None = None) -> dict:
    """只补 INDEX 登记。返回 {"added": [...], "skipped": [...], "failed": [(id, 原因)]}。"""
    root = _project_root(project_root)
    notes_dir = root / "rss" / "notes"
    raw_dir = root / "rss" / "raw"
    index_path = notes_dir / "INDEX.md"          # 全局单文件，不随节目目录变
    uniq = list(dict.fromkeys(i.strip() for i in ids if i and i.strip()))
    print(f"[backfill-index] RSS | 项目根 {root} | 请求 {len(ids)} 个期号"
          f"（去重后 {len(uniq)}）", flush=True)

    added: list[str] = []
    skipped: list[str] = []
    failed: list[tuple[str, str]] = []
    pending: list[tuple[str, pathlib.Path, dict, str]] = []
    existing = index_path.read_text(encoding="utf-8") if index_path.exists() else ""

    for hash8 in uniq:
        if not BACKFILL_HASH_RE.match(hash8):
            failed.append((hash8, "格式不符（应为 8 位小写十六进制期号）"))
            print(f"  ⚠️ 格式不符，跳过：{hash8}", flush=True)
            continue
        if f"| {hash8} |" in existing:
            skipped.append(hash8)
            print(f"  ⏭️ 已登记，跳过：{hash8}", flush=True)
            continue
        note = find_note_by_hash8(notes_dir, hash8)
        if note is None:
            failed.append((hash8, f"找不到笔记 {notes_dir}/**/*-{hash8}.md"))
            print(f"  ⚠️ 找不到笔记，跳过：{hash8}", flush=True)
            continue
        info = find_info_by_hash8(raw_dir, hash8)
        if info is None:
            failed.append((hash8, f"找不到 info.json（raw/*-{hash8}.info.json）"))
            print(f"  ⚠️ 找不到 info.json，跳过：{hash8}", flush=True)
            continue
        # 节目名：info 里的节目标题优先；取不到就用笔记所在的节目目录名 ——
        # 生成时目录名本身就来自同一个字段，两者同源。
        source_name = ((info.get("feed") or {}).get("title") or "").strip() \
            or note.parent.name
        pending.append((hash8, note, info, source_name))

    if pending:
        try:
            from append_index import register_rss_index
        except Exception as e:  # noqa: BLE001
            # 导入失败不能静默：整批都登记不上却报「补 0 条」，
            # 调用方会以为「本来就没什么可补」。
            for hash8, _, _, _ in pending:
                failed.append((hash8, f"索引写入模块导入失败：{type(e).__name__}: {e}"))
            pending = []
        for hash8, note, info, source_name in pending:
            try:
                with _project_root_env(root):
                    res = register_rss_index(info, note, source_name=source_name,
                                             index_path=index_path)
            except Exception as e:  # noqa: BLE001
                failed.append((hash8, f"索引写入失败：{type(e).__name__}: {e}"))
                print(f"  ⚠️ 写入失败：{hash8} {type(e).__name__}: {e}", flush=True)
                continue
            after = index_path.read_text(encoding="utf-8") \
                if index_path.exists() else ""
            # 数「补了几条」以**写完后的实际内容**为准，不以写入函数返回什么为准。
            if f"| {hash8} |" in after:
                added.append(hash8)
                print(f"  ✅ 已补登记：{hash8} → {note.name}（{source_name}，"
                      f"写入返回 {res}）", flush=True)
            else:
                failed.append((hash8, f"写入后 INDEX 里仍查不到该期号（返回 {res}）"))
                print(f"  ⚠️ 写入后仍查不到登记行：{hash8}", flush=True)

    print(f"✅ 补 {len(added)} 条（跳过 {len(skipped)} 条，失败 {len(failed)} 条）",
          flush=True)
    return {"added": added, "skipped": skipped, "failed": failed}


def _write_atomic(path: pathlib.Path, text: str) -> None:
    """原子落盘：先写同目录临时文件再 os.replace。

    write_text() 是 open('w')——先截断再逐块写；两进程同时写同一路径会各自截断、
    交错落盘，产出「半截 A + 半截 B」的撕裂文件且**不报错**。
    os.replace 在同一文件系统内是原子的，读者只可能看到完整旧版或完整新版。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def mark_subscribe_done(info: dict, note_path: pathlib.Path) -> str:
    """把订阅状态文件里对应期标 [done] 移入「已转化」区，返回一句话结果。

    `state_path` 与**完整 guid** 都从 info.json 反查，与 `regenerate_note_v2`
    的 `_mark_done_args` 同源：

    - guid 取 `item.guid` —— 状态文件里存的是**完整 guid**，拿 `guid_hash8`
      （sha256 前 8 位）去全等匹配注定落空；
    - state_path 由 `feed.title` 在订阅表里匹配节目名、再拼该项的 `state_file`；
      根目录用 `subscribe_manager.PROJECT_ROOT`，与该模块自己读订阅表的根一致。

    标记失败不该让笔记生成失败（保留原意图），但**必须打出来**：静默
    `except: pass` 会让「整条接线从未通过」这件事完全看不出来。
    """
    try:
        import subscribe_manager
        guid = (info.get("item") or {}).get("guid") or ""
        feed_title = (info.get("feed") or {}).get("title") or ""
        if not guid or not feed_title:
            return "跳过：info.json 缺 item.guid / feed.title"
        for src in subscribe_manager.load_subscriptions().get("sources", []):
            if src.get("name") == feed_title:
                state_path = subscribe_manager.PROJECT_ROOT / src["state_file"]
                ok = subscribe_manager.mark_done(state_path, guid, str(note_path))
                return (f"已标记 {state_path.name}" if ok
                        else f"未找到 guid={guid}（{state_path.name}）")
        return f"跳过：{feed_title} 不在订阅表里"
    except Exception as e:  # noqa: BLE001
        return f"失败（不影响笔记）：{type(e).__name__}: {e}"


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
    ap.add_argument("--backfill-index", metavar="IDS",
                    help="只补 INDEX 登记，不生成任何内容：传逗号分隔期号，"
                         "读已存在的笔记把缺的登记补进 INDEX.md。"
                         "不调 LLM / 不跑 ASR / 不下载 / 不改正文；已登记的自动跳过")
    ap.add_argument("--project-root", default=None,
                    help="项目根目录（默认按 find_project_root 定位；"
                         "补登记时可显式指定，指向真正要改 INDEX 的那份数据）")
    a = ap.parse_args()

    # 补登记分支排在定位/闸门之前：它只读本地文件，一个外部依赖都不需要。
    if a.backfill_index is not None:
        ids = [s.strip() for s in a.backfill_index.split(",") if s.strip()]
        if not ids:
            ap.error("--backfill-index 至少要一个期号")
        backfill_index(ids, project_root=a.project_root)
        return 0

    if a.transcript and a.info:
        t, i = a.transcript, a.info
        existing = None
    elif a.guid_hash8:
        t, i, existing = locate(a.guid_hash8)
    else:
        ap.error("需要 <guid_hash8> 或 --transcript + --info")

    if not a.transcript and not a.info and not a.guid_hash8:
        ap.error("需要 <guid_hash8> 或 --transcript + --info")

    # hash8 在打标接线处也要用（retag --ids 的主键），而下面只在「没有显式
    # --output 且没有既有产物」那条分支里算它 —— 早退路径上它是未定义的。
    # 提到这里统一算一次，两处共用同一个值。
    # 从源文件名尾部取（`<标题>-<hash8>.transcript.md`），别用 `t.stem` ——
    # 它只剥 `.md`，会留下 `...-6505cacd.transcript` 这种尾巴。
    _m_hash = re.search(r"-([0-9a-f]{8})\.transcript\.md$", t.name)
    hash8 = a.guid_hash8 or (_m_hash.group(1) if _m_hash else t.name)

    if not a.output:
        # ⚠️ 2026-10-05（与 bili 侧同源修复）：原先默认落
        # `SCRIPT_DIR / f"d3out-{t.stem}.md"` —— **skill 脚本目录内部**，
        # 那是调试期用的临时路径，漏进了默认值。后果：首次生成的新笔记
        # 会落进 `.agents/skills/rss-grab/scripts/`，还在那儿自动建 INDEX.md
        # （bili 侧实测踩过，rss 侧代码完全相同）。
        # 已有笔记走 `existing` 分支才正常，所以「重跑」看着没事、
        # 「首次生成」却落错地方。
        if existing:
            a.output = existing
        else:
            info = json.loads(pathlib.Path(i).read_text(encoding="utf-8"))
            # 源目录名 = feed.title（与既有 `rss/notes/<节目名>/` 布局一致，
            # 实测 十字路口Crossing / 独树不成林 都是这么来的）。
            # ⚠️ 不要自造 `_source_dir` 之类字段 —— info.json 里根本没有它。
            from fetch_rss_feed import sanitize_title
            source_dir = (info.get("feed") or {}).get("title") or "Unknown"
            a.output = (_SHARED.parent.parent.parent / "rss" / "notes" / source_dir /
                        f"{sanitize_title((info.get('item') or {}).get('title') or hash8)}"
                        f"-{hash8}.md")

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
    from append_index import register_rss_index
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
    # ⚠️ 喂 LLM 的必须是 `fm.prompt_info(info)` 而不是原始 `info`：
    # 共享层的 info_for_prompt / {desc} 占位符都**直取 info 顶层**，而 info.json
    # 是 feed/item/local 三层嵌套，直传会让元信息区块恒为空（详见 fm.prompt_info）。
    # 信头侧（fm.build）与索引/状态标记仍用原始 info：它们自己会解嵌套。
    body, report, stats = shared.generate_note_body(
        t, fm.prompt_info(info), client, a.work_root, SCRIPT_DIR / "typos")

    # 源文过短：共享层已跳过正文生成，直接返回 status=empty。注意此时的 stats
    # **没有** net_chars / route（赋值在闸门之后），下面统计行会 KeyError；
    # 而且若不在这里收住，会先落一篇空正文笔记并登记进生产 INDEX，
    # 占位信头又大于防重闸门的 500 字节阈值 → 重跑被判「已跳过」，
    # 永久卡在空笔记上。故：只落占位信头，且**不进索引**。
    if stats.get("status") == "empty":
        fmv = fm.build(info, transcript_status="empty",
                       summary_llm="（源文内容过少，未生成正文）")
        _write_atomic(a.output,
                      fm.dump(fmv) + "\n> ⚠️ 源文内容过少（清洗后 "
                      f"{stats.get('source_chars')} 字符），未生成正文。\n")
        print(f"⏭️ 源文过短，落占位：{a.output}")
        # 占位信头多大取决于 info 里的标题/简介长度（实测最短 480、常见 673+），
        # 所以「会不会被防重闸门拦下」是**因内容而异**的，不能一句话套死。
        size = a.output.stat().st_size
        print(f"   {size:,} bytes "
              + ("已超防重闸门 500，重跑须加 --force" if size > 500
                 else "未超防重闸门 500，重跑不受影响"))
        return 0

    # 落盘：信头 + 正文
    llm = stats.get("llm") or {}          # summary 在 LLM 的 JSON 里
    # V1.2 AC3：tags 不再由 v3 产出，交给 scripts/retag.py 事后按产物打标
    fmv = fm.build(info, summary_llm=llm.get("summary", ""))
    _write_atomic(a.output, fm.dump(fmv) + "\n" + body + "\n")

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

    # 进度日志：把订阅状态文件里对应期标 [done] 移入「已转化」区。
    # 开关与上面的索引闸门一致（--no-index = 暂存批次，不碰生产状态），
    # **不用** guid_hash8 当条件：显式 --transcript/--info 时它为 None，
    # 会把本来能标记的单篇生成一并跳过。
    if not a.no_index:
        print(f"   订阅状态={mark_subscribe_done(info, a.output)}")

    # 自动打标：正文已落盘，这里才打标。本脚本一次只处理一篇（批量由
    # batch_notes.py 起多个进程驱动），所以每篇调一次。
    # 闸门是「产物落在生产 notes 目录」：--output 指到别处（如批量重生成到
    # 暂存目录）时不消耗打标额度。--no-index 批量跑产物仍在生产目录，故仍打标。
    try:
        _prod_notes = _prod_notes_dir().resolve()
        _in_prod = _prod_notes in a.output.resolve().parents
    except OSError:
        _in_prod = False
    if _in_prod:
        auto_retag(RETAG_SOURCE, [hash8])
    return 0


if __name__ == "__main__":
    sys.exit(main())
