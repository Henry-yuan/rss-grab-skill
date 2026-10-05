#!/usr/bin/env python3
"""根据 transcript 净字数，决定笔记生成走 v3 的哪条路径。

用法：
    python3 decide_mode.py <transcript.md>
    例：python3 decide_mode.py rss/transcripts/独树不成林-361-f35eb04d.transcript.md

## 契约（2026-10-05 两源统一）

    route 字符串：**长短文都返回 "v3"**（D6 拍板，只有一条 v3 管线）
    stdout：      "<route>  # <理由>"

    exit code：
        0 = 短文（v3 单次调用路径）
        1 = 长文（v3 分块路径）
        2 = 用法错误（缺参数 / 文件不存在）

    ⚠️ 调用方按 route 字符串分流，**不是按 exit code**；
    exit code 只表达长短文档位，供外部脚本判断。

    旧的两条 route 名（分别代表「短文由当前 agent 直写」和「长文走 v2
    两段式摘要」）已于 2026-10-05 废止（V1.0 D6），本文件不再返回它们。
    本仓库旧写法是短文返回 "skill"、长文返回 "v3"，与主项目不一致，
    同一份调用逻辑会分流到不同分支 —— 已统一为都返回 "v3"。

本文件的契约（route 字符串 + exit code 三档）由跨源测试
`test_decide_mode_contract.py` 断言：任一路采集源与之不一致即变红。

## 口径（2026-10-04 改，spec AC-A3）

    净字数 > 15,000  → 长文（v3 分块路径）
    净字数 ≤ 15,000  → 短文（v3 单次调用路径）

「净字数」= 剥掉 ASR 段标记 `**[mm:ss]**` 后的非空白字符数，
口径实现在 `_shared/long_doc.py`（本项目唯一的长文计量定义）。

## 历史：为什么从「原始字符」改成「净字数」

改前是「原始字符 ≥ 50,000 判长文」。原始字符里 ASR 时间戳占很大一块
（实测 48 篇 rss 里有 8,352 处标记剥不掉，集中在超 100 分钟的那篇，
最大 325 分钟），而按原始字符计量量纲失真 —— 导致
**24 篇（48 篇里正好一半）被当短文处理**，它们的净字 15,000–25,000、
原始字符 29,732–49,433，全部卡在 50,000 线下方。
统一到净字口径后这 24 篇改走长文路径。

设计说明：rss feed 全部是对话/观点类播客，统一阈值后不再需要按标题分流
（原设计的「临界区一律长文」是针对 50K–100K 档的，单一阈值下该规则
已被阈值本身覆盖）。

## ✅ 长文走 v3 四步管线（D6 已拍板，2026-10-04）

D6 决定：长文一律走 v3（clean → 纠错 → 分块扫描 → 合并去重 → 撰写 → 校验）。
依据：4 篇同篇对照 4/4 同向，完全覆盖口径 Δ +37.7 ~ +79.4pp；
被取代的 v2 两段式摘要路径问题是「提到但讲不透」（严格 Δ +37.7pp /
宽松仅 +11.4pp），且有结构性硬失败（输出预算 4000 被 thinking 吃光
导致分块阶段截断，成功率仅 3/8）。

本仓库的入口是 `scripts/generate_note_v3.py`（薄壳：信头 + 落盘 + 索引）。
管线本体在 `_shared/build_v3.py`，**入口函数是 `generate_note_body()`**。
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
    return ("v3", f"net_len={n} <= {LONG_DOC_THRESHOLD} · 短文路径（同样走 v3）")


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
    # exit code 只表达长短文档位，**不依赖 route 字符串**：
    #   0 = 短文（v3 单次调用）  1 = 长文（v3 分块）  2 = 用法错误
    # 旧写法 `0 if mode == "skill" else 1` 与 route 耦合，
    # 短文 route 统一成 "v3" 后会把短文误判成 exit 1，故改为直接问 is_long_doc。
    sys.exit(1 if is_long_doc(transcript_path.read_text()) else 0)


if __name__ == "__main__":
    main()
