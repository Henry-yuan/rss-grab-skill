#!/usr/bin/env python3
"""rss-grab 订阅模式核心数据层。

职责：
  1. 订阅表 subscriptions.json 读写（全局源清单）
  2. 订阅状态文件 .md 读写（1 源 1 文件，含 frontmatter + 三区 + guid 注释）
  3. 增量去重（按原始 guid，无 guid 降级用 link）
  4. mark_done（标记"已转化"）/ update_summary（单期摘要替换）/ 退订

状态文件格式见 docs/superpowers/plans/2026-08-11-rss-subscribe.md 第 2 节。

依赖：Python 3.12+（标准库 fcntl/json/re/pathlib/datetime，无第三方包）。
"""
from __future__ import annotations

import base64
import fcntl
import json
import re
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[4]  # <project-root>（脚本深度反推）
SUBSCRIBE_DIR = PROJECT_ROOT / "rss" / "订阅"
SUBSCRIPTIONS_PATH = SUBSCRIBE_DIR / "subscriptions.json"
LOGS_DIR = SUBSCRIBE_DIR / ".logs"

# 三区标题（正则容错：允许尾部计数和 HTML 注释提示行）
# 匹配 "## 待确认" / "## 待确认 (3)" / "## 待确认 (3)   <!-- ... -->"
SECTION_PATTERNS = {
    "pending": re.compile(r"^##\s*待确认.*$", re.MULTILINE),
    "confirmed": re.compile(r"^##\s*确认.*$", re.MULTILINE),
    "done": re.compile(r"^##\s*已转化.*$", re.MULTILINE),
}

# checkbox 标记
CHECKBOX_PENDING = "[ ]"
CHECKBOX_GRAB = "[x]"
CHECKBOX_SKIP = "[~]"
CHECKBOX_DONE = "[done]"

# guid 注释提取（非贪婪，容错空格）
GUID_COMMENT_RE = re.compile(r"<!--\s*guid:(.*?)\s*-->")
# guid 编码后存储（含危险字符时 base64）
GUID_B64_PREFIX = "b64:"

# 提示行提取
HINT_RE = re.compile(r"<!--\s*updated:\s*(.*?)\s*\|-->\s*$")

# 状态文件结构注入防御：外部可控文本（RSS 标题/节目名/LLM 摘要值）写入前
# 拍平所有行分隔符（含 \r 等被 splitlines 认、渲染 join("\n") 不认的字符），
# 并破坏 HTML 注释定界符（防伪造 guid 注释 / 分区标题 / checkbox 行）
_INLINE_BREAK_RE = re.compile(r"[\s\x1c-\x1e]+")


def sanitize_inline(text) -> str:
    """外部可控文本 -> 状态文件单行安全串。

    - 所有空白拍平为单空格：防止写入后重解析时切成多行、伪造
      "- [x] N. ..." 条目行或 "## 分区" 标题
    - "<!--" / "-->" 替换为不可配对字符：防止内容里伪造 guid 注释
    """
    s = _INLINE_BREAK_RE.sub(" ", str(text))
    return s.replace("<!--", "‹").replace("-->", "›")


# ---------------------------------------------------------------------------
# guid 编解码（防止 guid 含 --> 破坏 HTML 注释）
# ---------------------------------------------------------------------------

def encode_guid(guid: str) -> str:
    """guid -> 安全存入 HTML 注释的字符串。

    guid 含 --> 或 <!-- 等危险字符时 base64 编码，前缀 b64: 标识。
    """
    if not guid:
        return ""
    if "-->" in guid or "<!--" in guid:
        b64 = base64.b64encode(guid.encode("utf-8")).decode("ascii")
        return f"{GUID_B64_PREFIX}{b64}"
    return guid


def decode_guid(stored: str) -> str:
    """encode_guid 的逆操作。"""
    if not stored:
        return ""
    if stored.startswith(GUID_B64_PREFIX):
        b64 = stored[len(GUID_B64_PREFIX):]
        return base64.b64decode(b64).decode("utf-8")
    return stored


