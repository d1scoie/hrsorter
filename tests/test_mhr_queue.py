from __future__ import annotations

import hashlib
import io
import json
import time

import pytest
from pypdf import PdfWriter

from hrsorter import mhr_queue as q
from hrsorter.config import make_config
from hrsorter.db import Repository
from hrsorter.mhr_client import MhrUploadUncertain, MockMhr
from hrsorter.mhr_models import MhrDocRef, MhrEmployee, MhrError, MhrUnavailable, ValidationResult


@pytest.fixture
def cfg(tmp_path):
    return make_config({"paths": {"root": str(tmp_path)},
                        "mhr": {"enabled": True, "mode": "mock", "mock_dir": str(tmp_path / "mhr")}})


@pytest.fixture
def repo(cfg):
    r = Repository(cfg.paths.db)
    yield r
    r.close()


@pytest.fixture
def mhr(tmp_path) -> MockMhr:
    d = tmp_path / "mhr"
    d.mkdir()
    (d / "employees.json").write_text(json.dumps([{"id": "E001", "personal_number": "01001000001"}]),
                                      encoding="utf-8")
    return MockMhr(d)


def add_doc(repo, tmp_path, doc_id="doc_1", type_id="contract", pn="01001000001") -> None:
    w = PdfWriter()
    w.add_blank_page(200, 200)
    buf = io.BytesIO()
    w.write(buf)
    pdf = tmp_path / "Sorted" / f"{doc_id}.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(buf.getvalue())
    repo.upsert_document({"id": doc_id, "job_id": "job_1", "source_id": "src_1", "page_start": 0, "page_end": 0,
                          "status": "proposed", "type_id": type_id, "personal_number": pn,
                          "output_path": str(pdf), "output_sha256": hashlib.sha256(buf.getvalue()).hexdigest()})
    q.enqueue(repo, doc_id, pn)


class FakeValidate:
    def __init__(self, status="passed", exc: BaseException | None = None, original: MhrDocRef | None = None):
        self.status, self.exc, self.original, self.calls = status, exc, original, []

    def __call__(self, repo, cfg, doc_id, client) -> ValidationResult:
        self.calls.append(doc_id)
        if self.exc:
            raise self.exc
        return ValidationResult(status=self.status, employee=MhrEmployee("E001", personal_number="01001000001"),
                                original=self.original, summary=["ok"])


def status(repo, doc_id="doc_1") -> str:
    return repo.mhr_get(doc_id)["status"]


def uploads(mhr: MockMhr) -> list:
    p = mhr.dir / "uploads.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


class BoomClient:
    def __init__(self, exc, find=MhrUnavailable("down")):
        self.exc, self.find, self.calls, self.finds = exc, find, 0, 0

    def upload(self, *a, **k):
        self.calls += 1
        raise self.exc

    def find_by_external_id(self, external_id):
        self.finds += 1
        if isinstance(self.find, BaseException):
            raise self.find
        return self.find


# --- ვალიდაცია ------------------------------------------------------------------------------

def test_passed_to_ready_with_validation_and_employee(repo, cfg, mhr, tmp_path):
    add_doc(repo, tmp_path)
    c = q.sweep(repo, cfg, mhr, FakeValidate("passed"))
    row = repo.mhr_get("doc_1")
    assert c["ready"] == 1 and row["status"] == "ready" and row["employee_id"] == "E001"
    assert json.loads(row["validation"])["status"] == "passed"
    assert uploads(mhr) == []   # დადასტურების გარეშე არ იტვირთება


def test_require_approval_false_goes_to_approved_and_uploads_same_sweep(repo, cfg, mhr, tmp_path):
    cfg.data["mhr"]["require_approval"] = False
    add_doc(repo, tmp_path)
    c = q.sweep(repo, cfg, mhr, FakeValidate("passed"))
    assert c["approved"] == 1 and c["uploaded"] == 1 and status(repo) == "uploaded"


@pytest.mark.parametrize("res", ["failed", "skipped"])
def test_failed_and_skipped(repo, cfg, mhr, tmp_path, res):
    add_doc(repo, tmp_path)
    assert q.sweep(repo, cfg, mhr, FakeValidate(res))[res] == 1
    assert status(repo) == res


