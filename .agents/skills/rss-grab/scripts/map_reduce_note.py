#!/usr/bin/env python3
"""Map-reduce 笔记生成：调 OpenAI 兼容 LLM API 处理长播客 transcript。

工作流：
  1. 解析 transcript.md -> cues 列表
  2. 按 token 切分（每块 ~30K tokens，留 1M context 余量）
  3. MAP 阶段: 并发 4 调 LLM API，每块生成段级 JSON
  4. REDUCE 阶段: 调 LLM API 一次，输出完整笔记 JSON
  5. final.json 交给 generate_note.py --source summary 拼最终 MD

用法:
  python3 map_reduce_note.py <transcript.md>

环境变量:
  LLM_API_KEY  - 必填（与 .env.example 一致，由 _shared/env.py 加载）
  LLM_BASE_URL - 必填（你的 LLM 服务 OpenAI 兼容接口地址）
  LLM_MODEL    - 必填（你的模型名）
"""
import json
import os
import pathlib
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from openai import OpenAI

# 配置
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
OUTPUT_DIR = SCRIPT_DIR / "output"
CHUNK_TOKENS = 30_000          # 每块目标 token 数
MAX_WORKERS = 4               # map 阶段并发数
RETRY = 3                     # API 重试次数
MAP_MAX_TOKENS = 4000         # map 阶段输出上限
REDUCE_MAX_TOKENS = 16000     # reduce 阶段输出上限
TEMPERATURE = 0               # 可复现
sys.path.insert(0, str(SCRIPT_DIR.parent.parent / "_shared"))
from env import load_env

MAP_SYSTEM = "你是一个播客转写分析专家。"
MAP_PROMPT = """分析以下播客转写片段，输出严格 JSON：
{{
  "time_range": ["起始 mm:ss", "结束 mm:ss"],
  "main_points": ["核心要点 1", "核心要点 2", ...],   // 至少 3 条
  "key_quotes": ["原文金句 1", "原文金句 2", ...],   // 至少 2 条，直接引用（保留原样，含转写错字也不要修正——错字统一在最终笔记阶段申报纠正）
  "subjects": ["话题 1", "话题 2", ...]              // 至少 2 个
}}

播客转写片段：
{cues}
"""

REDUCE_SYSTEM = "你是一个专业的中文笔记编辑。"
REDUCE_PROMPT = """基于以下播客片段摘要，整合成一篇结构化中文笔记，输出严格 JSON：

{{
  "meta": {{
    "title": "完整播客标题",
    "type": "播客",
    "duration_sec": {duration},
    "cues_count": {cues_count}
  }},
  "tl_dr": ["要点 1", "要点 2", ...],                  // 至少 4 段
  "chapters": [
    {{
      "index": 1,
      "title": "章节标题",
      "time_range": ["0:00", "2:00"],
      "key_points": ["本章节核心观点 1", ...],         // 至少 2 条
      "quotes": ["本章节原话引用 1", ...],            // 至少 1 条
      "subjects": ["本章节话题"]
    }}
  ],                                                  // 至少 8 个章节
  "key_quotes": ["全文金句 1", "全文金句 2", ...]      // 至少 5 条
}}

要求:
- 章节数 = ceil(duration_minutes / 5)，最少 8 个
- TL;DR 至少 4 段要点
- 关键引用至少 5 条（直接引用原文）
- 整合时**不要丢失**任何片段的核心信息

播客片段摘要（JSON 数组）：
{chunks}
"""


def estimate_tokens(text: str) -> int:
    """粗估 token：中文 1 字符 ~1.3，英文 1 字符 ~0.3。"""
    chinese = sum(1 for c in text if '一' <= c <= '鿿')
    return int(chinese * 1.3 + (len(text) - chinese) * 0.3)


def parse_transcript(text: str) -> list:
    """解析 transcript.md 为 [(ts_sec, content), ...] 列表。

    正则 \\*\\*\\[(\\d+):(\\d+)\\]\\*\\* 支持超 60 分钟（如 [75:30] -> 75*60+30）。
    """
    pattern = re.compile(r'\*\*\[(\d+):(\d+)\]\*\*\s*(.*?)(?=\n\n|\Z)', re.DOTALL)
    return [
        (int(m.group(1)) * 60 + int(m.group(2)), m.group(3).strip())
        for m in pattern.finditer(text)
    ]


