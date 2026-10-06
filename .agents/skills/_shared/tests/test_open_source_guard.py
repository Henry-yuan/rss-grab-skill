#!/usr/bin/env python3
"""opensource 仓库的开源边界守门测试（Henry 2026-10-04 硬规则）。

**为什么必须有这个测试**：`~/Projects/opensource/rss-grab` 是公开仓库，只开源 rss。
小红书与 B 站不打算开源。往公开仓库里带进不开源部分的**后果不可逆** ——
即使事后删文件，git 历史里仍然存在。靠人记这条规则迟早会漏，故做成机器拦截。

用法：
    python3 scripts/tests/test_open_source_guard.py
    # 或纳入 pytest：python3 -m pytest scripts/tests/ -v

⚠️ 本文件自身含有被禁关键词。匹配时已排除本文件与 OPEN_SOURCE_GUARD 标记行，
因此「测试文件里出现关键词」不会自我触发失败。
"""
from __future__ import annotations

import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]      # .agents/skills/_shared/tests/ → 仓库根
GUARD_FILE = pathlib.Path(__file__).resolve()

# 禁止出现的源专属特征。
# OPEN_SOURCE_GUARD: 逐类列出「为什么禁」，改名单时连理由一起改，避免只留关键词丢失语义。
BANNED = {
    "小红书（中文名）": re.compile(r"小红书"),
    "小红书（拼音）": re.compile(r"xiaohongshu", re.I),
    "小红书（缩写）": re.compile(r"\bxhs\b", re.I),
    # ⚠️ 左边界不能省：`B\s*站` 会命中「rss-gr**ab 站**在巨人的肩膀上」
    # （README 致谢段，真实误报）。要求 B 前面不是字母数字下划线。
    "B 站（中文名）": re.compile(r"(?<![0-9A-Za-z_])B\s*站"),
    "B 站（拼音）": re.compile(r"bilibili", re.I),
    "B 站 skill 名": re.compile(r"bili-grab"),
    "B 站 bvid": re.compile(r"\bBV[0-9A-Za-z]{10}\b"),
    "小红书分享参数": re.compile(r"xsec_token|xhsshare|xsec_source", re.I),
    # ⚠️ `note_id` **不在**黑名单里：它是 AC-C2 定的**三源共用主键名**
    # （本仓库侧填 guid_hash8），曾被误列为某源专属而误报，见 spec 决策记录。
    "某源专属的原始字段名": re.compile(r"\bnickname\b|\bpublish_time_ms\b"),
    # ⚠️ 2026-10-06 修「源码转义绕过」：
    # 原文 `r"images\.md"` 匹配的是**不带反斜杠**的 `images.md`，而源码里
    # 实际写的是 `\.images\.md`（正则字符串，多一层转义）→ **永远匹配不上**。
    # 实测 `rss-grab/scripts/adapt_template.py:154` 就这样溜过去了。
    # 修法：中间的 `\` 与 `.` 都改成可选，两种写法都能命中。
    # 通用教训：**黑名单里的模式若本身是正则片段，必须能匹配「它作为源码正则出现」的形态**。
    "小红书图文扩展名": re.compile(r"images\\?\.?md"),
    "图文采集（OCR）": re.compile(r"\bOCR\b"),
    # ⚠️ 2026-10-06 补：B 站字幕产物后缀。此前不在名单，
    # 于是 `adapt_template.py` 里的 `.ai-zh.transcript.md` 一路放行。
    # ⚠️ 同样按「源码转义形态」修：`\.ai-zh\.` 在源码里带反斜杠，
    # 写死 `ai-zh` 虽然能命中，但为与其他条目一致，这里允许中间有转义符。
    "B 站字幕产物后缀": re.compile(r"ai[-]?zh"),
    # ⚠️ 2026-10-06 补：B 站字段名。实测 `bvid_or_note_id` 就在 rss 脚本里。
    "B 站字段名": re.compile(r"\bbvid\b"),
    # ⚠️ 2026-10-06 补：疑似真实平台账号 ID（B 站 mid 是 8–15 位连续数字）。
    # 为什么**不按字段名禁 `uploader_id`**：它是 AC-C2 的 17 字段契约成员
    # （rss 侧填 guid_hash8），按词禁会误报（实测报 3 处假警报）。
    # 为什么**不按 `uploader_id: <值>` 禁**：rss 侧本来就该给它填 guid_hash8，
    # 禁掉就把合法赋值也毙了。
    # 只能按**值的形态**判 —— 12 位以上连续数字在这个仓库里没有正当用途。
    "疑似真实平台账号 ID": re.compile(r"(?<![0-9])[0-9]{12,}(?![0-9])"),
    # ⚠️ 2026-10-06 补：非本源主键的 24 位十六进制形态。
    #
    # 🔴 **为什么不能只按「形态」禁**（这条是本仓库特有的坑，改名单前务必读完）：
    #     本域 podcast 托管平台的 episode ID **也是 24 位小写 hex**，
    #     与另一源的主键**形态完全相同**、字符串上无法区分。
    #     实测：按形态禁会误报 40 处，其中 26 处来自真实 feed 样本
    #     （`fixtures/podcast_sample.xml`），14 处来自用它做 guid 的单测。
    #     也就是说形态规则一旦上线，本仓库的**核心数据**会被判红 —— 守门随即
    #     被迫整体豁免，形同虚设。这比不写这条规则更危险。
    #
    # 改用**带值形态**：非本源字段名 + 这个长度的值同时出现才算泄露。
    # 依据：契约里该字段名是三源共用的，本仓库侧**必须填 8 位 guid_hash8**
    # （见上面 `uploader_id` 那条的同类理由）—— 所以「该字段名 + 24 位 hex 值」
    # 在本仓库没有任何正当用途，一旦出现必是带进来的真实数据。
    # 键与值之间留 40 字符窗口，覆盖 `"键": "值"` / `键="值"` / `键: 值` 等常见写法。
    #
    # ⚠️ 已知局限（未能覆盖，属可接受取舍）：
    #     ① 只认「键在前、值在后」；值在前键在后的写法漏。
    #     ② 若泄露时把该字段名换成裸 `id`，本条漏（但裸 `id` 形态在本仓库
    #        无法与本域 episode ID 区分，见上面的形态冲突说明）。
    "非本源主键形态（字段名 + 24 位十六进制值）":
        re.compile(r"(?<![0-9A-Za-z_])note_id(?![0-9A-Za-z_])[\s\S]{0,40}?\b[0-9a-f]{24}\b"),
    # ==================================================================
    # 飞书（Lark）凭证与表格标识 —— 2026-10-06 随「飞书表格镜像（可选功能）」新增
    # ==================================================================
    # ⚠️ 设计前提：**按「值的形态」拦，不按「lark / 飞书」这个词拦**。
    # 本仓库合法地会出现 lark-cli 调用、开放平台文档域名等实现代码，
    # 按关键词拦会让功能代码自己判红、守门被整体豁免（与上面 24 位 hex
    # 形态冲突同一个坑）。
    #
    # 1) 应用 ID：飞书 app_id 固定 `cli_` + 16 位左右 base36。这个前缀是
    #    飞书的保留命名空间，全网无第二家用，故零误报。
    #    左边界不能省：`lark-cli` 的 `cli` 后面跟的是 `-` 不是 `_`，本来也不匹配；
    #    但写成 `lark_cli_bin` 这类变量名就会命中，所以要求 `cli` 前不是标识符字符。
    "飞书应用 ID": re.compile(
        r"(?<![0-9A-Za-z_])cli_[0-9A-Za-z]{8,24}(?![0-9A-Za-z])", re.I),
    # 2) 用户 / 租户身份 ID：`ou_`（open_id）、`on_`（union_id），
    #    后面固定 32 位小写 hex。`ox_`/`oc_` 是文档/群 ID，本仓库用不到，不加。
    "飞书身份 ID": re.compile(
        r"(?<![0-9A-Za-z_])o[un]_[0-9a-f]{32}(?![0-9A-Za-z])", re.I),
    # 3) 表格 / 多维表格链接：飞书域 + 表格类路径段。
    #    ⚠️ 必须带**域名 + 路径段**两个条件：只禁域名会误伤
    #    `docs.feishu.cn`、`open.feishu.cn/document` 这类合法文档引用；
    #    只禁路径段则毫无意义。`sheets` / `s` / `base` 是表格与 Bitable 三种入口。
    "飞书表格链接": re.compile(
        r"https?://[A-Za-z0-9._-]+\.(?:feishu|larksuite)\.(?:cn|com)/(?:s|sheets|base)/", re.I),
    # 4) 凭证值（键 + 值同时出现才算泄露）。
    #    🔴 **为什么必须带键名**：飞书表格 token 本身是 20-64 位无格式串，
    #    与本域 episode ID（24 位 hex）形态重叠 —— 只按值禁会误报本仓库核心数据。
    #    （与上面 24 位 hex 那条同源问题，此处同样选「带键名」而不是「禁形态」。）
    #    值要求 20-64 位 token 形态字符。两条豁免让功能代码不被误伤：
    #      · placeholder 词（your/example/xxx/替换…）—— .env.example 与文档里的
    #        占位值是**必须允许**的，否则文档没法写示例；
    #      · 纯点号属性链（cfg.lark_sheet_token / os.environ.get）—— 那是
    #        变量引用不是凭证字面量。
    "飞书凭证字面量（键名 + 值）": re.compile(
        r"(?<![0-9A-Za-z_])"
        r"(?:(?:lark|feishu)[_-]?[A-Za-z0-9_]*?(?:secret|token|password|passwd|key)"
        r"|spreadsheet[_-]?token|sheet[_-]?token|excel[_-]?id"
        r"|app[_-]?(?:id|secret))"
        r"(?![0-9A-Za-z_])"
        r"[\"']?\s*[:=]\s*"
        # 豁免 1：值开头出现占位词
        r"(?![\"']?[0-9A-Za-z._\-]{0,30}(?:your|example|placeholder|change_me"
        r"|changeme|dummy|fake|sample|xxxx|todo|replace|insert|redacted|here"
        r"|notset|none)[0-9A-Za-z._\-]{0,30}[\"']?(?=[\s'\",#)]|$))"
        # 豁免 2：纯点号属性链（变量引用）
        r"(?![\"']?[0-9A-Za-z_]+(?:\.[0-9A-Za-z_]+)+[\"']?(?=$|[\s,'\"#)]))"
        r"[\"']?[0-9A-Za-z._\-]{20,64}[\"']?", re.I),
    # 5) 访问令牌字面量：`t-` 前缀 + 30 位以上 base64url（tenant/user access token）。
    #    这类令牌**没有键名可依托**（常直接写进命令或 curl 头），只能按值形态拦。
    #    左边界排除标识符与连字符，避免把 `t-` 当普通分隔符命中。
    "飞书访问令牌字面量（t- 前缀）": re.compile(
        r"(?<![0-9A-Za-z_-])t-[A-Za-z0-9]{30,}(?![0-9A-Za-z])", re.I),
}

