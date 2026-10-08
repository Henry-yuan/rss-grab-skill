#!/usr/bin/env python3
"""resolve_apple_podcast.py 单测。

覆盖纯函数（不联网）：
  - extract_apple_id：从 URL 提取数字 ID
  - parse_feed_url_from_html：从 HTML 字符串提取 feedUrl
  - parse_meta_from_html：从 HTML 提取 title / description
  - parse_lookup_response：解析 iTunes Lookup JSON（主路径）
  - resolve_apple_url 的**编排**：API 优先、故障降级 HTML、查无此节目不兜底
    （用 monkeypatch 替换两个 _fetch_* 接缝，不真联网）

背景：Apple 已把节目页改成 JS 动态渲染，HTML 里取不到 feedUrl / 节目名
（2026-10-09 实测 75 万字符页面里 schema:show 与 "feedUrl" 各 0 处），
主路径因此换成 iTunes Lookup API，HTML 解析降级为兜底。
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

# 改版后的 Apple 页面形态（2026-10-09 实测）：schema:show 已消失，但推荐流里的
# feedUrl 还在，且「第一个」属于**别的节目**。此时取第一个 feedUrl 会订阅错节目。
MOCK_HTML_RECOMMENDATION_FEED_ONLY = """
<script>
  var shelfItems = [
    {"feedUrl":"https://feed.other-station.example/abc","name":"别的节目"},
    {"feedUrl":"https://feed.other-station.example/def","name":"又一档节目"}
  ];
</script>
<div>Apple 播客（CN）- 网页播放器</div>
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


def test_is_apple_url_exact_host():
    """域名精确匹配：子域伪装 / 参数绕过 / file 协议全部拒绝。"""
    # 真链接
    assert rap.is_apple_podcasts_url("https://podcasts.apple.com/cn/podcast/xxx/id123") is True
    assert rap.is_apple_podcasts_url("https://podcasts.apple.com/us/podcast/y/id456?i=9") is True
    # 子域伪装（host 是 evil.com）
    assert rap.is_apple_podcasts_url("https://podcasts.apple.com.evil.com/id123") is False
    # 参数 / 路径绕过（host 不是 apple）
    assert rap.is_apple_podcasts_url("https://evil.com/?podcasts.apple.com") is False
    assert rap.is_apple_podcasts_url("https://evil.com/podcasts.apple.com") is False
    # 非 http(s) / 非法
    assert rap.is_apple_podcasts_url("file:///podcasts.apple.com") is False
    assert rap.is_apple_podcasts_url("not a url") is False
    assert rap.is_apple_podcasts_url("") is False
    print("✅ test_is_apple_url_exact_host")


def test_ensure_http_url_protocol_whitelist():
    """feed_url 协议白名单：仅 http/https，其他协议抛 ValueError。"""
    assert rap.ensure_http_url("https://feed.example/x") == "https://feed.example/x"
    assert rap.ensure_http_url("http://feed.example/y") == "http://feed.example/y"
    for bad in ("file:///etc/passwd", "javascript:alert(1)", "ftp://x/y", "不是url"):
        try:
            rap.ensure_http_url(bad)
            assert False, f"应抛 ValueError: {bad}"
        except ValueError:
            pass
    print("✅ test_ensure_http_url_protocol_whitelist")


# ── iTunes Lookup 主路径 ──
#
# 样例字段名与结构取自 itunes.apple.com/lookup?id=<id>&media=podcast 的真实响应
# （2026-10-09 实测：命中时 resultCount=1，results[0] 里是 collectionName /
# collectionDescription / feedUrl；未收录时 resultCount=0 且 results 为空数组）。
# 这里的节目名与 feed 地址是占位值，不作为断言基准 —— 真实值随 Apple 目录变化。

MOCK_LOOKUP_OK = json.dumps({
    "resultCount": 1,
    "results": [{
        "wrapperType": "collection",
        "kind": "podcast",
        "collectionId": 1200361736,
        "collectionName": "示例播客节目名",
        "collectionDescription": "这是节目简介。",
        "feedUrl": "https://feeds.example.com/abc123",
    }],
})

MOCK_LOOKUP_EMPTY = json.dumps({"resultCount": 0, "results": []})

MOCK_LOOKUP_NO_FEED_URL = json.dumps({
    "resultCount": 1,
    "results": [{"collectionId": 1200361736, "collectionName": "没有 feed 的节目"}],
})


def _patch_fetch(monkeypatch, api_payload=None, api_error=None,
                 html_payload=None, html_error=None, calls=None):
    """替换 _fetch_text / _fetch_html 两个接缝，记录调用顺序。全程不联网。"""
    def fake_text(url, what):
        if calls is not None:
            calls.append(("api", url))
        if api_error is not None:
            raise RuntimeError(api_error)
        return api_payload

    def fake_html(url):
        if calls is not None:
            calls.append(("html", url))
        if html_error is not None:
            raise RuntimeError(html_error)
        return html_payload

    monkeypatch.setattr(rap, "_fetch_text", fake_text)
    monkeypatch.setattr(rap, "_fetch_html", fake_html)


