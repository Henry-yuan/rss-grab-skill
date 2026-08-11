#!/usr/bin/env python3
"""Apple Podcasts 链接 -> RSS feed URL 反推。

从 Apple Podcasts 节目页面提取 RSS feedUrl + 节目元数据（name / description）。
反推逻辑（实测通过）：
  1. curl 抓 Apple Podcasts 页面 HTML（约 500KB）
  2. 正则取第一个 "feedUrl":"..." 匹配（本节目的；推荐区的 feedUrl 在后面）
  3. 从 <script id="schema:show" type="application/ld+json"> 提取 name / description
  4. 返回 {feed_url, title, description, apple_id}

用法：
  from resolve_apple_podcast import resolve_apple_url
  info = resolve_apple_url("https://podcasts.apple.com/cn/podcast/.../id1729552193")
  print(info["feed_url"])  # https://feed.xyzfm.space/68fyjknth9hj
"""
from __future__ import annotations

import json
import re
import subprocess


def extract_apple_id(apple_url: str) -> str:
    """从 Apple Podcasts URL 提取数字 ID。

    https://podcasts.apple.com/cn/podcast/十字路口crossing/id1729552193
    -> "1729552193"
    """
    m = re.search(r"id(\d+)", apple_url)
    if not m:
        raise ValueError(f"无法从 URL 提取 Apple Podcasts ID: {apple_url}")
    return m.group(1)


def parse_feed_url_from_html(html_text: str) -> str:
    """从 Apple Podcasts 页面 HTML 提取 feedUrl（取第一个匹配）。

    页面里可能有多个 feedUrl（推荐区），第一个是本节目的。
    """
    m = re.search(r'"feedUrl"\s*:\s*"([^"]+)"', html_text)
    if not m:
        raise RuntimeError("HTML 中未找到 feedUrl 字段")
    return m.group(1)


def parse_meta_from_html(html_text: str) -> dict:
    """从 Apple Podcasts 页面 HTML 提取节目名和简介。

    从 <script id="schema:show" type="application/ld+json"> 中解析
    JSON-LD，提取 name / description。
    找不到时返回 {"title": None, "description": None}。
    """
    m = re.search(
        r'<script[^>]*id=["\']?schema:show["\']?[^>]*>(.*?)</script>',
        html_text,
        re.DOTALL,
    )
    if not m:
        return {"title": None, "description": None}
    try:
        data = json.loads(m.group(1).strip())
    except json.JSONDecodeError:
        return {"title": None, "description": None}
    return {
        "title": data.get("name"),
        "description": data.get("description"),
    }


def _fetch_html(url: str) -> str:
    """curl 抓 Apple Podcasts 页面 HTML（返回 str）。

    与 fetch_rss_feed.fetch_xml 风格一致：subprocess 调 curl，
    -f 让 HTTP 4xx/5xx 走清晰报错分支，-L 跟随重定向。
    """
    r = subprocess.run(
        ["curl", "-sfSL", "--retry", "3", "--max-time", "60", url],
        capture_output=True,
        timeout=90,
    )
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"curl 失败 (exit={r.returncode}): {err[:200]}")
    if not r.stdout.strip():
        raise RuntimeError("curl 返回空内容")
    return r.stdout.decode("utf-8", errors="replace")


def resolve_apple_url(apple_url: str) -> dict:
    """从 Apple Podcasts 节目链接反推 RSS feed URL + 节目元数据。

    参数：
      apple_url: Apple Podcasts 节目链接
        (如 https://podcasts.apple.com/cn/podcast/.../id1729552193)

    返回：
      {"feed_url": str, "title": str|None, "description": str|None, "apple_id": str}

    异常：
      ValueError: apple_url 不是 Apple Podcasts 链接
      RuntimeError: 抓取或解析失败
    """
    if "podcasts.apple.com" not in apple_url:
        raise ValueError(
            f"不是 Apple Podcasts 链接（需包含 podcasts.apple.com）: {apple_url}"
        )

    apple_id = extract_apple_id(apple_url)
    html_text = _fetch_html(apple_url)
    feed_url = parse_feed_url_from_html(html_text)
    meta = parse_meta_from_html(html_text)

    return {
        "feed_url": feed_url,
        "title": meta["title"],
        "description": meta["description"],
        "apple_id": apple_id,
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print(
            "usage: resolve_apple_podcast.py <apple_podcasts_url>",
            file=sys.stderr,
        )
        sys.exit(2)

    result = resolve_apple_url(sys.argv[1])
    print(f"feed_url:    {result['feed_url']}")
    print(f"title:       {result['title']}")
    print(f"apple_id:    {result['apple_id']}")
    desc = result.get("description") or ""
    if len(desc) > 100:
        desc = desc[:100] + "..."
    print(f"description: {desc}")
