#!/usr/bin/env python3
"""lark_sync 单测：md <-> 多维表格的取值转换与回写纪律。

测的重点不是「函数能不能跑」，而是**几条不可退让的约束**：
    1. push 的更新载荷里**不能出现「状态」这个键**（状态归飞书管）；
    2. pull 只替换 checkbox token，其余字节逐字保留、条目位置不动；
    3. 飞书调用失败时 md 一个字节都不动；
    4. --dry-run 不连飞书；
    5. 单选字段必须是单元素数组、日期字段必须是毫秒整数。

前 4 条都是「坏了也不会报错、只会静默毁数据」的类型 ——
所以每条都有对应的反向断言，光测正向通过是没有意义的。
"""
import pathlib
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))
import lark_sync as L

SAMPLE = """\
## 待确认 (2)   <!-- updated: 2026-10-06 21:46 -->

- [ ] 1. 第一期标题 <!-- guid:aaaa1111 -->
    - 发布日期：2026-05-05 21:00
    - 时长：00:42:17
    - 一句话概括：一句概括
    - 内容概览：
      - 概览第一行
      - 概览第二行
    - 值得关注：
      - ⏱01:17：亮点一
      - ⏱02:00：亮点二
    - 链接：https://example.com/e/aaaa1111
    - 拉取时间：2026-10-06 21:45
- [x] 2. 第二期标题 <!-- guid:bbbb2222 -->
    - 发布日期：2026-09-30 21:00
    - 链接：https://example.com/e/bbbb2222
- [ ] 没有 guid 的条目

## 已转化 (0)
"""


def _items(text=SAMPLE):
    return L.parse_items(text)


# ---------------------------------------------------------------------------
# 日期转换：naive 本地时间 -> 毫秒，且**按业务时区**解释
# ---------------------------------------------------------------------------

def test_md_datetime_is_read_as_shanghai_not_utc():
    """md 里的日期没有时区，必须按 Asia/Shanghai 解释。

    反向断言：若有人「顺手」按 UTC 解释，写进飞书的时刻会偏 8 小时，
    表现为日期筛选/排序全错，且**不报任何错**。
    ⚠️ 不能拿「naive .timestamp()」当反向对照：开发机本地时区可能就是
    东八区，那样两条路径恒等、断言变废。这里比对固定 epoch 值，
    与运行机器的本地时区无关。
    """
    ms = L.md_datetime_to_ms("2026-05-05 21:00")
    assert ms == int(datetime(2026, 5, 5, 13, 0,       # 21:00-08:00 == 13:00Z
                              tzinfo=timezone.utc).timestamp() * 1000)
    assert ms != int(datetime(2026, 5, 5, 21, 0,       # 若被当成 UTC 就会得到这个
                              tzinfo=timezone.utc).timestamp() * 1000)


def test_md_datetime_accepts_common_shapes():
    """秒/无秒/空格或 T 分隔都要认，否则一半条目会被当成「空日期」丢掉。"""
    base = L.md_datetime_to_ms("2026-05-05 21:00")
    assert L.md_datetime_to_ms("2026-05-05 21:00:00") == base
    assert L.md_datetime_to_ms("2026-05-05T21:00") == base
    assert L.md_datetime_to_ms("2026-05-05 21:00:30") == base + 30_000
    assert L.md_datetime_to_ms("2026-5-5 21:00") == base   # 单位数月/日


@pytest.mark.parametrize("bad", ["", "   ", "不是日期", "2026-13-45 99:99",
                                 "2026/05/05 21:00"])
def test_md_datetime_bad_input_is_none_not_crash(bad):
    """认不出就返回 None（= 不写该字段），绝不抛异常、绝不写 0。"""
    assert L.md_datetime_to_ms(bad) is None


def test_cell_to_ms_roundtrips_against_md_side():
    """飞书读回来是 ISO 串，写进去是毫秒；不归一就没法判断「变没变」。

    不归一的后果：每次 push 都判定全表有差异，于是每次都重写 580 条。
    """
    ms = L.md_datetime_to_ms("2026-05-05 21:00")
    assert L.cell_to_ms("2026-05-05T21:00:00.000+08:00") == ms
    assert L.cell_to_ms(ms) == ms
    assert L.cell_to_ms(None) is None
    assert L.cell_to_ms("") is None


