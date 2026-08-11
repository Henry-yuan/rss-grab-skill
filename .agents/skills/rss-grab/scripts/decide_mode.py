#!/usr/bin/env python3
"""根据 transcript 字符数，决定用 skill 笔记还是 map-reduce 笔记。

用法：
  python3 decide_mode.py <transcript.md>
  例：python3 decide_mode.py rss/transcripts/独树不成林-361-f35eb04d.transcript.md

输出：
  stdout 打印 "skill" 或 "map_reduce"  # 后面跟理由
  exit code: 0 = skill, 1 = map_reduce

阈值（rss 播客版）：
  - transcript_chars < 50,000  -> skill
  - transcript_chars >= 50,000 -> map_reduce

设计说明（为什么不保留临界区标题关键词判断）：
  临界区（50K-100K）两种模式都可用（skill 略省、map-reduce 略全）。
  实验结论：播客/访谈/对谈类内容倾向 map-reduce。
  rss feed 全部是对话/观点类播客，临界区一律 map-reduce 更简单且不会误伤。
  用户确认此设计（2026-08-09）。

注意：rss 播客普遍 35-60 分钟，transcript 约 20-40K 字符，多数走 skill 模式。
"""
import pathlib
import re
import sys

THRESHOLD_LOW = 50_000


def extract_hash8_from_path(path: pathlib.Path) -> str:
    """从 transcript 文件名提取 guid_hash8（最后一段 - 后的 8 位 hex）。

    例：独树不成林-361-f35eb04d.transcript.md -> f35eb04d
    """
    name = path.name
    # 去后缀
    name = re.sub(r"\.transcript\.md$", "", name)
    # 取最后一个 - 后的部分
    if "-" in name:
        return name.rsplit("-", 1)[-1]
    return ""


def decide(transcript_path: pathlib.Path) -> tuple[str, str]:
    """核心决策。返回 (mode, reason)。

    transcript 路径：<root>/rss/transcripts/<title>-<hash8>.transcript.md
    -> 项目根 = transcript_path.parent.parent.parent
    """
    text = transcript_path.read_text()
    chars = len(text)

    if chars < THRESHOLD_LOW:
        return ("skill", f"transcript_chars={chars} < {THRESHOLD_LOW}")
    return ("map_reduce", f"transcript_chars={chars} >= {THRESHOLD_LOW}")


def main():
    if len(sys.argv) < 2:
        print("用法：python3 decide_mode.py <transcript.md>", file=sys.stderr)
        sys.exit(2)
    transcript_path = pathlib.Path(sys.argv[1])
    if not transcript_path.exists():
        print(f"ERROR: 文件不存在: {transcript_path}", file=sys.stderr)
        sys.exit(2)
    mode, reason = decide(transcript_path)
    print(f"{mode}  # {reason}")
    sys.exit(0 if mode == "skill" else 1)


if __name__ == "__main__":
    main()
