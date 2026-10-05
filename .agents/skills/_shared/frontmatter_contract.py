#!/usr/bin/env python3
"""笔记 frontmatter 的**统一契约**（V1.0 C6 / AC-C2，2026-10-05 抽出）。

## 为什么要有这个模块

C6 把磁盘上 320 篇产物的 frontmatter 统一到 17 字段后，
**有一个源的 builder 没跟上**——它还在吐 12 字段 + `asr_status`，
再跑一次管线就会写回旧格式，让磁盘上已有的产物变成两种格式并存。

根因不是"忘了改"，是**契约没有单一定义源**：每个源各写各的 `build()`，
字段集靠约定维持，约定一破就静默漂移。实测同一个项目里同时存在
12 字段版和 17 字段版两个 builder，一个用「缺键就省略」、另一个用
「无数据也留空串」，行为都不一样。

所以这里只放**契约**（字段表 + 序列化 + 自检），
各源的 `build()` 负责从自己的 info.json 取数，但**必须**产出 `FIELDS`
里的全部 key，顺序也按它来。`_shared/tests/test_frontmatter_contract.py`
逐条锁住序列化行为（含「行首 YAML 指示符必须加引号」这个真实故障）。

## 两份拷贝必须逐字节相同

主项目与 opensource 各有一份**独立实体拷贝**（AC-C5），按约定手动双改、
禁止脚本单向覆盖。因此**本文件全文不得出现任何具体源的名字**——
本仓库的守门测试会拦，而「复制过去再改写注释」这种手工步骤迟早会漏。
要举例就说「某一源」。

## 字段表（顺序即落盘顺序）

`FIELDS` 是唯一权威。改这里等于改全部三个源的产物格式，
必须同步改测试与 `docs/versions/V1.0/spec.md` 的 AC-C2。
"""
from __future__ import annotations

import re

try:                                  # PyYAML 不是硬依赖，见 _resolves_to_str
    import yaml as _yaml
except ImportError:                   # pragma: no cover - 取决于运行环境
    _yaml = None

# ── AC-C2 的 17 字段，顺序即落盘顺序 ──────────────────────
FIELDS = (
    "note_id",
    "title",
    "uploader",
    "uploader_id",
    "speaker",
    "type",
    "duration",
    "published_at",
    "source_url",
    "subtitle_source",
    "transcript_status",
    "source_tags",
    "tags",
    "summary",
    "generated_at",
    "description",
    "show_name",
)

# 旧字段名 → 新字段名。迁移脚本与回读兼容用；
# **新代码一律写新名**，不要在 build() 里产出旧名。
LEGACY_FIELD_ALIASES = {
    "asr_status": "transcript_status",     # 拆分见 C6：status + subtitle_source
    "publish_time": "published_at",
    "source_type": None,                    # 已被 subtitle_source 取代，删除
    "asr_model": None,                      # 同上
}


def check(fm: dict) -> list[str]:
    """字段集自检。返回问题列表，空列表 = 合规。"""
    keys = set(fm)
    problems = []
    missing = [f for f in FIELDS if f not in keys]
    extra = sorted(keys - set(FIELDS))
    if missing:
        problems.append(f"缺字段 {missing}")
    if extra:
        problems.append(f"多字段 {extra}")
    legacy = sorted(k for k in extra if k in LEGACY_FIELD_ALIASES)
    if legacy:
        problems.append(
            f"含已废弃字段 {legacy}（应改用 "
            f"{[LEGACY_FIELD_ALIASES[k] for k in legacy]}）")
    return problems


LIST_FIELDS = ("tags", "source_tags")


def normalize(fm: dict) -> dict:
    """按 FIELDS 顺序重排；缺键补空串，多余键丢弃。

    补空串是刻意的：字段集稳定比"少写几个空字段"重要，
    否则跨源比对时会因为"这源没这个字段"而误判成不等价。

    列表型字段（tags / source_tags）统一成 list：空值一律 `[]` 而不是 `""`
    ——否则同一字段在磁盘上会时而 `source_tags: []` 时而 `source_tags: ""`，
    yaml 读回来一个是 list 一个是 str，跨源比对又是一处假差异。
    ⚠️ 判空用**真值判断**而不是 `dict.get(k, 默认值)`：后者只在**键缺失**时
    兜底，键存在但值为 `""`（build() 里显式写空串是很常见的）会原样透传，
    于是磁盘上就留下一批 `source_tags: ""`。这正是 2026-10-05 清理前
    43 篇产物出现该形态的成因。
    """
    out = {}
    for k in FIELDS:
        v = fm.get(k)
        if k in LIST_FIELDS:
            # str 要按「单个标签」包起来：`list("AI")` 会碎成 ['A','I']
            if not v:
                v = []
            elif isinstance(v, str):
                v = [v]
            else:
                v = list(v)
        else:
            v = "" if v is None else v
        out[k] = v
    return out


# ── 序列化（从 rss-grab/build_frontmatter.py 搬来，三源共用）──
# 行首即改变 YAML 解析结果的字符（- ? : [ ] { } # & * ! | > % @ ` ' "）
YAML_LEAD = "-?:[]{}#&*!|>%@`\'\""
# 裸写出来会被 YAML 读成 bool / null 的值。
# ⚠️ 这张表**只是快速路径**，不是判据本身 —— 真正的判据是 `_resolves_to_str`
# 里那个「问解析器」的探针。留着它是为了在探针不可用时（见该函数）也不至于漏掉
# 最常见的一批；两张表若哪天不一致，以探针为准。
YAML_RESERVED = frozenset(
    {"true", "false", "null", "~", "yes", "no", "on", "off", ".inf", ".nan"})

