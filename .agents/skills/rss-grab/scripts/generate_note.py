#!/usr/bin/env python3
"""生成笔记（Step 2）：读 transcript 全文 + 适配后模板，调 LLM 生成结构化笔记（rss 版）。

用法:
    python3 scripts/generate_note.py <transcript_path> <template_plan_path> --info-json <info.json> --output <note.md>

与同系列笔记生成脚本的差异（本版为 rss 播客适配）：
  - get_capture_mtime：路径 rss/raw + rss/transcripts，按 guid_hash8 找
  - register_rss_index：按 guid_hash8 dedup，列 = 日期/标题/作者/时长/期号/笔记
  - format_info_json：适配 rss 的 feed/item/local 三层结构
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path

from openai import OpenAI

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent.parent / "_shared"))
from env import load_env
from paths import find_project_root


def read_template_body(template_path: Path) -> str:
    """读模板完整正文（frontmatter 之后的部分）。"""
    text = template_path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n.*?\n---\s*\n", text, re.DOTALL)
    if m:
        return text[m.end():]
    return text


def read_transcript_full(path: Path) -> str:
    """读 transcript 全文。"""
    return path.read_text(encoding="utf-8")


def format_info_json(info: dict) -> str:
    """格式化 rss info.json（feed/item/local 三层）为可读文本。"""
    lines = []
    for section in ("feed", "item", "local"):
        section_data = info.get(section, {})
        lines.append(f"# {section}")
        for k, v in section_data.items():
            if isinstance(v, (str, int, float)) and v:
                lines.append(f"- {k}: {v}")
    return "\n".join(lines)


def strip_thinking_blocks(text: str) -> str:
    """去掉 LLM 输出里的 <think>...</think> 思考痕迹块（M3 模型常见）。

    与统一正则一致（M3 实测输出<think>标签，已用真实 API 验证）。
    """
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)


def call_llm_for_note_generation(
    content: str,
    template_body: str,
    info_text: str,
    is_fallback: bool,
    source: str = "transcript",
) -> str:
    """调 LLM 生成笔记 Markdown。prompt 适配播客场景。

    source="transcript"：原始逐字稿（短播客 < 50K 字符）
    source="summary"：map-reduce 结构化摘要（长播客 > 50K 字符，先跑 map_reduce_note.py）
    """
    client = OpenAI(api_key=os.environ["LLM_API_KEY"], base_url=os.environ["LLM_BASE_URL"])

    if is_fallback:
        system_prompt = (
            "你是播客笔记生成助手。基于提供的 transcript、元信息生成结构化中文笔记。\n\n"
            "**重要**：transcript 是 ASR 机器转写产物，可能有同音错字（如事故vs世故）。"
            "请在生成笔记时主动识别并纠正--以 info.json 的 feed.description / item.title 作为权威参照"
            "（作者写的元数据，比 ASR 更准）。\n"
            "硬要求：\n"
            "1. 保留 4 个硬编码章节：播客元信息、TL;DR、关键引用、信息可信度\n"
            "2. 其他章节可自由组织（基于内容实际自然划分）\n"
            "3. 输出 Markdown，直接是笔记内容，不要加额外说明\n"
            "4. **ASR 错字纠正透明化**：在'信息可信度'章节末尾加'ASR 错字纠正'子段，"
            "列出识别并纠正的错字（格式：原 transcript 写'X'，实为'Y'，已修正）。"
            "如果没纠错也写'本笔记无 ASR 错字'。"
        )
        content_label = "## Map-reduce 结构化摘要" if source == "summary" else "## Transcript 全文"
    elif source == "summary":
        # summary 模式：内容已是 map-reduce 结构化摘要，模板骨架约束章节结构
        system_prompt = (
            "你是播客笔记生成助手。下方内容是长 transcript 的 map-reduce 结构化摘要（包含 TL;DR、"
            "按时间区间分段的章节、每段的关键要点和原文引用）。请基于这份摘要 + 模板骨架生成结构化中文笔记。\n\n"
            "**重要**：摘要源自 ASR 转写，可能有同音错字。请以 info.json 的 feed.description / item.title "
            "作为权威参照主动纠正。\n"
            "硬要求：\n"
            "1. 保留 4 个硬编码章节：播客元信息、TL;DR、关键引用、信息可信度\n"
            "2. 模板骨架的章节结构（背景与语境 / 主要话题 / 关键观点与论证 / 立场与反思）必须遵循\n"
            "3. 摘要中的 key_points 必须全部保留（不要压缩或合并）\n"
            "4. 摘要中的 quotes 必须保留（含时分）\n"
            "5. 软编码章节可以适度调整顺序\n"
            "6. 关键观点与论证章节：保留所有有实质内容的论证。数量参考下限：长播客（>2h）至少 25 个；"
            "中播客（1-2h）至少 15 个。宁可多保留，不要为精简而合并。\n"
            "7. TL;DR 的'主要话题清单'必须用按序号换行格式：1. 话题 / 2. 话题，每个单独一行。\n"
            "8. **ASR 错字纠正透明化**：在'信息可信度'末尾加'ASR 错字纠正'子段。\n\n"
            "输出 Markdown，直接是笔记内容，不要加额外说明。"
        )
        content_label = "## Map-reduce 结构化摘要（已摘要，保留所有 key_points 和 quotes）"
    else:
        # transcript 模式（默认）
        system_prompt = (
            "你是播客笔记生成助手。基于提供的 transcript、元信息和模板骨架生成结构化中文笔记。\n\n"
            "**重要**：transcript 是 ASR 机器转写产物，可能有同音错字（如事故vs世故）。"
            "请在生成笔记时主动识别并纠正--以 info.json 的 feed.description / item.title 作为权威参照"
            "（作者写的元数据，比 ASR 更准）。\n"
            "硬要求：\n"
            "1. 保留 4 个硬编码章节：播客元信息、TL;DR、关键引用、信息可信度\n"
            "2. 模板章节结构是参考，可省略不适用部分、调整顺序，或根据内容适度新增\n"
            "3. 关键观点与论证章节：保留所有有实质内容的论证（含具体观点/论据/金句），"
            "寒暄/重复可略。数量参考下限：长播客（>2h）至少 25 个；中播客（1-2h）至少 15 个；"
            "短播客（<1h）至少 5 个。宁可多保留，不要为精简而合并。\n"
            "4. TL;DR 的'主要话题清单'必须用按序号换行格式：1. 话题 / 2. 话题 / 3. 话题，"
            "每个话题单独一行，不要表格、不要 inline 列表。\n"
            "5. **ASR 错字纠正透明化**：在'信息可信度'章节末尾加'ASR 错字纠正'子段，"
            "列出识别并纠正的错字（格式：原 transcript 写'X'，实为'Y'，已修正）。"
            "如果没纠错也写'本笔记无 ASR 错字'。\n\n"
            "输出 Markdown，直接是笔记内容，不要加额外说明。"
        )
        content_label = "## Transcript 全文"

    user_prompt = f"""## 模板骨架
{template_body if not is_fallback else "(兜底模式，无模板)"}

