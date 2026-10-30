"""ინბოქსიდან გადატანა (Processed / _Failed) და ვადაგასულის წაშლა — რეალური ფაილებით tmp საქაღალდეში."""
from __future__ import annotations

import os
import stat
import time
from datetime import datetime, timezone

import pytest
from pypdf import PdfWriter

from hrsorter import housekeeping
from hrsorter.config import ConfigError, make_config
from hrsorter.db import Repository
from hrsorter.watcher import InboxScanner

DAY = 86400


def _cfg(tmp_path, **cleanup):
    return make_config({"paths": {"root": str(tmp_path)}, "watcher": {"stability_seconds": 0},
                        "cleanup": {"move_processed": True, "processed_days": 0, "uploaded_days": 0, **cleanup}})


@pytest.fixture
def env(tmp_path):
    cfg = _cfg(tmp_path)
    repo = Repository(cfg.paths.db)
    yield cfg, repo
    repo.close()


def drop_pdf(cfg, name="scan.pdf", width=100):
    inbox = cfg.profile("batch").inbox
    inbox.mkdir(parents=True, exist_ok=True)
    w = PdfWriter()
    w.add_blank_page(width, 100)
    with open(inbox / name, "wb") as f:
        w.write(f)
    return inbox / name


def ingest(cfg, repo):
    sc = InboxScanner(cfg, repo, clock=lambda: 1e9)
    sc.scan_once()
    return sc.scan_once()


def finish(repo, status):
    job = repo.claim_next_job("t", 60)
    repo.finish_job(job["id"], status)
    return job


def test_done_file_moves_to_processed_once(env):
    cfg, repo = env
    src = drop_pdf(cfg)
    ingest(cfg, repo)
    assert housekeeping.sweep(repo, cfg)["moved"] == 0          # რიგშია — ადგილზე რჩება
    finish(repo, "done")
    assert housekeeping.sweep(repo, cfg)["moved"] == 1
    assert not src.exists() and (cfg.paths.processed / "Batch" / "scan.pdf").exists()
    assert housekeeping.sweep(repo, cfg)["moved"] == 0
    assert len(list(cfg.paths.archive.rglob("*.pdf"))) == 1     # ორიგინალის ასლი არქივშია


def test_failed_goes_to_failed_then_back_after_successful_reprocess(env):
    cfg, repo = env
    drop_pdf(cfg)
    ingest(cfg, repo)
    job = finish(repo, "failed")
    housekeeping.sweep(repo, cfg)
    failed = cfg.paths.failed / "Batch" / "scan.pdf"
    assert failed.exists()
    repo.create_job(source_id=job["source_id"], profile_id="batch", mode="batch_pdf", config_version="v",
                    config_snapshot="{}", reprocess_of=job["id"])
    finish(repo, "done")
    housekeeping.sweep(repo, cfg)
    assert not failed.exists() and (cfg.paths.processed / "Batch" / "scan.pdf").exists()


def test_invalid_pdf_goes_to_failed(env):
    cfg, repo = env
    inbox = cfg.profile("batch").inbox
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "broken.pdf").write_bytes(b"not a pdf")
    ingest(cfg, repo)
    housekeeping.sweep(repo, cfg)
    assert (cfg.paths.failed / "Batch" / "broken.pdf").exists()


def test_name_collision_and_replaced_file_with_same_name(env):
    cfg, repo = env
    (cfg.paths.processed / "Batch").mkdir(parents=True)
    (cfg.paths.processed / "Batch" / "scan.pdf").write_bytes(b"older")
    src = drop_pdf(cfg)
    ingest(cfg, repo)
    finish(repo, "done")
    housekeeping.sweep(repo, cfg)
    assert (cfg.paths.processed / "Batch" / "scan (2).pdf").exists()

    drop_pdf(cfg, width=200)          # სკანერმა იგივე სახელით ახალი ფაილი დადო
    ingest(cfg, repo)
    finish(repo, "done")
    os.utime(src, ns=(1, 1))          # …და ის ჯერ კიდევ შეიცვალა — ჩანაწერი აღარ ემთხვევა
    housekeeping.sweep(repo, cfg)
    assert src.exists()               # შეუსაბამო ფაილს არ ვეხებით