def test_unavailable_then_pending_after_next_try(repo, cfg, mhr, tmp_path):
    add_doc(repo, tmp_path)
    v = FakeValidate("unavailable")
    t0 = time.time()
    q.sweep(repo, cfg, mhr, v, now=lambda: t0)
    row = repo.mhr_get("doc_1")
    assert row["status"] == "unavailable" and row["attempts"] == 1
    assert row["next_try"] == pytest.approx(t0 + 600)
    q.sweep(repo, cfg, mhr, v, now=lambda: t0 + 60)       # ვადა ჯერ არ გასულა
    assert status(repo) == "unavailable" and len(v.calls) == 1
    v.status = "passed"
    c = q.sweep(repo, cfg, mhr, v, now=lambda: t0 + 601)
    assert c["requeued"] == 1 and status(repo) == "ready"


def test_validate_exception_is_unavailable_type_only(repo, cfg, mhr, tmp_path):
    add_doc(repo, tmp_path)
    q.sweep(repo, cfg, mhr, FakeValidate(exc=ValueError("ნინო ბერიძე 01001000001")))
    row = repo.mhr_get("doc_1")
    assert row["status"] == "unavailable" and row["error"] == "ValueError"
    events = " ".join(r["message"] for r in repo.q("SELECT message FROM events"))
    assert "01001000001" not in events and "ნინო" not in events


def test_type_not_uploadable_is_skipped_without_validation(repo, cfg, mhr, tmp_path):
    add_doc(repo, tmp_path, "doc_u", type_id="unknown")
    cfg.data["mhr"]["upload_type_ids"] = ["fine"]
    add_doc(repo, tmp_path, "doc_c", type_id="contract")
    v = FakeValidate("passed")
    assert q.sweep(repo, cfg, mhr, v)["skipped"] == 2
    assert v.calls == [] and status(repo, "doc_u") == "skipped" and status(repo, "doc_c") == "skipped"


def test_disabled_is_noop(repo, cfg, mhr, tmp_path):
    cfg.data["mhr"]["enabled"] = False
    add_doc(repo, tmp_path)
    v = FakeValidate()
    assert sum(q.sweep(repo, cfg, mhr, v).values()) == 0
    assert status(repo) == "pending" and v.calls == []


# --- ატვირთვა ---------------------------------------------------------------------------------

def test_approve_upload_uploaded_with_file_in_mock(repo, cfg, mhr, tmp_path):
    add_doc(repo, tmp_path)
    q.sweep(repo, cfg, mhr, FakeValidate())
    assert q.approve(repo, "doc_1", who="ლაშა")
    assert not q.approve(repo, "doc_1")            # ორმაგი დადასტურება — არაფერს ცვლის
    c = q.sweep(repo, cfg, mhr, FakeValidate())
    row = repo.mhr_get("doc_1")
    up = uploads(mhr)
    assert c["uploaded"] == 1 and row["status"] == "uploaded" and row["remote_id"] == up[0]["id"]
    assert row["decided_by"] == "ლაშა" and row["uploaded_at"]
    pdf = (tmp_path / "Sorted" / "doc_1.pdf").read_bytes()
    assert len(up) == 1 and up[0]["category"] == "contract" and up[0]["external_id"] == "doc_1"
    assert up[0]["sha256"] == hashlib.sha256(pdf).hexdigest() and (mhr.dir / up[0]["path"]).read_bytes() == pdf
    assert up[0]["title"] == f"{cfg.doc_type('contract').name_ka} — ხელმოწერილი სკანი"
    assert up[0]["signed_copy_of"] is None and up[0]["document_number"] is None
    md = json.loads(up[0]["metadata"])
    assert md == {"type_id": "contract", "source_sha256": None, "validation_status": "passed",
                  "pipeline_version": md["pipeline_version"]} and md["pipeline_version"]
    q.sweep(repo, cfg, mhr, FakeValidate())        # მეორე გავლა — ხელახლა არ იტვირთება
    assert len(uploads(mhr)) == 1 and not q.approve(repo, "doc_1")


