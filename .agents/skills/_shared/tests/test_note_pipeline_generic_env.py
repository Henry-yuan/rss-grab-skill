#!/usr/bin/env python3
"""笔记生成管线的环境变量名：**通用名优先、专有名兜底**（2026-10-14）。

## 这是「代码承诺与实际不符」的缺陷，不是设计偏好

README 承诺「不绑定任何特定服务」，`.env.example` 与本仓 `env.py` 也一律用
通用名 `LLM_*`。但笔记管线的三个文件此前**只认专有名** `MINIMAX_*`：
用户照文档配好 OpenAI 兼容服务后，**笔记这一步必然失败** ——
拿到的是写死的默认模型与服务地址，而不是自己配的那套。

同一项目里的摘要脚本 `rss-grab/scripts/preview_podcast.py` 用的却是
通用名 `LLM_EFFORT`。**一项目内两套命名并存 = bug 的直接证据**：
若「必须用专有名」是有意设计，摘要脚本不会用通用名。

## 修法（三个文件同样处理）

`build_v3.py` / `chunk_scan.py` / `correct_typos.py`：

| 变量 | 优先级 | 缺省 |
|---|---|---|
| `MODEL` | `LLM_MODEL` → `MINIMAX_MODEL` → 默认 | `MiniMax-M3.1-Flash-Preview` |
| `EFFORT` | `LLM_EFFORT` → `MINIMAX_EFFORT` → 默认 | `max`（chunk_scan：`xhigh`/`medium`） |
| `BASE_URL` / `MAX_COMPLETION` / `TIMEOUT` | 同上 | 同上 |

**显式设空 = 关闭该参数**：`LLM_EFFORT=`（空串）时调用处**不发**
`reasoning_effort`。因此取值**必须**用嵌套 `os.environ.get`，
不能用 `or` —— `or` 把「显式设空」和「未设置」混为一谈，
「显式设空」这个语义根本表达不出来。

## 判据

配置都是 **import 时求值的模块级常量**，所以每条用例都 `importlib.reload`
重新导入，直接断言模块属性 —— 测的是真实取值，不是复制一遍的逻辑。
调用处则用假 client 抓 `create()` 实际收到的 kwargs，断言参数**在不在**。
"""
from __future__ import annotations

import importlib
import os
import pathlib
import sys

import pytest

_SHARED = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SHARED))

ALL_KEYS = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_EFFORT",
            "LLM_MAX_COMPLETION", "LLM_TIMEOUT",
            "MINIMAX_API_KEY", "MINIMAX_BASE_URL", "MINIMAX_MODEL",
            "MINIMAX_EFFORT", "MINIMAX_MAX_COMPLETION", "MINIMAX_TIMEOUT")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """每个用例都从「环境里什么都没有」开始，避免用例之间互相污染。"""
    for k in ALL_KEYS:
        monkeypatch.delenv(k, raising=False)
    yield


def _reload(name: str):
    """重新导入模块，让模块级常量按当前环境变量重新求值。"""
    return importlib.reload(importlib.import_module(name))


class _FakeCompletions:
    """记录 create() 实际收到的 kwargs，供断言参数在不在。"""

    def __init__(self) -> None:
        self.kwargs: dict | None = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        msg = type("M", (), {"content": "ok"})()
        return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()


class _FakeClient:
    def __init__(self) -> None:
        self.completions = _FakeCompletions()
        self.chat = type("Chat", (), {"completions": self.completions})()


# ── 1. LLM_MODEL 生效（本次 bug 的正面判据）───────────────────────

def test_通用名MODEL生效():
    """只配 LLM_MODEL 时必须用它 —— 修复前这里拿到的是写死的默认模型。"""
    os.environ["LLM_MODEL"] = "some-generic-model"
    for mod in ("build_v3", "chunk_scan"):
        assert _reload(mod).MODEL == "some-generic-model", mod
    assert _reload("correct_typos")._model() == "some-generic-model"


def test_通用名BASE_URL生效():
    os.environ["LLM_BASE_URL"] = "https://generic.example/v1"
    for mod in ("build_v3", "chunk_scan"):
        assert _reload(mod).BASE_URL == "https://generic.example/v1", mod
    assert _reload("correct_typos")._base_url() == "https://generic.example/v1"


def test_通用名MAX_COMPLETION生效():
    os.environ["LLM_MAX_COMPLETION"] = "12345"
    for mod in ("build_v3", "chunk_scan", "correct_typos"):
        assert _reload(mod).MAX_COMPLETION == 12345, mod


