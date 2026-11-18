"""სკანის ასლი ამ კომპიუტერზე (`keep`): ძირითადი საქაღალდიდან გამქრალი ფაილი მაინც იტვირთება, ჩანს და თავიდან მუშავდება.

ნამდვილი შემთხვევა: ქსელური საცავი ღამით შლიდა დაახლოებით ერთი დღის წინანდელ ფაილებს; ატვირთვა „file unreadable
(FileNotFoundError)“-ით ჩერდებოდა და ქაღალდი თავიდან იყო დასასკანერებელი. აქ ყველა მონაცემი გამოგონილია.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import time

import pytest
from pypdf import PdfWriter

from hrsorter import housekeeping, keep
from hrsorter import mhr_queue as q
from hrsorter.config import ConfigError, load_config, make_config
from hrsorter.db import Repository
from hrsorter.mhr_client import MockMhr
from tests.test_mhr_queue import FakeValidate, status, uploads
from tests.test_packets import LOCAL
from tests.test_ui import client  # noqa: F401  (fixture)

DAY = 86400
PN = "01001000001"


@pytest.fixture(autouse=True)
def _fresh_memory():
    keep._gone.clear()
    yield
    keep._gone.clear()


@pytest.fixture
def cfg(tmp_path):
    return make_config({"paths": {"root": str(tmp_path)},
                        "mhr": {"enabled": True, "mode": "mock", "mock_dir": str(tmp_path / "mhr")}})


@pytest.fixture
def repo(cfg):
    cfg.paths.root.mkdir(parents=True, exist_ok=True)
    r = Repository(cfg.paths.db)
    yield r
    r.close()


@pytest.fixture
def mhr(tmp_path) -> MockMhr:
    d = tmp_path / "mhr"
    d.mkdir()
    (d / "employees.json").write_text(json.dumps([{"id": "E001", "personal_number": PN}]), encoding="utf-8")
    return MockMhr(d)


def pdf_bytes(width: int) -> bytes:
    w = PdfWriter()
    w.add_blank_page(width, 200)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def add_scan(repo, cfg, doc_id="doc_1", width=200, enqueue=True) -> tuple:
    """სკანის ორიგინალი არქივში და მისი შედეგის PDF, როგორც დამუშავების შემდეგ რჩება. აბრუნებს (ორიგინალი, შედეგი) ბილიკებს."""
    src_bytes, out_bytes = pdf_bytes(width), pdf_bytes(width + 1)
    sha = hashlib.sha256(src_bytes).hexdigest()
    archive = cfg.paths.archive / sha[:2] / f"{sha}.pdf"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(src_bytes)
    sid = repo.insert_source(sha256=sha, size=len(src_bytes), page_count=1, archive_path=str(archive),
                             first_path="in/scan.pdf", profile_id="batch")
    out = cfg.paths.proposed / f"{doc_id}.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(out_bytes)
    repo.upsert_document({"id": doc_id, "job_id": "job_1", "source_id": sid, "page_start": 0, "page_end": 0,
                          "status": "proposed", "type_id": "contract", "personal_number": PN,
                          "output_path": str(out), "output_sha256": hashlib.sha256(out_bytes).hexdigest()})
    if enqueue:
        q.enqueue(repo, doc_id, PN)
    return archive, out


def lose(*paths) -> None:
    """ის, რასაც ქსელური საცავი ღამით აკეთებს."""
    for p in paths:
        os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
        p.unlink()


# --- ასლის შენახვა ----------------------------------------------------------------------------

def test_new_scan_and_result_are_copied_once(repo, cfg):
    archive, out = add_scan(repo, cfg)
    assert keep.sweep(repo, cfg) == {"kept": 2, "expired": 0}
    assert keep.sweep(repo, cfg) == {"kept": 0, "expired": 0}
    doc, src = repo.get_document("doc_1"), repo.get_source(repo.get_document("doc_1")["source_id"])
    assert keep.local_path(cfg, doc["output_sha256"]).read_bytes() == out.read_bytes()
    assert keep.local_path(cfg, src["sha256"]).read_bytes() == archive.read_bytes()
    assert keep.document_file(cfg, doc) == out and keep.source_file(cfg, src) == archive   # სანამ ძირითადი არსებობს — ის იკითხება
    lose(archive, out)
    assert keep.document_file(cfg, doc) == keep.local_path(cfg, doc["output_sha256"])
    assert keep.source_file(cfg, src) == keep.local_path(cfg, src["sha256"])


def test_cleanup_sweep_makes_the_copies(repo, cfg):
    add_scan(repo, cfg)
    assert housekeeping.sweep(repo, cfg)["kept"] == 2
    assert len(list(keep.root(cfg).rglob("*.pdf"))) == 2


def test_half_written_file_is_not_kept(repo, cfg):
    _, out = add_scan(repo, cfg)
    whole = out.read_bytes()
    out.write_bytes(whole[:50])                       # შიგთავსი ჯერ ბაზის sha-ს არ ემთხვევა
    assert keep.sweep(repo, cfg)["kept"] == 1         # მხოლოდ ორიგინალი
    out.write_bytes(whole)
    assert keep.sweep(repo, cfg)["kept"] == 1 and not list(keep.root(cfg).rglob("*.tmp"))


def test_file_already_gone_is_looked_for_again_only_after_an_hour(repo, cfg):
    archive, out = add_scan(repo, cfg)
    whole = out.read_bytes()
    lose(out)
    now = time.time()
    assert keep.sweep(repo, cfg, now)["kept"] == 1
    out.write_bytes(whole)
    assert keep.sweep(repo, cfg, now + 60)["kept"] == 0
    assert keep.sweep(repo, cfg, now + keep.RECHECK_GONE_SECONDS + 1)["kept"] == 1


def test_copies_expire_and_old_scans_are_not_copied_again(repo, cfg):
    add_scan(repo, cfg)
    keep.sweep(repo, cfg)
    later = time.time() + 62 * DAY                     # ნაგულისხმევი ვადა 60 დღეა
    assert keep.sweep(repo, cfg, later) == {"kept": 0, "expired": 2}
    assert keep.sweep(repo, cfg, later) == {"kept": 0, "expired": 0} and not list(keep.root(cfg).rglob("*.pdf"))


def test_zero_days_makes_no_copies_but_old_ones_still_work(repo, cfg):
    _, out = add_scan(repo, cfg)
    keep.sweep(repo, cfg)
    cfg.data["cleanup"]["local_copy_days"] = 0
    add_scan(repo, cfg, "doc_2", width=300)
    assert keep.sweep(repo, cfg, time.time() + 400 * DAY) == {"kept": 0, "expired": 0}
    lose(out)
    assert keep.document_file(cfg, repo.get_document("doc_1")) is not None
    assert keep.local_path(cfg, repo.get_document("doc_2")["output_sha256"]).exists() is False


def test_unreachable_main_folder_is_skipped(tmp_path):
    cfg = make_config({"paths": {"root": str(tmp_path / "share"), "state": str(tmp_path / "state")}})
    repo = Repository(cfg.paths.db)
    try:
        assert keep.sweep(repo, cfg) == {"kept": 0, "expired": 0}
    finally:
        repo.close()


def test_negative_days_are_refused(tmp_path):
    with pytest.raises(ConfigError):
        make_config({"paths": {"root": str(tmp_path)}, "cleanup": {"local_copy_days": -1}})


def test_sha_must_look_like_a_sha(cfg):
    assert keep.local_path(cfg, "../../evil") is None and keep.local_path(cfg, None) is None
    assert keep.find(cfg, None, "zz") is None


# --- ატვირთვა ---------------------------------------------------------------------------------

def test_upload_works_after_the_share_lost_the_file(repo, cfg, mhr):
    _, out = add_scan(repo, cfg)
    whole = out.read_bytes()
    q.sweep(repo, cfg, mhr, FakeValidate())
    housekeeping.sweep(repo, cfg)
    lose(out)
    assert q.approve(repo, "doc_1")
    assert q.sweep(repo, cfg, mhr, FakeValidate())["uploaded"] == 1 and status(repo) == "uploaded"
    up = uploads(mhr)
    assert len(up) == 1 and up[0]["filename"] == "doc_1.pdf" and (mhr.dir / up[0]["path"]).read_bytes() == whole


def test_upload_without_any_copy_says_the_scan_is_gone(repo, cfg, mhr):
    _, out = add_scan(repo, cfg)
    q.sweep(repo, cfg, mhr, FakeValidate())
    lose(out)
    q.approve(repo, "doc_1")
    assert q.sweep(repo, cfg, mhr, FakeValidate())["upload_failed"] == 1
    row = repo.mhr_get("doc_1")
    assert row["status"] == "upload_failed" and row["error"] == q.FILE_GONE and not uploads(mhr)


# --- თავიდან დამუშავება და გადარჩენა -------------------------------------------------------------

def test_reprocess_gets_the_original_back_from_the_copy(repo, cfg):
    archive, _ = add_scan(repo, cfg)
    src = repo.get_source(repo.get_document("doc_1")["source_id"])
    whole = archive.read_bytes()
    assert keep.restore_source(cfg, src) == archive                      # არქივშია — არაფერი იცვლება
    lose(archive)
    assert keep.restore_source(cfg, src) is None                         # ასლიც არ არის
    keep.store(cfg, _write(cfg.paths.root / "again.pdf", whole), src["sha256"])
    assert keep.restore_source(cfg, src) == archive and archive.read_bytes() == whole
    assert not os.access(archive, os.W_OK)                               # არქივის ფაილი ისევ read-only-ა


def _write(path, data: bytes):
    path.write_bytes(data)
    return path


def test_import_folder_recovers_only_what_the_database_knows(repo, cfg, tmp_path):
    archive, out = add_scan(repo, cfg)
    backup = tmp_path / "backup" / "deep"
    backup.mkdir(parents=True)
    _write(backup / "renamed.pdf", out.read_bytes())
    _write(backup / "stranger.pdf", pdf_bytes(555))
    lose(out)
    doc = repo.get_document("doc_1")
    assert keep.document_file(cfg, doc) is None
    assert keep.import_folder(repo, cfg, tmp_path / "backup") == {"seen": 2, "kept": 1}
    assert keep.document_file(cfg, doc) == keep.local_path(cfg, doc["output_sha256"])
    assert keep.import_folder(repo, cfg, tmp_path / "backup") == {"seen": 2, "kept": 0}
    assert len(list(keep.root(cfg).rglob("*.pdf"))) == 1


def test_purged_upload_leaves_no_copy(repo, cfg):
    add_scan(repo, cfg)
    cfg.data["cleanup"]["uploaded_days"] = 1
    keep.sweep(repo, cfg)
    repo.mhr_transition("doc_1", ("pending",), "uploaded", uploaded_at="2020-01-01T00:00:00+00:00")
    out = housekeeping.sweep(repo, cfg)
    assert out["purged_documents"] == 1 and out["purged_sources"] == 1 and out["kept"] == 0
    assert not list(keep.root(cfg).rglob("*.pdf"))


# --- ეკრანი -----------------------------------------------------------------------------------

DOC = "doc_" + "a" * 32
EMP = {"employee_id": "E1", "personal_number": PN, "first_name": "ნინო", "last_name": "ბერიძე"}


def _ui_item(cfg_path, mhr_status: str, error: str | None = None):
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data["mhr"] = {"enabled": True}
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    cfg = load_config(cfg_path)
    cfg.paths.root.mkdir(parents=True, exist_ok=True)
    repo = Repository(cfg.paths.db)
    archive, out = add_scan(repo, cfg, DOC)
    repo.mhr_transition(DOC, ("pending",), mhr_status, validation={"status": "failed", "employee": EMP}, employee_id="E1", error=error)
    return cfg, repo, archive, out


def test_gone_scan_page_says_rescan_and_offers_no_upload(client):
    c, cfg_path = client
    cfg, repo, archive, out = _ui_item(cfg_path, "upload_failed", "file unreadable (FileNotFoundError)")
    page = c.get(f"/mhr/{DOC}").text
    assert "ხელახლა ატვირთვა" in page and "ახლა ფაილი ადგილზეა" in page       # ფაილი ადგილზეა — ატვირთვა ისევ შეიძლება
    assert "file unreadable" not in page and "სკანის ფაილი აღარ არსებობს" not in page
    lose(archive, out)
    page = c.get(f"/mhr/{DOC}").text
    assert "სკანის ფაილი აღარ არსებობს" in page and "თავიდან დაასკანერე" in page
    assert "file unreadable" not in page and "ხელახლა ატვირთვა" not in page
    assert f"/mhr/{DOC}/retry" not in page and "/manual-upload" not in page   # ატვირთვის არცერთი ღილაკი
    assert f'action="/mhr/{DOC}/reject"' in page and "სიიდან ამოღება" in page
    assert "სკანის ფაილი აღარ არსებობს" in c.get(f"/doc/{DOC}").text and "file unreadable" not in c.get("/mhr?tab=other").text
    assert c.get(f"/file/output/{DOC}").status_code == 404 and c.get(f"/page/{DOC}/0.png").status_code == 404
    r = c.post(f"/mhr/{DOC}/reject", headers={"origin": LOCAL}, follow_redirects=False)
    assert r.status_code == 303 and repo.mhr_get(DOC)["status"] == "rejected"
    repo.close()


def test_stopped_file_without_scan_has_no_upload_anyway_button(client):
    c, cfg_path = client
    cfg, repo, archive, out = _ui_item(cfg_path, "failed")
    assert "ატვირთვა მაინც" in c.get(f"/mhr/{DOC}").text
    lose(out)
    page = c.get(f"/mhr/{DOC}").text
    assert "ატვირთვა მაინც" not in page and "სიიდან ამოღება" in page
    repo.close()


def test_pdf_and_page_picture_come_from_the_copy(client):
    c, cfg_path = client
    cfg, repo, archive, out = _ui_item(cfg_path, "failed")
    whole = out.read_bytes()
    keep.sweep(repo, cfg)
    lose(archive, out)
    page = c.get(f"/mhr/{DOC}").text
    assert "სკანის ფაილი აღარ არსებობს" not in page and "ატვირთვა მაინც" in page
    r = c.get(f"/file/output/{DOC}")
    assert r.status_code == 200 and r.content == whole
    assert c.get(f"/file/source/{DOC}").status_code == 200
    img = c.get(f"/page/{DOC}/0.png")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    assert f'src="/file/output/{DOC}"' in c.get(f"/doc/{DOC}").text
    repo.close()


def test_settings_show_the_copy_period(client):
    c, _ = client
    assert 'name="cleanup.local_copy_days"' in c.get("/settings").text
