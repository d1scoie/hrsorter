"""ანკეტა „ინფორმაცია თანამშრომელზე“: ამოკითხვა, MHR-თან და პირადობასთან შედარება, ავტომატური ატვირთვა, დოკუმენტის
თარიღი. მონაცემები გამოგონილია."""
from __future__ import annotations

import json

from hrsorter.classifier import Classifier
from hrsorter.config import make_config
from hrsorter.empform import parse_employee_form
from hrsorter.models import Extraction, FieldValue, PageText
from hrsorter.pipeline import _typed_document_date
from tests.test_identity import id_text
from tests.test_packets import PN, add_scan, sweep, validation, world  # noqa: F401  (world — fixture)

START = "2026-11-05"


def form_text(start="05.11.2026", name="[?]", pn=PN, birth="01.01.1990", signed=True, fill="06.11.2026") -> str:
    return ("ინფორმაცია თანამშრომელზე\n\n"
            f"მუშაობის დაწყების თარიღი | [ხელნაწერი: {start}]\n"
            f"ოფიციალური სახელი, გვარი | [ხელნაწერი: {name}]\n"
            f"პირადი № | [ხელნაწერი: {pn}]\n"
            f"დაბადების თარიღი | [ხელნაწერი: {birth}]\n"
            "მისამართი | [ხელნაწერი: [?] 98]\n"
            "მობილური ტელ, სახლის ტელეფონი | [ხელნაწერი: 555-00-00-00]\n"
            "პირადი ელ-ფოსტა | [ხელნაწერი: nino@example.com]\n\n"
            "მე ვადასტურებ მოცემული, ჩემს მიერ ამ ანკეტაში შევსებული ინფორმაციის სიზუსტეს.\n"
            "თანამშრომლის სამსახურში აყვანის ფორმა არის საფუძველი თანამშრომლად აყვანის მოთხოვნისა.\n\n"
            + ("თანამშრომლის ხელმოწერა [ხელნაწერი: [?]]\n" if signed else "თანამშრომლის ხელმოწერა\n")
            + f"შევსების თარიღი [ხელნაწერი: {fill}]\n")


def hired(world, start=START):
    """MHR-ში თანამშრომელს მუშაობის დაწყების თარიღი უწერია."""
    (world.dir / "employees.json").write_text(json.dumps([{
        "id": "E1", "personal_number": PN, "first_name": "ნინო", "last_name": "ბერიძე", "status": "active",
        "contract_start": start, "employments": [{"start": start, "end": None, "active": True}]}], ensure_ascii=False), encoding="utf-8")


def verified_id(world):
    add_scan(world, [(id_text(), {})], doc_id="doc_id", job="job_id", type_id="id_card")
    sweep(world)
    assert world.repo.mhr_get("doc_id")["status"] == "uploaded"


def _uploads(w):
    path = w.dir / "uploads.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


# --- ამოკითხვა ------------------------------------------------------------------------------------------------
def test_handwritten_fields_are_read():
    f = parse_employee_form(form_text())
    assert (f.start_date, f.personal_number, f.birth_date, f.fill_date) == (START, PN, "1990-01-01", "2026-11-06")
    assert f.full_name is None and f.unread == ["full_name"] and f.signed


def test_readable_name_spaced_date_and_missing_signature():
    f = parse_employee_form(form_text(start="5. 11. 2026", name="ნინო ბერიძე", signed=False))
    assert f.start_date == START and f.full_name == "ნინო ბერიძე" and f.unread == [] and not f.signed


def test_form_type_is_recognised():
    c = Classifier(make_config()).classify_text(form_text())
    assert c.type_id == "employee_form" and c.confidence > 0.8
    assert make_config().doc_type("employee_form").employee_form is True


# --- MHR -------------------------------------------------------------------------------------------------------
def test_full_match_with_verified_id_is_uploaded_without_asking(world):
    hired(world)
    verified_id(world)
    add_scan(world, [(form_text(), {})], doc_id="doc_form", job="job_form", type_id="employee_form")
    sweep(world)
    item = world.repo.mhr_get("doc_form")
    assert item["status"] == "uploaded" and item["decided_by"] == "auto:employee_form"
    up = next(u for u in _uploads(world) if u["external_id"] == "doc_form")
    assert up["category"] == "employee_form" and up["signed_copy_of"] is None
    checks = {c["field"]: c for c in validation(world, "doc_form")["checks"]}
    assert checks["start_date"]["status"] == checks["birth_date"]["status"] == checks["employee_form"]["status"] == "match"


