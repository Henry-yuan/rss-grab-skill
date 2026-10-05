#!/usr/bin/env python3
"""源文件清洗：剥掉不该进 prompt 的内容。

剥三类：
- ASR 失败横幅（转写未识别到有效内容时，脚本会打横幅 —— 那不是内容）
- `> 来源：…` / `> 时长：…` 头部元信息行（原始元数据，单独喂给模型，不该占正文）
- 分享链接里的 query 凭据（正文里若出现链接，不应带凭据）

清洗后只留下可核验的原始内容。

> ⚠️ **与主项目那份的差异**（手动双改，禁止单向覆盖）：
> 主项目那份还处理两类本仓库用不到的东西 —— 图像类素材的「描述」重复段、
> 以及某一来源的分享凭据参数名。本仓库只处理语音转写，故全部删去，
> 保留上面三类。差异已用 AST 比对确认。
"""
from __future__ import annotations

import re

# 头部元信息行：`> 来源：…` / `> 时长：xxx · 语言：zh · 模型：…`
META_LINE = re.compile(r"^>\s*(来源|时长|图片数|作者)[：:].*$", re.M)

# ASR 失败横幅：只匹配真正的横幅行，避免误伤正文
# 注意 ⚠️ 是 U+26A0 + U+FE0F(变体选择符) 两个码位，必须容忍
BANNER = re.compile(
    r"^#{0,3}\s*[>|-]?\s*[⚠❗️]?\s*ASR\s*失败.*$"
    r"|^>\s*本视频\s*ASR\s*转写未识别到有效内容.*$"
    r"|^>\s*建议人工处理或检查视频源。?\s*$",
    re.M,
)

# 通用分享凭据：本仓库的 feed 可能带各类跟踪/凭据 query，一律剥掉
_SHARE_PARAMS = ("utm_source", "utm_medium", "utm_campaign", "utm_term",
                 "utm_content", "source", "ref", "referrer", "spm")


def strip_share_token(text: str) -> str:
    """去掉 URL 里的 query 参数（保留路径本身）。

    本仓库不保留任何来源特有的凭据参数名 —— 统一按通用跟踪参数清单剥离，
    避免把某个来源的实现细节写进公开仓库。
    """
    params = "|".join(_SHARE_PARAMS)
    text = re.sub(rf"\b({params})\s*=\s*[^\s)\]\"'&,]+", "", text)
    text = re.sub(rf"[?&]({params})=[^\s)\]\"']+", "", text)
    # 清理行尾残留的 ? 或 & 前导
    text = re.sub(r"(https?://[^\s)\]\"']*?)\?[^\s)\]\"']*", r"\1", text)
    text = re.sub(r"&(?=\s|$)", "", text)
    return text


def clean_source(text: str) -> tuple[str, dict]:
    """清洗源文本。

    Returns:
        (清洗后文本, 清洗统计)
    """
    stats = {
        "chars_in": len(text),
        "banner_removed": 0,
        "meta_removed": 0,
        "token_stripped": 0,
    }
    out = text

    # 1. 头部元信息行 / ASR 失败横幅
    def _drop_meta(m):
        stats["meta_removed"] += 1
        return ""

    def _drop_banner(m):
        stats["banner_removed"] += 1
        return ""

    out = META_LINE.sub(_drop_meta, out)
    out = BANNER.sub(_drop_banner, out)

    # 2. 分享凭据
    # ⚠️ 2026-10-05 T1.14.2：原名 token_removed，实为 0/1 布尔，与同 dict 里
    # banner_removed / meta_removed（真计数，+= 1 累加几处）不是一个东西，
    # 读报告的人会当成「剥了几处」。strip_share_token 是纯 re.sub、
    # 不返回次数，所以这里只能是布尔 → 改名让它说实话。
    before = out
    out = strip_share_token(out)
    if out != before:
        stats["token_stripped"] = 1

    # 3. 归一化空行（连续 3+ 空行压成 2）
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = out.strip()

    stats["chars_out"] = len(out)
    return out, stats


def load_and_clean(path) -> tuple[str, dict]:
    """读源文件并清洗。path 需为 pathlib.Path。"""
    return clean_source(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    import sys
    from pathlib import Path

    if len(sys.argv) < 2:
        print("用法: python3 clean_source.py <transcript 路径>")
        raise SystemExit(1)
    cleaned, st = load_and_clean(Path(sys.argv[1]))
    print(f"清洗统计: {st}")
    print(f"--- 前 300 字符 ---\n{cleaned[:300]}")
