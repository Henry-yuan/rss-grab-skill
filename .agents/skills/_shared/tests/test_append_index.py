#!/usr/bin/env python3
"""`_shared/append_index.py` 的存在性与签名回归（V1.1 T2.3）。

## 判据

1. **4 个函数都能 import** —— 不是「文件里有 def」，而是 `from append_index import ...`
   真能跑通。函数「存在」不等于「可导入」：模块路径没进 sys.path、模块名写错，
   在薄壳里都只表现为一行 `⚠️ 索引追加失败`，笔记照落、退出码 0（V1.0 实测）。
2. **`register_rss_index` 的签名未变** —— 薄壳按位置传 `(info, note_path)`，
   另外两项有默认值（测试要传临时 index_path）。
3. **反向断言** —— 确定不存在的名字必须 import 失败，否则第 1 条是恒真命题。

## 为什么签名要钉死

v2 脚本归档后，`register_rss_index` 的调用方只剩薄壳。签名一旦悄悄改动
（加必填参数 / 改参数顺序），薄壳会在 `try` 里被吞掉，表现仍是「一条警告」。
"""
from __future__ import annotations

import inspect
import pathlib
import sys

import pytest

SHARED = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SHARED))

import append_index  # noqa: E402

FUNCS = ("get_capture_mtime", "sanitize_index_cell",
         "format_duration_mm_ss", "register_rss_index")
FAKE_FUNC = "register_definitely_not_exists_xyz"


@pytest.mark.parametrize("name", FUNCS)
def test_四个索引函数都能导入(name):
    assert hasattr(append_index, name), f"append_index 里没有 {name}"
    assert callable(getattr(append_index, name))


def test_从模块直接导入也能跑通():
    """按薄壳的写法原样执行一遍，确保 sys.path 前提成立。"""
    ns: dict = {}
    exec("from append_index import ("
         "get_capture_mtime, sanitize_index_cell, "
         "format_duration_mm_ss, register_rss_index)", ns)   # noqa: S102
    for name in FUNCS:
        assert callable(ns[name])


def test_register_rss_index_签名未变():
    """参数名与默认值都是薄壳/测试的隐含契约，不能悄悄改。"""
    params = list(inspect.signature(append_index.register_rss_index).parameters)
    assert params == ["info", "note_path", "source_name", "index_path"], \
        f"register_rss_index 参数变成了 {params}"
    sig = inspect.signature(append_index.register_rss_index)
    # 前两个是薄壳按位置传的，必须能接；后两个有默认值，测试要传临时 index_path
    assert list(sig.parameters)[:2] == ["info", "note_path"]
    assert sig.parameters["source_name"].default == ""
    assert sig.parameters["index_path"].default is None


def test_不存在的函数名导入必须失败():
    """反向断言：证明上面几条不是「什么都通过」。"""
    assert not hasattr(append_index, FAKE_FUNC), \
        f"不存在的 {FAKE_FUNC} 竟然存在 —— 存在性断言已退化为恒真命题"
