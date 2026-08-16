#!/usr/bin/env python3
"""rss-grab 阶段 1 主入口：抓 RSS XML -> 解析期数 -> 下载音频 -> 落 info.json。

用法：
  python3 fetch_rss_feed.py <rss_url> [--max N] [--out-dir rss] [--force]

参数：
  rss_url          RSS feed URL（http(s)://...xml 或 feed.xxx 域名）
  --max N          最多下载 N 期（默认 5；先跑通少量）
  --out-dir DIR    输出根目录（默认 rss，产物到 rss/raw/，音频到 /tmp/rss-grab-audio/）
  --force          跳过 dedup，强制重新下载
  --cleanup-audio  音频转写完成后自动删除 /tmp/rss-grab-audio/ 下的音频（默认保留）

行为：
  1. curl 抓 RSS XML 全文
  2. parse_rss 解析 feed + items
  3. is_podcast_audio_feed 判断；非播客音频 feed -> 提示并退出
  4. 对前 N 期：
     - derive_rss_id 派生 guid_hash8
     - dedup_check（除非 --force）；命中 -> 跳过
     - yt-dlp 下载音频到 audio/<sanitized_title>-<hash8>.<ext>
     - 写 raw/<sanitized_title>-<hash8>.info.json
     - sleep 5 秒（CDN 礼仪）
  5. 打印汇总

依赖：yt-dlp、curl、Python 3.12+
"""
from __future__ import annotations
import html
import json
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import parse_rss
import derive_rss_id
import dedup_check


DEFAULT_OUT_DIR = Path("rss")
DEFAULT_MAX = 5
SLEEP_BETWEEN = 5  # 秒（方案 §4.6 / RSS 指南 §7：CDN 礼仪）

# 音频临时目录（/tmp，转写后可选清理；转写完成后音频不保留）
# 音频是转写唯一原料，源站 url 存在 info.json（入 git），需要时可重下
TMP_AUDIO_DIR = Path("/tmp/rss-grab-audio")

# sanitized 规则：保留 CJK + 字母数字，其他替换为 -
MAX_TITLE_LEN = 60
SAFE_RE = re.compile(r'[^0-9A-Za-z\u4e00-\u9fff]+')


def sanitize_title(title: str) -> str:
    """标题 -> 文件名安全串（保留 CJK + 字母数字，其他替换为 -）。"""
    s = SAFE_RE.sub('-', title)
    s = re.sub(r'-+', '-', s).strip('-')
    if len(s) > MAX_TITLE_LEN:
        s = s[:MAX_TITLE_LEN].rstrip('-')
    return s


def ext_from_enclosure(enclosure_type: str) -> str:
    """enclosure type -> 扩展名。"""
    m = {
        "audio/mp4": "m4a",
        "audio/mpeg": "mp3",
        "audio/x-m4a": "m4a",
        "audio/ogg": "ogg",
        "audio/flac": "flac",
    }
    return m.get(enclosure_type, "m4a")  # 默认 m4a（播客最常见）


def fetch_xml(url: str) -> bytes:
    """curl 抓 RSS XML 全文（返回 bytes，交由 ET.fromstring 按 XML 声明解码）。

    -f：HTTP 4xx/5xx 时 curl 返回非零退出码，走清晰报错分支，避免错误页正文
    被当 XML 解析后抛裸 ParseError。
    """
    r = subprocess.run(
        ["curl", "-sfSL", "--retry", "3", "--max-time", "60", url],
        capture_output=True, timeout=90,
    )
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"curl 失败 (exit={r.returncode}): {err[:200]}")
    if not r.stdout.strip():
        raise RuntimeError("curl 返回空内容")
    return r.stdout


def download_audio(audio_url: str, save_path: Path,
                   force: bool = False) -> tuple[bool, str]:
    """yt-dlp 下载音频。返回 (ok, err_or_empty)。

    force=True 时传 --force-overwrites，避免 yt-dlp 默认 --no-force-overwrites
    导致目标文件已存在时直接跳过返回 0（文件损坏/零字节无法用 --force 修复）。
    """
    cmd = [
        "yt-dlp",
        "-o", str(save_path),
        "--no-progress",
        "--no-warnings",
    ]
    if force:
        cmd.append("--force-overwrites")
    cmd.append(audio_url)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode == 0:
            return True, ""
        return False, (r.stderr.strip() or r.stdout.strip())[:300]
    except subprocess.TimeoutExpired:
        return False, "yt-dlp 超时（30 分钟）"


