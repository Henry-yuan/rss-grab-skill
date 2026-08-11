#!/usr/bin/env python3
"""subscribe_manager.py 单测。覆盖：
  - guid 编解码（含 base64 兜底）
  - load/save 往返（frontmatter + feed 元数据 + 三区 + guid 注释）
  - find_new_items 增量去重（guid / link 降级）
  - collect_known_guids 三区扫描
  - mark_done 确认 -> 已转化
  - update_summary 单期摘要替换
  - YAML frontmatter 解析失败降级
  - add/remove subscription 订阅表读写
"""
import json
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))  # scripts/ 目录

import subscribe_manager as sm


def test_guid_encode_decode():
    """普通 guid 不编码，含 --> 的 base64 编码，往返一致。"""
    # 普通 guid
    assert sm.encode_guid("6a773808c4079d62c57f5802") == "6a773808c4079d62c57f5802"
    assert sm.decode_guid("6a773808c4079d62c57f5802") == "6a773808c4079d62c57f5802"

    # 含危险字符
    dangerous = "abc-->def<!--ghi"
    encoded = sm.encode_guid(dangerous)
    assert encoded.startswith("b64:")
    assert sm.decode_guid(encoded) == dangerous

    # 空串
    assert sm.encode_guid("") == ""
    assert sm.decode_guid("") == ""
    print("✅ test_guid_encode_decode")


def test_extract_guid():
    """从行文本提取 guid 注释。"""
    line = "- [x] 6. Agent 元年 <!-- guid:6a4511d92e335a35a80c8431 -->"
    assert sm.extract_guid(line) == "6a4511d92e335a35a80c8431"

    # 无注释
    assert sm.extract_guid("- [ ] 1. 标题") == ""

    # base64 编码的 guid
    dangerous = "abc-->def"
    encoded = sm.encode_guid(dangerous)
    line = f"- [ ] 1. 标题 <!-- guid:{encoded} -->"
    assert sm.extract_guid(line) == dangerous
    print("✅ test_extract_guid")


def test_load_save_roundtrip(tmp_path):
    """load/save 往返：frontmatter + feed 元数据 + 三区 + guid 注释。"""
    state_path = tmp_path / "test_source.md"

    # 构造 state 并保存
    state = sm._empty_state()
    state["frontmatter"] = {
        "source": "测试播客",
        "feed_url": "https://feed.xyzfm.space/test123",
        "subscribed_at": "2026-08-11",
        "last_fetched": "2026-08-11T22:30",
    }
    state["feed_meta"] = {
        "节目名": "测试播客",
        "作者": "测试作者",
        "语言": "zh-cn",
        "节目链接": "https://example.com/podcast/1",
        "RSS 源": "https://feed.xyzfm.space/test123",
        "节目简介": "测试简介",
    }
    state["pending"] = [{
        "checkbox": "[ ]",
        "seq": 1,
        "title": "第一期",
        "guid": "guid-001",
        "fields": {
            "发布日期": "Sun, 09 Aug 2026 16:00:00 GMT",
            "时长": "00:56:24",
            "一句话概括": "这是概括",
            "链接": "https://example.com/episode/1",
        },
    }]
    state["confirmed"] = [{
        "checkbox": "[x]",
        "seq": 2,
        "title": "第二期",
        "guid": "guid-002",
        "fields": {"链接": "https://example.com/episode/2"},
    }]

    sm.save_state(state_path, state)

    # 重新加载，验证往返
    loaded = sm.load_state(state_path)
    assert loaded["frontmatter"]["source"] == "测试播客"
    assert loaded["frontmatter"]["feed_url"] == "https://feed.xyzfm.space/test123"
    assert loaded["feed_meta"]["节目名"] == "测试播客"
    assert loaded["feed_meta"]["作者"] == "测试作者"

    assert len(loaded["pending"]) == 1
    assert loaded["pending"][0]["guid"] == "guid-001"
    assert loaded["pending"][0]["title"] == "第一期"
    assert loaded["pending"][0]["checkbox"] == "[ ]"
    assert loaded["pending"][0]["fields"]["一句话概括"] == "这是概括"
    assert loaded["pending"][0]["seq"] == 1

    assert len(loaded["confirmed"]) == 1
    assert loaded["confirmed"][0]["guid"] == "guid-002"
    assert loaded["confirmed"][0]["checkbox"] == "[x]"
    print("✅ test_load_save_roundtrip")


def test_find_new_items_guid():
    """按 guid 去重：已知 guid 跳过，新 guid 返回。"""
    feed_items = [
        {"guid": "g1", "title": "t1", "link": "l1"},
        {"guid": "g2", "title": "t2", "link": "l2"},
        {"guid": "g3", "title": "t3", "link": "l3"},
    ]
    known = {"g1", "g3"}
    new = sm.find_new_items(feed_items, known)
    assert len(new) == 1
    assert new[0]["guid"] == "g2"
    print("✅ test_find_new_items_guid")


