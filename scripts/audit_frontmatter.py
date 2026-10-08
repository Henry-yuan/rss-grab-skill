#!/usr/bin/env python3
"""产物 frontmatter 体检（打标漏跑兜底 + 字段合规）。

本文件与主私有工作区的同名文件同源，两边按项目约定的双改纪律手工同步。
本仓为 rss-only 形态：配置表与 dispatch 只覆盖 rss 一个源；
G 标签体检层（G1~G4）与源无关，两份一致。

改动纪律：**先在主项目改，再同步至此**；有意的源相关差异
登记在主项目的 AST 比对守门测试白名单里，不靠记忆维持。

## 为什么需要它

frontmatter 是笔记的「入口契约」：下游消费（知识库、看板、批量重生成）
都按 `_shared/frontmatter_contract.py` 的 17 字段读。但契约的**写入方**修好
不等于**存量**修好 —— 一次性迁移用的 `scalar()` 当时还没有 YAML 转义，
一批笔记的 `generated_at` 被写成 `"'2026-10-03T14:50:36+08:00'"`。
脚本当天修好了，**存量却直到几天后才回填**，缺陷潜伏在磁盘上没人察觉。

⚠️ **光靠 `yaml.safe_load()` 抓不到这一类**：YAML 会把双引号里的单引号当普通
字符照收，解析**成功**，值里却多了两个字面单引号 → 下游 `fromisoformat()` 抛错。
所以本脚本的 `generated_at` 检查是**独立的值级断言**，不是解析的副产品。

## 检查分层

| 层 | 查什么 |
|---|---|
| A 结构 | frontmatter 存在 / 可解析 / 17 字段齐全 / 无多余字段 / 顺序符合 FIELDS |
| B 值级 | 每个字段的**形态**（枚举、时间戳、URL、主键、列表、控制字符、占位符） |
| C 正文 | 三个固定 H2 存在；H3 数字编号连续 |
| D 对账 | frontmatter ↔ `raw/*.info.json` 的主键与关键字段一致 |
| G 标签 | G1 漏跑 retag / G2 数量上限（**报警**）；G3 超长 / G4 近义重复（**仅报告**） |

## rss 的原料命名与主键

`raw/<标题>-<hash8>.info.json` 的**顶层只有 `feed` / `item` / `local` 三层，
没有扁平的主键字段**：主键在 `item.guid_hash8`，`uploader` 取自 `feed.title`。
因此对账**按主键查预建索引**，不靠文件名猜。

## 已知且**不算 bug** 的例外

`transcript_status: empty` 的笔记是 ASR 拿不到内容的**占位产物**（保留策略），
本来就没有三段正文。本脚本把它们归到 `KNOWN` 分类并单列，不计入异常数。

## 用法

    /usr/local/bin/python3 scripts/audit_frontmatter.py            # 人读的分组报告
    /usr/local/bin/python3 scripts/audit_frontmatter.py --json     # 机器读，便于 diff 两次运行
    /usr/local/bin/python3 scripts/audit_frontmatter.py --source rss
    /usr/local/bin/python3 scripts/audit_frontmatter.py --self-test  # 注入样本自检，不碰真实产物

退出码：有异常 1，无异常 0（便于接进验收流程）；`--self-test` 用例全过 0，不过 1。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field as dc_field
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

try:
    import yaml
except ImportError:
    sys.exit("需要 PyYAML：请用 /usr/local/bin/python3 运行")

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ / ".agents" / "skills" / "_shared"))

# 被扫描的产物根目录。`--root` 只改它，**不改**上面那行 ——
# 契约 FIELDS 永远取自真实的 skill 目录，否则自检就变成了自己判自己。
ROOT = PROJ
import frontmatter_contract as fc  # noqa: E402

FM_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)

REQUIRED_H2 = ("## TL;DR", "## 要点总结", "## 关键引用")

ENUM_TYPE = {"video", "podcast", "image"}
ENUM_SUBTITLE_SOURCE = {"asr", "ocr", "ai_zh"}          # 空值只在 empty 占位里出现
ENUM_TRANSCRIPT_STATUS = {"ok", "empty"}

# ── G 标签体检层（V1.2 spec §8「标签体检规范」，替代已删的 v3 C8）──────
# 三个阈值与 `scripts/retag.py` 保持一致；**故意不 import 那个模块** ——
# retag 顶层会 sys.path 注入 `_shared` 并 import openai/env，体检脚本不该
# 被这些副作用绑住（自检跑不到真产物时也仍要能跑）。
# ⚠️ 改这三处时必须同步改 retag.py 的同名常量，否则审计口径与产出口径错位。
# 阈值不在本文件定义 —— 与 retag.py 共用 scripts/_tag_limits.py 这一处真源。
# 此前两边各写一份，改一边另一边不跟，会出现「写盘放过 / 体检报警」的口径错位。
# 故意不 import retag：那会在 import 期拉进 openai / env，
# 体检脚本不该为三个数字绑上 LLM SDK 的 import 副作用。
from _tag_limits import MAX_TAGS, MAX_TAG_CHARS, JACCARD_NEAR_DUP  # noqa: E402

DURATION_RE = re.compile(r"^(?:\d+:[0-5]\d|\d{1,2}:[0-5]\d:[0-5]\d)$")

# 控制字符：不含 \t(09) \n(0a) \r(0d) —— 那三个是合法的排版字符
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# 高信号占位符。⚠️ **不查 `description`**：它是上游原文照抄，
# 实测真实产物里有 2 篇正文里真的出现「xxx」「示例」这两个词（一个在引用的
# 示例提示词里，一个是「角色+任务+约束+示例+输出格式」里的"示例"），
# 查了就是误报。
PLACEHOLDER_RE = re.compile(r"(\bTODO\b|\bFIXME\b|\bTBD\b|\{\{|\}\}|PLACEHOLDER)", re.I)
PLACEHOLDER_SKIP_FIELDS = frozenset({"description"})

# H3 章节编号：**数字后面必须跟分隔符**才算章节号。
# ⚠️ 这条判据是踩过坑才定下来的：H3 大量是自由文本，
#   「### 2C 定位」「### 3D 场景」「### 5+1 架构」「### 12 至 18 个月」
#   「### 2026 年趋势」都用数字开头，但**都不是章节编号**。
#   实测全库严格章节号 0 处、朴素正则会误判 19 处。
H3_SECTION_RE = re.compile(r"^###\s+(\d+)\s*[.、)）]\s*\S")

# 本仓 rss 的主键 = `<guid>` 的 sha256 前 8 位，落进 frontmatter 的 `note_id`
ID_SHAPES = {
    "rss": re.compile(r"^[0-9a-f]{8}$"),
}

EXPECTED_HOSTS = {
    "rss": {"www.xiaoyuzhoufm.com", "xiaoyuzhoufm.com"},
}

# 原料主键取哪个字段 + 参与对账的 (frontmatter 字段, 原料取值器)
XREF = {
    "rss": {
        # ⚠️ rss 的 info.json 顶层只有 feed/item/local，**没有扁平的 note_id**
        "key": lambda j: (j.get("item") or {}).get("guid_hash8"),
        "fields": {
            "title": lambda j: (j.get("item") or {}).get("title"),
            "uploader": lambda j: (j.get("feed") or {}).get("title"),
        },
    },
}

SUMMARY_MIN, SUMMARY_MAX = 10, 600


@dataclass
class Finding:
    source: str
    path: str
    code: str
    detail: str
    field: str = ""


@dataclass
class Known:
    """已知且不算 bug 的例外，单列不计入异常。"""
    source: str
    path: str
    code: str
    detail: str


@dataclass
class Report:
    findings: list = dc_field(default_factory=list)
    known: list = dc_field(default_factory=list)
    notes_by_source: dict = dc_field(default_factory=dict)
    # 仅报告项（G3/G4）。**不计入异常数、不影响退出码** —— 它们是观察，
    # 不是缺陷：超长术语与近义标签实测都可能是对的，阻断会造成大量误杀。
    warnings: list = dc_field(default_factory=list)


def notes_of(source: str) -> list[Path]:
    """rss 按节目分子目录（`rss/notes/<节目名>/<标题>-<hash8>.md`），故递归。"""
    return sorted(p for p in (ROOT / source / "notes").rglob("*.md")
                  if p.name != "INDEX.md")


def build_raw_index(source: str) -> dict:
    """按**主键**建索引，不靠文件名。返回 {key: json}，重复键会被记下。"""
    cfg = XREF[source]
    idx, dup = {}, Counter()
    for f in (ROOT / source / "raw").glob("*.info.json"):
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        k = cfg["key"](j)
        if k is None:
            continue
        dup[k] += 1
        idx.setdefault(k, j)
    return idx, dup


def has_body(body: str) -> bool:
    """正文有实质内容吗？

    ⚠️ 判据是**「要点总结」段里有字**，不是「文件有 frontmatter」或「正文非空」。
    G1（漏跑 retag）要用它把两类 `tags: []` 分开：内容好的却没标签 = 漏跑；
    内容本来就空（ASR 占位）= tags 空是对的。只看「正文非空」会把
    占位产物里那行标题也判成有内容。
    """
    m = re.search(r"^##\s+要点总结\s*$(.*?)(?=^##\s|\Z)", body, re.M | re.S)
    return bool(m and m.group(1).strip())


def char_jaccard(a: str, b: str) -> float:
    """字符集 Jaccard —— 近义标签的相似度度量。

    为什么用**字符**而不是词/编辑距离：`tags` 是中英混排的短术语，
    中文没有空格可切词，词级 Jaccard 对「模型管理」/「AI 管理」这类
    跨语言近义会算出 0。字符集是这类短串上最稳的无依赖近似。
    门限 0.6 偏严（宁可漏报也不误杀），因为 G4 本就只报告不阻断。
    """
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def check_tags(fm: dict, body: str, rel: str, source: str, rep: Report) -> None:
    """G1~G4 标签质量检查（spec §8）。调用方须已确认 `fm` 是 dict。

    报警（G1/G2）进 `rep.findings`，影响退出码；仅报告（G3/G4）进
    `rep.warnings`，不参与异常计数与退出码 —— 详见 G3/G4 各自的注释。
    """
    tags = fm.get("tags")
    if not isinstance(tags, list):
        return                        # 形态问题已由 list_field_type 报出，不重复
    tags = [t for t in tags if isinstance(t, str)]
    ts = fm.get("transcript_status")

    # ── G1 漏跑 retag（报警）────────────────────────────────────
    # ⚠️ 两种「tags 为空」必须分开，这是本检查唯一的判断难点：
    #   ok   + 有正文 → 内容是好的，标签本该有，空 = **漏跑了 retag**；
    #   empty（占位）  → ASR 没识别出语音，内容本来就空，tags 空是**对的**。
    # 判据若写成「有正文且 tags 空」而不带 transcript_status 占位排除，
    # 存量里那批 empty 占位笔记会被整批误报成漏跑。
    if not tags and ts == "ok" and has_body(body):
        rep.findings.append(Finding(
            source, rel, "G1_tags_missing",
            f"transcript_status=ok 且有正文，但 tags 为空 —— 漏跑 retag，"
            f"该补跑 scripts/retag.py", "tags"))

    # ── G2 数量上限（报警）──────────────────────────────────────
    if len(tags) > MAX_TAGS:
        rep.findings.append(Finding(
            source, rel, "G2_tags_over_limit",
            f"标签 {len(tags)} 个，超出上限 {MAX_TAGS} 个（与 retag.py MAX_TAGS 一致）",
            "tags"))

    # ── G3 超长标签（仅报告，不阻断）────────────────────────────
    # ⚠️ 不阻断是**实测结论**，不是省事：LangGraph(9) / ReAct Agent(11)
    # 这类是合理术语，打标侧两轮返工证明截断（"Session管"）
    # 和丢弃都比原样保留更糟。报告出来让人看，程序不动它。
    long_tags = [t for t in tags if len(t) > MAX_TAG_CHARS]
    if long_tags:
        rep.warnings.append(Finding(
            source, rel, "G3_tag_too_long",
            f"{len(long_tags)} 个标签超过 {MAX_TAG_CHARS} 字符"
            f"（按实测只报告、不截断不丢弃）：{long_tags}", "tags"))

    # ── G4 近义重复（仅报告，不阻断）────────────────────────────
    # 标签 ≤10 个，两两比对最多 45 次，代价可忽略。
    # 完全相同的重复已由 retag 去重，这里报的近义更值得关注。
    near = []
    for i, a in enumerate(tags):
        for b in tags[i + 1:]:
            s = char_jaccard(a, b)
            if s >= JACCARD_NEAR_DUP:
                near.append(f"{a} ≈ {b}（{s:.2f}）")
    if near:
        rep.warnings.append(Finding(
            source, rel, "G4_tag_near_dup",
            f"{len(near)} 对标签字符 Jaccard ≥ {JACCARD_NEAR_DUP}："
            + "；".join(near[:8]), "tags"))


def audit_file(path: Path, source: str, raw_idx: dict, rep: Report):
    rel = str(path.relative_to(ROOT))
    text = path.read_text(encoding="utf-8")

    def bad(code, detail, fld=""):
        rep.findings.append(Finding(source, rel, code, detail, fld))

    m = FM_RE.match(text)
    if not m:
        bad("fm_missing", "没有 frontmatter（文件开头不是 --- 块）")
        return
    fm_body, body = m.group(1), text[m.end():]

    # ── A 结构层 ────────────────────────────────────────────
    # ⚠️ **顺序是硬要求：原文级检查必须全部前置到 `yaml.safe_load()` 之前。**
    # 曾踩过：原本这段原文检查写在解析的 `return` 之后，于是
    # 「解析失败」的文件永远走不到它 —— 体检对最该看的样本恰好是瞎的。
    # 而「解析失败」和「含未闭合引号标量」完全可能同时成立（前者常由后者
    # 引发的下游错误造成），两者是**并列**关系，不是包含关系。
    # 判据：**只有依赖解析结果的检查（值级 / 对账）才可以放在解析之后。**

    # ⚠️ 裸换行**不会**让 YAML 报错，它会把换行+缩进静默折叠成一个空格
    # （实测 `"第一行\n  第二行"` → `'第一行 第二行'`，解析成功、内容被改）。
    # 所以「能不能解析」抓不到这一类，只能**看原文**：加了引号的标量必须
    # 在同一物理行内闭合，否则就是没转义的裸换行。
    for ln_no, line in enumerate(fm_body.split("\n"), 1):
        for opener in ('"', "'"):
            mm = re.match(rf"^[A-Za-z_]+:\s*{re.escape(opener)}(.*)$", line)
            if not mm:
                continue
            tail = mm.group(1)
            if not tail.endswith(opener):
                bad("fm_unterminated_scalar",
                    f"frontmatter 第 {ln_no} 行的引号标量未在本行闭合 —— "
                    f"值里有**未转义的裸换行**，YAML 会静默折叠成空格（内容被改但不报错）")
            break

    # ⚠️ 必须兜 `Exception` 而不是 `yaml.YAMLError`：PyYAML 解析
    # `published_at: 2026-13-45` 这类越界日期时，异常来自
    # `construct_yaml_timestamp` 里的 `datetime.date(...)`，是**裸 ValueError**，
    # 不会被 YAMLError 接住。只捕 YAMLError 的话，一个坏文件会让整轮体检
    # 直接崩掉 —— 体检工具自己被坏数据打垮，等于没有体检。
    try:
        fm = yaml.safe_load(fm_body)
    except Exception as e:
        bad("fm_unparsable", f"yaml.safe_load 抛异常 → {type(e).__name__}: {str(e).splitlines()[0]}")
        return
    if not isinstance(fm, dict):
        bad("fm_not_dict", f"frontmatter 解析结果不是 dict（是 {type(fm).__name__}）")
        return

    for problem in fc.check(fm):
        bad("fm_field_set", problem)
    if tuple(fm.keys()) != fc.FIELDS:
        bad("fm_field_order", f"字段顺序与 FIELDS 不一致：{list(fm.keys())}")

    # ── 已知例外：ASR 占位笔记 ─────────────────────────────
    is_placeholder = fm.get("transcript_status") == "empty"
    if is_placeholder:
        rep.known.append(Known(source, rel, "asr_placeholder",
                               "transcript_status=empty：无逐字稿的占位产物，"
                               "按保留策略保留，正文段豁免"))

    # ── B 值级 ──────────────────────────────────────────────
    nid = fm.get("note_id")
    if not isinstance(nid, str) or not nid:
        bad("note_id_empty", f"note_id 为空或非 str（{nid!r}）", "note_id")
    else:
        if not ID_SHAPES[source].match(nid):
            bad("note_id_shape", f"note_id 不符合 {source} 形态（{nid!r}）", "note_id")
        if not path.name.endswith(f"-{nid}.md"):
            bad("note_id_filename", f"文件名与 note_id 不一致：{path.name} vs {nid}", "note_id")

    v = fm.get("type")
    if v not in ENUM_TYPE:
        bad("type_enum", f"type 非法枚举（{v!r}），合法={sorted(ENUM_TYPE)}", "type")

    v = fm.get("subtitle_source")
    if v == "" and is_placeholder:
        pass                                        # 占位产物允许空
    elif v not in ENUM_SUBTITLE_SOURCE:
        bad("subtitle_source_enum",
            f"subtitle_source 非法枚举（{v!r}），合法={sorted(ENUM_SUBTITLE_SOURCE)}",
            "subtitle_source")

    v = fm.get("transcript_status")
    if v not in ENUM_TRANSCRIPT_STATUS:
        bad("transcript_status_enum",
            f"transcript_status 非法枚举（{v!r}），合法={sorted(ENUM_TRANSCRIPT_STATUS)}",
            "transcript_status")

    dur = fm.get("duration")
    if dur == "":
        if fm.get("type") != "image":
            bad("duration_empty", f"duration 为空，但 type={fm.get('type')!r}（仅 image 允许空）",
                "duration")
    elif not isinstance(dur, str) or not DURATION_RE.match(dur):
        bad("duration_format", f"duration 不合 M:SS / H:MM:SS（{dur!r}）", "duration")

    pa = fm.get("published_at")
    if pa != "":
        try:
            if not isinstance(pa, str):
                raise TypeError
            date.fromisoformat(pa)
        except Exception:
            bad("published_at_format", f"published_at 不能被 date.fromisoformat 解析（{pa!r}）",
                "published_at")

    # ⚠️ 值级检查必须**独立于 yaml 解析**
    ga = fm.get("generated_at")
    try:
        if not isinstance(ga, str):
            raise TypeError
        datetime.fromisoformat(ga)
    except Exception:
        bad("generated_at_not_iso",
            f"generated_at 不能被 datetime.fromisoformat 解析（{ga!r}）"
            + ("　← 值里含多余引号（一次性迁移缺陷）" if isinstance(ga, str) else ""),
            "generated_at")

    s = fm.get("summary")
    if not isinstance(s, str) or not s.strip():
        bad("summary_empty", "summary 为空", "summary")
    elif not (SUMMARY_MIN <= len(s) <= SUMMARY_MAX):
        bad("summary_length", f"summary 长度 {len(s)} 超出 [{SUMMARY_MIN}, {SUMMARY_MAX}]", "summary")

    url = fm.get("source_url")
    if not isinstance(url, str) or not url:
        bad("source_url_empty", "source_url 为空", "source_url")
    else:
        pu = urlparse(url)
        if pu.scheme not in ("http", "https") or not pu.netloc:
            bad("source_url_format", f"source_url 不是合法 URL（{url!r}）", "source_url")
        elif pu.hostname not in EXPECTED_HOSTS[source]:
            bad("source_url_host",
                f"source_url 域名与源不符：{pu.hostname} 不属于 {source} {sorted(EXPECTED_HOSTS[source])}",
                "source_url")

    for k in fc.LIST_FIELDS:
        lv = fm.get(k)
        if not isinstance(lv, list):
            bad("list_field_type", f"{k} 应为 list，实际 {type(lv).__name__}（{lv!r}）", k)
        elif any((not isinstance(x, str)) or not x.strip() for x in lv):
            bad("list_field_item", f"{k} 含非 str 或空串项：{lv!r}", k)

    for k, val in fm.items():
        items = val if isinstance(val, list) else [val]
        for it in items:
            if not isinstance(it, str):
                continue
            if CTRL_RE.search(it):
                bad("control_char",
                    f"{k} 含控制字符 "
                    f"{[hex(ord(c)) for c in CTRL_RE.findall(it)][:5]}", k)
            if k not in PLACEHOLDER_SKIP_FIELDS and PLACEHOLDER_RE.search(it):
                bad("placeholder", f"{k} 残留占位符字面量（{it[:40]!r}）", k)

    # ── G 标签体检层（G1~G4，V1.2 spec §8）──────────────────────
    # 放在对账层**之前**：`xref_raw_missing` 会 `return`，放在它后面的话，
    # 原料索引缺失的笔记会顺带丢掉标签检查（又一处「体检对最该看的样本是瞎的」）。
    check_tags(fm, body, rel, source, rep)

    # ── C 正文层 ────────────────────────────────────────────
    if not is_placeholder:
        for h in REQUIRED_H2:
            if h not in body:
                bad("h2_missing", f"正文缺少固定章节 {h}")

    nums = []
    for line in body.split("\n"):
        mm = H3_SECTION_RE.match(line.strip())
        if mm:
            nums.append(int(mm.group(1)))
    if nums and nums != list(range(1, len(nums) + 1)):
        bad("h3_numbering", f"H3 章节编号不连续：{nums}")

    # ── D 对账层 ────────────────────────────────────────────
    if not isinstance(nid, str) or nid not in raw_idx:
        bad("xref_raw_missing", f"raw/*.info.json 里找不到主键 {nid!r}", "note_id")
        return
    raw = raw_idx[nid]
    for fld, getter in XREF[source]["fields"].items():
        got = fm.get(fld)
        want = getter(raw)
        if (got or "") != (want or ""):
            bad("xref_mismatch",
                f"{fld} 与原料不一致：frontmatter={got!r} vs info.json={want!r}", fld)


def run(sources: list[str]) -> Report:
    rep = Report()
    for source in sources:
        raw_idx, dup = build_raw_index(source)
        for k, c in dup.items():
            if c > 1:
                rep.findings.append(Finding(
                    source, "raw/*.info.json", "raw_duplicate_key",
                    f"原料主键重复 {c} 次：{k}（对账只会用到第一份）"))
        notes = notes_of(source)
        rep.notes_by_source[source] = len(notes)
        for p in notes:
            audit_file(p, source, raw_idx, rep)
    return rep


LAYER = {
    "fm": "A 结构层 · frontmatter 结构",
    "note_id": "B 值级 · 主键",
    "type": "B 值级 · 枚举",
    "subtitle_source": "B 值级 · 枚举",
    "transcript_status": "B 值级 · 枚举",
    "duration": "B 值级 · 时长",
    "published_at": "B 值级 · 发布日期",
    "generated_at": "B 值级 · 生成时间",
    "summary": "B 值级 · 摘要",
    "source_url": "B 值级 · 来源链接",
    "list_field": "B 值级 · 列表字段",
    "control_char": "B 值级 · 字符卫生",
    "placeholder": "B 值级 · 占位符",
    "h2": "C 正文层 · 固定章节",
    "h3": "C 正文层 · 编号",
    "G": "G 标签体检层 · G1 漏跑 / G2 数量（报警）",
    "xref": "D 对账层 · frontmatter ↔ info.json",
    "raw": "D 对账层 · 原料索引",
}

WARN_LAYER = {
    "G3_tag_too_long": "G 标签体检层 · G3 超长标签（仅报告）",
    "G4_tag_near_dup": "G 标签体检层 · G4 近义重复（仅报告）",
}

CODE_FIELD = {
    "fm_missing": "fm", "fm_unparsable": "fm", "fm_not_dict": "fm",
    "fm_field_set": "fm", "fm_field_order": "fm", "fm_unterminated_scalar": "fm",
    "note_id_empty": "note_id", "note_id_shape": "note_id", "note_id_filename": "note_id",
    "type_enum": "type", "subtitle_source_enum": "subtitle_source",
    "transcript_status_enum": "transcript_status",
    "duration_empty": "duration", "duration_format": "duration",
    "published_at_format": "published_at", "generated_at_not_iso": "generated_at",
    "summary_empty": "summary", "summary_length": "summary",
    "source_url_empty": "source_url", "source_url_format": "source_url",
    "source_url_host": "source_url",
    "list_field_type": "list_field", "list_field_item": "list_field",
    "control_char": "control_char", "placeholder": "placeholder",
    "h2_missing": "h2", "h3_numbering": "h3",
    "G1_tags_missing": "G", "G2_tags_over_limit": "G",
    "xref_raw_missing": "xref", "xref_mismatch": "xref",
    "raw_duplicate_key": "raw",
}


def print_report(rep: Report, show_known: bool, max_per_group: int) -> None:
    total = sum(rep.notes_by_source.values())
    srcs = "  ".join(f"{k}={v}" for k, v in sorted(rep.notes_by_source.items()))
    print(f"frontmatter 体检　共 {total} 篇（{srcs}）")
    if total == 0:
        # 干净 clone 里还没有产物目录是常态。给一句可执行的提示，
        # 而不是让使用者对着一句「✅ 无异常」怀疑体检是不是没跑。
        print("ℹ️  没有可体检的笔记：先确认 <repo>/rss/notes/ 下已有笔记产物，"
              "或用 --root <样本目录> 指定别的扫描根目录。")

    groups = defaultdict(list)
    for f in rep.findings:
        groups[(CODE_FIELD.get(f.code, "other"), f.code)].append(f)

    if not groups:
        print("\n（无异常明细）")
    for layer_key in ("fm", "note_id", "type", "subtitle_source", "transcript_status",
                      "duration", "published_at", "generated_at", "summary", "source_url",
                      "list_field", "control_char", "placeholder", "h2", "h3", "G",
                      "xref", "raw"):
        for (key, code), items in sorted(groups.items()):
            if key != layer_key:
                continue
            print(f"\n🔴 {LAYER.get(key, key)}　[{code}]　{len(items)} 处")
            for f in items[:max_per_group]:
                fld = f"　<{f.field}>" if f.field else ""
                print(f"   {f.path}{fld}\n      {f.detail}")
            if len(items) > max_per_group:
                print(f"   …… 另有 {len(items) - max_per_group} 处同类（--all 展开）")

    if show_known and rep.known:
        c = Counter((k.source, k.code) for k in rep.known)
        print(f"\nℹ️  已知例外（不计异常）")
        for (src, code), n in sorted(c.items()):
            print(f"   [{code}] {src}　{n} 处")
        for k in rep.known[:3]:
            print(f"      例：{k.path[:70]}\n          {k.detail}")

    if rep.warnings:
        print(f"\n⚠️  仅报告项（不计入异常、不影响退出码）　{len(rep.warnings)} 处")
        wg = defaultdict(list)
        for f in rep.warnings:
            wg[f.code].append(f)
        for code, items in sorted(wg.items()):
            print(f"\n{WARN_LAYER.get(code, 'G 标签体检层')}　[{code}]　{len(items)} 处")
            for f in items[:max_per_group]:
                fld = f"　<{f.field}>" if f.field else ""
                print(f"   {f.path}{fld}\n      {f.detail}")
            if len(items) > max_per_group:
                print(f"   …… 另有 {len(items) - max_per_group} 处同类（--all 展开）")

    n = len(rep.findings)
    kinds = len({f.code for f in rep.findings})
    if n == 0:
        print("\n✅ 无异常")
    else:
        print(f"\n🔴 {n} 处异常（{kinds} 类）")


# ── 注入式自检（--self-test）─────────────────────────────────────
# 上一轮的注入式自检是**临时在 shell 里跑的、没进 git**，
# 所以「全部通过」既无法复现、也没人能在后续改动后重跑 —— 校验资产本身丢了。
#
# ⚠️ 早先自检**失效**的原因不是用例写错，是**用例没能触发那条路径**：
#   当时原文检查（fm_unterminated_scalar）写在 `yaml.safe_load()` 失败后的
#   `return` 之后 → 只要 frontmatter 解析失败，就永远走不到它。
#   而两个用例都是「能解析」的形状，于是这条分支一次都没被执行过，
#   门禁看着是绿的，实际从没看过该看的样本。
#   所以这次两类用例**分开构造**，并分别断言**实际命中的 code 名**。

def _selftest_fixture(note_id: str, desc_lines: list[str], over: dict = None) -> tuple:
    """造一篇样例笔记，返回 (markdown 文本, info.json dict)。

    `desc_lines` 是 description 在 frontmatter 里的**原文行**（未经序列化）——
    用例正是靠给出不同的原文形状来构造不同缺陷。

    `over` 是**整行覆盖**：{字段名: 完整 YAML 行}，按行首 `字段名:` 精确替换。
    用来让同一个 fixture 造出 `transcript_status` 的 ok / empty 两种形状
    —— G1 的判据正是这两种形状的**差集**，不换形状就验不到那条分界。

    原料 info.json 按本仓 rss 的三层结构（feed / item）构造，主键在
    `item.guid_hash8` —— 与真实产物同形，避免自检在一个不存在的形状上通过。
    """
    title, show = "自检样本", "自检播客"
    raw = {
        "feed": {"title": show},
        "item": {"guid_hash8": note_id, "title": title},
    }
    fm = [
        f"note_id: {note_id}",
        f"title: {title}",
        f"uploader: {show}",
        'uploader_id: ""',
        'speaker: ""',
        "type: podcast",
        'duration: "1:00"',
        'published_at: "2026-10-08"',
        'source_url: "https://www.xiaoyuzhoufm.com/episode/self-test-0001"',
        "subtitle_source: asr",
        "transcript_status: ok",
        "source_tags: []",
        "tags: []",
        "summary: 自检用样本摘要，内容固定，仅用于触发结构与原文两类检查。",
        'generated_at: "2026-10-08T00:00:00+08:00"',
        *desc_lines,
        f"show_name: {show}",
    ]
    for key, line in (over or {}).items():
        fm = [line if ln.startswith(f"{key}:") else ln for ln in fm]
    md = ("---\n" + "\n".join(fm) + "\n---\n\n"
          "## TL;DR\n\n自检样本。\n\n## 要点总结\n\n- 自检\n\n## 关键引用\n\n> 自检\n")
    return md, raw


# (用例名, note_id, description 原文行, 必须命中的 code, 必须**不**命中的 code, 字段覆盖)
SELFTEST_CASES = (
    (
        "可解析但含未闭合引号标量（YAML 静默折叠那种形状）",
        "a1b2c301",
        ['description: "第一行', '第二行"'],
        {"fm_unterminated_scalar"},
        {"fm_unparsable"},
        {},
    ),
    (
        # ⚠️ 第二行**刻意不带收尾引号**：引号一个都不闭合，YAML 才会连
        # `---` 结束行一起吞进标量里报错。用例 1 才是「有收尾引号、
        # 能解析但被静默折叠」的那一种 —— 两例只差这一个字符。
        "解析失败且含未闭合引号标量（两类必须**同时**报出）",
        "a1b2c302",
        ['description: "第一行', '第二行'],
        {"fm_unparsable", "fm_unterminated_scalar"},
        set(),
        {},
    ),
    # ── G1 的两个分支 ───────────────────────────────────────
    # 这两例**形状只差 transcript_status 一个字段**，结论却必须相反。
    # 只测「ok → 报」不测「empty → 不报」等于没验判据：一个写成
    # 「有正文且 tags 空」的 G1 照样能过前者，而存量里那批 empty
    # 占位笔记会被整批误报成漏跑。
    (
        "G1 漏跑：transcript_status=ok + 有正文 + tags 空 → 必须报",
        "a1b2c303",
        ['description: ""'],
        {"G1_tags_missing"},
        set(),
        {},
    ),
    (
        "G1 不误报：transcript_status=empty（占位）+ 有正文 + tags 空 → 必须不报",
        "a1b2c304",
        ['description: ""'],
        set(),
        {"G1_tags_missing"},
        {"transcript_status": "transcript_status: empty"},
    ),
)


def run_selftest() -> int:
    """在临时目录里造注入样本跑一遍，打印每个用例**实际命中**的 code。"""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # 按真实目录形状（notes 下再分子目录）造，顺带验 rglob 扫描
        notes = root / "rss" / "notes" / "自检播客"
        raws = root / "rss" / "raw"
        notes.mkdir(parents=True)
        raws.mkdir(parents=True)
        for case in SELFTEST_CASES:
            nid, desc_lines = case[1], case[2]
            md, raw = _selftest_fixture(nid, desc_lines, case[5])
            (notes / f"自检样本-{nid}.md").write_text(md, encoding="utf-8")
            (raws / f"自检样本-{nid}.info.json").write_text(
                json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        global ROOT
        saved, ROOT = ROOT, root
        try:
            rep = run(["rss"])
        finally:
            ROOT = saved

    by_path = defaultdict(set)
    for f in rep.findings:
        by_path[f.path].add(f.code)

    passed = 0
    print(f"注入式自检　{len(SELFTEST_CASES)} 个用例")
    for name, nid, _desc, expect, forbid, _over in SELFTEST_CASES:
        got = by_path.get(f"rss/notes/自检播客/自检样本-{nid}.md", set())
        missing, leaked = expect - got, forbid & got
        ok = not missing and not leaked
        passed += ok
        print(f"\n  {'✅' if ok else '❌'} {name}")
        print(f"     实际命中 code：{', '.join(sorted(got)) or '（无）'}")
        if missing:
            print(f"     应报未报：{', '.join(sorted(missing))}")
        if leaked:
            print(f"     不该报却报了：{', '.join(sorted(leaked))}")

    print(f"\n{'✅' if passed == len(SELFTEST_CASES) else '❌'} {passed}/{len(SELFTEST_CASES)} 通过")
    return 0 if passed == len(SELFTEST_CASES) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="笔记 frontmatter 体检")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--source", action="append", choices=sorted(XREF), help="只查某个源（可重复）")
    ap.add_argument("--all", action="store_true", help="异常明细全部展开，不截断")
    ap.add_argument("--hide-known", action="store_true", help="不显示已知例外")
    ap.add_argument("--root", help="改扫描根目录（自检用：指向含坏 frontmatter 的样本树）")
    ap.add_argument("--self-test", action="store_true",
                    help="跑注入式自检（临时样本树，不碰真实产物）后退出")
    args = ap.parse_args()

    if args.self_test:
        return run_selftest()

    global ROOT
    if args.root:
        ROOT = Path(args.root).resolve()

    rep = run(args.source or sorted(XREF))
    if args.json:
        print(json.dumps({
            "notes_by_source": rep.notes_by_source,
            "finding_count": len(rep.findings),
            "findings": [asdict(f) for f in rep.findings],
            "known": [asdict(k) for k in rep.known],
            "warnings": [asdict(w) for w in rep.warnings],
            "pending_retag": sum(1 for f in rep.findings
                                 if f.code == "G1_tags_missing"),
        }, ensure_ascii=False, indent=2))
    else:
        print_report(rep, not args.hide_known, 10**9 if args.all else 5)
    return 1 if rep.findings else 0


if __name__ == "__main__":
    raise SystemExit(main())