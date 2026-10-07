#!/usr/bin/env python3
"""订阅清单 -> 飞书多维表格（base / 记录 API）的**可选**双向镜像（push / pull）。

设计前提：**飞书是可选增强，不是依赖。**
    git clone 本仓库的人不装飞书、不配凭证，本脚本根本不会被调用，
    采集链路（fetch_rss_feed.py / run_fetch_updates.sh）行为与本文件无关。
    定时任务 22:30 跑的 run_fetch_updates.sh 只读 md，**完全不碰飞书**。

表结构：一张表
----------------
    三个节目**混在同一张表**里，靠「节目」单选字段区分 —— 不是每个节目一张表。
    这样才能做跨节目的筛选/排序/看板（「近期新增」= 所有节目里最近拉到的）。

字段级分工（结构上消灭冲突：每个字段只归一边管）
--------------------------------------------------
    | 字段                                          | 归谁管   | push | pull |
    |----------------------------------------------|----------|------|------|
    | 标题/发布日期/一句话概括/内容概览/值得关注/链接 | 只读镜像 | 推送 | 不碰  |
    | 拉取时间 / 节目 / guid                        | 只读镜像 | 推送 | 只读  |
    | 状态（待确认/确认抓取/确认不抓/已转化）        | 飞书可写 | 只在**新建**时给初值 | 回写 |

    「状态列归飞书所有」靠结构保证，不靠自觉：
      1. push 更新已有记录时，**载荷里根本没有状态这个键**——不是「算了不改」，
         是构造更新载荷时就不放它，改也改不掉；
      2. 状态只在**新建记录**时写入，取 md 里那条的 checkbox 当初值
         （新记录还没有人工审阅结果，md 是此刻唯一有依据的那边）。

    「pull 绝不写坏非状态列」同样靠结构：
      1. pull 只投影 状态 + guid 两列，其余列**不读**——数据不进内存；
      2. 回写只替换条目主行上的 checkbox token，前缀与后半行逐字保留。

状态映射（md checkbox <-> 单选字段「状态」）
------------------------------------------
    [ ]    <-> 待确认      [x]    <-> 确认抓取
    [~]    <-> 确认不抓    [done] <-> 已转化

    注意：分区标题（待确认/确认/已转化）由 checkbox 派生，归
    `fetch_rss_feed.py --sync-pick` 管。本脚本**只改 checkbox，不搬条目**，
    与既有工具的职责不重叠。pull 之后照常跑一次 --sync-pick 即可归区。

用法
----
    python3 lark_sync.py push  [--节目名 X] [--dry-run] [--force]
    python3 lark_sync.py pull  [--dry-run]
    python3 lark_sync.py sync  [--dry-run]     # = pull -> push(全量) -> --sync-pick 归区

凭证（**只**从环境变量或 scripts/.env 读，绝不硬编码、绝不写进被跟踪的文件）
    LARK_BASE_TOKEN    多维表格 token（必填）
    LARK_TABLE_ID      数据表 table_id（必填，形如 tblXXXX）
    飞书应用自身的鉴权由 lark-cli 自己管理，本脚本不碰 app_id / app_secret。

降级行为
--------
    lark-cli 缺失 / 未配凭证 / 飞书侧报错 -> 明确报错并非零退出，
    **绝不静默假装成功**。pull 失败时 md 一个字节都不会被改。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]
SCRIPT_DIR = Path(__file__).resolve().parent
SUBSCRIBE_DIR = PROJECT_ROOT / "rss" / "订阅"

# lark-cli 是 npm 全局包装，装在 ~/.local/bin —— 非交互 shell 读不到，
# 必须显式补 PATH（与本机其他脚本的同一约定）。
LARK_BIN_CANDIDATES = ("lark-cli", os.path.expanduser("~/.local/bin/lark-cli"))


# ---------------------------------------------------------------------------
# 字段契约
# ---------------------------------------------------------------------------
# 全部按**字段名**寻址，不按列序、不按下标。多维表格的列序是用户可以在界面上
# 拖的，任何「按第 N 列读」的做法都会在用户拖一下之后读错数据。
COL_STATUS = "状态"      # select，归飞书管
COL_GUID = "guid"        # text，关联键
COL_SHOW = "节目"        # select，一张表里区分节目

# 只读镜像列：push 可写、pull 绝不回写。
MIRROR_TEXT = ("标题", "一句话概括", "内容概览", "值得关注", "链接")
MIRROR_DATE = ("发布日期", "拉取时间")
# 顺序即 pull/push 的投影顺序（ndjson 每行是按字段名给的 dict，顺序不影响语义）。
MIRROR_COLUMNS = ("标题", "发布日期", "值得关注", "链接", "一句话概括",
                  "内容概览", COL_SHOW, "拉取时间")
PUSH_COLUMNS = (COL_GUID,) + MIRROR_COLUMNS

# 一次批量写最多 200 条（飞书侧上限）。
BATCH_MAX_RECORDS = 200
# 另外按**字节**兜一刀：内容概览动辄几 KB，200 条能到几百 KB，
# 走 @file 传参就不会撞命令行长度上限，但单请求体仍该有上限。
BATCH_MAX_BYTES = 400_000

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

# md 里的日期是**本地墙钟**（「2026-05-05 21:00」），没有时区。
# 写进 datetime 字段要的是「时刻」，所以必须先按业务时区解释再换算成毫秒。
# 不做这一步的话，naive 时间会被当成 UTC，差 8 小时。
try:                                              # pragma: no cover - 环境分支
    from zoneinfo import ZoneInfo
    _SHANGHAI = ZoneInfo("Asia/Shanghai")
except Exception:                                 # pragma: no cover - 缺 tzdata 时兜底
    _SHANGHAI = timezone(timedelta(hours=8))

_MD_DT_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})"
                       r"(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?$")

# 凭证值只用于**打码**，避免报错信息把 base token 写进终端回滚/日志。
_SECRETS: list[str] = []


def _redact(text: str) -> str:
    for s in _SECRETS:
        if s:
            text = text.replace(s, "***")
    return text


class SyncError(RuntimeError):
    """面向用户的失败。调用方捕获后打印并非零退出，不打栈。"""


@dataclass(frozen=True)
class LarkCfg:
    base_token: str
    table_id: str


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
            # guid 注释要从标题里摘掉：多维表格的 guid 本来就是独立字段，
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


def lark_config() -> LarkCfg:
    """返回 (base_token, table_id)。缺配置 -> SyncError（不是栈）。"""
    load_dotenv()
    base = os.environ.get("LARK_BASE_TOKEN", "").strip()
    table = os.environ.get("LARK_TABLE_ID", "").strip()
    missing = [k for k, v in (("LARK_BASE_TOKEN", base),
                              ("LARK_TABLE_ID", table)) if not v]
    if missing:
        raise SyncError(
            f"未配置 {'、'.join(missing)}。\n"
            f"  做法：编辑 {SCRIPT_DIR / '.env'}，填这两个值；\n"
            "  或：export LARK_BASE_TOKEN=... && export LARK_TABLE_ID=...\n"
            "  取值：base_token 是表格链接里 /base/ 后面那串字符；\n"
            "        table_id 形如 tblXXXX（多维表格里任一数据表的标识）。\n"
            "  这是**可选**功能，不配也不影响采集主链路。"
        )
    _SECRETS[:] = [base]
    return LarkCfg(base_token=base, table_id=table)


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
            input=stdin_data, env=env, timeout=300,
        )
    except FileNotFoundError as exc:
        raise SyncError(f"无法执行 lark-cli：{exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise SyncError(
            f"lark-cli 超时（300s）：{_redact(' '.join(args[:5]))} …") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise SyncError(
            "lark-cli 调用失败（非零退出）。\n"
            f"  命令：lark-cli {_redact(' '.join(args))}\n"
            f"  退出码：{proc.returncode}\n"
            f"  摘要：{_redact(detail)[:600] or '（无输出）'}\n"
            "  常见原因：未登录/授权过期、base_token 或 table_id 不对、权限不足。"
        )
    return proc.stdout


def _loads_or_die(raw: str):
    """解析飞书返回的 JSON；失败时抛 SyncError 并明确说「md 未改动」。"""
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SyncError(f"无法解析飞书返回的 JSON：{exc}；已放弃，md 未改动。") from exc


def read_records(cfg: LarkCfg, columns: tuple[str, ...]) -> list[dict]:
    """读表，返回每条一个 dict（按**字段名**取，另带 record_id）。

    用 `--format ndjson` 而不是默认的 markdown / json 矩阵：
      · ndjson 自动分页（580 条要 3 页），不用自己算 offset；
      · 每行是 `{"guid": ..., "标题": ..., "record_id": "recXXX"}`，
        **字段名直接是键** —— 于是「不要假设列序」是结构上成立的，
        不是靠约定；json 矩阵给的只是二维数组，列名要从别处猜。

    临时文件必须落在 /tmp：lark-cli 的 --output 只允许
    「当前工作目录 / /tmp / ~/files」三个根，macOS 的 tempfile 默认目录
    /var/folders 会被判 unsafe output path 直接拒。
    """
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="lark-sync-") as td:
        out = Path(td) / "records.ndjson"
        args = ["base", "+record-list",
                "--base-token", cfg.base_token,
                "--table-id", cfg.table_id]
        for col in columns:
            args += ["--field-id", col]
        args += ["--format", "ndjson", "--output", str(out), "--limit", "2000"]
        manifest = _loads_or_die(run_lark(args))
        if manifest.get("has_more"):
            raise SyncError(
                f"表内记录超过单次读取上限（已读到 {manifest.get('records_count')} 条"
                " 仍有更多），为避免只比对一半而**放弃**，md 未改动。")
        if not out.exists():
            raise SyncError(
                "lark-cli 说成功但没产出记录文件，已放弃，md 未改动。")
        text = out.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 取值转换：md <-> 多维表格单元格
# ---------------------------------------------------------------------------

def md_datetime_to_ms(value: str) -> int | None:
    """md 里的「YYYY-MM-DD HH:MM」-> 毫秒时间戳；空/认不出 -> None。

    None 的含义是**不要写这个字段**，不是「清空」。datetime 字段传 null 是
    清空语义，而 md 里没有这个日期时我们并不知道该不该清（如实留空的存量
    条目不该被抹成今天），所以宁可不写。
    """
    m = _MD_DT_RE.match((value or "").strip())
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hh = int(m.group(4) or 0)
    mm = int(m.group(5) or 0)
    ss = int(m.group(6) or 0)
    try:
        dt = datetime(y, mo, d, hh, mm, ss, tzinfo=_SHANGHAI)
    except ValueError:
        return None
    return int(dt.timestamp() * 1000)


def cell_to_ms(value) -> int | None:
    """把读回来的 datetime 单元格归一成毫秒，便于和 md 侧比对。

    读回来是 ISO 串（带时区），写进去是毫秒整数 —— 不归一就没法判断
    「到底有没有变」，于是每次 push 都会把全表重写一遍。
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(datetime.fromisoformat(str(value)).timestamp() * 1000)
    except ValueError:
        return None


