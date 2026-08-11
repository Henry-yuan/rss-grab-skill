#!/usr/bin/env python3
"""重新生成 rss 笔记：decide_mode 判断 -> adapt_template -> (skill|map_reduce) -> generate_note。

用法:
    python3 scripts/regenerate_note.py <guid_hash8> [--template <path>] [--keep-v1] [--mode auto|skill|map_reduce]
    python3 scripts/regenerate_note.py --transcript <path> --info <info.json> --output <note.md> [--mode auto]
"""
from __future__ import annotations

import argparse
import datetime
import json
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent.parent / "_shared"))
from paths import find_project_root

PROJECT_ROOT = find_project_root()
RSS_TRANSCRIPT_DIR = PROJECT_ROOT / "rss" / "transcripts"
RSS_RAW_DIR = PROJECT_ROOT / "rss" / "raw"
RSS_NOTE_DIR = PROJECT_ROOT / "rss" / "notes"

# adapt_template.py 就在本仓库 scripts/ 下（不硬编码路径，传 --templates-dir）
ADAPT_TEMPLATE = PROJECT_ROOT / ".agents" / "skills" / "rss-grab" / "scripts" / "adapt_template.py"
RSS_TEMPLATES_DIR = PROJECT_ROOT / ".agents" / "skills" / "rss-grab" / "templates"


def find_transcript(guid_hash8: str) -> Path:
    """在 rss/transcripts/ 找 *-<hash8>.transcript.md。"""
    if not RSS_TRANSCRIPT_DIR.exists():
        raise FileNotFoundError(f"{RSS_TRANSCRIPT_DIR} 不存在")
    matches = list(RSS_TRANSCRIPT_DIR.glob(f"*-{guid_hash8}.transcript.md"))
    if not matches:
        raise FileNotFoundError(f"找不到 guid_hash8={guid_hash8} 的 transcript")
    return matches[0]


def find_info_json(transcript_path: Path) -> Path:
    """transcript -> 对应 info.json（同名换后缀）。"""
    info_name = transcript_path.name.replace(".transcript.md", ".info.json")
    info_path = RSS_RAW_DIR / info_name
    if not info_path.exists():
        raise FileNotFoundError(f"找不到 info.json: {info_path}")
    return info_path


def find_existing_note(guid_hash8: str) -> Path | None:
    """找现有笔记（如有）。递归搜索子目录（笔记按源分目录）。"""
    if not RSS_NOTE_DIR.exists():
        return None
    for f in RSS_NOTE_DIR.rglob(f"*-{guid_hash8}.md"):
        if f.name != "INDEX.md":
            return f
    return None


def _source_dir_from_info(info_path: Path) -> Path:
    """按 info.json 的 feed.title 决定笔记子目录：rss/notes/<源名>/。"""
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
        feed_title = info.get("feed", {}).get("title", "")
    except Exception:
        feed_title = ""
    safe = ""
    if feed_title:
        import re as _re
        safe = _re.sub(r'[^0-9A-Za-z一-鿿]+', '-', feed_title)
        safe = _re.sub(r'-+', '-', safe).strip('-')
        if len(safe) > 60:
            safe = safe[:60].rstrip('-')
    return RSS_NOTE_DIR / (safe or "未命名")