def test_category_mapping(repo, cfg, mhr, tmp_path):
    cfg.data["mhr"]["categories"] = {"contract": "შრომითი"}
    add_doc(repo, tmp_path)
    q.sweep(repo, cfg, mhr, FakeValidate())
    q.approve(repo, "doc_1")
    q.sweep(repo, cfg, mhr, FakeValidate())
    assert uploads(mhr)[0]["category"] == "შრომითი"


def test_mhr_unavailable_during_upload_retries_later(repo, cfg, mhr, tmp_path):
    add_doc(repo, tmp_path)
    q.sweep(repo, cfg, mhr, FakeValidate())
    q.approve(repo, "doc_1")
    (mhr.dir / "unavailable").write_text("", encoding="utf-8")
    t0 = time.time()
    c = q.sweep(repo, cfg, mhr, FakeValidate(), now=lambda: t0)
    row = repo.mhr_get("doc_1")
    assert c["upload_retry"] == 1 and row["status"] == "approved" and row["attempts"] == 1 and row["next_try"] > t0
    (mhr.dir / "unavailable").unlink()
    q.sweep(repo, cfg, mhr, FakeValidate(), now=lambda: t0 + 60)    # next_try ჯერ არ მოსულა
    assert status(repo) == "approved"
    q.sweep(repo, cfg, mhr, FakeValidate(), now=lambda: t0 + 601)
    assert status(repo) == "uploaded" and len(uploads(mhr)) == 1


def test_upload_unavailable_capped_to_upload_failed(repo, cfg, tmp_path):
    add_doc(repo, tmp_path)
    client = BoomClient(MhrUnavailable("down"))
    q.sweep(repo, cfg, client, FakeValidate())
    q.approve(repo, "doc_1")
    t = time.time()
    for i in range(q.UPLOAD_MAX_ATTEMPTS):
        q.sweep(repo, cfg, client, FakeValidate(), now=lambda: t + i * 3600)
    assert status(repo) == "upload_failed" and client.calls == q.UPLOAD_MAX_ATTEMPTS


def test_mhr_error_upload_failed_then_retry_upload(repo, cfg, mhr, tmp_path):
    add_doc(repo, tmp_path)
    q.sweep(repo, cfg, mhr, FakeValidate())
    q.approve(repo, "doc_1")
    c = q.sweep(repo, cfg, BoomClient(MhrError("http_400")), FakeValidate())
    row = repo.mhr_get("doc_1")
    assert c["upload_failed"] == 1 and row["status"] == "upload_failed" and row["error"] == "http_400"
    assert not q.approve(repo, "doc_1")
    assert q.retry_upload(repo, "doc_1") and status(repo) == "approved"
    q.sweep(repo, cfg, mhr, FakeValidate())
    assert status(repo) == "uploaded" and len(uploads(mhr)) == 1


def test_uncertain_upload_stays_needs_check_while_lookup_unavailable(repo, cfg, tmp_path):
    add_doc(repo, tmp_path)
    client = BoomClient(MhrUploadUncertain("upload result unknown (ReadTimeout)"))   # find → MhrUnavailable
    q.sweep(repo, cfg, client, FakeValidate())
    q.approve(repo, "doc_1")
    t = time.time()
    q.sweep(repo, cfg, client, FakeValidate(), now=lambda: t)
    row = repo.mhr_get("doc_1")
    assert row["status"] == "needs_check" and row["attempts"] == 1 and row["next_try"] > t and client.finds == 1
    q.sweep(repo, cfg, client, FakeValidate(), now=lambda: t + 86400)
    assert status(repo) == "needs_check" and client.calls == 1 and client.finds == 2   # ხელახლა არ იგზავნება


def test_uncertain_upload_found_by_external_id_is_uploaded(repo, cfg, tmp_path):
    add_doc(repo, tmp_path)
    client = BoomClient(MhrUploadUncertain("x"), find=MhrDocRef("9310", raw={"employee_id": "E001"}))
    q.sweep(repo, cfg, client, FakeValidate())
    q.approve(repo, "doc_1")
    c = q.sweep(repo, cfg, client, FakeValidate())
    row = repo.mhr_get("doc_1")
    assert c["uploaded"] == 1 and row["status"] == "uploaded" and row["remote_id"] == "9310" and client.calls == 1