def test_move_disabled(tmp_path):
    cfg = _cfg(tmp_path, move_processed=False)
    repo = Repository(cfg.paths.db)
    src = drop_pdf(cfg)
    ingest(cfg, repo)
    finish(repo, "done")
    assert housekeeping.sweep(repo, cfg)["moved"] == 0 and src.exists()
    repo.close()


def test_processed_deleted_after_days_but_failed_kept(tmp_path):
    cfg = _cfg(tmp_path, processed_days=30)
    repo = Repository(cfg.paths.db)
    drop_pdf(cfg, "ok.pdf")
    drop_pdf(cfg, "bad.pdf", width=300)
    ingest(cfg, repo)
    finish(repo, "done")
    finish(repo, "failed")
    now = time.time()
    housekeeping.sweep(repo, cfg, now=now)
    assert housekeeping.sweep(repo, cfg, now=now + 29 * DAY)["deleted"] == 0
    assert housekeeping.sweep(repo, cfg, now=now + 31 * DAY)["deleted"] == 1
    assert list((cfg.paths.processed / "Batch").iterdir()) == []
    assert len(list((cfg.paths.failed / "Batch").iterdir())) == 1
    repo.close()


def _doc(repo, cfg, job, n, status="proposed"):
    out = cfg.paths.proposed / f"d{n}.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"pdf")
    out.with_suffix(".json").write_text("{}")
    repo.upsert_document({"id": f"doc{n}", "job_id": job["id"], "source_id": job["source_id"], "page_start": n,
                          "page_end": n, "status": status, "output_path": str(out),
                          "sidecar_path": str(out.with_suffix(".json"))})
    return out


def _uploaded(repo, doc_id, ts):
    repo.mhr_enqueue(doc_id, "01001010101")
    iso = datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")
    assert repo.mhr_transition(doc_id, ("pending",), "uploaded", uploaded_at=iso)


def test_uploaded_purge_removes_outputs_then_archive_and_processed_copy(tmp_path):
    cfg = _cfg(tmp_path, uploaded_days=7)
    repo = Repository(cfg.paths.db)
    drop_pdf(cfg)
    ingest(cfg, repo)
    job = finish(repo, "done")
    out1, out2 = _doc(repo, cfg, job, 1), _doc(repo, cfg, job, 2, status="needs_review")
    now = time.time()
    _uploaded(repo, "doc1", now - 10 * DAY)
    archive = repo.get_source(job["source_id"])["archive_path"]
    assert not os.stat(archive).st_mode & stat.S_IWRITE          # არქივი read-only-ა

    r = housekeeping.sweep(repo, cfg, now=now)
    assert r["purged_documents"] == 1 and r["purged_sources"] == 0
    assert not out1.exists() and not out1.with_suffix(".json").exists() and out2.exists()
    assert os.path.exists(archive)                               # doc2 ჯერ არ არის ატვირთული

    _uploaded(repo, "doc2", now - 8 * DAY)
    r = housekeeping.sweep(repo, cfg, now=now)
    assert r["purged_sources"] == 1 and not os.path.exists(archive)
    assert list((cfg.paths.processed / "Batch").iterdir()) == []  # ინბოქსის ასლიც
    assert repo.get_source(job["source_id"])["purged_at"]
    repo.close()


def test_recent_upload_not_purged(tmp_path):
    cfg = _cfg(tmp_path, uploaded_days=7)
    repo = Repository(cfg.paths.db)
    drop_pdf(cfg)
    ingest(cfg, repo)
    job = finish(repo, "done")
    out = _doc(repo, cfg, job, 1)
    _uploaded(repo, "doc1", time.time() - 2 * DAY)
    assert housekeeping.sweep(repo, cfg)["purged_documents"] == 0 and out.exists()
    repo.close()


def test_config_rejects_negative_days_and_inbox_inside_processed(tmp_path):
    with pytest.raises(ConfigError):
        _cfg(tmp_path, processed_days=-1)
    with pytest.raises(ConfigError):
        make_config({"paths": {"root": str(tmp_path), "processed": "Inbox"}})
