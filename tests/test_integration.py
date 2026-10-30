"""ბოლომდე გავლა: სინთეზური PDF-ები ინბოქსში → სკანირება → რიგი → შედეგები (სპეც. §20 ფუნქციური კრიტერიუმები)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from hrsorter.cli import reprocess
from hrsorter.config import EXAMPLE_CONFIG_PATH, EXAMPLE_TYPES_PATH, make_config
from hrsorter.db import Repository
from hrsorter.ocr.anthropic_vision import AnthropicVisionProvider
from hrsorter.ocr.chain import build_ocr_chain
from hrsorter.pdfio import sha256_file
from hrsorter.pipeline import process_job
from hrsorter.watcher import InboxScanner
from hrsorter.worker import Worker
from tools import synthetic


def _cfg(root: Path, **over):
    example = json.loads(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))
    base = {"paths": {"root": str(root)}, "watcher": {"stability_seconds": 0}, "companies": example["companies"],
            "employee_api": {"enabled": True, "mode": "mock"}}
    base.update(over)
    cfg = make_config(base)
    for d in [*cfg.paths.internal_dirs(), *[p.inbox for p in cfg.profiles]]:
        d.mkdir(parents=True, exist_ok=True)
    return cfg


def _run(cfg) -> Repository:
    repo = Repository(cfg.paths.db)
    sc = InboxScanner(cfg, repo)
    sc.scan_once()
    sc.scan_once()
    w = Worker(lambda: cfg, process_job)
    while w.run_once():
        pass
    return repo


def _final_files(cfg) -> list[Path]:
    p = cfg.paths
    return [f for d in (p.sorted, p.proposed, p.needs_review) for f in d.rglob("*.pdf")]


@pytest.fixture()
def processed(tmp_path):
    cfg = _cfg(tmp_path)
    batch = cfg.profile("batch").inbox / "batch.pdf"
    gt = synthetic.make_batch(batch, n_docs=25, seed=7)
    synthetic.make_singles(cfg.profile("single").inbox, n=5, seed=7)
    repo = _run(cfg)
    return cfg, repo, gt, batch


def test_end_to_end_accounting_and_integrity(processed):
    cfg, repo, gt, batch = processed
    assert {r["status"] for r in repo.q("SELECT status FROM jobs")} == {"done"}
    src = repo.source_by_sha(sha256_file(batch))
    job = repo.one("SELECT id FROM jobs WHERE source_id=?", (src["id"],))
    pages = [r["page_index"] for r in repo.q(
        "SELECT page_index FROM page_assignments WHERE job_id=? ORDER BY page_index", (job["id"],))]
    assert pages == list(range(gt["pages"]))                                  # ყოველი გვერდი ზუსტად ერთხელ
    assert sha256_file(src["archive_path"]) == src["sha256"] == sha256_file(batch)  # ორიგინალი უცვლელი
    files = _final_files(cfg)
    assert files
    for f in files:                                                           # ყოველ შედეგს აქვს sidecar
        meta = json.loads(f.with_suffix(".json").read_text(encoding="utf-8"))
        assert meta["output_sha256"] == sha256_file(f)
        assert repo.get_source(meta["source_id"])["sha256"] == meta["source_sha256"]


def test_trial_mode_never_auto_saves_and_unknown_year_folder(processed):
    cfg, repo, *_ = processed
    assert not list(cfg.paths.sorted.rglob("*.pdf"))                          # საცდელ რეჟიმში Sorted ცარიელია
    for r in repo.q("SELECT output_path FROM documents WHERE status='proposed' AND year IS NULL"):
        assert "წელი-უცნობია" in r["output_path"]


def test_redetection_creates_no_new_outputs(processed):
    cfg, repo, gt, batch = processed
    before = sorted(map(str, _final_files(cfg)))
    jobs = repo.one("SELECT COUNT(*) n FROM jobs")["n"]
    shutil.copyfile(batch, cfg.profile("batch").inbox / "batch_copy_again.pdf")  # იგივე შიგთავსი, სხვა სახელი
    _run(cfg)
    assert sorted(map(str, _final_files(cfg))) == before
    assert repo.one("SELECT COUNT(*) n FROM jobs")["n"] == jobs


def test_reprocess_keeps_single_active_result(processed):
    cfg, repo, gt, batch = processed
    src = repo.source_by_sha(sha256_file(batch))
    job = repo.one("SELECT id FROM jobs WHERE source_id=?", (src["id"],))
    n_active = len(repo.active_documents_for_source(src["id"]))
    before = len(_final_files(cfg))
    assert reprocess(repo, cfg, job["id"])
    _run(cfg)
    assert len(repo.active_documents_for_source(src["id"])) == n_active
    assert len(_final_files(cfg)) == before                                    # ძველები _Superseded-ში
    assert list(cfg.paths.superseded.rglob("*.pdf"))


def test_corrupt_and_encrypted_go_to_error(tmp_path):
    cfg = _cfg(tmp_path)
    synthetic.make_corrupt(cfg.profile("single").inbox / "bad.pdf")
    synthetic.make_encrypted(cfg.profile("single").inbox / "locked.pdf")
    repo = _run(cfg)
    codes = {r["error_code"] for r in repo.q("SELECT error_code FROM inbox_files WHERE state='error'")}
    assert codes == {"pdf_corrupt", "pdf_encrypted"}


def test_online_ocr_is_off_unless_explicitly_enabled(tmp_path):
    assert build_ocr_chain(_cfg(tmp_path)).describe()["sends_outside_machine"] is False
    half = _cfg(tmp_path, ocr={"path": "hybrid", "online": {"enabled": False, "provider": "anthropic"}})
    assert build_ocr_chain(half).describe()["sends_outside_machine"] is False
    on = _cfg(tmp_path, ocr={"path": "hybrid", "online": {"enabled": True, "provider": "anthropic"}})
    assert isinstance(build_ocr_chain(on).online, AnthropicVisionProvider)


def test_new_type_from_json_only(tmp_path):
    from hrsorter.classifier import Classifier
    types = json.loads(EXAMPLE_TYPES_PATH.read_text(encoding="utf-8"))
    types["types"].append({"id": "reference", "name_ka": "ცნობა", "folder": "ცნობა", "notes": "დამატებითი გასაღები",
                           "positive": [{"pattern": "ცნობა", "weight": 5, "title_only": True}]})
    cfg = make_config({"paths": {"root": str(tmp_path)}}, types_data=types)
    assert cfg.doc_type("reference").auto_save_enabled is False                 # ახალი ტიპი — ჯერ არა ავტომატური
    text = "შპს „მაგალითი“\nცნობა\nგაიცა იმის დასადასტურებლად, რომ ნინო ბერიძე მუშაობს კომპანიაში."
    assert Classifier(cfg).classify_text(text).type_id == "reference"


@pytest.mark.slow
def test_500_page_batch(tmp_path):
    cfg = _cfg(tmp_path)
    gt = synthetic.make_batch(cfg.profile("batch").inbox / "big.pdf", n_docs=420, seed=11)
    assert gt["pages"] >= 500
    repo = _run(cfg)
    job = repo.one("SELECT * FROM jobs")
    assert job["status"] == "done"
    assert repo.one("SELECT COUNT(*) n FROM page_assignments WHERE job_id=?", (job["id"],))["n"] == gt["pages"]


def test_failed_reprocess_keeps_previous_results_active(processed, monkeypatch):
    cfg, repo, gt, batch = processed
    src = repo.source_by_sha(sha256_file(batch))
    job = repo.one("SELECT id FROM jobs WHERE source_id=?", (src["id"],))
    old = {r["id"] for r in repo.active_documents_for_source(src["id"])}
    import hrsorter.pipeline as pl
    from hrsorter.errors import JobError
    real = pl.publish
    calls = {"n": 0}

    def flaky(repo_, outcome, cfg_):
        calls["n"] += 1
        if calls["n"] == 3:
            raise JobError("output_verify_failed", "simulated", retryable=False)
        return real(repo_, outcome, cfg_)
    monkeypatch.setattr(pl, "publish", flaky)
    assert reprocess(repo, cfg, job["id"])
    _run(cfg)
    assert old <= {r["id"] for r in repo.active_documents_for_source(src["id"])}   # ძველი შედეგები არ გაქრა

