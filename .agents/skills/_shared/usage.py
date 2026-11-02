#!/usr/bin/env python3
"""LLM token 消耗计量（v3 管线三源共享）。

为什么需要（2026-11-02 登记）
--------------------------
`build_v3.py` 的 stats 原本只记 `source_chars` / `net_chars` / `route` /
纠错统计与成功率，**没有 token 数** —— 跑一次拿不到真实额度数字。
而 LLM 生成的具体消耗是决策依据（并发上限、是否重跑、预算控制），
任何「这篇生成花了多少」的估算都只能靠猜。

采集点（三个 LLM 调用入口，全部经 OpenAI 兼容接口，响应带 `usage`）
  - `_shared/build_v3.py::_call_with_template()`  撰写（短文 / 长文）
  - `_shared/correct_typos.py::_call()`            错字纠正
  - `_shared/chunk_scan.py::_call()`              分块扫描 / 合并去重

为什么用线程局部、而不是给函数加参数
------------------------------------
`_shared/` 是**双仓共享**代码（主项目 + opensource/rss-grab），给三个模块的
函数签名加参数要两仓同步改 + push。`threading.local()` 天然按线程隔离：
批量生成走 `ThreadPoolExecutor`（`_shared/batch_notes.py`），一个任务一篇笔记、
一个线程一篇，互不串账；而一篇笔记内部的四类调用（纠错 → 扫描 → 合并 → 撰写）
都在同一线程，能汇成一份账。

⚠️ 边界：计量以**线程**为界。同一篇笔记若被拆到多个线程，账会被拆开
（当前流程不会 —— `generate_note_body` 全程同步）。

落盘
----
每次会话结束写一行 JSON 到 `<项目根>/.usage/llm_usage.jsonl`（追加、文件锁保护）。
该目录已在两仓 `.gitignore` 里 —— 这是**运行数据**，不是产物，不进版本库。
写盘失败**不抛异常**：额度统计是辅助信息，绝不能因此毁掉一次正常生成。
"""
from __future__ import annotations

import json
import pathlib
import threading
from datetime import datetime

_local = threading.local()

USAGE_DIRNAME = ".usage"
USAGE_FILENAME = "llm_usage.jsonl"


def _get(obj, key, default=None):
    """dict / 对象都取得到（OpenAI 兼容响应是 pydantic 对象）。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def extract_usage(response):
    """从 OpenAI 兼容响应里取 usage。

    返回 `None` = 该响应**没有** usage 字段（服务不返回，或被裁掉）；
    返回 dict = 有 usage 字段（值可能全是 0 —— 那是服务自己报的 0）。

    两者必须区分：前者是「拿不到数字」，后者是「确实是 0」。
    混为一谈会让「服务没返 usage」被误读成「这篇几乎没花额度」。
    """
    usage = _get(response, "usage")
    if usage is None:
        return None
    det = _get(usage, "completion_tokens_details")
    return {
        "prompt_tokens": int(_get(usage, "prompt_tokens") or 0),
        "completion_tokens": int(_get(usage, "completion_tokens") or 0),
        "total_tokens": int(_get(usage, "total_tokens") or 0),
        "reasoning_tokens": int(_get(det, "reasoning_tokens") or 0) if det is not None else 0,
    }


class Meter:
    """一次会话（通常 = 一篇笔记）的 token 账。"""

    def __init__(self, stem: str = ""):
        self.stem = stem
        self.calls = 0
        self.calls_without_usage = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.reasoning_tokens = 0

    def add(self, usage: dict | None):
        self.calls += 1
        if usage is None:
            self.calls_without_usage += 1
            return
        self.prompt_tokens += usage.get("prompt_tokens", 0)
        self.completion_tokens += usage.get("completion_tokens", 0)
        self.total_tokens += usage.get("total_tokens", 0)
        self.reasoning_tokens += usage.get("reasoning_tokens", 0)

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "calls_without_usage": self.calls_without_usage,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


# ── 会话（线程局部）────────────────────────────────────────────────────

def start_session(stem: str = "") -> Meter:
    """开一篇笔记的账。同一线程内重复调用会**覆盖**上一本账。"""
    meter = Meter(stem)
    _local.meter = meter
    return meter


def current() -> Meter | None:
    return getattr(_local, "meter", None)


def end_session(extra: dict | None = None) -> dict:
    """收账：返回统计 dict，并写一行 jsonl。

    **没有会话时返回空 dict**（不是在会话外调用 LLM 的正常情况）。
    写盘失败只打印提示，不抛异常 —— 额度统计不该毁掉一次正常生成。
    """
    meter = getattr(_local, "meter", None)
    _local.meter = None
    if meter is None:
        return {}
    out = meter.as_dict()
    out["stem"] = meter.stem
    if extra:
        out.update(extra)
    _append_jsonl(out)
    return out


def record(response) -> None:
    """把一次 LLM 响应的 usage 记进当前会话。没会话就静默忽略。

    静默忽略是刻意的：单独的纠错 / 分块扫描脚本（不经过 `generate_note_body`）
    不该因为「没有会话」而报错。
    """
    meter = current()
    if meter is None:
        return
    meter.add(extract_usage(response))


# ── 落盘 ─────────────────────────────────────────────────────────────

def usage_log_path() -> pathlib.Path:
    """`<项目根>/.usage/llm_usage.jsonl`。"""
    from paths import find_project_root
    return find_project_root() / USAGE_DIRNAME / USAGE_FILENAME


def _append_jsonl(row: dict) -> bool:
    """追加一行 JSON。返回是否写成功（失败不抛异常）。"""
    path = usage_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(row)
        payload["ts"] = datetime.now().isoformat(timespec="seconds")
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        import fcntl
        with path.open("a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(line)
                f.flush()
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"    ⚠️ token 用量写盘失败（不影响产物）：{type(e).__name__}: {str(e)[:120]}",
              flush=True)
        return False