def test_without_verified_id_it_passes_but_waits_for_the_operator(world):
    hired(world)
    add_scan(world, [(form_text(), {})], doc_id="doc_form", job="job_form", type_id="employee_form")
    sweep(world)
    v = validation(world, "doc_form")
    assert world.repo.mhr_get("doc_form")["status"] == "ready" and _uploads(world) == []
    assert not any(c["field"] == "employee_form" for c in v["checks"])
    assert any(c["field"] == "identity" and "ჯერ არ დადასტურებულა" in c["note"] for c in v["checks"])


def test_other_start_date_stops_and_shows_both(world):
    hired(world, start="2026-10-01")
    verified_id(world)
    add_scan(world, [(form_text(), {})], doc_id="doc_form", job="job_form", type_id="employee_form")
    sweep(world)
    bad = next(c for c in validation(world, "doc_form")["checks"] if c["field"] == "start_date")
    assert world.repo.mhr_get("doc_form")["status"] == "failed"
    assert bad["status"] == "mismatch" and bad["document_value"] == START and bad["mhr_value"] == "2026-10-01"


def test_birth_date_must_match_the_verified_id(world):
    hired(world)
    verified_id(world)
    add_scan(world, [(form_text(birth="02.02.1991"), {})], doc_id="doc_form", job="job_form", type_id="employee_form")
    sweep(world)
    bad = next(c for c in validation(world, "doc_form")["checks"] if c["field"] == "birth_date")
    assert world.repo.mhr_get("doc_form")["status"] == "failed" and bad["status"] == "mismatch" and bad["mhr_value"] == "1990-01-01"


def test_unsigned_form_stops(world):
    hired(world)
    verified_id(world)
    add_scan(world, [(form_text(signed=False), {})], doc_id="doc_form", job="job_form", type_id="employee_form")
    sweep(world)
    assert world.repo.mhr_get("doc_form")["status"] == "failed"
    assert any(c["field"] == "signature" and c["blocking"] for c in validation(world, "doc_form")["checks"])


def test_misread_handwritten_name_is_only_a_note(world):
    hired(world)
    verified_id(world)
    add_scan(world, [(form_text(name="ნონა ბერაძე"), {})], doc_id="doc_form", job="job_form", type_id="employee_form")
    sweep(world)
    name = next(c for c in validation(world, "doc_form")["checks"] if c["field"] == "full_name")
    assert world.repo.mhr_get("doc_form")["status"] == "uploaded" and name["status"] == "info" and not name["blocking"]


def test_second_form_is_not_uploaded_twice(world):
    hired(world)
    verified_id(world)
    add_scan(world, [(form_text(), {})], doc_id="doc_form", job="job_form", type_id="employee_form")
    sweep(world)
    add_scan(world, [(form_text(), {})], doc_id="doc_form2", job="job_form2", type_id="employee_form")
    sweep(world)
    assert world.repo.mhr_get("doc_form2")["status"] == "failed"
    assert [u["external_id"] for u in _uploads(world) if u["category"] == "employee_form"] == ["doc_form"]


# --- დოკუმენტის თარიღი: არა დაბადების თარიღი ----------------------------------------------------------------------
def _page(text: str) -> PageText:
    return PageText(page_index=0, text=text, text_source="ocr:online", ocr_quality=0.9, is_blank=False, has_marks=True,
                    marks_only=False, ink_ratio=0.05)


def _extraction_with_birth_date() -> Extraction:
    return Extraction(fields={"document_date": FieldValue(value="1990-01-01", raw="01.01.1990", source="document",
                                                          page_index=0, confidence=0.7, candidates=["2026-11-05"])})


def test_form_document_date_is_the_fill_date():
    cfg, ex = make_config(), _extraction_with_birth_date()
    _typed_document_date(ex, cfg.doc_type("employee_form"), [_page(form_text())])
    assert ex.fields["document_date"].value == "2026-11-06" and "1990-01-01" in ex.fields["document_date"].candidates


def test_id_card_document_date_is_the_issue_date_and_other_types_are_untouched():
    cfg, ex = make_config(), _extraction_with_birth_date()
    _typed_document_date(ex, cfg.doc_type("id_card"), [_page(id_text())])
    assert ex.fields["document_date"].value == "2026-10-26"
    ex = _extraction_with_birth_date()
    _typed_document_date(ex, cfg.doc_type("contract"), [_page(form_text())])
    assert ex.fields["document_date"].value == "1990-01-01"


def test_unreadable_date_is_unknown_rather_than_the_birth_date():
    cfg, ex = make_config(), _extraction_with_birth_date()
    _typed_document_date(ex, cfg.doc_type("employee_form"), [_page(form_text(start="[?]", fill="[?]"))])
    assert ex.fields["document_date"].value is None