def cell_text(value) -> str:
    """文本单元格归一成 str（空值统一成 ""，这样 None 与 "" 不会假报差异）。"""
    if value is None:
        return ""
    if isinstance(value, list):
        return cell_text(value[0]) if value else ""
    return str(value)


def render_text(value: str) -> str:
    """md 的多行字段 -> 单元格文本：还原 md 里的 `- ` 项目符号。

    `parse_items` 解析子字段行时会剥掉 `- `（`sub = line.strip().lstrip("- ")`），
    那是**解析期的归一**，不是「md 里本来就没符号」。多维表格的单元格是
    多行文本，直接塞归一后的值会把原来的列表结构拍平成一堵字墙。

    单行值**不加符号也不加前导换行** —— 与既有表里的写法一致
    （实测 580 条 × 标题/链接/一句话概括/内容概览/值得关注，
    按此规则渲染与表里已有内容逐字相同）。
    """
    if not value or "\n" not in value:
        return value
    return "\n- " + "\n- ".join(value.split("\n"))


def item_to_fields(item: dict, show: str,
                   with_status: bool = False) -> dict:
    """md 条目 -> 多维表格字段字典（**只含只读镜像列**，状态另议）。

    `with_status` 的含义随场景不同，这是**状态机归属**的核心：

    | 场景 | with_status | 效果 |
    |---|---|---|
    | 新建记录（飞书上还没有这条） | True | 写入 md 的状态当**初值** |
    | 更新已有记录 | False | 载荷里**没有状态键**，改不掉 |

    另一个场景：程序判定了 `[x] -> [done]`（产物验证通过，见
    generate_note_for_confirmed.should_mark_done）。那时 md 里已经是
    `[done]`，要把它送回飞书 —— 走 `push_system_status()`，
    **不是**这里的 with_status。后者只在「这条记录刚建、还没人审过」时用。
    """
    out: dict = {
        "标题": render_text(item["title"]),
        "一句话概括": render_text(pick_field(item, "一句话概括")),
        "内容概览": render_text(pick_field(item, "内容概览")),
        "值得关注": render_text(pick_field(item, "值得关注")),
        "链接": render_text(pick_field(item, "链接")),
        # 单选字段的值必须是**单元素数组**，传裸字符串会被拒。
        COL_SHOW: [show],
        COL_GUID: item["guid"],
    }
    for col in MIRROR_DATE:
        ms = md_datetime_to_ms(pick_field(item, col))
        if ms is not None:
            out[col] = ms
    if with_status:
        out[COL_STATUS] = [CHECKBOX_TO_STATUS.get(item["checkbox"], "待确认")]
    return out


