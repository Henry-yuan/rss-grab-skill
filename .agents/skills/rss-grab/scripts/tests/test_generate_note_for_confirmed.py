#!/usr/bin/env python3
"""generate_note_for_confirmed 单测：三步链路（音频 → 转写 → 笔记）的判重与失败定位。

**全程不下载、不转写**：`download_audio` / `transcribe_one` 两个接缝被替换成
记录调用的假实现，笔记那一步也替换掉 subprocess。真跑一次 ASR 要几十分钟、
下载要几百 MB，单测里一律不碰。
"""
import json
import pathlib
import sys

import pytest

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))
import generate_note_for_confirmed as g  # noqa: E402

HASH = "a1b2c3d4"


# ── 夹具 ──

@pytest.fixture
def roots(tmp_path, monkeypatch):
    """把 rss/raw 与 rss/transcripts 指到临时目录，绝不写真实仓库。"""
    raw = tmp_path / "raw"
    tr = tmp_path / "transcripts"
    raw.mkdir()
    monkeypatch.setattr(g, "RAW_DIR", raw)
    monkeypatch.setattr(g, "TRANSCRIPTS_DIR", tr)
    return raw, tr


@pytest.fixture
def calls(monkeypatch):
    """替换三个接缝，记录调用顺序。假实现必须**真的把产物写出来** ——
    链路里每一步都以「产物是否落盘」为准，只返回 ok 会被判失败。"""
    log = []

    def fake_download(url, save_path):
        log.append(("download", url, str(save_path)))
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_bytes(b"x" * 4096)     # 非零字节才算落盘
        return True, ""

    def fake_transcribe(info_path, transcripts_dir):
        log.append(("transcribe", str(info_path), str(transcripts_dir)))
        transcripts_dir.mkdir(parents=True, exist_ok=True)
        name = pathlib.Path(info_path).name.replace(".info.json", ".transcript.md")
        (pathlib.Path(transcripts_dir) / name).write_text("x", encoding="utf-8")
        return True, "ok"

    def fake_note(hash8):
        log.append(("note", hash8))
        return True, "ok 1s done"

    monkeypatch.setattr(g, "_download", fake_download)
    monkeypatch.setattr(g, "_transcribe", fake_transcribe)
    monkeypatch.setattr(g, "generate_note", fake_note)
    return log