def chunk_cues(cues: list, chunk_tokens: int = CHUNK_TOKENS) -> list:
    """按 token 切分 cues 列表，保持每个 chunk 是连续的时间段。"""
    chunks = []
    current = []
    current_tokens = 0
    for ts, content in cues:
        cue_tokens = estimate_tokens(content) + 10  # +10 时间戳开销
        if current and current_tokens + cue_tokens > chunk_tokens:
            chunks.append(current)
            current = []
            current_tokens = 0
        # 单 cue 超 chunk_tokens 保护
        if cue_tokens > chunk_tokens and not current:
            print(f"  ⚠️ 单 cue {cue_tokens} tokens 超 chunk 上限 {chunk_tokens}，仍合并（可能超 token）")
        current.append((ts, content))
        current_tokens += cue_tokens
    if current:
        chunks.append(current)
    return chunks


def run_with_heartbeat(api_call_fn, label, interval=30):
    """调 API 时启动后台心跳线程，每 N 秒 print 一次（治"看起来 hang"）。"""
    stop_event = threading.Event()
    t0 = time.time()

    def heartbeat():
        while not stop_event.wait(interval):
            elapsed = time.time() - t0
            print(f"  [{label}] 仍运行中... {elapsed:.0f}s", flush=True)

    t = threading.Thread(target=heartbeat, daemon=True)
    t.start()
    try:
        return api_call_fn()
    finally:
        stop_event.set()


def fix_json_quotes(text: str) -> str:
    """逐字符扫描，修复 JSON 字符串内部的未转义双引号。

    LLM（M3）输出 JSON 时常常用自然中文引号而非 \\"，导致 json.loads 失败。
    规则：在字符串内部遇到 "，如果它后面紧跟 ,:]}/空白 -> 是字符串结束；
    否则是嵌套引号，替换成 \\"。
    """
    result = []
    in_string = False
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '\\' and i + 1 < n:
            result.append(c)
            result.append(text[i+1])
            i += 2
            continue
        if c == '"':
            if not in_string:
                in_string = True
                result.append(c)
            else:
                j = i + 1
                while j < n and text[j] in ' \t\n\r':
                    j += 1
                if j >= n or text[j] in ',:}]':
                    in_string = False
                    result.append(c)
                else:
                    result.append('\\"')
            i += 1
            continue
        result.append(c)
        i += 1
    return ''.join(result)


def call_api(client, model, messages, max_tokens, label, raw_dump_dir) -> dict:
    """带重试的 API 调用 + JSON fallback 解析。"""
    for attempt in range(1, RETRY + 1):
        try:
            t0 = time.time()
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=TEMPERATURE,
                max_tokens=max_tokens,
            )
            dt = time.time() - t0
            content = response.choices[0].message.content
            usage = response.usage
            print(
                f"  [{label}] attempt={attempt} ok in {dt:.1f}s "
                f"tokens={usage.total_tokens if usage else '?'}"
            )

            # 解析前先 strip thinking block（M3 特性，统一正则处理）
            content_clean = re.sub(
                r'<think>.*?</think>\s*', '', content, flags=re.DOTALL
            ).strip()
            if content_clean != content:
                content = content_clean

            # 尝试直接 json.loads
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                pass

            # Fallback 1: strip markdown 代码块
            stripped = re.sub(r'^\s*```(?:json)?\s*\n', '', content)
            stripped = re.sub(r'\n```\s*$', '', stripped)
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                pass

            # Fallback 2: 找第一个 { 到最后一个 }
            m = re.search(r'\{.*\}', content, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError:
                    pass

            # Fallback 3: 修复 JSON 字符串内的未转义双引号
            fixed = fix_json_quotes(stripped)
            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                pass

            # 全部失败：保存原始内容供 debug
            if raw_dump_dir:
                dump = pathlib.Path(raw_dump_dir) / f"{label}.raw.txt"
                dump.parent.mkdir(parents=True, exist_ok=True)
                dump.write_text(content)
            raise ValueError(f"无法解析 JSON（content 前 200 字符: {content[:200]!r}）")
        except Exception as e:
            print(f"  [{label}] attempt={attempt} failed: {type(e).__name__}: {e}")
            if attempt < RETRY:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"[{label}] 重试 {RETRY} 次仍失败")


def extract_hash8(path: pathlib.Path) -> str:
    """从 transcript 文件名提取 guid_hash8（最后一段 - 后的 8 位 hex）。

    例：独树不成林-361-f35eb04d.transcript.md -> f35eb04d
    与 decide_mode.extract_hash8_from_path 行为一致：无 - 时返回 ""。
    """
    name = path.name.replace(".transcript.md", "")
    if "-" in name:
        return name.rsplit("-", 1)[-1]
    return ""