# 系统可以写回飞书的状态值 —— **只有这一个**。
#
# 状态机的归属按「转换」划分，不是按「字段」划分（Henry 2026-10-06 纠正）：
#
#   待确认 -> 确认抓取   用户在飞书上点    数据只往 md 走
#   待确认 -> 确认不抓   用户在飞书上点    数据只往 md 走
#   确认抓取 -> 已转化   程序判定产物达标  数据只往飞书走
#
# 「同一个转换只有一个方向」—— 所以 push 永远不需要碰用户那三个值，
# 用户也永远不需要碰「已转化」。两边不会互相覆盖。
#
# ⚠️ 为什么不能图省事写成「状态照着 md 推」：那等于允许 push 把用户
# 在飞书上刚点的「确认抓取」用 md 里的旧值冲回去 —— 也就是当初把 md
# 覆盖回飞书的那个 bug，只是方向反了。
SYSTEM_WRITABLE_STATUS = "已转化"
# [done] 在 md 里的 checkbox 形态。**从映射派生**，不另写一份字面量：
# 改了上面那张表却忘了这里，保护逻辑就会跟着错（与 lark_sync 里
# 「计数与文本必须同一次遍历产出」同源的漂移风险）。
DONE_CHECKBOX = STATUS_TO_CHECKBOX[SYSTEM_WRITABLE_STATUS]


