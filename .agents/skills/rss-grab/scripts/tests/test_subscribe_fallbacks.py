#!/usr/bin/env python3
"""订阅链路的「None 兜底」与「无订阅」友好提示回归测试。

两条独立的坑，都曾以裸 traceback 呈现：

## A. None 兜底失效（C4，2026-10-09 修）

Apple 节目页改版后 `schema:show` 不存在 → 节目名恒为 None，沿途两道兜底都没接住：

    result.get("title", "")      # default 只管 key 缺失，key 在但值 None 时原样返回
    meta.setdefault("title", …)  # 不覆盖已存在的 None

结果 source_name 变成 None，一路带到 `subscribe_manager._sanitize_filename`
里的 `re.sub(pattern, …, None)` → TypeError 栈回溯。

这里用 cmd_subscribe 全程打桩跑一遍（不联网），断言**节目名最终来自 RSS 元数据**。

## B. 还没订阅就跑 --all（C2，2026-10-09 修）

`generate_note_for_confirmed.py --all` 直接 read subscriptions.json，
用户还没订阅 → FileNotFoundError 栈回溯。现在读之前先判断，打一行提示、退出码 0。
"""
import json
import pathlib
import sys

import pytest

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import fetch_rss_feed as f                     # noqa: E402
import generate_note_for_confirmed as g        # noqa: E402
import subscribe_manager as sm                 # noqa: E402


# ── A. None 兜底 ──

FEED_WITH_TITLE = {
    "title": "来自 RSS 的真实节目名",
    "author": "作者",
    "language": "zh",
    "link": "https://example.com/show",
    "description": "RSS 里的完整简介",
    "items": [{
        "title": "第 1 期",
        "guid": "g1",
        "pub_date": "2026-10-01",
        "enclosure": {"url": "https://example.com/1.m4a", "type": "audio/mp4"},
    }],
}


@pytest.fixture
def subscribed(monkeypatch, tmp_path):
    """跑完 cmd_subscribe 后，把 add_subscription 收到的参数交出来断言。

    打桩点：Apple 反推、RSS 抓取与解析、订阅表落盘 —— 全程不联网、不写真实仓库。
    """
    import resolve_apple_podcast

    captured = {}

    # Apple 反推：返回 title=None（这正是改版后的真实形态）
    monkeypatch.setattr(
        resolve_apple_podcast, "resolve_apple_url",
        lambda url: {"feed_url": "https://feed.example/rss",
                     "title": None, "description": None,
                     "apple_id": "1200361736", "source": "itunes_api"},
    )
    monkeypatch.setattr(f, "fetch_xml", lambda url: b"<rss/>")
    monkeypatch.setattr(f.parse_rss, "parse_text", lambda x: dict(FEED_WITH_TITLE))
    monkeypatch.setattr(sm, "add_subscription",
                        lambda feed_url, name, meta: captured.update(
                            feed_url=feed_url, name=name, meta=meta) or {})
    monkeypatch.setattr(sm, "load_subscriptions",
                        lambda: {"sources": []})
    return captured


def test_subscribe_falls_back_to_rss_title_when_apple_gives_none(subscribed):
    """Apple 返回 title=None 时，节目名必须来自 RSS 元数据（不能是 None/"未命名"）。"""
    f.cmd_subscribe("https://podcasts.apple.com/us/podcast/x/id1200361736")
    assert subscribed["name"] == "来自 RSS 的真实节目名"
    assert subscribed["name"] is not None
    assert subscribed["meta"]["title"] == "来自 RSS 的真实节目名"
    print("✅ test_subscribe_falls_back_to_rss_title_when_apple_gives_none")


def test_subscribe_falls_back_when_title_is_empty_string(subscribed):
    """Apple 返回 title=""（空串）同样要兜底 —— 空串和 None 一样是「没取到」。"""
    import resolve_apple_podcast

    resolve_apple_podcast.resolve_apple_url = lambda url: {
        "feed_url": "https://feed.example/rss", "title": "",
        "description": "", "apple_id": "1", "source": "itunes_api"}
    f.cmd_subscribe("https://podcasts.apple.com/us/podcast/x/id1200361736")
    assert subscribed["name"] == "来自 RSS 的真实节目名"
    print("✅ test_subscribe_falls_back_when_title_is_empty_string")


def test_subscribe_keeps_apple_title_when_available(subscribed):
    """Apple 取到节目名时不该被 RSS 覆盖（正常路径不被兜底逻辑误伤）。"""
    import resolve_apple_podcast

    resolve_apple_podcast.resolve_apple_url = lambda url: {
        "feed_url": "https://feed.example/rss", "title": "Apple 上的节目名",
        "description": "Apple 上的简介", "apple_id": "1", "source": "itunes_api"}
    f.cmd_subscribe("https://podcasts.apple.com/us/podcast/x/id1200361736")
    assert subscribed["name"] == "Apple 上的节目名"
    print("✅ test_subscribe_keeps_apple_title_when_available")