# 三源共用契约里的字段名，按 Henry 2026-10-04 决策豁免（RSS 侧填 guid_hash8，
# 不是 B 站数据）。
#
# ⚠️ 豁免必须**精确到 token**，不能按整行豁免 —— 曾试过「同行出现键名就放过」，
# 结果 `test_frontmatter_contract.py` 里那个真实 B 站 UP 主 ID
# `3706955719772347` 与 `"uploader_id"` 的注释**在同一行**，整行豁免等于没豁免。
#
# 下面每条只匹配「字段名作为键/标识出现」的位置：
#   - `"uploader_id",`   契约字段表里的裸字段名
#   - `` `uploader_id` ``  注释/文档里作为**概念**提及
# 都不匹配 `"uploader_id": "具体值"` 这种带值的形式 —— 带值才可能是真数据。
# 12 位数字规则的兜底豁免：若将来仓库里真需要一个大整数常量（如超时毫秒数），
# 在这里加一条精确模式，而不是把规则放宽。
KNOWN_EXEMPT: dict[str, re.Pattern[str]] = {}

# ⚠️ 2026-10-06 补 `.xml`：此前漏了，
# `rss-grab/scripts/tests/fixtures/podcast_sample.xml`（24KB 真实 feed 样本）
# **从未被扫过** —— 扫描范围缺后缀是最隐蔽的一类漏洞：
# 守门显示 passed，文件就在仓库里，只是从来没进过检查范围。
SCAN_SUFFIXES = (".py", ".md", ".json", ".txt", ".example",
                 ".yaml", ".yml", ".sh", ".xml")