def test_uncertain_found_on_other_employee_is_not_trusted(repo, cfg, tmp_path):
    add_doc(repo, tmp_path)
    client = BoomClient(MhrUploadUncertain("x"), find=MhrDocRef("9310", raw={"employee_id": "E999"}))
    q.sweep(repo, cfg, client, FakeValidate())
    q.approve(repo, "doc_1")
    q.sweep(repo, cfg, client, FakeValidate(), now=lambda: time.time() + 86400 * 2)
    assert status(repo) == "needs_check" and client.calls == 1


def test_needs_check_not_found_goes_back_to_approved_and_uploads_once(repo, cfg, mhr, tmp_path):
    add_doc(repo, tmp_path)
    q.sweep(repo, cfg, mhr, FakeValidate())
    q.approve(repo, "doc_1")
    t = time.time()
    q.sweep(repo, cfg, BoomClient(MhrUploadUncertain("x"), find=None), FakeValidate(), now=lambda: t)
    assert status(repo) == "needs_check"                              # მაშინვე „არ არის" ცალსახა არ არის
    c = q.sweep(repo, cfg, mhr, FakeValidate(), now=lambda: t + 601)   # find → None ⇒ approved ⇒ ატვირთვა
    assert c["upload_retry"] == 1 and c["uploaded"] == 1 and status(repo) == "uploaded"
    q.sweep(repo, cfg, mhr, FakeValidate(), now=lambda: t + 1300)
    assert len(uploads(mhr)) == 1


def test_needs_check_attempts_capped_then_manual_only(repo, cfg, tmp_path):
    add_doc(repo, tmp_path)
    client = BoomClient(MhrUploadUncertain("x"), find=None)
    q.sweep(repo, cfg, client, FakeValidate())
    q.approve(repo, "doc_1")
    t = time.time()
    for i in range(q.UPLOAD_MAX_ATTEMPTS + 3):
        q.sweep(repo, cfg, client, FakeValidate(), now=lambda: t + i * 3600)
    assert status(repo) == "needs_check" and client.calls == q.UPLOAD_MAX_ATTEMPTS


def test_stale_uploading_resolved_by_external_id(repo, cfg, mhr, tmp_path):
    add_doc(repo, tmp_path)
    q.sweep(repo, cfg, mhr, FakeValidate())
    q.approve(repo, "doc_1")
    assert repo.mhr_transition("doc_1", ("approved",), "uploading")   # „ავარია" ატვირთვის შუაში
    q.sweep(repo, cfg, mhr, FakeValidate())                           # ჯერ ახალია — ხელს არ ვახლებთ
    assert status(repo) == "uploading" and uploads(mhr) == []
    c = q.sweep(repo, cfg, mhr, FakeValidate(), now=lambda: time.time() + 3600)
    # stale → needs_check → MHR-ში არ არის → approved → ატვირთვა (ერთხელ)
    assert c["needs_check"] == 1 and status(repo) == "uploaded" and len(uploads(mhr)) == 1


def test_stale_uploading_that_reached_mhr_is_not_uploaded_twice(repo, cfg, mhr, tmp_path):
    add_doc(repo, tmp_path)
    q.sweep(repo, cfg, mhr, FakeValidate())
    q.approve(repo, "doc_1")
    assert repo.mhr_transition("doc_1", ("approved",), "uploading")
    pdf = (tmp_path / "Sorted" / "doc_1.pdf").read_bytes()
    rid = mhr.upload("E001", pdf, "doc_1.pdf", "contract",
                     {"external_id": "doc_1", "sha256": hashlib.sha256(pdf).hexdigest()})   # მივიდა, მერე ავარია
    c = q.sweep(repo, cfg, mhr, FakeValidate(), now=lambda: time.time() + 3600)
    row = repo.mhr_get("doc_1")
    assert c["recovered"] == 1 and row["status"] == "uploaded" and row["remote_id"] == rid
    assert len(uploads(mhr)) == 1
    assert not q.revalidate(repo, "doc_1")                              # uploaded — აღარაფერი ხდება


