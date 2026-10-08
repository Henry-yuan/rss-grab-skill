#!/usr/bin/env python3
"""用产物正文重新生成 frontmatter 的 `tags`。

读已落盘的笔记，按其 `## TL;DR` + `## 要点总结` 两节调用 LLM 重新打标签。
笔记生成管线本身不产出 tags（标签与正文是两次独立生成），本脚本负责补上这一步。

范围：只喂 `## TL;DR` + `## 要点总结` 全量（不截断），不喂 source_tags
（发布者自打标签不可信）、不喂 title/uploader（避免复读标题）。

默认 dry-run：只报告不写盘。加 --apply 才落盘，且落盘后逐字节断言正文未变。
"""
import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".agents/skills/_shared"))
sys.path.insert(0, str(ROOT / "scripts"))
from env import load_env  # noqa: E402
# 阈值不在本文件定义 —— 与体检脚本共用 _tag_limits 这一处真源
from _tag_limits import MAX_TAGS, MAX_TAG_CHARS  # noqa: E402

FM_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)

# 提示词按 Henry 2026-10-08 要求定稿：
#  · 标签语言不强制中文（"RAG" / "FDE" / "AI Agent" 保留原写法）
#  · 不给标签内容案例（怕过度引导那套词），只给 JSON 结构示例
#  · 不喂 source_tags，但用第 2 条从正面压制"复读博主标签"
SYS_PROMPT = """你是内容标注助手。阅读一篇中文内容的摘要与要点，为它打标签。

要求：
1. 标签描述这篇内容实际谈论的主题、领域、话题和涉及的角色，不是泛化的通用词。
2. 从正文实际论述出发推断，不复述博源自打的标签，不照搬标题里的说法。
3. 数量不超过 10 个。如果内容涵盖的独立主题不足，宁可少给几个，也不要把同一个主题拆成不同的说法，也不要退回到只有共同大类、没有区分度的宽泛词。每个标签都应当能对应到内容中一个具体的论述。
4. 每个标签 2 到 8 个字符。使用内容中术语的原始写法，技术专有名词保留其常见中英文形式，不强行翻译。
5. 只依据正文内容判断，不推测正文没有的信息。
6. 标签描述的是内容本身谈论的对象。如果信息很少，就给出能够确定的部分。

输出格式：
{"tags": ["标签一", "标签二", "标签三"]}

只输出这个 JSON 对象，不输出任何其他文字。"""


# 429 限流的恢复窗口远长于普通网络抖动（实测 10 并发下 212 篇耗尽 3 次
# 1s/2s 退避，17 篇失败）。这里给到分钟级，总退避约 160s。
BACKOFF_429 = (5, 20, 45, 90)

# 正文自述「内容缺失」的笔记 —— 2026-10-08 抽样实测命中 1 篇
# （做视频-装这6个就够了-6a351e84，正文写着"没有可供总结的视频正文"，
#  被打出「转写内容缺失 / 无法提取主题」这种描述输入缺陷的元标签）。
# 这类不该打标签：source_tags 反而是真实主题，正文没有可标注的对象。
FAKE_BODY = re.compile(
    r"没有可供总结|无可提取|正文缺失|未包含观点|无法进一步整理|"
    r"转写内容缺失|未识别到有效内容|内容总结受限|没有正文")


def extract_sections(text: str) -> dict:
    """切出所有 H2 段落 -> {标题: 内容}"""
    m = FM_RE.match(text)
    body = text[m.end():] if m else text
    parts = re.split(r"^##\s+(.+?)\s*$", body, flags=re.M)
    return {parts[i].strip(): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}


def build_input(text: str) -> str:
    """TL;DR + 要点总结 全量。返回空串表示该篇无正文（ASR 占位产物）。"""
    sec = extract_sections(text)
    if not sec.get("要点总结"):
        return ""
    return (sec.get("TL;DR", "") + "\n\n" + sec.get("要点总结", "")).strip()


def parse_tags(raw: str) -> list:
    """解析模型输出。effort=high 会有思考输出，content 可能被污染，故多级兜底。"""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("空响应")
    try:
        obj = json.loads(raw)
    except Exception:
        # 兜底 1：截取第一个 {...} 块（思考输出常把 JSON 挤到末尾）
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            raise ValueError(f"无 JSON 可解析，响应前 120 字：{raw[:120]}")
        obj = json.loads(m.group(0))
    if isinstance(obj, list):
        tags = obj
    elif isinstance(obj, dict):
        tags = obj.get("tags") or []
    else:
        raise ValueError(f"JSON 顶层既不是 dict 也不是 list：{type(obj).__name__}")
    if not isinstance(tags, list):
        raise ValueError(f"tags 不是数组：{type(tags).__name__}")
    return [str(t).strip() for t in tags if str(t).strip()]