def _is_guard_file(path: pathlib.Path) -> bool:
    return path.resolve() == GUARD_FILE.resolve()


def _iter_files():
    for p in REPO_ROOT.rglob("*"):
        if not p.is_file() or p.suffix not in SCAN_SUFFIXES:
            continue
        if _is_guard_file(p):
            continue
        if any(part in {".git", "__pycache__", ".pytest_cache", "output", "scan_work"}
               for part in p.parts):
            continue
        yield p


class TestOpenSourceBoundary(unittest.TestCase):
    """rss-grab 公开仓库只允许出现 rss 相关内容。"""

    def test_no_banned_source_markers(self):
        """仓库内（除本文件外）不得出现任何小红书 / B 站采集特征。"""
        hits: list[str] = []
        for p in _iter_files():
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines = text.split("\n")
            for label, rx in BANNED.items():
                for m in rx.finditer(text):
                    line_no = text.count("\n", 0, m.start()) + 1
                    # 豁免只按**所在整行**判：契约里的字段名（`"uploader_id":`）
                    # 合法，同一行的**数据值**不豁免。
                    # ⚠️ 曾试过只豁免「键名部分」，结果 `test_frontmatter_contract.py`
                    # 里那个真实 B 站 UP 主 ID 正好在同一行 —— 只看键名等于没豁免。
                    # 只看**命中点附近 40 字符**：豁免的是「字段名 token」，
                    # 不是整行 —— 同一个值可能既在注释里又在数据里。
                    ctx = text[max(0, m.start() - 10): m.end() + 10]
                    if KNOWN_EXEMPT and any(ex.search(ctx) for ex in KNOWN_EXEMPT.values()):
                        continue
                    rel = p.relative_to(REPO_ROOT)
                    hits.append(f"{rel}:{line_no}  [{label}] …{text[max(0, m.start()-30):m.end()+30]!r}")
        if hits:
            self.fail(
                f"发现 {len(hits)} 处不允许的开源内容：\n"
                + "\n".join(f"  {h}" for h in hits[:40])
                + ("\n  …（其余省略）" if len(hits) > 40 else "")
                + "\n\n依据：AGENTS.md「🔴 开源边界」。小红书 / B 站不打算开源，"
                  "本仓库只开源 rss。此类泄露一旦 push 即不可逆（git 历史仍在）。"
            )

    def test_no_reference_to_main_project_path(self):
        """rss 代码不得引用主项目的私有路径（别人 clone 本仓库必坏）。"""
        bad = re.compile(r"内容渠道采集|video-grab")
        hits = []
        for p in _iter_files():
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in bad.finditer(text):
                rel = p.relative_to(REPO_ROOT)
                # 只允许出现在解释「不要这么做」的注释/文档里，且必须同段提到禁止
                ctx = text[max(0, m.start() - 120):m.end() + 120]
                if "不得" in ctx or "禁止" in ctx or "不要" in ctx or "clone" in ctx:
                    continue
                line_no = text.count("\n", 0, m.start()) + 1
                hits.append(f"{rel}:{line_no}  …{ctx.strip()[:90]!r}")
        if hits:
            self.fail(
                "发现引用主项目私有路径的可疑代码：\n"
                + "\n".join(f"  {h}" for h in hits[:20])
                + "\n\n本仓库是公开的 rss 仓库，引用主项目路径会让别人 clone 后无法运行。"
            )

    def test_guard_file_itself_is_complete(self):
        """守门测试自身要有效：被禁词表不能为空，且真的能匹配到东西。"""
        self.assertGreaterEqual(len(BANNED), 10, "被禁词表过短，可能漏了新的源专属特征")
        # 冒烟：确认正则可编译且至少有一条真的能命中
        hits = sum(1 for rx in BANNED.values() if rx.search("这里有小红书和 bilibili 与 xsec_token"))
        self.assertGreaterEqual(hits, 3, "被禁正则疑似失效（连构造串都命中不了几条）")


