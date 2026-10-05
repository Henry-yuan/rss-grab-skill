"""rss 笔记 INDEX.md 追加：索引写入的共享实现（V1.1 T2.3）。

## 为什么抽出来（背景，不是历史考据）

这 4 个函数原本住在 `scripts/generate_note.py` —— 一个**已归档的 v2 脚本**。
但 v3 薄壳 `generate_note_v3.py` 的索引入口是它的**唯一**实际调用方，于是长期
存在一个矛盾：文档写「v2 已退役、不该再动」，代码却还在每次生成笔记时调用它。
T2.3 把索引逻辑抽到这里，v2 文件只留 re-export，两者解耦。

## 为什么 `find_project_root` 不在这里实现

它已经在 `_shared/paths.py`。**重新实现就是造第二份拷贝**，而副本漂移会让两处
各自演化、行为分叉。这里直接复用。

## ⚠️ 副本纪律

`_shared/append_index.py` 在本仓库与另一份工作区的 `_shared/` 里各存在一份
**独立实体拷贝**，改这里必须**手动双改 + 两边各跑测试**，禁止脚本单向覆盖。

**本文件的 docstring 按仓库范围裁剪，函数体（`__all__` 及其下全部代码）必须与
另一份逐字一致** —— 只有文档可以说本仓库的事，逻辑不能分叉。

本模块只服务 rss（`register_rss_index` 是 rss 专用）。
"""
from __future__ import annotations

import datetime
import re
from pathlib import Path

from paths import find_project_root

__all__ = [
    "format_duration_mm_ss",
    "get_capture_mtime",
    "register_rss_index",
    "sanitize_index_cell",
]


def get_capture_mtime(guid_hash8: str):
    """从 rss/raw/<...>-<hash8>.info.json 找抓取 mtime（INDEX 日期用抓取时间）。"""
    project_root = find_project_root()
    raw_dir = project_root / "rss" / "raw"
    if not raw_dir.exists():
        return None
    for info_path in raw_dir.glob(f"*-{guid_hash8}.info.json"):
        return datetime.datetime.fromtimestamp(info_path.stat().st_mtime)
    return None


def sanitize_index_cell(s: str) -> str:
    """清洗 INDEX.md 表格单元格：| -> /，换行->空格，去控制字符。"""
    if not s:
        return ""
    s = str(s).replace("|", "/")
    s = re.sub(r"[\r\n\t]+", " ", s)
    s = re.sub(r"\x1b\[[0-9;]*m", "", s)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    return s.strip()


def format_duration_mm_ss(seconds):
    """秒数 -> 'm:ss' 或 'h:mm:ss'。"""
    if seconds is None or seconds <= 0:
        return ""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h > 0:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def register_rss_index(info: dict, note_path: Path, source_name: str = "",
                       index_path: Path | None = None) -> str:
    """在 rss/notes/INDEX.md 追加一行（按 guid_hash8 dedup）。

    INDEX.md 格式: | 日期 | 标题 | 作者 | 时长 | 期号 | 源 | 笔记 |
    返回 "added" / "exists" / "skipped"

    index_path 默认全局 rss/notes/INDEX.md（不随笔记目录变，审查反馈 P5）；
    测试可传临时路径。
    """
    if index_path is None:
        project_root = find_project_root()
        index_path = project_root / "rss" / "notes" / "INDEX.md"
    item = info.get("item", {})
    feed = info.get("feed", {})
    guid_hash8 = item.get("guid_hash8", "")
    if not guid_hash8:
        print("  WARN: register_index: info 缺 guid_hash8，跳过 INDEX 追加")
        return "skipped"

    capture_mtime = get_capture_mtime(guid_hash8)
    if capture_mtime:
        today = capture_mtime.strftime("%Y-%m-%d %H:%M:%S")
    else:
        today = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"  WARN: get_capture_mtime 没找到 raw，INDEX 时间用 now() 兜底")

    title = sanitize_index_cell(item.get("title", ""))
    author = sanitize_index_cell(feed.get("author", ""))
    duration = format_duration_mm_ss(item.get("duration_seconds"))
    src = sanitize_index_cell(source_name or feed.get("title", ""))
    # 相对 INDEX.md 的链接（笔记可能在子目录 rss/notes/<源名>/）
    try:
        rel = note_path.resolve().relative_to(index_path.parent.resolve())
        md_link = f"[{note_path.stem}](./{rel.as_posix()})"
    except ValueError:
        md_link = f"[{note_path.stem}](./{note_path.name})"

    index_path.parent.mkdir(parents=True, exist_ok=True)
    import fcntl
    with index_path.open("a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            existing = f.read()
            if f"| {guid_hash8} |" in existing:
                return "exists"
            line = f"| {today} | {title} | {author} | {duration} | {guid_hash8} | {src} | {md_link} |\n"
            if not existing:
                header = (
                    "# RSS 播客笔记索引\n\n"
                    "> 按抓取时间倒序排列。新增笔记时在此追加一行。\n\n"
                    "| 日期 | 标题 | 作者 | 时长 | 期号 | 源 | 笔记 |\n"
                    "|---|---|---|---|---|---|---|\n"
                )
                f.write(header)
            f.write(line)
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return "added"