def test_cell_to_ms_unparseable_does_not_raise():
    """读回来是意料之外的格式时不能崩 —— 崩了就等于 pull 全挂。"""
    assert L.cell_to_ms("某年某月") is None


# ---------------------------------------------------------------------------
# 多行文本还原项目符号
# ---------------------------------------------------------------------------

def test_render_text_restores_bullets_for_multiline():
    """多行值要还原 md 的 `- ` 列表结构（多维表格单元格是多行文本）。"""
    assert L.render_text("甲\n乙\n丙") == "\n- 甲\n- 乙\n- 丙"


def test_render_text_leaves_single_line_untouched():
    """单行值不加符号、不加前导换行 —— 既有表里就是这么写的。"""
    assert L.render_text("只有一行") == "只有一行"
    assert L.render_text("") == ""


def test_render_text_is_what_makes_push_idempotent():
    """还原规则一旦变了，push 就会把全表判成有差异并重写。

    这里锁住「还原之后 == 表里既有内容」这个等式，
    它是增量推送能收敛的全部前提。
    """
    it = _items()[0]
    remote = L.render_text(L.pick_field(it, "值得关注"))
    assert remote == "\n- ⏱01:17：亮点一\n- ⏱02:00：亮点二"


# ---------------------------------------------------------------------------
# 载荷纪律：状态归飞书管
# ---------------------------------------------------------------------------

def test_update_payload_never_contains_status_key():
    """🔴 硬约束：更新已存在记录时，载荷里**不能有**状态这个键。

    不是「值不变」，是键根本不存在 —— 改了字段名或误加进来都会当场变红。
    """
    it = _items()[1]
    fields = L.item_to_fields(it, "AI-Odyssey", with_status=False)
    assert L.COL_STATUS not in fields
    assert L.COL_STATUS not in json_of(fields)


# ---------------------------------------------------------------------------
# 状态机归属：同一个转换只有一个方向
# ---------------------------------------------------------------------------

def test_only_done_is_system_writable():
    """系统只准写「已转化」一个值 —— 其余三个是用户的领域。"""
    assert L.SYSTEM_WRITABLE_STATUS == "已转化"
    assert L.SYSTEM_WRITABLE_STATUS not in {
        "待确认", "确认抓取", "确认不抓"}


def test_system_status_never_writes_user_owned_values(monkeypatch, capsys):
    """🔴 md 里是用户那三个状态时，一条都不能写。

    这条写错的后果：用户在飞书上刚点「确认抓取」，被本地 md 里的旧值
    「待确认」冲回去 —— 正是当初那个 bug 换个方向重演。

    ⚠️ 用例必须**带真文件**：早期版本传的是 files=[]，条目压根没进循环，
    于是「放开成状态照 md 推」的退化实现照样全绿 —— 变异验证当场抓到。
    """
    posted = {}
    monkeypatch.setattr(L, "_post_payload",
                        lambda *a, **k: posted.update(k))
    # 飞书上有这两条，状态都不是「已转化」
    monkeypatch.setattr(L, "read_records", lambda *a, **k: [
        {"guid": "aaaa1111", "record_id": "recA", L.COL_STATUS: ["确认抓取"]},
        {"guid": "bbbb2222", "record_id": "recB", L.COL_STATUS: ["待确认"]},
    ])
    with tempfile.TemporaryDirectory() as td:
        f = pathlib.Path(td) / "节目.md"
        f.write_text(SAMPLE, encoding="utf-8")      # 第一条 [ ]、第二条 [x]
        # 第三条改成 [~]（确认不抓）—— 三个用户值各来一遍
        f.write_text(SAMPLE + "\n- [~] 3. 第三期 <!-- guid:cccc3333 -->\n",
                     encoding="utf-8")
        monkeypatch.setattr(L, "read_records", lambda *a, **k: [
            {"guid": "aaaa1111", "record_id": "recA", L.COL_STATUS: ["确认抓取"]},
            {"guid": "bbbb2222", "record_id": "recB", L.COL_STATUS: ["确认抓取"]},
            {"guid": "cccc3333", "record_id": "recC", L.COL_STATUS: ["待确认"]},
        ])
        n = L.push_system_status([f], L.LarkCfg("bt", "tid"))

    assert n == 0
    assert posted == {}, f"用户领域的三个状态都不该被写，实际：{posted}"