def validate(tags: list) -> tuple:
    """去重（唯一保留的干预）+ 超长标签仅记录、不改动。

    ⚠️ 2026-10-08 两轮返工记录：
    1. 原先 `t[:MAX_TAG_CHARS]` **截断**，"Session管理"→"Session管"、
       "Generation"→"Generati"，产出的是残缺词，比丢弃更糟。
    2. 改成"超长就丢弃"同样是错的 —— **程序没有判断依据**：
       `Session管理`(9) / `SSH认证` 在中英混排的标签里都完全合理，
       字符数上限是按纯中文设的，套到英文术语上就不成立。

    所以这里**只去重**：`["A","A"]`→`["A"]` 不损失任何信息，是格式修正；
    截断和丢弃都会丢信息，都不做。长度由提示词作软约束，程序不硬管。
    超长标签返回给调用方，仅用于报告可观测性。

    ⚠️ **长度不硬管，但数量硬管**：两者害处不同。超长是标签本身的性质，
    程序没有判断依据（上面两点返工记录）；超数量则是模型为凑数把一个主题
    拆成几种说法，正是宪法第 15 条要防的退化 —— 所以 `MAX_TAGS` 是阻断项。
    """
    out, seen, long_ones = [], set(), []
    for t in tags:
        if len(t) > MAX_TAG_CHARS:
            long_ones.append(t)
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    if not out:
        # 两种情况都到这里：模型直接返回空数组，或返回值全是重复项。
        # 都必须当失败 —— 静默写入 tags: [] 会让该篇看起来"正常但没标签"，
        # 与「已标记抓取但无产物」是同一族静默失败。
        raise ValueError(f"模型未产出可用标签，原始：{tags}")
    # ── G2 数量上限（V1.2 spec §8「标签体检规范」）─────────────────
    # 判据用**去重后**的 out，与 audit 侧的 G2（查 frontmatter 里的 tags）
    # 口径一致 —— 落盘的就是 out，拿原始 tags 去判会出现
    # 「10 个重复项共 30 条」这种审计放过、写盘却拦住的错位。
    # 措辞与上面的 G1 分开：G1 是「一个都没有」，这里是「给太多了」。
    if len(out) > MAX_TAGS:
        raise ValueError(f"标签数量超上限：{len(out)} 个 > MAX_TAGS={MAX_TAGS}，"
                         f"去重前 {len(tags)} 个：{tags}")
    return out, long_ones


def pick_files_by_ids(source: str, ids: list[str]) -> list:
    """按 frontmatter 的 `note_id` 精确过滤。

    **主键是 frontmatter 的 `note_id` 字段，不是文件名**：笔记文件名要过一遍
    标题派生的 sanitize，标题一改文件名就失效。`note_id` 是信头契约里的稳定主键，
    与文件名解耦，取值不随标题变化。

    本仓的 note_id 取值为 guid 的 sha256 前 8 位（见 build_frontmatter.py）。

    与 `pick_files`（抽样）不同：这里**不过滤无正文**，找不到 / 无正文的都交给
    `one()` 按「无正文，跳过」如实报出来 —— 调用方（v3 薄壳）传进来的就是刚生成
    的那几篇，静默少报一篇会让「以为打上了」和「实际没打上」无法区分。
    """
    d = ROOT / source / "notes"
    want = {i.strip() for i in ids if i and i.strip()}
    if not want:
        return []
    import yaml
    hits = []
    for p in sorted(d.rglob("*.md")):
        if p.name == "INDEX.md":
            continue
        m = FM_RE.match(p.read_text(encoding="utf-8"))
        if not m:
            continue
        fm = yaml.safe_load(m.group(1)) or {}
        if str(fm.get("note_id") or "").strip() in want:
            hits.append(p)
    return hits


