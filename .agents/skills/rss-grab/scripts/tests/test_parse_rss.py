#!/usr/bin/env python3
"""parse_rss 单测：用 fixture（5 期播客）验证解析。"""
import sys, pathlib
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))
import parse_rss as p

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "podcast_sample.xml"


def test_parse_feed_metadata():
    """feed 级元数据：标题、作者、描述、link。"""
    feed = p.parse_file(FIXTURE)
    assert feed["title"] == "独树不成林"
    assert feed.get("author") == "鬼鬼祟祟的树"
    assert feed.get("link", "").startswith("https://www.xiaoyuzhoufm.com/podcast/")
    assert "description" in feed and len(feed["description"]) > 0


def test_parse_items_count():
    """5 期 fixture。"""
    feed = p.parse_file(FIXTURE)
    assert len(feed["items"]) == 5


def test_parse_item_fields():
    """每期应有 title / guid / pub_date / duration / enclosure。"""
    feed = p.parse_file(FIXTURE)
    it = feed["items"][0]
    assert it["title"], "title 不应为空"
    assert it["guid"], "guid 不应为空"
    assert it["pub_date"], "pub_date 不应为空"
    assert it["duration"], "duration 不应为空"
    enc = it["enclosure"]
    assert enc["url"].startswith("https://"), "enclosure url 应是 https"
    assert enc["type"].startswith("audio/"), "enclosure type 应是 audio/*"
    assert enc["length"], "enclosure length 不应为空"


def test_parse_item_description_present():
    """description 存在（即使含 HTML）。"""
    feed = p.parse_file(FIXTURE)
    it = feed["items"][0]
    assert it.get("description"), "description 不应为空"


def test_is_podcast_audio_feed_true():
    """播客音频 feed：至少 1 期 enclosure type 是 audio/*。"""
    feed = p.parse_file(FIXTURE)
    assert p.is_podcast_audio_feed(feed) is True


def test_is_podcast_audio_feed_false_for_article():
    """文章类 RSS（无 enclosure 或非 audio）应返回 False。"""
    fake_feed = {
        "title": "文章feed",
        "items": [
            {"title": "x", "guid": None, "pub_date": "Mon, 01 Jan 2024 00:00:00 GMT",
             "duration": None, "enclosure": None, "description": "正文"},
        ],
    }
    assert p.is_podcast_audio_feed(fake_feed) is False


def test_parse_malformed_xml_raises():
    """畸形 XML 应抛 ParseError（不静默吞）。"""
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False, encoding="utf-8") as f:
        f.write("<rss><channel><title>畸形</title><item><title>没闭合</item></channel>")
        path = f.name
    try:
        import xml.etree.ElementTree as ET
        try:
            p.parse_file(path)
            assert False, "畸形 XML 应抛异常"
        except ET.ParseError:
            pass  # 期望
    finally:
        pathlib.Path(path).unlink(missing_ok=True)


def test_duration_seconds_normalization():
    """duration 字符串 -> 秒数。'00:35:04' -> 2104。"""
    assert p.duration_to_seconds("00:35:04") == 2104
    assert p.duration_to_seconds("35:04") == 2104
    assert p.duration_to_seconds("2104") == 2104
    assert p.duration_to_seconds(None) is None
    assert p.duration_to_seconds("") is None