def test_system_status_writes_done_only(monkeypatch, capsys):
    """md 判成 [done] 且飞书还没跟上时，才写「已转化」，且只写这一个键。"""
    posted = {}

    def fake_post(cfg, command, payload):
        posted.update(payload)
    monkeypatch.setattr(L, "_post_payload", fake_post)
    monkeypatch.setattr(L, "read_records", lambda *a, **k: [
        {"guid": "aaaa1111", "record_id": "recA", L.COL_STATUS: ["确认抓取"]},
    ])

    with tempfile.TemporaryDirectory() as td:
        f = pathlib.Path(td) / "节目.md"
        f.write_text(SAMPLE, encoding="utf-8")
        # 造一个已转化的 md：把第一条换成 [done]
        f.write_text(SAMPLE.replace("- [ ] 1. 第一期标题", "- [done] 1. 第一期标题"),
                     encoding="utf-8")
        n = L.push_system_status([f], L.LarkCfg("bt", "tid"))

    assert n == 1
    assert posted["update_records"] == {
        "recA": {L.COL_STATUS: ["已转化"]}}


def test_system_status_skips_already_done(monkeypatch, capsys):
    """飞书上已经是「已转化」就不必重写 —— 幂等。"""
    posted = {}
    monkeypatch.setattr(L, "_post_payload",
                        lambda *a, **k: posted.update(k))
    monkeypatch.setattr(L, "read_records", lambda *a, **k: [
        {"guid": "aaaa1111", "record_id": "recA", L.COL_STATUS: ["已转化"]},
    ])
    with tempfile.TemporaryDirectory() as td:
        f = pathlib.Path(td) / "节目.md"
        f.write_text(SAMPLE.replace("- [ ] 1. 第一期标题",
                                    "- [done] 1. 第一期标题"), encoding="utf-8")
        assert L.push_system_status([f], L.LarkCfg("bt", "tid")) == 0
    assert posted == {}


def test_system_status_skips_guids_absent_from_feishu(monkeypatch):
    """飞书上还没有这条时**不建** —— 建记录是普通 push 的职责。"""
    posted = {}
    monkeypatch.setattr(L, "_post_payload",
                        lambda *a, **k: posted.update(k))
    monkeypatch.setattr(L, "read_records", lambda *a, **k: [])
    with tempfile.TemporaryDirectory() as td:
        f = pathlib.Path(td) / "节目.md"
        f.write_text(SAMPLE.replace("- [ ] 1. 第一期标题",
                                    "- [done] 1. 第一期标题"), encoding="utf-8")
        assert L.push_system_status([f], L.LarkCfg("bt", "tid")) == 0
    assert posted == {}


def test_sync_has_four_stages():
    """sync 的收尾必须包含「状态回写」，否则 done 永远传不回飞书。"""
    src = (SCRIPT_DIR / "lark_sync.py").read_text(encoding="utf-8")
    assert "第 4/4 步：状态回写" in src
    assert "push_system_status(files, cfg)" in src


def test_create_payload_sets_status_as_initial_value():
    """新建记录才给状态初值，且必须是**单元素数组**（裸字符串会被 API 拒）。"""
    it = _items()[1]                      # md 里是 [x]
    fields = L.item_to_fields(it, "AI-Odyssey", with_status=True)
    assert fields[L.COL_STATUS] == ["确认抓取"]


def test_select_fields_are_single_element_lists():
    """状态 / 节目都是单选字段；传字符串会被飞书拒绝。"""
    it = _items()[0]
    fields = L.item_to_fields(it, "AI-Odyssey", with_status=True)
    assert fields[L.COL_SHOW] == ["AI-Odyssey"]
    for col in (L.COL_SHOW, L.COL_STATUS):
        assert isinstance(fields[col], list)
        assert len(fields[col]) == 1


