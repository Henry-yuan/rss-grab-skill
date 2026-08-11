#!/usr/bin/env python3
"""去重校验：检查 rss/raw/ 里是否已有某 guid_hash8 的 info.json。

匹配规则：
  - raw/ 文件名格式：<sanitized_title>-<guid_hash8>.info.json
  - glob 找 *-<hash8>.info.json -> 命中即视为「已存在」

⚠️  匹配规则：raw/*.info.json 已落盘 = 已处理（原料已落盘即视为已存在）。

用法：
  python3 dedup_check.py <raw_dir> <guid_hash8>
  例：python3 dedup_check.py rss/raw abcdef12

输出（stdout）：
  EXISTS=true
  FILE=<filename>     # 仅命中时有

退出码：
  0 = 存在
  1 = 不存在
  2 = 参数错误 / IO 错误
"""
import sys
import pathlib


def check_rss_raw_exists(raw_dir: pathlib.Path, guid_hash8: str):
    """检查 rss/raw/ 里是否已有 guid_hash8 对应的 info.json。

    返回 (exists, filename or None)
    """
    raw_dir = pathlib.Path(raw_dir)
    if not raw_dir.exists():
        return False, None
    suffix = f"-{guid_hash8}.info.json"
    for f in raw_dir.glob(f"*{suffix}"):
        return True, f.name
    return False, None


def main():
    if len(sys.argv) != 3:
        print("usage: dedup_check.py <raw_dir> <guid_hash8>", file=sys.stderr)
        sys.exit(2)
    raw_dir = sys.argv[1]
    guid_hash8 = sys.argv[2]
    try:
        exists, fname = check_rss_raw_exists(raw_dir, guid_hash8)
    except OSError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)
    if exists:
        print("EXISTS=true")
        print(f"FILE={fname}")
        sys.exit(0)
    else:
        print("EXISTS=false")
        sys.exit(1)


if __name__ == "__main__":
    main()
