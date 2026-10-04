#!/usr/bin/env python3
"""rss 笔记 frontmatter 组装（V1.0 tasks C6，spec AC-C2 的 17 字段表）。

**这是「信头」** —— A10 把管线本体抽进 `_shared/build_v3.py` 后，信头归各源自带：
本模块只负责「这篇播客是谁、什么时候、多长、什么类型」，不管正文怎么生成。
`build_v3.PROMPT_INFO_FIELDS` 引用本模块的 `PROMPT_INFO_FIELDS`，
共享模块据此生成喂给 LLM 的元信息块。

## 值从哪来（全部程序填充，不经模型）

这是本模块存在的核心理由：实测发现 **bili 侧 4 处日期编造 + 「关键背景」补写
全部发生在「视频元信息」节**，因为那一节由**模型撰写**；而 rss 侧的元信息由 feed
直填、不经模型，故 rss 编造率（0.095%）显著低于 bili（0.340%）。
**元信息改为程序填充 = 这个失败模式从结构上消失**，不需要靠提示词约束。

## 纪律

- **无数据就留空，不猜、不填占位值**（AC-C3-3）。`subtitle_source` 推不出来就留空，
  由 `transcript_status: empty` 表达「没有文本」这件事。
- 时长、嘉宾、节目名都从 `info.json` 的 feed/item 字段解析，**不从正文抠**。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

# 喂给 LLM 的元信息白名单（共享模块的 info_for_prompt 按此取）。
# 只列「对写作有帮助且不会误导」的：标题/节目名/嘉宾/类型/时长/简介。
# 不含主键与原始链接 —— 对写作无帮助，还占 token。
PROMPT_INFO_FIELDS = (
    "title", "show_name", "speaker", "type", "duration", "description",
)

# `type` 的值域。本仓库只产 podcast；另两个值由别的源使用。
TYPE_PODCAST = "podcast"

# 「时 长：01:23:45」/「时长: 45:00」→ 秒。允许 h:mm:ss 与 mm:ss 两种。
_DUR_RE = re.compile(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{2})\s*$")
_DUR_MIN_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:分钟|分|min|minutes?)\s*$", re.I)

# ── 嘉宾名从 item.title 解析 ──
#
# ⚠️ 重要发现：**info.json 里没有嘉宾字段**。嘉宾只出现在 `item.title` 里，
# 形如「…｜对谈于文渊：阿里云百炼技术负责人」或「…｜和王筱圃聊时序大模型…」。
# 现有 48 篇笔记里的完整版「于文渊(阿里云百炼研发负责人)」是**模型从转写稿生成的**
# —— 那正是本模块要移除的编造风险路径（bili 侧 4 处日期编造 + 「关键背景」补写
# 全部出在模型撰写的元信息节上）。
#
# 所以本字段**只放 title 里能确定的人名，不补机构/头衔**：
# 「于文渊」可靠，「阿里云百炼研发负责人」是模型加的，不要。
_SPEAKER_PATTERNS = (
    # 「对谈XXX：」「访谈XXX：」—— 冒号前的人名（可含机构后缀，取人名部分）
    re.compile(r"(?:对谈|访谈|对话)\s*([^：:｜|\n]{2,24})?\s*[：:]"),
    # 「和XXX聊」「与XXX聊」—— 介词 + 人名 + 聊
    re.compile(r"[和与]\s*([^：:｜|\n]{2,24}?)\s*聊"),
    # 「对谈XXX」结尾无冒号（如「｜对谈 AirJelly 黄柏特」）
    re.compile(r"[｜|]\s*对谈\s*([^：:｜|\n]{2,20})\s*$"),
)

# 机构/公司特征词：命中即判定不是人名。
# 「百药师」「集风科技」这类被「与…的」「和…聊」误抓的机构名必须排除 ——
# **填错比留空更糟**：本字段的定位是权威数据，错值等于把编造换个地方放。
_ORG_WORDS = ("科技", "公司", "集团", "股份", "有限", "平台", "团队", "实验室",
              "工作室", "资本", "基金", "创业", "网络", "信息", "数据", "智能",
              "系统", "软件", "硬件", "研究院", "事业部", "中心", "大学", "学院")
_LATIN_NAME = re.compile(r"^[A-Z][a-zA-Z.]{1,14}$")
_CJK_NAME = re.compile(r"^[\u4e00-\u9fff]{2,6}$")


def extract_speaker(title: str) -> str:
    """从 **item.title** 解析嘉宾人名。取不到返回空串。

    **只取 title 里能确定的人名，不补机构与头衔** —— 那些本仓库的数据里没有，
    硬补就是编造（AC-C3-3：无数据就留空，不猜）。

    宁可漏（留空）也不可错（填入机构名）：错值会让下游把它当权威数据用。
    """
    if not title:
        return ""
    for pat in _SPEAKER_PATTERNS:
        for m in pat.finditer(title):
            cand = (m.group(1) or "").strip().strip("（）()· 　｜|")
            if not cand:
                continue
            # 机构后缀切掉：「字节AI产品负责人Vanessa」→「Vanessa」
            for w in sorted(_ORG_WORDS, key=len, reverse=True):
                if w in cand:
                    cand = cand.split(w)[-1]
            # 切完可能残留动词/称谓前缀：「连续创业|者刘夜」→「者刘夜」需再剥「者」
            cand = re.sub(r"^[的与和了者家们的\s]+|[的了与和们者家\s]+$", "", cand)
            if not (2 <= len(cand) <= 14) or cand.isdigit():
                continue
            if any(w in cand for w in _ORG_WORDS):   # 切完还剩机构词 → 放弃
                continue
            if _CJK_NAME.match(cand) or _LATIN_NAME.match(cand):
                return cand
    return ""


def parse_duration(v) -> int | None:
    """时长 → 秒。解析不了返回 None（调用方留空，不猜）。

    支持 `h:mm:ss` / `mm:ss` / 「45 分钟」/ 已经是数字的秒数。
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v) if v >= 0 else None
    s = str(v).strip()
    if not s:
        return None
    m = _DUR_RE.match(s)
    if m:
        h, mm, ss = m.groups()
        return int(h or 0) * 3600 + int(mm) * 60 + int(ss)
    m = _DUR_MIN_RE.match(s)
    if m:
        return int(float(m.group(1)) * 60)
    # 纯数字：按秒处理（本仓库的 feed 也会给秒数）
    if s.isdigit():
        return int(s)
    return None