## 元信息
{info_text}

{content_label}
{content}
"""

    resp = client.chat.completions.create(
        model=os.environ["LLM_MODEL"],
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
        max_tokens=32000,
    )
    content = resp.choices[0].message.content.strip()
    return strip_thinking_blocks(content)


def inject_note_metadata(note_md: str, style_detected: str, template_used: str) -> str:
    """在笔记最前面注入 YAML frontmatter。"""
    plan = {
        "style_detected": style_detected,
        "template_used": template_used,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds"),
    }
    fm_lines = ["---"]
    for k, v in plan.items():
        fm_lines.append(f"{k}: {v}")
    fm_lines.append("---\n")
    fm_block = "\n".join(fm_lines)
    if note_md.startswith("---\n"):
        m = re.match(r"^---\s*\n.*?\n---\s*\n", note_md, re.DOTALL)
        if m:
            return fm_block + note_md[m.end():]
    return fm_block + note_md


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


def main():
    parser = argparse.ArgumentParser(description="生成笔记（Step 2，rss 版）")
    parser.add_argument("transcript_path", type=Path)
    parser.add_argument("template_plan_path", type=Path)
    parser.add_argument("--info-json", type=Path, required=True, help="rss info.json 路径")
    parser.add_argument("--output", type=Path, required=True, help="输出笔记 .md 路径")
    parser.add_argument("--source", choices=["transcript", "summary"], default="transcript",
                        help="输入类型：transcript=原始逐字稿；summary=map-reduce final.json 摘要")
    parser.add_argument("--no-register-index", dest="register_index", action="store_false",
                        help="跳过 INDEX.md 追加（默认开）")
    parser.add_argument("--state-file", type=Path,
                        help="订阅状态文件路径（可选）。笔记生成成功后调 mark_done 标记已转化")
    parser.add_argument("--guid", type=str, default="",
                        help="本期 guid（--state-file 配套）。按 guid 在状态文件定位")
    args = parser.parse_args()

    load_env(SCRIPT_DIR)

    plan = json.loads(args.template_plan_path.read_text(encoding="utf-8"))
    info = json.loads(args.info_json.read_text(encoding="utf-8"))

    template_body = ""
    if not plan.get("is_fallback", True):
        template_path = Path(plan["template_used"])
        if template_path.exists():
            template_body = read_template_body(template_path)

    content = read_transcript_full(args.transcript_path)
    info_text = format_info_json(info)

    note_md = call_llm_for_note_generation(
        content=content,
        template_body=template_body,
        info_text=info_text,
        is_fallback=plan.get("is_fallback", True),
        source=args.source,
    )

    note_md = inject_note_metadata(
        note_md=note_md,
        style_detected=plan.get("style_detected", "待确认"),
        template_used=plan.get("template_used", "兜底"),
    )

    if plan.get("is_fallback", True):
        fallback_notice = (
            "> **⚠️ 此笔记走兜底生成**（未匹配任何模板）\n\n"
        )
        m = re.match(r"^---\s*\n.*?\n---\s*\n", note_md, re.DOTALL)
        if m:
            note_md = note_md[:m.end()] + "\n" + fallback_notice + note_md[m.end():]
        else:
            note_md = fallback_notice + note_md

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(note_md, encoding="utf-8")
    print(f"✅ 笔记已生成: {args.output}")

    if args.register_index:
        source_name = ""
        if args.state_file:
            try:
                import subscribe_manager
                st = subscribe_manager.load_state(args.state_file)
                source_name = st.get("frontmatter", {}).get("source", "")
            except Exception:
                pass
        result = register_rss_index(info, args.output, source_name)
        if result == "added":
            print(f"📝 INDEX.md 已追加: {args.output.parent / 'INDEX.md'}")
        elif result == "exists":
            print(f"⏭️  INDEX.md 已存在该 guid_hash8，跳过追加")

    # mark_done：笔记生成成功后，把状态文件对应期标记为"已转化"
    if args.state_file and args.guid:
        try:
            import subscribe_manager
            ok = subscribe_manager.mark_done(args.state_file, args.guid, str(args.output))
            if ok:
                print(f"✅ 状态文件已标记已转化: {args.state_file.name}")
            else:
                print(f"⚠️  状态文件里找不到 guid={args.guid}，未标记")
        except Exception as e:
            print(f"⚠️  mark_done 失败: {e}")


if __name__ == "__main__":
    main()
