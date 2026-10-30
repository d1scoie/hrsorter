from __future__ import annotations

import base64
import copy

import httpx
import pytest

from hrsorter import secrets
from hrsorter.config import DEFAULTS, make_config
from hrsorter.db import Repository
from hrsorter.employee_api import (CachedDirectory, DisabledDirectory, HttpDirectory, MockDirectory,
                                   build_directory, lookup_employee)
from hrsorter.models import Extraction, FieldValue

MOCK = "config/mock_employees.example.json"


def ext(pn=None, first=None, last=None, emp_id=None, pns=None) -> Extraction:
    f = {}
    if pn:
        f["personal_number"] = FieldValue(pn, pn, "document", 0, 0.9)
    if first:
        f["first_name"] = FieldValue(first, first, "document", 0, 0.9)
    if last:
        f["last_name"] = FieldValue(last, last, "document", 0, 0.9)
    if emp_id:
        f["employee_id"] = FieldValue(emp_id, emp_id, "document", 0, 0.9)
    return Extraction(fields=f, employee_personal_numbers=pns if pns is not None else ([pn] if pn else []))


@pytest.fixture
def repo():
    r = Repository(":memory:")
    yield r
    r.close()


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch):
    store = {"hrsorter/employee_api": "s3cret-token"}
    monkeypatch.setattr(secrets, "get_secret", lambda name: store.get(name))
    return store


# --- mock directory + lookup rules ---------------------------------------------------------

def test_mock_pn_verified_and_leading_zeros():
    d = MockDirectory(MOCK)
    r = lookup_employee(d, ext(pn="00001000004"))
    assert r.status == "verified" and r.matched_by == "personal_number"
    assert r.record.employee_id == "E004" and r.record.personal_number == "00001000004"
    assert r.record.status == "dismissed"


def test_name_two_candidates_is_ambiguous_no_binding():
    r = lookup_employee(MockDirectory(MOCK), ext(first="გიორგი", last="კაპანაძე"))
    assert r.status == "ambiguous" and r.record is None
    assert {c.employee_id for c in r.candidates} == {"E002", "E003"}


def test_name_single_match_lower_trust():
    r = lookup_employee(MockDirectory(MOCK), ext(first="ნინო", last="ბერიძე"))
    assert r.status == "verified" and r.matched_by == "name" and r.detail == "name_only"


def test_multiple_employee_pns_ambiguous():
    r = lookup_employee(MockDirectory(MOCK), ext(pns=["01001000001", "01001000002"]))
    assert r.status == "ambiguous" and r.detail == "multiple_employees"


def test_ocr_suspect_pn_not_replaced_by_name_match():
    # პ/ნ-ში ერთი ციფრი შეცდომით: სახელით ნაპოვნი პირი მხოლოდ კანდიდატია
    r = lookup_employee(MockDirectory(MOCK), ext(pn="01001000007", first="ნინო", last="ბერიძე"))
    assert r.status == "not_found" and r.record is None
    assert [c.employee_id for c in r.candidates] == ["E001"]


def test_employee_id_lookup():
    r = lookup_employee(MockDirectory(MOCK), ext(emp_id="E002"))
    assert r.status == "verified" and r.matched_by == "employee_id"


def test_no_identifiers_and_disabled():
    assert lookup_employee(MockDirectory(MOCK), ext()).status == "not_checked"
    assert lookup_employee(DisabledDirectory(), ext(pn="01001000001")).status == "not_checked"
    cfg = make_config({"employee_api": {"enabled": False}})
    assert isinstance(build_directory(cfg, None), DisabledDirectory)


def test_missing_mock_file_is_unavailable_not_exception(tmp_path):
    r = MockDirectory(tmp_path / "nope.json").find_by_personal_number("01001000001")
    assert r.status == "api_unavailable"


def test_build_directory_mock_with_cache(repo):
    cfg = make_config({"employee_api": {"enabled": True, "mode": "mock", "mock_file": MOCK}})
    d = build_directory(cfg, repo)
    assert isinstance(d, CachedDirectory)
    assert d.find_by_personal_number("01001000001").status == "verified"
    assert repo.cache_get("pn:01001000001") is not None
    assert {c["id"] for c in d.companies()} == {"acme", "beta"}


# --- HTTP directory -------------------------------------------------------------------------

def api_cfg(**over) -> dict:
    c = copy.deepcopy(DEFAULTS["employee_api"])
    c.update(enabled=True, mode="http", base_url="https://hr.example.test/api", max_retries=2,
             retry_backoff_seconds=0.01)
    c["endpoints"]["by_personal_number"] = "/employees?personal_number={pn}"
    for k, v in over.items():
        c[k] = v
    return c


def http_dir(handler, sleeps=None, **over) -> HttpDirectory:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HttpDirectory(api_cfg(**over), client=client, sleep=(sleeps.append if sleeps is not None else lambda s: None))


NINO = {"emp": {"code": "E001"}, "name": {"first": "ნინო", "last": "ბერიძე"}, "pid": "01001000001",
        "org": "acme", "state": "active", "jobs": [{"company_id": "beta", "from": "2019-01-01"}]}
FIELD_MAP = {"employee_id": "emp.code", "first_name": "name.first", "last_name": "name.last",
             "personal_number": "pid", "company_id": "org", "branch": "", "status": "state", "history": "jobs"}


