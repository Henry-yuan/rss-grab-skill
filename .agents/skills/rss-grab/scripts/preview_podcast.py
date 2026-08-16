#!/usr/bin/env python3
"""AI 预读播客简介：把勾选文件里每期的 description 用 M3 摘要替换。

设计（方案 v6，用户确认 2026-08-10，从批量 JSON 改为单期纯文本）：
  - 每期一次调用（不批量、不要 JSON——M3 输出纯文本远比 JSON 稳定）
  - 并发 ≤8（信号量），发射间隔 ≥5s，流水线补位
  - 输出固定格式纯文本：一句话概括 / 内容概览 / 值得关注（⏱时间戳）
  - 单期失败重试（最多 3 次尝试），坏 1 期不影响其他
  - 全部完成后一次性替换 description 字段（避免并发写文件）
  - 每 CHECKPOINT_EVERY 期 check点落盘（防中断丢失）

用法（被 fetch_rss_feed --pick-gen 调用）：
  from preview_podcast import summarize_items
  summarize_items(feed, items, out_path)

或独立运行：
  python3 preview_podcast.py <rss_url> <out_path>
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path

from openai import OpenAI

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent.parent / "_shared"))
from env import load_env
import parse_rss

MAX_WORKERS = 20         # 在途并发上限（2026-08-11 从 8 提到 20，JSON 实验验证 20 并发无 429）
MAX_WORKERS_FALLBACK = 15  # 429 熔断后降级并发（2026-08-11 P3 新增）
LAUNCH_INTERVAL = 2      # 发射间隔（秒），错开峰值（2026-08-11 从 5 改 2）
MAX_ATTEMPTS = 3         # 每期最多尝试（1 次 + 2 次重试）
CHECKPOINT_EVERY = 25    # 每 N 期 check点落盘
SUMMARY_MAX_TOKENS = 1500

PROMPT = """你是播客简介摘要助手。为下面这一期播客生成结构化中文摘要，**直接输出纯文本**（不要 JSON，不要代码块，不要思考过程）：

## 播客标题
{title}

## 完整简介
{description}