def test_subscribe_exits_when_no_title_anywhere(monkeypatch):
    """Apple 与 RSS 都没有 title → 友好提示 + 退出码 1，不进 add_subscription。

    否则会造出一个名字叫「未命名节目」的订阅源，坏得无声无息。
    """
    import resolve_apple_podcast

    called = []
    feed_without_title = dict(FEED_WITH_TITLE)
    feed_without_title["title"] = ""
    monkeypatch.setattr(
        resolve_apple_podcast, "resolve_apple_url",
        lambda url: {"feed_url": "https://feed.example/rss", "title": None,
                     "description": None, "apple_id": "1", "source": "itunes_api"})
    monkeypatch.setattr(f, "fetch_xml", lambda url: b"<rss/>")
    monkeypatch.setattr(f.parse_rss, "parse_text", lambda x: dict(feed_without_title))
    monkeypatch.setattr(sm, "add_subscription",
                        lambda *a, **k: called.append(a) or {})
    with pytest.raises(SystemExit) as e:
        f.cmd_subscribe("https://podcasts.apple.com/us/podcast/x/id1200361736")
    assert e.value.code == 1
    assert called == []          # 没落任何订阅源
    print("✅ test_subscribe_exits_when_no_title_anywhere")


def test_add_subscription_tolerates_none_name(monkeypatch, tmp_path):
    """add_subscription 的最后一道防线：source_name=None 不炸栈。

    直接调 re.sub(pattern, x, None) 会 TypeError；这里钉死「不抛异常」。
    """
    sub_dir = tmp_path / "订阅"
    sub_dir.mkdir()
    monkeypatch.setattr(sm, "SUBSCRIBE_DIR", sub_dir)
    monkeypatch.setattr(sm, "SUBSCRIPTIONS_PATH", sub_dir / "subscriptions.json")
    monkeypatch.setattr(sm, "PROJECT_ROOT", tmp_path)
    entry = sm.add_subscription("https://feed.example/rss", None)
    assert entry["name"] == "未命名节目"
    # 状态文件路径是 PROJECT_ROOT / state_file（state_file 形如 rss/订阅/<名>.md）
    assert (tmp_path / "rss" / "订阅" / "未命名节目.md").exists()
    print("✅ test_add_subscription_tolerates_none_name")


# ── B. 无订阅时的友好提示 ──

def _run_main(argv, monkeypatch, tmp_path, subs_content):
    """把 main() 的仓库根指到临时目录后跑一次，返回 (退出码, 输出)。"""
    subs_path = tmp_path / "rss" / "订阅" / "subscriptions.json"
    if subs_content is not None:
        subs_path.parent.mkdir(parents=True, exist_ok=True)
        subs_path.write_text(subs_content, encoding="utf-8")
    root = tmp_path
    # main() 用 SCRIPT_DIR 往上数四层推仓库根：伪造一层即可
    monkeypatch.setattr(g, "SCRIPT_DIR", root / "a" / "b" / "c" / "d")
    monkeypatch.setattr(sys, "argv", ["generate_note_for_confirmed.py"] + argv)
    return g.main()


def test_all_without_subscriptions_file_is_friendly(monkeypatch, tmp_path, capsys):
    """subscriptions.json 不存在：提示 + 退出码 0，不抛 FileNotFoundError。"""
    code = _run_main(["--all", "--dry-run"], monkeypatch, tmp_path, None)
    out = capsys.readouterr().out
    assert code == 0
    assert "还没有任何订阅" in out
    assert "--subscribe" in out          # 告诉用户下一步做什么
    assert "Traceback" not in out
    print("✅ test_all_without_subscriptions_file_is_friendly")


def test_all_with_empty_sources_is_friendly(monkeypatch, tmp_path, capsys):
    """订阅表存在但 sources 为空：同样友好退出，且不说「文件不存在」。"""
    code = _run_main(["--all", "--dry-run"], monkeypatch, tmp_path,
                     json.dumps({"sources": []}))
    out = capsys.readouterr().out
    assert code == 0
    assert "订阅表是空的" in out
    assert "Traceback" not in out
    print("✅ test_all_with_empty_sources_is_friendly")


def test_all_with_subscriptions_still_processes(monkeypatch, tmp_path, capsys):
    """有订阅时行为不变：进入正常处理循环（本次全跳过，因状态文件不存在）。"""
    payload = json.dumps({"sources": [
        {"name": "示例节目", "feed_url": "https://feed.example/rss",
         "state_file": "rss/订阅/示例节目.md"},
    ]})
    code = _run_main(["--all", "--dry-run"], monkeypatch, tmp_path, payload)
    out = capsys.readouterr().out
    assert code == 0
    assert "还没有任何订阅" not in out       # 不该误报
    assert "订阅表是空的" not in out
    assert "状态文件不存在" in out           # 正常走「跳过」分支
    assert "确认抓取 0 期" in out
    print("✅ test_all_with_subscriptions_still_processes")