def _differs(want: dict, have: dict) -> bool:
    """want 里有而远端不一致的键，任一即算「要更新」。

    只看 **want 里有的键**：want 里没有的键表示「这次不碰」，不是「删掉」。
    """
    for key, val in want.items():
        if key in MIRROR_DATE:
            if cell_to_ms(val) != cell_to_ms(have.get(key)):
                return True
        elif key == COL_SHOW or key == COL_STATUS:
            if cell_text(val) != cell_text(have.get(key)):
                return True
        else:
            if cell_text(val) != cell_text(have.get(key)):
                return True
    return False


def batched_create(items: list[dict]) -> list[list[dict]]:
    """按「条数 200 + 字节 400K」双上限切批。

    只按条数切会出事：内容概览单条能到几 KB，200 条就是几百 KB 的请求体。
    """
    out: list[list[dict]] = []
    cur: list[dict] = []
    size = 0
    for it in items:
        n = len(json.dumps(it, ensure_ascii=False).encode("utf-8"))
        if cur and (len(cur) >= BATCH_MAX_RECORDS
                    or size + n > BATCH_MAX_BYTES):
            out.append(cur)
            cur, size = [], 0
        cur.append(it)
        size += n
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------------------
# push：md -> 多维表格
# ---------------------------------------------------------------------------

def do_push(show: str, items: list[dict], cfg: LarkCfg | None,
            dry_run: bool, force: bool = False) -> dict:
    """推送。

    **默认增量**：新建表里没有的 guid；已存在且镜像列有出入的才更新。
    `--force` 跳过逐条比对，把所有已存在记录的镜像列全部重写一遍。

    两种模式**都不碰「状态」列**（状态归飞书管）。sync = 先 pull 再 push，
    所以「你在飞书上勾的状态」不会被这一步冲回待确认 —— 更准确地说，
    是 push 根本没往那个字段写东西。

    ⚠️ dry-run 必须在**任何飞书调用之前**返回：比对远端差异本身就要读表，
    一旦在 dry-run 里读表，「dry-run 不连飞书」这条承诺就是假的
    （而且用户往往正是靠 dry-run 在没配凭证的机器上预览）。
    """
    if dry_run:
        _print_dry_run(show, items, force)
        return {"created": 0, "updated": 0}

    remote = read_records(cfg, PUSH_COLUMNS)
    by_guid: dict[str, dict] = {}
    dup = 0
    for rec in remote:
        guid = cell_text(rec.get(COL_GUID))
        if not guid:
            continue
        if guid in by_guid:
            dup += 1          # 同一条在表里出现多次：只认第一条，别重复写
            continue
        by_guid[guid] = rec

    to_create: list[dict] = []
    to_update: list[tuple[str, dict]] = []
    no_guid = 0
    for it in items:
        guid = it["guid"]
        if not guid:
            no_guid += 1
            continue
        want = item_to_fields(it, show, with_status=False)
        cur = by_guid.get(guid)
        if cur is None:
            # 新记录才给状态初值：此刻还没有人工审阅结果，md 是唯一有依据的一边。
            to_create.append(item_to_fields(it, show, with_status=True))
        elif force or _differs(want, cur):
            to_update.append((cell_text(cur.get("record_id")), want))

    print(f"→ {show}：本地 {len(items)} 条 / 表里已有 {len(by_guid)} 条 / "
          f"新建 {len(to_create)} 条 / 更新 {len(to_update)} 条"
          + ("（FORCE：镜像列全量重写）" if force else ""))
    if no_guid:
        print(f"   ⚠️  {no_guid} 条没有 guid 注释，已跳过"
              f"（多维表格靠 guid 关联，缺了就对不上）", file=sys.stderr)
    if dup:
        print(f"   ⚠️  表里有 {dup} 条 guid 重复，只按第一条处理", file=sys.stderr)

    if not to_create and not to_update:
        print("   无新增、无镜像列变化，已跳过（不会动你改过的状态）")
        return {"created": 0, "updated": 0}

    _ensure_show_option(cfg, show)

    created = 0
    for chunk in batched_create(to_create):
        _post_payload(cfg, "+record-batch-create", {"create_records": chunk})
        created += len(chunk)
        print(f"   新建 {len(chunk)} 条")

    updated = 0
    for start in range(0, len(to_update), BATCH_MAX_RECORDS):
        window = to_update[start:start + BATCH_MAX_RECORDS]
        payload = {"update_records": {rid: fields for rid, fields in window}}
        _post_payload(cfg, "+record-batch-update", payload)
        updated += len(window)
        print(f"   更新 {len(window)} 条")

    print(f"✅ {show}：新建 {created} 条 / 更新 {updated} 条（状态列未被写入）")
    return {"created": created, "updated": updated}


