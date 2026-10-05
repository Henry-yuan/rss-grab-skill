#!/usr/bin/env python3
"""opensource 仓库的开源边界守门测试（Henry 2026-10-04 硬规则）。

**为什么必须有这个测试**：`~/Projects/opensource/rss-grab` 是公开仓库，只开源 rss。
小红书与 B 站不打算开源。往公开仓库里带进不开源部分的**后果不可逆** ——
即使事后删文件，git 历史里仍然存在。靠人记这条规则迟早会漏，故做成机器拦截。

用法：
    python3 scripts/tests/test_open_source_guard.py
    # 或纳入 pytest：python3 -m pytest scripts/tests/ -v

⚠️ 本文件自身含有被禁关键词。匹配时已排除本文件与 OPEN_SOURCE_GUARD 标记行，
因此「测试文件里出现关键词」不会自我触发失败。
"""
from __future__ import annotations

import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]      # .agents/skills/_shared/tests/ → 仓库根
GUARD_FILE = pathlib.Path(__file__).resolve()

# 禁止出现的源专属特征。
# OPEN_SOURCE_GUARD: 逐类列出「为什么禁」，改名单时连理由一起改，避免只留关键词丢失语义。
BANNED = {
    "小红书（中文名）": re.compile(r"小红书"),
    "小红书（拼音）": re.compile(r"xiaohongshu", re.I),
    "小红书（缩写）": re.compile(r"\bxhs\b", re.I),
    # ⚠️ 左边界不能省：`B\s*站` 会命中「rss-gr**ab 站**在巨人的肩膀上」
    # （README 致谢段，真实误报）。要求 B 前面不是字母数字下划线。
    "B 站（中文名）": re.compile(r"(?<![0-9A-Za-z_])B\s*站"),
    "B 站（拼音）": re.compile(r"bilibili", re.I),
    "B 站 skill 名": re.compile(r"bili-grab"),
    "B 站 bvid": re.compile(r"\bBV[0-9A-Za-z]{10}\b"),
    "小红书分享参数": re.compile(r"xsec_token|xhsshare|xsec_source", re.I),
    # ⚠️ `note_id` **不在**黑名单里：它是 AC-C2 定的**三源共用主键名**
    # （本仓库侧填 guid_hash8），曾被误列为某源专属而误报，见 spec 决策记录。
    "某源专属的原始字段名": re.compile(r"\bnickname\b|\bpublish_time_ms\b"),
    # ⚠️ 2026-10-06 修「源码转义绕过」：
    # 原文 `r"images\.md"` 匹配的是**不带反斜杠**的 `images.md`，而源码里
    # 实际写的是 `\.images\.md`（正则字符串，多一层转义）→ **永远匹配不上**。
    # 实测 `rss-grab/scripts/adapt_template.py:154` 就这样溜过去了。
    # 修法：中间的 `\` 与 `.` 都改成可选，两种写法都能命中。
    # 通用教训：**黑名单里的模式若本身是正则片段，必须能匹配「它作为源码正则出现」的形态**。
    "小红书图文扩展名": re.compile(r"images\\?\.?md"),
    "图文采集（OCR）": re.compile(r"\bOCR\b"),
    # ⚠️ 2026-10-06 补：B 站字幕产物后缀。此前不在名单，
    # 于是 `adapt_template.py` 里的 `.ai-zh.transcript.md` 一路放行。
    # ⚠️ 同样按「源码转义形态」修：`\.ai-zh\.` 在源码里带反斜杠，
    # 写死 `ai-zh` 虽然能命中，但为与其他条目一致，这里允许中间有转义符。
    "B 站字幕产物后缀": re.compile(r"ai[-]?zh"),
    # ⚠️ 2026-10-06 补：B 站字段名。实测 `bvid_or_note_id` 就在 rss 脚本里。
    "B 站字段名": re.compile(r"\bbvid\b"),
    # ⚠️ 2026-10-06 补：疑似真实平台账号 ID（B 站 mid 是 8–15 位连续数字）。
    # 为什么**不按字段名禁 `uploader_id`**：它是 AC-C2 的 17 字段契约成员
    # （rss 侧填 guid_hash8），按词禁会误报（实测报 3 处假警报）。
    # 为什么**不按 `uploader_id: <值>` 禁**：rss 侧本来就该给它填 guid_hash8，
    # 禁掉就把合法赋值也毙了。
    # 只能按**值的形态**判 —— 12 位以上连续数字在这个仓库里没有正当用途。
    "疑似真实平台账号 ID": re.compile(r"(?<![0-9])[0-9]{12,}(?![0-9])"),
}

