#!/usr/bin/env python3
"""订阅清单 -> 飞书表格的**可选**双向镜像（push / pull）。

设计前提：**飞书是可选增强，不是依赖。**
    git clone 本仓库的人不装飞书、不配凭证，本脚本根本不会被调用，
    采集链路（fetch_rss_feed.py / run_fetch_updates.sh）行为与本文件无关。
    定时任务 22:30 跑的 run_fetch_updates.sh 只读 md，**完全不碰飞书**。

字段级分工（结构上消灭冲突：每个字段只归一边管）
--------------------------------------------------
    | 字段                                        | 归谁管   | push | pull |
    |---------------------------------------------|----------|------|------|
    | 序号/日期/时长/标题/一句话概括/内容概览/链接 | 只读镜像 | 推送 | 不碰  |
    | 状态（待确认/确认抓取/确认不抓/已转化）      | 飞书可写 | 推送 | 回写 |
    | guid                                        | 关联键   | 推送 | 只读  |

    「pull 绝不写坏非状态列」不是靠自觉，而是靠两层结构性保证：
      1. pull 只向飞书请求 H/I 两列（状态 + guid），**从不读取**其余列，
         所以那些列的数据根本不进本进程的内存；
      2. 回写时只替换条目主行上的 checkbox token，其余字节逐字保留。

状态映射（md checkbox <-> 表格状态下拉）
----------------------------------------
    [ ]    <-> 待确认      [x]    <-> 确认抓取
    [~]    <-> 确认不抓    [done] <-> 已转化

    注意：分区标题（待确认/确认/已转化）由 checkbox 派生，归
    `fetch_rss_feed.py --sync-pick` 管。本脚本**只改 checkbox，不搬条目**，
    与既有工具的职责不重叠。pull 之后照常跑一次 --sync-pick 即可归区。

用法
----
    python3 lark_sync.py push [--节目名 X] [--dry-run]
    python3 lark_sync.py pull [--节目名 X] [--dry-run]

凭证（**只**从环境变量或 scripts/.env 读，绝不硬编码、绝不写进被跟踪的文件）
    LARK_SPREADSHEET_TOKEN   表格 token（必填）
    LARK_SHEET_NAME         子表名（可选，默认取节目名）
    飞书应用自身的鉴权由 lark-cli 自己管理，本脚本不碰 app_id / app_secret。

降级行为
--------
    lark-cli 缺失 / 未配凭证 / 飞书侧报错 -> 明确报错并非零退出，
    **绝不静默假装成功**。pull 失败时 md 一个字节都不会被改。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]
SCRIPT_DIR = Path(__file__).resolve().parent
SUBSCRIBE_DIR = PROJECT_ROOT / "rss" / "订阅"

# lark-cli 是 npm 全局包装，装在 ~/.local/bin —— 非交互 shell 读不到，
# 必须显式补 PATH（与本机其他脚本的同一约定）。
LARK_BIN_CANDIDATES = ("lark-cli", os.path.expanduser("~/.local/bin/lark-cli"))

# 表格列序（A..I）。顺序即契约，改动会让旧表读不出列。
COL_SEQ, COL_DATE, COL_DUR, COL_TITLE = "A", "B", "C", "D"
COL_SUMMARY, COL_OVERVIEW, COL_HIGHLIGHT, COL_LINK = "E", "F", "G", "H"
COL_STATUS, COL_GUID = "I", "J"
HEADERS = ["序号", "发布日期", "时长", "标题", "一句话概括", "内容概览",
           "值得关注", "链接", "状态", "guid"]

# ⚠️ 上面这组列字母只是**建表时的默认布局**，不是 pull 的依赖。
# pull 一律先读表头、按**表头名**定位实际列（见 `locate_columns`），
# 这样用户在飞书上拖动列序、插列、删列都不会让 pull 读错数据。
# 曾硬编码「状态=H、guid=I」，用户随手拖一下列顺序，pull 就会把标题当状态
# 写回 md —— 静默、且方向错得离谱。
_HEADER_KEY = "状态"
_GUID_KEY = "guid"


def col_letter(idx0: int) -> str:
    """0 基列号 -> 列字母（0->A, 25->Z, 26->AA）。"""
    out = ""
    n = idx0 + 1
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def locate_columns(token: str, sheet: str) -> dict[str, str]:
    """读表头行，返回 {表头名: 列字母}。

    读不到表头（空表 / 刚建好的子表只有默认表名）时返回空 dict，
    调用方据此退回默认列序。
    """
    try:
        raw = run_lark(["sheets", "+cells-get",
                        "--spreadsheet-token", token,
                        "--sheet-name", sheet,
                        "--range", "A1:AZ1"])
    except SyncError:
        return {}
    payload = _loads_or_die(raw)
    grid = find_grid(payload)
    if not grid or not grid[0]:
        return {}
    out: dict[str, str] = {}
    for i, cell in enumerate(grid[0]):
        name = (cell_text(cell) or "").strip()
        if name and name not in out:      # 同名列取最左边那个
            out[name] = col_letter(i)
    return out


def resolve_columns(token: str, sheet: str) -> tuple[str, str, dict[str, str]]:
    """返回 (状态列, guid列, 全部表头映射)。缺列时退回默认并说明。"""
    cols = locate_columns(token, sheet)
    if not cols:
        return COL_STATUS, COL_GUID, cols
    status = cols.get(_HEADER_KEY, COL_STATUS)
    guid = cols.get(_GUID_KEY, COL_GUID)
    missing = [k for k in (_HEADER_KEY, _GUID_KEY) if k not in cols]
    if missing:
        print(f"⚠️  {sheet}：表头里找不到 {missing}，退回默认列序"
              f"（状态={status} guid={guid}）——请检查表头名是否被改过。",
              file=sys.stderr)
    return status, guid, cols

STATUS_TO_CHECKBOX = {
    "待确认": "[ ]",
    "确认抓取": "[x]",
    "确认不抓": "[~]",
    "已转化": "[done]",
}
CHECKBOX_TO_STATUS = {v: k for k, v in STATUS_TO_CHECKBOX.items()}
STATUS_OPTIONS = list(STATUS_TO_CHECKBOX)

# 与 subscribe_manager.py 同构：条目主行 + 4 空格缩进的子字段行。
ITEM_RE = re.compile(r"^(-\s*)\[( |x|~|done)\](\s*)(.*)$")
GUID_RE = re.compile(r"<!--\s*guid:(.*?)\s*-->")
FIELD_RE = re.compile(r"^\s*-\s*([^：:]+)[：:]\s*(.*)$")


class SyncError(RuntimeError):
    """面向用户的失败。调用方捕获后打印并非零退出，不打栈。"""


# ---------------------------------------------------------------------------
# 读 md
# ---------------------------------------------------------------------------

def parse_items(text: str) -> list[dict]:
    """把一个订阅状态文件解析成条目列表（顺序 = 文件里的出现顺序）。

    只取主行 + 其后**缩进**的子字段行；分区标题的计数与提示行**原样保留**
    （由 --sync-pick 重算，本脚本不碰）。

    ⚠️ 子字段必须按「行首是空白」判定，不能用「不像字段行就置空当前条目」：
    那样会把**还没 append 的条目直接丢掉**（实测漏 4 条：条目主行与下一条主行
    之间夹了非缩进行时，条目当场消失）。条目只在该遇到下一条主行时才 append。
    """
    items: list[dict] = []
    cur: dict | None = None
    pending_key: str | None = None
    pending_indent: int | None = None
    for line in text.split("\n"):
        m = ITEM_RE.match(line)
        if m:
            if cur:
                items.append(cur)
            head = m.group(4)
            gm = GUID_RE.search(head)
            # guid 注释要从标题里摘掉：表格的 guid 本来就是独立一列，
            # 留在标题里会出现 `标题 <!-- guid:xxx -->` 这种脏数据。
            title = GUID_RE.sub("", head).strip()
            cur = {
                "checkbox": "[" + m.group(2) + "]",
                "seq": None,
                "title": title,
                "guid": gm.group(1).strip() if gm else "",
                "fields": {},
            }
            pending_key = None
            pending_indent = None
            continue
        if cur is None or line[:1] not in (" ", "\t"):
            continue
        # 多行字段值：`    - 内容概览：` 后面跟的一串**缩进更深**的 `      - 子项`
        # 都属于同一个字段，不另起新键（否则内容概览会是空的，推送丢内容）。
        # ⚠️ 缩进必须**更深**：同级缩进的下一行是兄弟字段（`- 链接：…`），
        # 无条件当成续行会把兄弟字段吞进上一个字段（实测链接列丢了 7 条）。
        if pending_key is not None:
            if len(line) - len(line.lstrip()) <= pending_indent:
                pending_key = None
            else:
                sub = line.strip().lstrip("- ").strip()
                if sub:
                    cur["fields"][pending_key] = (
                        cur["fields"][pending_key] + "\n" + sub
                    ).strip()
                continue
        fm = FIELD_RE.match(line)
        if fm:
            key = fm.group(1).strip()
            val = fm.group(2).strip()
            if key not in cur["fields"]:
                cur["fields"][key] = val
                pending_key = key if not val else None
                pending_indent = len(line) - len(line.lstrip())
    if cur:
        items.append(cur)
    _fill_seq(items)
    return items


def _fill_seq(items: list[dict]) -> None:
    """主行里的序号可能被前面的文本粘住，从标题里再抽一次。"""
    for it in items:
        m = re.match(r"\s*(\d+)\.\s*(.*)$", it["title"])
        if m:
            it["seq"] = int(m.group(1))
            it["title"] = m.group(2).strip()
            it["seq_raw"] = m.group(1)
        else:
            it["seq_raw"] = ""


def pick_field(item: dict, *names: str) -> str:
    for n in names:
        if n in item["fields"]:
            return item["fields"][n]
    return ""


def state_files(only: str | None) -> list[Path]:
    if not SUBSCRIBE_DIR.is_dir():
        raise SyncError(f"找不到订阅目录：{SUBSCRIBE_DIR}")
    files = sorted(
        p for p in SUBSCRIBE_DIR.glob("*.md")
        if not p.name.startswith(".")
    )
    if only:
        files = [p for p in files if p.stem == only]
        if not files:
            avail = "、".join(p.stem for p in
                              sorted(SUBSCRIBE_DIR.glob("*.md"))) or "（无）"
            raise SyncError(f"找不到节目「{only}」。现有节目：{avail}")
    if not files:
        raise SyncError(f"{SUBSCRIBE_DIR} 下没有 .md 状态文件")
    return files


# ---------------------------------------------------------------------------
# 配置与飞书调用
# ---------------------------------------------------------------------------

def load_dotenv() -> None:
    """读 scripts/.env 到 os.environ（不覆盖已有环境变量）。

    刻意**不**复用 _shared/env.py：那份会强制校验 LLM_API_KEY，
    而本脚本跟 LLM 无关，被牵连成「没配 LLM key 就用不了飞书镜像」很荒唐。
    """
    env_path = SCRIPT_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(k.strip(), v)


def lark_config(stem: str) -> tuple[str, str]:
    """返回 (spreadsheet_token, sheet_name)。缺配置 -> SyncError（不是栈）。"""
    load_dotenv()
    token = os.environ.get("LARK_SPREADSHEET_TOKEN", "").strip()
    if not token:
        raise SyncError(
            "未配置 LARK_SPREADSHEET_TOKEN。\n"
            f"  做法：编辑 {SCRIPT_DIR / '.env'}，填入表格 token；\n"
            "  或：export LARK_SPREADSHEET_TOKEN=... \n"
            "  提示：token 是表格链接里 base 与表格标识之间那串字符。\n"
            "  这是**可选**功能，不配也不影响采集主链路。"
        )
    sheet = os.environ.get("LARK_SHEET_NAME", "").strip() or stem
    return token, sheet


def find_lark() -> str:
    for cand in LARK_BIN_CANDIDATES:
        if os.path.sep in cand and Path(cand).exists():
            return cand
    from shutil import which
    found = which("lark-cli")
    if found:
        return found
    raise SyncError(
        "找不到 lark-cli。飞书镜像是**可选**功能，不装它不影响采集。\n"
        "  装法参考 lark-cli 官方说明；装好后本脚本会自动用 "
        "~/.local/bin/lark-cli 或 PATH 里的 lark-cli。"
    )


def run_lark(args: list[str], stdin_data: str | None = None) -> str:
    """跑一次 lark-cli，返回 stdout。失败 -> SyncError（带上 stderr 摘要）。"""
    exe = find_lark()
    env = dict(os.environ)
    env["PATH"] = os.path.expanduser("~/.local/bin") + os.pathsep + env.get("PATH", "")
    try:
        proc = subprocess.run(
            [exe] + args, capture_output=True, text=True,
            input=stdin_data, env=env, timeout=180,
        )
    except FileNotFoundError as exc:
        raise SyncError(f"无法执行 lark-cli：{exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise SyncError(f"lark-cli 超时（180s）：{' '.join(args[:4])} …") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise SyncError(
            "lark-cli 调用失败（非零退出）。\n"
            f"  命令：lark-cli {' '.join(args)}\n"
            f"  退出码：{proc.returncode}\n"
            f"  摘要：{detail[:600] or '（无输出）'}\n"
            "  常见原因：未登录/授权过期、表格 token 或子表名不对、权限不足。"
        )
    return proc.stdout


def build_csv(items: list[dict], with_header: bool = True) -> str:
    """生成 CSV。

    `with_header=False` 用于**增量推送**：表头已存在，写进去会覆盖用户
    可能已经改过的表头文字。
    """
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    if with_header:
        w.writerow(HEADERS)
    for it in items:
        w.writerow([
            it["seq"] if it["seq"] is not None else "",
            pick_field(it, "发布日期"),
            pick_field(it, "时长"),
            it["title"],
            pick_field(it, "一句话概括"),
            pick_field(it, "内容概览"),
            pick_field(it, "值得关注"),
            pick_field(it, "链接"),
            CHECKBOX_TO_STATUS.get(it["checkbox"], "待确认"),
            it["guid"],
        ])
    return buf.getvalue()


def _read_existing(token: str, sheet: str, guid_col: str
                   ) -> tuple[set[str], int]:
    """读表里已有的 guid 与表尾行号。

    返回 (已有 guid 集合, 最后非空行号)。表为空时返回 (set(), 0)，
    调用方据此从第 1 行开始写 —— 也就退化成全量首推。
    """
    try:
        raw = run_lark(["sheets", "+cells-get",
                        "--spreadsheet-token", token,
                        "--sheet-name", sheet,
                        "--range", f"{guid_col}1:{guid_col}5000"])
    except SyncError:
        return set(), 0
    payload = _loads_or_die(raw)
    grid = find_grid(payload)
    if not grid:
        return set(), 0
    guids: set[str] = set()
    last = 0
    for i, row in enumerate(grid, start=1):
        # ⚠️ 第 1 行是**表头**，它的 guid 列里是字符串 "guid" 而非真 guid。
        #    不过滤会同时坏两处：① 计数虚高；② 表空时 last_row 至少为 1，
        #    增量从第 2 行开始写 → **表头永远写不进去**。
        if i == 1:
            continue
        if not row:
            continue
        v = cell_text(row[0]).strip()
        if v and v != _GUID_KEY:      # 再兜一层：表头被挪动过的情况
            guids.add(v)
            last = i
    return guids, last


def do_push(items: list[dict], token: str, sheet: str, dry_run: bool,
            force: bool = False) -> None:
    """推送。

    **默认增量**：只写表里还没有的 guid，已存在的行一个字都不碰。
    否则每次全量覆盖都会把用户在飞书上改的状态（确认抓取 / 确认不抓）
    冲回「待确认」—— 那是用户唯一的审阅成果，覆盖等于销毁。

    `force=True` 才做全量覆盖（建表首推、修表结构时用）。
    """
    status_col, guid_col, _cols = resolve_columns(token, sheet)

    to_write, start_row, wrote_header = items, 1, True
    if not force:
        existing, last_row = _read_existing(token, sheet, guid_col)
        if existing:
            fresh = [it for it in items if it["guid"] not in existing]
            print(f"→ {sheet}：本地 {len(items)} 条 / 表里已有 {len(existing)} 条 / "
                  f"新增 {len(fresh)} 条")
            if not fresh:
                print("   无新增，跳过（已有行不动，保留你改过的状态）")
                return
            to_write, start_row, wrote_header = fresh, last_row + 1, False

    payload = build_csv(to_write, with_header=wrote_header)
    n = len(to_write)
    tail = f"，起始行 {start_row}" + ("（FORCE 全量覆盖）" if force and not wrote_header else "")
    print(f"→ {sheet}：写入 {n} 行{tail}")

    if dry_run:
        print("\n--- 将写入的表格（dry-run，未调用飞书） ---")
        for i2, row in enumerate(csv.reader(io.StringIO(payload))):
            cells = [c[:28] + ("…" if len(c) > 28 else "") for c in row]
            print(f"  {i2:>4} | " + " | ".join(cells))
        print("\n--- 将执行的命令 ---")
        print(f"  lark-cli sheets +csv-put --spreadsheet-token <你的 token> "
              f"--sheet-name {sheet} --csv - --start-cell A{start_row}")
        print(f"  lark-cli sheets +dropdown-set --spreadsheet-token <你的 token> "
              f"--sheet-name {sheet} "
              f"--range {status_col}{start_row}:{status_col}{start_row + n - 1} "
              f"--options {json.dumps(STATUS_OPTIONS, ensure_ascii=False)}")
        return

    # CSV 走 **stdin**（`--csv -`），不用 `@file`：
    #   `@file` 只接受「当前目录下的相对路径」，传绝对路径会被 lark-cli 拒
    #   （实测报 invalid file path）；写临时文件到 CWD 又会污染用户目录。
    #   stdin 无此限制，且 451 行长文本也不会撞命令行长度上限。
    run_lark(["sheets", "+csv-put",
              "--spreadsheet-token", token,
              "--sheet-name", sheet,
              "--csv", "-",
              "--start-cell", f"A{start_row}"],
             stdin_data=payload)

    try:
        run_lark(["sheets", "+dropdown-set",
                  "--spreadsheet-token", token,
                  "--sheet-name", sheet,
                  "--range", f"{status_col}{start_row}:{status_col}{start_row + n - 1}",
                  "--options", json.dumps(STATUS_OPTIONS, ensure_ascii=False)])
    except SyncError as exc:
        # 数据已经写进去了，只是下拉没挂上 —— 说清楚，别让人以为全失败了。
        print(f"⚠️  数据已推送，但状态下拉未挂上：{exc}", file=sys.stderr)
        return

    print("✅ 推送完成（含状态下拉）")

def find_grid(obj, depth: int = 0):
    """在飞书返回的 JSON 里找出二维单元格数组。

    不同接口版本会把值放在 value / text / 字符串里，包法也不一样。
    这里做**结构化搜索**而不是认死某条路径：只要找到一个 list-of-list 就用。
    """
    if depth > 12:
        return None
    if isinstance(obj, list):
        if obj and all(isinstance(r, list) for r in obj):
            return obj
        for r in obj:
            got = find_grid(r, depth + 1)
            if got:
                return got
    elif isinstance(obj, dict):
        for v in obj.values():
            got = find_grid(v, depth + 1)
            if got:
                return got
    return None


def cell_text(c) -> str:
    if c is None:
        return ""
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, (int, float)):
        return str(c)
    if isinstance(c, dict):
        for k in ("value", "text", "formattedValue", "v"):
            if k in c:
                return cell_text(c[k])
    return ""


def read_statuses(token: str, sheet: str, n_rows: int) -> dict[str, str]:
    """只读状态列 + guid 列，返回 {guid: 状态文字}。

    ⚠️ 行列都**按表头名定位**、行数从飞书实际范围取，不写死列号：
    用户在飞书上拖动列序 / 插列 / 删列都不影响这里。曾经硬编码
    「状态=H、guid=I」，拖一下列就会把标题当状态写回 md。

    `n_rows` 只作为**下限**（本地比表里多时以本地为准），实际读多少由
    飞书返回的 `actual_range` 决定 —— 少读一行会**静默漏掉最后一期**，
    而症状只是「少改了一条」，极难察觉。
    """
    status_col, guid_col, _cols = resolve_columns(token, sheet)
    rng = f"{status_col}1:{guid_col}{max(n_rows, 0) + 1}"
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "grid.json"
        run_lark(["sheets", "+cells-get",
                  "--spreadsheet-token", token,
                  "--sheet-name", sheet,
                  "--range", rng,
                  "--output-path", str(out)])
        raw = out.read_text(encoding="utf-8")
    payload = _loads_or_die(raw)

    grid = find_grid(payload)
    if not grid:
        raise SyncError("飞书返回里找不到表格数据（结构与预期不符），已放弃，md 未改动。")

    # grid 每行从 status_col 开始：偏移 = status_col - 'A'
    off = ord(status_col) - ord("A")
    span = ord(guid_col) - ord(status_col)
    out_map: dict[str, str] = {}
    for row in grid:
        if len(row) <= max(off, off + span):
            continue
        guid = cell_text(row[off + span]).strip()
        status = cell_text(row[off]).strip()
        if guid and status:
            out_map[guid] = status
    return out_map


# ---------------------------------------------------------------------------
# pull：把状态列写回 md（原子）
# ---------------------------------------------------------------------------

def _loads_or_die(raw: str):
    """解析飞书返回的 JSON；失败时抛 SyncError 并明确说「md 未改动」。

    ⚠️ 放在 `cell_text` 之后：`locate_columns` / `_read_existing` 都要用它。
    曾一度被一段「do_push 到 find_grid 的整段替换」顺手删掉（它正好夹在
    两者中间），报 NameError —— 教训：**按行区间替换代码前，先确认区间里
    没有别的东西**。
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SyncError(f"无法解析飞书返回的 JSON：{exc}；已放弃，md 未改动。") from exc


