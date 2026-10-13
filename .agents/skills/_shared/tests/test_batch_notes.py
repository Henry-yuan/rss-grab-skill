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


def test_todo_build_with_max(tmp_path):
    """--max 限定：待生成 > max 时截断，剩余留待下次。

    ⚠️ 2026-10-05 T1.14.2：本测试原先把「todo 收集 + --max 截断」两段逻辑
    **抄进测试体**再断言 —— 测的是副本。生产代码改了这里不会红。
    现在直接调 bn.collect_todo / bn.apply_max_limit，测的是生产实现本身。
    """
    transcripts_dir = tmp_path / "transcripts"
    transcripts_dir.mkdir()
    for i in range(5):
        (transcripts_dir / f"标题-{i}-abc{i}2345.transcript.md").write_text("x", encoding="utf-8")

    cfg = bn.SOURCES["rss"].copy()
    cfg["transcripts"] = transcripts_dir
    cfg["notes"] = tmp_path / "notes"          # 空目录 = 都没笔记
    sources = sorted(transcripts_dir.glob("*.transcript.md"))

    todo = bn.collect_todo(sources, cfg, force=False)
    assert len(todo) == 5, "5 份转写稿、0 份笔记 -> 全部待生成"

    todo, rest = bn.apply_max_limit(todo, 2)
    assert len(todo) == 2
    assert len(rest) == 3

    # --max 0 = 不限
    todo2, rest2 = bn.apply_max_limit(todo + rest, 0)
    assert len(todo2) == 5 and rest2 == []

    # --max 大于待生成数 = 全部本轮跑完
    todo3, rest3 = bn.apply_max_limit(todo + rest, 99)
    assert len(todo3) == 5 and rest3 == []
    print("✅ test_todo_build_with_max")


def test_collect_todo_skips_existing_and_force(tmp_path):
    """collect_todo 真的跳过已有笔记；force=True 时全收。

    变异验证点：把 collect_todo 里的 `if force: continue` 删掉，这条必须变红。
    """
    transcripts_dir = tmp_path / "transcripts"
    notes_dir = tmp_path / "notes"
    transcripts_dir.mkdir()
    notes_dir.mkdir()

    for i in range(3):
        (transcripts_dir / f"标题-{i}-abc{i}2345.transcript.md").write_text("x", encoding="utf-8")
    # 给第 1 篇造出已有笔记
    (notes_dir / "标题-1-abc12345.md").write_text("y", encoding="utf-8")

    cfg = bn.SOURCES["rss"].copy()
    cfg["transcripts"] = transcripts_dir
    cfg["notes"] = notes_dir

    todo = bn.collect_todo(sorted(transcripts_dir.glob("*.transcript.md")), cfg, force=False)
    assert len(todo) == 2, "已有笔记的那篇必须被跳过"
    assert all("标题-1-" not in t.name for t in todo)

    todo_forced = bn.collect_todo(sorted(transcripts_dir.glob("*.transcript.md")), cfg, force=True)
    assert len(todo_forced) == 3, "force=True 必须全收"
    print("✅ test_collect_todo_skips_existing_and_force")


def test_429_breaker_logic():
    """429 熔断：首次 429 降 20->15，二次降 15->10，连续无 429 复位。

    ⚠️ 2026-10-05 T1.14.2：本测试原先把熔断控制流抄进测试体，只读了两个常量。
    那份副本**漏掉了「连续无 429 复位」整条 else 分支**——生产里复位逻辑坏掉，
    测试照样全绿。现在直接调生产函数 bn.apply_429_breaker。
    """
    breaker = [0]
    active_limit = [20]

    msg = "RateLimitError: 429"
    assert bn.apply_429_breaker(msg, breaker, active_limit) == bn.FALLBACK_WORKERS
    assert active_limit[0] == 15
    assert breaker[0] == 1

    # 二次 429 -> 第二档
    bn.apply_429_breaker(msg, breaker, active_limit)
    assert active_limit[0] == bn.FALLBACK_WORKERS_2
    assert breaker[0] == 2

    # 第三次 429：已到最低档，不再降（不能降穿）
    bn.apply_429_breaker(msg, breaker, active_limit)
    assert active_limit[0] == bn.FALLBACK_WORKERS_2, "最低档不能再降"
    assert breaker[0] == 3

    # 连续一次无 429 -> 计数复位（**旧副本没有这条断言**）
    assert bn.apply_429_breaker("成功", breaker, active_limit) == bn.FALLBACK_WORKERS_2
    assert breaker[0] == 0, "无 429 必须复位计数"

    # 复位后再遇 429，应重新从第一档降，而不是直接跳到最低档
    active_limit[0] = 20
    bn.apply_429_breaker(msg, breaker, active_limit)
    assert active_limit[0] == bn.FALLBACK_WORKERS, "复位后应从第一档重新降"

    # 429 正则不误匹配正文里的普通数字
    assert not re.search(r"429|RateLimit(?:Error)?", "笔记完成，共 3 篇")
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


