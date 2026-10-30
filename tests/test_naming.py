from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from hrsorter.config import make_config
from hrsorter.models import (Classification, CompanyResolution, Decision, DocumentOutcome, DuplicateInfo,
                             EmployeeLookup, EmployeeRecord, Extraction, FieldValue, Segment, SegmentKind)
from hrsorter.naming import build_output_path, sanitize_component, short_id, trusted_personal_number

PN = "01001000001"


def make_cfg(root: Path | str = "C:/HRScan", **over):
    base = {"paths": {"root": str(root)},
            "companies": [{"id": "acme", "name": "შპს „მაგალითი\"", "folder": "მაგალითი"}]}
    for k, v in over.items():
        base[k] = v
    return make_config(base)


def fv(value, raw=None, conf=0.95, page=0, source="document", **kw) -> FieldValue:
    return FieldValue(value=value, raw=raw if raw is not None else (None if value is None else str(value)),
                      source=source if value is not None else None, page_index=page, confidence=conf, **kw)


def make_outcome(*, outcome: str = "proposed", reasons: list[str] | None = None, type_id: str = "fine",
                 company_id: str | None = "acme", company_source: str = "document", year: int | None = 2024,
                 date: str | None = "2024-03-15", first: str | None = "ნინო", last: str | None = "ბერიძე",
                 pn: str | None = PN, pn_conf: float = 0.95, pn_handwritten: bool = False,
                 doc_number: str | None = "15", employee: EmployeeLookup | None = None,
                 pages: list[int] | None = None, source_path: str = "", job_id: str = "job_" + "1" * 32,
                 document_id: str | None = None, source_id: str = "src_" + "2" * 32,
                 source_sha256: str = "0" * 64) -> DocumentOutcome:
    pages = pages if pages is not None else [0]
    fields = {"first_name": fv(first), "last_name": fv(last),
              "personal_number": fv(pn, conf=pn_conf, handwritten=pn_handwritten),
              "document_number": fv(doc_number), "document_date": fv(date, raw="15.03.2024" if date else None)}
    ext = Extraction(fields=fields, employee_personal_numbers=[pn] if pn else [],
                     signature_status="present")
    comp = CompanyResolution(company_id, "შპს „მაგალითი\"" if company_id else None,
                             "მაგალითი" if company_id else None, company_source)
    seg = Segment(start=pages[0], end=pages[-1], kind=SegmentKind.DOCUMENT, boundary_confidence=0.95,
                  evidence=["title"])
    return DocumentOutcome(
        document_id=document_id or f"doc_{uuid.uuid4().hex}", job_id=job_id, source_id=source_id,
        source_sha256=source_sha256, source_path=source_path, profile_id="batch", mode="batch_pdf",
        segment=seg, pages=pages,
        classification=Classification(type_id, 0.9, 0.5, evidence=["title: ბრძანება"]),
        extraction=ext, employee=employee or EmployeeLookup("not_checked", detail="api_disabled"),
        company=comp, document_date=fv(date, raw="15.03.2024" if date else None),
        year=fv(year, source="derived"), duplicate=DuplicateInfo(),
        decision=Decision(outcome, review_reasons=reasons or [], flags=[]),
        signature_status="present", content_fingerprint="c" * 64, text_fingerprint="t" * 64,
        provider="text_layer", model_version="", pipeline_version="0.1.0", config_version="abc", attempt=1)


# --- sanitize -----------------------------------------------------------------------

@pytest.mark.parametrize("raw", ['ა<b>c:d"e/f\\g|h?i*j', "x\x00y\x1fz", "CON", "con.txt", "COM1", "LPT9.pdf",
                                 "name. . .", "  spaced   name  "])
def test_sanitize_component_windows_safe(raw):
    out = sanitize_component(raw, 40)
    assert not any(ch in out for ch in '<>:"/\\|?*')
    assert all(ord(ch) >= 32 for ch in out)
    assert " " not in out
    assert not out.endswith((".", " "))
    assert out.split(".")[0].upper() not in {"CON", "PRN", "AUX", "NUL", "COM1", "LPT9"}


def test_sanitize_nfc_and_length():
    decomposed = "e\u0301" * 10
    out = sanitize_component(decomposed, 5)
    assert out == "\u00e9" * 5


def test_short_id():
    assert short_id("doc_ab12cd34ffffffff") == "ab12cd34"


# --- path layout ----------------------------------------------------------------------