# ---------------------------------------------------------------------------
# 正样例的凭证值一律**拼接生成**，不以字面量出现在源码里
# ---------------------------------------------------------------------------
# 🔴 **为什么必须拆**（主代理 2026-10-06 验收时发现的设计漏洞）：
#     `_iter_files()` 里的 `_is_guard_file()` 让**本文件成为全仓库唯一的扫描
#     豁免路径**（黑名单词表本身必须待在本文件，无法取消豁免）。
#     于是本文件若混入**真实**凭证，守门会永远显示绿色 —— 而
#     「写测试时顺手粘一个真实值当用例」是非常自然的行为。
#     这不是假想：2026-10-05 就发生过同款事故（`test_frontmatter_contract.py`
#     里混进一个真实的 B 站 UP 主 ID，形态逼真、守门放行，直到人工复核才抓到）。
#
# ⚠️ 拆开只是**降低误粘的概率**（完整形态不再连续存在于源码），
#    **不能替代纪律** —— 真要用真实凭证做用例，就不该用。拆分后的运行时值
#    仍必须能被规则命中，否则正样例自测会整体落空（假绿）。
_P_APP = "cli_"                        # 应用 ID 前缀
_P_OID = "ou_"                         # open_id 前缀
_P_UN = "on_"                          # union_id 前缀
_P_TK = "t-"                           # 访问令牌前缀
_P_FS = "fei" + "shu"                  # 飞书域名（拆两半）
_P_LS = "lark" + "suite"               # 国际版域名
_P_SHEETS = "/" + "sheets" + "/"
_P_SHORT = "/" + "s" + "/"
_P_BASE = "/" + "base" + "/"