def extract_guid(line: str) -> str:
    """从单行文本提取 guid（解析 <!-- guid:xxx --> 注释）。

    一行有多个注释时取最后一个：渲染器写的真实 guid 永远在行尾，
    即使标题内容漏消毒注入了伪注释，也覆盖不了真实 guid（纵深防御）。

    返回原始 guid（已 decode）。无注释返回空串。
    """
    matches = GUID_COMMENT_RE.findall(line)
    if not matches:
        return ""
    return decode_guid(matches[-1])


# ---------------------------------------------------------------------------
# 订阅表 subscriptions.json
# ---------------------------------------------------------------------------

def load_subscriptions() -> dict:
    """读订阅表。不存在返回 {"sources": []}。"""
    if not SUBSCRIPTIONS_PATH.exists():
        return {"sources": []}
    try:
        with open(SUBSCRIPTIONS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "sources" not in data:
            data["sources"] = []
        return data
    except (json.JSONDecodeError, OSError) as e:
        # 订阅表损坏：不崩，返回空 + 告警
        print(f"⚠️ 订阅表解析失败({e})，当作空表处理: {SUBSCRIPTIONS_PATH}", file=__import__("sys").stderr)
        return {"sources": []}


def save_subscriptions(data: dict) -> None:
    """写订阅表（带 flock 文件锁）。"""
    SUBSCRIBE_DIR.mkdir(parents=True, exist_ok=True)
    with open(SUBSCRIPTIONS_PATH, "w", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def find_source(source_name: str) -> dict | None:
    """按节目名查订阅源。找不到返回 None。"""
    for src in load_subscriptions().get("sources", []):
        if src.get("name") == source_name:
            return src
    return None


def add_subscription(feed_url: str, source_name: str, meta: dict | None = None) -> dict:
    """新增订阅源 + 初始化状态文件。

    参数：
      feed_url: RSS feed URL（feed.xyzfm.space/xxx）
      source_name: 节目名（也是状态文件名）
      meta: feed 级元数据 {title, author, language, link, description}

    返回新建的 source 条目 dict。已存在同名声源则更新 feed_url + meta。
    """
    meta = meta or {}
    data = load_subscriptions()

    # 查重：同名声源更新，不新增
    existing = None
    for src in data["sources"]:
        if src.get("name") == source_name:
            existing = src
            break

    safe_name = _sanitize_filename(source_name) or "未命名"
    state_file = f"rss/订阅/{safe_name}.md"

    if existing:
        existing["feed_url"] = feed_url
        existing["state_file"] = state_file
        existing["meta"] = meta
    else:
        entry = {
            "name": source_name,
            "feed_url": feed_url,
            "state_file": state_file,
            "subscribed_at": datetime.now().strftime("%Y-%m-%d"),
        }
        if meta:
            entry["meta"] = meta
        data["sources"].append(entry)

    save_subscriptions(data)

    # 初始化状态文件（不存在时创建）
    state_path = PROJECT_ROOT / state_file
    if not state_path.exists():
        _init_state_file(state_path, source_name, feed_url, meta)

    return existing or data["sources"][-1]


def remove_subscription(source_name: str) -> bool:
    """退订：从订阅表删除条目。状态文件标记"已退订"但保留（可恢复）。

    返回是否找到并删除了条目。
    """
    data = load_subscriptions()
    new_sources = []
    removed = None
    for s in data["sources"]:
        if s.get("name") == source_name:
            removed = s
        else:
            new_sources.append(s)
    data["sources"] = new_sources

    if not removed:
        return False

    save_subscriptions(data)

    # 状态文件标记已退订（保留文件）
    state_path = PROJECT_ROOT / removed.get("state_file", "")
    if state_path.exists():
        _mark_unsubscribed(state_path, source_name)

    return True


# ---------------------------------------------------------------------------
# 状态文件 .md 读写
# ---------------------------------------------------------------------------

def load_state(state_path: Path) -> dict:
    """解析状态文件，返回结构化数据。

    返回结构：
      {
        "frontmatter": {source, feed_url, subscribed_at, last_fetched},
        "feed_meta": {title, author, language, link, description},
        "pending": [item, ...],
        "confirmed": [item, ...],
        "done": [item, ...],
      }
    每个 item = {checkbox, seq, title, guid, fields: {发布日期, 时长, 一句话概括, ...}}

    YAML frontmatter 解析失败时：保留文件 + 返回空结构 + 调用方负责告警。
    """
    if not state_path.exists():
        return _empty_state()

    text = state_path.read_text(encoding="utf-8")

    # 拆 frontmatter 和正文
    fm, body = _split_frontmatter(text)

    state = _empty_state()
    state["frontmatter"] = fm

    # 解析 feed 级元数据（正文里的 > **节目名**：xxx 行）
    state["feed_meta"] = _parse_feed_meta(body)

    # 按三区切分
    sections, hints = _split_sections(body)
    state["hint"] = hints

    for zone, items in sections.items():
        state[zone] = items

    return state


def save_state(state_path: Path, state: dict) -> None:
    """写回状态文件（带 flock 文件锁）。

    保留 frontmatter + feed 级元数据 + 重写三区（按 state 里的 pending/confirmed/done）。
    """
    state_path.parent.mkdir(parents=True, exist_ok=True)

    # 用临时文件 + rename 原子写，降低并发风险
    tmp_path = state_path.with_suffix(".md.tmp")

    with open(tmp_path, "w", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(_render_state(state))
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    tmp_path.replace(state_path)


def find_new_items(feed_items: list[dict], known_guids: set[str]) -> list[dict]:
    """增量去重：返回 feed_items 中不在 known_guids 里的期。

    按原始 guid 去重；item 无 guid 时降级用 link；再无降级用 title+pub_date。
    返回的 item 额外注入 _dedup_key 字段，供后续按 key 写 guid 注释。
    """
    result = []
    for it in feed_items:
        key = _dedup_key(it)
        if key and key not in known_guids:
            it["_dedup_key"] = key
            result.append(it)
    return result


def collect_known_guids(state: dict) -> set[str]:
    """从 state 里收集所有已知 guid（三区都扫）。"""
    guids = set()
    for zone in ("pending", "confirmed", "done"):
        for item in state.get(zone, []):
            if item.get("guid"):
                guids.add(item["guid"])
    return guids


def mark_done(state_path: Path, guid: str, note_path: str) -> bool:
    """把指定 guid 的期从"确认"移到"已转化"，标 [done] 并附笔记路径。

    返回是否找到并标记了。
    """
    state = load_state(state_path)

    target = None
    for item in state["confirmed"]:
        if item.get("guid") == guid:
            target = item
            break
    if not target:
        # 可能在 pending（用户直接标 done 跳过确认）
        for item in state["pending"]:
            if item.get("guid") == guid:
                target = item
                break

    if not target:
        return False

    # 移到 done 区
    if target in state["confirmed"]:
        state["confirmed"].remove(target)
    elif target in state["pending"]:
        state["pending"].remove(target)

    target["checkbox"] = CHECKBOX_DONE
    target["note_path"] = note_path
    state["done"].append(target)

    save_state(state_path, state)
    return True


def update_summary(state_path: Path, guid: str, new_summary: str) -> bool:
    """单期 AI 摘要替换（--retry-summary 调用）。

    new_summary 是纯文本（一句话概括/内容概览/值得关注 三段），
    替换该期的"一句话概括 + 内容概览 + 值得关注"字段。

    返回是否找到并替换了。
    """
    state = load_state(state_path)

    target = None
    for zone in ("pending", "confirmed"):
        for item in state[zone]:
            if item.get("guid") == guid:
                target = item
                break
        if target:
            break

    if not target:
        return False

    # 解析 new_summary 的三段，写入 fields
    parsed = _parse_summary_segments(new_summary)
    fields = target.setdefault("fields", {})
    for k in ("一句话概括", "内容概览", "值得关注"):
        if k in parsed:
            fields[k] = parsed[k]

    save_state(state_path, state)
    return True


# ---------------------------------------------------------------------------
# 内部：文件名 / frontmatter / 分区解析
# ---------------------------------------------------------------------------

def _sanitize_filename(name: str) -> str:
    """节目名 -> 文件名安全串（与 fetch_rss_feed.sanitize_title 一致规则）。"""
    safe = re.sub(r'[^0-9A-Za-z一-鿿]+', '-', name)
    safe = re.sub(r'-+', '-', safe).strip('-')
    if len(safe) > 60:
        safe = safe[:60].rstrip('-')
    return safe


def _empty_state() -> dict:
    return {
        "frontmatter": {},
        "feed_meta": {},
        "pending": [],
        "confirmed": [],
        "done": [],
        "hint": {},   # zone -> 提示行内容（"更新于 ... | 说明"）
    }


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """拆 YAML frontmatter 和正文。

    frontmatter 在 --- 之间。解析失败返回 ({}, 原文)。
    简易 YAML 解析（只支持 key: value 单层），不用 PyYAML 依赖。
    """
    if not text.startswith("---"):
        return {}, text

    # 找第二个 ---
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text

    fm_text = text[3:end].strip()
    body = text[end + 4:].lstrip("\n")

    fm = {}
    for line in fm_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        fm[key] = val

    return fm, body


def _parse_feed_meta(body: str) -> dict:
    """解析正文里的 > **字段**：值 行（支持多行值，续行是 > 前缀）。

    字段分隔约定（渲染时字段间加空引用行 >，保证预览每个字段独立成段）：
      > **节目名**：十字路口Crossing
      >
      > **作者**：Koji
      >
      > **节目简介**：第一段
      > 第二段          <- 续行（值的一部分）
      >
      > **状态**：...
    解析为 {"节目名": "十字路口Crossing", "作者": "Koji",
            "节目简介": "第一段\n第二段"}。

    空引用行 ">" 的处理：
      - 后跟 "**key**：" -> 字段分隔（不属于任何值）
      - 后跟普通 "> 内容" -> 值内段落分隔（追加 \n 到当前值）
    """
    meta = {}
    lines = body.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r'^>\s*\*\*(.+?)\*\*[：:]\s*(.*)$', line)
        if not m:
            i += 1
            continue
        key = m.group(1).strip()
        val = m.group(2).strip()
        # 收集续行
        i += 1
        while i < len(lines):
            cont = lines[i]
            cm = re.match(r'^>\s*\*\*(.+?)\*\*[：:]\s*', cont)
            if cm and cm.group(1).strip() != key:
                break  # 下一个字段开始
            if cont == ">":
                # 空引用行：看下一行决定是字段分隔还是值内段落
                nxt = lines[i + 1] if i + 1 < len(lines) else ""
                nm = re.match(r'^>\s*\*\*(.+?)\*\*[：:]\s*', nxt)
                if nm or not nxt.startswith("> "):
                    break  # 字段分隔或结束
                # 值内段落分隔：追加 \n，跳过空行继续
                val += "\n"
                i += 1
                continue
            if cont.startswith("> "):
                val += "\n" + cont[2:].strip()
                i += 1
            else:
                break
        meta[key] = val
    return meta


def _split_sections(body: str) -> dict:
    """按三区标题切分正文，每区返回 item 列表 + hint。"""
    sections = {"pending": [], "confirmed": [], "done": []}
    hints = {}

    # 找三个区的起止位置 + 提示行
    zone_starts = {}
    for zone, pattern in SECTION_PATTERNS.items():
        m = pattern.search(body)
        if m:
            zone_starts[zone] = m.start()
            # 提取提示行：标题行尾的 <!-- updated: xxx -->
            header_line = body[m.start():body.find("\n", m.start())]
            hm = re.search(r"<!--\s*updated:\s*(.*?)\s*-->", header_line)
            if hm:
                hints[zone] = hm.group(1)

    # 按起始位置排序，确定每区范围
    ordered = sorted(zone_starts.items(), key=lambda x: x[1])
    for i, (zone, start) in enumerate(ordered):
        end = ordered[i + 1][1] if i + 1 < len(ordered) else len(body)
        section_text = body[start:end]
        sections[zone] = _parse_items(section_text)

    return sections, hints


def _parse_items(section_text: str) -> list[dict]:
    """解析一个区内的期数列表。

    每个 item 以 - [ ] / - [x] / - [~] / - [done] 开头，后跟序号.标题 + <!-- guid:xxx -->
    子字段以 4 空格缩进 - 开头。
    """
    items = []
    current = None
    current_field_key = None

    for line in section_text.splitlines():
        # 期数主行：- [x] 6. 标题 <!-- guid:xxx -->
        m = re.match(r'^-\s*\[( |x|~|done)\]\s*(?:(\d+)\.\s*)?(.*?)(?:\s*<!--\s*guid:.*-->)?\s*$', line)
        if m:
            if current:
                items.append(current)
            checkbox = f"[{m.group(1)}]"
            seq = int(m.group(2)) if m.group(2) else None
            title = m.group(3).strip()
            guid = extract_guid(line)
            current = {
                "checkbox": checkbox,
                "seq": seq,
                "title": title,
                "guid": guid,
                "fields": {},
                "note_path": "",
            }
            current_field_key = None
            continue

        # 嵌套子项（6+ 空格缩进，多行字段的子项）-- 先匹配，比普通子字段更深
        m = re.match(r'^\s{6,}-\s+(.*)$', line)
        if m and current and current_field_key:
            current["fields"][current_field_key] += "\n- " + m.group(1).strip()
            continue

        # 普通子字段行：    - 发布日期：xxx  或  - 一句话概括：xxx（4 空格缩进）
        m = re.match(r'^\s{4}-\s+(.+?)\s*[：:]\s*(.*)$', line)
        if m and current:
            key = m.group(1).strip()
            val = m.group(2).strip()
            # 笔记字段还原到 note_path（不进 fields）
            if key == "笔记":
                link_m = re.match(r'\[(.*?)\]\(.*?\)', val)
                current["note_path"] = link_m.group(1) if link_m else val
                continue
            # 如果 val 为空，可能是多行字段的开始（值在后续缩进行）
            if val:
                current["fields"][key] = val
            else:
                current["fields"][key] = ""
                current_field_key = key
            continue

        # 多行字段值续行（更深缩进的非 - 行）
        if current_field_key and line.startswith("        "):
            current["fields"][current_field_key] += "\n" + line.strip()
            continue

    if current:
        items.append(current)

    return items


def _parse_summary_segments(summary: str) -> dict:
    """解析 AI 摘要三段（一句话概括/内容概览/值得关注）。

    兼容多种 LLM 输出格式（与 preview_podcast 渲染逻辑一致）。
    """
    result = {}
    markers = ("一句话概括", "内容概览", "值得关注")
    for marker in markers:
        for sep in ("：", ":"):
            pos = summary.find(marker + sep)
            if pos >= 0:
                seg_start = pos + len(marker) + len(sep)
                # 段终点 = 下一个标记
                ends = []
                for m2 in markers:
                    if m2 == marker:
                        continue
                    for sep2 in ("：", ":"):
                        p = summary.find(m2 + sep2, seg_start)
                        if p >= 0:
                            ends.append(p)
                seg_end = min(ends) if ends else len(summary)
                result[marker] = summary[seg_start:seg_end].strip()
                break
    return result


def _dedup_key(item: dict) -> str:
    """生成去重 key。优先 guid，降级 link，再降级 title+pub_date。"""
    if item.get("guid"):
        return item["guid"]
    if item.get("link"):
        return f"link:{item['link']}"
    if item.get("title") and item.get("pub_date"):
        return f"title:{item['pub_date']}|{item['title']}"
    return ""


def _init_state_file(state_path: Path, source: str, feed_url: str, meta: dict) -> None:
    """初始化空状态文件（feed 级信息填好，三区为空）。"""
    state = _empty_state()
    state["frontmatter"] = {
        "source": source,
        "feed_url": feed_url,
        "subscribed_at": datetime.now().strftime("%Y-%m-%d"),
        "last_fetched": "",
    }
    state["feed_meta"] = {
        "节目名": meta.get("title", source),
        "作者": meta.get("author", ""),
        "语言": meta.get("language", ""),
        "节目链接": meta.get("link", ""),
        "RSS 源": feed_url,
        "节目简介": meta.get("description", ""),
    }
    save_state(state_path, state)


def _mark_unsubscribed(state_path: Path, source_name: str) -> None:
    """状态文件标记"已退订"（放 frontmatter 之后，防破坏解析）。"""
    text = state_path.read_text(encoding="utf-8")
    alert = f"> ⚠️ 已退订（{datetime.now().strftime('%Y-%m-%d')}），定时任务不再拉取此源。文件保留可恢复。\n"
    if "已退订" in text:
        return
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end >= 0:
            insert_at = end + 4
            text = text[:insert_at] + "\n" + alert + text[insert_at:].lstrip("\n")
            state_path.write_text(text, encoding="utf-8")
            return
    state_path.write_text(alert + text, encoding="utf-8")


def _render_state(state: dict) -> str:
    """把结构化 state 渲染成 .md 文本。"""
    lines = []

    # frontmatter
    fm = state.get("frontmatter", {})
    lines.append("---")
    for k in ("source", "feed_url", "subscribed_at", "last_fetched"):
        if k in fm:
            lines.append(f'{k}: "{sanitize_inline(fm[k])}"')
    lines.append("---")
    lines.append("")

    # feed 级元数据
    meta = state.get("feed_meta", {})
    source_name = fm.get("source", meta.get("节目名", ""))
    lines.append(f"# {sanitize_inline(source_name)} - 订阅")
    lines.append("")
    for key in ("节目名", "作者", "语言", "节目链接", "RSS 源", "节目简介"):
        if not meta.get(key):
            continue
        # 多行值（如节目简介含换行）：每行都加 > 前缀，保证引用块连续
        # 空行也渲染为 >（无内容），避免 Markdown 引用块被空行中断
        # 每行过 sanitize_inline：防 \r 等字符造成"写入单行、解析切多行"错位
        val = str(meta[key])
        first = True
        for line in val.split("\n"):
            line = sanitize_inline(line)
            if first:
                lines.append(f"> **{key}**：{line}")
                first = False
            elif line.strip():
                lines.append(f"> {line}")
            else:
                lines.append(">")
        # 字段之间加空引用行 >，让每个字段独立成段（CommonMark 连续行会合并成
        # 一个段落，预览时软换行渲染成空格——字段全挤一行）
        lines.append(">")
    lines.append("")
    lines.append("> 状态：[ ] 待确认 ｜ [x] 确认抓取 ｜ [~] 确认不抓 ｜ [done] 已转化")
    lines.append(">")
    lines.append("> 操作：改 checkbox 后保存，跑 --sync-pick 同步分区。")
    lines.append("")

    # 三区
    zone_names = {
        "pending": "待确认",
        "confirmed": "确认",
        "done": "已转化",
    }
    for zone, label in zone_names.items():
        items = state.get(zone, [])
        hint = state.get("hint", "").get(zone, "")
        hint_str = f"   <!-- updated: {hint} -->" if hint else ""
        lines.append(f"## {label} ({len(items)}){hint_str}")
        lines.append("")
        for item in items:
            lines.append(_render_item(item))
        lines.append("")

    return "\n".join(lines)


def _render_item(item: dict) -> str:
    """渲染单期为 markdown 文本。

    标题/字段值均为外部可控内容（RSS / LLM 输出），统一过 sanitize_inline
    防止向状态文件注入伪造的条目行或 guid 注释。
    """
    checkbox = item.get("checkbox", CHECKBOX_PENDING)
    seq = item.get("seq")
    title = sanitize_inline(item.get("title", ""))
    guid = item.get("guid", "")
    guid_str = f" <!-- guid:{encode_guid(guid)} -->" if guid else ""
    note_path = item.get("note_path", "")

    seq_str = f"{seq}. " if seq else ""
    lines = [f"- {checkbox} {seq_str}{title}{guid_str}"]

    fields = item.get("fields", {})
    for key in ("发布日期", "时长", "一句话概括", "内容概览", "值得关注", "链接"):
        val = fields.get(key)
        if not val:
            continue
        # 多行字段（含 \n 或子项）用主行 + 嵌套子项
        if "\n" in val:
            lines.append(f"    - {key}：")
            for sub in val.split("\n"):
                sub = sanitize_inline(sub).strip()
                if sub:
                    if sub.startswith("-"):
                        lines.append(f"      {sub}")
                    else:
                        lines.append(f"      - {sub}")
        else:
            lines.append(f"    - {key}：{sanitize_inline(val)}")

    # 笔记路径（已转化区用，渲染成 markdown 链接）
    if note_path:
        lines.append(f"    - 笔记：[{note_path}]({note_path})")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 命令行入口（测试用）
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: subscribe_manager.py <state.md> | --subs", file=sys.stderr)
        sys.exit(2)
    if sys.argv[1] == "--subs":
        print(json.dumps(load_subscriptions(), ensure_ascii=False, indent=2))
    else:
        state = load_state(Path(sys.argv[1]))
        print(json.dumps(state, ensure_ascii=False, indent=2, default=str))
