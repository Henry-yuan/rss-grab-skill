#!/usr/bin/env python3
"""fetch_rss_pending 纯函数单测：parse_rss_url / find_pending_urls。"""
import sys, pathlib, tempfile
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))
import fetch_rss_pending as p


def test_parse_rss_url_https():
    """https RSS URL 原样返回。"""
    assert p.parse_rss_url("https://feed.xyzfm.space/y9qnpfdrctnx") == "https://feed.xyzfm.space/y9qnpfdrctnx"


def test_parse_rss_url_http():
    assert p.parse_rss_url("http://example.com/feed.xml") == "http://example.com/feed.xml"


def test_parse_rss_url_empty():
    """空串 / 非 URL -> None。"""
    assert p.parse_rss_url("") is None
    assert p.parse_rss_url("不是url") is None


def test_find_pending_urls():
    """从 ## 待抓取 段提取 - [ ] URL。"""
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        md = td / "RSS.md"
        md.write_text("""# RSS 待抓取

## 已抓取

- [x] https://done.example.com/feed ✅ 2026-08-08

## 待抓取

- [ ] https://feed.xyzfm.space/y9qnpfdrctnx
- [ ] https://example.com/feed.xml
- [ ]
""", encoding="utf-8")
        lines = md.read_text(encoding="utf-8").splitlines(keepends=True)
        pending = p.find_pending_urls(lines)
        assert len(pending) == 2
        urls = [u for _, u in pending]
        assert "https://feed.xyzfm.space/y9qnpfdrctnx" in urls
        assert "https://example.com/feed.xml" in urls


def test_find_pending_urls_empty():
    """无待抓取 -> 空列表。"""
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        md = td / "RSS.md"
        md.write_text("# RSS\n\n## 待抓取\n\n（无）\n", encoding="utf-8")
        lines = md.read_text(encoding="utf-8").splitlines(keepends=True)
        assert p.find_pending_urls(lines) == []
