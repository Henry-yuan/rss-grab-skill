#!/usr/bin/env python3
"""阶段 B 实验：长文分块扫描（只用于选参数，不产生可交付笔记）。

四步：
  ① 分块      脚本本地切，**[mm:ss]** 为不可分割最小单元，块间 overlap
  ② 逐块抽取  每块提要点，输出结构化 JSON
  ③ 合并去重  汇总成主题清单（effort 待实测）
  ④ 写正文    基于清单 + 源生成（effort=max）——本脚本可选执行

产物全部写 `docs/实验/chunk-scan-<date>/`，不碰生产 notes 目录。
>
> ⚠️ 与主项目那份的差异：**仅本头注**。正文 0 处源专属，逻辑完全一致
> （AST 比对已验证）。

用法：
  python3 chunk_scan.py --source <transcript> --out <实验目录> --size 5000
  python3 chunk_scan.py --source <transcript> --out <实验目录> --size 5000 --write-note
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))
sys.path.insert(0, str(SCRIPT_DIR.parent.parent / "_shared"))

TS_LINE = re.compile(r"^\*\*\[\d{1,2}:\d{2}\]\*\*")
SENT_END = re.compile(r"[。！？!?…；;]([\"』」）)]*)\s*$")

MODEL = os.environ.get("MINIMAX_MODEL", "MiniMax-M3.1-Flash-Preview")
BASE_URL = os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.cn/v1")
MAX_COMPLETION = int(os.environ.get("MINIMAX_MAX_COMPLETION", "100000"))
TIMEOUT = 1800

# ── ② 逐块抽取提示词 ─────────────────────────────────
SCAN_SYS = """你是转写内容分析助手。下方是某段语音转写的一个片段。

请从中提取**要点**，要求：

1. 只提取这一段里**实际说到**的内容，不要补充、不要推断、不要加自己的看法
2. 每条要点是**完整可读的判断句**（不是短语，不是关键词堆砌）
3. 标注该要点的类型：问题 / 论点 / 方法 / 案例 / 数据 / 结论 / 提问
4. 若是承接上文的句子（以「它 / 这个 / 该 / 就是」等开头），在 continue 字段标 true，
   并尽量补全它的指代对象
5. 含具体数字、比例、金额、人名、机构名的，在 has_number / has_person 标 true
6. **片段可能被切断**（首句承接上文、末句未完），遇到时照常提取已知信息即可
7. **直接写内容本身**。禁止用「回答者表示 / 提问者询问 / 发言者提到 /
   对方认为 / 演讲者认为」等引导语开头——这类元话语只描述"谁在说"，
   不携带信息，会被判为无效要点。要写就写他说了什么。
   （反例：「回答者表示没有明确愿景」→ 正例：「团队对 TUI 与 GUI 的选择没有明确愿景」）
8. **问句形式不是要点，但问句里的信息是**。转写里嘉宾和听众的
   反问句本身不单独记成一条要点，但**只要句子里陈述了事实，就改写成
   陈述句保留，不要丢**。按下面三档处理：
   - 句中已经陈述了事实（哪怕是反问语气）→ **改写保留**
     （反例：「Claude Code 自身具备比 Codex 更多的 orchestration 功能和 agent view？」
      → 正例：「Claude Code 自身带有很多 orchestration 功能且强于 Codex，
      并提供 agent view」）
   - 源里紧接着给出了答案 → 把问题和答案合并成一条陈述句
     （反例：「企业为什么关注 Codex？」→「企业关注 Codex 和 open-weight model，
      原因是 Anthropic 账单已成第一大开销」）
   - 既不携带事实、源里也没有答案（纯寒暄 / 纯发问）→ **才丢弃**
     （反例：「Pi 能否使用 Claude Code？」「现在谈了几个人？」→ 不产出任何条目）
   拿不准算不算携带信息时，**倾向于保留并改写**，不要丢。