def test_signed_copy_of_from_generated_original(repo, cfg, mhr, tmp_path):
    (mhr.dir / "documents.json").write_text(json.dumps([
        {"id": "9001", "employee_id": "E001", "category": "contract", "kind": "generated", "signed_at": None}]),
        encoding="utf-8")
    add_doc(repo, tmp_path)
    original = MhrDocRef("9001", "contract", raw={"id": "9001", "employee_id": "E001", "kind": "generated"})
    q.sweep(repo, cfg, mhr, FakeValidate(original=original))
    q.approve(repo, "doc_1")
    q.sweep(repo, cfg, mhr, FakeValidate())
    assert status(repo) == "uploaded" and uploads(mhr)[0]["signed_copy_of"] == "9001"
    assert json.loads((mhr.dir / "documents.json").read_text(encoding="utf-8"))[0]["signed_at"]
    # ატვირთული სკანი (kind uploaded) ორიგინალად არ ითვლება → signed_copy_of არ იგზავნება
    add_doc(repo, tmp_path, "doc_2")
    q.sweep(repo, cfg, mhr, FakeValidate(original=MhrDocRef("9310", raw={"kind": "uploaded"})))
    q.approve(repo, "doc_2")
    q.sweep(repo, cfg, mhr, FakeValidate())
    assert status(repo, "doc_2") == "uploaded" and uploads(mhr)[1]["signed_copy_of"] is None


def test_superseded_document_rejected(repo, cfg, mhr, tmp_path):
    add_doc(repo, tmp_path)
    q.sweep(repo, cfg, mhr, FakeValidate())
    q.approve(repo, "doc_1")
    repo.upsert_document({"id": "doc_1", "job_id": "job_1", "source_id": "src_1", "page_start": 0, "page_end": 0,
                          "status": "superseded", "active": 0})
    c = q.sweep(repo, cfg, mhr, FakeValidate())
    row = repo.mhr_get("doc_1")
    assert c["rejected"] == 1 and row["status"] == "rejected" and row["error"] == "document superseded"
    assert uploads(mhr) == []


def test_changed_output_file_not_uploaded(repo, cfg, mhr, tmp_path):
    add_doc(repo, tmp_path)
    q.sweep(repo, cfg, mhr, FakeValidate())
    q.approve(repo, "doc_1")
    (tmp_path / "Sorted" / "doc_1.pdf").write_bytes(b"%PDF-other")
    q.sweep(repo, cfg, mhr, FakeValidate())
    assert status(repo) == "upload_failed" and uploads(mhr) == []


def test_reject_and_revalidate(repo, cfg, mhr, tmp_path):
    add_doc(repo, tmp_path)
    assert not q.reject(repo, "doc_1")             # pending — ჯერ არაფერია უარსაყოფი
    q.sweep(repo, cfg, mhr, FakeValidate("failed"))
    assert q.revalidate(repo, "doc_1") and status(repo) == "pending"
    q.sweep(repo, cfg, mhr, FakeValidate())
    assert q.reject(repo, "doc_1", who="op") and status(repo) == "rejected"
    assert not q.approve(repo, "doc_1") and not q.retry_upload(repo, "doc_1")
    assert not q.enqueue(repo, "doc_1", "01001000001")   # არსებული ჩანაწერი არ იცვლება


def test_default_client_built_from_config(repo, cfg, mhr, tmp_path):
    add_doc(repo, tmp_path)
    q.sweep(repo, cfg, None, FakeValidate())
    q.approve(repo, "doc_1")
    q.sweep(repo, cfg, None, FakeValidate())
    assert status(repo) == "uploaded" and len(uploads(mhr)) == 1


def test_worker_heartbeat_runs_sweep_and_survives_failure(cfg, monkeypatch):
    import threading

    from hrsorter.worker import Worker
    called = threading.Event()

    def boom(repo, c):
        called.set()
        raise RuntimeError("sweep failed")

    monkeypatch.setattr(q, "sweep", boom)
    stop = threading.Event()
    t = threading.Thread(target=Worker(lambda: cfg, lambda *a: None).run_forever, args=(stop,))
    t.start()
    try:
        assert called.wait(10)
    finally:
        stop.set()
        t.join(timeout=20)
    assert not t.is_alive()


