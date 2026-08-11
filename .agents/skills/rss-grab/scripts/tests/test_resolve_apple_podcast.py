#!/usr/bin/env python3
"""resolve_apple_podcast.py 单测。

覆盖纯函数（不联网）：
  - extract_apple_id：从 URL 提取数字 ID
  - parse_feed_url_from_html：从 HTML 字符串提取 feedUrl
  - parse_meta_from_html：从 HTML 提取 title / description
"""
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))  # scripts/ 目录

import resolve_apple_podcast as rap


# ── 预制 HTML 片段（模拟 Apple Podcasts 页面结构）──

MOCK_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta name="apple:content_id" content="1729552193">
  <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"BreadcrumbList"}
  </script>
  <script id="schema:show" type="application/ld+json">
    {"@context":"https://schema.org","@type":"PodcastSeries",
     "name":"十字路口Crossing",
     "description":"两个在北美科技圈工作的朋友聊科技和社会的播客",
     "url":"https://podcasts.apple.com/cn/podcast/id1729552193"}
  </script>
</head>
<body>
  <script>
    var shelfItems = [
      {"feedUrl":"https://feed.xyzfm.space/68fyjknth9hj","name":"十字路口Crossing"},
      {"feedUrl":"https://feed.xyzfm.space/recommended1","name":"推荐播客1"}
    ];
    var showData = {"feedUrl":"https://feed.xyzfm.space/68fyjknth9hj"};
  </script>
  <div>页面正文</div>
</body>
</html>
"""

MOCK_HTML_NO_FEED = """
<!DOCTYPE html>
<html><body><div>没有 feedUrl 的页面</div></body></html>
"""

MOCK_HTML_NO_META = """
<!DOCTYPE html>
<html><body>
  <script>{"feedUrl":"https://example.com/feed.xml"}</script>
</body></html>
"""

MOCK_HTML_MULTI_FEED = """
<script>
  var data = [
    {"feedUrl":"https://first/feed.xml","name":"本节目"},
    {"feedUrl":"https://second/feed.xml","name":"推荐1"},
    {"feedUrl":"https://third/feed.xml","name":"推荐2"}
  ];
</script>
"""


def test_extract_apple_id_normal():
    """正常 URL 提取 ID。"""
    url = "https://podcasts.apple.com/cn/podcast/%E5%8D%81%E5%AD%97%E8%B7%AF%E5%8F%A3crossing/id1729552193"
    assert rap.extract_apple_id(url) == "1729552193"
    print("✅ test_extract_apple_id_normal")


def test_extract_apple_id_us():
    """美区 URL 也能提取。"""
    url = "https://podcasts.apple.com/us/podcast/some-show/id1711052890"
    assert rap.extract_apple_id(url) == "1711052890"
    print("✅ test_extract_apple_id_us")


def test_extract_apple_id_no_id():
    """URL 不含 id<数字> 时抛 ValueError。"""
    url = "https://podcasts.apple.com/cn/podcast/some-show"
    try:
        rap.extract_apple_id(url)
        assert False, "应抛 ValueError"
    except ValueError as e:
        assert "无法从 URL 提取" in str(e)
    print("✅ test_extract_apple_id_no_id")


def test_parse_feed_url_from_html():
    """从完整 mock HTML 提取 feedUrl（取第一个匹配）。"""
    feed_url = rap.parse_feed_url_from_html(MOCK_HTML)
    assert feed_url == "https://feed.xyzfm.space/68fyjknth9hj"
    print("✅ test_parse_feed_url_from_html")


def test_parse_feed_url_from_html_not_found():
    """HTML 中无 feedUrl 时抛 RuntimeError。"""
    try:
        rap.parse_feed_url_from_html(MOCK_HTML_NO_FEED)
        assert False, "应抛 RuntimeError"
    except RuntimeError as e:
        assert "未找到 feedUrl" in str(e)
    print("✅ test_parse_feed_url_from_html_not_found")


def test_parse_feed_url_from_html_multi():
    """多个 feedUrl 时取第一个。"""
    feed_url = rap.parse_feed_url_from_html(MOCK_HTML_MULTI_FEED)
    assert feed_url == "https://first/feed.xml"
    print("✅ test_parse_feed_url_from_html_multi")


def test_parse_meta_from_html():
    """从 mock HTML 提取 title / description。"""
    meta = rap.parse_meta_from_html(MOCK_HTML)
    assert meta["title"] == "十字路口Crossing"
    assert "科技和社会" in meta["description"]
    print("✅ test_parse_meta_from_html")


def test_parse_meta_from_html_no_meta():
    """HTML 中无 schema:show 时返回 None。"""
    meta = rap.parse_meta_from_html(MOCK_HTML_NO_META)
    assert meta["title"] is None
    assert meta["description"] is None
    print("✅ test_parse_meta_from_html_no_meta")


def test_parse_meta_from_html_broken_json():
    """schema:show 的 JSON 损坏时返回 None（不崩）。"""
    broken_html = """
    <script id="schema:show" type="application/ld+json">
      {这不是合法JSON}
    </script>
    """
    meta = rap.parse_meta_from_html(broken_html)
    assert meta["title"] is None
    assert meta["description"] is None
    print("✅ test_parse_meta_from_html_broken_json")


def test_parse_meta_missing_fields():
    """JSON-LD 存在但缺 name / description 时返回 None。"""
    html = """
    <script id="schema:show" type="application/ld+json">
      {"@context":"https://schema.org","@type":"PodcastSeries"}
    </script>
    """
    meta = rap.parse_meta_from_html(html)
    assert meta["title"] is None
    assert meta["description"] is None
    print("✅ test_parse_meta_missing_fields")


def test_resolve_apple_url_rejects_non_apple():
    """非 Apple Podcasts 链接抛 ValueError。"""
    try:
        rap.resolve_apple_url("https://example.com/podcast/123")
        assert False, "应抛 ValueError"
    except ValueError as e:
        assert "不是 Apple Podcasts 链接" in str(e)
    print("✅ test_resolve_apple_url_rejects_non_apple")


if __name__ == "__main__":
    tests = [
        test_extract_apple_id_normal,
        test_extract_apple_id_us,
        test_extract_apple_id_no_id,
        test_parse_feed_url_from_html,
        test_parse_feed_url_from_html_not_found,
        test_parse_feed_url_from_html_multi,
        test_parse_meta_from_html,
        test_parse_meta_from_html_no_meta,
        test_parse_meta_from_html_broken_json,
        test_parse_meta_missing_fields,
        test_resolve_apple_url_rejects_non_apple,
    ]

    for t in tests:
        t()

    print(f"\n全部 {len(tests)} 个测试通过")
