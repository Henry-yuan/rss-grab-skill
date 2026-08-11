#!/usr/bin/env python3
"""批量笔记生成（20 并发，可暂停/续跑）—— rss 源。

薄 wrapper：实际实现见 _shared/batch_notes.py（共享模块，rss 源专用）。

用法：
  python3 batch_notes.py [--max-workers N] [--max N] [--force]
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent.parent / "_shared"))

import batch_notes as impl


def main():
    # 转成通用入口：--source rss
    argv = ["batch_notes.py", "--source", "rss"] + [a for a in sys.argv[1:]]
    sys.argv = argv
    impl.main()


if __name__ == "__main__":
    main()
