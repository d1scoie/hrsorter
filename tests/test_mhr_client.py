"""MHR კლიენტის ტესტები — მხოლოდ ოფლაინ: MockMhr (JSON საქაღალდე) და HttpMhr `httpx.MockTransport`-ით
(OpenAPI-ის მაგალითების ასლი, docs/api/hr-sorter-openapi.yaml). ქსელი არასდროს გამოიყენება."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from hrsorter import secrets
from hrsorter.config import DEFAULTS, make_config
from hrsorter.mhr_client import MAX_UPLOAD_BYTES, HttpMhr, MhrUploadUncertain, MockMhr, build_mhr_client
from hrsorter.mhr_models import MhrDocRef, MhrError, MhrUnavailable

DOCX_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
ALFA = {"id": "1", "name": "შპს ალფა 2009", "tax_id": "200000001"}
EMP = {"id": "4521", "personal_number": "01001000001", "first_name": "ნინო", "last_name": "ბერიძე",
       "company": ALFA, "branch": "ვაზისუბანი", "position": "ბუღალტერი",
       "salary": {"amount": "2500.00", "currency": "GEL"}, "contract_start": "2024-06-04",
       "contract_end": "2026-06-04", "status": "active", "dismissed_at": None,
       "employments": [
           {"company": ALFA, "branch": "ვაზისუბანი", "position": "ბუღალტერი",
            "salary": {"amount": "2500.00", "currency": "GEL"}, "start": "2024-06-04", "end": "2026-06-04", "active": True},
           {"company": ALFA, "branch": "ვაზისუბანი", "position": "ბუღალტრის ასისტენტი",
            "salary": {"amount": "1800.00", "currency": "GEL"}, "start": "2022-06-04", "end": "2024-06-03", "active": False}]}
DOC_UPLOADED = {"id": "9310", "employee_id": "4521", "category": "contract", "title": "შრომითი ხელშეკრულება (ხელმოწერილი)",
                "document_number": "0783", "document_date": "2024-06-04", "created_at": "2026-10-28T13:40:00Z",
                "filename": "3f1c0e5b9d.pdf", "content_type": "application/pdf", "sha256": "9b" * 32, "size": 183244,
                "kind": "uploaded", "signed_at": None, "signed_copy_of": "9001", "external_id": "doc_5f2a91"}
DOC_GENERATED = {"id": "9001", "employee_id": "4521", "category": "contract", "title": "ხელშეკრულება",
                 "document_number": None, "document_date": "2024-06-04", "created_at": "2024-06-04T09:12:00Z",
                 "filename": "a8d2c1f0e4.docx", "content_type": DOCX_CT, "sha256": "1e" * 32, "size": 48211,
                 "kind": "generated", "signed_at": "2026-10-28T13:40:00Z", "signed_copy_of": None, "external_id": None}
PDF = b"%PDF-1.4 signed scan"


def meta(pdf: bytes = PDF, **over) -> dict:
    m = {"external_id": "doc_5f2a91", "sha256": hashlib.sha256(pdf).hexdigest()}
    m.update(over)
    return m


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch):
    store = {"hrsorter/mhr": "s3cret-token"}
    monkeypatch.setattr(secrets, "get_secret", lambda name: store.get(name))
    return store


@pytest.fixture
def mock_dir(tmp_path) -> Path:
    d = tmp_path / "mhr"
    (d / "files").mkdir(parents=True)
    (d / "employees.json").write_text(json.dumps([EMP, {"id": "4600", "personal_number": "01001000099"}],
                                                 ensure_ascii=False), encoding="utf-8")
    gen = {**DOC_GENERATED, "signed_at": None, "filename": "d1.docx"}
    (d / "documents.json").write_text(json.dumps([
        gen, {"id": "9002", "employee_id": "4521", "category": "hire_order", "title": "ბრძანება",
              "created_at": "2024-02-01", "filename": "d2.pdf", "kind": "generated"},
    ], ensure_ascii=False), encoding="utf-8")
    (d / "files" / "d1.docx").write_bytes(b"PK\x03\x04 original docx")
    return d


# --- MockMhr ---------------------------------------------------------------------------------

def test_mock_get_employee_nested_maps(mock_dir):
    c = MockMhr(mock_dir)
    e = c.get_employee("01001000001")
    assert e.employee_id == "4521" and e.first_name == "ნინო" and e.company_id == "1" and e.salary == "2500.00"
    assert e.raw["company"]["tax_id"] == "200000001" and len(e.raw["employments"]) == 2
    assert c.get_employee("99999999999") is None


def test_mock_lookup_active_then_dismissed_and_multiple(tmp_path):
    d = tmp_path / "m"
    d.mkdir()
    pn = "01001000001"
    rows = [{"id": "1", "personal_number": pn, "status": "dismissed", "dismissed_at": "2022-01-01"},
            {"id": "2", "personal_number": pn, "status": "dismissed", "dismissed_at": "2023-05-01"}]
    (d / "employees.json").write_text(json.dumps(rows), encoding="utf-8")
    assert MockMhr(d).get_employee(pn).employee_id == "2"            # ბოლო განთავისუფლებული
    rows.append({"id": "3", "personal_number": pn, "status": "active"})
    (d / "employees.json").write_text(json.dumps(rows), encoding="utf-8")
    assert MockMhr(d).get_employee(pn).employee_id == "3"            # აქტიური უპირატესია
    rows.append({"id": "4", "personal_number": pn, "status": "active"})
    (d / "employees.json").write_text(json.dumps(rows), encoding="utf-8")
    with pytest.raises(MhrError, match="multiple_matches"):
        MockMhr(d).get_employee(pn)


def test_mock_list_and_download(mock_dir):
    c = MockMhr(mock_dir)
    assert [r.doc_id for r in c.list_documents("4521")] == ["9001", "9002"]
    refs = c.list_documents("4521", "contract")
    assert [r.doc_id for r in refs] == ["9001"] and refs[0].raw["kind"] == "generated"
    assert refs[0].raw["content_type"] == DOCX_CT
    assert c.download(refs[0]) == b"PK\x03\x04 original docx"
    with pytest.raises(MhrError):
        c.download(MhrDocRef(doc_id="9002"))   # ფაილი არ არსებობს
    assert c.list_documents("4999") == []


def test_mock_upload_idempotent_signed_copy_and_find(mock_dir):
    c = MockMhr(mock_dir)
    assert c.find_by_external_id("doc_5f2a91") is None
    rid = c.upload("4521", PDF, filename="x/../scan.pdf", category="contract",
                   meta=meta(title="ხელშეკრულება — ხელმოწერილი სკანი", signed_copy_of="9001"))
    assert rid.isdigit()
    up = json.loads((mock_dir / "uploads.json").read_text(encoding="utf-8"))[0]
    assert (mock_dir / up["path"]).read_bytes() == PDF and up["path"].endswith("__scan.pdf")
    assert up["kind"] == "uploaded" and up["external_id"] == "doc_5f2a91" and up["signed_copy_of"] == "9001"
    docs = json.loads((mock_dir / "documents.json").read_text(encoding="utf-8"))
    assert docs[0]["signed_at"]                                        # ორიგინალი მოინიშნა ხელმოწერილად
    # იდემპოტენტური: იგივე external_id → იგივე id, ახალი ჩანაწერი არ იქმნება
    assert c.upload("4521", PDF, "scan.pdf", "contract", meta()) == rid
    assert len(json.loads((mock_dir / "uploads.json").read_text(encoding="utf-8"))) == 1
    with pytest.raises(MhrError, match="external_id_conflict"):
        c.upload("4600", PDF, "scan.pdf", "contract", meta())
    found = c.find_by_external_id("doc_5f2a91")
    assert found.doc_id == rid and found.raw["kind"] == "uploaded"
    assert [r.raw["kind"] for r in c.list_documents("4521", "contract")] == ["generated", "uploaded"]
    assert c.download(found) == PDF


@pytest.mark.parametrize("pdf,m,code", [
    (b"not a pdf", None, "not_pdf"),
    (PDF, {"external_id": "e1", "sha256": "0" * 64}, "sha256_mismatch"),
    (PDF, {"sha256": hashlib.sha256(PDF).hexdigest()}, "missing_external_id"),
    (PDF, "bad_copy", "invalid_signed_copy_of"),
])
def test_mock_upload_validation_errors(mock_dir, pdf, m, code):
    if m is None:
        m = meta(pdf)
    elif m == "bad_copy":
        m = meta(signed_copy_of="9999")
    with pytest.raises(MhrError, match=code):
        MockMhr(mock_dir).upload("4521", pdf, "a.pdf", "contract", m)
    assert not (mock_dir / "uploads.json").exists()
    with pytest.raises(MhrError, match="not_found"):
        MockMhr(mock_dir).upload("4999", PDF, "a.pdf", "contract", meta())


def test_mock_unavailable_marker(mock_dir):
    (mock_dir / "unavailable").write_text("", encoding="utf-8")
    c = MockMhr(mock_dir)
    for call in (lambda: c.get_employee("01001000001"), lambda: c.list_documents("4521"),
                 lambda: c.download(MhrDocRef(doc_id="9001")), lambda: c.upload("4521", PDF, "a.pdf", "c", meta()),
                 lambda: c.find_by_external_id("doc_5f2a91")):
        with pytest.raises(MhrUnavailable):
            call()
    assert not (mock_dir / "uploads.json").exists()


def test_build_client_mock_relative_dir(tmp_path):
    cfg = make_config({"mhr": {"mode": "mock", "mock_dir": "mymhr"}})
    from hrsorter.config import PROJECT_DIR
    assert build_mhr_client(cfg).dir == PROJECT_DIR / "mymhr"
    cfg.path = tmp_path / "config.json"
    assert build_mhr_client(cfg).dir == tmp_path / "mymhr"
    assert isinstance(build_mhr_client(make_config({"mhr": {"mode": "http"}})), HttpMhr)


# --- HttpMhr: რეალური API-ის კონტრაქტი (ნაგულისხმევი endpoint-ები/map-ები) ------------------------------

BASE = "https://d.mhr.example/api/v1/hr-sorter"


def http_cfg(**over) -> dict:
    c = copy.deepcopy(DEFAULTS["mhr"])
    c.update(base_url=BASE, max_retries=2)
    for k, v in over.items():
        c[k] = v
    return c


def make_client(handler, cfg=None, sleeps=None):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HttpMhr(cfg or http_cfg(), client=client, sleep=(sleeps.append if sleeps is not None else lambda s: None))


def err(status: int, code: str, message: str = "სერვერის ტექსტი 01001000001") -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message}})


def test_defaults_match_contract():
    d = DEFAULTS["mhr"]
    assert d["enabled"] is False and d["mode"] == "mock" and d["base_url"] == ""
    assert d["auth"]["method"] == "bearer"
    assert d["endpoints"]["employee_lookup"] == "/employees/lookup" and d["endpoints"]["employee_by_pn"] == ""
    assert d["categories"] == {"contract_annex": "annex"} and d["document_map"]["items_key"] == "items"


def test_http_lookup_is_post_json_pn_not_in_url():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=EMP)

    e = make_client(handler).get_employee("01001000001")
    req = seen[0]
    assert req.method == "POST" and str(req.url) == f"{BASE}/employees/lookup"
    assert json.loads(req.content) == {"personal_number": "01001000001"} and "01001000001" not in str(req.url)
    assert req.headers["Authorization"] == "Bearer s3cret-token"
    assert (e.employee_id, e.company_id, e.salary, e.position) == ("4521", "1", "2500.00", "ბუღალტერი")
    assert e.raw["employments"][1]["position"] == "ბუღალტრის ასისტენტი"


@pytest.mark.parametrize("status,code", [(401, "unauthenticated"), (403, "forbidden"), (422, "validation_failed")])
def test_http_lookup_errors_carry_only_code(status, code):
    with pytest.raises(MhrError) as ei:
        make_client(lambda r: err(status, code)).get_employee("01001000001")
    assert str(ei.value) == code and not isinstance(ei.value, MhrUnavailable)


def test_http_lookup_404_none_and_409_multiple_matches():
    assert make_client(lambda r: err(404, "not_found")).get_employee("01001000001") is None
    with pytest.raises(MhrError, match="^multiple_matches$"):
        make_client(lambda r: err(409, "multiple_matches")).get_employee("01001000001")
    with pytest.raises(MhrError, match="^multiple_matches$"):   # კოდის გარეშე 409-იც
        make_client(lambda r: httpx.Response(409)).get_employee("01001000001")


def test_http_legacy_get_fallback_when_lookup_empty():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"data": {"emp_id": 7, "pn": "01001000001", "name": {"first": "ნინო"}}})

    cfg = http_cfg(employee_map={"employee_id": "data.emp_id", "personal_number": "data.pn",
                                 "first_name": "data.name.first"})
    cfg["endpoints"] = {**cfg["endpoints"], "employee_lookup": "", "employee_by_pn": "/employees?pn={pn}"}
    e = make_client(handler, cfg).get_employee("01001000001")
    assert e.employee_id == "7" and e.first_name == "ნინო" and e.salary is None
    assert seen[0].method == "GET" and str(seen[0].url) == f"{BASE}/employees?pn=01001000001"
    body = [{"id": "A", "personal_number": "01001000002"}, {"id": "B", "personal_number": "01001000001"}]
    cfg["employee_map"] = DEFAULTS["mhr"]["employee_map"]
    assert make_client(lambda r: httpx.Response(200, json=body), cfg).get_employee("01001000001").employee_id == "B"


def test_http_basic_and_api_key_auth():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(404)

    make_client(handler, http_cfg(auth={"method": "basic", "secret_name": "hrsorter/mhr"})).get_employee("1")
    assert seen[-1].headers["Authorization"] == "Basic " + base64.b64encode(b"s3cret-token:").decode()
    make_client(handler, http_cfg(auth={"method": "api_key_header", "secret_name": "hrsorter/mhr",
                                        "header": "X-Key"})).get_employee("1")
    assert seen[-1].headers["X-Key"] == "s3cret-token"


def test_http_missing_secret_is_error_without_leak(fake_keyring):
    fake_keyring.clear()
    with pytest.raises(MhrError) as ei:
        make_client(lambda r: httpx.Response(200, json={})).get_employee("1")
    assert "s3cret" not in str(ei.value)


def test_http_int_personal_number_kept_as_str():
    e = make_client(lambda r: httpx.Response(200, json={"id": 1, "personal_number": 1001000001})).get_employee("x")
    assert e.personal_number == "1001000001"   # წამყვანი ნული არ აღდგება


def test_http_list_documents_items_category_query_and_docx_download():
    seen = []

    def handler(req):
        seen.append(req)
        if req.url.path.endswith("/file"):
            return httpx.Response(200, content=b"PK\x03\x04docx", headers={"content-type": DOCX_CT})
        return httpx.Response(200, json={"items": [DOC_UPLOADED, DOC_GENERATED]})

    c = make_client(handler)
    refs = c.list_documents("4521", "contract")
    assert seen[0].url.path == "/api/v1/hr-sorter/employees/4521/documents"
    assert seen[0].url.params["category"] == "contract"
    assert [(r.doc_id, r.raw["kind"]) for r in refs] == [("9310", "uploaded"), ("9001", "generated")]
    assert refs[1].raw["document_number"] is None and refs[1].raw["document_date"] == "2024-06-04"
    assert c.download(refs[1]) == b"PK\x03\x04docx" and seen[-1].url.path == "/api/v1/hr-sorter/documents/9001/file"
    c.list_documents("4521")
    assert "category" not in seen[-1].url.params
    with pytest.raises(MhrError, match="unknown_category"):
        make_client(lambda r: err(422, "unknown_category")).list_documents("4521", "xx")


def test_http_legacy_templates_url_encoding_and_top_level_list():
    def handler(req):
        assert req.url.raw_path == b"/api/v1/hr-sorter/employees/E%201/docs/contract"
        return httpx.Response(200, json=[{"id": 5, "category": "contract"}])

    cfg = http_cfg()
    cfg["endpoints"] = {**cfg["endpoints"], "list_documents": "employees/{employee_id}/docs/{category}"}
    assert make_client(handler, cfg).list_documents("E 1", "contract")[0].doc_id == "5"


def test_http_find_by_external_id():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=DOC_UPLOADED) if req.url.path.endswith("doc_5f2a91") else err(404, "not_found")

    c = make_client(handler)
    ref = c.find_by_external_id("doc_5f2a91")
    assert ref.doc_id == "9310" and ref.raw["external_id"] == "doc_5f2a91" and seen[0].method == "GET"
    assert seen[0].url.path == "/api/v1/hr-sorter/documents/by-external-id/doc_5f2a91"
    assert c.find_by_external_id("doc_other") is None


def _form(req: httpx.Request) -> str:
    return req.content.decode("utf-8", "replace")


@pytest.mark.parametrize("status", [201, 200])   # 201 — შეიქმნა, 200 — უკვე არსებობს (იდემპოტენტური)
def test_http_upload_multipart_fields_and_idempotency_key(status):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(status, json=DOC_UPLOADED)

    m = meta(title="შრომითი ხელშეკრულება — ხელმოწერილი სკანი", document_date="2024-06-04", document_number=None,
             signed_copy_of="9001", metadata={"type_id": "contract", "validation_status": "passed"},
             document_id="not-a-contract-field")
    rid = make_client(handler).upload("4521", PDF, "dir/scan.pdf", "contract", m)
    assert rid == "9310"
    req = seen[0]
    assert req.method == "POST" and req.url.path == "/api/v1/hr-sorter/employees/4521/documents"
    assert req.headers["Idempotency-Key"] == "doc_5f2a91" == m["external_id"]
    assert req.headers["content-type"].startswith("multipart/form-data")
    body = _form(req)
    for part in ('name="file"; filename="scan.pdf"', "%PDF-1.4 signed scan", 'name="category"', 'name="external_id"',
                 'name="sha256"', hashlib.sha256(PDF).hexdigest(), 'name="title"', "ხელმოწერილი სკანი",
                 'name="document_date"', "2024-06-04", 'name="signed_copy_of"', 'name="metadata"',
                 '{"type_id":"contract","validation_status":"passed"}'):
        assert part in body
    assert 'name="document_number"' not in body and 'name="document_id"' not in body


def test_http_upload_local_checks_before_sending():
    c = make_client(lambda r: pytest.fail("no request expected"))
    big = b"%PDF-" + b"0" * MAX_UPLOAD_BYTES
    with pytest.raises(MhrError, match="file_too_large"):
        c.upload("4521", big, "a.pdf", "contract", meta(big))
    with pytest.raises(MhrError, match="sha256_mismatch"):
        c.upload("4521", PDF, "a.pdf", "contract", meta(sha256="0" * 64))
    with pytest.raises(MhrError, match="missing_external_id"):
        c.upload("4521", PDF, "a.pdf", "contract", {})
    with pytest.raises(MhrError, match="metadata_too_long"):
        c.upload("4521", PDF, "a.pdf", "contract", meta(metadata="x" * 20001))


@pytest.mark.parametrize("status,code", [(409, "external_id_conflict"), (413, "file_too_large"), (422, "unknown_category"),
                                         (422, "not_pdf"), (422, "sha256_mismatch"), (422, "invalid_signed_copy_of"),
                                         (422, "validation_failed"), (400, "idempotency_key_mismatch"), (403, "forbidden")])
def test_http_upload_errors_are_codes(status, code):
    calls = []

    def handler(req):
        calls.append(1)
        return err(status, code)

    with pytest.raises(MhrError) as ei:
        make_client(handler).upload("4521", PDF, "a.pdf", "contract", meta())
    assert str(ei.value) == code and not isinstance(ei.value, MhrUploadUncertain) and len(calls) == 1
    assert "01001000001" not in str(ei.value)


def test_http_429_honors_retry_after_bounded_then_unavailable():
    sleeps, calls = [], []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "7"}, json={"error": {"code": "rate_limited", "message": "x"}})

    with pytest.raises(MhrUnavailable):
        make_client(handler, sleeps=sleeps).upload("4521", PDF, "a.pdf", "contract", meta())
    assert len(calls) == 3 and sleeps == [7.0, 7.0]
    sleeps.clear()
    with pytest.raises(MhrUnavailable):
        make_client(lambda r: httpx.Response(429, headers={"Retry-After": "3600"}), sleeps=sleeps).get_employee("1")
    assert sleeps == [30.0, 30.0]                                       # ≤ 30 წამი ერთ ლოდინზე


def test_http_429_then_success():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "1"}) if len(calls) == 1 else httpx.Response(201, json={"id": "77"})

    assert make_client(handler).upload("4521", PDF, "a.pdf", "contract", meta()) == "77" and len(calls) == 2


def test_http_5xx_retries_then_unavailable():
    calls, sleeps = [], []

    def handler(req):
        calls.append(1)
        return httpx.Response(503)

    with pytest.raises(MhrUnavailable):
        make_client(handler, sleeps=sleeps).get_employee("1")
    assert len(calls) == 3 and len(sleeps) == 2 and sleeps[1] > sleeps[0]


def test_http_network_error_then_success():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"id": "E1", "personal_number": "1"})

    assert make_client(handler).get_employee("1").employee_id == "E1"


def test_http_other_4xx_without_body_is_error():
    with pytest.raises(MhrError) as ei:
        make_client(lambda r: httpx.Response(401)).get_employee("1")
    assert not isinstance(ei.value, MhrUnavailable) and str(ei.value) == "http_401"


def test_http_unconfigured_is_error():
    cfg = http_cfg()
    cfg["endpoints"] = {**cfg["endpoints"], "upload_document": "", "by_external_id": ""}
    c = make_client(lambda r: pytest.fail("no request expected"), cfg)
    with pytest.raises(MhrError, match="endpoint not configured: upload_document"):
        c.upload("4521", PDF, "a.pdf", "c", meta())
    with pytest.raises(MhrError, match="endpoint not configured: by_external_id"):
        c.find_by_external_id("doc_1")
    with pytest.raises(MhrError, match="base_url not configured"):   # ნაგულისხმევი: base_url ცარიელია
        HttpMhr(DEFAULTS["mhr"], client=httpx.Client(transport=httpx.MockTransport(lambda r: None))).get_employee("1")


def test_http_upload_ambiguous_failures_are_uncertain_not_retried():
    calls = []

    def timeout(req):
        calls.append(1)
        raise httpx.ReadTimeout("slow")

    with pytest.raises(MhrUploadUncertain):
        make_client(timeout).upload("4521", PDF, "a.pdf", "c", meta())
    assert len(calls) == 1
    with pytest.raises(MhrUploadUncertain):
        make_client(lambda r: httpx.Response(500)).upload("4521", PDF, "a.pdf", "c", meta())
    with pytest.raises(MhrUploadUncertain):
        make_client(lambda r: httpx.Response(200, json={"other": 1})).upload("4521", PDF, "a.pdf", "c", meta())
    with pytest.raises(MhrUnavailable):   # 503 — ცალსახად არ დამუშავდა → retry, შემდეგ unavailable
        make_client(lambda r: httpx.Response(503)).upload("4521", PDF, "a.pdf", "c", meta())


def test_mock_contract_template(mock_dir):
    (mock_dir / "templates.json").write_text(json.dumps([{"id": "312", "employee_id": "4521", "position": "მენეჯერი",
                                                          "work_status": "შრომითი", "filename": "t312.docx"}],
                                                        ensure_ascii=False), encoding="utf-8")
    (mock_dir / "files" / "t312.docx").write_bytes(b"PK template")
    m = MockMhr(mock_dir)
    tpl = m.get_contract_template("4521")
    assert tpl.doc_id == "312" and tpl.raw["work_status"] == "შრომითი"
    assert m.download_template(tpl) == b"PK template"
    assert m.get_contract_template("4600") is None


def test_http_contract_template_and_file():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        if req.url.path.endswith("/employees/4521/contract-template"):
            return httpx.Response(200, json={"id": "312", "position": "მენეჯერი", "work_status": "შრომითი",
                                             "filename": "t.docx", "sha256": "ab"})
        if req.url.path.endswith("/contract-templates/312/file"):
            return httpx.Response(200, content=b"PK template")
        return err(404, "template_not_found")

    c = make_client(handler)
    tpl = c.get_contract_template("4521")
    assert tpl.doc_id == "312" and tpl.raw["position"] == "მენეჯერი"
    assert c.download_template(tpl) == b"PK template"
    assert c.get_contract_template("4600") is None   # 404 template_not_found → შაბლონი არ არის
    assert seen[0] == "/api/v1/hr-sorter/employees/4521/contract-template"
    # ძველი კონფიგურაცია endpoint-ის გარეშე — შაბლონი უბრალოდ არ გამოიყენება
    eps = {**DEFAULTS["mhr"]["endpoints"], "contract_template": ""}
    assert make_client(handler, http_cfg(endpoints=eps)).get_contract_template("4521") is None