9. **推广与广告不构成要点**，遇到就跳过，不要提取。

   判据不是关键词，而是这句话的**作用**：
   - 它的作用是**说服读者去看、去买、去加入** → 广告，丢掉
   - 它的作用是**传递一个事实** → 信息，保留

   自检方法：**把这句删掉，读者对内容的理解会不会受影响？**
   不受影响 → 丢掉；会漏掉一件事 → 保留。

   常见的广告形态（举例，不是穷举，按上面的判据自行判断）：
   社区/社群的规模宣传（成员数、关注者数、加入方式）
   付费课程或产品的推广（价格、学员数、评分、课时数、退款政策、赠品）
   试用邀请、优惠码、内测名额
   平台自夸式宣传（"最好""第一""遥遥领先""最强"）

   （反例：跳过「AI Builders 课程已迭代 13 期、3000 多名学员、
     在 Maven 拿满分好评、14 天无理由退款、赠送一年期会员」
   正例：保留「Orca 每天发布一个 stable 版本，每个版本含上百个 PR」——
     后者说明产品的迭代节奏，是事实）

   注意：**创始人自己讲自家产品的能力和做法不是广告**，
   只有"请你来看看/买买"性质的内容才是。

只输出 JSON，不要任何其他文字。"""

# 提示词指纹：写进每块的 raw 元数据，保证"三档用同一版提示词"可事后核验
_PROMPT_SHA = hashlib.sha256(SCAN_SYS.encode("utf-8")).hexdigest()[:12]

SCAN_USER = """## 转写片段（第 {idx}/{total} 块，起始 {start}）

{chunk}

## 输出格式

{{
  "points": [
    {{
      "text": "一句话写完整的要点",
      "type": "论点",
      "continue": false,
      "has_number": false,
      "has_person": false
    }}
  ]
}}"""

# ── ③ 合并去重提示词 ─────────────────────────────────
MERGE_SYS = """你是要点清单整理助手。下方是多段转写分别提取出的要点列表。

任务：合并去重，整理成一份**主题清单**。

规则：
1. 语义相同的要点合并成一条，保留信息最完整的表述
2. 把只讲「我理解 / 我们认为」这类元话语的条目剔除
3. 把**只有问句、源里没有对应答案**的条目剔除；若问句在原文里其实
   有答案（抽取时被拆成了问 + 答两条），把两条合并成一条陈述句
4. 保留具体的判断、方法、数据、案例，不要抽象成空泛的主题名
5. 按主题聚类排序，相近的放在一起
6. **不要新增清单里没有的要点**
7. 标注每条是否含具体数字或具名人物

8. **每条输入要点都必须被至少一条主题认领**，用 from_blocks 标出它的序号。
   不允许出现「有要点没进任何主题」的情况。主题宁可多写一条、内容啰嗦，
   也不能把要点整个丢掉。

9. **认领一条要点，要把它的因果和限定一起带过来，不能只留一句概括。**

   【反面案例】输入要点：
     「iTerm 和 Ghostty 的更新速度很慢，因此基本没有人敢更新，
       用户与产品一起迭代的速度也会变慢」
     错误做法 —— 只留了一句概括：
       标题「Orca 避免终端更新导致 Agent session 丢失」
     问题：工具名和现象都保住了，但「更新慢 → 没人敢更新 → 迭代变慢」
     这条因果链整个消失了。

   【正面案例】同一个要点，正确做法：
       标题「iTerm 和 Ghostty 更新缓慢，没人敢更新，
             导致用户与产品一起迭代的速度也变慢」
       detail「Orca 用持久化 PTY snapshot 和 restore 解决了这个问题：
               终端更新时所有 PTY 及运行状态都能恢复」

   合并后逐条自检，三者缺一就把缺失部分补进 detail：
     · 要点里「因为 / 所以 / 因此 / 导致」这层因果关系还在吗？
     · 要点里「如果 / 只有…才 / 不能保证 / 仅限 / 前提下」这类限定还在吗？
     · 要点里的对比项还在吗（如「不是 AWS」里的 AWS、「而不是 X」里的 X）？

10. **detail 不是可选项。** 一条要点如果包含多件事
    （多步流程、多个例子、多个并列判断），全部写进 detail，
    不要只留其中一件。输入要点里出现过的数字、具名工具名、步骤序列，
    都要能在 detail 里找到。

