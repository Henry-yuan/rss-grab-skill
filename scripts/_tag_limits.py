"""标签检查的阈值 —— `retag.py` 与 `audit_frontmatter.py` 的**唯一真源**。

## 为什么要单独一个文件

打标（`retag.py`，写盘前**阻断**）与体检（`audit_frontmatter.py`，事后**报告**）
都要用到同一组阈值。此前两边各写一份 `MAX_TAGS = 10` / `MAX_TAG_CHARS = 8`，
改一边另一边不会跟着变 —— 后果是两边口径错位：

- 把 `MAX_TAGS` 调到 15 → retag 放行 11~15 个，audit 仍按 10 判定，
  **把正常笔记报成 G2 超限**；
- 调到 8 → retag 拦下 9~10 个，audit 按 10 **放过**本该拦的。

## 为什么不干脆让 audit import retag

`retag.py` 在被 import 时会 `sys.path.insert` 注入 `_shared` 并 import
`openai` / `env`。体检脚本本来是个零依赖的轻量工具，
不该为了两个数字绑上 LLM SDK 及其 import 期副作用
（side effects at import time 是最难测的一类依赖）。

本模块**只有常量、没有 import**，因此可以安全地被任何一方导入。

## 改动纪律

阈值要改，**只改这里**，然后跑：

```bash
/usr/local/bin/python3 -m pytest .agents/skills/_shared/tests/ -q   # 无关，但确认没打破导入
/usr/local/bin/python3 scripts/audit_frontmatter.py                # 应仍 exit 0
```
"""

# G2 · 标签数量上限。retag.py 写盘前阻断；audit 报告为 finding。
MAX_TAGS = 10

# G3 · 单个标签字符数上限。**仅报告不阻断** —— 实测 `LangGraph`(9)、
# `ReAct Agent`(11) 这类英文术语完全合理，硬截断只会产出残词。
MAX_TAG_CHARS = 8

# G4 · 近义标签的字符集 Jaccard 阈值。**仅报告不阻断**。
# 用字符集而非词集：中文短术语没有空格可切词，词级 Jaccard 对
# 「模型管理 / AI 管理」这类跨语言近义会算成 0。
JACCARD_NEAR_DUP = 0.6
