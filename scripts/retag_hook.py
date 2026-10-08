#!/usr/bin/env python3
"""笔记落盘后自动补跑打标 —— **薄壳接线端**（三源共用逻辑的精简版）。

## 这是什么

正文生成管线（v3）只写正文与信头，不产出 `tags`；标签由 `retag.py`
在**落盘之后**单独跑一次补上。本模块就是那次调用的统一入口，
供生成脚本在写完笔记后调用。

## 为什么不 import retag.py

`retag.py` 在 import 期就会注入 sys.path 并加载环境配置（缺配置会直接退出），
会把「打标」变成正文生成的硬依赖。正文已经落盘，不该因为打标环境有问题
就让整条流程失败。故走 **subprocess**：隔离失败面。

## 失败语义

正文已落盘，打标失败只 `print` 一行提示，**不抛异常、不回滚**。
留下的痕迹是「tags 为空 + 转写状态正常」，由既有的体检流程
（`scripts/audit_frontmatter.py` 的 G1 告警）抓出来 —— 本模块不重复造，
只在失败时指向它。

## 单篇与批量

单篇生成 → 落盘后立刻调一次。
批量生成 → 传全部 id，**只起一个进程**；每篇起一次进程等于每篇一次
环境加载，篇数多了开销与限流风险都被放大。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# 本文件与 retag.py 同目录：<repo>/scripts/retag_hook.py -> <repo>
REPO_ROOT = Path(__file__).resolve().parent.parent


def find_retag_script() -> Path | None:
    """找本仓的 `scripts/retag.py`；找不到返回 None。

    只在本仓内找，不去别处兜底：本仓是自包含的，retag.py 与打标阈值
    （`_tag_limits.py`）都随仓发布，克隆下来即可用。
    找不到时调用方打一行提示并正常返回 —— 打标缺席不该让正文生成失败。
    """
    cand = REPO_ROOT / "scripts" / "retag.py"
    return cand if cand.is_file() else None


def _hint(script_missing: bool) -> str:
    """统一的失败提示。`script_missing` 决定是否附「手动跑」的完整命令示例。"""
    base = ("⚠️ 自动打标未完成（不影响正文产出）。"
            "该笔记的 tags 仍为空，下次体检会被 G1 告警抓出来；"
            "也可以现在手动补跑。")
    if not script_missing:
        return base
    return (base + "\n   找不到 retag.py，请手动执行，例如：\n"
            "     python3 scripts/retag.py --source rss "
            "--ids <note_id,note_id> --apply")


def auto_retag(source: str, ids: list[str], apply: bool = True) -> bool:
    """笔记落盘后调 retag.py 打标。返回是否成功（失败只提示，从不抛异常）。

    参数
    ----
    source : retag.py 的 `--source` 取值（本仓只有 rss）
    ids    : 本次要打标的 note_id 列表（guid 的 sha256 前 8 位）。
             单篇传 1 个；批量传全部，避免每篇起一次进程。
    apply  : 是否让 retag 真正写盘。默认 True —— 自动打标不写盘等于没做。
    """
    ids = [i for i in (ids or []) if i and str(i).strip()]
    if not ids:
        return True                      # 没有要打标的对象，不是失败
    script = find_retag_script()
    if script is None:
        print(_hint(True), flush=True)
        return False
    cmd = [sys.executable, str(script), "--source", source,
           "--ids", ",".join(ids)]
    if apply:
        cmd.append("--apply")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           # 单篇打标含一次 LLM 往返，给足超时；
                           # 超时也只是这次打标没成，不影响正文。
                           timeout=1800)
    except Exception as e:  # noqa: BLE001
        print(_hint(False) + f"\n   原因：{type(e).__name__}: {e}", flush=True)
        return False
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        print(_hint(False) + (f"\n   retag 退出码 {r.returncode}："
                              + " / ".join(t.strip() for t in tail)),
              flush=True)
        return False
    print(f"   自动打标完成：{len(ids)} 篇（{script}）", flush=True)
    return True