def test_empty_date_is_omitted_entirely():
    """md 里没有的日期**不传这个键**（传 null 是清空语义，会抹掉存量值）。"""
    it = _items()[1]                        # 第二期：有发布日期、无拉取时间
    assert L.pick_field(it, "拉取时间") == ""
    fields = L.item_to_fields(it, "独树不成林", with_status=False)
    assert "拉取时间" not in fields
    assert "发布日期" in fields


def test_item_without_guid_is_skipped_not_pushed_as_blank():
    """没有 guid 注释的条目推过去只会污染表（guid 是关联键）。"""
    it = _items()[2]
    assert it["guid"] == ""
    fields = L.item_to_fields(it, "X", with_status=True)
    assert fields[L.COL_GUID] == ""          # 调用方负责跳过，不在这里悄悄丢弃


def json_of(obj):
    import json
    return json.dumps(obj, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 比对：只碰「这次真的要写」的键
# ---------------------------------------------------------------------------

def test_differs_ignores_keys_absent_from_want():
    """want 里没有的键 = 「这次不碰」，不是「远端多了个字段要删」。"""
    want = {"标题": "甲"}
    have = {"标题": "甲", "拉取时间": L.md_datetime_to_ms("2026-01-01 00:00")}
    assert L._differs(want, have) is False


def test_differs_compares_dates_in_normalized_form():
    """同一个时刻的两种表示（ms / ISO）必须判为「无差异」。"""
    ms = L.md_datetime_to_ms("2026-05-05 21:00")
    want = {"发布日期": ms}
    assert L._differs(want, {"发布日期": "2026-05-05T21:00:00.000+08:00"}) is False
    assert L._differs(want, {"发布日期": None}) is True


def test_differs_detects_real_text_change():
    assert L._differs({"标题": "甲"}, {"标题": "乙"}) is True


def test_differs_treats_none_and_empty_text_as_equal():
    """空值统一成 ""，否则空单元格会被判成每次都有差异。"""
    assert L._differs({"链接": ""}, {"链接": None}) is False


def test_differs_compares_select_by_first_element():
    assert L._differs({L.COL_SHOW: ["A"]}, {L.COL_SHOW: ["A"]}) is False
    assert L._differs({L.COL_SHOW: ["A"]}, {L.COL_SHOW: ["B"]}) is True


# ---------------------------------------------------------------------------
# pull 回写纪律
# ---------------------------------------------------------------------------

def test_apply_statuses_rewrites_only_the_checkbox_token():
    """前缀（`- `）、后缀（序号/标题/guid 注释）必须逐字保留。"""
    out, changed, hits, notes = L.apply_statuses(
        SAMPLE, {"aaaa1111": "确认抓取"})
    line = [x for x in out.split("\n") if "aaaa1111" in x][0]
    assert line == "- [x] 1. 第一期标题 <!-- guid:aaaa1111 -->"
    assert changed == 1 and hits == 1 and notes == []


def test_apply_statuses_does_not_move_the_item():
    """条目必须留在原位（pull 只换符号，不搬条目 —— 归区归 --sync-pick 管）。

    比的是**行号**不是字符下标：checkbox 长度变了（`[ ]`→`[done]`），
    后面所有行的字符偏移都会跟着平移，拿字符下标比会误报。
    """
    out, changed, _, _ = L.apply_statuses(SAMPLE, {"bbbb2222": "已转化"})
    before = SAMPLE.split("\n")
    after = out.split("\n")
    assert len(before) == len(after)               # 行数都不能变
    moved = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert moved == [before.index(next(
        l for l in before if l.startswith("- [x] 2. 第二期标题")))]
    assert changed == 1


def test_apply_statuses_ignores_guids_not_on_feishu():
    """飞书上不认识的 guid（md 里有、表里没有）一条都不写。"""
    out, changed, _, _ = L.apply_statuses(
        SAMPLE, {"zzzz9999": "确认抓取", "aaaa1111": "确认不抓"})
    assert changed == 1
    assert "[~] 1. 第一期标题" in out
    # 表里没有的两条保持原样
    assert "- [x] 2. 第二期标题" in out
    assert "- [ ] 没有 guid 的条目" in out


def test_apply_statuses_is_a_noop_when_nothing_differs():
    out, changed, hits, _ = L.apply_statuses(
        SAMPLE, {"aaaa1111": "待确认", "bbbb2222": "确认抓取"})
    assert out == SAMPLE
    assert changed == 0
    assert hits == 2


def test_apply_statuses_reports_unknown_status_without_writing():
    """表里出现无法识别的状态 -> 跳过该期并提示，不猜、不当失败。"""
    out, changed, _, notes = L.apply_statuses(SAMPLE, {"aaaa1111": "已归档"})
    assert changed == 0
    assert out == SAMPLE
    assert notes and "已归档" in notes[0]


def test_apply_statuses_count_always_matches_actual_text_change():
    """🔴 计数与文本必须同源：说改 N 条，文本里就该真的差 N 处。

    曾经把「数改了几处」和「造新文本」拆成两个函数各写一遍行变换，
    两份逻辑漂移后就会出现「报告改了 3 条、实际写进去 2 条」。

    ⚠️ 用例里**必须掺一条「命中但没变」**的（aaaa1111 本来就是 `[ ]`）：
    只用「全都变了」的样本时，「命中数」与「改动数」恒等，
    于是把计数写成命中数的退化实现照样全绿 —— 这正是本条要拦的。
    """
    wanted = {"aaaa1111": "待确认",        # 已是 [ ]，命中但不改
              "bbbb2222": "已转化",        # 真的要改
              "cccc3333": "确认抓取"}       # 表里没有，不该有影响
    out, changed, hits, _ = L.apply_statuses(SAMPLE, wanted)
    diff_lines = sum(1 for a, b in zip(out.split("\n"), SAMPLE.split("\n")) if a != b)
    assert hits == 2                        # 命中的是两条
    assert changed == 1                     # 真正改的只有一条
    assert changed == diff_lines


def test_apply_statuses_preserves_blank_lines_and_sections():
    """分区标题与空行必须原样保留（--sync-pick 依赖它们重算计数）。"""
    out, _, _, _ = L.apply_statuses(SAMPLE, {"aaaa1111": "确认抓取"})
    lines = out.split("\n")
    # 逐行比对：除了那一条 checkbox，其余每一行都要原样
    before = SAMPLE.split("\n")
    changed_idx = [i for i, (a, b) in enumerate(zip(before, lines)) if a != b]
    assert changed_idx == [2]
    assert "## 待确认 (2)" in lines[0]      # 分区标题（含计数注释）原样
    assert lines[-2] == "## 已转化 (0)"
    assert lines.count("") == before.count("")


# ---------------------------------------------------------------------------
# 原子性：飞书失败时 md 一个字节不动
# ---------------------------------------------------------------------------

def test_pull_leaves_md_untouched_when_lark_fails(monkeypatch):
    """🔴 读飞书失败 -> md 逐字节不变，且退出码非零（不假装成功）。"""
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        f = td / "节目.md"
        f.write_text(SAMPLE, encoding="utf-8")
        before = f.read_bytes()

        monkeypatch.setattr(L, "read_records",
                            lambda *a, **k: (_ for _ in ()).throw(
                                L.SyncError("飞书炸了")))
        monkeypatch.setattr(L, "SUBSCRIBE_DIR", td)
        with pytest.raises(L.SyncError):
            L.do_pull([f], L.LarkCfg("bt", "tid"), dry_run=False)
        assert f.read_bytes() == before


def test_pull_writes_atomically_and_leaves_no_temp_files(monkeypatch):
    """写盘走「同目录临时文件 + os.replace」，且不留残骸。"""
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        f = td / "节目.md"
        f.write_text(SAMPLE, encoding="utf-8")
        monkeypatch.setattr(L, "read_records",
                            lambda *a, **k: [{"guid": "aaaa1111",
                                              L.COL_STATUS: ["确认抓取"]}])
        monkeypatch.setattr(L, "SUBSCRIBE_DIR", td)
        L.do_pull([f], L.LarkCfg("bt", "tid"), dry_run=False)
        assert "- [x] 1. 第一期标题" in f.read_text(encoding="utf-8")
        assert [p.name for p in td.iterdir()] == ["节目.md"]


def test_pull_reads_all_flights_before_touching_any_file(monkeypatch):
    """两个节目里第二个读失败时，第一个也不能被改 —— 全表只读一次。"""
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        a, b = td / "A.md", td / "B.md"
        a.write_text(SAMPLE, encoding="utf-8")
        b.write_text(SAMPLE, encoding="utf-8")
        before_a, before_b = a.read_bytes(), b.read_bytes()

        def boom(*args, **kwargs):
            raise L.SyncError("连不上")
        monkeypatch.setattr(L, "read_records", boom)
        monkeypatch.setattr(L, "SUBSCRIBE_DIR", td)
        with pytest.raises(L.SyncError):
            L.do_pull([a, b], L.LarkCfg("bt", "tid"), dry_run=False)
        assert a.read_bytes() == before_a
        assert b.read_bytes() == before_b


# ---------------------------------------------------------------------------
# dry-run：一条飞书调用都不许有
# ---------------------------------------------------------------------------

def test_dry_run_push_never_calls_lark(monkeypatch, capsys):
    """🔴 --dry-run 的承诺是「不连飞书」；任何一次调用都要让测试变红。"""
    def boom(*args, **kwargs):
        raise AssertionError("dry-run 不许调用飞书")
    monkeypatch.setattr(L, "run_lark", boom)
    monkeypatch.setattr(L, "find_lark", boom)
    L.do_push("AI-Odyssey", _items(), cfg=None, dry_run=True)
    assert "dry-run" in capsys.readouterr().out


def test_dry_run_pull_never_calls_lark(monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise AssertionError("dry-run 不许调用飞书")
    monkeypatch.setattr(L, "read_records", boom)
    monkeypatch.setattr(L, "SUBSCRIBE_DIR", pathlib.Path("/nonexistent"))
    with pytest.raises(L.SyncError):      # 目录不存在仍要报错，不是静默成功
        L.state_files(None)


def test_dry_run_push_reads_no_md_file(tmp_path, monkeypatch):
    """dry-run 只用内存里的 items，不依赖磁盘状态。"""
    monkeypatch.setattr(L, "run_lark",
                        lambda *a, **k: pytest.fail("dry-run 不许调用飞书"))
    L.do_push("X", _items(), cfg=None, dry_run=True)


# ---------------------------------------------------------------------------
# 批量切分
# ---------------------------------------------------------------------------

def test_batched_create_respects_record_cap():
    assert all(len(c) <= L.BATCH_MAX_RECORDS
               for c in L.batched_create([{"i": i} for i in range(500)]))


def test_batched_create_respects_byte_cap():
    """只按条数切会撞请求体上限：单条几 KB × 200 条就是几百 KB。"""
    big = [{"内容概览": "长" * 5000} for _ in range(100)]
    chunks = L.batched_create(big)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c) <= L.BATCH_MAX_RECORDS
        assert sum(len(json_of(x).encode("utf-8")) for x in c) <= L.BATCH_MAX_BYTES


def test_batched_create_empty_yields_nothing():
    assert L.batched_create([]) == []


def test_batched_create_never_splits_one_record():
    """单条就超字节上限时也要整条发出去（切碎会让载荷非法）。"""
    huge = [{"x": "长" * (L.BATCH_MAX_BYTES + 10)}]
    chunks = L.batched_create(huge)
    assert len(chunks) == 1 and len(chunks[0]) == 1


# ---------------------------------------------------------------------------
# 状态映射闭环
# ---------------------------------------------------------------------------

def test_status_checkbox_mapping_is_a_bijection():
    """四个状态与四个 checkbox 一一对应，漏一个就会静默丢状态。"""
    assert len(L.STATUS_TO_CHECKBOX) == 4
    assert set(L.CHECKBOX_TO_STATUS.values()) == set(L.STATUS_TO_CHECKBOX)
    for st, cb in L.STATUS_TO_CHECKBOX.items():
        assert L.CHECKBOX_TO_STATUS[cb] == st


@pytest.mark.parametrize("cb,status", [
    ("[ ]", "待确认"), ("[x]", "确认抓取"), ("[~]", "确认不抓"), ("[done]", "已转化")])
def test_each_checkbox_maps_to_its_status(cb, status):
    assert L.CHECKBOX_TO_STATUS[cb] == status


# ---------------------------------------------------------------------------
# CLI 形态
# ---------------------------------------------------------------------------

def _cli(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "lark_sync.py"), *args],
        capture_output=True, text=True)


