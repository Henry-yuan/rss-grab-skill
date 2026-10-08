#!/usr/bin/env python3
"""Apple Podcasts 链接 -> RSS feed URL 反推。

## 主路径：iTunes Lookup API

Apple 已把节目页改成客户端渲染的播放器壳子（2026-10-09 实测 75 万字符的页面里
`<script id="schema:show">` 与 `"feedUrl"` 各 0 处，`<title>` 恒为
「Apple 播客（CN）- 网页播放器」，唯一的 ld+json 是 Apple **公司**的组织信息）。
节目数据改成 JS 运行时请求，HTML 里取不到了。

改用 Apple 官方的 iTunes Lookup 接口，它按节目 id 直接返回 feedUrl 与节目名：

    https://itunes.apple.com/lookup?id=<podcast_id>&media=podcast

  · 命中：resultCount=1，results[0].feedUrl / .collectionName / .artistName
    （实测 collectionDescription **并不返回**，简介取不到时由 RSS feed 侧补，
      不影响订阅 —— 别把它当成必需字段）
  · 没命中：**resultCount=0 且不报错**（链接无效、已下架、或只是单集链接 id?i=...）

## 兜底：页面 HTML 解析

保留原来的 HTML 路径（`_fetch_html` + 两个 `parse_*_from_html`）作为兜底，
应对接口不可达 / 改版。若两条路都拿不到，抛的RuntimeError 带**可操作的中文提示**
（说明多半是链接 id 用错或节目已下架），而不是裸 traceback。

用法：
  from resolve_apple_podcast import resolve_apple_url
  info = resolve_apple_url("https://podcasts.apple.com/cn/podcast/.../id1729552193")
  print(info["feed_url"])  # https://feed.xyzfm.space/68fyjknth9hj
"""
from __future__ import annotations

import json
import re
import subprocess
from urllib.parse import urlparse

ITUNES_LOOKUP_API = "https://itunes.apple.com/lookup"


def is_apple_podcasts_url(url: str) -> bool:
    """URL 的 host 是否精确为 podcasts.apple.com。

    子串包含（"podcasts.apple.com" in url）可被子域伪装
    （podcasts.apple.com.evil.com）或参数绕过，这里按 host 精确匹配。
    """
    try:
        host = urlparse(url).hostname
    except ValueError:
        return False
    return host == "podcasts.apple.com"


def ensure_http_url(url: str, what: str = "URL") -> str:
    """校验 URL 协议为 http/https，否则抛 ValueError。

    反推出的 feed_url 会写入订阅表被定时任务长期反复拉取，
    非 http(s) 协议（file:// 等）必须在入口拦住。
    """
    scheme = urlparse(url).scheme
    if scheme not in ("http", "https"):
        raise ValueError(f"{what} 协议必须是 http/https: {url}")
    return url


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


def _fetch_text(url: str, what: str) -> str:
    """curl 抓一个 URL 返回正文文本（返回 str）。

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
        raise RuntimeError(f"curl 抓取{what}失败 (exit={r.returncode}): {err[:200]}")
    if not r.stdout.strip():
        raise RuntimeError(f"curl 抓取{what}返回空内容")
    return r.stdout.decode("utf-8", errors="replace")


def _fetch_html(url: str) -> str:
    """curl 抓 Apple Podcasts 页面 HTML（兜底路径用）。"""
    return _fetch_text(url, "节目页 HTML")


# ---------------------------------------------------------------------------
# 主路径：iTunes Lookup API
# ---------------------------------------------------------------------------

def build_lookup_url(apple_id: str) -> str:
    """拼 iTunes Lookup 请求 URL。

    media=podcast 而不是 entity=podcast：lookup 的 id 参数要的是 **collection id**
    （节目 id），entity 只用于在其下再展开单集，两者都用反而会多返回一堆 track。
    """
    return f"{ITUNES_LOOKUP_API}?id={apple_id}&media=podcast"


class PodcastNotFound(RuntimeError):
    """iTunes Lookup 明确回答「没有这个节目」（resultCount=0）。

    单独一个类型，是为了让调用方能区分「查无此节目」（重试无意义，提示换链接）
    与「网络/接口故障」（可能该重试或该走兜底）。
    """


def parse_lookup_response(payload: str, apple_id: str) -> dict:
    """解析 iTunes Lookup 返回的 JSON，产出与 HTML 路径同构的结果 dict。

    抛 PodcastNotFound 的两种情形（都不抛裸异常）：
      · resultCount=0 / results 为空 —— 这个 id 在 Apple 上没有节目
      · 命中了但 results[0].feedUrl 为空 —— 存在条目却拿不到 feed，无从反推
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"iTunes Lookup 返回的不是合法 JSON: {e}")

    results = data.get("results") or []
    if not data.get("resultCount") or not results:
        raise PodcastNotFound(
            f"iTunes Lookup 未收录这个节目（id={apple_id}）。"
            "链接里的 id 可能不是节目 id（例如只取了单集链接的 ?i= 参数），"
            "或该节目已下架。可在浏览器打开链接确认页面存在，"
            "或用 --feed-url <manual> 直接指定 RSS 地址。"
        )

    item = results[0]
    feed_url = (item.get("feedUrl") or "").strip()
    if not feed_url:
        raise PodcastNotFound(
            f"iTunes Lookup 命中了 id={apple_id}，但返回里没有 feedUrl"
            f"（节目名：{item.get('collectionName') or '未知'}）。"
            "可改用 --feed-url <manual> 手动指定 RSS 地址。"
        )

    return {
        "feed_url": feed_url,
        # 注意：collectionName 缺失时给 None 而不是 ""。调用方必须自己兜底
        # （见 fetch_rss_feed.cmd_subscribe），别指望这里编造一个节目名。
        "title": item.get("collectionName") or None,
        "description": item.get("collectionDescription") or None,
        "apple_id": apple_id,
        "source": "itunes_api",
    }