def _print_dry_run(show: str, items: list[dict], force: bool) -> None:
    """只凭本地 md 打印将要推送的形态，**不连飞书**。

    因此它无法告诉你「远端有没有这条」—— 这是「不连飞书」的必然代价，
    得说清楚，不能让人以为看到的就是最终结果。
    """
    ready = [it for it in items if it["guid"]]
    print(f"→ {show}：本地 {len(items)} 条（有 guid {len(ready)} 条）"
          f"｜{'FORCE 模式' if force else '增量模式'}")
    if len(ready) != len(items):
        print(f"   ⚠️  {len(items) - len(ready)} 条没有 guid 注释，"
              f"实跑时会跳过", file=sys.stderr)
    print("--- 将推送的记录（dry-run，未连接飞书，因此不知道远端是否已有） ---")
    for it in ready[:10]:
        st = CHECKBOX_TO_STATUS.get(it["checkbox"], "待确认")
        print(f"  · 标题：{it['title'][:40]}"
              f" ｜ 状态初值：{st}"
              f" ｜ 节目：{show}"
              f" ｜ 发布日期：{pick_field(it, '发布日期') or '（空，不写该字段）'}")
    if len(ready) > 10:
        print(f"  …（另有 {len(ready) - 10} 条）")
    print("--- 将执行的命令（形态如下，base_token 用占位符） ---")
    print("  lark-cli base +record-list --base-token <BASE_TOKEN> "
          "--table-id <TABLE_ID> --field-id <每列一个> "
          "--format ndjson --output <临时文件> --limit 2000")
    print("  lark-cli base +record-batch-create --base-token <BASE_TOKEN> "
          "--table-id <TABLE_ID> --json @<临时文件>")
    print("  lark-cli base +record-batch-update --base-token <BASE_TOKEN> "
          "--table-id <TABLE_ID> --json @<临时文件>")
    print("  注：更新载荷里**没有「状态」这个键**，状态列不会被写。")


_SHOW_OPTIONS_CACHE: dict[str, set[str]] = {}


def _ensure_show_option(cfg: LarkCfg, show: str) -> None:
    """新建前确认「节目」单选里有这个选项，否则 API 会报错且信息不直观。

    只在真的要建记录时才查（纯更新不查），读一次缓存在进程内。
    """
    key = cfg.table_id
    if key not in _SHOW_OPTIONS_CACHE:
        payload = _loads_or_die(run_lark(
            ["base", "+field-list", "--base-token", cfg.base_token,
             "--table-id", cfg.table_id, "--format", "json"]))
        fields = payload.get("data", {}).get("fields", [])
        opts: set[str] = set()
        for f in fields:
            if f.get("name") == COL_SHOW:
                opts = {o.get("name", "") for o in (f.get("options") or [])}
        _SHOW_OPTIONS_CACHE[key] = opts
    if show not in _SHOW_OPTIONS_CACHE[key]:
        raise SyncError(
            f"「节目」单选字段里没有选项「{show}」，新建记录会被飞书拒绝。\n"
            f"  现有选项：{'、'.join(sorted(_SHOW_OPTIONS_CACHE[key])) or '（无）'}\n"
            f"  做法：在多维表格里手动加一个名为「{show}」的选项。"
        )


def _post_payload(cfg: LarkCfg, command: str, payload: dict) -> None:
    """把批量载荷写进 /tmp 临时文件，再用 `@file` 传给 --json。

    不用命令行直接传：一期内容概览就能到几 KB，几百条拼起来会撞
    命令行长度上限（macOS ARG_MAX 约 1MB），症状是莫名其妙的 E2BIG。
    """
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="lark-sync-") as td:
        f = Path(td) / "payload.json"
        f.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        run_lark(["base", command,
                  "--base-token", cfg.base_token,
                  "--table-id", cfg.table_id,
                  "--json", f"@{f}"])