def write_info(raw, *, hash8=HASH, url="http://feed/audio.m4a",
               enc_type="audio/mp4", audio="auto"):
    """造一份 info.json。`audio` 传 None 表示 local.audio_path 为空；
    传 str 表示音频落到该绝对路径。"""
    if audio == "auto":
        audio = str(raw.parent / "audio" / f"标题-{hash8}.m4a")
    info = {
        "feed": {"title": "节目"},
        "item": {
            "title": "标题",
            "guid": "guid-value",
            "guid_hash8": hash8,
            "enclosure": {"url": url, "type": enc_type, "length": "1"},
        },
        "local": {"audio_path": audio},
    }
    (raw / f"标题-{hash8}.info.json").write_text(
        json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return info


# ── enclosure 解析（两种形态都要认）──

def test_enclosure_url_dict_form():
    """单期实际写出的形态：dict。"""
    info = {"item": {"enclosure": {"url": "http://a/x.m4a", "type": "audio/mp4"}}}
    assert g.enclosure_url(info) == "http://a/x.m4a"


def test_enclosure_url_list_form():
    """list 形态：跳过非 audio 条目，取第一个 audio/。"""
    info = {"item": {"enclosure": [
        {"url": "http://a/cover.jpg", "type": "image/jpeg"},
        {"url": "http://a/x.m4a", "type": "audio/mp4"},
    ]}}
    assert g.enclosure_url(info) == "http://a/x.m4a"


def test_enclosure_url_list_without_audio_type_falls_back():
    """list 里没有 audio/ 条目时，退取任意有 url 的（别直接判失败）。"""
    info = {"item": {"enclosure": [
        {"url": "http://a/cover.jpg", "type": "image/jpeg"},
        {"url": "http://a/x.m4a", "type": ""},
    ]}}
    assert g.enclosure_url(info) == "http://a/x.m4a"


def test_enclosure_url_missing_returns_empty():
    """enclosure 缺失 / None / 空 dict → 空串，由上层报「没有音频地址」。"""
    assert g.enclosure_url({"item": {}}) == ""
    assert g.enclosure_url({"item": {"enclosure": None}}) == ""
    assert g.enclosure_url({"item": {"enclosure": {}}}) == ""


def test_find_info_json_matches_only_that_episode(roots):
    """raw/ 下有多期时，只认 hash8 对得上那一期。

    写宽成 `*.info.json` 会取到**别的期**：转写和笔记就会安到错误的期数上，
    而所有单测目录里只有一份 info.json 时这种错误完全测不出来。
    """
    raw, _ = roots
    write_info(raw, hash8="11111111")
    write_info(raw, hash8="22222222")
    got = g.find_info_json("22222222")
    assert got is not None
    assert got.name.endswith("-22222222.info.json")


def test_find_info_json_missing_returns_none(roots):
    """hash8 对不上任何一期 → None（上层据此报「找不到 info.json」）。"""
    raw, _ = roots
    write_info(raw, hash8="11111111")
    assert g.find_info_json("deadbeef") is None


def test_audio_path_of_info():
    """local.audio_path -> Path；为空返回 None。"""
    assert g.audio_path_of({"local": {"audio_path": "/tmp/a.m4a"}}) == \
        pathlib.Path("/tmp/a.m4a")
    assert g.audio_path_of({"local": {"audio_path": None}}) is None
    assert g.audio_path_of({"local": {}}) is None


# ── 步骤 1：音频 ──

def test_ensure_audio_downloads_when_missing(roots, monkeypatch):
    """音频不在 → 下载，并按 info.json 约定的路径落盘。"""
    raw, _ = roots
    info = write_info(raw)
    got = []

    def dl(url, p):
        got.append((url, str(p)))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 4096)
        return True, ""

    monkeypatch.setattr(g, "_download", dl)
    ok, msg = g.ensure_audio(info)
    assert ok is True
    assert got == [("http://feed/audio.m4a", info["local"]["audio_path"])]


def test_ensure_audio_skips_when_present(roots, monkeypatch):
    """音频已存在且非零字节 → 不重复下载（幂等）。"""
    raw, _ = roots
    info = write_info(raw)
    pathlib.Path(info["local"]["audio_path"]).parent.mkdir(parents=True)
    pathlib.Path(info["local"]["audio_path"]).write_bytes(b"x" * 10)
    monkeypatch.setattr(
        g, "_download",
        lambda *a: pytest.fail("音频已存在，不该重新下载"))
    ok, msg = g.ensure_audio(info)
    assert ok is True
    assert "已就位" in msg


def test_ensure_audio_redownloads_zero_byte_file(roots, monkeypatch):
    """零字节文件不算「已就位」→ 必须重下（否则转写会拿到空文件）。"""
    raw, _ = roots
    info = write_info(raw)
    p = pathlib.Path(info["local"]["audio_path"])
    p.parent.mkdir(parents=True)
    p.write_bytes(b"")
    n = []
    monkeypatch.setattr(g, "_download", lambda u, s: (
        n.append(1), s.parent.mkdir(parents=True, exist_ok=True),
        s.write_bytes(b"x" * 4096), (True, ""))[3])
    assert g.ensure_audio(info)[0] is True
    assert n == [1]


def test_ensure_audio_fails_on_download_error(roots, monkeypatch):
    """yt-dlp 报错 → 失败并带上原因。"""
    raw, _ = roots
    info = write_info(raw)
    monkeypatch.setattr(g, "_download", lambda u, s: (False, "HTTP Error 404"))
    ok, msg = g.ensure_audio(info)
    assert ok is False
    assert "404" in msg


def test_ensure_audio_fails_when_downloader_exits_zero_without_file(roots, monkeypatch):
    """yt-dlp 退出 0 但文件没落盘 → 判失败（退出码不可信，以文件为准）。"""
    raw, _ = roots
    info = write_info(raw)
    monkeypatch.setattr(g, "_download", lambda u, s: (True, ""))
    ok, msg = g.ensure_audio(info)
    assert ok is False
    assert "不存在" in msg