def cleanup_audio_files(audio_paths: list[Path]) -> int:
    """删除本次下载的音频文件（白名单删除，返回删除数）。

    /tmp/rss-grab-audio 是共享临时目录：只删本次下载清单内的文件，
    不 iterdir 全删（防误删其他会话 / 手动放入的文件）。
    """
    n = 0
    for p in audio_paths:
        try:
            if p.is_file():
                p.unlink()
                n += 1
        except OSError:
            continue
    return n


def build_info_json(feed: dict, item: dict, guid_hash8: str,
                    audio_path: Path | None) -> dict:
    """组装 info.json 内容。"""
    return {
        "feed": {
            "title": feed["title"],
            "author": feed.get("author"),
            "link": feed.get("link"),
            "description": feed.get("description", ""),
            "language": feed.get("language"),
            "image": feed.get("itunes_image"),
        },
        "item": {
            "title": item["title"],
            "guid": item["guid"],
            "guid_hash8": guid_hash8,
            "pub_date": item["pub_date"],
            "duration": item["duration"],
            "duration_seconds": item["duration_seconds"],
            "enclosure": item["enclosure"],
            "link": item.get("link"),
            "image": item.get("itunes_image"),
        },
        "local": {
            "audio_path": str(audio_path) if audio_path else None,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        },
    }


def parse_pick_spec(spec: str, total: int) -> list[int]:
    """解析 --pick 选择表达式，返回要下载的 item 下标列表。

    支持：
      "3"          -> [2]                      单个序号（从 1 开始）
      "1,3,5"      -> [0,2,4]                  逗号分隔
      "5-8"        -> [4,5,6,7]                范围（含两端）
      "1,3,5-8"    -> [0,2,4,5,6,7]            混合
      "last"       -> 最近 5 期（等价 --max 5）
      "last:3"     -> 最近 3 期
      "abcdef12"   -> []                       8 位 guid hash（由 guid_to_idx 另行匹配）

    非法/超范围输入忽略（不抛异常）。
    """
    spec = spec.strip()
    if not spec:
        return []
    if spec == "last":
        return list(range(max(0, total - DEFAULT_MAX), total))
    if spec.startswith("last:"):
        n = spec[5:].strip()
        if n.isdigit():
            return list(range(max(0, total - int(n)), total))
        return []
    result: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if a.isdigit() and b.isdigit():
                lo, hi = int(a), int(b)
                if lo < 1 or hi > total or lo > hi:
                    continue
                result.extend(range(lo - 1, hi))
        elif part.isdigit():
            n = int(part)
            if 1 <= n <= total:
                result.append(n - 1)
        # 8 位 hex guid hash 或非法输入：忽略（guid 另行匹配）
    # 去重保序
    seen = set()
    return [i for i in result if not (i in seen or seen.add(i))]


def guid_to_idx(items: list[dict], guid_hash8: str) -> int | None:
    """按 guid_hash8 精确匹配 item 下标（用于 --pick 传 guid）。"""
    for i, it in enumerate(items):
        h = derive_rss_id.derive_id(
            guid=it["guid"], pub_date=it["pub_date"], title=it["title"])
        if h == guid_hash8:
            return i
    return None


def list_items(items: list[dict]) -> None:
    """打印全部分期列表（--list 用），供用户确认选择。"""
    print(f"\n共 {len(items)} 期：")
    print(f"{'序号':>4} | {'日期':<20} | {'时长':<10} | {'guid_hash8':<10} | 标题")
    print("-" * 80)
    for i, it in enumerate(items, 1):
        dur = it.get("duration") or "?"
        pub = (it.get("pub_date") or "")[:16]
        h = derive_rss_id.derive_id(
            guid=it["guid"], pub_date=it["pub_date"], title=it["title"])
        title = it["title"][:50]
        print(f"{i:>4} | {pub:<20} | {dur:<10} | {h:<10} | {title}")


# ---------------------------------------------------------------------------
# 订阅模式命令（--subscribe / --fetch-updates / --sync-pick / --pick-subscribe
#               / --retry-summary / --unsubscribe）
# ---------------------------------------------------------------------------

