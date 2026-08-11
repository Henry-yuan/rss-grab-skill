#!/usr/bin/env python3
"""批量处理 RSS 待抓取文件。

输入：`待抓取URL/RSS.md`（- [ ] URL 一行一条）
行为：
  1. 读 ## 待抓取 段所有 - [ ] URL
  2. 每条：调 fetch_rss_feed.py（只落原料：音频 + info.json，不生成笔记）
  3. 成功 -> ✅ 写入「已抓取」段；失败 -> ❌ 写入「失败」段
  4. 重写文件 + 汇总

注意：只落原料，不转写、不生成笔记。
"""
import re, sys, subprocess, time, pathlib, datetime
from typing import Optional

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
FETCH_SCRIPT = SCRIPT_DIR / "fetch_rss_feed.py"

DEFAULT_PENDING_FILE = pathlib.Path("待抓取URL/RSS.md")
DEFAULT_OUT_DIR = pathlib.Path("rss")
SLEEP_BETWEEN = 5  # 秒（CDN 礼仪，与 fetch_rss_feed 一致）


def parse_rss_url(s: str) -> Optional[str]:
    """校验是否合法 RSS URL（http/https 开头）。"""
    s = s.strip()
    if not s:
        return None
    if re.match(r"^https?://", s):
        return s
    return None


def find_pending_urls(lines):
    """从 markdown 行里提取 ## 待抓取 段的 - [ ] URL。

    返回 [(line_index, url), ...]
    """
    pending = []
    in_pending = False
    for i, line in enumerate(lines):
        if line.startswith("## "):
            in_pending = line.startswith("## 待抓取")
            continue
        if in_pending:
            m = re.match(r'^- \[ \]\s*(\S.*)$', line.strip())
            if m and m.group(1):
                url = parse_rss_url(m.group(1).strip())
                if url:
                    pending.append((i, url))
    return pending


def fetch_one(url: str, out_dir: pathlib.Path) -> tuple[bool, str]:
    """调 fetch_rss_feed.py 抓一个 feed（--max 5 默认下最近 5 期）。"""
    try:
        r = subprocess.run(
            [sys.executable, str(FETCH_SCRIPT), url, "--out-dir", str(out_dir)],
            capture_output=True, text=True, timeout=1800,
        )
        if r.returncode == 0:
            return True, ""
        return False, (r.stderr.strip() or r.stdout.strip())[:300]
    except subprocess.TimeoutExpired:
        return False, "超时（30 分钟）"


def rebuild_md(lines, pending_items, results, date_str):
    """重写 markdown：处理过的行移到已抓取/失败段。"""
    processed_indices = {idx for idx, _ in pending_items}
    new_lines = [line for i, line in enumerate(lines) if i not in processed_indices]

    completed = [r["new_line"] for r in results if r["status"] == "ok"]
    failed = [r["new_line"] for r in results if r["status"] == "fail"]
    all_done = completed

    if not all_done and not failed:
        return "".join(new_lines)

    pending_section_idx = None
    for i, line in enumerate(new_lines):
        if line.startswith("## 待抓取"):
            pending_section_idx = i
            break

    extra_blocks = []
    if all_done:
        extra_blocks.append(f"## 已抓取（{date_str}）\n")
        extra_blocks.extend(l + "\n" for l in all_done)
        extra_blocks.append("\n")
    if failed:
        extra_blocks.append(f"## 失败（{date_str}）\n")
        extra_blocks.extend(l + "\n" for l in failed)
        extra_blocks.append("\n")

    if pending_section_idx is not None:
        new_lines = new_lines[:pending_section_idx] + extra_blocks + new_lines[pending_section_idx:]
    else:
        new_lines.extend(extra_blocks)
    return "".join(new_lines)


def main():
    md_path = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PENDING_FILE
    if not md_path.exists():
        sys.exit(f"❌ 文件不存在：{md_path}")

    out_dir = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT_DIR

    print(f"=== 批量处理 {md_path} ===")
    text = md_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    pending = find_pending_urls(lines)
    if not pending:
        print("📭 待抓取段没有 - [ ] URL，退出")
        return

    print(f"待处理：{len(pending)} 条")

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    date_str = datetime.datetime.now().strftime("%Y-%m-%d")

    results = []
    for orig_idx, url in pending:
        print(f"\n--- {url[:80]} ---")
        ok, err = fetch_one(url, out_dir)
        if ok:
            print(f"  ✅ 抓取完成（原料已落盘）")
            results.append({"status": "ok", "new_line": f"- [x] {url} ✅ {timestamp}"})
        else:
            print(f"  ❌ 抓取失败：{err[:200]}")
            results.append({"status": "fail", "new_line": f"- [x] {url} ❌ {timestamp} {err[:120]}"})
        time.sleep(SLEEP_BETWEEN)

    new_text = rebuild_md(lines, pending, results, date_str)
    md_path.write_text(new_text, encoding="utf-8")
    print(f"\n📝 已更新文件 {md_path}")

    n_ok = sum(1 for r in results if r["status"] == "ok")
    n_fail = sum(1 for r in results if r["status"] == "fail")
    print(f"\n=== 完成：✅ {n_ok} | ❌ {n_fail} ===")
    if n_ok:
        print(f"\n💡 接下来跑 asr_podcast.py --all 转写，再跑 generate_note.py 生成笔记")


if __name__ == "__main__":
    main()