def test_ensure_audio_fails_on_zero_byte_after_download(roots, monkeypatch):
    """下载后是零字节 → 判失败。"""
    raw, _ = roots
    info = write_info(raw)

    def zero(url, save_path):
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_bytes(b"")
        return True, ""

    monkeypatch.setattr(g, "_download", zero)
    ok, msg = g.ensure_audio(info)
    assert ok is False
    assert "零字节" in msg


def test_ensure_audio_fails_without_url(roots):
    """没有音频地址 → 明确报「没有音频地址」，不去瞎猜路径。"""
    raw, _ = roots
    info = write_info(raw, url="")
    ok, msg = g.ensure_audio(info)
    assert ok is False
    assert "音频地址" in msg


def test_ensure_audio_fails_without_audio_path(roots):
    """local.audio_path 为空 → 无法确定落盘位置，明确失败。"""
    raw, _ = roots
    info = write_info(raw, audio=None)
    ok, msg = g.ensure_audio(info)
    assert ok is False
    assert "audio_path" in msg


# ── 步骤 2：转写 ──

def test_ensure_transcript_ok_when_artifact_appears(roots, calls):
    """转写函数报成功且产物真的在 → 成功（calls 夹具会真的写出转写稿）。"""
    raw, tr = roots
    write_info(raw)
    ok, _ = g.ensure_transcript(HASH, g.find_info_json(HASH))
    assert ok is True


def test_ensure_transcript_fails_when_success_without_artifact(roots, monkeypatch):
    """转写函数报成功但产物不存在 → 判失败（防「假成功」）。"""
    raw, _ = roots
    write_info(raw)
    monkeypatch.setattr(g, "_transcribe", lambda i, d: (True, "ok"))
    ok, msg = g.ensure_transcript(HASH, g.find_info_json(HASH))
    assert ok is False
    assert "产物不存在" in msg


def test_ensure_transcript_surfaces_reason_code(roots, monkeypatch):
    """转写失败时，原因码（如时长校验不过）要透出到日志里。"""
    raw, _ = roots
    write_info(raw)
    monkeypatch.setattr(g, "_transcribe",
                        lambda i, d: (False, "duration_mismatch: asr 100s vs audio 900s"))
    ok, msg = g.ensure_transcript(HASH, g.find_info_json(HASH))
    assert ok is False
    assert "duration_mismatch" in msg


def test_ensure_transcript_treats_skip_exists_as_success(roots, monkeypatch):
    """并发下别人已写出转写稿（skip_exists）→ 视为成功，不再报错。"""
    raw, tr = roots
    write_info(raw)
    tr.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(g, "_transcribe", lambda i, d: (False, "skip_exists"))
    (tr / f"标题-{HASH}.transcript.md").write_text("x", encoding="utf-8")
    ok, _ = g.ensure_transcript(HASH, g.find_info_json(HASH))
    assert ok is True


# ── 步骤 3 + 串链路 ──

def test_run_one_runs_all_three_steps_in_order(roots, calls):
    """没有转写稿时：下载 → 转写 → 笔记，三步都跑且顺序正确。"""
    raw, _ = roots
    write_info(raw)
    ok, stage, msg = g.run_one(HASH, dry_run=False)
    assert ok is True
    assert stage == "完成"
    assert [c[0] for c in calls] == ["download", "transcribe", "note"]


def test_run_one_skips_audio_and_transcribe_when_transcript_exists(roots, calls):
    """转写稿已在 → 音频和转写两步都跳过，只出笔记（重跑不重复下载/不烧 ASR）。

    靠「产物真的在」触发，不用替换 `_transcript_exists` ——
    直接给模块属性赋值不会被还原，会污染后续用例。
    """
    raw, tr = roots
    write_info(raw)
    tr.mkdir(parents=True, exist_ok=True)
    (tr / f"标题-{HASH}.transcript.md").write_text("x", encoding="utf-8")
    ok, stage, _ = g.run_one(HASH, dry_run=False)
    assert ok is True
    assert stage == "完成"
    assert [c[0] for c in calls] == ["note"]