def test_find_new_items_no_guid_fallback_link():
    """无 guid 时降级用 link 去重。"""
    feed_items = [
        {"guid": None, "title": "t1", "link": "https://e.com/1"},
        {"guid": None, "title": "t2", "link": "https://e.com/2"},
    ]
    known = {"link:https://e.com/1"}
    new = sm.find_new_items(feed_items, known)
    assert len(new) == 1
    assert new[0]["_dedup_key"] == "link:https://e.com/2"
    print("✅ test_find_new_items_no_guid_fallback_link")


def test_collect_known_guids():
    """三区都扫，收集所有已知 guid。"""
    state = {
        "pending": [{"guid": "g1"}, {"guid": "g2"}],
        "confirmed": [{"guid": "g3"}],
        "done": [{"guid": "g4"}, {"guid": "g5"}],
    }
    guids = sm.collect_known_guids(state)
    assert guids == {"g1", "g2", "g3", "g4", "g5"}
    print("✅ test_collect_known_guids")


def test_mark_done(tmp_path):
    """确认区 [x] -> 已转化 [done]，附笔记路径。"""
    state_path = tmp_path / "test.md"
    state = sm._empty_state()
    state["frontmatter"] = {"source": "测试", "feed_url": "https://x", "subscribed_at": "2026-08-11"}
    state["confirmed"] = [{
        "checkbox": "[x]",
        "seq": 6,
        "title": "Agent 元年",
        "guid": "g6",
        "fields": {},
    }]
    sm.save_state(state_path, state)

    ok = sm.mark_done(state_path, "g6", "rss/notes/测试/6-Agent元年.md")
    assert ok is True

    loaded = sm.load_state(state_path)
    assert len(loaded["confirmed"]) == 0
    assert len(loaded["done"]) == 1
    assert loaded["done"][0]["guid"] == "g6"
    assert loaded["done"][0]["checkbox"] == "[done]"
    assert "6-Agent元年.md" in loaded["done"][0].get("note_path", "")
    print("✅ test_mark_done")


def test_mark_done_not_found(tmp_path):
    """guid 不存在返回 False。"""
    state_path = tmp_path / "test.md"
    state = sm._empty_state()
    state["frontmatter"] = {"source": "测试", "feed_url": "https://x", "subscribed_at": "2026-08-11"}
    sm.save_state(state_path, state)

    ok = sm.mark_done(state_path, "not-exist", "note.md")
    assert ok is False
    print("✅ test_mark_done_not_found")


def test_update_summary(tmp_path):
    """单期摘要替换：三段写入 fields。"""
    state_path = tmp_path / "test.md"
    state = sm._empty_state()
    state["frontmatter"] = {"source": "测试", "feed_url": "https://x", "subscribed_at": "2026-08-11"}
    state["pending"] = [{
        "checkbox": "[ ]",
        "seq": 1,
        "title": "第一期",
        "guid": "g1",
        "fields": {"一句话概括": "旧概括"},
    }]
    sm.save_state(state_path, state)

    new_summary = "一句话概括：新概括\n内容概览：新概览\n值得关注：⏱01:00 重点"
    ok = sm.update_summary(state_path, "g1", new_summary)
    assert ok is True

    loaded = sm.load_state(state_path)
    item = loaded["pending"][0]
    assert item["fields"]["一句话概括"] == "新概括"
    assert item["fields"]["内容概览"] == "新概览"
    assert "⏱01:00" in item["fields"]["值得关注"]
    print("✅ test_update_summary")


def test_yaml_frontmatter_broken(tmp_path):
    """frontmatter 解析失败不崩，返回空 frontmatter + 正文。"""
    state_path = tmp_path / "test.md"
    # 故意写坏 frontmatter（缺少闭合 ---）
    state_path.write_text(
        '---\n'
        'source: 测试\n'
        'feed_url: https://x\n'
        '（没有闭合的 frontmatter）\n'
        '# 正文\n',
        encoding="utf-8",
    )
    state = sm.load_state(state_path)
    # 不崩即可，frontmatter 可能为空
    assert "pending" in state
    print("✅ test_yaml_frontmatter_broken")


def test_add_remove_subscription(tmp_path, monkeypatch):
    """订阅表读写：新增 + 退订。"""
    # 用临时目录替换 SUBSCRIBE_DIR
    tmp_subscribe = tmp_path / "订阅"
    tmp_subscribe.mkdir()
    tmp_subs = tmp_subscribe / "subscriptions.json"

    monkeypatch.setattr(sm, "SUBSCRIBE_DIR", tmp_subscribe)
    monkeypatch.setattr(sm, "SUBSCRIPTIONS_PATH", tmp_subs)
    monkeypatch.setattr(sm, "PROJECT_ROOT", tmp_path)

    # 新增
    meta = {"title": "测试播客", "author": "作者", "link": "https://e.com/p"}
    sm.add_subscription("https://feed.xyzfm.space/test", "测试播客", meta)

    subs = sm.load_subscriptions()
    assert len(subs["sources"]) == 1
    assert subs["sources"][0]["name"] == "测试播客"
    assert subs["sources"][0]["feed_url"] == "https://feed.xyzfm.space/test"

    # 状态文件生成
    state_path = tmp_path / "rss" / "订阅" / "测试播客.md"
    assert state_path.exists()

    # 退订
    ok = sm.remove_subscription("测试播客")
    assert ok is True
    subs = sm.load_subscriptions()
    assert len(subs["sources"]) == 0

    # 状态文件保留（标记已退订）
    assert state_path.exists()
    content = state_path.read_text(encoding="utf-8")
    assert "已退订" in content
    print("✅ test_add_remove_subscription")


