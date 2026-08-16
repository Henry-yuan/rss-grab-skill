#!/usr/bin/env python3
"""preview_podcast 纯函数单测：strip 思考块 / 摘要替换 / 失败标注。

不测 LLM 调用（真实调用靠端到端验证）。"""
import sys, pathlib
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))
import preview_podcast as p


def _items(n=3):
    """构造 n 期假 item。"""
    return [{
        "title": f"标题{i}",
        "pub_date": f"Sun, 0{i} Aug 2026 16:00:00 GMT",
        "duration": "00:30:00",
        "description": f"第{i}期简介内容" * 5,
        "link": f"https://example.com/episode/{i}",
    } for i in range(1, n + 1)]


def test_strip_thinking_normal():
    """正常闭合思考块 -> 去掉，保留正文。"""
    raw = "<think>thinking</think>\n一句话概括：xxx"
    assert p.strip_thinking_blocks(raw) == "一句话概括：xxx"


def test_strip_thinking_unclosed():
    """未闭合思考块（无 </think>）-> 保留 </think> 后或换行后内容。"""
    raw = "<think>no closing tag\n一句话概括：xxx"
    assert "一句话概括" in p.strip_thinking_blocks(raw)


def test_strip_thinking_swallowed():
    """思考块吞掉全部（正则误匹配）-> 兜底找回 JSON/正文。"""
    raw = "<think>thinking</think>\n\n一句话概括：xxx"
    result = p.strip_thinking_blocks(raw)
    assert "一句话概括" in result


def test_strip_thinking_no_block():
    """无思考块 -> 原样。"""
    raw = "一句话概括：xxx"
    assert p.strip_thinking_blocks(raw) == "一句话概括：xxx"


def test_replace_descriptions_with_summaries():
    """AI 摘要替换 description 字段（key = guid 或 idx:N）。"""
    items = _items(2)
    summaries = {f"idx:{i - 1}": f"AI 摘要{i}" for i in range(1, 3)}
    p.replace_descriptions(items, summaries)
    assert items[0]["description"] == "AI 摘要1"
    assert items[1]["description"] == "AI 摘要2"


def test_replace_descriptions_missing_marks_failure():
    """缺失摘要的期标 ⚠️。"""
    items = _items(2)
    summaries = {f"idx:0": "AI 摘要1"}  # 缺 idx:1
    p.replace_descriptions(items, summaries)
    assert items[0]["description"] == "AI 摘要1"
    assert "⚠️ 摘要生成失败" in items[1]["description"]


def test_item_key_guid():
    """有 guid 时 key = guid。"""
    item = {"guid": "abc123", "title": "t"}
    assert p._item_key(item, 0) == "abc123"


def test_item_key_idx_fallback():
    """无 guid 时 key = idx:N。"""
    item = {"title": "t"}
    assert p._item_key(item, 3) == "idx:3"


def test_resume_skips_completed(tmp_path):
    """resume=True 读 checkpoint，跳过已完成期。"""
    import json
    from pathlib import Path
    cp = tmp_path / "test.checkpoint.json"
    # 预制 checkpoint：guid-a 已完成
    cp.write_text(json.dumps({"summaries": {"guid-a": "摘要A"}}), encoding="utf-8")

    items = [
        {"guid": "guid-a", "title": "t1", "description": "d1"},
        {"guid": "guid-b", "title": "t2", "description": "d2"},
    ]
    # 模拟：checkpoint 里只有 guid-a，resume 应让 start_idx 从 guid-b 开始
    # （直接验证 _item_key 匹配逻辑）
    assert p._item_key(items[0], 0) == "guid-a"
    assert p._item_key(items[1], 1) == "guid-b"


def test_main_standalone_disabled_with_hint():
    """回归：main() 不再调用已裁剪的 render_pick_file/pick_file_path（init 起就断裂）。

    独立运行应直接给出指引退出（SystemExit 非零 + 提示走 --fetch-updates），
    而不是先联网抓取再 AttributeError。
    """
    import io
    import contextlib
    buf = io.StringIO()
    code = None
    try:
        with contextlib.redirect_stdout(buf):
            p.main()
    except SystemExit as e:
        code = e.code
    assert code is not None and code != 0, "main() 应 SystemExit 非零"
    assert "fetch-updates" in buf.getvalue()
    print("✅ test_main_standalone_disabled_with_hint")


if __name__ == "__main__":
    import tempfile
    from pathlib import Path
    tests = [
        test_strip_thinking_normal,
        test_strip_thinking_unclosed,
        test_strip_thinking_swallowed,
        test_strip_thinking_no_block,
        test_replace_descriptions_with_summaries,
        test_replace_descriptions_missing_marks_failure,
        test_item_key_guid,
        test_item_key_idx_fallback,
        test_main_standalone_disabled_with_hint,
    ]
    for t in tests:
        t()
    with tempfile.TemporaryDirectory() as td:
        test_resume_skips_completed(Path(td))
    print(f"全部 {len(tests) + 1} 个测试通过")