def test_operator_override_uploads_failed_document(repo, cfg, mhr, tmp_path):
    """შედარება ვერ გაიარა (მაგ. ცრუ განგაში ბეჭედზე) — ოპერატორმა ნახა და მაინც ატვირთა."""
    add_doc(repo, tmp_path)
    q.sweep(repo, cfg, mhr, FakeValidate("failed"))
    assert status(repo) == "failed" and not q.approve(repo, "doc_1")      # ჩვეულებრივი დადასტურება — არა
    assert q.approve_override(repo, "doc_1", who="op") and status(repo) == "approved"
    assert repo.mhr_get("doc_1")["decided_by"] == "op (override)"
    assert any("override" in r["message"] for r in repo.q("SELECT message FROM events WHERE kind='mhr'"))
    q.sweep(repo, cfg, mhr, FakeValidate())
    assert status(repo) == "uploaded" and len(uploads(mhr)) == 1


def test_operator_override_needs_employee_found_in_mhr(repo, cfg, mhr, tmp_path):
    add_doc(repo, tmp_path)
    q.sweep(repo, cfg, mhr, FakeValidate("failed"))
    repo.conn.execute("UPDATE mhr_items SET employee_id=NULL WHERE document_id='doc_1'")   # თანამშრომელი ვერ მოიძებნა
    assert not q.approve_override(repo, "doc_1") and status(repo) == "failed"


# --- ხელით ატვირთვა (approve_manual / upload_now) ----------------------------------------------

EMP = MhrEmployee("E001", personal_number="01001000001", first_name="ნინო", last_name="ბერიძე")


def test_manual_approve_skips_validation_and_uploads_now(repo, cfg, mhr, tmp_path):
    add_doc(repo, tmp_path)
    q.sweep(repo, cfg, mhr, FakeValidate("failed"))        # ავტომატური შედარება ვერ გაიარა
    assert status(repo) == "failed"
    assert q.approve_manual(repo, "doc_1", EMP, "01001000001", who="ლაშა (manual)") is None
    row = repo.mhr_get("doc_1")
    assert row["status"] == "approved" and row["employee_id"] == "E001" and row["decided_by"] == "ლაშა (manual)"
    assert json.loads(row["validation"])["status"] == "manual"
    assert q.upload_now(repo, cfg, "doc_1", mhr) == "uploaded"
    up = uploads(mhr)
    assert len(up) == 1 and up[0]["employee_id"] == "E001" and json.loads(up[0]["metadata"])["validation_status"] == "manual"
    assert q.approve_manual(repo, "doc_1", EMP, "01001000001") == "already_uploaded"
    q.sweep(repo, cfg, mhr, FakeValidate())                  # worker ხელახლა არ ტვირთავს
    assert len(uploads(mhr)) == 1


def test_manual_approve_without_queue_row_and_unknown_type(repo, cfg, mhr, tmp_path):
    add_doc(repo, tmp_path, type_id="unknown")
    repo.conn.execute("DELETE FROM mhr_items")               # მაგ. დოკუმენტი „გადასამოწმებელში“ იყო — რიგში არ ჩავარდა
    assert q.approve_manual(repo, "doc_1", EMP, "01001000001") == "type_unknown"
    assert q.approve_manual(repo, "doc_1", EMP, "01001000001", type_id="contract") is None
    assert repo.get_document("doc_1")["type_id"] == "contract"
    assert q.upload_now(repo, cfg, "doc_1", mhr) == "uploaded"
    assert uploads(mhr)[0]["category"] == "contract"


def test_manual_approve_blocked_and_unavailable(repo, cfg, mhr, tmp_path):
    add_doc(repo, tmp_path)
    assert q.approve_manual(repo, "doc_x", EMP, "01001000001") == "document_not_found"
    repo.mhr_transition("doc_1", ("pending",), "uploading")
    assert q.approve_manual(repo, "doc_1", EMP, "01001000001") == "already_uploading"
    repo.mhr_transition("doc_1", ("uploading",), "pending")
    assert q.approve_manual(repo, "doc_1", EMP, "01001000001") is None
    (mhr.dir / "unavailable").write_text("")
    assert q.upload_now(repo, cfg, "doc_1", mhr) == "approved"   # worker მოგვიანებით სცდის
    assert repo.mhr_get("doc_1")["error"] == "MHR unavailable"
