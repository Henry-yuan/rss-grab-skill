#!/usr/bin/env python3
"""适配模板：读 transcript 前 2-3K 字 + 所有模板 frontmatter，调 LLM 选模板。

输出 scripts/output/<id>.template_plan.json。

用法:
    python3 scripts/adapt_template.py <transcript_path> [--templates-dir templates] [--output-dir scripts/output]
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

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.minimaxi.com/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "MiniMax-M3")
SCRIPT_DIR = Path(__file__).resolve().parent
TRUNCATE_CHARS = 3000
sys.path.insert(0, str(SCRIPT_DIR.parent.parent / "_shared"))
from env import load_env


def read_transcript_head(path: Path, chars: int = TRUNCATE_CHARS) -> tuple[str, int]:
    """读 transcript 前 N 字符，返回 (前N字内容, 总字符数)。"""
    text = path.read_text(encoding="utf-8")
    return text[:chars], len(text)


def parse_frontmatter(md_path: Path) -> dict | None:
    """简易 YAML frontmatter 解析（仅支持 spec §3.2 定义的 3 字段）。

    支持:
      ---
      key: value
      key: "quoted value"
      key:
        - item1
        - item2
      ---
    """
    text = md_path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return None
    block = m.group(1)
    result: dict = {}
    current_key = None
    for line in block.splitlines():
        if not line.strip():
            continue
        if line.lstrip().startswith("- "):
            if current_key and current_key in result and isinstance(result[current_key], list):
                result[current_key].append(line.lstrip()[2:].strip())
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if not value:
                current_key = key
                result[key] = []
            else:
                if (value.startswith('"') and value.endswith('"')) or (
                    value.startswith("'") and value.endswith("'")
                ):
                    value = value[1:-1]
                result[key] = value
                current_key = key
    return result


def collect_template_frontmatters(templates_dir: Path) -> list[dict]:
    """收集 templates/ 下所有 .md 文件的 frontmatter。"""
    if not templates_dir.exists():
        return []
    result = []
    for md_path in sorted(templates_dir.glob("*.md")):
        fm = parse_frontmatter(md_path)
        if fm:
            result.append({"path": str(md_path), **fm})
    return result


def call_llm_for_template_choice(
    transcript_head: str,
    templates: list[dict],
) -> dict:
    """调 LLM 让其选模板。返回 {style_detected, template_used, is_fallback, adaptation_notes}。"""
    client = OpenAI(api_key=os.environ["LLM_API_KEY"], base_url=LLM_BASE_URL)

    template_list_str = "\n".join(
        f"- {t['path']}: style={t.get('style', '?')}, description={t.get('description', '?')}"
        for t in templates
    )

    system_prompt = (
        "你是笔记模板选择助手。根据用户提供的 transcript 片段和模板清单，"
        "选择最合适的一个模板。如果都不合适，输出 is_fallback=true，"
        "template_used 设为'兜底'，style_detected 设为'待确认'。"
        "必须返回严格 JSON，不要有任何额外文字。"
    )

    user_prompt = f"""## 模板清单
{template_list_str if template_list_str else "(无模板)"}

## Transcript 前 {TRUNCATE_CHARS} 字
{transcript_head}

## 输出格式（严格 JSON）
{{
  "style_detected": "教程 或 其他风格名 或 待确认",
  "template_used": "templates/教程.md 或 兜底",
  "is_fallback": false 或 true,
  "adaptation_notes": "本次适配的简短说明（1-2 句话），比如'省略 选购建议 章节'或'无合适模板，走兜底'"
}}"""

    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
        # 显式大值，让 LLM 充分发挥
        max_tokens=32000,
    )
    content = resp.choices[0].message.content.strip()

    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*\n", "", content)
        content = re.sub(r"\n```\s*$", "", content)

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def derive_id_from_path(transcript_path: Path) -> str:
    """从 transcript 文件名派生 id。例：B 站 BV198jE6pE8Y.ai-zh.transcript.md → BV198jE6pE8Y。"""
    name = transcript_path.name
    name = re.sub(r"\.ai-zh\.transcript\.md$|\.transcript\.md$|\.images\.md$", "", name)
    return name


def main():
    parser = argparse.ArgumentParser(description="适配模板（Step 1）")
    parser.add_argument("transcript_path", type=Path, help="transcript 文件路径")
    parser.add_argument("--templates-dir", type=Path, default=Path(__file__).resolve().parent.parent / "templates", help="模板目录（默认自动检测调用者 skill 自己的 templates/）")
    parser.add_argument("--output-dir", type=Path, default=SCRIPT_DIR / "output", help="输出目录（默认 skill 内 scripts/output）")
    parser.add_argument("--title", type=str, default="", help="视频/笔记标题（可选）")
    args = parser.parse_args()

    load_env(SCRIPT_DIR)

    transcript_head, total_chars = read_transcript_head(args.transcript_path)
    templates = collect_template_frontmatters(args.templates_dir)
    if not templates:
        print(f"WARNING: {args.templates_dir} 下没有模板，走兜底", file=sys.stderr)

    llm_result = call_llm_for_template_choice(transcript_head, templates)

    plan = {
        "bvid_or_note_id": derive_id_from_path(args.transcript_path),
        "title": args.title or args.transcript_path.stem,
        "transcript_chars": total_chars,
        "truncated_chars_used": len(transcript_head),
        "style_detected": llm_result.get("style_detected", "待确认"),
        "template_used": llm_result.get("template_used", "兜底"),
        "is_fallback": llm_result.get("is_fallback", True),
        "adaptation_notes": llm_result.get("adaptation_notes", ""),
        "generated_at": datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds"),
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_path = args.output_dir / f"{plan['bvid_or_note_id']}.template_plan.json"
    output_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"✅ 模板适配完成")
    print(f"   style_detected: {plan['style_detected']}")
    print(f"   template_used:  {plan['template_used']}")
    print(f"   is_fallback:    {plan['is_fallback']}")
    print(f"   输出:           {output_path}")


if __name__ == "__main__":
    main()