# 键名同样拆开：否则源码里会出现 `LARK_APP_SECRET=aB3x…` 这样的连续完整形态
_K_SECRET = "LARK_APP_" + "SECRET"
_K_SECRET_LC = "lark_app_" + "secret"
_K_APP_ID = "LARK_APP_" + "ID"
_K_SHEET_TOKEN = "LARK_SPREADSHEET_" + "TOKEN"
_K_SHEET_TOKEN2 = "SPREADSHEET_" + "TOKEN"
_K_EXCEL_ID = "excel" + "_id"


class TestLarkCredentialRedLine(unittest.TestCase):
    """飞书凭证红线：**每条新规则都必须既抓得住、又放得过**。

    这不是重复守门测试，而是守门测试自身的**有效性**测试。
    起因：2026-10-06 加这批规则时，作者自己的正则先后出现过
    「漏掉一个右括号」和「把键与值之间的赋值符删掉」两次低级错误，
    两者都表现为**全部漏抓**（守门显示 passed，实际一条都拦不住）。
    光靠「跑一次守门是绿的」发现不了 —— 守门绿恰恰是因为它没匹配到东西。
    所以把正反样例固化进测试：规则退化了这里立刻变红。

    本类的正样例（MUST_CATCH）值全部由上面的片段拼接而来，
    原因见那段注释 —— 本文件是全仓库唯一不被守门扫描的路径。
    """