def main():
    if len(sys.argv) < 2:
        sys.exit("用法: python3 map_reduce_note.py <transcript.md>")
    transcript_path = pathlib.Path(sys.argv[1])
    if not transcript_path.exists():
        sys.exit(f"ERROR: 文件不存在: {transcript_path}")

    load_env(SCRIPT_DIR)
    client = OpenAI(
        api_key=os.environ["LLM_API_KEY"],
        base_url=os.environ["LLM_BASE_URL"],
        # 20 分钟硬超时：覆盖 reduce 阶段异常慢的情况
        timeout=1200.0,
    )
    model = os.environ["LLM_MODEL"]

    text = transcript_path.read_text()
    cues = parse_transcript(text)
    total_tokens = estimate_tokens(text)
    duration = cues[-1][0] if cues else 0
    guid_hash8 = extract_hash8(transcript_path)
    # 输出 id = transcript 文件名去后缀（含标题-hash8），与 regenerate_note 查找一致
    out_id = transcript_path.name.replace(".transcript.md", "")

    print(f"=== Map-reduce 笔记生成（rss）===")
    print(f"guid_hash8: {guid_hash8}")
    print(f"transcript: {transcript_path.name}")
    print(f"  chars: {len(text):,}")
    print(f"  cues: {len(cues)}")
    print(f"  est tokens: {total_tokens:,}")
    print(f"  duration: {duration}s = {duration // 60}m{duration % 60}s")

    if not cues:
        sys.exit("ERROR: transcript 无 cues")

    chunks = chunk_cues(cues)
    print(f"  chunks: {len(chunks)} (target ~{CHUNK_TOKENS} tokens each)")

    # ---- MAP 阶段 ----
    print(f"\n--- Map 阶段（并发 {MAX_WORKERS}）---")
    map_inputs = []
    for i, ch in enumerate(chunks):
        cues_text = "\n".join(f"[{ts // 60:02d}:{ts % 60:02d}] {c}" for ts, c in ch)
        time_range = [
            f"{ch[0][0] // 60:02d}:{ch[0][0] % 60:02d}",
            f"{ch[-1][0] // 60:02d}:{ch[-1][0] % 60:02d}",
        ]
        map_inputs.append({
            "index": i,
            "cues_text": cues_text,
            "time_range": time_range,
        })

    map_summaries = [None] * len(chunks)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {}
        for inp in map_inputs:
            messages = [
                {"role": "system", "content": MAP_SYSTEM},
                {"role": "user", "content": MAP_PROMPT.format(cues=inp["cues_text"])},
            ]
            label = f"map-{inp['index']}/{len(chunks)}"
            fut = executor.submit(
                call_api, client, model, messages,
                MAP_MAX_TOKENS, label, str(OUTPUT_DIR),
            )
            futures[fut] = inp["index"]
        for fut in as_completed(futures):
            idx = futures[fut]
            map_summaries[idx] = fut.result()
            print(f"  ✓ chunk {idx} done")

    # 写 map 阶段中间产物
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    map_out = OUTPUT_DIR / f"{out_id}.map.json"
    map_out.write_text(
        json.dumps(map_summaries, ensure_ascii=False, indent=2)
    )
    print(f"\nmap summaries saved: {map_out}")

    # ---- REDUCE 阶段 ----
    print(f"\n--- Reduce 阶段---")
    reduce_messages = [
        {"role": "system", "content": REDUCE_SYSTEM},
        {"role": "user", "content": REDUCE_PROMPT.format(
            chunks=json.dumps(map_summaries, ensure_ascii=False, indent=2),
            duration=duration,
            cues_count=len(cues),
        )},
    ]
    final = run_with_heartbeat(
        lambda: call_api(
            client, model, reduce_messages,
            REDUCE_MAX_TOKENS, "reduce", str(OUTPUT_DIR),
        ),
        label="reduce",
        interval=30,
    )

    final_out = OUTPUT_DIR / f"{out_id}.final.json"
    final_out.write_text(json.dumps(final, ensure_ascii=False, indent=2))
    print(f"final note saved: {final_out}")

    print(f"\n=== 完成 ===")
    print(f"  map 阶段: {len(chunks)} 次 API 调用")
    print(f"  reduce 阶段: 1 次 API 调用")
    print(f"  合计: {len(chunks) + 1} 次 API 调用")


if __name__ == "__main__":
    main()