def test_通用名TIMEOUT生效():
    os.environ["LLM_TIMEOUT"] = "42"
    assert _reload("build_v3").TIMEOUT == 42


# ── 2. 未设 LLM_* 时回落 MINIMAX_*（兼容性底线）──────────────────

def test_未设通用名时回落专有名():
    """专用名用户（现有三源 .env）行为**必须完全不变**。"""
    os.environ["MINIMAX_MODEL"] = "vendor-model"
    os.environ["MINIMAX_BASE_URL"] = "https://vendor.example/v1"
    os.environ["MINIMAX_MAX_COMPLETION"] = "999"
    os.environ["MINIMAX_TIMEOUT"] = "77"
    os.environ["MINIMAX_EFFORT"] = "high"
    for mod in ("build_v3", "chunk_scan"):
        m = _reload(mod)
        assert m.MODEL == "vendor-model", mod
        assert m.BASE_URL == "https://vendor.example/v1", mod
        assert m.MAX_COMPLETION == 999, mod
    bv = _reload("build_v3")
    assert bv.TIMEOUT == 77
    assert bv.EFFORT == "high"
    cs = _reload("chunk_scan")
    assert cs.SCAN_EFFORT == "high" and cs.MERGE_EFFORT == "high"
    ct = _reload("correct_typos")
    assert ct.MAX_COMPLETION == 999
    assert ct._model() == "vendor-model"
    assert ct._base_url() == "https://vendor.example/v1"


def test_两套都设时通用名优先():
    """同时配了两套 → 通用名胜出（与 env.py 的 env_get 同序）。"""
    os.environ["LLM_MODEL"] = "generic-model"
    os.environ["MINIMAX_MODEL"] = "vendor-model"
    assert _reload("build_v3").MODEL == "generic-model"
    assert _reload("chunk_scan").MODEL == "generic-model"
    assert _reload("correct_typos")._model() == "generic-model"


# ── 3. 一个都没设 → 用默认值 ────────────────────────────────────

def test_都没设时用默认值():
    """三套都没配时行为与改动前一致 —— 不能因为统一命名就改了缺省行为。"""
    assert _reload("build_v3").MODEL == "MiniMax-M3.1-Flash-Preview"
    assert _reload("build_v3").EFFORT == "max"
    assert _reload("build_v3").MAX_COMPLETION == 100000
    assert _reload("build_v3").TIMEOUT == 1800
    cs = _reload("chunk_scan")
    assert cs.SCAN_EFFORT == "xhigh"       # 逐块抽取的实测档位
    assert cs.MERGE_EFFORT == "medium"     # 合并的占位档位
    assert _reload("chunk_scan").MODEL == "MiniMax-M3.1-Flash-Preview"


# ── 4. 显式设空 = 关闭该参数（本轮的关键语义）─────────────────────

def test_通用名EFFORT显式设空时取到空串():
    """`LLM_EFFORT=` 必须是**空串**而不是「未设置」。

    这里用 `or` 实现就会失败：空串是 falsy，会被吃掉继续回落。
    """
    os.environ["MINIMAX_EFFORT"] = "high"
    os.environ["LLM_EFFORT"] = ""
    assert _reload("build_v3").EFFORT == ""
    cs = _reload("chunk_scan")
    assert cs.SCAN_EFFORT == "" and cs.MERGE_EFFORT == ""


def test_build_v3显式设空时不发reasoning_effort():
    """`LLM_EFFORT=` → 调用时**不生成** reasoning_effort 这个参数。

    条件是「参数在不在」，不是「值是不是空」—— 后者的话参数仍会被发出去，
    而部分 OpenAI 兼容服务不接受该参数，直接 400。
    """
    os.environ["LLM_EFFORT"] = ""
    bv = _reload("build_v3")
    client = _FakeClient()
    bv._call_with_template(client, "src={src}", "正文", {}, heartbeat=False)
    assert client.completions.kwargs is not None
    assert "reasoning_effort" not in client.completions.kwargs, \
        f"显式设空仍发了 reasoning_effort：{sorted(client.completions.kwargs)}"
    assert client.completions.kwargs["model"] == bv.MODEL


@pytest.mark.parametrize("effort", ["max", "xhigh", "high"])
def test_build_v3有档位时照常发出reasoning_effort(effort):
    """对照组：非空档位必须照常发 —— 防止「顺手删掉参数」也算过。"""
    os.environ["LLM_EFFORT"] = effort
    bv = _reload("build_v3")
    client = _FakeClient()
    bv._call_with_template(client, "src={src}", "正文", {}, heartbeat=False)
    assert client.completions.kwargs is not None
    assert client.completions.kwargs.get("reasoning_effort") == effort