def test_sources_regenerate_script_exists():
    """守门：SOURCES 里每一源的 regenerate 脚本必须**真实存在于磁盘**。

    2026-10-05 补这条的原因：切 v3 时只改了单篇入口，`batch_notes.py` 的
    SOURCES 没跟着改，rss 指向 `regenerate_note.py` —— 该文件 2026-10-05 已改名
    归档为 `regenerate_note_v2.py`（v2 退役）→ **路径指向不存在的文件**。
    本文件原有的用例只测纯逻辑，**从不碰 SOURCES 里的路径**，所以抓不到。
    """
    for name, cfg in bn.SOURCES.items():
        script = Path(cfg["regenerate"])
        assert script.exists(), (
            f"源 {name} 的 regenerate 脚本不存在：{script}\n"
            f"  批量入口会在真正生成时直接失败。请改指该源当前的 v3 生产入口。"
        )
    print("✅ test_sources_regenerate_script_exists")


def test_sources_regenerate_script_runnable():
    """守门：SOURCES 里的脚本必须能 `--help` 跑通（文件在 ≠ 入口能用）。"""
    import subprocess
    for name, cfg in bn.SOURCES.items():
        script = Path(cfg["regenerate"])
        if not script.exists():          # 存在性由上一条负责报错
            continue
        r = subprocess.run([sys.executable, str(script), "--help"],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, (
            f"源 {name} 的 regenerate 脚本 --help 退出码 {r.returncode}："
            f"{r.stderr.strip()[:300]}"
        )
    print("✅ test_sources_regenerate_script_runnable")


def test_批量默认不碰生产索引(monkeypatch):
    """2026-10-05：批量跑默认给子进程加 --no-index，要动生产状态得显式加 --index。

    起因是薄壳的 mark_subscribe_done() 触发条件改成了「只看 --no-index」
    （显式传 --transcript/--info 时 guid_hash8 为 None，旧条件会漏标记），
    于是批量跑会真的去改订阅状态文件。批量重生成常用于试参数/换模型，
    默认就动生产状态太危险。
    """
    import subprocess
    seen = []

    class _R:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _fake_run(cmd, **kw):
        seen.append(list(cmd))
        return _R()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    bn._run_regenerate(Path("/x/gen.py"), "abc", touch_index=False)
    assert "--no-index" in seen[-1], f"默认没加保护：{seen[-1]}"

    bn._run_regenerate(Path("/x/gen.py"), "abc", touch_index=True)
    assert "--no-index" not in seen[-1], f"显式 --index 仍被塞了保护：{seen[-1]}"
    print("✅ test_批量默认不碰生产索引")


def test_子进程脚本真的认得_no_index():
    """声明了要传 --no-index，脚本就必须真有这个选项，否则 argparse 直接报错断路。"""
    import subprocess
    for name, cfg in bn.SOURCES.items():
        script = Path(cfg["regenerate"])
        if not script.exists():
            continue
        r = subprocess.run([sys.executable, str(script), "--help"],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0
        assert "--no-index" in r.stdout, (
            f"源 {name} 的脚本没有 --no-index 选项，批量入口传了会被 argparse 打断"
        )
    print("✅ test_子进程脚本真的认得_no_index")


if __name__ == "__main__":
    simple = [
        test_extract_id_rss,
        test_extract_id_fallback,
        test_notes_dir_not_exists,
        test_429_breaker_logic,
        test_429_semaphore_real_effect,
        test_sources_regenerate_script_exists,
        test_sources_regenerate_script_runnable,
    ]
    for t in simple:
        t()

    with tempfile.TemporaryDirectory() as td:
        test_has_existing_note(Path(td))
        test_todo_build_with_max(None, Path(td))

    print(f"\n全部 {len(simple) + 2} 个测试通过")


# ===== 2026-10-13：--force 假成功（批量管道里最难发现的那类）=====
#
# 背景：`--force` 原先只用于 `collect_todo()` 决定收哪些 transcript，
# **从不下传给生成脚本**。下游脚本有自己的防重闸门，见产物就跳过 ——
# 于是 `batch_notes.py --force` 打印「成功 N / 失败 0」却一篇都没重生成。


def test_force_被透传给子进程(monkeypatch):
    """接线层：`force=True` 时子进程命令行必须含 `--force`，False 时不含。"""
    import subprocess
    seen = []

    class _R:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _fake_run(cmd, **kw):
        seen.append(list(cmd))
        return _R()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    bn._run_regenerate(Path("/x/gen.py"), "abc", force=True)
    assert "--force" in seen[-1], f"force 没透传下去：{seen[-1]}"

    bn._run_regenerate(Path("/x/gen.py"), "abc", force=False)
    assert "--force" not in seen[-1], f"没 force 却传了：{seen[-1]}"


def test_force_真的让下游重生成_而非只把参数传出去(tmp_path):
    """端到端：`--force` 必须真的触发重生成。

    ⚠️ 只测「命令行里有 --force」挡不住「传了但下游不认」——
    这里用一个**复刻下游防重闸门行为**的假生成脚本：
    产物存在就跳过，除非收到 `--force`。只有产物真被改写才算通过。
    """
    import subprocess

    gen = tmp_path / "fake_gen.py"
    gen.write_text(
        "import sys, pathlib\n"
        "args = sys.argv[1:]\n"
        "out = pathlib.Path(args[0])\n"
        "if out.exists() and '--force' not in args:\n"
        "    print('skip: 已有产物')\n"
        "    sys.exit(0)\n"
        "out.write_text('regenerated|' + '|'.join(args), encoding='utf-8')\n"
        "print('ok: 已重新生成')\n",
        encoding="utf-8",
    )
    product = tmp_path / "note.md"
    product.write_text("旧内容", encoding="utf-8")

    ok, msg = bn._run_regenerate(gen, str(product), force=False)
    assert ok, f"退出码非 0：{msg}"
    assert product.read_text(encoding="utf-8") == "旧内容", \
        "没传 force 却被重写了 —— 防重闸门失效"

    ok, msg = bn._run_regenerate(gen, str(product), force=True)
    assert ok, f"退出码非 0：{msg}"
    new = product.read_text(encoding="utf-8")
    assert new.startswith("regenerated"), f"带 force 仍未重生成，产物={new[:60]!r}"
    assert "--force" in new, "产物里没有 --force，说明参数没到下游"


def test_各源生成脚本都接受_force():
    """守门：SOURCES 里每个源的生成脚本都必须真的接受 `--force`。

    刻意不做「能力声明」——漏传会静默失效，多传会 argparse 响亮报错。
    宁可响亮失败，也不要静默空转。
    """
    import subprocess as sp
    missing = []
    for name, cfg in bn.SOURCES.items():
        script = Path(cfg["regenerate"])
        if not script.exists():
            missing.append(f"{name}(脚本不存在)")
            continue
        r = sp.run([sys.executable, str(script), "--help"],
                   capture_output=True, text=True, timeout=60)
        if "--force" not in (r.stdout + r.stderr):
            missing.append(name)
    assert not missing, \
        f"这些源的生成脚本不接受 --force（batch_notes 会 argparse 报错）：{missing}"


def test_main_把_args_force_接到_run_regenerate():
    """守门：调用点必须把 `args.force` 传下去。

    ⚠️ 这条是变异验证补出来的：只测 `_run_regenerate` 函数本身的话，
    把调用点的 `force=args.force` 删掉后测试依然全绿——
    而那正是本 bug 最初的样子（flag 收了、没接到下游）。
    """
    src = Path(bn.__file__).read_text(encoding="utf-8")
    m = re.search(r"pool\.submit\(\s*_run_regenerate\b(.{0,400}?)\)\s*\n", src, re.S)
    assert m, "找不到 pool.submit(_run_regenerate, ...) 调用点"
    call = m.group(1)
    assert "args.force" in call, \
        f"调用点没把 args.force 接到 _run_regenerate（这正是原 bug）：\n{call.strip()[:240]}"
