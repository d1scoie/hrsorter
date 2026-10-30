from __future__ import annotations

import copy

from hrsorter.config import make_config
from hrsorter.decision import decide
from hrsorter.models import (Classification, CompanyResolution, DuplicateInfo, EmployeeLookup, EmployeeRecord,
                             Extraction, FieldValue, PageText, Segment, SegmentKind)

PN = "01001000001"


def types_with_auto(enabled: bool) -> dict:
    cfg = make_config()
    data = copy.deepcopy(cfg.types_data)
    for t in data["types"]:
        t["auto_save_enabled"] = enabled
    return data


CFG_LIVE = make_config({"processing": {"trial_mode": False}}, types_with_auto(True))


def good_inputs(**over) -> dict:
    ext = Extraction(fields={"personal_number": FieldValue(PN, PN, "document", 0, 0.95),
                             "document_date": FieldValue("2024-03-15", "15.03.2024", "document", 0, 0.9),
                             "fine_amount": FieldValue(100, "100 ლარი", "document", 0, 0.9)},
                     employee_personal_numbers=[PN], signature_status="present")
    d = dict(segment=Segment(0, 1, SegmentKind.DOCUMENT, 0.95),
             classification=Classification("fine", 0.9, 0.5),
             doc_type=CFG_LIVE.doc_type("fine"),
             company=CompanyResolution("acme", "შპს „მაგალითი\"", "მაგალითი", "document"),
             extraction=ext,
             employee=EmployeeLookup("verified", record=EmployeeRecord("E001", personal_number=PN, company_id="acme"),
                                     matched_by="personal_number"),
             duplicate=DuplicateInfo(), mode_mismatch=False, cfg=CFG_LIVE)
    d.update(over)
    return d


def test_all_good_auto_saved():
    d = decide(**good_inputs())
    assert d.outcome == "auto_saved" and d.review_reasons == [] and d.flags == []


def test_trial_mode_proposes_without_review_reason():
    cfg = make_config({"processing": {"trial_mode": True}}, types_with_auto(True))
    d = decide(**good_inputs(cfg=cfg, doc_type=cfg.doc_type("fine")))
    assert d.outcome == "proposed" and d.review_reasons == []
    assert any("trial_mode" in e for e in d.explanation)


def test_type_not_auto_enabled_proposes():
    cfg = make_config({"processing": {"trial_mode": False}}, types_with_auto(False))
    d = decide(**good_inputs(cfg=cfg, doc_type=cfg.doc_type("fine")))
    assert d.outcome == "proposed" and d.review_reasons == []
    assert any("auto_save_enabled" in e for e in d.explanation)


def reasons(**over) -> list[str]:
    d = decide(**good_inputs(**over))
    assert d.outcome == "needs_review"
    return d.review_reasons


def test_blocking_reasons():
    assert reasons(segment=Segment(0, 1, SegmentKind.UNCERTAIN, 0.95)) == ["uncertain_boundary"]
    assert reasons(segment=Segment(0, 1, SegmentKind.DOCUMENT, 0.5)) == ["uncertain_boundary"]
    assert reasons(classification=Classification("unknown", 0.1, 0.0), doc_type=CFG_LIVE.doc_type("unknown")) \
        == ["unknown_type"]
    assert reasons(classification=Classification("fine", 0.6, 0.5)) == ["low_type_confidence"]
    assert reasons(classification=Classification("fine", 0.95, 0.1)) == ["low_type_confidence"]
    assert reasons(company=CompanyResolution(None, None, None, "document", conflict=True)) == ["company_conflict"]
    assert reasons(company=CompanyResolution(None, None, None, "document", unrecognized=True)) \
        == ["company_unrecognized"]
    assert reasons(duplicate=DuplicateInfo("possible", [{"document_id": "doc_x", "score": 0.9, "reasons": []}])) \
        == ["possible_duplicate"]
    assert reasons(duplicate=DuplicateInfo("exact", [{"document_id": "doc_x", "score": 1.0, "reasons": []}])) \
        == ["possible_duplicate"]
    assert reasons(mode_mismatch=True) == ["mode_mismatch"]


def test_multiple_employees_blocks():
    inp = good_inputs()
    inp["extraction"].employee_personal_numbers = [PN, "01001000002"]
    inp["employee"] = EmployeeLookup("ambiguous", detail="multiple_employees")
    d = decide(**inp)
    assert d.review_reasons == ["multiple_employees"]


def test_ocr_unreadable_only_when_all_pages_low():
    low = PageText(0, "x", "ocr:tesseract", 0.1, False, False, False, 0.05)
    ok = PageText(1, "ტექსტი", "ocr:tesseract", 0.8, False, False, False, 0.05)
    blank = PageText(2, "", "none", 0.0, True, False, False, 0.0)
    assert reasons(pages=[low, blank]) == ["ocr_unreadable"]
    d = decide(**good_inputs(pages=[low, ok, blank]))
    assert d.outcome == "auto_saved" and "low_ocr_quality" in d.flags


def test_non_blocking_flags_do_not_block_type_sort():
    ext = Extraction(fields={}, employee_personal_numbers=[], signature_status="not_detected")
    d = decide(**good_inputs(extraction=ext, employee=EmployeeLookup("api_unavailable"),
                             company=CompanyResolution("default", "ნ", "ნ", "default")))
    assert d.outcome == "auto_saved" and d.review_reasons == []
    for f in ("year_unknown", "date_unknown", "signature_not_detected", "employee_api_unavailable",
              "employee_unverified", "personal_number_missing", "company_default", "amount_missing"):
        assert f in d.flags


def test_signature_uncertain_and_handwritten_pn_flags():
    inp = good_inputs()
    inp["extraction"].signature_status = "uncertain"
    inp["extraction"].fields["personal_number"].handwritten = True
    d = decide(**inp)
    assert d.outcome == "auto_saved"
    assert {"signature_uncertain", "personal_number_handwritten"} <= set(d.flags)


def test_name_candidates_and_api_company_differs():
    cands = EmployeeLookup("ambiguous", candidates=[EmployeeRecord("E2"), EmployeeRecord("E3")], matched_by="name")
    d = decide(**good_inputs(employee=cands))
    assert d.outcome == "auto_saved" and {"employee_name_candidates", "employee_unverified"} <= set(d.flags)
    other = EmployeeLookup("verified", record=EmployeeRecord("E3", company_id="beta"), matched_by="personal_number")
    d = decide(**good_inputs(employee=other))
    assert d.outcome == "auto_saved" and "api_company_differs" in d.flags


def test_high_model_self_confidence_does_not_override_rules():
    # კლასიფიკატორის მაღალი confidence ვერ ფარავს საზღვრის გაურკვევლობას
    d = decide(**good_inputs(classification=Classification("fine", 0.99, 0.9),
                             segment=Segment(0, 1, SegmentKind.DOCUMENT, 0.6)))
    assert d.outcome == "needs_review"
