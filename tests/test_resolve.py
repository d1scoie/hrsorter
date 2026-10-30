from __future__ import annotations

from hrsorter.config import make_config
from hrsorter.models import EmployeeLookup, EmployeeRecord, Extraction, FieldValue
from hrsorter.resolve import resolve_company, resolve_year

CFG = make_config({
    "companies": [{"id": "acme", "name": "შპს „მაგალითი\"", "folder": "მაგალითი"},
                  {"id": "beta", "name": "შპს „ბეტა\"", "folder": "ბეტა"}],
    "profiles": [{"id": "batch", "inbox": "Inbox/Batch", "mode": "batch_pdf",
                  "default_company": {"id": "beta", "name": "შპს „ბეტა\"", "folder": "ბეტა"}},
                 {"id": "single", "inbox": "Inbox/Single", "mode": "single_document_pdf"}],
})
NOBODY = EmployeeLookup("not_checked")


def ext(companies=None, unrecognized=None, **fields) -> Extraction:
    return Extraction(fields=fields, companies_found=companies or [], unrecognized_companies=unrecognized or [])


def test_single_document_company():
    r = resolve_company(ext([{"company_id": "acme", "role": "other", "page_index": 0, "evidence": "შპს მაგალითი"}]),
                        CFG, "batch", NOBODY)
    assert (r.company_id, r.folder, r.source, r.conflict) == ("acme", "მაგალითი", "document", False)


def test_employer_role_preferred_over_other_company():
    r = resolve_company(ext([{"company_id": "beta", "role": "counterparty"},
                             {"company_id": "acme", "role": "employer"}]), CFG, "batch", NOBODY)
    assert r.company_id == "acme" and not r.conflict


def test_two_companies_without_employer_is_conflict_not_default():
    r = resolve_company(ext([{"company_id": "acme", "role": "other"}, {"company_id": "beta", "role": "other"}]),
                        CFG, "batch", NOBODY)
    assert r.conflict and r.company_id is None and r.source == "document"


def test_two_employers_conflict():
    r = resolve_company(ext([{"company_id": "acme", "role": "employer"}, {"company_id": "beta", "role": "employer"}]),
                        CFG, "batch", NOBODY)
    assert r.conflict


def test_unrecognized_only():
    r = resolve_company(ext(unrecognized=["შპს უცნობი"]), CFG, "batch", NOBODY)
    assert r.unrecognized and r.company_id is None


def test_nothing_found_uses_profile_default():
    r = resolve_company(ext(), CFG, "batch", NOBODY)
    assert (r.company_id, r.source) == ("beta", "default")
    r2 = resolve_company(ext(), CFG, "single", NOBODY)
    assert (r2.company_id, r2.source) == ("default", "default")


def test_api_company_does_not_override_document():
    emp = EmployeeLookup("verified", record=EmployeeRecord("E1", company_id="beta"), matched_by="personal_number")
    r = resolve_company(ext([{"company_id": "acme", "role": "employer"}]), CFG, "batch", emp)
    assert r.company_id == "acme" and r.source == "document"


def test_api_company_not_used_when_document_has_none():
    emp = EmployeeLookup("verified", record=EmployeeRecord("E1", company_id="acme"), matched_by="personal_number")
    r = resolve_company(ext(), CFG, "single", emp)
    assert r.source == "default" and r.company_id == "default"


def test_year_from_full_date():
    d, y = resolve_year(ext(document_date=FieldValue("2023-11-02", "02.11.2023", "document", 1, 0.9)))
    assert d.value == "2023-11-02" and d.raw == "02.11.2023"
    assert y.value == 2023 and y.source == "derived" and y.page_index == 1


def test_year_only_does_not_invent_date():
    d, y = resolve_year(ext(document_date=FieldValue(None, "2022 წ.", "document", 0, 0.7, candidates=[{"year": 2022}])))
    assert d.value is None and d.raw == "2022 წ."
    assert y.value == 2022


def test_no_date_no_year_and_never_current_year():
    d, y = resolve_year(ext())
    assert d.value is None and y.value is None
    d, y = resolve_year(ext(document_date=FieldValue(None, candidates=[{"year": 2020}, {"year": 2021}])))
    assert y.value is None


def test_known_company_without_role_plus_unrecognized_org_is_conflict():
    from hrsorter.config import make_config
    from hrsorter.models import EmployeeLookup, Extraction
    from hrsorter.resolve import resolve_company
    cfg = make_config({"companies": [{"id": "acme", "name": "შპს მაგალითი", "folder": "მაგალითი"}]})
    ex = Extraction(companies_found=[{"company_id": "acme", "role": "unknown", "page_index": 0, "evidence": "მაგალითი"}],
                    unrecognized_companies=['შპს "გამა"'])
    res = resolve_company(ex, cfg, "batch", EmployeeLookup(status="not_checked"))
    assert res.conflict and res.company_id is None