def push_system_status(files: list[Path], cfg: LarkCfg,
                       dry_run: bool = False) -> int:
    """把**程序判定的 `[done]`** 回写到飞书。只写「已转化」，其余一律不碰。

    为什么需要它：用户勾的三个状态由 pull 从飞书带回 md，本来就同步好了；
    但「这条已经转成笔记了」是**程序**判定的（产物验证通过才标），
    发生在本地，必须由这里送回飞书，否则飞书上永远停在「确认抓取」。

    🔴 硬约束：只允许写 `已转化`。md 里是「待确认 / 确认抓取 / 确认不抓」
    的条目**一个都不写** —— 那些是用户的领域，由 pull 单向同步。
    任何绕过这条的改动都会导致「用户刚点的确认抓取被本地旧值冲掉」。

    返回实际写了的条数。
    """
    remote = read_records(cfg, (COL_GUID, COL_STATUS))
    by_guid: dict[str, dict] = {}
    for rec in remote:
        guid = cell_text(rec.get(COL_GUID))
        if guid:
            by_guid[guid] = rec

    updates: list[tuple[str, dict]] = []
    for path in files:
        for it in parse_items(path.read_text(encoding="utf-8")):
            if CHECKBOX_TO_STATUS.get(it["checkbox"]) != SYSTEM_WRITABLE_STATUS:
                continue                      # 非系统状态：跳过，不写
            guid = it["guid"]
            rec = by_guid.get(guid)
            if not rec:
                continue                      # 飞书上还没有这条，交给普通 push 建
            rid = cell_text(rec.get("record_id"))
            if not rid:
                continue
            if cell_text(rec.get(COL_STATUS)) == SYSTEM_WRITABLE_STATUS:
                continue                      # 已经是「已转化」，不必重写
            updates.append((rid, {COL_STATUS: [SYSTEM_WRITABLE_STATUS]}))

    if not updates:
        print(f"→ 状态回写：无需更新（飞书上已是「{SYSTEM_WRITABLE_STATUS}」，"
              f"或其他状态由 pull 单向同步）")
        return 0
    print(f"→ 状态回写：{len(updates)} 条标记为「{SYSTEM_WRITABLE_STATUS}」")
    if dry_run:
        for rid, f in updates[:5]:
            print(f"   [dry-run] {rid} -> {cell_text(f[COL_STATUS])}")
        return 0
    _post_payload(cfg, "+record-batch-update", {"update_records": dict(updates)})
    print(f"✅ 已回写 {len(updates)} 条「{SYSTEM_WRITABLE_STATUS}」")
    return len(updates)


# ---------------------------------------------------------------------------
# pull：把状态列写回 md（原子）
# ---------------------------------------------------------------------------

def read_statuses(cfg: LarkCfg) -> dict[str, str]:
    """只投影 状态 + guid 两列，返回 {guid: 状态文字}。

    ⚠️ 投影是这条命令的**结构性保证**：其余列根本不进内存，也就无从回写。
    一张表里混着三个节目，所以只读一次全表，各节目 md 共用这份结果。
    """
    out: dict[str, str] = {}
    for rec in read_records(cfg, (COL_GUID, COL_STATUS)):
        guid = cell_text(rec.get(COL_GUID))
        status = cell_text(rec.get(COL_STATUS))
        if guid and status:
            out[guid] = status
    return out