def cmd_subscribe(apple_url: str, feed_url_override: str = "") -> None:
    """订阅新源：Apple Podcasts 链接 -> 反推 feed URL -> 写订阅表 + 初始化状态文件。"""
    import subscribe_manager
    import resolve_apple_podcast

    feed_url = ""
    meta = {}

    # 优先用反推，失败时用 --feed-url 手动指定
    if resolve_apple_podcast.is_apple_podcasts_url(apple_url):
        try:
            result = resolve_apple_podcast.resolve_apple_url(apple_url)
            feed_url = result["feed_url"]
            meta = {
                "title": result.get("title", ""),
                "description": result.get("description", ""),
            }
            print(f"✅ Apple Podcasts 反推成功：{result.get('title', '')}")
            print(f"   feed_url: {feed_url}")
        except (RuntimeError, ValueError) as e:
            if not feed_url_override:
                print(f"❌ Apple Podcasts 反推失败: {e}")
                print("   可用 --feed-url <manual> 手动指定 RSS feed URL")
                sys.exit(1)
            print(f"⚠️  Apple 反推失败({e})，改用手动 --feed-url")

    if feed_url_override:
        feed_url = feed_url_override
        print(f"   使用手动 feed_url: {feed_url}")

    if not feed_url:
        print("❌ 无 feed_url（Apple 反推失败且未传 --feed-url），退出")
        sys.exit(1)

    # 拉 RSS XML 验证 + 取 feed 级元数据
    print(f"\n=== 验证 RSS feed: {feed_url} ===")
    try:
        xml_bytes = fetch_xml(feed_url)
        feed = parse_rss.parse_text(xml_bytes)
    except (RuntimeError, ET.ParseError, ValueError) as e:
        print(f"❌ RSS 解析失败: {e}")
        sys.exit(1)

    if not parse_rss.is_podcast_audio_feed(feed):
        print("❌ 该 RSS 不是播客音频 feed（无 audio/* enclosure），不订阅")
        sys.exit(1)

    # 补全 meta（反推可能只拿到 title/description，RSS XML 有完整元数据）
    meta.setdefault("title", feed["title"])
    meta["author"] = feed.get("author", "")
    meta["language"] = feed.get("language", "")
    meta["link"] = feed.get("link", "")
    # 优先 RSS 完整简介（Apple 反推的可能只有摘要段落）；RSS 没有才用 Apple 的
    meta["description"] = feed.get("description", "") or meta.get("description", "")

    source_name = meta["title"]
    entry = subscribe_manager.add_subscription(feed_url, source_name, meta)
    print(f"\n✅ 订阅成功：{source_name}")
    print(f"   状态文件: {entry.get('state_file', '')}")
    print(f"   共 {len(feed['items'])} 期（跑 --fetch-updates 拉取内容）")


