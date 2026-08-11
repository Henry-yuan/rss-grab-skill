#!/usr/bin/env python3
"""RSS 2.0 + itunes 命名空间解析（播客音频类）。

用标准库 xml.etree.ElementTree，不引入 feedparser。
容错策略：单字段缺失返回 None / 空串，不整体崩；畸形 XML 直接抛 ParseError。

用法：
  from parse_rss import parse_file, parse_text, is_podcast_audio_feed
  feed = parse_file("podcast.xml")
  if not is_podcast_audio_feed(feed):
      print("不是播客音频 feed")
"""
from __future__ import annotations
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

ITUNES_NS = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"


def _text(elem, tag: str) -> Optional[str]:
    """从 elem 找 tag 文本，找不到返回 None。"""
    if elem is None:
        return None
    child = elem.find(tag)
    return child.text if child is not None and child.text else None


def _findall_items(root: ET.Element) -> tuple[ET.Element, list[ET.Element]]:
    """返回 (channel, items)。"""
    channel = root.find("channel")
    if channel is None:
        raise ET.ParseError("RSS 缺 <channel>")
    return channel, channel.findall("item")


def _parse_item(item: ET.Element) -> dict:
    """解析单个 <item>。"""
    enc = item.find("enclosure")
    enclosure = None
    if enc is not None:
        enclosure = {
            "url": enc.get("url", ""),
            "type": enc.get("type", ""),
            "length": enc.get("length", ""),
        }
    return {
        "title": _text(item, "title") or "",
        "guid": _text(item, "guid"),
        "pub_date": _text(item, "pubDate"),
        "duration": _text(item, f"{ITUNES_NS}duration"),
        "duration_seconds": duration_to_seconds(_text(item, f"{ITUNES_NS}duration")),
        "enclosure": enclosure,
        "description": _text(item, "description") or "",
        "content_encoded": _text(item, f"{CONTENT_NS}encoded"),
        "link": _text(item, "link"),
        "itunes_image": (item.find(f"{ITUNES_NS}image").get("href")
                         if item.find(f"{ITUNES_NS}image") is not None else None),
    }


def parse_text(xml_text: str | bytes) -> dict:
    """解析 RSS XML 字符串（str 或 bytes）。

    接受 bytes 是为了正确处理非 UTF-8 编码的 feed：ET.fromstring 会读 XML
    声明里的 encoding 自动解码，比 subprocess text=True 按本机 locale 解码更稳。
    """
    root = ET.fromstring(xml_text)
    return _parse_root(root)


def parse_file(path: str | Path) -> dict:
    """解析 RSS XML 文件。畸形 XML 抛 ET.ParseError。"""
    tree = ET.parse(str(path))
    return _parse_root(tree.getroot())


def _parse_root(root: ET.Element) -> dict:
    channel, items = _findall_items(root)
    feed = {
        "title": _text(channel, "title") or "",
        "author": _text(channel, f"{ITUNES_NS}author"),
        "description": _text(channel, "description") or "",
        "link": _text(channel, "link"),
        "language": _text(channel, "language"),
        "itunes_image": (channel.find(f"{ITUNES_NS}image").get("href")
                         if channel.find(f"{ITUNES_NS}image") is not None else None),
        "items": [_parse_item(it) for it in items],
    }
    return feed


def is_podcast_audio_feed(feed: dict) -> bool:
    """判断是否播客音频 feed：至少 1 期 enclosure type 以 audio/ 开头。"""
    for it in feed.get("items", []):
        enc = it.get("enclosure")
        if enc and enc.get("type", "").startswith("audio/"):
            return True
    return False


def duration_to_seconds(dur: Optional[str]) -> Optional[int]:
    """'HH:MM:SS' / 'MM:SS' / 'SS' -> 秒。无法解析返回 None。"""
    if not dur:
        return None
    dur = dur.strip()
    if not dur:
        return None
    # 纯数字（秒）
    if dur.isdigit():
        return int(dur)
    parts = dur.split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return None
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 1:
        return parts[0]
    return None


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: parse_rss.py <xml_path_or_url>", file=sys.stderr)
        sys.exit(2)
    arg = sys.argv[1]
    if arg.startswith("http"):
        import urllib.request
        xml_text = urllib.request.urlopen(arg, timeout=30).read().decode("utf-8")
        feed = parse_text(xml_text)
    else:
        feed = parse_file(arg)
    print(f"节目: {feed['title']}")
    print(f"作者: {feed.get('author') or 'N/A'}")
    print(f"共 {len(feed['items'])} 期")
    print(f"播客音频 feed: {is_podcast_audio_feed(feed)}")
    for it in feed["items"][:3]:
        print(f"  - {it['title']} | {it['pub_date']} | {it['duration']} | {it['guid']}")