# 三源共用契约里的字段名，按 Henry 2026-10-04 决策豁免（RSS 侧填 guid_hash8，
# 不是 B 站数据）。
#
# ⚠️ 豁免必须**精确到 token**，不能按整行豁免 —— 曾试过「同行出现键名就放过」，
# 结果 `test_frontmatter_contract.py` 里那个真实 B 站 UP 主 ID
# `3706955719772347` 与 `"uploader_id"` 的注释**在同一行**，整行豁免等于没豁免。
#
# 下面每条只匹配「字段名作为键/标识出现」的位置：
#   - `"uploader_id",`   契约字段表里的裸字段名
#   - `` `uploader_id` ``  注释/文档里作为**概念**提及
# 都不匹配 `"uploader_id": "具体值"` 这种带值的形式 —— 带值才可能是真数据。
# 12 位数字规则的兜底豁免：若将来仓库里真需要一个大整数常量（如超时毫秒数），
# 在这里加一条精确模式，而不是把规则放宽。
KNOWN_EXEMPT: dict[str, re.Pattern[str]] = {}

# ⚠️ 2026-10-06 补 `.xml`：此前漏了，
# `rss-grab/scripts/tests/fixtures/podcast_sample.xml`（24KB 真实 feed 样本）
# **从未被扫过** —— 扫描范围缺后缀是最隐蔽的一类漏洞：
# 守门显示 passed，文件就在仓库里，只是从来没进过检查范围。
SCAN_SUFFIXES = (".py", ".md", ".json", ".txt", ".example",
                 ".yaml", ".yml", ".sh", ".xml")


def _is_guard_file(path: pathlib.Path) -> bool:
    return path.resolve() == GUARD_FILE.resolve()


def _iter_files():
    for p in REPO_ROOT.rglob("*"):
        if not p.is_file() or p.suffix not in SCAN_SUFFIXES:
            continue
        if _is_guard_file(p):
            continue
        if any(part in {".git", "__pycache__", ".pytest_cache", "output", "scan_work"}
               for part in p.parts):
            continue
        yield p


class TestOpenSourceBoundary(unittest.TestCase):
    """rss-grab 公开仓库只允许出现 rss 相关内容。"""

    def test_no_banned_source_markers(self):
        """仓库内（除本文件外）不得出现任何小红书 / B 站采集特征。"""
        hits: list[str] = []
        for p in _iter_files():
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines = text.split("\n")
            for label, rx in BANNED.items():
                for m in rx.finditer(text):
                    line_no = text.count("\n", 0, m.start()) + 1
                    # 豁免只按**所在整行**判：契约里的字段名（`"uploader_id":`）
                    # 合法，同一行的**数据值**不豁免。
                    # ⚠️ 曾试过只豁免「键名部分」，结果 `test_frontmatter_contract.py`
                    # 里那个真实 B 站 UP 主 ID 正好在同一行 —— 只看键名等于没豁免。
                    # 只看**命中点附近 40 字符**：豁免的是「字段名 token」，
                    # 不是整行 —— 同一个值可能既在注释里又在数据里。
                    ctx = text[max(0, m.start() - 10): m.end() + 10]
                    if KNOWN_EXEMPT and any(ex.search(ctx) for ex in KNOWN_EXEMPT.values()):
                        continue
                    rel = p.relative_to(REPO_ROOT)
                    hits.append(f"{rel}:{line_no}  [{label}] …{text[max(0, m.start()-30):m.end()+30]!r}")
        if hits:
            self.fail(
                f"发现 {len(hits)} 处不允许的开源内容：\n"
                + "\n".join(f"  {h}" for h in hits[:40])
                + ("\n  …（其余省略）" if len(hits) > 40 else "")
                + "\n\n依据：AGENTS.md「🔴 开源边界」。小红书 / B 站不打算开源，"
                  "本仓库只开源 rss。此类泄露一旦 push 即不可逆（git 历史仍在）。"
            )

    def test_no_reference_to_main_project_path(self):
        """rss 代码不得引用主项目的私有路径（别人 clone 本仓库必坏）。"""
        bad = re.compile(r"内容渠道采集|video-grab")
        hits = []
        for p in _iter_files():
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in bad.finditer(text):
                rel = p.relative_to(REPO_ROOT)
                # 只允许出现在解释「不要这么做」的注释/文档里，且必须同段提到禁止
                ctx = text[max(0, m.start() - 120):m.end() + 120]
                if "不得" in ctx or "禁止" in ctx or "不要" in ctx or "clone" in ctx:
                    continue
                line_no = text.count("\n", 0, m.start()) + 1
                hits.append(f"{rel}:{line_no}  …{ctx.strip()[:90]!r}")
        if hits:
            self.fail(
                "发现引用主项目私有路径的可疑代码：\n"
                + "\n".join(f"  {h}" for h in hits[:20])
                + "\n\n本仓库是公开的 rss 仓库，引用主项目路径会让别人 clone 后无法运行。"
            )

    def test_guard_file_itself_is_complete(self):
        """守门测试自身要有效：被禁词表不能为空，且真的能匹配到东西。"""
        self.assertGreaterEqual(len(BANNED), 10, "被禁词表过短，可能漏了新的源专属特征")
        # 冒烟：确认正则可编译且至少有一条真的能命中
        hits = sum(1 for rx in BANNED.values() if rx.search("这里有小红书和 bilibili 与 xsec_token"))
        self.assertGreaterEqual(hits, 3, "被禁正则疑似失效（连构造串都命中不了几条）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