def pick_files(source: str, n: int) -> list:
    """抽样：已有 tags / 无 tags 各一半，且覆盖不同长度档。"""
    d = ROOT / source / "notes"
    import yaml
    with_tags, without = [], []
    for p in sorted(d.rglob("*.md")):
        if p.name == "INDEX.md":
            continue
        text = p.read_text(encoding="utf-8")
        m = FM_RE.match(text)
        if not m:
            continue
        fm = yaml.safe_load(m.group(1)) or {}
        if not build_input(text):
            continue  # ASR 占位产物，无正文
        (with_tags if fm.get("tags") else without).append((len(build_input(text)), p))
    # 每组按长度排序后等距取样，覆盖短中长
    def spread(items, k):
        items.sort(key=lambda x: x[0])
        if not items or k == 0:
            return []
        idx = [round(i * (len(items) - 1) / (k - 1)) if k > 1 else 0 for i in range(k)]
        return [items[i][1] for i in sorted(set(idx))]
    k = n // 2
    return spread(with_tags, k) + spread(without, n - k)


def write_tags(path: Path, tags: list) -> None:
    """只改 frontmatter 的 tags 字段，正文逐字节不动。

    不用字符串拼接改 frontmatter —— 2026-10-05 的 `["[]"]` 损坏就是这么来的
    （str([]) 被 YAML 解析成单元素列表）。走 frontmatter_contract.dump()，
    与生产路径同一套写出逻辑。
    """
    import yaml
    sys.path.insert(0, str(ROOT / ".agents/skills/_shared"))
    import frontmatter_contract as fc

    text = path.read_text(encoding="utf-8")
    m = FM_RE.match(text)
    if not m:
        raise ValueError("frontmatter 锚定失败，拒绝写入")
    body_before = text[m.end():]
    fm_before = yaml.safe_load(m.group(1)) or {}

    fm_after = dict(fm_before)
    fm_after["tags"] = tags
    new_text = fc.dump(fm_after) + "\n" + body_before

    # ── 三道断言，任一不过就拒绝写入 ──
    mn = FM_RE.match(new_text)
    if not mn:
        raise ValueError("写回后 frontmatter 无法锚定，拒绝写入")
    if new_text[mn.end():] != body_before:
        raise ValueError("正文发生变化，拒绝写入")
    fm_check = yaml.safe_load(mn.group(1)) or {}
    # 判据是「有没有 tags 之外的改动」，不是「改动集合是否恰好等于 {tags}」——
    # 后者会让幂等重写（tags 没变、差异集为空）误报失败。
    changed = {k for k in set(fm_before) | set(fm_check)
               if fm_before.get(k) != fm_check.get(k)}
    if changed - {"tags"}:
        raise ValueError(f"除 tags 外还有字段被改动：{changed - {'tags'}}，拒绝写入")

    path.write_text(new_text, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="rss")
    # --sample 与 --ids 互斥：抽样是「挑一批看看」，--ids 是「精确打这几篇」。
    # 两者语义相反（一个按有无 tags / 长度分层，一个按主键点名），同时给会
    # 静默丢掉 --ids 的精确性，故用互斥组挡掉而不是靠 if/else 优先级。
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--sample", type=int, default=6, help="抽样篇数，0 = 全量")
    grp.add_argument("--ids", help="逗号分隔的 note_id，只处理这些（与 --sample 互斥）")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--apply", action="store_true", help="真正写盘；默认只报告")
    ap.add_argument("--skip-modified", action="store_true",
                    help="跳过 git 中已修改的笔记（中断续跑用，避免重复烧 token）")
    ap.add_argument("--effort", default="high")
    ap.add_argument("--max-completion", type=int, default=100000)
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args()

    # skill 目录名与笔记目录名不一致，必须显式映射：rss/ -> rss-grab/
    # （本仓只含 rss 一个源）
    SKILL_DIR = {"rss": "rss-grab"}
    if args.source not in SKILL_DIR:
        sys.exit(f"未知 source：{args.source}，可选：{list(SKILL_DIR)}")
    load_env(ROOT / ".agents/skills" / SKILL_DIR[args.source] / "scripts")
    # 本仓的 env.load_env 已校验 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 三项齐备，
    # 缺任一项会直接 sys.exit 并给出配置提示，故此处直接取，不再做二次判空。
    import os
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["LLM_API_KEY"],
                    base_url=os.environ["LLM_BASE_URL"],
                    timeout=args.timeout)

    # 三种取文件的方式，优先级：--ids（点名）> --sample（抽样）> 全量。
    # ⚠️ --sample 默认值是 6，所以不能写 `if args.sample:` 来判「用户是否要抽样」
    # —— 那会让「不传 --sample」也走进抽样分支，--ids 之外的默认行为被改掉。
    if args.ids:
        ids = [s.strip() for s in args.ids.split(",") if s.strip()]
        files = pick_files_by_ids(args.source, ids)
        mode = f"指定 {len(ids)} 个 id，命中 {len(files)} 篇"
        # 点名却一篇没命中，通常是 id 传错或笔记没落在这个 source 下。
        # 不提示的话调用方只看到「待处理 0 篇」，与「打标全失败」长得一样。
        if not files:
            print(f"⚠️ {args.source}/notes 下没有匹配这些 note_id 的笔记："
                  f"{', '.join(ids[:10])}"
                  + ("…" if len(ids) > 10 else ""), file=sys.stderr)
    elif args.sample:
        files = pick_files(args.source, args.sample)
        mode = f"抽样 {args.sample}"
    else:
        files = [p for p in sorted((ROOT / args.source / "notes").rglob("*.md"))
                 if p.name != "INDEX.md"]
        mode = "全量"
    if args.skip_modified:
        import subprocess
        done = {ln for ln in subprocess.run(
            ["git", "diff", "--name-only"], cwd=ROOT,
            capture_output=True, text=True).stdout.splitlines() if ln.strip()}
        before = len(files)
        files = [f for f in files if str(f.relative_to(ROOT)) not in done]
        print(f"跳过本轮已写盘 {before - len(files)} 篇，剩余 {len(files)} 篇")
    print(f"待处理 {len(files)} 篇（{mode}，"
          f"{'写盘' if args.apply else '仅报告不写盘'}）\n")

    def one(p):
        text = p.read_text(encoding="utf-8")
        payload = build_input(text)
        if not payload:
            return p, None, 0, 0, "无正文，跳过", 0, []
        if FAKE_BODY.search(payload):
            return p, None, 0, 0, "正文自述内容缺失，跳过（见注释 FAKE_BODY）", 0, []
        last = ""
        for i in range(len(BACKOFF_429)):
            try:
                r = client.chat.completions.create(
                    model=os.environ["LLM_MODEL"],
                    reasoning_effort=args.effort,
                    max_completion_tokens=args.max_completion,
                    temperature=0.2, timeout=args.timeout,
                    messages=[{"role": "system", "content": SYS_PROMPT},
                              {"role": "user", "content": payload}])
                msg = r.choices[0].message
                # 核验 effort 是否真生效：MiniMax 把思考放在 reasoning_content，
                # 不在 completion_tokens 里，所以只看 token 数会误判"没思考"
                rc = getattr(msg, "reasoning_content", None)
                tags, dropped = validate(parse_tags(msg.content))
                u = r.usage
                return p, tags, getattr(u, "prompt_tokens", 0), \
                    getattr(u, "completion_tokens", 0), None, len(rc or ""), dropped
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
                if i < len(BACKOFF_429) - 1:
                    # 429 的恢复窗口是秒级到分钟级（实测 1s/2s 退避不够，
                    # 10 并发下 212 篇有 17 篇耗尽重试）；422 内容审核拦截
                    # 重试无用，单独短退避快速失败，留给人工判断。
                    if "429" in last or "RateLimit" in last:
                        time.sleep(BACKOFF_429[i])
                    else:
                        time.sleep(2 ** i)
        return p, None, 0, 0, last, 0, []

    results, tin, tout, fail, wrote = [], 0, 0, 0, 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(one, p): p for p in files}
        for f in as_completed(futs):
            p, tags, i_t, o_t, err, rc_len, dropped = f.result()
            tin += i_t; tout += o_t
            if err:
                fail += 1
                print(f"❌ {p.name[:46]}  {err[:90]}")
            else:
                results.append((p, tags))
                if args.apply:
                    try:
                        write_tags(p, tags)
                        wrote += 1
                    except Exception as e:
                        fail += 1
                        print(f"❌ 写盘失败 {p.name[:40]}  {type(e).__name__}: {e}")
                        continue
                print(f"\n{'💾' if args.apply else '✅'} {p.name[:56]}")
                print(f"   输入 {i_t:,} tok / 输出 {o_t:,} tok"
                      f" / 思考 {rc_len:,} 字符")
                print(f"   标签（{len(tags)} 个）：{' · '.join(tags)}")
                if dropped:
                    print(f"   ℹ️ 超 8 字符（已原样保留，未截断未丢弃）：{dropped}")

    print(f"\n成功 {len(results)} / 失败 {fail}" + (f" / 已写盘 {wrote}" if args.apply else ""))
    print(f"token 合计：输入 {tin:,}  输出 {tout:,}")
    return results, tin, tout, fail


if __name__ == "__main__":
    main()