# ⚠️ 本文件里的样例值一律是**人工合成的模式值**（0-f 循环 / AbCdEf 之类），
#    刻意不使用任何真实环境里出现过的值 —— 本文件是守门唯一不扫描的路径，
#    真实身份/凭证一旦粘在这里就永远抓不到（2026-10-05 已有同款事故）。
    # (规则标签, 应当被拦下的样例)
    MUST_CATCH = [
        ("飞书应用 ID", 'APP_ID = "' + _P_APP + "a1b2c3d4e5f6g7h8" + '"'),
        ("飞书应用 ID", _K_APP_ID + "=" + _P_APP + "a1b2c3d4e5f6g7h8"),
        ("飞书身份 ID", 'open_id = "' + _P_OID + "0123456789abcdef0123456789abcdef" + '"'),
        ("飞书身份 ID", "UNION_ID: " + _P_UN + "0123456789abcdef0123456789abcdef"),
        ("飞书表格链接", "见 https://my-org." + _P_FS + ".cn" + _P_SHEETS + "AbCdEf12345"),
        ("飞书表格链接", "https://my-org." + _P_LS + ".com" + _P_SHORT + "AbCdEf12345"),
        ("飞书表格链接", "https://my-org." + _P_FS + ".cn" + _P_BASE + "bascnAbCdEf12345"),
        ("飞书凭证字面量（键名 + 值）", _K_SECRET + "=aB3xY9zQ7mN2pL5kR8vT4wS6dF1gH0j"),
        ("飞书凭证字面量（键名 + 值）", _K_SECRET + '="aB3xY9zQ7mN2pL5kR8vT4wS6dF1gH0j"'),
        ("飞书凭证字面量（键名 + 值）", _K_SECRET_LC + ": k7Hg2Lp9Qw3Er5Ty1Ui8Op0As6Df4"),
        ("飞书凭证字面量（键名 + 值）", _K_SHEET_TOKEN + "=shtcnAbCdEfGhIjKlMnOpQr"),
        ("飞书凭证字面量（键名 + 值）", _K_SHEET_TOKEN2 + " = AbCdEfGhIjKlMnOpQrStUvWx"),
        ("飞书凭证字面量（键名 + 值）", _K_EXCEL_ID + ": Qw3Er5Ty1Ui8Op0As6Df4Gh0Jk"),
        ("飞书访问令牌字面量（t- 前缀）", 'token = "' + _P_TK + "Ab3xY9zQ7mN2pL5kR8vT4wS6dF1gH0jKl" + '"'),
        ("飞书访问令牌字面量（t- 前缀）", _P_TK + "9Kq2Lm5Np8Rt1Vw4Xz7Bc3Df6Gh0Jk2"),
    ]

    # 合法实现代码 / 文档占位符：**绝不能**被判红，否则守门会被整体豁免
    MUST_PASS = [
        'lark-cli sheets +csv-put --spreadsheet-token "$TOKEN" --sheet-name "$S"',
        "lark-cli sheets +csv-put --spreadsheet-token token --sheet-name name",
        '["sheets", "+csv-put", "--spreadsheet-token", token, "--sheet-name", name]',
        'os.environ.get("LARK_SPREADSHEET_TOKEN", "")',
        "LARK_SPREADSHEET_TOKEN = os.environ.get(...)",
        "lark_sheet_token = cfg.lark_sheet_token",
        "See https://open.feishu.cn/document for docs",
        "域名为 docs.feishu.cn，用于说明文档",
        'export PATH="$HOME/.local/bin:$PATH"; lark-cli whoami',
        "LARK_APP_SECRET=<从飞书开放平台获取>",
        "LARK_SPREADSHEET_TOKEN=",
        "LARK_APP_SECRET=your-app-secret-here",
        "LARK_SPREADSHEET_TOKEN=your-spreadsheet-token",
        "LARK_APP_SECRET=xxxxxxxxxxxxxxxx",
        "APP_SECRET=changeme",
        'LARK_APP_SECRET="replace-me-with-real-value"',
        "token = t-  # 说明",
    ]

    LARK_LABELS = {
        "飞书应用 ID",
        "飞书身份 ID",
        "飞书表格链接",
        "飞书凭证字面量（键名 + 值）",
        "飞书访问令牌字面量（t- 前缀）",
    }

    def test_every_lark_rule_catches_its_shape(self):
        """每条飞书规则都要能命中自己的形态 —— 漏抓就是规则已失效。"""
        missing_rules = self.LARK_LABELS - set(BANNED)
        self.assertFalse(missing_rules, f"飞书红线规则不见了：{missing_rules}")
        misses = []
        for label, sample in self.MUST_CATCH:
            rx = BANNED.get(label)
            if rx is None or not rx.search(sample):
                misses.append(f"[{label}] {sample}")
        if misses:
            self.fail("以下形态没被抓到（规则可能已失效）：\n  "
                      + "\n  ".join(misses))

    def test_lark_rules_do_not_flag_legitimate_code(self):
        """飞书规则不得误伤功能代码与文档占位符。"""
        fps = []
        for sample in self.MUST_PASS:
            for label in self.LARK_LABELS:
                rx = BANNED.get(label)
                if rx and rx.search(sample):
                    fps.append(f"[{label}] {sample}")
        if fps:
            self.fail("以下合法写法被误判（守门会因此被整体豁免）：\n  "
                      + "\n  ".join(fps))

    def test_guard_source_has_no_credential_shape(self):
        """本文件源码里**不得出现任何会被飞书红线命中的完整形态**。

        为什么需要这条：本文件是 `_iter_files()` 全仓库**唯一**的扫描豁免路径，
        混进真实凭证时守门永远绿。正样例因此必须靠拼接生成（见上面注释）。
        这条把「靠拼接」从约定变成机器拦截 —— 将来谁又粘了个字面量真值进来，
        这里立刻变红，而不是等到人工复核。
        """
        src = GUARD_FILE.read_text(encoding="utf-8")
        hits = []
        for label in sorted(self.LARK_LABELS):
            rx = BANNED.get(label)
            if not rx:
                continue
            for m in rx.finditer(src):
                line_no = src.count("\n", 0, m.start()) + 1
                hits.append(f"{line_no}  [{label}] "
                            f"…{src[max(0, m.start()-30):m.end()+20]!r}")
        if hits:
            self.fail(
                f"守门文件自身出现 {len(hits)} 处飞书凭证形态：\n  "
                + "\n  ".join(hits[:20])
                + "\n\n本文件不被守门扫描，等于没有防线。请把值改成拼接生成"
                  "（参考文件顶部 _P_APP / _K_SECRET 等片段），"
                  "并确认拼接后的运行时值仍能被规则命中。"
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