11. **广告与课程推广不承载信息**，不要为它们单独立主题：
    社区推广、试用邀请、付费课程（含课程价格、学员人数、评分、课时数）。

只输出 JSON，不要其他文字。"""


MERGE_USER = """## 要点列表（{n} 条，来自 {total} 块转写）

{points}

## 输出格式

{{
  "topics": [
    {{
      "title": "要点标题（完整判断句）",
      "detail": "补充说明",
      "has_number": true,
      "has_person": false,
      "from_blocks": [1, 3]
    }}
  ]
}}"""


def _load_env() -> None:
    try:
        from env import load_env
        load_env(SCRIPT_DIR)
    except Exception:
        pass


def apply_typo_rules(text: str, rules: list[dict] | None) -> tuple[str, int]:
    """对文本套用错字规则。rules 为空则原样返回。

    在两个位置调用：
      ② 扫描前——源里的 ASR 错拼不进模型
      ③ 合并后——抓合并阶段 LLM 新造/放大的错拼
    """
    if not rules:
        return text, 0
    from correct_typos import apply_typos
    out, hits = apply_typos(text, rules)
    return out, sum(h.get("occurrences", 0) for h in hits)


def correct_merge_output(obj: dict, rules: list[dict] | None) -> tuple[dict, int]:
    """③ 合并完成后对主题标题/detail 再套一次错字规则。"""
    if not rules:
        return obj, 0
    total = 0
    for t in obj.get("topics", []):
        for k in ("title", "detail"):
            if isinstance(t.get(k), str) and t[k]:
                t[k], n = apply_typo_rules(t[k], rules)
                total += n
    return obj, total


def split_chunks(text: str, target: int = 5000, overlap: int = 1) -> list[dict]:
    """按 [mm:ss] 段界切块，块间 overlap 段。

    段界优先：绝不在时间戳段内部切。段界稀疏时退化为句末标点切。
    """
    # ① 切出「段」：时间戳行开启新段；无时间戳则整段为一个大段
    raw_segs: list[list[str]] = []
    cur: list[str] = []
    has_ts = bool(TS_LINE.match(text.lstrip().split("\n")[0])) or bool(
        re.search(r"^\*\*\[\d{1,2}:\d{2}\]\*\*", text, re.M))
    for ln in text.split("\n"):
        if not ln.strip():
            continue
        if has_ts and TS_LINE.match(ln) and cur:
            raw_segs.append(cur)
            cur = []
        cur.append(ln)
    if cur:
        raw_segs.append(cur)

    # 无时间戳 → 按句末标点细分
    if not has_ts:
        finer: list[list[str]] = []
        for seg in raw_segs:
            units, pos = [], 0
            for ln in seg:
                for m in SENT_END.finditer(ln):
                    if m.end() > pos:
                        units.append(ln[pos:m.end()])
                        pos = m.end()
                if pos < len(ln):
                    units.append(ln[pos:])
            if units:
                finer.append(units)
        raw_segs = finer

    # ② 段累积到 target
    chunks, block, size = [], [], 0
    for seg in raw_segs:
        slen = sum(len(x) + 1 for x in seg)
        if block and size + slen > target:
            chunks.append(block)
            block, size = [], 0
        block.extend(seg)
        size += slen
    if block:
        chunks.append(block)

    # ③ 加 overlap
    out = []
    for i, c in enumerate(chunks):
        if i > 0 and overlap > 0:
            prev = chunks[i - 1]
            extra = []
            for s in reversed(prev):
                if sum(len(x) + 1 for x in extra) > 400:
                    break
                extra.insert(0, s)
                if len(extra) >= overlap:
                    break
            if extra:
                c = extra + c
        ts = ""
        m = re.search(r"\*\*\[(\d{1,2}:\d{2})\]\*\*", "\n".join(c))
        if m:
            ts = m.group(1)
        out.append({"index": i + 1, "start": ts, "text": "\n".join(c),
                    "chars": sum(len(x) + 1 for x in c)})
    return out


def _call(client, sys_p, user_p, effort: str) -> str:
    stop = threading.Event()

    def _beat():
        t0 = time.time()
        while not stop.wait(30):
            print(f"    … 生成中 {int(time.time() - t0)}s", flush=True)

    th = threading.Thread(target=_beat, daemon=True)
    th.start()
    try:
        r = client.chat.completions.create(
            model=MODEL, reasoning_effort=effort,
            max_completion_tokens=MAX_COMPLETION, temperature=0.2,
            timeout=TIMEOUT,
            messages=[{"role": "system", "content": sys_p},
                      {"role": "user", "content": user_p}])
        return r.choices[0].message.content or ""
    finally:
        stop.set()


def _parse_json(raw: str) -> dict:
    t = re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"```\s*$", "", t).strip()
    s, e = t.find("{"), t.rfind("}")
    return json.loads(t[s:e + 1])


def run_scan(chunks, out_dir: Path, client, effort: str,
             concurrency: int = 1, stagger: float = 2.0,
             empty_retries: int = 1, typo_rules: list[dict] | None = None
             ) -> list[dict]:
    """② 逐块抽取要点。

    并发模式下：
      - 最多 concurrency 个块同时在飞，块间 stagger 秒错开发射
      - 每块完成立刻追加写 pts_dir/scan-<i>.json（断点续跑的唯一依据）
      - 返回 0 要点的块自动重试 empty_retries 次
    typo_rules 非空时，**每块在进模型前**先套错字纠正（② 阶段）。
    """
    pts_dir, raw_dir = out_dir / "points", out_dir / "raw"
    pts_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    done = [0]
    t0 = time.time()

    def one(c: dict) -> list[dict]:
        """抽一块；失败/空结果按 empty_retries 重试。"""
        chunk_text = c["text"]
        typo_n = 0
        if typo_rules:
            chunk_text, typo_n = apply_typo_rules(chunk_text, typo_rules)
        user = SCAN_USER.format(idx=c["index"], total=len(chunks),
                                start=c["start"] or "?", chunk=chunk_text)
        attempt, pts, err, el = 0, [], None, 0.0
        while attempt <= empty_retries:
            attempt += 1
            s = time.time()
            try:
                raw = _call(client, SCAN_SYS, user, effort)
                obj = _parse_json(raw)
                pts = obj.get("points", [])
                err = None
            except Exception as e:  # noqa: BLE001
                raw, pts = "", []
                err = f"{type(e).__name__}: {str(e)[:120]}"
            el = round(time.time() - s, 1)
            if pts or attempt > empty_retries:
                break
            print(f"    ↻ 块{c['index']} 返回 0 要点，重试 {attempt}/{empty_retries}",
                  flush=True)
            time.sleep(stagger)
        for p in pts:
            p["_block"] = c["index"]
        # 逐块落盘：raw 记耗时/错误，points 记内容（断点续跑靠后者）
        (raw_dir / f"scan-{c['index']:02d}.json").write_text(
            json.dumps({"block": c["index"], "start": c["start"],
                        "chars": c["chars"], "elapsed": el, "n": len(pts),
                        "attempts": attempt, "err": err,
                        "typo_fixed": typo_n,
                        "prompt_sha": _PROMPT_SHA},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        (pts_dir / f"scan-{c['index']:02d}.json").write_text(
            json.dumps(pts, ensure_ascii=False, indent=2), encoding="utf-8")
        with lock:
            done[0] += 1
            tm = f" 纠错{typo_n}处" if typo_n else ""
            print(f"    [{done[0]:2d}/{len(chunks)}] 块{c['index']:2d} "
                  f"({c['start'] or '?'}) {c['chars']:5d}字 -> {len(pts):2d} 要点  "
                  f"{el:6.1f}s{tm}{'  ' + err if err else ''}", flush=True)
        return pts

    if concurrency <= 1:
        allpts = [p for c in chunks for p in one(c)]
    else:
        print(f"    并发 {concurrency}，发射间隔 {stagger}s", flush=True)
        allpts = []
        with cf.ThreadPoolExecutor(max_workers=concurrency) as ex:
            futs = []
            for c in chunks:
                futs.append(ex.submit(one, c))
                if stagger > 0:
                    time.sleep(stagger)
            for f in futs:
                allpts.extend(f.result())

    log = []
    for c in chunks:
        pf = pts_dir / f"scan-{c['index']:02d}.json"
        rf = raw_dir / f"scan-{c['index']:02d}.json"
        if not pf.exists():
            log.append({"block": c["index"], "chars": c["chars"],
                        "points": None, "status": "missing"})
            continue
        rec = json.loads(rf.read_text(encoding="utf-8"))
        log.append({"block": c["index"], "chars": c["chars"],
                    "points": len(json.loads(pf.read_text(encoding="utf-8"))),
                    "elapsed": rec.get("elapsed"),
                    "attempts": rec.get("attempts"), "err": rec.get("err")})
    (pts_dir / "scan-log.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "points.json").write_text(
        json.dumps(allpts, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"    扫描完成：{len(allpts)} 条要点 / {time.time() - t0:.0f}s", flush=True)
    return allpts


def run_merge(points, out_dir: Path, client, effort: str, tag: str,
              typo_rules: list[dict] | None = None) -> dict:
    """③ 合并去重。

    目录由本函数自建：不能依赖 run_scan 先跑过（--merge-only 路径不会跑 run_scan，
    缺目录会让已付费的 LLM 返回在落盘时崩掉，输出全丢）。

    typo_rules 非空时，**合并完成后对主题标题/detail 再套一次**（③ 阶段），
    抓合并阶段 LLM 新造或放大的错拼。
    """
    (out_dir / "raw").mkdir(parents=True, exist_ok=True)
    (out_dir / "merged").mkdir(parents=True, exist_ok=True)
    lines = []
    for i, p in enumerate(points, 1):
        flags = []
        if p.get("has_number"):
            flags.append("含数字")
        if p.get("has_person"):
            flags.append("含人名")
        if p.get("continue"):
            flags.append("承接")
        mark = f"[{','.join(flags)}]" if flags else ""
        lines.append(f"{i}. {p.get('text','')} {mark}")
    user = MERGE_USER.format(n=len(points), total="多", points="\n".join(lines))
    t0 = time.time()
    raw = _call(client, MERGE_SYS, user, effort)
    obj = _parse_json(raw)
    obj, typo_n = correct_merge_output(obj, typo_rules)
    (out_dir / "raw" / f"merge-{tag}.json").write_text(
        json.dumps({"elapsed": round(time.time() - t0, 1), "typo_fixed": typo_n,
                    "raw": raw[:6000]},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "merged" / f"topics-{tag}.json").write_text(
        json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tm = f"，纠错 {typo_n} 处" if typo_n else ""
    print(f"    合并({effort}) {len(points)} 要点 -> {len(obj.get('topics', []))} 主题 "
          f"{time.time() - t0:.1f}s{tm}", flush=True)
    return obj


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--size", type=int, default=10000,
                    help="分块目标字符数。实测选定 10000（Orca 篇 n=2 召回 98.7%%，零波动）")
    ap.add_argument("--overlap", type=int, default=1)
    ap.add_argument("--scan-effort", default="xhigh",
                    help="逐块抽取 effort。实测选定 xhigh（96.2%% > max 94.9%%，且快一倍）")
    ap.add_argument("--merge-effort", default="medium",
                    help="合并 effort。⚠️ 尚未实测，当前为占位默认值")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="逐块抽取的并发数；>1 时按 --stagger 错开发射")
    ap.add_argument("--stagger", type=float, default=2.0,
                    help="并发模式下块间发射间隔秒数")
    ap.add_argument("--empty-retries", type=int, default=1,
                    help="返回 0 要点的块自动重试次数")
    ap.add_argument("--no-merge", action="store_true",
                    help="只跑 ② 逐块抽取，跳过 ③ 合并")
    ap.add_argument("--merge-only", default=None, metavar="POINTS_JSON",
                    help="跳过 ①②，直接对已有 points.json 跑 ③ 合并（合并步实验用）")
    ap.add_argument("--typos", action="store_true",
                    help="启用专名错字纠正：② 扫描前 + ③ 合并后各套一次")
    ap.add_argument("--typos-seed", default=None,
                    help="错字词表路径（默认用脚本目录下的 typos_global.json）")
    ap.add_argument("--typos-json", default=None,
                    help="已生成的 correct_typos.py 报告 JSON（补充单篇检测结果）")
    ap.add_argument("--tag", default=None)
    args = ap.parse_args()

    _load_env()
    if not os.environ.get("MINIMAX_API_KEY"):
        print("❌ 未配置 MINIMAX_API_KEY", file=sys.stderr)
        return 1

    from openai import OpenAI
    client = OpenAI(api_key=os.environ["MINIMAX_API_KEY"],
                    base_url=BASE_URL, timeout=TIMEOUT)

    # ── 错字规则：种子表 + （可选）单篇检测结果 ──
    typo_rules: list[dict] = []
    if args.typos or args.typos_json:
        from correct_typos import build_rules, load_global
        seed = load_global(Path(args.typos_seed) if args.typos_seed else None)
        det = None
        if args.typos_json:
            det = json.loads(
                Path(args.typos_json).read_text(encoding="utf-8")).get("typos")
        typo_rules = build_rules(seed, det)
        n_ap = sum(1 for r in typo_rules if r.get("applied"))
        print(f"[typos] 规则 {len(typo_rules)} 条，其中 {n_ap} 条会执行替换", flush=True)

    # ── 合并步单独实验：复用已有要点，跳过分块与抽取 ──
    if args.merge_only:
        pts = json.loads(Path(args.merge_only).read_text(encoding="utf-8"))
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        mtag = args.tag or f"merge{args.merge_effort}"
        print(f"[merge-only] 载入 {len(pts)} 条要点 <- {args.merge_only}", flush=True)
        obj = run_merge(pts, out, client, args.merge_effort, mtag,
                        typo_rules=typo_rules)
        print(f"[merge-only] -> {len(obj.get('topics', []))} 主题，"
              f"写入 {out / 'merged' / f'topics-{mtag}.json'}", flush=True)
        return 0

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tag = args.tag or f"size{args.size}-merge{args.merge_effort}"
    (out / "config.yaml").write_text(
        f"source: {args.source}\nsize: {args.size}\noverlap: {args.overlap}\n"
        f"scan_effort: {args.scan_effort}\nmerge_effort: {args.merge_effort}\n"
        f"concurrency: {args.concurrency}\nstagger: {args.stagger}\n"
        f"empty_retries: {args.empty_retries}\n"
        f"typos_enabled: {bool(typo_rules)}\n"
        f"typos_applied_rules: {sum(1 for r in typo_rules if r.get('applied'))}\n"
        f"scan_prompt_sha: {_PROMPT_SHA}\n"
        f"model: {MODEL}\nbase_url: {BASE_URL}\ntag: {tag}\n"
        f"started_at: {datetime.now().isoformat(timespec='seconds')}\n",
        encoding="utf-8")

    text = Path(args.source).read_text(encoding="utf-8")
    chunks = split_chunks(text, args.size, args.overlap)
    print(f"[chunk-scan] {len(chunks)} 块 | size={args.size} overlap={args.overlap} "
          f"| 源 {len(text)} 字符", flush=True)
    for c in chunks:
        print(f"    块{c['index']:2d} {c['start'] or '?':>6} {c['chars']:5d} 字符", flush=True)

    pts = run_scan(chunks, out, client, args.scan_effort,
                   concurrency=args.concurrency, stagger=args.stagger,
                   empty_retries=args.empty_retries, typo_rules=typo_rules)
    (out / "points" / f"points-{tag}.json").write_text(
        json.dumps(pts, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[chunk-scan] 抽取完成：{len(pts)} 条要点", flush=True)

    if args.no_merge:
        print("[chunk-scan] --no-merge，跳过 ③ 合并", flush=True)
        return 0

    obj = run_merge(pts, out, client, args.merge_effort, tag,
                    typo_rules=typo_rules)
    (out / "metrics.json").write_text(json.dumps({
        "tag": tag, "size": args.size, "overlap": args.overlap,
        "scan_effort": args.scan_effort, "merge_effort": args.merge_effort,
        "chunks": len(chunks), "raw_points": len(pts),
        "topics": len(obj.get("topics", [])),
        "source_chars": len(text),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