def apply_statuses(text: str,
                   wanted: dict[str, str]) -> tuple[str, int, int, list[str]]:
    """按 guid 把 checkbox 换掉。**只动条目主行的 checkbox token**。

    返回 (新文本, 改动条数, 命中条数, 未匹配说明列表)。

    飞书上不认识的 guid 一条都不写：回写完全由 wanted 驱动，
    不在 wanted 里的条目，主行逐字保留。

    ⚠️ 计数与新文本**必须由同一次遍历产出**：曾经把这两件事拆成两个函数
    （一个数改了几处、一个造新文本），两份行变换逻辑各写一遍，
    迟早会漂移 —— 于是「说改了 3 条、实际写进去 2 条」这种不一致
    只有跑完几十期之后才暴露，且无法复现。

    🔴 **不覆盖 md 里已是 `[done]` 的条目**（2026-10-08 加）。
    `[done]` 是**程序判定的**：只有在笔记产物落盘且达标之后才写的，
    不是用户随手能表达的状态。所以飞书上那条如果是「确认抓取」「待确认」
    之类的**旧值**（回写没跑到、或被人误改），把它灌回 md 会把已完成的一期
    重新推回流水线 —— 白跑一遍下载/ASR，还会因为笔记已存在而被防重闸门挡下，
    看起来「处理了」实则什么都没变。

    这与 `push` 侧的方向性是对称的：`SYSTEM_WRITABLE_STATUS` 保证**只有**
    「已转化」会被程序写进飞书，反过来 pull 就不该用飞书的值去撤销它。

    ⚠️ **被保护的条目要写进 notes 报出来**，不静默跳过 —— 否则你只会看到
    「更新 0 条」，完全看不出是保护生效了还是同步压根没匹配上。
    """
    changed = 0
    hits = 0
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
                    hits += 1
                    new_cb = STATUS_TO_CHECKBOX.get(status)
                    cur_cb = "[" + m.group(2) + "]"
                    if new_cb is None:
                        notes.append(f"未知状态「{status}」-> {guid}（跳过）")
                    elif cur_cb == DONE_CHECKBOX and new_cb != DONE_CHECKBOX:
                        notes.append(
                            f"{guid} 已是已转化，飞书上仍是「{status}」"
                            f"（不覆盖；要改请先跑 push-status）")
                    elif new_cb != cur_cb:
                        # 唯一一处改写：前缀原样、checkbox 换掉、其余逐字保留
                        line = f"{m.group(1)}{new_cb}{m.group(3)}{m.group(4)}"
                        changed += 1
        out_lines.append(line)
    return "\n".join(out_lines), changed, hits, notes


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


def do_pull(files: list[Path], cfg: LarkCfg | None, dry_run: bool) -> None:
    """把飞书上的状态写回 md。**只改 checkbox**，其余字节逐字保留。"""
    if dry_run:
        for path in files:
            items = parse_items(path.read_text(encoding="utf-8"))
            todo = sum(1 for i in items if i["checkbox"] != "[done]")
            print(f"=== {path.name}（dry-run，未连飞书、未改 md）===")
            print(f"  {len(items)} 条，其中待审（[ ]/[x]/[~]）{todo} 条")
        print("\n（dry-run：未连接飞书，未改动任何文件）")
        return

    statuses = read_statuses(cfg)          # 一次读全表；失败在此抛，md 一个字节都没动
    print(f"→ 飞书读到 {len(statuses)} 条 guid+状态")

    # 「飞书上有、本地没有」要拿**全部节目**的 guid 并集来算。
    # 拿单个文件的 guid 去减，别的节目的条目会被整片报成「本地没有」——
    # 三个节目共用一张表时，这个数字会大到吓人（实测 515/447/198），
    # 而它其实一条都不缺，只是住在另一个 md 里。
    all_local: set[str] = set()
    parsed: dict[Path, list[dict]] = {}
    for path in files:
        parsed[path] = parse_items(path.read_text(encoding="utf-8"))
        all_local |= {i["guid"] for i in parsed[path] if i["guid"]}
    orphan = set(statuses) - all_local
    if orphan:
        print(f"   飞书上有 {len(orphan)} 条 guid 在任何本地 md 里都找不到"
              f"（已忽略，不新建）")

    total_changed = 0
    for path in files:
        original = path.read_text(encoding="utf-8")

        new_text, changed, hits, notes = apply_statuses(original, statuses)
        for n in notes:
            print(f"  {path.name}: {n}", file=sys.stderr)

        if changed == 0:
            print(f"✅ {path.name}：无变化（{hits} 条已对齐）")
            continue

        atomic_write(path, new_text)
        total_changed += changed
        print(f"✅ {path.name}：更新 {changed} 条状态")
        print("   接着跑一次归区：")
        print(f"     python3 {SCRIPT_DIR / 'fetch_rss_feed.py'} "
              f"--sync-pick {path}")

    print(f"\n完成：共更新 {total_changed} 条状态")


# ---------------------------------------------------------------------------
# sync：pull -> push(全量) -> --sync-pick 归区
# ---------------------------------------------------------------------------