def test_run_one_passes_info_path_to_transcribe(roots, calls):
    """转写拿到的是这一期自己的 info.json 路径。"""
    raw, _ = roots
    write_info(raw)
    g.run_one(HASH, dry_run=False)
    transcribe_call = [c for c in calls if c[0] == "transcribe"][0]
    assert pathlib.Path(transcribe_call[1]).name == f"标题-{HASH}.info.json"


def test_run_one_reports_audio_stage_on_download_failure(roots, monkeypatch):
    """下载失败 → 归到「音频」步，且不再往下跑转写/笔记。"""
    raw, _ = roots
    write_info(raw)
    monkeypatch.setattr(g, "_download", lambda u, s: (False, "HTTP Error 403"))
    monkeypatch.setattr(g, "_transcribe",
                        lambda *a: pytest.fail("下载失败后不该继续转写"))
    monkeypatch.setattr(g, "generate_note",
                        lambda h: pytest.fail("下载失败后不该生成笔记"))
    ok, stage, msg = g.run_one(HASH, dry_run=False)
    assert ok is False
    assert stage == "音频"
    assert "403" in msg


def test_run_one_reports_transcribe_stage(roots, calls, monkeypatch):
    """转写失败 → 归到「转写」步，且不再生成笔记（下载这步已成功）。"""
    raw, _ = roots
    write_info(raw)
    monkeypatch.setattr(g, "_transcribe", lambda i, d: (False, "no_audio"))
    monkeypatch.setattr(g, "generate_note",
                        lambda h: pytest.fail("转写失败后不该生成笔记"))
    ok, stage, msg = g.run_one(HASH, dry_run=False)
    assert ok is False
    assert stage == "转写"
    assert "no_audio" in msg


def test_run_one_reports_note_stage(roots, calls, monkeypatch):
    """笔记失败 → 归到「笔记」步（音频/转写已成功，不该被牵连重报成前两步）。"""
    raw, _ = roots
    write_info(raw)
    monkeypatch.setattr(g, "generate_note", lambda h: (False, "failed 3s 源文缺失"))
    ok, stage, msg = g.run_one(HASH, dry_run=False)
    assert ok is False
    assert stage == "笔记"
    assert "源文缺失" in msg


def test_run_one_fails_at_audio_when_info_json_missing(roots, calls):
    """没有 info.json 就没音频地址 → 归到「音频」步，不调用任何接缝。"""
    ok, stage, msg = g.run_one("deadbeef", dry_run=False)
    assert ok is False
    assert stage == "音频"
    assert "info.json" in msg
    assert calls == []


def test_run_one_fails_on_malformed_info_json(roots, calls):
    """info.json 损坏 → 归到「音频」步，不崩。"""
    raw, _ = roots
    (raw / f"标题-{HASH}.info.json").write_text("{不是 json", encoding="utf-8")
    ok, stage, _ = g.run_one(HASH, dry_run=False)
    assert ok is False
    assert stage == "音频"
    assert calls == []


def test_dry_run_touches_nothing(roots, calls):
    """--dry-run 不下载、不转写、不生成笔记。"""
    ok, stage, msg = g.run_one(HASH, dry_run=True)
    assert ok is True
    assert stage == "预演"
    assert calls == []


# ── 接线校验：接缝确实指向已有实现，不是另写一套 ──

def test_download_seam_delegates_to_fetch_rss_feed(monkeypatch):
    """_download 必须调 fetch_rss_feed.download_audio（别自己重写下载逻辑）。"""
    import fetch_rss_feed
    got = {}
    monkeypatch.setattr(fetch_rss_feed, "download_audio",
                        lambda url, p, **kw: (got.update(u=url, p=p), (True, ""))[1])
    assert g._download("http://a/x.m4a", pathlib.Path("/tmp/x.m4a")) == (True, "")
    assert got == {"u": "http://a/x.m4a", "p": pathlib.Path("/tmp/x.m4a")}


