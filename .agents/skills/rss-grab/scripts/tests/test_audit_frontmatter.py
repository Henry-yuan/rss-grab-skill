#!/usr/bin/env python3
"""`scripts/audit_frontmatter.py`（产物 frontmatter 体检）的单测。

## 为什么要有这个文件

体检脚本是**打标漏跑的安全网**（G1 告警）：`retag_hook.py` 打标失败后
只留一行提示，真正的兜底就是这个脚本在事后报出来。安全网本身坏了
（import 就崩、判据写反、无数据时炸）不会有任何告警 —— 属于
「工具没坏，只是没人记得它在跑」之外更难发现的一类失效。

## 覆盖

1. 能 import（`_tag_limits` 与 `frontmatter_contract` 两条 import 都要通）
2. `--help` 正常退出
3. **无产物目录时不崩**（干净 clone 的常态）
4. G1 漏跑判定：ok + 有正文 + 空标签 → 报；empty 占位 + 空标签 → 不报
5. G2 数量上限：超限报、恰好等于上限不报（边界必须锁）
6. rss 主键形态（8 位 hex）与 `raw/` 三层结构的对账取值器

运行：
    python3 -m pytest .agents/skills/rss-grab/scripts/tests/test_audit_frontmatter.py -v
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import subprocess
import sys

import pytest

TESTS_DIR = pathlib.Path(__file__).resolve().parent.parent      # …/rss-grab/scripts/tests
SKILL_DIR = TESTS_DIR.parent
REPO_ROOT = SKILL_DIR.parent.parent.parent                       # tests→scripts→skill→skills→repo
sys.path.insert(0, str(REPO_ROOT / "scripts"))                  # _tag_limits 在仓根 scripts/ 下

_spec = importlib.util.spec_from_file_location(
    "audit_under_test", REPO_ROOT / "scripts" / "audit_frontmatter.py")
audit = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = audit
_spec.loader.exec_module(audit)

SCRIPT = REPO_ROOT / "scripts" / "audit_frontmatter.py"
NOTE_ID = "a1b2c303"
SHOW = "自检播客"

# 与 frontmatter_contract.FIELDS 同序；测试自己排一遍序，
# 免得「顺序检查」被同一份常量带着走 —— 顺序错了就没人报警了。
DEFAULTS = {
    "note_id": NOTE_ID,
    "title": "自检样本",
    "uploader": SHOW,
    "uploader_id": '""',
    "speaker": '""',
    "type": "podcast",
    "duration": '"1:00"',
    "published_at": '"2026-10-08"',
    "source_url": '"https://www.xiaoyuzhoufm.com/episode/self-test-0001"',
    "subtitle_source": "asr",
    "transcript_status": "ok",
    "source_tags": "[]",
    "tags": "[]",
    "summary": "自检用样本摘要，内容固定，仅供单测触发各类检查。",
    "generated_at": '"2026-10-08T00:00:00+08:00"',
    "description": '""',
    "show_name": SHOW,
}

BODY = ("\n## TL;DR\n\n自检样本。\n\n## 要点总结\n\n- 自检\n\n## 关键引用\n\n> 自检\n")


def build_note(**over) -> str:
    fields = {**DEFAULTS, **over}
    return ("---\n"
            + "\n".join(f"{k}: {fields[k]}" for k in audit.fc.FIELDS)
            + "\n---\n" + BODY)


def build_raw(note_id: str = NOTE_ID, title: str = "自检样本", show: str = SHOW) -> dict:
    """rss 的 info.json 是 feed / item 两层，主键在 `item.guid_hash8`。"""
    return {"feed": {"title": show}, "item": {"guid_hash8": note_id, "title": title}}


def nid_of(md: str) -> str:
    m = re.search(r"^note_id:\s*(\S+)\s*$", md, re.M)
    assert m, "frontmatter 里必须有 note_id"
    return m.group(1).strip('"')


def run_audit(tmp_path, monkeypatch, md: str, raw: dict | None = None) -> "audit.Report":
    """造一棵最小产物树（notes 下带节目子目录，与真实布局同形）并体检。"""
    notes = tmp_path / "rss" / "notes" / SHOW
    raws = tmp_path / "rss" / "raw"
    notes.mkdir(parents=True, exist_ok=True)
    raws.mkdir(parents=True, exist_ok=True)
    (notes / f"自检样本-{nid_of(md)}.md").write_text(md, encoding="utf-8")
    if raw is not None:
        (raws / "自检样本.info.json").write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    return audit.run(["rss"])


def codes(rep, code: str) -> int:
    return sum(1 for f in rep.findings if f.code == code)


class TestModuleSurface:
    def test_imports_cleanly(self):
        """两条 import 都要通：`frontmatter_contract` 与 `_tag_limits`。"""
        assert audit.main is not None
        assert audit.MAX_TAGS >= 1 and audit.JACCARD_NEAR_DUP > 0
        assert tuple(audit.fc.FIELDS)[0] == "note_id"

    def test_configured_for_this_repo_single_source(self):
        """本仓只处理 rss 一个源：配置表与 dispatch 都只认它。"""
        assert sorted(audit.XREF) == ["rss"]
        assert sorted(audit.ID_SHAPES) == ["rss"]
        assert sorted(audit.EXPECTED_HOSTS) == ["rss"]
        # rss 主键 = guid 的 sha256 前 8 位。
        # ⚠️ 反例必须挑**能区分实现差异**的样本：大写 hex（"A1B2C3D4"）能挡住
        # 「正则放宽成 [0-9a-zA-Z]」这类变异，但**挡不住**「放宽成 [0-9a-z]」
        # —— 后者放行的是小写非 hex（"gggggggg"）。两个方向都要有样本，
        # 否则「正则只收小写 hex」这条判据实际上没被任何断言锁住。
        assert audit.ID_SHAPES["rss"].match(NOTE_ID)
        assert audit.ID_SHAPES["rss"].match("01234567")       # 全数字
        for bad_id in ("gggggggg",           # 小写非 hex
                       "ZZZZZZZZ",           # 大写
                       "A1B2C3D4",           # 大写 hex
                       "a1b2c3",             # 太短
                       "a1b2c3d",            # 差一位
                       NOTE_ID + "a"):       # 太长
            assert not audit.ID_SHAPES["rss"].match(bad_id), f"不该放行：{bad_id}"

    def test_cli_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                           capture_output=True, text=True, cwd=REPO_ROOT)
        assert r.returncode == 0, r.stderr
        assert "usage:" in r.stdout

    def test_self_test_passes(self):
        """内置的注入式自检必须 4/4 —— 它是体检脚本自身的回归网。"""
        assert audit.run_selftest() == 0


class TestCleanClone:
    """干净 clone 里还没有 rss/ 目录，不得崩、也不得报假异常。"""

    def test_no_artifact_dir_does_not_crash(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(audit, "ROOT", tmp_path)          # 空的，连 rss/ 都没有
        rep = audit.run(["rss"])
        assert rep.notes_by_source == {"rss": 0}
        assert rep.findings == []

        audit.print_report(rep, True, 5)
        out = capsys.readouterr().out
        assert "没有可体检的笔记" in out         # 给出可执行提示，而不是静默「无异常」

    def test_notes_dir_without_raw_is_reported_as_xref_missing(self, tmp_path, monkeypatch):
        """只有笔记、没有原料时，对账层要报出来（不是静默放过）。"""
        notes = tmp_path / "rss" / "notes" / SHOW
        notes.mkdir(parents=True)
        (notes / f"自检样本-{NOTE_ID}.md").write_text(build_note(), encoding="utf-8")
        monkeypatch.setattr(audit, "ROOT", tmp_path)
        rep = audit.run(["rss"])
        assert codes(rep, "xref_raw_missing") == 1

    def test_json_mode_on_empty_repo(self, tmp_path):
        """--json 在无产物时也要输出合法 JSON，退出码 0。"""
        r = subprocess.run([sys.executable, str(SCRIPT), "--json", "--root", str(tmp_path)],
                           capture_output=True, text=True, cwd=REPO_ROOT)
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["finding_count"] == 0


class TestGTags:
    """G1/G2 是打标漏跑的安全网，判据错了不会有人告警。"""

    def test_g1_fires_for_ok_with_body_and_empty_tags(self, tmp_path, monkeypatch):
        rep = run_audit(tmp_path, monkeypatch, build_note(), build_raw())
        assert codes(rep, "G1_tags_missing") == 1

    def test_g1_not_fired_for_placeholder(self, tmp_path, monkeypatch):
        """transcript_status=empty 的占位产物，tags 空是对的 —— 误报会造成批量噪声。"""
        rep = run_audit(tmp_path, monkeypatch,
                        build_note(transcript_status="empty"), build_raw())
        assert codes(rep, "G1_tags_missing") == 0
        assert any(k.code == "asr_placeholder" for k in rep.known)

    def test_g1_not_fired_when_tags_present(self, tmp_path, monkeypatch):
        rep = run_audit(tmp_path, monkeypatch, build_note(tags='["播客"]'), build_raw())
        assert codes(rep, "G1_tags_missing") == 0

    def test_g2_fires_over_limit(self, tmp_path, monkeypatch):
        tags = "[" + ", ".join(f'"标签{i}"' for i in range(audit.MAX_TAGS + 1)) + "]"
        rep = run_audit(tmp_path, monkeypatch, build_note(tags=tags), build_raw())
        assert codes(rep, "G2_tags_over_limit") == 1

    def test_g2_boundary_at_limit_is_quiet(self, tmp_path, monkeypatch):
        tags = "[" + ", ".join(f'"标签{i}"' for i in range(audit.MAX_TAGS)) + "]"
        rep = run_audit(tmp_path, monkeypatch, build_note(tags=tags), build_raw())
        assert codes(rep, "G2_tags_over_limit") == 0

    def test_g3_g4_are_warnings_not_findings(self, tmp_path, monkeypatch):
        """G3/G4 只报告、不影响退出码 —— 截断与误杀的代价实测更大。"""
        tags = "[" + '"LangGraph"' + "]"
        rep = run_audit(tmp_path, monkeypatch, build_note(tags=tags), build_raw())
        assert codes(rep, "G3_tag_too_long") == 0
        assert any(w.code == "G3_tag_too_long" for w in rep.warnings)
        assert not rep.findings


class TestRawReconcile:
    """本类只关心对账层：统一带上标签，免得 G1 混进同一个 rep 干扰断言。"""

    TAGGED = {"tags": '["播客"]'}

    def test_clean_note_has_no_finding(self, tmp_path, monkeypatch):
        rep = run_audit(tmp_path, monkeypatch, build_note(**self.TAGGED), build_raw())
        assert rep.findings == [], [f.code for f in rep.findings]

    def test_title_mismatch_against_raw_is_reported(self, tmp_path, monkeypatch):
        """对账取值器认的是 `item.title`（rss 的主键在 item 层，不在顶层）。"""
        rep = run_audit(tmp_path, monkeypatch, build_note(**self.TAGGED),
                        build_raw(title="别的标题"))
        assert codes(rep, "xref_mismatch") == 1

    def test_uploader_taken_from_feed_title(self, tmp_path, monkeypatch):
        """`uploader` 取自 `feed.title`（节目名），不是 `item.title`。"""
        rep = run_audit(tmp_path, monkeypatch, build_note(**self.TAGGED),
                        build_raw(show="别的节目"))
        assert codes(rep, "xref_mismatch") == 1

    def test_foreign_host_is_reported(self, tmp_path, monkeypatch):
        rep = run_audit(tmp_path, monkeypatch,
                        build_note(source_url='"https://example.com/episode/x"', **self.TAGGED),
                        build_raw())
        assert codes(rep, "source_url_host") == 1

    def test_wrong_note_id_shape_is_reported(self, tmp_path, monkeypatch):
        rep = run_audit(tmp_path, monkeypatch,
                        build_note(note_id="NOTHASH8", **self.TAGGED), build_raw())
        assert codes(rep, "note_id_shape") == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))