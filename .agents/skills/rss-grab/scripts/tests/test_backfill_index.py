#!/usr/bin/env python3
"""`--backfill-index` 单测：只补索引登记，不生成任何内容（全程离线）。

覆盖六处「看起来能跑、其实没在补」或「补错」的形态：

1. **精确匹配**：节目目录里放一个「字母序更靠前的别的期号笔记」，
   请求 A 期时必须登记 A 期自己那篇。取「目录里第一个 .md」的实现会在此变红。
2. **info 与文件名对不上要拒**：文件名是 A 期、内容里的期号是 B 期 ——
   登记出去等于把 B 期的信息写到 A 期行上。
3. **幂等**：同一期连补两次，第二次补 0 条、索引逐字不变。
4. **不改正文**：补登记前后笔记字节逐字相同。
5. **不加载凭证**：把 load_env 换成「一调用就炸」的替身，补登记仍能跑完。
6. **`--project-root` 指向哪就改哪**：改的是临时目录里的索引，
   本仓那份逐字不变 —— 判据是「本仓没被动过」，不是「临时目录里有了」。

另有一条针对节目名的：info 里没有节目标题时，必须退回用笔记所在的节目
目录名 —— 索引的「源」列不能空着，也不能退化成期号。

不调 LLM / 不跑 ASR / 不下载：全部用例只写 pytest 临时目录下的假 info 与假笔记。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parent.parent / "_shared"))

import generate_note_v3 as v3  # noqa: E402

SHOW = "测试节目"
H1 = "aaaa0001"
H2 = "bbbb0002"
DECOY = "deadbee1"
MISSING = "cccc0003"
BAD_ID = "不是号"


def _info(hash8: str, title: str, show: str | None = SHOW) -> dict:
    info = {
        "feed": {"author": "主播", "link": "https://example.com/p"},
        "item": {"title": title, "guid": "g-" + hash8, "guid_hash8": hash8,
                 "pub_date": "Thu, 06 Aug 2026 05:10:23 GMT",
                 "duration_seconds": 2104, "link": "https://example.com/e/1"},
        "local": {"fetched_at": "2026-08-08T19:05:30+0800"},
    }
    if show is not None:
        info["feed"]["title"] = show
    return info


def _setup(tmp_path: pathlib.Path) -> pathlib.Path:
    raw = tmp_path / "rss" / "raw"
    notes = tmp_path / "rss" / "notes" / SHOW
    raw.mkdir(parents=True, exist_ok=True)
    notes.mkdir(parents=True, exist_ok=True)
    for h, title in ((H1, "甲期标题"), (H2, "乙期标题")):
        (raw / f"{title}-{h}.info.json").write_text(
            json.dumps(_info(h, title), ensure_ascii=False), encoding="utf-8")
        (notes / f"{title}-{h}.md").write_text(
            f"---\nguid_hash8: {h}\ntitle: {title}\n---\n\n## 正文\n\n内容。\n",
            encoding="utf-8")
    # 诱饵：字母序排在最前、属于**别的期号**。任何「取第一个」的定位都会选它。
    (raw / f"AAA诱饵-{DECOY}.info.json").write_text(
        json.dumps(_info(DECOY, "AAA诱饵"), ensure_ascii=False), encoding="utf-8")
    (notes / f"AAA诱饵-{DECOY}.md").write_text(
        f"---\nguid_hash8: {DECOY}\n---\n\n诱饵正文\n", encoding="utf-8")
    return notes


def _rows(idx: pathlib.Path) -> list[str]:
    """索引里的数据行（表头行与分隔行都剔掉）。"""
    if not idx.exists():
        return []
    out = []
    for ln in idx.read_text(encoding="utf-8").splitlines():
        if not ln.startswith("| ") or "---" in ln or ln.startswith("| 日期"):
            continue
        out.append(ln)
    return out


def _note_bytes(notes: pathlib.Path) -> dict:
    return {p.name: p.read_bytes() for p in notes.glob("*.md")
            if p.name != "INDEX.md"}


# ── 1. 补齐缺失登记 ────────────────────────────────────────────

def test_缺失的两期都被补上(tmp_path):
    _setup(tmp_path)
    r = v3.backfill_index([H1, H2], project_root=str(tmp_path))
    idx = tmp_path / "rss" / "notes" / "INDEX.md"
    assert r["added"] == [H1, H2], r
    assert r["failed"] == [], r
    rows = _rows(idx)
    assert len(rows) == 2, rows
    assert f"| {H1} |" in rows[0], rows[0]
    assert f"| {H2} |" in rows[1], rows[1]
    # 链接相对全局索引：笔记在节目子目录下
    assert f"./{SHOW}/甲期标题-{H1}.md" in rows[0], rows[0]
    assert f"./{SHOW}/乙期标题-{H2}.md" in rows[1], rows[1]
    # 「源」列用节目标题
    assert f"| {SHOW} |" in rows[0], rows[0]


def test_诱饵笔记不会被登记到别的期号下(tmp_path):
    """定位必须按期号精确后缀匹配，不能取「目录里第一个」。"""
    _setup(tmp_path)
    v3.backfill_index([H1], project_root=str(tmp_path))
    text = (tmp_path / "rss" / "notes" / "INDEX.md").read_text(encoding="utf-8")
    assert DECOY not in text, "把别的期号登记进来了"
    assert "诱饵" not in text, "链接指向了诱饵笔记"
    assert len(_rows(tmp_path / "rss" / "notes" / "INDEX.md")) == 1


def test_找不到info不登记(tmp_path):
    _setup(tmp_path)
    (tmp_path / "rss" / "raw" / f"乙期标题-{H2}.info.json").unlink()
    r = v3.backfill_index([H1, H2], project_root=str(tmp_path))
    assert r["added"] == [H1], r
    assert [i for i, _ in r["failed"]] == [H2], r


def test_找不到笔记不登记(tmp_path):
    _setup(tmp_path)
    (tmp_path / "rss" / "notes" / SHOW / f"甲期标题-{H1}.md").unlink()
    r = v3.backfill_index([H1], project_root=str(tmp_path))
    assert r["added"] == []
    assert [i for i, _ in r["failed"]] == [H1], r
    assert not (tmp_path / "rss" / "notes" / "INDEX.md").exists()


def test_info里的期号与文件名不符要拒(tmp_path):
    """文件名是 A 期、内容里的期号是 B 期 —— 登记出去就是错行。

    判据不只看「failed 里有没有这个期号」：必须同时断言「一行都没写进去」。
    只看 failed 的话，去掉前置校验也能蒙混过关 —— 事后核对照样会报 failed，
    但索引里已经多了一行属于别的期的内容。
    """
    _setup(tmp_path)
    p = tmp_path / "rss" / "raw" / f"甲期标题-{H1}.info.json"
    p.write_text(json.dumps(_info(H2, "甲期标题"), ensure_ascii=False),
                 encoding="utf-8")
    r = v3.backfill_index([H1], project_root=str(tmp_path))
    assert r["added"] == [], r
    assert [i for i, _ in r["failed"]] == [H1], r
    idx = tmp_path / "rss" / "notes" / "INDEX.md"
    assert not idx.exists(), "一致性校验没拦住，索引里被写进了错期号的行"


def test_格式非法的id被拒(tmp_path):
    _setup(tmp_path)
    r = v3.backfill_index([BAD_ID], project_root=str(tmp_path))
    assert r["added"] == [] and [i for i, _ in r["failed"]] == [BAD_ID], r


def test_重复id只补一次(tmp_path):
    _setup(tmp_path)
    r = v3.backfill_index([H1, H1], project_root=str(tmp_path))
    assert r["added"] == [H1], r
    assert len(_rows(tmp_path / "rss" / "notes" / "INDEX.md")) == 1


def test_info缺节目标题时退回节目目录名(tmp_path):
    """「源」列不能空，也不能退化成期号 —— 退回笔记所在的节目目录名。"""
    _setup(tmp_path)
    p = tmp_path / "rss" / "raw" / f"甲期标题-{H1}.info.json"
    p.write_text(json.dumps(_info(H1, "甲期标题", show=None), ensure_ascii=False),
                 encoding="utf-8")
    r = v3.backfill_index([H1], project_root=str(tmp_path))
    assert r["added"] == [H1], r
    row = _rows(tmp_path / "rss" / "notes" / "INDEX.md")[0]
    assert f"| {SHOW} |" in row, row


# ── 2. 幂等 ───────────────────────────────────────────────────

def test_再补一次补0条且索引逐字不变(tmp_path):
    _setup(tmp_path)
    v3.backfill_index([H1, H2], project_root=str(tmp_path))
    idx = tmp_path / "rss" / "notes" / "INDEX.md"
    before = idx.read_text(encoding="utf-8")
    r = v3.backfill_index([H1, H2], project_root=str(tmp_path))
    assert r["added"] == [], r
    assert r["skipped"] == [H1, H2], r
    assert idx.read_text(encoding="utf-8") == before, "重复补登记改动了索引"


def test_已登记的跳过_未登记的照补(tmp_path):
    _setup(tmp_path)
    v3.backfill_index([H1], project_root=str(tmp_path))
    r = v3.backfill_index([H1, H2], project_root=str(tmp_path))
    assert r["skipped"] == [H1] and r["added"] == [H2], r
    assert len(_rows(tmp_path / "rss" / "notes" / "INDEX.md")) == 2


def test_没有可补的不创建索引(tmp_path):
    (tmp_path / "rss" / "notes" / SHOW).mkdir(parents=True)
    r = v3.backfill_index([H1], project_root=str(tmp_path))
    assert r["added"] == []
    assert not (tmp_path / "rss" / "notes" / "INDEX.md").exists()


# ── 3. 不生成、不改正文、不碰凭证 ──────────────────────────────

def test_不改正文(tmp_path):
    notes = _setup(tmp_path)
    before = _note_bytes(notes)
    v3.backfill_index([H1, H2], project_root=str(tmp_path))
    assert _note_bytes(notes) == before, "补登记改动了笔记正文"


def test_不加载凭证(tmp_path, monkeypatch):
    """补登记只读本地文件：加载凭证不该发生。"""
    _setup(tmp_path)

    def boom(*a, **kw):
        raise AssertionError("补登记不该加载凭证")

    monkeypatch.setattr(v3, "load_env", boom)
    r = v3.backfill_index([H1], project_root=str(tmp_path))
    assert r["added"] == [H1]


# ── 3b. 项目根的传递与还原 ─────────────────────────────────────

def _mtime(path: pathlib.Path, ts: int) -> str:
    os.utime(path, (ts, ts))
    return __import__("datetime").datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def test_日期列取的是指定项目根里的抓取时间(tmp_path):
    """显式给了项目根，索引的日期列就必须用那份数据里的抓取时间，不是当前时间。"""
    _setup(tmp_path)
    raw = tmp_path / "rss" / "raw" / f"甲期标题-{H1}.info.json"
    stamp = _mtime(raw, 1754656000)
    v3.backfill_index([H1], project_root=str(tmp_path))
    row = _rows(tmp_path / "rss" / "notes" / "INDEX.md")[0]
    assert row.startswith(f"| {stamp} |"), row


def test_写完不留进程级环境变量(tmp_path, monkeypatch):
    """补登记期间会临时把项目根同步给按环境变量定位的代码，写完必须还原。"""
    _setup(tmp_path)
    monkeypatch.delenv("CODEX_PROJECT_ROOT", raising=False)
    v3.backfill_index([H1], project_root=str(tmp_path))
    assert "CODEX_PROJECT_ROOT" not in os.environ, "给整个进程留了隐式全局副作用"


# ── 4. CLI ────────────────────────────────────────────────────

def test_CLI_不加载凭证也能补(tmp_path, monkeypatch, capsys):
    _setup(tmp_path)
    monkeypatch.setattr(v3, "load_env", lambda *a, **kw: pytest.fail("不该 load_env"))
    for k in ("LLM_API_KEY", "MINIMAX_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(sys, "argv", [
        "generate_note_v3.py", "--project-root", str(tmp_path),
        "--backfill-index", f"{H1},{H2}"])
    assert v3.main() == 0
    assert "补 2 条" in capsys.readouterr().out
    assert len(_rows(tmp_path / "rss" / "notes" / "INDEX.md")) == 2


def test_CLI_重复调用第二次补0条(tmp_path, monkeypatch, capsys):
    _setup(tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "generate_note_v3.py", "--project-root", str(tmp_path),
        "--backfill-index", H1])
    assert v3.main() == 0
    assert "补 1 条" in capsys.readouterr().out
    assert v3.main() == 0
    assert "补 0 条" in capsys.readouterr().out


def test_CLI_只改指定项目根的那份索引(tmp_path, monkeypatch):
    """本仓的索引逐字不变 —— 判据不是「临时目录里有了」，是「本仓没被动过」。"""
    _setup(tmp_path)
    own_idx = SCRIPTS.parent.parent.parent.parent / "rss" / "notes" / "INDEX.md"
    before = own_idx.read_text(encoding="utf-8") if own_idx.exists() else None
    monkeypatch.setattr(sys, "argv", [
        "generate_note_v3.py", "--project-root", str(tmp_path),
        "--backfill-index", H1])
    assert v3.main() == 0
    assert (tmp_path / "rss" / "notes" / "INDEX.md").exists()
    after = own_idx.read_text(encoding="utf-8") if own_idx.exists() else None
    assert after == before, "补登记改动了本仓的索引"


def test_CLI_空id列表报错退出(tmp_path, monkeypatch):
    _setup(tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "generate_note_v3.py", "--project-root", str(tmp_path),
        "--backfill-index", " , "])
    with pytest.raises(SystemExit):
        v3.main()