"""სკანის წაშლა ატვირთვამდე და დაბრუნება (removal.py). მონაცემები გამოგონილია."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hrsorter import duplicates, keep, removal
from hrsorter import mhr_queue as q
from tests.test_keep import DOC, PN, _fresh_memory, _ui_item, add_scan, cfg, lose, mhr, repo  # noqa: F401  (fixtures)
from tests.test_mhr_queue import FakeValidate, uploads
from tests.test_packets import LOCAL
from tests.test_ui import client  # noqa: F401  (fixture)


def gone_from_lists(repo, doc_id: str) -> bool:
    doc, m = repo.get_document(doc_id), repo.mhr_get(doc_id)
    return (not doc["active"] and doc["status"] == "deleted" and (m is None or m["status"] == "deleted")
            and not repo.active_documents_for_source(doc["source_id"]))


# --- წაშლა ----------------------------------------------------------------------------------------

def test_delete_moves_the_file_aside_and_the_scan_is_never_uploaded(repo, cfg, mhr):
    _, out = add_scan(repo, cfg)
    whole = out.read_bytes()
    assert removal.delete(repo, cfg, "doc_1") is None
    assert gone_from_lists(repo, "doc_1")
    moved = removal.deleted_dir(cfg, "doc_1") / out.name
    doc = repo.get_document("doc_1")
    assert not out.exists() and moved.read_bytes() == whole and doc["output_path"] == str(moved)
    assert keep.document_file(cfg, doc) == moved                         # გვერდი და PDF ისევ იხსნება
    info = json.loads(doc["metadata"])["deleted"]
    assert info["status"] == "proposed" and info["mhr_status"] == "pending" and info["output_path"] == str(out)
    q.sweep(repo, cfg, mhr, FakeValidate())
    assert repo.mhr_get("doc_1")["status"] == "deleted" and not uploads(mhr)   # worker აღარ ეხება
    assert removal.delete(repo, cfg, "doc_1") == "not_found"                   # მეორედ — არაფერი


def test_restore_puts_everything_back(repo, cfg):
    _, out = add_scan(repo, cfg)
    repo.mhr_transition("doc_1", ("pending",), "failed", validation={"status": "failed"}, next_try=123.0)
    removal.delete(repo, cfg, "doc_1")
    assert removal.restore(repo, cfg, "doc_1") is None
    doc, m = repo.get_document("doc_1"), repo.mhr_get("doc_1")
    assert doc["active"] == 1 and doc["status"] == "proposed" and doc["output_path"] == str(out) and out.exists()
    assert m["status"] == "failed" and m["next_try"] == 123.0 and json.loads(m["validation"]) == {"status": "failed"}
    assert "deleted" not in json.loads(doc["metadata"] or "{}")
    assert removal.restore(repo, cfg, "doc_1") == "not_deleted"


@pytest.mark.parametrize("st", removal.DELETABLE_MHR)
def test_every_not_yet_uploaded_state_can_be_deleted_and_comes_back_the_same(repo, cfg, st):
    add_scan(repo, cfg)
    if st != "pending":
        repo.mhr_transition("doc_1", ("pending",), st)
    assert removal.delete(repo, cfg, "doc_1") is None and gone_from_lists(repo, "doc_1")
    assert removal.restore(repo, cfg, "doc_1") is None and repo.mhr_get("doc_1")["status"] == st


@pytest.mark.parametrize("st", ["approved", "uploading", "uploaded", "needs_check", "split"])
def test_upload_started_or_done_is_never_deleted(repo, cfg, st):
    _, out = add_scan(repo, cfg)
    repo.mhr_transition("doc_1", ("pending",), st)
    assert removal.delete(repo, cfg, "doc_1") == f"mhr_{st}"
    assert repo.get_document("doc_1")["active"] == 1 and repo.mhr_get("doc_1")["status"] == st and out.exists()


def test_split_scan_is_deleted_part_by_part(repo, cfg):
    add_scan(repo, cfg)
    repo.conn.execute("UPDATE documents SET status='split' WHERE id='doc_1'")
    assert removal.delete(repo, cfg, "doc_1") == "document_split"


def test_document_without_mhr_entry_can_be_deleted(repo, cfg):
    add_scan(repo, cfg, enqueue=False)
    assert removal.delete(repo, cfg, "doc_1") is None and gone_from_lists(repo, "doc_1")
    assert removal.restore(repo, cfg, "doc_1") is None and repo.mhr_get("doc_1") is None


def test_file_the_share_already_lost_is_read_from_the_copy(repo, cfg):
    _, out = add_scan(repo, cfg)
    whole = out.read_bytes()
    keep.sweep(repo, cfg)
    lose(out)
    assert removal.delete(repo, cfg, "doc_1") is None
    doc = repo.get_document("doc_1")
    assert Path(doc["output_path"]).parent == removal.deleted_dir(cfg, "doc_1")   # ძველ ადგილას ახალი ფაილი შეიძლება გაჩნდეს
    assert keep.document_file(cfg, doc).read_bytes() == whole
    assert removal.restore(repo, cfg, "doc_1") is None
    doc = repo.get_document("doc_1")
    assert doc["output_path"] == str(out) and keep.document_file(cfg, doc).read_bytes() == whole


def test_restore_never_overwrites_a_newer_file_in_the_old_place(repo, cfg):
    _, out = add_scan(repo, cfg)
    removal.delete(repo, cfg, "doc_1")
    out.write_bytes(b"another document")
    assert removal.restore(repo, cfg, "doc_1") is None
    doc = repo.get_document("doc_1")
    assert out.read_bytes() == b"another document" and doc["output_path"] == str(removal.deleted_dir(cfg, "doc_1") / out.name)


def test_locked_file_changes_nothing(repo, cfg, monkeypatch):
    _, out = add_scan(repo, cfg)
    repo.mhr_transition("doc_1", ("pending",), "ready")

    def locked(src, dst):
        raise PermissionError(13, "in use")
    monkeypatch.setattr(removal.os, "replace", locked)
    assert removal.delete(repo, cfg, "doc_1") == "file_locked"
    assert repo.get_document("doc_1")["active"] == 1 and repo.mhr_get("doc_1")["status"] == "ready" and out.exists()


def test_scan_reprocessed_after_delete_is_not_restored(repo, cfg):
    add_scan(repo, cfg)
    removal.delete(repo, cfg, "doc_1")
    sid = repo.get_document("doc_1")["source_id"]
    repo.upsert_document({"id": "doc_2", "job_id": "job_2", "source_id": sid, "page_start": 0, "page_end": 0, "status": "proposed"})
    assert removal.restore(repo, cfg, "doc_1") == "reprocessed" and repo.get_document("doc_1")["status"] == "deleted"


def test_rescan_of_a_deleted_paper_is_not_a_duplicate(repo, cfg):
    add_scan(repo, cfg)
    repo.conn.execute("UPDATE documents SET content_fingerprint='fp1' WHERE id='doc_1'")

    def check():
        return duplicates.check(repo, content_fp="fp1", text_fp="", shingles=[], type_id="contract", personal_number=PN,
                                doc_number=None, document_date=None, job_id="job_9", cfg=cfg).status
    assert check() == "exact"
    removal.delete(repo, cfg, "doc_1")
    assert check() == "none"


# --- ეკრანი ---------------------------------------------------------------------------------------

def test_delete_and_restore_from_the_pages(client):
    c, cfg_path = client
    cfg, repo, archive, out = _ui_item(cfg_path, "failed")
    page = c.get(f"/mhr/{DOC}").text
    assert f'action="/doc/{DOC}/delete"' in page and "ეს სკანი საერთოდ არ მჭირდება" in page and "დიახ, წაიშალოს" in page
    assert f'action="/doc/{DOC}/delete"' in c.get(f"/doc/{DOC}").text and DOC in c.get("/mhr?tab=failed").text

    r = c.post(f"/doc/{DOC}/delete", headers={"origin": LOCAL}, follow_redirects=False)   # ჩამრთველის გარეშე
    assert r.status_code == 303 and "err=" in r.headers["location"] and repo.get_document(DOC)["active"] == 1

    r = c.post(f"/doc/{DOC}/delete", data={"confirm": "1"}, headers={"origin": LOCAL}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/doc/{DOC}?msg=")
    assert gone_from_lists(repo, DOC) and DOC not in c.get("/mhr?tab=failed").text and DOC not in c.get("/mhr?tab=other").text
    assert DOC not in c.get("/documents").text
    page = c.get(r.headers["location"]).text
    assert "სკანი წაიშალა" in page and "სკანი წაშლილია" in page and f'action="/doc/{DOC}/restore"' in page
    assert f'action="/doc/{DOC}/delete"' not in page
    page = c.get(f"/mhr/{DOC}").text
    assert "სკანი წაშლილია" in page and f'action="/doc/{DOC}/restore"' in page and f'action="/doc/{DOC}/delete"' not in page
    assert "ატვირთვა მაინც" not in page and "/manual-upload" not in page

    r = c.post(f"/doc/{DOC}/restore", headers={"origin": LOCAL}, follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert repo.get_document(DOC)["active"] == 1 and DOC in c.get("/mhr?tab=failed").text and out.exists()
    repo.close()


def test_uploaded_scan_has_no_delete_button(client):
    c, cfg_path = client
    cfg, repo, archive, out = _ui_item(cfg_path, "uploaded")
    assert "/delete" not in c.get(f"/mhr/{DOC}").text and "/delete" not in c.get(f"/doc/{DOC}").text
    r = c.post(f"/doc/{DOC}/delete", data={"confirm": "1"}, headers={"origin": LOCAL}, follow_redirects=False)
    assert "err=" in r.headers["location"] and repo.mhr_get(DOC)["status"] == "uploaded"
    repo.close()


def test_gone_scan_offers_delete(client):
    c, cfg_path = client
    cfg, repo, archive, out = _ui_item(cfg_path, "upload_failed", "file unreadable (FileNotFoundError)")
    lose(archive, out)
    page = c.get(f"/mhr/{DOC}").text
    assert "სკანის ფაილი აღარ არსებობს" in page and f'action="/doc/{DOC}/delete"' in page and "/retry" not in page
    repo.close()


def test_home_shows_a_scan_whose_documents_were_all_deleted(client):
    c, cfg_path = client
    cfg, repo, archive, out = _ui_item(cfg_path, "failed")
    sid = repo.get_document(DOC)["source_id"]
    jid = repo.create_job(source_id=sid, profile_id="batch", mode="batch_pdf", config_version=cfg.version, config_snapshot=cfg.snapshot())
    repo.conn.execute("UPDATE jobs SET status='done' WHERE id=?", (jid,))
    repo.conn.execute("UPDATE documents SET job_id=? WHERE id=?", (jid, DOC))
    removal.delete(repo, cfg, DOC)
    recent = c.get("/").text.split("<h2>ბოლო სკანები</h2>")[1]
    assert "წაიშალა" in recent and f'href="/doc/{DOC}"' in recent
    repo.close()