def test_build_lookup_url():
    """请求 URL 用 media=podcast（collection id 走 media，不是 entity）。"""
    url = rap.build_lookup_url("1200361736")
    assert url == "https://itunes.apple.com/lookup?id=1200361736&media=podcast"
    assert "entity=" not in url
    print("✅ test_build_lookup_url")


def test_parse_lookup_response_ok():
    """命中：feedUrl / collectionName / collectionDescription 三件套。"""
    info = rap.parse_lookup_response(MOCK_LOOKUP_OK, "1200361736")
    assert info["feed_url"] == "https://feeds.example.com/abc123"
    assert info["title"] == "示例播客节目名"
    assert info["description"] == "这是节目简介。"
    assert info["apple_id"] == "1200361736"
    assert info["source"] == "itunes_api"
    print("✅ test_parse_lookup_response_ok")


def test_parse_lookup_response_zero_result_is_friendly():
    """resultCount=0：抛 PodcastNotFound，且消息可操作（提示换链接 / 用 --feed-url）。"""
    try:
        rap.parse_lookup_response(MOCK_LOOKUP_EMPTY, "id123456789")
        assert False, "应抛 PodcastNotFound"
    except rap.PodcastNotFound as e:
        msg = str(e)
        assert "id123456789" in msg          # 点明是哪个 id
        assert "未收录" in msg                    # 说清是「查无此节目」而非崩溃
        assert "--feed-url" in msg               # 给出下一步
    print("✅ test_parse_lookup_response_zero_result_is_friendly")


def test_parse_lookup_response_result_count_nonzero_but_empty_results():
    """resultCount 与 results 不一致（results 空）也按查无此节目处理，不 KeyError。"""
    payload = json.dumps({"resultCount": 1, "results": []})
    try:
        rap.parse_lookup_response(payload, "123")
        assert False, "应抛 PodcastNotFound"
    except rap.PodcastNotFound:
        pass
    print("✅ test_parse_lookup_response_result_count_nonzero_but_empty_results")


def test_parse_lookup_response_missing_feed_url():
    """命中但没有 feedUrl：抛 PodcastNotFound，消息带出节目名便于人工确认。"""
    try:
        rap.parse_lookup_response(MOCK_LOOKUP_NO_FEED_URL, "1200361736")
        assert False, "应抛 PodcastNotFound"
    except rap.PodcastNotFound as e:
        assert "没有 feed 的节目" in str(e)
    print("✅ test_parse_lookup_response_missing_feed_url")


def test_parse_lookup_response_bad_json():
    """非 JSON 抛 RuntimeError（不是 KeyError / TypeError 裸栈）。"""
    try:
        rap.parse_lookup_response("这不是 JSON", "1200361736")
        assert False, "应抛 RuntimeError"
    except RuntimeError as e:
        assert "合法 JSON" in str(e)
    print("✅ test_parse_lookup_response_bad_json")


def test_parse_lookup_response_missing_name_is_none_not_empty_str():
    """节目名缺失时给 None（不是 ""）—— 调用方据此走自己的兜底链。

    这里钉死 None 而非 ""：改成 "" 会让 cmd_subscribe 分不清「没取到」，
    回到 setdefault 不覆盖 None 的老坑。
    """
    payload = json.dumps({
        "resultCount": 1,
        "results": [{"collectionId": 1, "feedUrl": "https://feeds.example.com/x"}],
    })
    info = rap.parse_lookup_response(payload, "1")
    assert info["title"] is None
    assert info["description"] is None
    print("✅ test_parse_lookup_response_missing_name_is_none_not_empty_str")


def test_resolve_apple_url_prefers_api(monkeypatch):
    """API 命中时不碰 HTML（Apple 页面现在根本取不到东西，多抓一次纯浪费）。"""
    calls = []
    _patch_fetch(monkeypatch, api_payload=MOCK_LOOKUP_OK,
                 html_payload=MOCK_HTML, calls=calls)
    info = rap.resolve_apple_url("https://podcasts.apple.com/us/podcast/x/id1200361736")
    assert info["source"] == "itunes_api"
    assert info["feed_url"] == "https://feeds.example.com/abc123"
    assert [c[0] for c in calls] == ["api"]      # HTML 接缝一次都没被调用
    print("✅ test_resolve_apple_url_prefers_api")


