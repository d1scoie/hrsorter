from __future__ import annotations

import errno
import json
from pathlib import Path

import pytest
from pypdf import PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from hrsorter import output
from hrsorter.db import Repository
from hrsorter.errors import JobError
from hrsorter.models import EmployeeLookup, EmployeeRecord
from hrsorter.output import publish, recover_publishing, retire_previous
from hrsorter.pdfio import sha256_file
from hrsorter.sidecar import validate_sidecar
from tests.test_naming import make_cfg, make_outcome

PROJECT = Path(__file__).resolve().parent.parent
FONT = "C:/Windows/Fonts/sylfaen.ttf"


def make_pdf(path: Path, n: int = 3) -> Path:
    c = canvas.Canvas(str(path), pagesize=A4)
    if Path(FONT).exists():
        pdfmetrics.registerFont(TTFont("Sylfaen", FONT))
        c.setFont("Sylfaen", 14)
    for i in range(n):
        c.drawString(72, 750, f"ბრძანება № {i + 1} — სატესტო გვერდი {i + 1}")
        c.showPage()
    c.save()
    return path


@pytest.fixture
def env(tmp_path):
    cfg = make_cfg(tmp_path / "root")
    repo = Repository(tmp_path / "state.sqlite3")
    src = make_pdf(tmp_path / "source.pdf")
    yield cfg, repo, src
    repo.close()


def outcome_for(src: Path, **kw):
    kw.setdefault("pages", [0, 1])
    return make_outcome(source_path=str(src), source_sha256=sha256_file(src), **kw)