def do_sync(files: list[Path], cfg: LarkCfg | None, dry_run: bool) -> None:
    """重跑摘要后的正确姿势。

    顺序不能反：先 pull 把状态收回 md，再 push 把新摘要带上去。
    （push 本身不写状态列，所以这里即使顺序反了也不会冲掉状态，
    但先 pull 仍是对的 —— 它保证「飞书上的最新状态」进入本次推送的上下文。）
    """
    print("=" * 60)
    print("第 1/4 步：pull（把飞书上改过的状态写回 md）")
    print("=" * 60)
    do_pull(files, cfg, dry_run)

    print()
    print("=" * 60)
    print("第 2/4 步：push（镜像列增量对齐）")
    print("=" * 60)
    if dry_run:
        for path in files:
            items = parse_items(path.read_text(encoding="utf-8"))
            print(f"\n=== {path.name}（dry-run）===")
            print(f"  本地 {len(items)} 条；将比对的镜像字段：{'、'.join(PUSH_COLUMNS)}")
        print("\n--- 将执行的命令 ---")
        print("  lark-cli base +record-batch-create / +record-batch-update "
              "… --json @<临时文件>")
        print("  lark-cli python3 scripts/fetch_rss_feed.py --sync-pick <md>")
    else:
        for path in files:
            items = parse_items(path.read_text(encoding="utf-8"))
            # 增量（force=False）：只写「镜像列确实有出入」的条目。
            # 原为 force=True（全量重写全表），终态虽相同但每次 sync 要
            # 重写 580 条；而且 sync 要进 22:30 定时链路，全量等于每晚一次
            # 无谓的批量写。Henry 2026-10-06 确认为增量。
            do_push(path.stem, items, cfg, False, force=False)

    # 第 3 步：按 checkbox 重新归区，消掉「区位置」与「符号」不一致的状态。
    #   为什么要这步：pull 只换 checkbox、不动条目位置（有意为之，避免重排），
    #   于是「在飞书勾成 [x] 的条目」会留在「待确认」区、只多了个 [x]，
    #   而阶段 3 的 pick_confirmed 只扫「确认」区 —— 这类条目会被漏掉。
    #   fetch_rss_feed.py --sync-pick 早就实现了「按 checkbox 归区」，
    #   这里只是把它接进 sync 的收尾，不新增任何逻辑。
    print("\n" + "=" * 60)
    print("第 3/4 步：按 checkbox 重新归区（保证「区」与「符号」一致）")
    print("=" * 60)
    if dry_run:
        for path in files:
            print(f"  {path.name}：将跑 fetch_rss_feed.py --sync-pick（dry-run 未执行）")
        print("  lark_sync.py lark-cli … 状态回写（dry-run 未执行）")
        print("\n（dry-run：未连接飞书，未改动任何文件）")
        return
    for path in files:
        r = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "fetch_rss_feed.py"),
             "--sync-pick", str(path)],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT))
        tail = [x for x in (r.stdout or "").splitlines() if x.strip().startswith("✅")]
        print(f"  {path.name}：{tail[-1] if tail else (r.stderr or '').strip()[:80]}")

    # 第 4 步：把**程序判定的** `[done]` 回写飞书（只写「已转化」）。
    #   归区之后 md 里 [done] 才是权威位置，这时回写才不会把
    #   「还在确认区、符号已变」的中间态推上去。
    print()
    print("=" * 60)
    print("第 4/4 步：状态回写（只把程序判定的「已转化」推给飞书）")
    print("=" * 60)
    push_system_status(files, cfg)

    print("\n✅ sync 完成：状态已归区、镜像列已对齐、「已转化」已回写")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="订阅清单 <-> 飞书多维表格（可选镜像）。",
        epilog="不配凭证 / 不装 lark-cli 都不影响采集主链路。",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, help_text in (("push", "md -> 飞书多维表格（新建缺失 + 对齐镜像列）"),
                            ("pull", "飞书状态 -> md（只改 checkbox）"),
                            ("sync", "先 pull 保状态，再对齐镜像列，最后归区"),
                            ("push-status", "把程序判定的「已转化」回写飞书"
                                            "（22:30 链路 stage 4 专用；"
                                            "用户那三个状态一律不碰）")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--节目名", dest="show", default=None,
                       help="只处理这一个节目；缺省则处理全部")
        p.add_argument("--dry-run", action="store_true",
                       help="只预览，不连飞书、不改文件")
        p.add_argument("--force", action="store_true",
                       help="push 才有效：跳过逐条比对，把已存在记录的镜像列全部重写。"
                            "默认只更新「镜像列有出入」的条目（状态列任何情况下都不写）")
    args = ap.parse_args(argv)

    try:
        files = state_files(args.show)
        # dry-run 连配置都不读：它不连飞书，也就不该因为没配凭证而失败。
        cfg = None if args.dry_run else lark_config()
        if args.cmd == "sync":
            do_sync(files, cfg, args.dry_run)
        elif args.cmd == "push-status":
            push_system_status(files, cfg, args.dry_run)
        elif args.cmd == "push":
            for path in files:
                items = parse_items(path.read_text(encoding="utf-8"))
                do_push(path.stem, items, cfg, args.dry_run, force=args.force)
            if not args.dry_run:
                print("\n提示：多维表格里「状态」「节目」必须是单选字段、"
                      "「发布日期」「拉取时间」必须是日期字段，否则写入会被拒。")
        else:
            do_pull(files, cfg, args.dry_run)
        return 0
    except SyncError as exc:
        print(f"\n❌ {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已中断（md 未被改动）", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())