def test_sorted_layout_and_template():
    cfg = make_cfg()
    o = make_outcome(outcome="auto_saved", document_id="doc_ab12cd34" + "0" * 24)
    p = build_output_path(Path("C:/HRScan/Sorted"), o, cfg)
    assert p.parent == Path("C:/HRScan/Sorted/მაგალითი/ჯარიმა/2024")
    assert p.name == f"2024-03-15_ჯარიმა_ნინო-ბერიძე_{PN}_15_ab12cd34.pdf"


def test_unknown_markers_and_missing_doc_number():
    cfg = make_cfg()
    o = make_outcome(year=None, date=None, first=None, last=None, pn=None, doc_number=None,
                     document_id="doc_ab12cd34" + "0" * 24)
    p = build_output_path(Path("C:/HRScan/Proposed"), o, cfg)
    assert p.parent.name == "წელი-უცნობია"
    assert p.name == "თარიღი-უცნობია_ჯარიმა_სახელი-უცნობია_პნ-უცნობია_ab12cd34.pdf"


def test_needs_review_uses_first_reason():
    cfg = make_cfg()
    o = make_outcome(outcome="needs_review", reasons=["company_conflict", "possible_duplicate"], company_id=None)
    p = build_output_path(Path("C:/HRScan/NeedsReview"), o, cfg)
    assert p.parent == Path("C:/HRScan/NeedsReview/company_conflict")


def test_folders_come_from_config_not_document_text():
    cfg = make_cfg()
    o = make_outcome(company_id="acme")
    o.company.folder = "..\\..\\evil"          # document-derived junk must not be used
    o.company.company_name = "C:/Windows"
    p = build_output_path(Path("C:/HRScan/Proposed"), o, cfg)
    assert p.parent == Path("C:/HRScan/Proposed/მაგალითი/ჯარიმა/2024")


def test_document_text_in_name_is_sanitized():
    cfg = make_cfg()
    o = make_outcome(first="..\\..\\CON", last='a/b:c*?', doc_number="12/3")
    p = build_output_path(Path("C:/HRScan/Proposed"), o, cfg)
    assert p.parent == Path("C:/HRScan/Proposed/მაგალითი/ჯარიმა/2024")
    assert not any(ch in p.name for ch in '<>:"/\\|?*')


# --- personal number trust ----------------------------------------------------------

def test_pn_untrusted_low_confidence_or_handwritten():
    assert trusted_personal_number(make_outcome(pn_conf=0.5)) is None
    assert trusted_personal_number(make_outcome(pn_handwritten=True)) is None
    assert trusted_personal_number(make_outcome()) == PN


def test_pn_name_only_match_not_used():
    rec = EmployeeRecord("E001", "ნინო", "ბერიძე", "01001000009")
    o = make_outcome(pn=None, employee=EmployeeLookup("verified", record=rec, matched_by="name"))
    assert trusted_personal_number(o) is None


def test_pn_verified_by_api_used_and_leading_zeros_kept():
    rec = EmployeeRecord("E004", "მარიამ", "ლომიძე", "00001000004")
    o = make_outcome(pn="00001000004", pn_conf=0.5,
                     employee=EmployeeLookup("verified", record=rec, matched_by="personal_number"))
    assert trusted_personal_number(o) == "00001000004"
    assert "_00001000004_" in build_output_path(Path("C:/HRScan/Proposed"), o, make_cfg()).name


# --- lengths & collisions -------------------------------------------------------------

def test_long_name_truncated_short_id_kept():
    cfg = make_cfg(naming={"max_filename_len": 80, "max_component_len": 200})
    o = make_outcome(first="ა" * 150, document_id="doc_ab12cd34" + "0" * 24)
    p = build_output_path(Path("C:/HRScan/Proposed"), o, cfg)
    assert len(p.name) <= 80
    assert p.stem.endswith("ab12cd34")


def test_total_path_length_capped():
    cfg = make_cfg(naming={"max_path_len": 120})
    o = make_outcome(first="ბ" * 40, last="გ" * 40, document_id="doc_ab12cd34" + "0" * 24)
    p = build_output_path(Path("C:/HRScan/Proposed"), o, cfg)
    assert len(str(p)) <= 120
    assert "ab12cd34" in p.name


def test_collision_appends_suffix(tmp_path):
    cfg = make_cfg(tmp_path)
    o = make_outcome(document_id="doc_ab12cd34" + "0" * 24)
    first = build_output_path(tmp_path, o, cfg)
    first.parent.mkdir(parents=True)
    first.write_bytes(b"other")
    second = build_output_path(tmp_path, o, cfg)
    assert second.name == first.stem + "-2.pdf"
    second.write_bytes(b"other2")
    assert build_output_path(tmp_path, o, cfg).name == first.stem + "-3.pdf"