def test_resolve_apple_url_falls_back_to_html(monkeypatch):
    """API 网络故障 → 降级 HTML 兜底，成功则照常返回。"""
    calls = []
    _patch_fetch(monkeypatch, api_error="curl 超时", html_payload=MOCK_HTML,
                 calls=calls)
    info = rap.resolve_apple_url("https://podcasts.apple.com/cn/podcast/x/id1729552193")
    assert info["source"] == "html"
    assert info["feed_url"] == "https://feed.xyzfm.space/68fyjknth9hj"
    assert [c[0] for c in calls] == ["api", "html"]
    print("✅ test_resolve_apple_url_falls_back_to_html")


def test_html_fallback_refuses_feed_url_without_schema_show(monkeypatch):
    """页面只剩推荐流的 feedUrl（无 schema:show）时，兜底必须拒绝而不是取第一个。

    否则会静默订阅到**别的节目** —— 比明确失败糟得多。
    """
    _patch_fetch(monkeypatch, api_error="curl 超时",
                 html_payload=MOCK_HTML_RECOMMENDATION_FEED_ONLY)
    try:
        rap.resolve_apple_url("https://podcasts.apple.com/us/podcast/x/id1503983105")
        assert False, "应抛 RuntimeError（而不是返回推荐流里的 feed）"
    except rap.PodcastNotFound:
        assert False, "接口故障不应被当成查无此节目"
    except RuntimeError as e:
        assert "schema:show" in str(e)
        assert "--feed-url" in str(e)
    print("✅ test_html_fallback_refuses_feed_url_without_schema_show")


def test_resolve_apple_url_does_not_fallback_when_not_found(monkeypatch):
    """查无此节目（resultCount=0）不触发 HTML 兜底：兜底也拿不到，纯浪费一次抓取。"""
    calls = []
    _patch_fetch(monkeypatch, api_payload=MOCK_LOOKUP_EMPTY,
                 html_payload=MOCK_HTML, calls=calls)
    try:
        rap.resolve_apple_url("https://podcasts.apple.com/us/podcast/x/id123456789")
        assert False, "应抛 PodcastNotFound"
    except rap.PodcastNotFound:
        pass
    # API 调了 1 次，HTML 兜底 0 次
    assert [c[0] for c in calls] == ["api"]
    print("✅ test_resolve_apple_url_does_not_fallback_when_not_found")


def test_resolve_apple_url_both_fail_is_actionable(monkeypatch):
    """两条路都失败 → RuntimeError 带两条原因 + 手动兜底建议，不抛裸异常。"""
    _patch_fetch(monkeypatch, api_error="连接超时", html_error="curl 返回空内容")
    try:
        rap.resolve_apple_url("https://podcasts.apple.com/us/podcast/x/id1200361736")
        assert False, "应抛 RuntimeError"
    except rap.PodcastNotFound:
        assert False, "接口故障不应被当成查无此节目"
    except RuntimeError as e:
        msg = str(e)
        assert "连接超时" in msg          # 保留 API 侧原因
        assert "curl 返回空内容" in msg    # 保留 HTML 侧原因
        assert "--feed-url" in msg         # 给出下一步
    print("✅ test_resolve_apple_url_both_fail_is_actionable")


def test_resolve_apple_url_still_rejects_non_apple():
    """兜底路径不能绕过入口校验（非 Apple 链接在联网前就拒）。"""
    try:
        rap.resolve_apple_url("https://example.com/podcast/123")
        assert False, "应抛 ValueError"
    except ValueError as e:
        assert "不是 Apple Podcasts 链接" in str(e)
    print("✅ test_resolve_apple_url_still_rejects_non_apple")


def test_podcast_not_found_is_runtimeerror_subclass():
    """PodcastNotFound 必须是 RuntimeError 子类。

    cmd_subscribe 只 except (RuntimeError, ValueError)，若不继承就会漏成裸栈回溯。
    """
    assert issubclass(rap.PodcastNotFound, RuntimeError)
    print("✅ test_podcast_not_found_is_runtimeerror_subclass")


if __name__ == "__main__":
    # 只列不依赖 pytest fixture 的用例（编排类用例要用 monkeypatch，走 pytest）。
    # 完整跑法：/usr/local/bin/python3 -m pytest tests/test_resolve_apple_podcast.py
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
        test_is_apple_url_exact_host,
        test_ensure_http_url_protocol_whitelist,
        test_build_lookup_url,
        test_parse_lookup_response_ok,
        test_parse_lookup_response_zero_result_is_friendly,
        test_parse_lookup_response_result_count_nonzero_but_empty_results,
        test_parse_lookup_response_missing_feed_url,
        test_parse_lookup_response_bad_json,
        test_parse_lookup_response_missing_name_is_none_not_empty_str,
        test_resolve_apple_url_still_rejects_non_apple,
        test_podcast_not_found_is_runtimeerror_subclass,
    ]

    for t in tests:
        t()

    print(f"\n{len(tests)} 个纯函数用例通过（编排类用例需 pytest）")