## 输出格式（严格按这个结构，直接写内容）
一句话概括：<30-60字>
内容概览：<3-6 条要点，用分号分隔>
值得关注：<2-5 条，有时间戳的标注 ⏱HH:MM，无则写"无特别标注">"""


def strip_thinking_blocks(text: str) -> str:
    """去掉 M3 输出的 <think>...</think> 思考块。

    防未闭合：正则非贪婪匹配不到 </think> 时会吞掉全部，用 <think> 分割兜底。
    """
    cleaned = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)
    if not cleaned.strip() or cleaned.strip().startswith("<think"):
        # 未闭合思考块：取 <think> 之后、且去掉开头残留
        idx = text.find("</think>")
        if idx >= 0:
            cleaned = text[idx + len("</think>"):]
        else:
            # 完全没有闭合标签：找第一个换行后的内容（思考块一般到输出前结束）
            cleaned = text.split("\n", 1)[-1]
    return cleaned.strip()


def summarize_one(client, model, item: dict, label: str,
                  breaker: list | None = None) -> str:
    """单期摘要：prompt -> API（最多 MAX_ATTEMPTS 次）-> 返回纯文本摘要。

    breaker: 熔断信号（list[bool]），429 限流时置 True，调用方据此降并发。
    失败（空输出/全思考块）重试，3 次后返回 ""（调用方标 ⚠️）。
    """
    prompt = PROMPT.format(
        title=item.get("title", ""),
        description=item.get("description", ""),
    )
    last_err = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "你是播客简介摘要助手，输出简洁的中文纯文本。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_tokens=SUMMARY_MAX_TOKENS,
            )
            content = resp.choices[0].message.content
            summary = strip_thinking_blocks(content)
            # 校验：摘要应包含关键结构（一句话概括/内容概览）
            if len(summary) < 20 or "一句话概括" not in summary:
                last_err = f"输出过短或格式不符（{len(summary)} 字符，attempt {attempt}）"
                if attempt < MAX_ATTEMPTS:
                    time.sleep(2 ** attempt)
                continue
            return summary
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}（attempt {attempt}）"
            # 429 限流：触发熔断信号（调用方降并发），并延长退避
            if type(e).__name__ == "RateLimitError" and breaker is not None:
                breaker[0] = True
                print(f"  ⚠️ [{label}] 429 限流，触发熔断降并发")
            if attempt < MAX_ATTEMPTS:
                time.sleep(2 ** attempt)
    print(f"  ❌ [{label}] 3 次尝试后仍失败: {last_err}")
    return ""


def _item_key(item: dict, idx: int = 0) -> str:
    """checkpoint 用的期数唯一 key：优先 guid，无 guid 用 idx 兜底。"""
    guid = item.get("guid")
    if guid:
        return guid
    return f"idx:{idx}"


def replace_descriptions(items: list[dict], summaries: dict) -> None:
    """用 AI 摘要替换每期 description（key = guid 或 idx:N）；缺失的标 ⚠️。"""
    for i, it in enumerate(items, 1):
        key = _item_key(it, i - 1)
        s = summaries.get(key)
        if s:
            it["description"] = s
        else:
            it["description"] = "⚠️ 摘要生成失败（3 次尝试后仍失败）"


def _get_client():
    """创建 OpenAI client（供外部模块复用，如 --retry-summary 单期重跑）。"""
    load_env(SCRIPT_DIR)
    client = OpenAI(
        api_key=os.environ["LLM_API_KEY"],
        base_url=os.environ["LLM_BASE_URL"],
        timeout=600.0,
    )
    model = os.environ["LLM_MODEL"]
    return client, model


def summarize_items(feed: dict, items: list[dict], checkpoint_path: Path | None = None,
                    resume: bool = False, on_item_done=None) -> None:
    """主流程：每期一次 M3 调用（并发 + 发射间隔 + 流水线补位）。

    feed: parse_rss 的 feed dict（items 从它拿）
    items: feed["items"]（会被原地替换 description）
    checkpoint_path: 每 CHECKPOINT_EVERY 期落盘位置（None = 不落盘）
    resume: True 时读已有 checkpoint，跳过已完成期（按 guid / idx key）
    on_item_done: 每期摘要完成后的回调（item, summary），用于增量写状态文件
    """
    load_env(SCRIPT_DIR)
    client = OpenAI(
        api_key=os.environ["LLM_API_KEY"],
        base_url=os.environ["LLM_BASE_URL"],
        timeout=600.0,
    )
    model = os.environ["LLM_MODEL"]

    total = len(items)
    summaries: dict[str, str] = {}   # key -> summary（key = guid 或 idx:N）
    lock = threading.Lock()
    breaker = [False]                # 429 熔断信号（summarize_one 里置位）

    # resume：读已有 checkpoint，跳过已完成期
    start_idx = 0
    if resume and checkpoint_path and checkpoint_path.exists():
        try:
            with open(checkpoint_path, "r", encoding="utf-8") as f:
                summaries = json.load(f).get("summaries", {})
            done_keys = set(summaries.keys())
            while start_idx < total:
                key = _item_key(items[start_idx], start_idx)
                if key in done_keys:
                    start_idx += 1
                else:
                    break
            skipped = sum(1 for i in range(total) if _item_key(items[i], i) in done_keys)
            if skipped:
                print(f"  ♻️  resume：跳过已完成 {skipped} 期（从第 {start_idx + 1} 期继续）")
        except (json.JSONDecodeError, OSError) as e:
            print(f"  ⚠️  checkpoint 读取失败({e})，从头跑")

    # 有效并发（熔断时降到 MAX_WORKERS_FALLBACK）
    effective_workers = [MAX_WORKERS]

    def check_in(done: int) -> None:
        """每 CHECKPOINT_EVERY 期完成时落盘中间结果。"""
        if not checkpoint_path:
            return
        if done % CHECKPOINT_EVERY == 0:
            with lock:
                checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
                checkpoint_path.write_text(
                    json.dumps({"summaries": summaries}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            print(f"  💾 checkpoint: {done}/{total} 期完成，已落盘 {checkpoint_path}")

    done = 0
    last_launch = 0.0
    lock_launch = threading.Lock()

    def launch(item_idx: int) -> None:
        nonlocal last_launch
        # 熔断后降并发：把当前在途控制在降级值内
        workers = effective_workers[0]
        while len([f for f in futures if not f.done()]) >= workers:
            time.sleep(0.5)
        with lock_launch:
            now = time.time()
            wait = LAUNCH_INTERVAL - (now - last_launch)
            if wait > 0:
                time.sleep(wait)
            fut = pool.submit(
                summarize_one, client, model, items[item_idx],
                f"期{item_idx + 1}", breaker,
            )
            futures[fut] = item_idx
            last_launch = time.time()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures: dict = {}
        next_idx = start_idx

        # 先发前 N 期（错开间隔）
        for i in range(start_idx, min(start_idx + MAX_WORKERS, total)):
            launch(i)
            next_idx += 1

        # 流水线：完成一期 -> 收结果 -> 补发下一期
        while futures:
            completed_futs, _ = wait(list(futures), return_when=FIRST_COMPLETED)
            for fut in completed_futs:
                item_idx = futures.pop(fut)
                summary = fut.result()
                key = _item_key(items[item_idx], item_idx)
                with lock:
                    if summary:
                        summaries[key] = summary
                done += 1
                status = "✅" if summary else "❌"
                print(f"  {status} 期 {item_idx + 1}/{total} 完成")
                check_in(done)
                # 每期完成回调（增量写状态文件）
                if on_item_done is not None:
                    try:
                        on_item_done(items[item_idx], summary)
                    except Exception as e:
                        print(f"  ⚠️  on_item_done 回调失败: {e}")
                if next_idx < total:
                    launch(next_idx)
                    next_idx += 1

            # 熔断检查：429 触发后降并发
            if breaker[0] and effective_workers[0] > MAX_WORKERS_FALLBACK:
                effective_workers[0] = MAX_WORKERS_FALLBACK
                print(f"  🔻 429 熔断：并发降级 {MAX_WORKERS} -> {MAX_WORKERS_FALLBACK}")

    # 全部完成 -> 替换 description（按 key 回填）
    replace_descriptions(items, summaries)
    n_ok = sum(1 for it in items if "⚠️ 摘要生成失败" not in it.get("description", ""))
    print(f"\n=== AI 摘要完成：✅ {n_ok}/{total} 期 ===")


def main():
    # 原 main 的独立运行入口调用 fetch_rss_feed.render_pick_file /
    # pick_file_path——这两个函数属于开源时裁剪掉的"选择下载"模式，仓库里
    # 不存在，独立运行必然 AttributeError。本脚本是内部模块，摘要由
    # fetch_rss_feed --fetch-updates 编排调用，不再支持独立运行。
    print("本脚本为内部模块（AI 摘要由 fetch_rss_feed.py 编排调用），不支持独立运行。")
    print("拉取增量 + AI 摘要请使用：python3 fetch_rss_feed.py --fetch-updates")
    sys.exit(2)


if __name__ == "__main__":
    main()