def cmd_fetch_updates() -> None:
    """遍历订阅表，每个源拉增量 -> AI 摘要 -> 写状态文件待确认区。

    处理两类：
      1. 新增期（guid 不在状态文件）-> 拉取 + AI 摘要 + 追加待确认区
      2. 已有期但缺 AI 摘要（如手动塞入/历史遗留）-> 补跑摘要（不重复追加）
    """
    import subscribe_manager
    import preview_podcast

    subs = subscribe_manager.load_subscriptions()
    if not subs["sources"]:
        print("📭 订阅表为空，先跑 --subscribe <apple_url> 订阅源")
        return

    print(f"=== 拉取增量（{len(subs['sources'])} 个源）===")
    total_new = 0
    for src in subs["sources"]:
        name = src["name"]
        feed_url = src["feed_url"]
        state_file = subscribe_manager.PROJECT_ROOT / src["state_file"]

        try:
            print(f"\n--- {name} ---")
            xml_bytes = fetch_xml(feed_url)
            feed = parse_rss.parse_text(xml_bytes)

            # 加载状态文件，收集已知 guid
            state = subscribe_manager.load_state(state_file)
            known = subscribe_manager.collect_known_guids(state)
            new_items = subscribe_manager.find_new_items(feed["items"], known)

            # 待补摘要的已有期：待确认区里缺"一句话概括"的
            missing = [it for it in state["pending"]
                       if not it.get("fields", {}).get("一句话概括")
                       and it.get("guid")]
            missing_by_guid = {it["guid"]: it for it in missing}

            if new_items:
                print(f"  新增 {len(new_items)} 期，跑 AI 摘要...")
            if missing:
                print(f"  补摘要 {len(missing)} 期（已有但缺 AI 摘要）")

            if not new_items and not missing:
                print(f"  无新增（已知 {len(known)} 期）")
                continue

            # AI 摘要（复用 preview_podcast，每期完成回调增量写状态文件）
            # checkpoint 统一放 rss/订阅/checkpoint/ 子目录
            checkpoint = (state_file.parent / "checkpoint" /
                          state_file.with_suffix(".checkpoint.json").name)
            pending_count = [0]  # 闭包计数：已增量写入的期数

            def on_done(item: dict, summary: str) -> None:
                """新增期：摘要完成后追加到待确认区。"""
                nonlocal state
                _append_pending_item(state_file, state, item, summary)
                pending_count[0] += 1
                if pending_count[0] % 10 == 0:
                    state["frontmatter"]["last_fetched"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                    subscribe_manager.save_state(state_file, state)
                    print(f"  💾 已增量写入 {pending_count[0]} 期")

            # 1) 新增期 -> 摘要 + 追加
            if new_items:
                preview_podcast.summarize_items(
                    feed, new_items, checkpoint, resume=True, on_item_done=on_done)

            # 2) 已有期补摘要 -> 摘要后回填 fields（不追加新条目）
            if missing:
                feed_by_guid = {it["guid"]: it for it in feed["items"]}
                need_items = []
                for guid, st_item in missing_by_guid.items():
                    fi = feed_by_guid.get(guid)
                    if fi:
                        fi = dict(fi)  # 复制，不污染 feed items
                        fi["_st_item"] = st_item
                        need_items.append(fi)

                def on_backfill(item: dict, summary: str) -> None:
                    """补摘要：回填到已有期的 fields，不追加。每期落盘（几百期补摘要需每期可见）。"""
                    nonlocal state
                    st_item = item.get("_st_item")
                    if not st_item or not summary:
                        return
                    parsed = subscribe_manager._parse_summary_segments(summary)
                    fields = st_item.setdefault("fields", {})
                    for k in ("一句话概括", "内容概览", "值得关注"):
                        if k in parsed:
                            fields[k] = parsed[k]
                    pending_count[0] += 1
                    subscribe_manager.save_state(state_file, state)
                    if pending_count[0] % 10 == 0:
                        print(f"  💾 已补摘要 {pending_count[0]} 期")

                if need_items:
                    preview_podcast.summarize_items(
                        feed, need_items, checkpoint, resume=True, on_item_done=on_backfill)
                # 补摘要用原始简介（item.description 是原始简介），
                # preview_podcast 会把摘要写回 item["description"]，但补摘要回填的是 fields

            # 最终写回：补齐提示行 + 最后一批
            state["frontmatter"]["last_fetched"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            state.setdefault("hint", {})["pending"] = \
                f"{datetime.now().strftime('%Y-%m-%d %H:%M')} | 新增 {len(new_items)} 期请审阅"
            subscribe_manager.save_state(state_file, state)
            total_new += len(new_items)
            if new_items or missing:
                print(f"  ✅ 新增 {len(new_items)} 期 + 补摘要 {len(missing)} 期完成")

        except Exception as e:
            print(f"  ❌ 拉取失败: {e}")
            # 状态文件顶部插告警（feed 失效）
            _alert_feed_failure(state_file, str(e)[:200])

    print(f"\n=== 完成，共新增 {total_new} 期 ===")



def cmd_sync_pick(state_path: Path) -> None:
    """读状态文件 checkbox，按 [x]/[~]/[done] 重新归区。"""
    import subscribe_manager

    state = subscribe_manager.load_state(state_path)

    # 扫描全文件所有区的 checkbox，按当前值重新归区
    all_items = []
    for zone in ("pending", "confirmed", "done"):
        all_items.extend(state[zone])
    state["pending"] = []
    state["confirmed"] = []
    state["done"] = []

    for item in all_items:
        cb = item.get("checkbox", "[ ]")
        if cb == "[ ]":
            state["pending"].append(item)
        elif cb in ("[x]", "[~]"):
            state["confirmed"].append(item)
        elif cb == "[done]":
            state["done"].append(item)
        else:
            state["pending"].append(item)

    # 更新"待确认"区提示行
    n_pending = len(state["pending"])
    n_reviewed = len(state["confirmed"]) + len(state["done"])
    if n_pending == 0:
        hint = f"{datetime.now().strftime('%Y-%m-%d %H:%M')} | 全部审完"
    else:
        hint = f"{datetime.now().strftime('%Y-%m-%d %H:%M')} | 已审 {n_reviewed} 期,剩 {n_pending} 期待确认"
    state.setdefault("hint", {})["pending"] = hint

    subscribe_manager.save_state(state_path, state)
    print(f"✅ 同步完成：待确认 {n_pending} / 确认 {len(state['confirmed'])} / 已转化 {len(state['done'])}")


def cmd_pick_subscribe(state_path: Path, args) -> None:
    """从订阅状态文件读 [x] 期的 guid，下载音频 -> 转写 -> 做笔记。"""
    import subscribe_manager

    state = subscribe_manager.load_state(state_path)
    feed_url = state["frontmatter"].get("feed_url", "")
    if not feed_url:
        print("❌ 状态文件缺 feed_url，无法拉 RSS feed")
        sys.exit(1)

    # 收集所有 [x] 期的 guid（全文件扫）
    target_guids = set()
    for zone in ("pending", "confirmed"):
        for item in state[zone]:
            if item.get("checkbox") == "[x]" and item.get("guid"):
                target_guids.add(item["guid"])

    if not target_guids:
        print("⚠️  状态文件里没有 [x] 的期数，退出")
        return

    print(f"=== 抓取 {len(target_guids)} 期 ===")
    xml_bytes = fetch_xml(feed_url)
    feed = parse_rss.parse_text(xml_bytes)

    # 按 guid 匹配 items
    items = [it for it in feed["items"] if it.get("guid") in target_guids]
    print(f"  匹配到 {len(items)} 期")

    # 复用现有下载流程
    out_dir = args.out_dir
    raw_dir = out_dir / "raw"
    # 音频存 /tmp（临时，转写后可选清理；raw/transcripts 入 git 防丢）
    audio_dir = TMP_AUDIO_DIR
    raw_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)

    n_ok = n_skip = n_fail = 0
    new_raw_paths = []
    downloaded_audio: list[Path] = []  # 本次下载成功清单（--cleanup-audio 白名单）
    for i, item in enumerate(items, 1):
        title = item["title"]
        guid_hash8 = derive_rss_id.derive_id(
            guid=item["guid"], pub_date=item["pub_date"], title=title)
        safe = sanitize_title(title)
        print(f"\n--- [{i}/{len(items)}] {title} ---")

        if not args.force:
            exists, existing = dedup_check.check_rss_raw_exists(raw_dir, guid_hash8)
            if exists:
                print(f"  ⏭️  已存在 info.json：{existing}")
                n_skip += 1
                continue

        enc = item["enclosure"]
        if not enc or not enc.get("url"):
            print("  ❌ 无 enclosure url，跳过")
            n_fail += 1
            continue
        ext = ext_from_enclosure(enc.get("type", ""))
        audio_path = audio_dir / f"{safe}-{guid_hash8}.{ext}"
        ok, err = download_audio(enc["url"], audio_path, force=args.force)
        if not ok:
            print(f"  ❌ 下载失败: {err[:200]}")
            n_fail += 1
            continue
        downloaded_audio.append(audio_path)

        info_path = raw_dir / f"{safe}-{guid_hash8}.info.json"
        info = build_info_json(feed, item, guid_hash8, audio_path)
        info_path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        new_raw_paths.append(info_path)
        n_ok += 1
        time.sleep(SLEEP_BETWEEN)

    print(f"\n=== 下载完成：成功 {n_ok} / 跳过 {n_skip} / 失败 {n_fail} ===")
    if new_raw_paths and args.transcribe:
        print("\n=== ASR 转写 ===")
        _transcribe_items(new_raw_paths)

    # 转写完成后清理 /tmp 音频（可选，--cleanup-audio；只删本次下载的）
    if args.cleanup_audio and downloaded_audio:
        n_del = cleanup_audio_files(downloaded_audio)
        print(f"🧹 --cleanup-audio：已清理 {n_del} 个 /tmp 音频")


def cmd_retry_summary(guid: str) -> None:
    """单期重跑 AI 摘要，结果写回状态文件。"""
    import subscribe_manager
    import preview_podcast

    subs = subscribe_manager.load_subscriptions()
    for src in subs["sources"]:
        state_path = subscribe_manager.PROJECT_ROOT / src["state_file"]
        state = subscribe_manager.load_state(state_path)
        for zone in ("pending", "confirmed"):
            for item in state[zone]:
                if item.get("guid") == guid:
                    print(f"找到 {guid}（{src['name']} - {item['title']}），重跑摘要...")
                    # 单期重跑
                    feed_url = state["frontmatter"]["feed_url"]
                    xml_bytes = fetch_xml(feed_url)
                    feed = parse_rss.parse_text(xml_bytes)
                    target = next((it for it in feed["items"] if it.get("guid") == guid), None)
                    if not target:
                        print("❌ RSS feed 里找不到该 guid")
                        return
                    client, model = preview_podcast._get_client()
                    summary = preview_podcast.summarize_one(client, model, target, "重试")
                    ok = subscribe_manager.update_summary(state_path, guid, summary)
                    if ok:
                        print("✅ 摘要已更新")
                    return
    print(f"❌ 所有订阅源里都找不到 guid: {guid}")


def cmd_unsubscribe(source_name: str) -> None:
    """退订：从订阅表删除 + 状态文件标记已退订。"""
    import subscribe_manager
    ok = subscribe_manager.remove_subscription(source_name)
    if ok:
        print(f"✅ 已退订：{source_name}（状态文件保留，标记已退订）")
    else:
        print(f"❌ 订阅表里找不到：{source_name}")


# ---- 订阅模式辅助函数 ----

def format_pub_date(pub_date: str) -> str:
    """RSS pubDate -> 'YYYY-MM-DD HH:MM'（'Sun, 26 Jul 2026 16:00:00 GMT' -> '2026-07-26 16:00'）。

    解析失败返回原串（不崩）。
    """
    if not pub_date:
        return ""
    # RFC 2822 格式：Sun, 26 Jul 2026 16:00:00 GMT（含 +0800 变体）
    m = re.search(
        r"\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})\s+(\d{2}):(\d{2})",
        pub_date,
    )
    if not m:
        return pub_date
    months = {"Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05", "Jun": "06",
              "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12"}
    day, mon, year, hour, minute = m.group(1).zfill(2), m.group(2), m.group(3), m.group(4), m.group(5)
    return f"{year}-{months[mon]}-{day} {hour}:{minute}"


def _append_pending_item(state_path: Path, state: dict, item: dict, summary: str) -> None:
    """把单期 item 追加到 state 的"待确认"区。"""
    import subscribe_manager
    # 解析 AI 摘要三段（preview_podcast 已把摘要写入 item["description"]）
    fields = {}
    if summary:
        parsed = subscribe_manager._parse_summary_segments(summary)
        fields.update(parsed)
    # 补基础字段（发布日期转 YYYY-MM-DD HH:MM）
    fields.setdefault("发布日期", format_pub_date(item.get("pub_date", "")))
    fields.setdefault("时长", item.get("duration", ""))
    fields.setdefault("链接", item.get("link", ""))

    seq = len(state["pending"]) + len(state["confirmed"]) + len(state["done"]) + 1
    # 无 guid 时持久化去重 key（link 降级），供 collect_known_guids 去重
    dedup_key = item.get("_dedup_key", "") or item.get("guid", "")
    new_item = {
        "checkbox": "[ ]",
        "seq": seq,
        "title": item.get("title", ""),
        "guid": item.get("guid", "") or dedup_key,
        "fields": fields,
        "note_path": "",
    }
    state["pending"].append(new_item)


def _alert_feed_failure(state_path: Path, err: str) -> None:
    """状态文件插告警行（feed 抓取失败）。放在 frontmatter 之后（防破坏解析）。"""
    if not state_path.exists():
        return
    text = state_path.read_text(encoding="utf-8")
    # err 来自 curl 异常消息（含外部可控 URL 片段）：单行化，
    # 防止换行注入伪造的分区标题 / checkbox 条目行（同标题注入漏洞）
    err = " ".join(str(err).split())
    alert = f"> ⚠️ feed 抓取失败（{datetime.now().strftime('%Y-%m-%d')}）：{err}\n"
    if "feed 抓取失败" in text:
        return
    # 插在 frontmatter 结束后（--- 后的第一行），不破坏 _split_frontmatter
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end >= 0:
            insert_at = end + 4
            text = text[:insert_at] + "\n" + alert + text[insert_at:].lstrip("\n")
            state_path.write_text(text, encoding="utf-8")
            return
    state_path.write_text(alert + text, encoding="utf-8")


def _transcribe_items(info_paths: list[Path]) -> None:
    """对下载的 items 跑 ASR 转写（复用 asr_podcast.transcribe_one）。"""
    try:
        import asr_podcast
        from pathlib import Path as _P
        transcripts_dir = _P("rss") / "transcripts"
        transcripts_dir.mkdir(parents=True, exist_ok=True)
        for p in info_paths:
            ok, msg = asr_podcast.transcribe_one(p, transcripts_dir)
            if not ok and msg != "skip_exists":
                print(f"  ⚠️  转写跳过: {msg}")
    except ImportError:
        print("⚠️  asr_podcast 模块不可用，跳过转写")


def main():
    import argparse
    ap = argparse.ArgumentParser(description="抓 RSS 播客 + 下载音频")
    ap.add_argument("rss_url", nargs="?", default="",
                    help="RSS feed URL（订阅模式命令 --subscribe/--fetch-updates/--sync-pick/--pick-subscribe 时可省略）")
    ap.add_argument("--max", type=int, default=DEFAULT_MAX,
                    help=f"最多下载 N 期（默认 {DEFAULT_MAX}）")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                    help="输出根目录（默认 rss）")
    ap.add_argument("--force", action="store_true", help="跳过 dedup 强制重下")
    ap.add_argument("--cleanup-audio", action="store_true",
                    help="转写完成后删除 /tmp/rss-grab-audio/ 下本次下载的音频")
    ap.add_argument("--transcribe", action="store_true",
                    help="下载后自动 ASR 转写到 transcripts/")
    ap.add_argument("--list", action="store_true",
                    help="只列出全部分期（序号/日期/时长/guid），不下载")
    ap.add_argument("--pick", type=str, default="",
                    help="按序号/范围/guid 选择下载，如 '1,3,5-8' 或 'last:3' 或 guid hash8")
    # 订阅模式命令
    ap.add_argument("--subscribe", type=str, default="",
                    help="订阅新源：传 Apple Podcasts 链接（自动反推 feed URL）")
    ap.add_argument("--feed-url", type=str, default="",
                    help="--subscribe 的手动 feed URL fallback（反推失败时用）")
    ap.add_argument("--fetch-updates", action="store_true",
                    help="遍历订阅表，拉取所有源的增量 -> AI 摘要 -> 写状态文件待确认区")
    ap.add_argument("--sync-pick", type=Path,
                    help="读状态文件的 checkbox，按 [x]/[~]/[done] 重新归区 + 更新提示行")
    ap.add_argument("--pick-subscribe", type=Path,
                    help="从订阅状态文件读 [x] 期的 guid，下载音频 -> 转写 -> 做笔记")
    ap.add_argument("--retry-summary", type=str, default="",
                    help="单期重跑 AI 摘要（传 guid），结果写回状态文件")
    ap.add_argument("--unsubscribe", type=str, default="",
                    help="退订：传节目名，从订阅表删除 + 状态文件标记已退订")
    args = ap.parse_args()

    # ---- 订阅模式命令分支（不需要 rss_url）----
    if args.subscribe:
        return cmd_subscribe(args.subscribe, feed_url_override=args.feed_url)
    if args.fetch_updates:
        return cmd_fetch_updates()
    if args.sync_pick:
        return cmd_sync_pick(args.sync_pick)
    if args.pick_subscribe:
        return cmd_pick_subscribe(args.pick_subscribe, args)
    if args.retry_summary:
        return cmd_retry_summary(args.retry_summary)
    if args.unsubscribe:
        return cmd_unsubscribe(args.unsubscribe)

    # ---- 传统抓取模式（需要 rss_url）----
    if not args.rss_url:
        ap.error("rss_url 是必填参数（除非用订阅模式命令 --subscribe/--fetch-updates/--sync-pick/--pick-subscribe）")

    out_dir = args.out_dir
    raw_dir = out_dir / "raw"
    # 音频存 /tmp（临时，转写后可选清理；raw/transcripts 入 git 防丢）
    audio_dir = TMP_AUDIO_DIR
    raw_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== 抓取 RSS: {args.rss_url} ===")
    xml_bytes = fetch_xml(args.rss_url)
    print(f"  XML 大小: {len(xml_bytes)} bytes")

    try:
        feed = parse_rss.parse_text(xml_bytes)
    except (ET.ParseError, ValueError) as e:
        print(f"\n⚠️  无法解析为 RSS XML：{e}")
        print("   该链接可能不是播客音频 feed（阶段 1 只支持 RSS 2.0 + itunes +")
        print("   audio enclosure）。文章/视频/Atom/JSON Feed 不处理。")
        print("   （若编码非 UTF-8/UTF-16，标准库 expat 可能不支持）")
        sys.exit(1)
    print(f"  节目: {feed['title']}")
    print(f"  作者: {feed.get('author') or 'N/A'}")
    print(f"  共 {len(feed['items'])} 期")

    if not parse_rss.is_podcast_audio_feed(feed):
        print("\n⚠️  该 RSS 不是播客音频 feed（无 audio/* enclosure）。")
        print("   阶段 1 只支持播客音频类 RSS，不处理文章/视频/Atom。")
        sys.exit(1)

    # 交互选择：--list / --pick
    if args.list:
        list_items(feed["items"])
        print("\n💡 提示：确认要抓哪些后，用 --pick '1,3,5-8' 选择下载（或走订阅模式）")
        return

    if args.pick:
        idx_list = parse_pick_spec(args.pick, len(feed["items"]))
        # guid 匹配（非数字/范围的 8 位 hash）
        for token in args.pick.replace(",", " ").split():
            if token and not token.isdigit() and "-" not in token and not token.startswith("last"):
                gi = guid_to_idx(feed["items"], token)
                if gi is not None and gi not in idx_list:
                    idx_list.append(gi)
        if not idx_list:
            print(f"⚠️  --pick '{args.pick}' 没有匹配到任何期数，退出")
            return
        idx_list.sort()
        items = [feed["items"][i] for i in idx_list]
        print(f"\n将处理 --pick 选中的 {len(items)} 期：")
    else:
        items = feed["items"][:args.max]
        print(f"\n将处理最近 {len(items)} 期：")

    n_ok = n_skip = n_fail = 0
    new_raw_paths = []  # 本次成功下载的 info.json 路径（--transcribe 用）
    downloaded_audio: list[Path] = []  # 本次下载成功清单（--cleanup-audio 白名单）
    for i, item in enumerate(items, 1):
        title = item["title"]
        guid_hash8 = derive_rss_id.derive_id(
            guid=item["guid"], pub_date=item["pub_date"], title=title)
        safe = sanitize_title(title)
        print(f"\n--- [{i}/{len(items)}] {title} ---")
        print(f"  guid_hash8: {guid_hash8}")

        # dedup
        if not args.force:
            exists, existing = dedup_check.check_rss_raw_exists(raw_dir, guid_hash8)
            if exists:
                print(f"  ⏭️  已存在 info.json：{existing}")
                n_skip += 1
                continue

        # 下载音频
        enc = item["enclosure"]
        if not enc or not enc.get("url"):
            print("  ❌ 无 enclosure url，跳过")
            n_fail += 1
            continue
        ext = ext_from_enclosure(enc.get("type", ""))
        audio_path = audio_dir / f"{safe}-{guid_hash8}.{ext}"
        print(f"  ⬇️  下载音频 -> {audio_path.name}")
        ok, err = download_audio(enc["url"], audio_path, force=args.force)
        if not ok:
            print(f"  ❌ 下载失败: {err[:200]}")
            n_fail += 1
            # 仍落 info.json（标记失败），方便后续重试
            info = build_info_json(feed, item, guid_hash8, None)
            info["local"]["audio_download_error"] = err[:300]
            raw_path = raw_dir / f"{safe}-{guid_hash8}.info.json"
            raw_path.write_text(json.dumps(info, ensure_ascii=False, indent=2),
                                encoding="utf-8")
            if i < len(items):
                time.sleep(SLEEP_BETWEEN)
            continue

        # 落 info.json
        info = build_info_json(feed, item, guid_hash8, audio_path)
        raw_path = raw_dir / f"{safe}-{guid_hash8}.info.json"
        raw_path.write_text(json.dumps(info, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print(f"  ✅ 完成：{raw_path.name}")
        n_ok += 1
        new_raw_paths.append(raw_path)
        downloaded_audio.append(audio_path)

        if i < len(items):
            print(f"  ...sleep {SLEEP_BETWEEN}s (CDN 礼仪)")
            time.sleep(SLEEP_BETWEEN)

    print(f"\n=== 下载完成：✅ {n_ok} | ⏭️ {n_skip} | ❌ {n_fail} ===")
    if n_ok and not args.transcribe:
        print(f"\n💡 原料已落盘。加 --transcribe 自动转写，或手动跑 asr_podcast.py")

    if args.transcribe and new_raw_paths:
        print(f"\n=== ASR 转写 {len(new_raw_paths)} 期 ===")
        sys.path.insert(0, str(SCRIPT_DIR))
        import asr_podcast
        transcripts_dir = out_dir / "transcripts"
        transcripts_dir.mkdir(parents=True, exist_ok=True)
        t_ok = t_skip = t_fail = 0
        for ip in new_raw_paths:
            ok, msg = asr_podcast.transcribe_one(ip, transcripts_dir, force=args.force)
            if ok:
                t_ok += 1
            elif msg == "skip_exists":
                print(f"  ⏭️  已有 transcript：{ip.name}")
                t_skip += 1
            elif msg == "no_audio":
                print(f"  ⏭️  无音频：{ip.name}")
                t_fail += 1
            else:
                print(f"  ❌ {msg[:200]}")
                t_fail += 1
        print(f"\n=== 转写完成：✅ {t_ok} | ⏭️ {t_skip} | ❌ {t_fail} ===")
        if t_ok:
            print(f"💡 阶段 2 完成。阶段 3（笔记生成）后续实现。")

    # 转写完成后清理 /tmp 音频（可选，--cleanup-audio；只删本次下载的）
    if args.cleanup_audio and downloaded_audio:
        n_del = cleanup_audio_files(downloaded_audio)
        print(f"🧹 --cleanup-audio：已清理 {n_del} 个 /tmp 音频")

    # 全部下载失败时返回非零退出码（让 fetch_rss_pending 正确识别失败）
    if n_ok == 0 and n_fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