def lookup_via_itunes(apple_id: str) -> dict:
    """走 iTunes Lookup API 反推（主路径）。

    网络/接口故障抛 RuntimeError；查无此节目抛 PodcastNotFound（RuntimeError 子类）。
    """
    payload = _fetch_text(build_lookup_url(apple_id), "iTunes Lookup 接口")
    return parse_lookup_response(payload, apple_id)


def resolve_apple_url(apple_url: str) -> dict:
    """从 Apple Podcasts 节目链接反推 RSS feed URL + 节目元数据。

    顺序：iTunes Lookup API 优先 → 页面 HTML 解析兜底。
    PodcastNotFound（确实没这个节目）不触发兜底 —— 兜底也拿不到，白跑一次；
    网络类故障才降级。

    参数：
      apple_url: Apple Podcasts 节目链接
        (如 https://podcasts.apple.com/cn/podcast/.../id1729552193)

    返回：
      {"feed_url": str, "title": str|None, "description": str|None,
       "apple_id": str, "source": "itunes_api"|"html"}

    异常：
      ValueError: apple_url 不是 Apple Podcasts 链接
      RuntimeError: 两条路都拿不到 feed_url（消息是可操作的中文提示，非裸异常）
    """
    if not is_apple_podcasts_url(apple_url):
        raise ValueError(
            f"不是 Apple Podcasts 链接（需 podcasts.apple.com 域名）: {apple_url}"
        )

    apple_id = extract_apple_id(apple_url)

    # 主路径：iTunes Lookup API
    try:
        return lookup_via_itunes(apple_id)
    except PodcastNotFound:
        raise                      # 查无此节目是确定答案，兜底也救不回来
    except RuntimeError as e:
        api_err = str(e)

    # 兜底：解析页面 HTML（接口故障 / 未来 Apple 改版时可仍走通）
    try:
        html_text = _fetch_html(apple_url)
        meta = parse_meta_from_html(html_text)
        # 必须先确认 schema:show 还在（= feedUrl 有归属），再取 feedUrl。
        # 现在的 Apple 页面里还留着几十个 feedUrl，但全部来自「猜你喜欢」推荐流，
        # schema:show 已随改版消失 —— 此时「取第一个 feedUrl」会拿到**别的节目**
        # （2026-10-09 实测：id1503983105 页面第一个 feedUrl 指向喜马拉雅上的
        # 另一档节目，而 API 给的正解是 feeds.simplecast.com/bFcadzb0）。
        # 宁可明确失败让人用 --feed-url，也不悄悄订阅错节目。
        if not meta.get("title"):
            raise RuntimeError(
                "页面已无 schema:show，取到的 feedUrl 无法确认属于本节目"
                "（当前只存在于推荐流里）"
            )
        feed_url = parse_feed_url_from_html(html_text)
        ensure_http_url(feed_url, "反推出的 feedUrl")
    except (RuntimeError, ValueError) as e:
        raise RuntimeError(
            f"无法反推 RSS：iTunes Lookup 接口不可用（{api_err}），"
            f"页面 HTML 兜底也失败（{e}）。\n"
            "   请确认链接能在浏览器正常打开该节目；"
            "若接口临时故障，可稍后重试，或用 --feed-url <manual> 手动指定 RSS 地址。"
        ) from e

    return {
        "feed_url": feed_url,
        # 兜底路径走到这里必然有 title（上面已把 None 的情形转成异常）
        "title": meta["title"],
        "description": meta.get("description") or None,
        "apple_id": apple_id,
        "source": "html",
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