DUMP_SAFE = str.maketrans({":": "：", "#": "＃", '"': "＂", "'": "＇",
                           "[": "［", "]": "］", "{": "｛", "}": "｝"})

# 值里含空白或非 ASCII → 一定不是 YAML 的隐式类型（隐式类型都是无空白单 token），
# 无 PyYAML 时的兜底据此放行。已用 ~10 万个枚举候选值验证过无反例。
_NEEDS_ESCAPE_RE = re.compile(r"[ \t\n\r]")


def _resolves_to_str(s: str) -> bool:
    """裸写出去的 `s`，被 YAML 读回来时**还是不是同一个 str**。

    ## 为什么用「问解析器」而不是维护类型清单

    YAML 1.1 的隐式类型远不止 int/float：还有 date、timestamp、bool、null、
    八/十六/二进制整数、`1_000` 这种下划线分隔、`.5`、`.inf`、以及
    **六十进制**（`1:08:29` → 4029，时长字段天然就是这个形状）。
    逐个补类型是打地鼠 —— 修过 int 之后又踩了 date，全数字的 id 也是同一类。
    判据只能是：**裸写 → 读回 → 比对类型与值**。

    探针在**映射值**和**序列项**两种上下文里判定结果一致（已验证），
    所以只测一种即可。

    ⚠️ **PyYAML 不是本模块的硬依赖**：`dump()` 目前能在
    `/usr/bin/python3`（无 PyYAML）下正常跑，顶部硬 `import yaml` 会把那条路打断。
    故这里 try-import；拿不到时退回保守启发式（宁可多引号）。
    """
    if _yaml is None:
        # 兜底：含空白或非 ASCII 的一律放行，其余一律引号（保守方向：加引号永远合法）
        return bool(_NEEDS_ESCAPE_RE.search(s) or any(ord(c) > 127 for c in s))
    try:
        back = _yaml.safe_load("k: " + s)["k"]
    except Exception:
        return False                      # 连 `k: <s>` 都解析不了 → 必须引号
    return isinstance(back, str) and back == s


def _scalar(v) -> str:
    """标量 → YAML 字面量。

    ⚠️ **换行必须转义成 \\n**（2026-10-04 修）：值里若含真换行，写出来的
    YAML 会跨行 —— 一旦值里还含 `---`（实际踩过：某期条目的 description 里就有），
    读回时会把它误认成 frontmatter 的闭合标记，**正文被整段吞掉**。
    转义成单行后这个失败模式彻底消失。
    """
    s = str(v)
    if s == "":
        return '""'
    # ⚠️ **行首的 YAML 指示符也必须加引号**（2026-10-05 修）。
    # 真实踩坑：某源 `info.json` 的 `desc` 字面就是一个短横线 `-`，
    # 原规则只查「含不含 `: # " ' [ ] { }`」和首尾空白，**漏了行首指示符**，
    # 于是写出 `description: -`；YAML 读回时把 `-` 当成序列项起始符，
    # 整份 frontmatter 解析失败。
    # 行首这些字符都会改变解析结果：`-`/`?` 序列或映射、`&`/`*` 锚点别名、
    # `!` 标签、`|`/`>` 块标量、`%` 指令、`@`/`` ` `` 保留字。
    if (s[0] in YAML_LEAD or "\n" in s or "\r" in s
            or s.translate(DUMP_SAFE) != s
            or re.search(r"^\s|\s$|:\s|\s#", s)
            or s.strip().lower() in YAML_RESERVED
            # 判据交给解析器：裸写出去若不再是同一个 str，就加引号。
            # 这一条覆盖 YAML 1.1 全部隐式类型（date / int 各进制 / 下划线分隔 /
            # 六十进制 / .inf / bool / null …），不再逐个补类型。
            or not _resolves_to_str(s)):
        return _quote(s)
    return s


def _quote(s: str) -> str:
    """加双引号并转义；换行一律转成 `\\n` 以保证单行。"""
    esc = (s.replace("\\", "\\\\").replace('"', '\\"')
            .replace("\r\n", "\\n").replace("\n", "\\n")
            .replace("\r", "\\n"))
    return '"' + esc + '"'


def dump(fm: dict) -> str:
    """frontmatter dict → YAML 文本（含首尾 `---`）。**按 FIELDS 顺序输出**。"""
    out = ["---"]
    for k, v in normalize(fm).items():
        if isinstance(v, list):
            if not v:
                out.append(f"{k}: []")
            else:
                out.append(f"{k}:")
                out += [f"  - {_scalar(x)}" for x in v]
        elif isinstance(v, bool):
            out.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            out.append(f"{k}: {v}")
        else:
            out.append(f"{k}: {_scalar(v)}")
    out.append("---")
    return "\n".join(out)


# 模块名刻意不叫 build_frontmatter：各源 skill 下已有同名模块，
# 同名会让 sys.path 的插入顺序决定 import 到哪一份（实测踩过）。
