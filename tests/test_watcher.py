from __future__ import annotations

import io
import os
import shutil
import time
from pathlib import Path

import pytest
from pypdf import PdfWriter

from hrsorter import archive
from hrsorter.archive import archive_source
from hrsorter.config import make_config
from hrsorter.db import Repository
from hrsorter.watcher import InboxScanner, InboxWatcher

GEORGIAN_FONT = Path("C:/Windows/Fonts/sylfaen.ttf")


def make_pdf(path: Path, title: str = "t", pages: int = 1, encrypt: bool = False) -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(200, 200)
    w.add_metadata({"/Title": title})
    if encrypt:
        w.encrypt("pw")
    buf = io.BytesIO()
    w.write(buf)
    path.write_bytes(buf.getvalue())
    return buf.getvalue()


def bump_mtime(path: Path, delta_ns: int = 5_000_000_000) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + delta_ns))


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def env(tmp_path):
    cfg = make_config({"paths": {"root": str(tmp_path)}})
    for p in cfg.profiles:
        p.inbox.mkdir(parents=True)
    repo = Repository(cfg.paths.db)
    clock = Clock()
    yield cfg, repo, clock, InboxScanner(cfg, repo, clock=clock)
    repo.close()


def inbox(cfg, profile: str = "batch") -> Path:
    return cfg.profile(profile).inbox


def actions(events) -> dict[str, str]:
    return {Path(e["path"]).name: e["action"] for e in events}


def count(repo, table: str) -> int:
    return repo.one(f"SELECT COUNT(*) AS n FROM {table}")["n"]


def test_waits_for_unchanged_size_and_mtime(env):
    cfg, repo, clock, sc = env
    make_pdf(inbox(cfg) / "a.pdf")
    assert actions(sc.scan_once()) == {"a.pdf": "waiting"}
    clock.t += 9.9
    assert actions(sc.scan_once()) == {"a.pdf": "waiting"}
    assert count(repo, "sources") == 0
    clock.t += 0.1
    ev = sc.scan_once()
    assert actions(ev) == {"a.pdf": "ingested"}
    job = repo.get_job(ev[0]["job_id"])
    assert job["status"] == "queued" and job["mode"] == "batch_pdf" and job["profile_id"] == "batch"
    assert job["config_version"] == cfg.version and job["config_snapshot"] == cfg.snapshot()
    src = repo.get_source(job["source_id"])
    assert Path(src["archive_path"]).read_bytes() == (inbox(cfg) / "a.pdf").read_bytes()
    assert src["page_count"] == 1
    assert (inbox(cfg) / "a.pdf").exists()   # ინბოქსის ფაილი არ იშლება


def test_growing_file_resets_stability(env):
    cfg, repo, clock, sc = env
    p = inbox(cfg) / "a.pdf"
    make_pdf(p)
    sc.scan_once()
    clock.t += 8
    with open(p, "ab") as f:
        f.write(b"\n")
    bump_mtime(p)
    assert actions(sc.scan_once()) == {"a.pdf": "waiting"}
    clock.t += 8     # 16 წმ პირველი ნახვიდან, მაგრამ 8 — ბოლო ცვლილებიდან
    assert actions(sc.scan_once()) == {"a.pdf": "waiting"}
    assert count(repo, "inbox_files") == 1  # ძველი pending ჩანაწერი ჩანაცვლდა
    clock.t += 2
    assert actions(sc.scan_once()) == {"a.pdf": "ingested"}


def test_locked_file_stays_pending_then_ingests(env):
    cfg, repo, clock, sc = env
    p = inbox(cfg) / "a.pdf"
    make_pdf(p)
    sc.scan_once()
    clock.t += 10
    with open(p, "ab"):   # მწერალს ფაილი ჯერ კიდევ უჭირავს
        ev = sc.scan_once()
        assert ev[0]["action"] == "waiting" and ev[0]["error_code"] == "file_locked"
        clock.t += 60
        assert sc.scan_once()[0]["action"] == "waiting"
    assert count(repo, "sources") == 0
    assert actions(sc.scan_once()) == {"a.pdf": "ingested"}


def test_locked_file_gives_up_after_max_wait(env):
    cfg, repo, clock, sc = env
    p = inbox(cfg) / "a.pdf"
    make_pdf(p)
    sc.scan_once()
    with open(p, "ab"):
        clock.t += 120 * 60
        ev = sc.scan_once()
    assert ev[0]["action"] == "error" and ev[0]["error_code"] == "file_locked"
    row = repo.one("SELECT * FROM inbox_files")
    assert row["state"] == "error" and row["error_code"] == "file_locked"
    assert sc.scan_once() == []


