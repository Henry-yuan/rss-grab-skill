#!/usr/bin/env python3
"""从 RSS item 的 guid / pubDate / title 派生稳定主键（8 位 hash）。

优先级：
  1. <guid>（最稳定，播客普遍有）
  2. pubDate + title 的 sha256 前 8 位（文章类常无 guid 时的兜底）
  3. title 的 sha256 前 8 位（极端兜底）
  4. 固定字符串 "no-key" 的 hash（啥都没有，告警但不崩）

用法：
  from derive_rss_id import derive_id
  h = derive_id(guid="6a741...", pub_date="Thu, 06 Aug 2026 ...", title="361-...")

  # 或命令行
  python3 derive_rss_id.py <guid>          # 仅 guid
"""
import hashlib
import sys


def guid_to_hash8(guid: str) -> str:
    """guid -> sha256 前 8 位（十六进制）。"""
    return hashlib.sha256(guid.encode("utf-8")).hexdigest()[:8]


def derive_id(guid: str | None = None, pub_date: str | None = None,
              title: str | None = None) -> str:
    """派生 8 位主键。优先 guid，无则 pubDate+title，再无则 title，最后占位。"""
    if guid:
        return guid_to_hash8(guid)
    if pub_date and title:
        raw = f"{pub_date}|{title}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    if title:
        return hashlib.sha256(title.encode("utf-8")).hexdigest()[:8]
    # 极端兜底：啥都没有
    print("WARN: RSS item 无 guid / pubDate / title，用占位主键", file=sys.stderr)
    return hashlib.sha256(b"no-key").hexdigest()[:8]


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: derive_rss_id.py <guid>", file=sys.stderr)
        sys.exit(2)
    print(derive_id(guid=sys.argv[1]))