def apply_statuses(text: str, wanted: dict[str, str]) -> tuple[int, int, list[str]]:
    """按 guid 把 checkbox 换掉。**只动条目主行的 checkbox token**。

    返回 (新文本, 改动条数, 未匹配说明列表)。
    """
    changed = 0
    notes: list[str] = []
    out_lines: list[str] = []

    for line in text.split("\n"):
        m = ITEM_RE.match(line)
        if m:
            gm = GUID_RE.search(m.group(4))
            if gm:
                guid = gm.group(1).strip()
                status = wanted.get(guid, "")
                if status:
                    new_cb = STATUS_TO_CHECKBOX.get(status)
                    if new_cb is None:
                        notes.append(f"未知状态「{status}」-> {guid}（跳过）")
                    elif new_cb != "[" + m.group(2) + "]":
                        # 唯一一处改写：前缀原样、checkbox 换掉、其余逐字保留
                        line = f"{m.group(1)}{new_cb}{m.group(3)}{m.group(4)}"
                        changed += 1
        out_lines.append(line)
    return "\n".join(out_lines), changed, notes


def atomic_write(path: Path, new_text: str) -> None:
    """同目录临时文件 + fsync + os.replace，保证读者永远看到完整旧版或完整新版。"""
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".lark_sync-",
                                    suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(new_text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def do_pull(files: list[Path], token_for, dry_run: bool) -> None:
    total_changed = 0
    for path in files:
        original = path.read_text(encoding="utf-8")
        items = parse_items(original)
        token, sheet = token_for(path.stem)

        if dry_run:
            print(f"\n=== {path.name}（dry-run，未改 md）===")
            print(f"  {len(items)} 条，其中待审（[ ]/[x]/[~]）"
                  f"{sum(1 for i in items if i['checkbox'] != '[done]')} 条")
            print(f"  将读取列：{COL_STATUS}(状态) + {COL_GUID}(guid)，其余列不读不写")
            continue

        statuses = read_statuses(token, sheet, len(items))
        if not statuses:
            print(f"⚠️  {path.name}：表格里没读到任何 guid+状态，跳过")
            continue

        new_text, changed, notes = apply_statuses(original, statuses)
        for n in notes:
            print(f"  {path.name}: {n}", file=sys.stderr)

        if changed == 0:
            print(f"✅ {path.name}：无变化（{len(statuses)} 个 guid 已对齐）")
            continue
        if new_text == original:
            print(f"✅ {path.name}：无变化")
            continue

        atomic_write(path, new_text)
        total_changed += changed
        print(f"✅ {path.name}：更新 {changed} 条状态")

        print("   接着跑一次归区：")
        print(f"     python3 {SCRIPT_DIR / 'fetch_rss_feed.py'} "
              f"--sync-pick {path}")

    if dry_run:
        print("\n（dry-run：未连接飞书，未改动任何文件）")
    else:
        print(f"\n完成：共更新 {total_changed} 条状态")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="订阅清单 <-> 飞书表格（可选镜像）。",
        epilog="不配凭证 / 不装 lark-cli 都不影响采集主链路。",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, help_text in (("push", "md -> 飞书表格"),
                            ("pull", "飞书表格状态 -> md（只改 checkbox）")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--节目名", dest="show", default=None,
                       help="只处理这一个节目；缺省则处理全部")
        p.add_argument("--dry-run", action="store_true",
                       help="只预览，不连飞书、不改文件")
        p.add_argument("--force", action="store_true",
                       help="push 才有效：强制全量覆盖。默认只增量写新条目，"
                            "已存在的行不动（否则会冲掉你在飞书上改过的状态）")
    args = ap.parse_args(argv)

    try:
        files = state_files(args.show)
        if args.cmd == "push":
            for path in files:
                items = parse_items(path.read_text(encoding="utf-8"))
                token, sheet = lark_config(path.stem)
                do_push(items, token, sheet, args.dry_run, force=args.force)
            print("\n提示：表格需事先手工建好空表，并把表头行保留为第一行。")
        else:
            # 配置按节目各自取（sheet 名默认=节目名），失败在 do_pull 之前
            # 就抛出 -> md 一个字节都不会动。
            do_pull(files, lark_config, args.dry_run)
        return 0
    except SyncError as exc:
        print(f"\n❌ {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已中断（md 未被改动）", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())