def pdfs_under(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.pdf") if "_Work" not in p.parts)


def test_publish_atomic_proposed_with_valid_sidecar(env):
    cfg, repo, src = env
    o = publish(repo, outcome_for(src), cfg)
    out = Path(o.output_path)
    assert out.parent == cfg.paths.proposed / "მაგალითი" / "ჯარიმა" / "2024"
    assert len(PdfReader(str(out)).pages) == 2
    assert sha256_file(out) == o.output_sha256
    side = json.loads(Path(o.sidecar_path).read_text(encoding="utf-8"))
    validate_sidecar(side)
    assert side["source_page_ranges"] == [{"start": 1, "end": 2}]
    assert side["output_sha256"] == o.output_sha256 and side["processing_status"] == "proposed"
    assert side["document_type_name"] == "ჯარიმა"
    row = repo.get_document(o.document_id)
    assert row["status"] == "proposed" and row["active"] == 1 and row["output_path"] == str(out)
    assert json.loads(row["metadata"])["document_id"] == o.document_id
    assert list(cfg.paths.work.glob("*")) == []   # tmp ფაილები არ რჩება


def test_auto_saved_and_needs_review_base_dirs(env):
    cfg, repo, src = env
    a = publish(repo, outcome_for(src, outcome="auto_saved"), cfg)
    assert Path(a.output_path).is_relative_to(cfg.paths.sorted)
    r = publish(repo, outcome_for(src, outcome="needs_review", reasons=["uncertain_boundary"], pages=[2]), cfg)
    assert Path(r.output_path).parent == cfg.paths.needs_review / "uncertain_boundary"
    validate_sidecar(json.loads(Path(r.sidecar_path).read_text(encoding="utf-8")))


def test_pn_leading_zeros_preserved(env):
    cfg, repo, src = env
    rec = EmployeeRecord("E004", "მარიამ", "ლომიძე", "00001000004", company_id="acme")
    o = publish(repo, outcome_for(src, pn="00001000004",
                                  employee=EmployeeLookup("verified", record=rec, matched_by="personal_number")), cfg)
    side = json.loads(Path(o.sidecar_path).read_text(encoding="utf-8"))
    assert side["personal_number"] == "00001000004"
    assert side["fields"]["personal_number"]["value"] == "00001000004"
    assert side["employee_id"] == "E004"
    assert "_00001000004_" in Path(o.output_path).name
    assert repo.get_document(o.document_id)["personal_number"] == "00001000004"


def test_idempotent_republish(env):
    cfg, repo, src = env
    o = outcome_for(src)
    first = publish(repo, o, cfg)
    second = publish(repo, o, cfg)
    assert first.output_path == second.output_path
    assert len(pdfs_under(cfg.paths.root)) == 1


def test_crash_between_replace_and_db_update_recovers_without_duplicates(env, monkeypatch):
    cfg, repo, src = env
    o = outcome_for(src)

    def crash(*a, **k):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(output, "_finalize_row", crash)
    with pytest.raises(RuntimeError):
        publish(repo, o, cfg)
    monkeypatch.undo()
    assert repo.get_document(o.document_id)["status"] == "publishing"
    assert len(pdfs_under(cfg.paths.root)) == 1      # os.replace უკვე მოხდა

    assert recover_publishing(repo, cfg) == 1
    row = repo.get_document(o.document_id)
    assert row["status"] == "proposed" and row["output_path"]
    again = publish(repo, o, cfg)                       # job-ის ხელახალი გაშვება
    assert again.output_path == row["output_path"]
    assert len(pdfs_under(cfg.paths.root)) == 1
    assert len(list(cfg.paths.root.rglob("*.json"))) == 1


def test_crash_then_rerun_without_explicit_recovery(env, monkeypatch):
    cfg, repo, src = env
    o = outcome_for(src)
    monkeypatch.setattr(output, "_finalize_row", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        publish(repo, o, cfg)
    monkeypatch.undo()
    again = publish(repo, o, cfg)
    assert repo.get_document(o.document_id)["status"] == "proposed"
    assert len(pdfs_under(cfg.paths.root)) == 1 and Path(again.output_path).exists()


def test_crash_before_replace_cleans_and_republishes(env, monkeypatch):
    cfg, repo, src = env
    o = outcome_for(src)

    def boom(a, b):
        raise RuntimeError("crash before replace")

    monkeypatch.setattr(output.os, "replace", boom)
    with pytest.raises(RuntimeError):
        publish(repo, o, cfg)
    monkeypatch.undo()
    assert pdfs_under(cfg.paths.root) == []
    assert recover_publishing(repo, cfg) == 1
    assert repo.get_document(o.document_id) is None
    publish(repo, o, cfg)
    assert len(pdfs_under(cfg.paths.root)) == 1


def test_output_verify_failure(env, monkeypatch):
    cfg, repo, src = env
    monkeypatch.setattr(output, "write_subset", lambda s, pages, dest: Path(dest).write_bytes(b"not a pdf"))
    with pytest.raises(JobError) as ei:
        publish(repo, outcome_for(src), cfg)
    assert ei.value.code == "output_verify_failed"
    assert pdfs_under(cfg.paths.root) == []


def test_disk_full_is_retryable(env, monkeypatch):
    cfg, repo, src = env

    def full(*a):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(output, "write_subset", full)
    with pytest.raises(JobError) as ei:
        publish(repo, outcome_for(src), cfg)
    assert ei.value.code == "disk_full" and ei.value.retryable


def test_retire_previous_moves_to_superseded(env):
    cfg, repo, src = env
    old = publish(repo, outcome_for(src, job_id="job_" + "a" * 32), cfg)
    keep = publish(repo, outcome_for(src, job_id="job_" + "b" * 32), cfg)
    n = retire_previous(repo, old.source_id, keep.job_id, cfg)
    assert n == 1
    assert not Path(old.output_path).exists() and Path(keep.output_path).exists()
    row = repo.get_document(old.document_id)
    assert row["active"] == 0 and row["status"] == "superseded" and row["superseded_by"] == keep.job_id
    moved = Path(row["output_path"])
    assert moved.parent == cfg.paths.superseded / old.document_id and moved.exists()
    side = json.loads(Path(row["sidecar_path"]).read_text(encoding="utf-8"))
    assert side["processing_status"] == "superseded" and side["superseded_by"] == keep.job_id
    validate_sidecar(side)
    assert [r["id"] for r in repo.active_documents_for_source(old.source_id)] == [keep.document_id]


def test_retire_previous_rejects_stale_mhr_item(env):
    """ხელახალი დამუშავება: ძველი დოკუმენტის „ვერ გაიარა"/„მზადაა" ჩანაწერი MHR-ის სიაში აღარ უნდა დარჩეს (ერთი სკანი
    ორჯერ ჩანდა); ატვირთული ჩანაწერი უცვლელია."""
    cfg, repo, src = env
    old = publish(repo, outcome_for(src, job_id="job_" + "c" * 32), cfg)
    keep = publish(repo, outcome_for(src, job_id="job_" + "d" * 32), cfg)
    repo.mhr_enqueue(old.document_id, "01001000001")
    assert repo.mhr_transition(old.document_id, ("pending",), "failed")
    retire_previous(repo, old.source_id, keep.job_id, cfg)
    assert repo.mhr_get(old.document_id)["status"] == "rejected"
    up = publish(repo, outcome_for(src, job_id="job_" + "e" * 32), cfg)
    repo.mhr_enqueue(up.document_id, "01001000001")
    assert repo.mhr_transition(up.document_id, ("pending",), "uploaded")
    retire_previous(repo, up.source_id, "job_" + "f" * 32, cfg)
    assert repo.mhr_get(up.document_id)["status"] == "uploaded"


def test_windows_forbidden_chars_and_reserved_names(env):
    cfg, repo, src = env
    o = publish(repo, outcome_for(src, first="CON", last='ა<ბ>:გ"/\\|?*', doc_number="LPT1"), cfg)
    p = Path(o.output_path)
    assert p.exists()
    assert not any(ch in p.name for ch in '<>:"/\\|?*')
    assert "__LPT1_" in p.name   # რეზერვირებულ კომპონენტს `_` ემატება


def test_reserved_name_as_whole_file_name(env):
    cfg, repo, src = env
    cfg.data["naming"]["template"] = "{name}"
    o = publish(repo, outcome_for(src, first="nul", last=None), cfg)
    assert Path(o.output_path).name.lower() == "_nul.pdf" and Path(o.output_path).exists()


def test_same_name_collision_gets_suffix(env):
    cfg, repo, src = env
    a = publish(repo, outcome_for(src, document_id="doc_ab12cd34" + "0" * 24), cfg)
    b = publish(repo, outcome_for(src, document_id="doc_ab12cd34" + "1" * 24), cfg)
    assert Path(b.output_path).name == Path(a.output_path).stem + "-2.pdf"
    assert Path(a.output_path).exists() and Path(b.output_path).exists()
    assert sha256_file(a.output_path) == a.output_sha256


def test_example_sidecar_is_valid():
    validate_sidecar(json.loads((PROJECT / "docs" / "sidecar_example.json").read_text(encoding="utf-8")))