def test_incomplete_pdf_waits_then_ingests_after_completion(env):
    cfg, repo, clock, sc = env
    p = inbox(cfg) / "a.pdf"
    full = make_pdf(p, pages=2)
    p.write_bytes(full[: len(full) // 2])     # %%EOF ჯერ არ არის
    sc.scan_once()
    clock.t += 10
    ev = sc.scan_once()
    assert ev[0]["action"] == "waiting" and ev[0]["error_code"] == "pdf_corrupt"
    assert count(repo, "sources") == 0
    p.write_bytes(full)
    bump_mtime(p)
    assert actions(sc.scan_once()) == {"a.pdf": "waiting"}
    clock.t += 10
    ev = sc.scan_once()
    assert actions(ev) == {"a.pdf": "ingested"}
    assert repo.get_source(ev[0]["source_id"])["page_count"] == 2


def test_incomplete_pdf_gives_up_after_max_wait(env):
    cfg, repo, clock, sc = env
    p = inbox(cfg) / "a.pdf"
    full = make_pdf(p)
    p.write_bytes(full[:100])
    sc.scan_once()
    clock.t += 120 * 60
    ev = sc.scan_once()
    assert ev[0]["action"] == "error" and ev[0]["error_code"] == "pdf_corrupt"


@pytest.mark.parametrize("kind, code", [("corrupt", "pdf_corrupt"), ("encrypted", "pdf_encrypted")])
def test_invalid_pdf_goes_to_error(env, kind, code):
    cfg, repo, clock, sc = env
    p = inbox(cfg) / "bad.pdf"
    if kind == "corrupt":
        p.write_bytes(b"%PDF-1.4\n" + b"garbage " * 50 + b"\n%%EOF\n")
    else:
        make_pdf(p, encrypt=True)
    sc.scan_once()
    clock.t += 10
    ev = sc.scan_once()
    assert ev == [{"path": str(p), "action": "error", "error_code": code}]
    row = repo.one("SELECT * FROM inbox_files")
    assert row["state"] == "error" and row["error_code"] == code
    assert count(repo, "sources") == 0 and count(repo, "jobs") == 0
    msgs = [r["message"] for r in repo.q("SELECT message FROM events WHERE kind='ingest_error'")]
    assert msgs == [f"bad.pdf: {code}"]
    assert sc.scan_once() == [] and p.exists()


def test_duplicate_content_under_other_name_creates_no_job(env):
    cfg, repo, clock, sc = env
    a = inbox(cfg) / "a.pdf"
    make_pdf(a)
    sc.scan_once(); clock.t += 10; sc.scan_once()
    shutil.copy(a, inbox(cfg, "single") / "copy of a.pdf")
    sc.scan_once(); clock.t += 10
    ev = sc.scan_once()
    assert actions(ev) == {"copy of a.pdf": "duplicate"}
    assert ev[0]["job_id"] is None
    assert count(repo, "sources") == 1 and count(repo, "jobs") == 1
    assert sc.scan_once() == []   # განმეორებითი სკანი არაფერს ქმნის
    assert count(repo, "jobs") == 1


def test_same_name_changed_content_is_new_source(env):
    cfg, repo, clock, sc = env
    p = inbox(cfg) / "scan.pdf"
    make_pdf(p, title="first")
    sc.scan_once(); clock.t += 10
    first = sc.scan_once()[0]
    make_pdf(p, title="second")
    bump_mtime(p)
    sc.scan_once(); clock.t += 10
    second = sc.scan_once()[0]
    assert second["action"] == "ingested" and second["sha256"] != first["sha256"]
    assert count(repo, "sources") == 2 and count(repo, "jobs") == 2
    states = sorted(r["state"] for r in repo.q("SELECT state FROM inbox_files"))
    assert states == ["ingested", "ingested"]


def test_internal_dirs_and_temp_files_ignored(tmp_path):
    cfg = make_config({"paths": {"root": str(tmp_path)}, "profiles": [
        {"id": "root", "inbox": ".", "mode": "batch_pdf"},          # ინბოქსი = root
        {"id": "work", "inbox": "_Work", "mode": "batch_pdf"},      # შიდა საქაღალდე
    ]})
    for d in (tmp_path / "_Work", cfg.paths.archive / "ab"):
        d.mkdir(parents=True)
    make_pdf(tmp_path / "_Work" / "inside_work.pdf")
    make_pdf(cfg.paths.archive / "ab" / "archived.pdf")
    for name in ("~$lock.pdf", ".hidden.pdf", "x.pdf.part", "x.tmp", "notes.txt"):
        make_pdf(tmp_path / name)
    hidden = tmp_path / "attr_hidden.pdf"
    make_pdf(hidden)
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(str(hidden), 0x2)
    make_pdf(tmp_path / "UPPER.PDF")
    repo = Repository(cfg.paths.db)
    try:
        clock = Clock()
        sc = InboxScanner(cfg, repo, clock=clock)
        sc.scan_once(); clock.t += 10
        ev = sc.scan_once()
        expected = {"UPPER.PDF": "ingested"}
        if os.name != "nt":
            expected["attr_hidden.pdf"] = "ingested"
        assert actions(ev) == expected
        assert {Path(r["path"]).name for r in repo.q("SELECT path FROM inbox_files")} == set(expected)
    finally:
        repo.close()


def test_source_changed_during_copy_waits_again(env, monkeypatch):
    cfg, repo, clock, sc = env
    p = inbox(cfg) / "a.pdf"
    make_pdf(p)
    real_copy = archive._copy_hashing

    def copy_then_modify(s, d):
        out = real_copy(s, d)
        with open(s, "ab") as f:
            f.write(b"\n")
        return out

    sc.scan_once(); clock.t += 10
    monkeypatch.setattr(archive, "_copy_hashing", copy_then_modify)
    ev = sc.scan_once()
    assert ev[0]["action"] == "waiting" and ev[0]["error_code"] == "source_changed_during_copy"
    assert count(repo, "sources") == 0
    monkeypatch.setattr(archive, "_copy_hashing", real_copy)
    bump_mtime(p)
    assert actions(sc.scan_once()) == {"a.pdf": "waiting"}
    clock.t += 10
    assert actions(sc.scan_once()) == {"a.pdf": "ingested"}


def test_restart_picks_up_pending_and_half_done_ingest(env):
    cfg, repo, clock, sc = env
    a, b = inbox(cfg) / "a.pdf", inbox(cfg) / "b.pdf"
    make_pdf(a, title="a")
    make_pdf(b, title="b")
    sc.scan_once()
    # ავარია: b უკვე არქივშია, მაგრამ sources/jobs ჩანაწერი არ შექმნილა
    archive_source(b, cfg.paths.archive, cfg.paths.work)
    repo2 = Repository(cfg.paths.db)
    try:
        clock.t += 10
        ev = InboxScanner(cfg, repo2, clock=clock).scan_once()
        assert actions(ev) == {"a.pdf": "ingested", "b.pdf": "ingested"}
        assert count(repo2, "sources") == 2 and count(repo2, "jobs") == 2
    finally:
        repo2.close()


@pytest.mark.skipif(not GEORGIAN_FONT.exists(), reason="sylfaen.ttf missing")
def test_georgian_text_pdf_ingests(env):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    cfg, repo, clock, sc = env
    pdfmetrics.registerFont(TTFont("Sylfaen", str(GEORGIAN_FONT)))
    p = inbox(cfg, "single") / "ბრძანება.pdf"
    c = canvas.Canvas(str(p))
    c.setFont("Sylfaen", 14)
    c.drawString(72, 750, "ბრძანება შვებულების შესახებ")
    c.showPage()
    c.save()
    sc.scan_once(); clock.t += 10
    ev = sc.scan_once()
    assert actions(ev) == {"ბრძანება.pdf": "ingested"}
    assert repo.get_job(ev[0]["job_id"])["mode"] == "single_document_pdf"


def test_inbox_watcher_thread_ingests(tmp_path):
    cfg = make_config({"paths": {"root": str(tmp_path)},
                       "watcher": {"stability_seconds": 0.3, "recheck_interval_seconds": 0.1,
                                   "poll_interval_seconds": 0.2}})
    w = InboxWatcher(cfg, cfg.paths.db)
    w.start()
    try:
        time.sleep(0.3)
        make_pdf(inbox(cfg) / "a.pdf")
        repo = Repository(cfg.paths.db)
        try:
            deadline = time.time() + 15
            while time.time() < deadline and count(repo, "jobs") == 0:
                time.sleep(0.1)
            assert count(repo, "jobs") == 1
        finally:
            repo.close()
    finally:
        w.stop()


def test_auto_mode_picks_single_for_one_page_and_batch_for_many(tmp_path):
    """„auto“ რეჟიმი: სკანერის ერთი საქაღალდე — 1 გვერდი → single_document_pdf, მეტი → batch_pdf."""
    scan_dir = tmp_path / "scanner"
    scan_dir.mkdir()
    cfg = make_config({"paths": {"root": str(tmp_path / "root")},
                       "profiles": [{"id": "scanner", "inbox": str(scan_dir), "mode": "auto"}]})
    repo = Repository(cfg.paths.db)
    clock = Clock()
    sc = InboxScanner(cfg, repo, clock=clock)
    make_pdf(scan_dir / "one.pdf", title="one", pages=1)
    make_pdf(scan_dir / "many.pdf", title="many", pages=3)
    sc.scan_once()
    clock.t += 10
    ev = {Path(e["path"]).name: e for e in sc.scan_once()}
    assert ev["one.pdf"]["action"] == "ingested" and ev["many.pdf"]["action"] == "ingested"
    assert repo.get_job(ev["one.pdf"]["job_id"])["mode"] == "single_document_pdf"
    assert repo.get_job(ev["many.pdf"]["job_id"])["mode"] == "batch_pdf"
    repo.close()
