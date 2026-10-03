"""_shared/long_doc.py 单测（spec AC-A2：加单测）。

重点回归两件事：
1. 净字口径本身（剥时间戳 + 去空白）
2. 分钟 1-3 位的修复 —— 2026-10-04 实测 xhs 256 篇 0 处超 100 分钟，
   所以这个缺陷在小红书上完全不可见，必须靠单测钉住，否则改回去没人发现。
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from long_doc import (  # noqa: E402
    LONG_DOC_THRESHOLD,
    is_long_doc,
    net_len,
    strip_timestamps,
)


class TestNetLen(unittest.TestCase):
    def test_strips_bold_segment_marker(self):
        self.assertEqual(net_len("**[00:12]** 你好"), 2)

    def test_removes_all_whitespace_kinds(self):
        # 空格 / 制表 / 换行 / 全角空格 全都要去掉
        self.assertEqual(net_len("你 好\t世\n界　啊"), 5)

    def test_empty_and_marker_only(self):
        self.assertEqual(net_len(""), 0)
        self.assertEqual(net_len("**[00:12]**"), 0)
        self.assertEqual(net_len("   \n\t  "), 0)

    def test_no_marker_text_unchanged(self):
        self.assertEqual(net_len("正常内容"), 4)


class TestTimestampMinuteDigits(unittest.TestCase):
    """分钟位数回归 —— 这组测试的存在理由见模块 docstring。"""

    def test_two_digit_minutes_stripped(self):
        self.assertEqual(net_len("**[12:34]** abc"), 3)

    def test_three_digit_minutes_stripped(self):
        # 原实现 \d{1,2} 在这三行全部失败，是本模块要修的缺陷
        for mark in ("**[144:23]**", "**[163:45]**", "**[240:00]**", "**[325:00]**"):
            with self.subTest(mark=mark):
                self.assertEqual(net_len(f"{mark} abc"), 3)

    def test_one_digit_minutes_still_stripped(self):
        self.assertEqual(net_len("**[1:05]** abc"), 3)

    def test_incomplete_marker_not_stripped(self):
        # 形如 **[1:2]** 不是合法段标记，不该被剥（避免误伤正文里的方括号内容）
        # 去空白后剩 `**[1:2]**abc` = 12 个字符
        self.assertEqual(net_len("**[1:2]** abc"), 12)

    def test_real_long_sample_is_not_inflated(self):
        """一段 240 分钟的转写，时间戳必须被完全剥掉。"""
        minutes = list(range(0, 240, 5))
        text = "".join(f"**[{m:03d}:00]** 内容{m}\n" for m in minutes)
        expected = len("".join(f"内容{m}" for m in minutes))
        self.assertEqual(net_len(text), expected)
        # 原始长度里时间戳占了大头，不剥就会虚高
        self.assertGreater(len(text), expected * 4)


class TestIsLongDoc(unittest.TestCase):
    def _text_of_len(self, n):
        return "字" * n

    def test_strictly_greater_than_threshold(self):
        """AC-A3：用严格大于而非 >=，与 build_v3.py 的 n_chars > THRESHOLD 对齐。"""
        self.assertFalse(is_long_doc(self._text_of_len(LONG_DOC_THRESHOLD)))
        self.assertTrue(is_long_doc(self._text_of_len(LONG_DOC_THRESHOLD + 1)))

    def test_threshold_default_is_15000(self):
        self.assertEqual(LONG_DOC_THRESHOLD, 15_000)

    def test_judges_on_net_len_not_raw_len(self):
        """原始字符远超阈值但净字不到 → 判短文（这正是旧口径的判错方式）。"""
        segs = 10_000
        # 分钟取模到 3 位：4 位分钟（1000+）不是合法 ASR 标记，正则按设计不匹配
        text = "".join(f"**[{i % 1000:03d}:00]** 短\n" for i in range(segs))
        self.assertGreater(len(text), LONG_DOC_THRESHOLD)   # 原始字符 > 15000
        self.assertLess(net_len(text), LONG_DOC_THRESHOLD)  # 净字 < 15000
        self.assertFalse(is_long_doc(text))

    def test_custom_threshold(self):
        self.assertTrue(is_long_doc("字" * 11, threshold=10))


class TestStripTimestamps(unittest.TestCase):
    def test_removes_bold_and_bare(self):
        out = strip_timestamps("a **[00:01]** b [00:02] c")
        self.assertNotIn("00:01", out)
        self.assertNotIn("00:02", out)
        self.assertIn("a", out)
        self.assertIn("c", out)




if __name__ == "__main__":
    unittest.main()