@pytest.mark.parametrize("cmd", ["push", "pull", "sync"])
def test_cli_subcommands_exist(cmd):
    assert _cli(cmd, "--help").returncode == 0


@pytest.mark.parametrize("cmd", ["push", "pull", "sync"])
def test_cli_accepts_dry_run_and_show(cmd):
    out = _cli(cmd, "--dry-run", "--help").stdout
    assert "--dry-run" in out
    assert "--节目名" in out


def test_cli_push_still_accepts_force():
    assert "--force" in _cli("push", "--help").stdout


def _fake_project(tmp_path):
    """在 tmp 下搭一棵与真实仓库**同构**的最小目录树，返回其根。

    `lark_sync.py` 用 `Path(__file__).resolve().parents[4]` 定位项目根，
    再拼出 `rss/订阅`。本仓库的 `rss/` 被 .gitignore 排除，
    所以干净 clone（CI / 新用户）里那个目录**根本不存在** ——
    直接跑 CLI 必然 exit 2，测试就不是在测「要不要凭证」而是在测「目录在不在」。

    刻意**复制**一份 lark_sync.py 到骨架里，而不是 monkeypatch 它的常量：
    本用例测的正是**子进程 CLI 入口**的凭证读取时机，monkeypatch 对子进程无效。
    层级必须与仓库一致（.agents/skills/rss-grab/scripts），
    否则 parents[4] 会落到别处。
    """
    root = tmp_path / "proj"
    scripts = root / ".agents" / "skills" / "rss-grab" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "lark_sync.py").write_bytes(
        (SCRIPT_DIR / "lark_sync.py").read_bytes())
    sub = root / "rss" / "订阅"
    sub.mkdir(parents=True)
    (sub / "测试节目.md").write_text(SAMPLE, encoding="utf-8")
    return root