def parse_duration_display(v) -> str:
    """把时长显示成 `1:23:45` / `45:00`。空值留空字符串。"""
    secs = parse_duration(v)
    if secs is None:
        return ""
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def normalize_published(v) -> str:
    """发布日期 → ISO 8601 日期（`YYYY-MM-DD`）。

    feed 的 `pub_date` 是 RFC 2822（`Mon, 02 Jun 2025 13:48:17 GMT`），
    用 email.utils 解析；已是 ISO 的直接取日期部分；解析不了留空。
    """
    if not v:
        return ""
    s = str(v).strip()
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(s).date().isoformat()
    except Exception:
        pass
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    m = re.match(r"^(\d{4})[/年](\d{1,2})[/月](\d{1,2})", s)
    if m:
        y, mo, d = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    return ""


def _first(*vals) -> str:
    for v in vals:
        if v not in (None, "", [], {}):
            return v
    return ""


def _strip_query(url: str) -> str:
    """剥掉 URL 的 query 与 fragment。权威字段不该带 utm 之类的跟踪参数。"""
    return str(url).split("?", 1)[0].split("#", 1)[0] if url else ""


def build(info: dict, *, transcript_status: str = "ok",
          summary_llm: str = "", tags_llm: list | None = None,
          generated_at: str = "") -> dict:
    """从 info.json 组装 rss 笔记的 frontmatter 字段。

    Args:
        info: `rss/raw/<标题>-<hash8>.info.json` 的内容（feed/item/local 三层）
        transcript_status: `ok` / `empty`（AC-C3-3 只有这两个值）
        summary_llm: LLM 产出的 summary
        tags_llm: LLM 产出的 tags
        generated_at: ISO 时间戳；不给就用当前时间

    Returns:
        字段名 → 值的 dict，**顺序即落盘顺序**。无数据的一律是空串（不省略 key），
        这样跨源比对时字段集是稳定的。
    """
    feed = info.get("feed") or {}
    item = info.get("item") or {}
    local = info.get("local") or {}

    desc = _first(feed.get("description"), item.get("description"), "")
    show_name = _first(feed.get("title"), "")
    duration = parse_duration(_first(item.get("duration_seconds"),
                                     item.get("duration"), local.get("duration")))
    empty = (transcript_status == "empty")

    return {
        "note_id": _first(item.get("guid_hash8"), ""),
        "title": _first(item.get("title"), ""),
        "uploader": show_name,                       # rss 侧 uploader = 节目名
        "uploader_id": "",                           # AC-C2：rss 侧空
        "speaker": extract_speaker(_first(item.get("title"), "")),  # rss 专用字段
        "type": TYPE_PODCAST,
        "duration": parse_duration_display(duration) if duration is not None else "",
        "published_at": normalize_published(item.get("pub_date")),
        # 单集链接：剥掉 feed 附带的跟踪 query，权威字段不该带 utm
        "source_url": _strip_query(_first(item.get("link"), "")),
        # 推不出来就留空，不填占位值（AC-C3-3）
        "subtitle_source": "" if empty else "asr",
        "transcript_status": transcript_status,
        "source_tags": "",                           # AC-C2：rss 侧空
        "tags": list(tags_llm or []),
        "summary": summary_llm or "",
        "generated_at": generated_at or datetime.now().astimezone().isoformat(
            timespec="seconds"),
        "description": desc,
        "show_name": show_name,
    }


DUMP_SAFE = str.maketrans({":": "：", "#": "＃", '"': "＂", "'": "＇",
                             "[": "［", "]": "］", "{": "｛", "}": "｝"})


def _scalar(v) -> str:
    """标量 → YAML 字面量。

    ⚠️ **换行必须转义成 \\n**（2026-10-04 修）：值里若含真换行，写出来的
    YAML 会跨行 —— 一旦值里还含 `---`（实际踩过：某期条目的 description 里就有），
    读回时会把它误认成 frontmatter 的闭合标记，**正文被整段吞掉**。
    转义成单行后这个失败模式彻底消失。
    """
    s = str(v)
    if s == "":
        return '""'
    if "\n" in s or "\r" in s or s.translate(DUMP_SAFE) != s \
            or re.search(r"^\s|\s$|:\s", s):
        esc = (s.replace("\\", "\\\\").replace('"', '\\"')
                .replace("\r\n", "\\n").replace("\n", "\\n")
                .replace("\r", "\\n"))
        return '"' + esc + '"'
    return s
def dump(fm: dict) -> str:
    """frontmatter dict → YAML 文本（含首尾 `---`）。"""
    out = ["---"]
    for k, v in fm.items():
        if isinstance(v, list):
            if not v:
                out.append(f"{k}: []")
            else:
                out.append(f"{k}:")
                out += [f"  - {_scalar(x)}" for x in v]
        elif isinstance(v, bool):
            out.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            out.append(f"{k}: {v}")
        else:
            out.append(f"{k}: {_scalar(v)}")
    out.append("---")
    return "\n".join(out)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python3 build_frontmatter.py <info.json>")
        raise SystemExit(1)
    print(dump(build(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))))