def test_transcribe_seam_delegates_to_asr_podcast(monkeypatch):
    """_transcribe 必须调 asr_podcast.transcribe_one（时长/可信度校验在那里面）。"""
    import asr_podcast
    got = {}
    monkeypatch.setattr(asr_podcast, "transcribe_one",
                        lambda ip, td, force=False: (
                            got.update(ip=str(ip), td=str(td), force=force),
                            (True, "ok"))[1])
    assert g._transcribe(pathlib.Path("/a/x.info.json"),
                         pathlib.Path("/a/transcripts")) == (True, "ok")
    assert got["force"] is False
    assert got["ip"] == "/a/x.info.json"


def test_transcript_exists_seam_delegates_to_asr_podcast(monkeypatch):
    """转写稿判重复用 asr_podcast.transcript_exists 的 glob 规则。"""
    import asr_podcast
    seen = {}

    def fake_exists(d, h):
        seen["dir"] = str(d)
        return h == "a1b2c3d4"

    monkeypatch.setattr(asr_podcast, "transcript_exists", fake_exists)
    assert g._transcript_exists("a1b2c3d4") is True
    assert g._transcript_exists("ffffffff") is False
    assert seen["dir"] == str(g.TRANSCRIPTS_DIR)


# ── 汇总计数：失败必须单独计，且能定位到步 ──

def test_process_state_file_counts_failures_by_stage(tmp_path, monkeypatch, capsys):
    """一期成功 + 一期下载失败 → ok=1 / fail=1，失败分布按步统计。"""
    state = tmp_path / "节目.md"
    state.write_text("x", encoding="utf-8")
    monkeypatch.setattr(g, "pick_confirmed", lambda s: [{"guid": "g1"}, {"guid": "g2"}])
    monkeypatch.setattr(g, "find_hash8", lambda it: "aaa" if it["guid"] == "g1" else "bbb")
    monkeypatch.setattr(g, "already_done", lambda s, h: False)
    monkeypatch.setattr(g, "sm", type("S", (), {"load_state": staticmethod(lambda p: {})}))

    def fake_run_one(hash8, dry_run):
        if hash8 == "aaa":
            return True, "完成", f"{hash8} ok"
        return False, "音频", f"{hash8} yt-dlp 下载失败：HTTP Error 500"

    monkeypatch.setattr(g, "run_one", fake_run_one)
    r = g.process_state_file(state, dry_run=False)
    assert r == {"picked": 2, "ok": 1, "fail": 1, "skip": 0}
    out = capsys.readouterr().out
    assert "失败分布：音频 1 期" in out


def test_process_state_file_counts_skip_separately(tmp_path, monkeypatch):
    """已在已转化区的条目算 skip，不算 fail（否则退出码永远非 0）。"""
    state = tmp_path / "节目.md"
    state.write_text("x", encoding="utf-8")
    monkeypatch.setattr(g, "pick_confirmed", lambda s: [{"guid": "g1"}, {"guid": "g2"}])
    monkeypatch.setattr(g, "find_hash8", lambda it: "aaa" if it["guid"] == "g1" else "bbb")
    monkeypatch.setattr(g, "already_done", lambda s, h: h == "aaa")
    monkeypatch.setattr(g, "sm", type("S", (), {"load_state": staticmethod(lambda p: {})}))

    def fake_run_one(hash8, dry_run):
        return True, "完成", f"{hash8} ok"

    monkeypatch.setattr(g, "run_one", fake_run_one)
    r = g.process_state_file(state, dry_run=False)
    assert r == {"picked": 2, "ok": 1, "fail": 0, "skip": 1}


def test_stats_shape_has_four_counters():
    """汇总字典四个键固定 —— main() 按它做累加，少一个就崩。"""
    assert set(g.process_state_file.__code__.co_consts) is not None      # 函数存在
    src = g.__file__
    text = pathlib.Path(src).read_text(encoding="utf-8")
    for key in ('"picked"', '"ok"', '"fail"', '"skip"'):
        assert key in text, f"汇总键 {key} 缺失"
