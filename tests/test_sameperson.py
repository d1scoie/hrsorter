from __future__ import annotations

import json

import pytest

from hrsorter import sameperson
from hrsorter.config import make_config
from hrsorter.db import Repository

PN_A, PN_B = "01001000001", "01001000002"


@pytest.fixture
def cfg(tmp_path):
    return make_config({"paths": {"root": str(tmp_path)},
                        "mhr": {"enabled": True, "mode": "mock", "mock_dir": str(tmp_path / "mhr")}})


@pytest.fixture
def repo(cfg):
    r = Repository(cfg.paths.db)
    yield r
    r.close()


def _field(value):
    return {"value": value, "raw": value, "source": "document" if value else None, "page": 1 if value else None,
            "confidence": 0.9 if value else 0.0, "evidence": None, "handwritten": False, "candidates": []}


def add_doc(repo, tmp_path, doc_id, first="გიორგი", last="ბერიძე", pn=None, status="needs_review",
            type_id="unknown", reasons=("uncertain_boundary", "unknown_type"), sidecar=False):
    meta = {"fields": {"first_name": _field(first), "last_name": _field(last), "personal_number": _field(pn)},
            "flags": [] if pn else ["employee_unverified", "personal_number_missing"]}
    side = tmp_path / f"{doc_id}.json"
    if sidecar:
        side.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    repo.upsert_document({"id": doc_id, "job_id": "job_1", "source_id": "src_1", "page_start": 0, "page_end": 0,
                          "status": status, "type_id": type_id, "personal_number": pn, "review_reasons": list(reasons),
                          "output_path": str(tmp_path / f"{doc_id}.pdf"), "sidecar_path": str(side) if sidecar else None,
                          "metadata": meta})


def confirm(repo, doc_id, pn, employee_id, first="გიორგი", last="ბერიძე"):
    """MHR-მა ამ დოკუმენტის პ/ნ-ით თანამშრომელი იპოვა."""
    repo.mhr_enqueue(doc_id, pn)
    repo.mhr_transition(doc_id, ("pending",), "failed", employee_id=employee_id, validation={
        "status": "failed", "employee": {"employee_id": employee_id, "personal_number": pn, "first_name": first, "last_name": last}})


def test_annex_without_pn_gets_it_from_the_same_employees_contract(repo, cfg, tmp_path):
    add_doc(repo, tmp_path, "doc_contract", pn=PN_A, status="proposed", type_id="contract", reasons=())
    confirm(repo, "doc_contract", PN_A, "E1")
    add_doc(repo, tmp_path, "doc_annex", last="ბერიძემ", sidecar=True)   # ბრუნვა ხელს არ უშლის

    assert sameperson.sweep(repo, cfg) == 1
    doc = repo.get_document("doc_annex")
    meta = json.loads(doc["metadata"])
    assert doc["personal_number"] == PN_A and doc["status"] == "needs_review"   # ტიპს MHR-თან შედარება დაადგენს
    assert meta["fields"]["personal_number"]["source"] == "derived"
    assert meta["flags"] == ["employee_unverified"]
    assert json.loads((tmp_path / "doc_annex.json").read_text(encoding="utf-8"))["fields"]["personal_number"]["value"] == PN_A
    item = repo.mhr_get("doc_annex")
    assert item["status"] == "pending" and item["personal_number"] == PN_A
    assert sameperson.sweep(repo, cfg) == 0                                       # მეორედ აღარ ეხება


def test_skipped_item_without_pn_is_revalidated(repo, cfg, tmp_path):
    add_doc(repo, tmp_path, "doc_contract", pn=PN_A, status="proposed", type_id="contract", reasons=())
    confirm(repo, "doc_contract", PN_A, "E1")
    add_doc(repo, tmp_path, "doc_rules", status="proposed", type_id="contract_annex", reasons=())
    repo.mhr_enqueue("doc_rules", None)
    repo.mhr_transition("doc_rules", ("pending",), "skipped", validation={"status": "skipped"})

    assert sameperson.sweep(repo, cfg) == 1
    item = repo.mhr_get("doc_rules")
    assert item["status"] == "pending" and item["personal_number"] == PN_A and item["validation"] is None


def test_two_people_with_the_same_name_are_left_to_the_operator(repo, cfg, tmp_path):
    for doc_id, pn, emp in (("doc_c1", PN_A, "1"), ("doc_c2", PN_B, "2")):
        add_doc(repo, tmp_path, doc_id, pn=pn, status="proposed", type_id="contract", reasons=())
        confirm(repo, doc_id, pn, emp)
    add_doc(repo, tmp_path, "doc_annex")

    assert sameperson.sweep(repo, cfg) == 0
    assert repo.get_document("doc_annex")["personal_number"] is None and repo.mhr_get("doc_annex") is None


def test_rehired_employee_with_two_mhr_records_is_one_person(repo, cfg, tmp_path):
    for doc_id, emp in (("doc_c1", "1"), ("doc_c2", "2")):
        add_doc(repo, tmp_path, doc_id, pn=PN_A, status="proposed", type_id="contract", reasons=())
        confirm(repo, doc_id, PN_A, emp)
    add_doc(repo, tmp_path, "doc_annex")
    assert sameperson.sweep(repo, cfg) == 1


@pytest.mark.parametrize("kw", [
    {"first": "ნინო"},                                          # სხვა თანამშრომელი
    {"first": None},                                            # სახელი არ იკითხება
    {"reasons": ("unknown_type", "company_conflict")},          # სხვა მიზეზით გადასამოწმებელი
])
def test_not_touched(repo, cfg, tmp_path, kw):
    add_doc(repo, tmp_path, "doc_contract", pn=PN_A, status="proposed", type_id="contract", reasons=())
    confirm(repo, "doc_contract", PN_A, "E1")
    add_doc(repo, tmp_path, "doc_annex", **kw)
    assert sameperson.sweep(repo, cfg) == 0
    assert repo.mhr_get("doc_annex") is None


def test_unconfirmed_personal_number_is_not_a_source(repo, cfg, tmp_path):
    """სხვა სკანის პ/ნ MHR-ში ვერ მოიძებნა (employee_id არ არის) — მას არ ვენდობით."""
    add_doc(repo, tmp_path, "doc_contract", pn=PN_A, status="proposed", type_id="contract", reasons=())
    repo.mhr_enqueue("doc_contract", PN_A)
    add_doc(repo, tmp_path, "doc_annex")
    assert sameperson.sweep(repo, cfg) == 0


def test_disabled_mhr_does_nothing(repo, tmp_path):
    off = make_config({"paths": {"root": str(tmp_path)}})
    add_doc(repo, tmp_path, "doc_annex")
    assert sameperson.sweep(repo, off) == 0
