#!/usr/bin/env python3
"""根据 transcript 净字数，决定用 skill 笔记还是 map-reduce 笔记。

用法：
    python3 decide_mode.py <transcript.md>
    例：python3 decide_mode.py rss/transcripts/独树不成林-361-f35eb04d.transcript.md

输出：
    stdout 打印 "skill" 或 "map_reduce"  # 后面跟理由
    exit code: 0 = skill, 1 = map_reduce

## 口径（2026-10-04 改，spec AC-A3）

    净字数 > 15,000  → map_reduce（长文路径）
    净字数 ≤ 15,000  → skill

「净字数」= 剥掉 ASR 段标记 `**[mm:ss]**` 后的非空白字符数，
口径实现在 `_shared/long_doc.py`（本项目唯一的长文计量定义）。

## 这次改了什么

改前是「原始字符 ≥ 50,000 → map_reduce」。原始字符里 ASR 时间戳占很大一块
（实测 48 篇 rss 里有 8,352 处标记剥不掉，集中在超 100 分钟的那篇，
最大 325 分钟），而按原始字符计量量纲失真 —— 导致
**24 篇（48 篇里正好一半）被当短文处理**，它们的净字 15,000–25,000、
原始字符 29,732–49,433，全部卡在 50,000 线下方。

统一到净字口径后这 24 篇改走长文路径。

设计说明：rss feed 全部是对话/观点类播客，统一阈值后不再需要按标题分流
（原设计的「临界区一律 map-reduce」是针对 50K–100K 档的，单一阈值下该规则
已被阈值本身覆盖）。

## ✅ 长文走 v3 四步管线（D6 已拍板，2026-10-04）

D6 决定：长文一律走 v3（clean → 纠错 → 分块扫描 → 合并去重 → 撰写 → 校验），
**不再走 map-reduce**。依据：4 篇同篇对照 4/4 同向，完全覆盖口径
Δ +37.7 ~ +79.4pp；map-reduce 的问题是「提到但讲不透」
（严格 Δ +37.7pp / 宽松仅 +11.4pp），且有结构性硬失败
（输出预算 4000 被 thinking 吃光导致 map 阶段截断，成功率仅 3/8）。

本仓库的入口是 `scripts/generate_note_v3.py`（薄壳：信头 + 落盘 + 索引）。
管线本体在 `_shared/build_v3.py`，**入口函数是 `generate_note_body()`**。

⚠️ exit code 约定没变：0 = 短文，1 = 长文。调用方按 mode 字符串分流。
"""
import pathlib
import re
import sys

# _shared/long_doc.py —— scripts/ → rss-grab/ → skills/ → _shared/
_SKILLS_DIR = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_SKILLS_DIR / "_shared"))

from long_doc import LONG_DOC_THRESHOLD, is_long_doc, net_len  # noqa: E402


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
    """
    text = transcript_path.read_text()
    n = net_len(text)

    if is_long_doc(text):
        return ("v3", f"net_len={n} > {LONG_DOC_THRESHOLD}")
    return ("skill", f"net_len={n} <= {LONG_DOC_THRESHOLD}")


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
