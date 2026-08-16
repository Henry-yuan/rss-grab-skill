#!/usr/bin/env python3
"""_shared/env.py 单测：.env 解析（引号剥离 / 注释 / setdefault 不覆盖）。"""
import os
import sys
import pathlib

TESTS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR.parent))  # _shared/ 目录

import env


def _clean_keys():
    for k in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        os.environ.pop(k, None)


def test_load_env_strips_matched_quotes(tmp_path):
    """成对引号剥离：KEY="value" / KEY='value' 的值不带引号。"""
    env_file = tmp_path / ".env"
    env_file.write_text(
        'LLM_API_KEY="sk-test-123"\n'
        "LLM_BASE_URL='https://x.example/v1'\n",
        encoding="utf-8",
    )
    _clean_keys()
    try:
        env.load_env(tmp_path)
        assert os.environ["LLM_API_KEY"] == "sk-test-123", \
            f"双引号未剥离: {os.environ['LLM_API_KEY']!r}"
        assert os.environ["LLM_BASE_URL"] == "https://x.example/v1", \
            f"单引号未剥离: {os.environ['LLM_BASE_URL']!r}"
    finally:
        _clean_keys()
    print("✅ test_load_env_strips_matched_quotes")


def test_load_env_keeps_bare_value(tmp_path):
    """无引号值 / 注释行 / 空行行为不变。"""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# 注释\n"
        "\n"
        "LLM_API_KEY=sk-bare\n"
        "LLM_BASE_URL=https://y.example/v1\n"
        "LLM_MODEL=m1\n",
        encoding="utf-8",
    )
    _clean_keys()
    try:
        env.load_env(tmp_path)
        assert os.environ["LLM_API_KEY"] == "sk-bare"
        assert os.environ["LLM_MODEL"] == "m1"
    finally:
        _clean_keys()
    print("✅ test_load_env_keeps_bare_value")


def test_load_env_no_override_existing(tmp_path):
    """setdefault 语义：已有环境变量不被 .env 覆盖（原行为保持）。"""
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_API_KEY=from-file\n", encoding="utf-8")
    os.environ["LLM_API_KEY"] = "from-env"
    os.environ["LLM_BASE_URL"] = "https://z.example/v1"
    try:
        env.load_env(tmp_path)
        assert os.environ["LLM_API_KEY"] == "from-env"
    finally:
        _clean_keys()
    print("✅ test_load_env_no_override_existing")


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td)
        test_load_env_strips_matched_quotes(p)
        test_load_env_keeps_bare_value(p)
        test_load_env_no_override_existing(p)
    print("\n全部 3 个测试通过")