def test_chunk_scan显式设空时不发reasoning_effort():
    os.environ["LLM_EFFORT"] = ""
    cs = _reload("chunk_scan")
    client = _FakeClient()
    cs._call(client, "sys", "user", cs.SCAN_EFFORT)
    assert client.completions.kwargs is not None
    assert "reasoning_effort" not in client.completions.kwargs, \
        f"显式设空仍发了 reasoning_effort：{sorted(client.completions.kwargs)}"


def test_chunk_scan有档位时照常发出reasoning_effort():
    os.environ["LLM_EFFORT"] = "xhigh"
    cs = _reload("chunk_scan")
    client = _FakeClient()
    cs._call(client, "sys", "user", cs.SCAN_EFFORT)
    assert client.completions.kwargs is not None
    assert client.completions.kwargs.get("reasoning_effort") == "xhigh"


def test_correct_typos显式设空时不发reasoning_effort():
    os.environ["LLM_EFFORT"] = ""
    ct = _reload("correct_typos")
    client = _FakeClient()
    ct._call(client, "sys", "user", "")
    assert client.completions.kwargs is not None
    assert "reasoning_effort" not in client.completions.kwargs, \
        f"显式设空仍发了 reasoning_effort：{sorted(client.completions.kwargs)}"


def test_correct_typos有档位时照常发出reasoning_effort():
    ct = _reload("correct_typos")
    client = _FakeClient()
    ct._call(client, "sys", "user", "high")
    assert client.completions.kwargs is not None
    assert client.completions.kwargs.get("reasoning_effort") == "high"


# ── 5. 防回归：源码里不得再有「只认专有名」的裸读 ────────────────

@pytest.mark.parametrize("mod", ["build_v3", "chunk_scan", "correct_typos"])
def test_不再有只认专用名的裸读(mod):
    """专有名只能以**兜底**形态出现：`os.environ.get("LLM_X", os.environ.get("MINIMAX_X", …))`。

    两种违规形态必须为零：
      ① `os.environ["MINIMAX_API_KEY"]` 下标硬读 → 缺 key 时直接 KeyError
      ② `os.environ.get("MINIMAX_X", …)` 单独取值 → 通用名用户被挡在门外

    ② 特意用 AST 判父子关系而非正则：正则分不清内外两层，会把**合法的**
    兜底读一并报出来（第一版就是这么写错的，16 passed / 3 failed）。

    这条直接覆盖 opensource 的两个 CLI 入口 —— 它们此前 `os.environ["MINIMAX_API_KEY"]`
    硬读，而本仓 load_env 只放行通用名，于是照 .env.example 配好的用户**必然**退出。
    """
    import ast
    m = _reload(mod)
    tree = ast.parse(pathlib.Path(m.__file__).read_text(encoding="utf-8"))
    parent = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}

    def _first_str(call) -> str | None:
        a = call.args[0] if call.args else None
        return a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else None

    def _is_environ_get(node) -> bool:
        # ⚠️ `os.environ.get(...)` 的 func 是 Attribute(attr="get",
        # value=Attribute(attr="environ", value=Name(id="os"))) ——
        # **value 不是 Name**。第一版写成 `isinstance(node.func.value, ast.Name)`
        # 导致匹配恒为 False，整条测试变成「怎么改都绿」的空筛子
        # （变异验证时被抓住：把代码改回专有名优先，它依然 PASSED）。
        return (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "environ"
                and isinstance(node.func.value.value, ast.Name)
                and node.func.value.value.id == "os")

    violations = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
                and node.slice.value.startswith("MINIMAX_")):
            violations.append(f"L{node.lineno} os.environ['{node.slice.value}'] 下标硬读")
            continue
        if _is_environ_get(node) and (name := _first_str(node)) \
                and name.startswith("MINIMAX_"):
            # 合法形态：它是通用名取值的兜底参数
            p = parent.get(node)
            ok = (isinstance(p, ast.Call) and _is_environ_get(p)
                  and (pf := _first_str(p)) and pf.startswith("LLM_")
                  and any(a is node for a in p.args[1:]))
            if not ok:
                violations.append(f"L{node.lineno} os.environ.get('{name}', …) 只认专有名")
    assert not violations, f"{mod} 仍有只认专用名的取值：\n  " + "\n  ".join(violations)
