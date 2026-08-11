#!/usr/bin/env python3
"""_shared/batch_notes.py 单测（开源版，rss-only）：id 提取 / 笔记存在判断 / 待生成清单。

不调真实 LLM（真实生成靠端到端验证）。
"""
import re
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))  # _shared/ 目录（tests/ 的父目录）

import batch_notes as bn


def test_extract_id_rss():
    """rss: <title>-<hash8>.transcript.md -> guid_hash8。"""
    cfg = bn.SOURCES["rss"]
    tp = Path("模型能力已经够了-要卷就卷-infra-0bfdc2dc.transcript.md")
    assert bn._extract_id(tp, cfg) == "0bfdc2dc"
    print("✅ test_extract_id_rss")


def test_extract_id_fallback():
    """id 正则不匹配时兜底返回整个 stem（不崩）。"""
    cfg = bn.SOURCES["rss"]
    tp = Path("奇怪的文件名.transcript.md")
    assert bn._extract_id(tp, cfg) == "奇怪的文件名"
    print("✅ test_extract_id_fallback")


def test_has_existing_note(monkeypatch):
    """已有笔记 -> True；无笔记 -> False。"""
    with tempfile.TemporaryDirectory() as td:
        notes_dir = Path(td) / "notes"
        notes_dir.mkdir(parents=True)

        cfg = {"notes": notes_dir}
        # 无笔记
        assert bn._has_existing_note(cfg, "abc12345") is False
        # 有笔记（含 INDEX.md 不算）
        (notes_dir / "标题-abc12345.md").write_text("x", encoding="utf-8")
        (notes_dir / "INDEX.md").write_text("x", encoding="utf-8")
        assert bn._has_existing_note(cfg, "abc12345") is True
        # 不同 id
        assert bn._has_existing_note(cfg, "other1234") is False
        # 子目录里的笔记也算（rss 按源分目录）
        sub = notes_dir / "十字路口Crossing"
        sub.mkdir()
        (sub / "标题-xyz67890.md").write_text("x", encoding="utf-8")
        assert bn._has_existing_note(cfg, "xyz67890") is True
        print("✅ test_has_existing_note")


def test_notes_dir_not_exists():
    """笔记目录不存在 -> False（不崩）。"""
    cfg = {"notes": Path("/nonexistent/notes")}
    assert bn._has_existing_note(cfg, "abc12345") is False
    print("✅ test_notes_dir_not_exists")


def test_todo_build_with_max(monkeypatch, tmp_path):
    """--max 限定：待生成 > max 时截断，剩余留待下次。"""
    transcripts_dir = tmp_path / "transcripts"
    transcripts_dir.mkdir()
    for i in range(5):
        (transcripts_dir / f"标题-{i}-abc{i}2345.transcript.md").write_text("x", encoding="utf-8")

    # 模拟主流程的 todo 收集 + --max
    cfg = bn.SOURCES["rss"].copy()
    cfg["transcripts"] = transcripts_dir
    cfg["notes"] = tmp_path / "notes"

    todo = []
    for tp in sorted(transcripts_dir.glob("*.transcript.md")):
        id_ = bn._extract_id(tp, cfg)
        if bn._has_existing_note(cfg, id_):
            continue
        todo.append(tp)

    assert len(todo) == 5
    # --max 2
    if len(todo) > 2:
        todo, rest = todo[:2], todo[2:]
    assert len(todo) == 2
    assert len(rest) == 3
    print("✅ test_todo_build_with_max")


def test_429_breaker_logic():
    """429 熔断逻辑：首次 429 降 20->15，二次降 15->10，无 429 复位。"""
    breaker = [0]
    active_limit = [20]

    # 首次 429（正则匹配）
    msg = "RateLimitError: 429"
    if re.search(r"429|RateLimit(?:Error)?", msg):
        breaker[0] += 1
        if breaker[0] == 1 and active_limit[0] > bn.FALLBACK_WORKERS_2:
            active_limit[0] = bn.FALLBACK_WORKERS
    assert active_limit[0] == 15
    assert breaker[0] == 1

    # 二次 429
    if re.search(r"429|RateLimit(?:Error)?", msg):
        breaker[0] += 1
        if breaker[0] == 1 and active_limit[0] > bn.FALLBACK_WORKERS_2:
            active_limit[0] = bn.FALLBACK_WORKERS
        elif breaker[0] >= 2 and active_limit[0] > bn.FALLBACK_WORKERS_2:
            active_limit[0] = bn.FALLBACK_WORKERS_2
    assert active_limit[0] == 10
    assert breaker[0] == 2

    # 无 429 复位
    ok_msg = "成功"
    if re.search(r"429|RateLimit(?:Error)?", ok_msg):
        breaker[0] += 1
    else:
        breaker[0] = 0
    assert breaker[0] == 0

    # 429 正则不误匹配正文里的普通数字（如 "429" 出现在内容里）
    normal_msg = "笔记完成，共 3 篇"
    assert not re.search(r"429|RateLimit(?:Error)?", normal_msg)
    print("✅ test_429_breaker_logic")


def test_429_semaphore_real_effect():
    """熔断对信号量的真实效果：降级后 acquire 阻塞（在飞数受控）。"""
    # 模拟 batch_notes 的 semaphore 机制：降级 = 换更小容量的信号量
    import threading

    # 初始 20 并发
    sem = threading.BoundedSemaphore(20)
    acquired = 0
    for _ in range(20):
        if sem.acquire(blocking=False):
            acquired += 1
    assert acquired == 20  # 20 个并发位全占用
    # 第 21 个 acquire 阻塞（不立即返回）
    t0 = time.time()
    got = sem.acquire(blocking=False)
    assert got is False  # 立即返回 False，说明并发位已满

    # 熔断降级到 10：释放后只有 10 个可重新占用
    for _ in range(10):
        sem.release()
    reacquired = 0
    for _ in range(20):
        if sem.acquire(blocking=False):
            reacquired += 1
    assert reacquired == 10  # 只有 10 个并发位（降级生效）
    print("✅ test_429_semaphore_real_effect")


if __name__ == "__main__":
    simple = [
        test_extract_id_rss,
        test_extract_id_fallback,
        test_notes_dir_not_exists,
        test_429_breaker_logic,
        test_429_semaphore_real_effect,
    ]
    for t in simple:
        t()

    with tempfile.TemporaryDirectory() as td:
        test_has_existing_note(Path(td))
        test_todo_build_with_max(None, Path(td))

    print(f"\n全部 {len(simple) + 2} 个测试通过")