def run_adapt_template(transcript_path: Path, output_dir: Path) -> Path:
    """跑 adapt_template.py（本仓库脚本），返回 template_plan.json 路径。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, str(ADAPT_TEMPLATE),
        str(transcript_path),
        "--templates-dir", str(RSS_TEMPLATES_DIR),
        "--output-dir", str(output_dir),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("STDOUT:", r.stdout, file=sys.stderr)
        print("STDERR:", r.stderr, file=sys.stderr)
        raise RuntimeError(f"adapt_template.py 失败: {r.returncode}")
    print(r.stdout)
    stem = transcript_path.name.replace(".transcript.md", "")
    return output_dir / f"{stem}.template_plan.json"


def run_decide_mode(transcript_path: Path) -> tuple[str, str]:
    """调用 rss decide_mode.py，返回 (mode, reason)。"""
    decide_script = SCRIPT_DIR / "decide_mode.py"
    if not decide_script.exists():
        return "skill", "decide_mode.py 不存在，默认 skill 模式"
    r = subprocess.run(
        [sys.executable, str(decide_script), str(transcript_path)],
        capture_output=True, text=True, timeout=30,
    )
    out = r.stdout.strip()
    if "map_reduce" in out:
        return "map_reduce", out.split("#", 1)[-1].strip() if "#" in out else ""
    return "skill", out.split("#", 1)[-1].strip() if "#" in out else ""


def render_final_as_md(final: dict) -> str:
    """把 map-reduce 的 final.json 渲染成结构化 markdown（充当 generate_note 的 summary 输入）。

    复用统一逻辑：保留所有 key_points（不压缩）+ 所有 quotes（含时分）。
    """
    md = []
    meta = final.get("meta", {})

    md.append(f"# {meta.get('title', '?')}")
    md.append("")
    md.append(f"> **类型**: {meta.get('type', '?')}")
    md.append(f"> **时长**: {meta.get('duration_sec', '?')}秒")
    if meta.get("subjects"):
        md.append(f"> **话题**: {', '.join(meta['subjects'])}")
    md.append("")

    if "tldr" in meta:
        md.append("## TL;DR (map-reduce 摘要)")
        tldr = meta["tldr"]
        if isinstance(tldr, list):
            for line in tldr:
                md.append(f"- {line}")
        else:
            md.append(tldr)
        md.append("")

    for ch in final.get("chapters", []):
        md.append(f"## {ch.get('index', '?')}. {ch.get('title', '?')}")
        if ch.get("time_range"):
            tr = ch["time_range"]
            md.append(f"> **时间区间**: {tr[0]} - {tr[1]}")
        if ch.get("subjects"):
            md.append(f"> **话题**: {', '.join(ch['subjects'])}")
        md.append("")
        for kp in ch.get("key_points", []):
            md.append(f"- {kp}")
        md.append("")
        for q in ch.get("quotes", []):
            md.append(f"> {q}")
        md.append("")

    return "\n".join(md)


def run_map_reduce(transcript_path: Path, plan_path: Path,
                   output: Path, info_path: Path) -> None:
    """map-reduce 模式：map_reduce_note.py -> final.json -> 渲染 intermediate.md -> generate_note --source summary。"""
    map_reduce_script = SCRIPT_DIR / "map_reduce_note.py"
    if not map_reduce_script.exists():
        raise RuntimeError(f"找不到 map_reduce_note.py: {map_reduce_script}")

    # 1. 跑 map_reduce_note.py（生成 final.json）
    print("   ▶ 跑 map_reduce_note.py（map 阶段 + reduce 阶段）")
    r = subprocess.run(
        [sys.executable, str(map_reduce_script), str(transcript_path)],
        capture_output=True, text=True, timeout=7200,
    )
    if r.returncode != 0:
        print("STDOUT:", r.stdout[-500:], file=sys.stderr)
        print("STDERR:", r.stderr[:500:], file=sys.stderr)
        raise RuntimeError(f"map_reduce_note.py 失败: {r.returncode}")
    print(r.stdout[-500:] if len(r.stdout) > 500 else r.stdout)

    # final.json 路径：output/<id>.final.json（id = transcript 文件名去后缀，与 map_reduce_note 输出一致）
    stem = transcript_path.name.replace(".transcript.md", "")
    final_json = SCRIPT_DIR / "output" / f"{stem}.final.json"
    if not final_json.exists():
        raise RuntimeError(
            f"map_reduce_note.py 未生成 final.json: {final_json}\n"
            f"  检查 output/ 目录是否有对应文件"
        )
    print(f"   final.json: {final_json}")

    # 2. 渲染 final.json -> intermediate.md（结构化 markdown 给 LLM）
    final = json.loads(final_json.read_text(encoding="utf-8"))
    intermediate_md = render_final_as_md(final)
    intermediate_md_path = SCRIPT_DIR / "output" / f"{stem}.summary.md"
    intermediate_md_path.parent.mkdir(parents=True, exist_ok=True)
    intermediate_md_path.write_text(intermediate_md, encoding="utf-8")
    print(f"   intermediate.md: {intermediate_md_path} ({len(intermediate_md)} 字符)")

    # 3. 调 generate_note.py --source summary（LLM 按模板骨架重新组织）
    print("   ▶ 跑 generate_note.py --source summary")
    cmd = [
        sys.executable, str(SCRIPT_DIR / "generate_note.py"),
        str(intermediate_md_path), str(plan_path),
        "--info-json", str(info_path),
        "--output", str(output),
        "--source", "summary",
    ]
    cmd += _mark_done_args(info_path)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("STDOUT:", r.stdout, file=sys.stderr)
        print("STDERR:", r.stderr, file=sys.stderr)
        raise RuntimeError(f"generate_note.py 失败: {r.returncode}")
    print(r.stdout.strip())

    # 4. 清理 summary.md 临时文件
    intermediate_md_path.unlink()
    print(f"   🗑️  清理 summary 临时文件: {intermediate_md_path}")


def _mark_done_args(info_path: Path) -> list[str]:
    """构造 mark_done 衔接参数（--state-file/--guid）。有订阅状态文件才传。

    从 info.json 拿完整 guid，跨订阅表找对应状态文件（按 feed title 匹配）。
    找不到返回 []（不衔接，纯笔记流程）。
    """
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
        guid = info.get("item", {}).get("guid", "")
        feed_title = info.get("feed", {}).get("title", "")
        if not guid or not feed_title:
            return []
        import subscribe_manager
        subs = subscribe_manager.load_subscriptions()
        for src in subs["sources"]:
            if src.get("name") == feed_title:
                return ["--state-file", str(PROJECT_ROOT / src["state_file"]),
                        "--guid", guid]
    except Exception:
        pass
    return []


def run_generate_note(transcript_path: Path, plan_path: Path,
                      info_path: Path, output: Path):
    """skill 模式：直接跑 generate_note.py（transcript 全文）。"""
    cmd = [
        sys.executable, str(SCRIPT_DIR / "generate_note.py"),
        str(transcript_path), str(plan_path),
        "--info-json", str(info_path),
        "--output", str(output),
    ]
    cmd += _mark_done_args(info_path)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("STDOUT:", r.stdout, file=sys.stderr)
        print("STDERR:", r.stderr, file=sys.stderr)
        raise RuntimeError(f"generate_note.py 失败: {r.returncode}")
    print(r.stdout.strip())


def main():
    parser = argparse.ArgumentParser(description="重生成 rss 笔记")
    parser.add_argument("guid_hash8", nargs="?", help="8 位 guid hash（或用 --transcript）")
    parser.add_argument("--transcript", type=Path, help="直接指定 transcript 路径")
    parser.add_argument("--info", type=Path, help="直接指定 info.json 路径")
    parser.add_argument("--output", type=Path, help="直接指定输出笔记路径")
    parser.add_argument("--template", type=Path, help="指定模板（跳过 adapt_template）")
    parser.add_argument("--mode", choices=["auto", "skill", "map_reduce"], default="auto",
                        help="生成模式（auto=decide_mode 判断；默认 auto）")
    parser.add_argument("--keep-v1", action="store_true", help="保留旧笔记为 _v1")
    args = parser.parse_args()

    # 确定 transcript / info / output
    if args.transcript:
        transcript_path = args.transcript.resolve()
        info_path = args.info.resolve() if args.info else find_info_json(transcript_path)
        if not args.output:
            stem = transcript_path.name.replace(".transcript.md", "")
            source_dir = _source_dir_from_info(info_path)
            args.output = source_dir / f"{stem}.md"
    else:
        if not args.guid_hash8:
            parser.error("需要 guid_hash8 或 --transcript")
        transcript_path = find_transcript(args.guid_hash8)
        info_path = args.info or find_info_json(transcript_path)
        existing = find_existing_note(args.guid_hash8)
        if not args.output:
            if existing:
                args.output = existing
            else:
                stem = transcript_path.name.replace(".transcript.md", "")
                source_dir = _source_dir_from_info(info_path)
                args.output = source_dir / f"{stem}.md"

    print(f"📄 transcript: {transcript_path}")
    print(f"📄 info.json:  {info_path}")
    print(f"📄 输出:       {args.output}")

    RSS_NOTE_DIR.mkdir(parents=True, exist_ok=True)

    # 保留旧笔记
    if args.keep_v1 and args.output.exists():
        v1 = args.output.with_name(args.output.stem + "_v1.md")
        shutil.copy2(args.output, v1)
        print(f"📦 保留旧笔记: {v1}")

    # Step 1: adapt_template（或用指定模板）
    output_dir = SCRIPT_DIR / "output"
    if args.template:
        plan = {
            "style_detected": "指定模板",
            "template_used": str(args.template),
            "is_fallback": False,
        }
        stem = transcript_path.name.replace(".transcript.md", "")
        plan_path = output_dir / f"{stem}.template_plan.json"
        output_dir.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        plan_path = run_adapt_template(transcript_path, output_dir)

    # Step 2: 判断模式 + 生成
    if args.mode == "auto":
        mode, reason = run_decide_mode(transcript_path)
        print(f"📋 decide_mode: {mode} ({reason})")
    else:
        mode = args.mode

    if mode == "map_reduce":
        print("\n▶ map-reduce 模式（长播客）")
        run_map_reduce(transcript_path, plan_path, args.output, info_path)
    else:
        print("\n▶ skill 模式（短播客）")
        run_generate_note(transcript_path, plan_path, info_path, args.output)
    print(f"\n✅ 重生成完成: {args.output}")


if __name__ == "__main__":
    main()