def test_http_bearer_and_field_map():
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"] = req.headers.get("Authorization")
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"items": [NINO]})

    d = http_dir(handler, auth={"method": "bearer", "secret_name": "hrsorter/employee_api"}, field_map=FIELD_MAP)
    r = d.find_by_personal_number("01001000001")
    assert seen["auth"] == "Bearer s3cret-token"
    assert seen["url"] == "https://hr.example.test/api/employees?personal_number=01001000001"
    assert r.status == "verified"
    rec = r.record
    assert (rec.employee_id, rec.first_name, rec.last_name, rec.company_id, rec.status) == \
        ("E001", "ნინო", "ბერიძე", "acme", "active")
    assert rec.history == NINO["jobs"]


def test_http_history_only_when_mapped():
    d = http_dir(lambda req: httpx.Response(200, json=[NINO]), field_map={**FIELD_MAP, "history": ""})
    assert d.find_by_personal_number("01001000001").record.history == []


def test_http_api_key_header_and_basic():
    seen = {}

    def handler(req):
        seen.update(req.headers)
        return httpx.Response(200, json=[])

    http_dir(handler, auth={"method": "api_key_header", "secret_name": "hrsorter/employee_api",
                            "header": "X-Api-Key"}).find_by_personal_number("1")
    assert seen["x-api-key"] == "s3cret-token"
    seen.clear()
    http_dir(handler, auth={"method": "basic", "secret_name": "hrsorter/employee_api"}).find_by_personal_number("1")
    assert seen["authorization"] == "Basic " + base64.b64encode(b"s3cret-token:").decode()


def test_http_missing_secret_is_unavailable(fake_keyring):
    fake_keyring.clear()
    d = http_dir(lambda req: httpx.Response(200, json=[]), auth={"method": "bearer", "secret_name": "x"})
    assert d.find_by_personal_number("01001000001").status == "api_unavailable"


def test_http_unconfigured_endpoint_not_checked():
    d = http_dir(lambda req: pytest.fail("must not call"))
    assert d.search_by_name("ნინო", "ბერიძე").status == "not_checked"
    assert d.find_by_id("E1").status == "not_checked"


def test_http_page_pagination():
    calls = []
    people = [{**NINO, "pid": f"0100100{i:04d}", "emp": {"code": f"E{i}"}} for i in range(5)]

    def handler(req):
        page = int(req.url.params["page"])
        calls.append(page)
        return httpx.Response(200, json={"items": people[(page - 1) * 2: page * 2]})

    d = http_dir(handler, field_map=FIELD_MAP,
                 pagination={"style": "page", "page_param": "page", "size_param": "page_size", "page_size": 2,
                             "items_key": "items"})
    r = d.find_by_personal_number("01001000003")
    assert calls == [1, 2, 3]
    assert r.status == "verified" and r.record.employee_id == "E3"


def test_http_next_url_pagination():
    def handler(req):
        if "cursor" in str(req.url):
            return httpx.Response(200, json={"data": {"rows": [NINO]}, "links": {"next": None}})
        return httpx.Response(200, json={"data": {"rows": []}, "links": {"next": "/api/employees?cursor=2"}})

    d = http_dir(handler, field_map=FIELD_MAP,
                 pagination={"style": "next_url", "items_key": "data.rows", "next_key": "links.next"})
    assert d.find_by_personal_number("01001000001").status == "verified"


def test_http_returned_near_candidates_kept_as_not_found():
    near = {**NINO, "pid": "01001000007"}
    d = http_dir(lambda req: httpx.Response(200, json=[near]), field_map=FIELD_MAP)
    r = d.find_by_personal_number("01001000001")
    assert r.status == "not_found" and [c.personal_number for c in r.candidates] == ["01001000007"]


def test_http_retries_then_success():
    sleeps, n = [], {"i": 0}

    def handler(req):
        n["i"] += 1
        if n["i"] == 1:
            raise httpx.ConnectError("down", request=req)
        if n["i"] == 2:
            return httpx.Response(429)
        return httpx.Response(200, json=[NINO])

    d = http_dir(handler, sleeps, field_map=FIELD_MAP)
    assert d.find_by_personal_number("01001000001").status == "verified"
    assert n["i"] == 3 and len(sleeps) == 2 and sleeps[1] > sleeps[0]


def test_http_5xx_bounded_then_cache_fallback_then_unavailable(repo):
    state = {"down": False, "calls": 0}

    def handler(req):
        state["calls"] += 1
        return httpx.Response(503) if state["down"] else httpx.Response(200, json=[NINO])

    d = CachedDirectory(http_dir(handler, field_map=FIELD_MAP), repo)
    ok = d.find_by_personal_number("01001000001")
    assert ok.status == "verified" and not ok.from_cache
    state.update(down=True, calls=0)
    cached = d.find_by_personal_number("01001000001")
    assert state["calls"] == 3  # 1 + max_retries
    assert cached.status == "verified" and cached.from_cache and cached.synced_at
    assert cached.record.employee_id == "E001"
    miss = d.find_by_personal_number("01001000002")
    assert miss.status == "api_unavailable" and not miss.from_cache


def test_http_secret_never_in_lookup_detail(fake_keyring):
    d = http_dir(lambda req: httpx.Response(401), auth={"method": "bearer", "secret_name": "hrsorter/employee_api"})
    r = d.find_by_personal_number("01001000001")
    assert r.status == "api_unavailable" and "s3cret" not in (r.detail or "")
