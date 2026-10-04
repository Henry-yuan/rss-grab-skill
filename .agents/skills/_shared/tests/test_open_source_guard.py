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

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]      # .agents/skills/_shared/tests/ → 仓库根
GUARD_FILE = pathlib.Path(__file__).resolve()

# 禁止出现的源专属特征。
# OPEN_SOURCE_GUARD: 逐类列出「为什么禁」，改名单时连理由一起改，避免只留关键词丢失语义。
BANNED = {
    "小红书（中文名）": re.compile(r"小红书"),
    "小红书（拼音）": re.compile(r"xiaohongshu", re.I),
    "小红书（缩写）": re.compile(r"\bxhs\b", re.I),
    "B 站（中文名）": re.compile(r"B\s*站|B站", re.I),
    "B 站（拼音）": re.compile(r"bilibili", re.I),
    "B 站 skill 名": re.compile(r"bili-grab"),
    "B 站 bvid": re.compile(r"\bBV[0-9A-Za-z]{10}\b"),
    "小红书分享参数": re.compile(r"xsec_token|xhsshare|xsec_source", re.I),
    # ⚠️ `note_id` **不在**黑名单里：它是 AC-C2 定的**三源共用主键名**
    # （本仓库侧填 guid_hash8），曾被误列为某源专属而误报，见 spec 决策记录。
    "某源专属的原始字段名": re.compile(r"\bnickname\b|\bpublish_time_ms\b"),
    "小红书图文扩展名": re.compile(r"images\.md"),
    "图文采集（OCR）": re.compile(r"\bOCR\b"),
}

SCAN_SUFFIXES = (".py", ".md", ".json", ".txt", ".example", ".yaml", ".yml", ".sh")


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
            for label, rx in BANNED.items():
                for m in rx.finditer(text):
                    line_no = text.count("\n", 0, m.start()) + 1
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