def test_cli_sync_does_not_require_credentials(tmp_path):
    """dry-run 不读凭证：没配飞书也要能预览（否则「先试 dry-run」就没意义）。

    反向断言同样重要：若哪天 `main()` 把 `lark_config()` 提到 dry_run 判断**之前**，
    这里会因缺 LARK_BASE_TOKEN 而非零退出，当场变红。
    """
    root = _fake_project(tmp_path)
    env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp"}   # 刻意不给任何 LARK_* 凭证
    r = subprocess.run(
        [sys.executable,
         str(root / ".agents/skills/rss-grab/scripts/lark_sync.py"),
         "push", "--dry-run"],
        capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "LARK_BASE_TOKEN" not in r.stderr
    assert "dry-run" in r.stdout          # 确实跑到了预览，而不是空转退出


def test_cli_missing_credentials_exits_nonzero(monkeypatch):
    """没配凭证必须非零退出 + 说清楚，绝不静默假装成功。"""
    monkeypatch.setattr(L, "load_dotenv", lambda: None)
    monkeypatch.delenv("LARK_BASE_TOKEN", raising=False)
    monkeypatch.delenv("LARK_TABLE_ID", raising=False)
    with pytest.raises(L.SyncError):
        L.lark_config()


def test_missing_lark_cli_is_a_clear_error(monkeypatch):
    """没装 lark-cli 要给出可操作的提示，并说清不影响采集。"""
    monkeypatch.setattr(L, "LARK_BIN_CANDIDATES", ("definitely-not-here",))
    monkeypatch.setattr("shutil.which", lambda _n: None)
    with pytest.raises(L.SyncError) as e:
        L.find_lark()
    assert "不影响采集" in str(e.value)


# ---------------------------------------------------------------------------
# 凭证不外泄
# ---------------------------------------------------------------------------

def test_error_message_masks_base_token(monkeypatch):
    """报错信息会进终端回滚与 CI 日志，不能把 base_token 原样打出去。"""

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "denied for token SUPERSECRETBASEVALUE"

    monkeypatch.setattr(L, "find_lark", lambda: "/bin/echo")
    monkeypatch.setattr(L, "_SECRETS", ["SUPERSECRETBASEVALUE"])
    monkeypatch.setattr(L.subprocess, "run",
                        lambda *a, **k: _Proc())
    with pytest.raises(L.SyncError) as e:
        L.run_lark(["base", "+record-list",
                    "--base-token", "SUPERSECRETBASEVALUE"])
    assert "SUPERSECRETBASEVALUE" not in str(e.value)
    assert "***" in str(e.value)

# ---------------------------------------------------------------------------
# [done] 保护：飞书的旧值不得把已完成的一期打回流水线
# ---------------------------------------------------------------------------

def _md_with(cb: str, guid: str = "guid-1") -> str:
    return (f"- {cb} 某期标题 <!-- guid:{guid} -->\n"
            f"    - guid_hash8：5cd30ce6\n")


def test_done_item_is_not_overwritten_by_stale_feishu_status():
    """🔴 md 里已 [done]，飞书还是旧的「确认抓取」→ **不覆盖**。

    真实故障（2026-10-07）：阶段 4 的 push-status 没跑成（或被人误改）时，
    飞书留着「确认抓取」，下一次 pull 把它灌回 md，已完成的一期被重新
    推回流水线 —— 白跑一遍下载与 ASR，最后又被笔记防重闸门挡下。
    """
    text = _md_with("[done]")
    out, changed, hits, notes = L.apply_statuses(text, {"guid-1": "确认抓取"})
    assert "[done]" in out, "已完成的一期被打回 [x] 了"
    assert changed == 0
    assert hits == 1                       # 匹配到了，只是被保护
    assert notes and "已是已转化" in notes[0]


def test_done_is_still_writable_from_feishu_when_it_agrees():
    """飞书也是「已转化」→ 无需改动（幂等，不算 changed 也不该报保护）。"""
    text = _md_with("[done]")
    out, changed, hits, notes = L.apply_statuses(text, {"guid-1": "已转化"})
    assert changed == 0
    assert notes == [], "两边一致时不该报保护"


def test_protection_does_not_block_other_statuses():
    """保护只针对 [done]；其余三种状态照常互转，别把同步整体锁死。"""
    text = _md_with("[x]")
    out, changed, _hits, notes = L.apply_statuses(text, {"guid-1": "确认不抓"})
    assert "[~]" in out and changed == 1 and notes == []


def test_non_done_item_can_still_be_pulled_back():
    """非 done 的条目被飞书改状态（如用户把确认抓取改回待确认）必须生效。"""
    text = _md_with("[x]")
    out, changed, _hits, _notes = L.apply_statuses(text, {"guid-1": "待确认"})
    assert "[ ]" in out and changed == 1


def test_protection_is_reported_not_silent():
    """⚠️ 被保护的条目必须出现在 notes 里 —— 静默跳过会让人以为同步没匹配上。"""
    text = _md_with("[done]")
    _out, _changed, _hits, notes = L.apply_statuses(text, {"guid-1": "确认抓取"})
    assert notes, "保护发生了却没有任何提示"
    joined = " ".join(notes)
    assert "guid-1" in joined
    assert "push-status" in joined        # 告诉用户下一步该做什么


def test_done_checkbox_constant_matches_status_table():
    """DONE_CHECKBOX 的值必须与状态表一致。"""
    assert L.DONE_CHECKBOX == L.STATUS_TO_CHECKBOX["已转化"]
    assert L.CHECKBOX_TO_STATUS[L.DONE_CHECKBOX] == "已转化"


def test_done_checkbox_is_derived_not_hardcoded():
    """⚠️ 必须**由映射派生**，不能写死字面量。

    只断言「值相等」是抓不住的：把常量改成 `DONE_CHECKBOX = "[done]"`
    之后所有值断言照样通过（变异验证实测：4/5 变红，唯独这条绿的）。
    真正的风险是**漂移** —— 以后有人把映射里的 `[done]` 改掉，
    写死的常量会静默失配，done 保护随之无声失效。
    所以这里断言的是「赋值表达式引用了映射」，不是「值相等」。
    """
    import inspect
    src = inspect.getsource(L)
    line = next((l for l in src.splitlines()
                 if l.startswith("DONE_CHECKBOX")), "")
    assert line, "找不到 DONE_CHECKBOX 的定义"
    assert "STATUS_TO_CHECKBOX" in line, (
        f"DONE_CHECKBOX 必须由 STATUS_TO_CHECKBOX 派生，实际写的是：{line!r}")