def test_render_multi_line_field(tmp_path):
    """多行字段（如值得关注的多个时间点）往返不丢。"""
    state_path = tmp_path / "test.md"
    state = sm._empty_state()
    state["frontmatter"] = {"source": "测试", "feed_url": "https://x", "subscribed_at": "2026-08-11"}
    state["pending"] = [{
        "checkbox": "[ ]",
        "seq": 1,
        "title": "第一期",
        "guid": "g1",
        "fields": {
            "值得关注": "- ⏱07:33 信号\n- ⏱17:50 机会",
        },
    }]
    sm.save_state(state_path, state)

    loaded = sm.load_state(state_path)
    val = loaded["pending"][0]["fields"]["值得关注"]
    assert "⏱07:33" in val
    assert "⏱17:50" in val
    print("✅ test_render_multi_line_field")




def test_mark_unsubscribed_keeps_frontmatter(tmp_path):
    """退订告警放 frontmatter 后，不破坏解析（Codex 审查反馈 P1）。"""
    state_path = tmp_path / "test.md"
    state = sm._empty_state()
    state["frontmatter"] = {
        "source": "测试", "feed_url": "https://feed.xyzfm.space/test",
        "subscribed_at": "2026-08-11",
    }
    sm.save_state(state_path, state)

    sm._mark_unsubscribed(state_path, "测试")
    # 重新加载，feed_url 必须还在
    loaded = sm.load_state(state_path)
    assert loaded["frontmatter"]["feed_url"] == "https://feed.xyzfm.space/test"
    assert "已退订" in state_path.read_text(encoding="utf-8")
    print("✅ test_mark_unsubscribed_keeps_frontmatter")

def test_feed_meta_multi_line_roundtrip(tmp_path):
    """多行简介 + 字段间空引用行 的 load/save/load 往返不丢（引用块修复回归）。"""
    state_path = tmp_path / "test.md"
    state = sm._empty_state()
    state["frontmatter"] = {"source": "测试", "feed_url": "https://x", "subscribed_at": "2026-08-11"}
    state["feed_meta"] = {
        "节目名": "测试",
        "作者": "作者A",
        "节目简介": "第一段简介。\n\n「引用」第二段。\n第三段。",
    }
    sm.save_state(state_path, state)

    loaded = sm.load_state(state_path)
    assert loaded["feed_meta"]["节目名"] == "测试"
    assert loaded["feed_meta"]["作者"] == "作者A"
    desc = loaded["feed_meta"]["节目简介"]
    assert "第二段" in desc
    assert "第三段" in desc
    print("✅ test_feed_meta_multi_line_roundtrip")

if __name__ == "__main__":
    # 创建临时目录给需要 tmp_path 的测试
    import tempfile
    tests_with_tmp = [
        ("test_load_save_roundtrip", test_load_save_roundtrip),
        ("test_mark_done", test_mark_done),
        ("test_mark_done_not_found", test_mark_done_not_found),
        ("test_update_summary", test_update_summary),
        ("test_yaml_frontmatter_broken", test_yaml_frontmatter_broken),
        ("test_add_remove_subscription", test_add_remove_subscription),
        ("test_render_multi_line_field", test_render_multi_line_field),
        ("test_mark_unsubscribed_keeps_frontmatter", test_mark_unsubscribed_keeps_frontmatter),
        ("test_feed_meta_multi_line_roundtrip", test_feed_meta_multi_line_roundtrip),
    ]
    simple_tests = [
        test_guid_encode_decode,
        test_extract_guid,
        test_find_new_items_guid,
        test_find_new_items_no_guid_fallback_link,
        test_collect_known_guids,
    ]

    for t in simple_tests:
        t()

    for name, t in tests_with_tmp:
        with tempfile.TemporaryDirectory() as td:
            import inspect
            sig = inspect.signature(t)
            if "monkeypatch" in sig.parameters:
                # monkeypatch 需要 pytest，这里手动 mock
                class FakeMonkey:
                    def setattr(self, obj, name, value):
                        setattr(obj, name, value)
                t(Path(td), FakeMonkey())
            else:
                t(Path(td))

    print(f"\n全部 {len(simple_tests) + len(tests_with_tmp)} 个测试通过")

