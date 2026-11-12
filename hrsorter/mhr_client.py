"""MHR კლიენტი: თანამშრომლის მოძიება, დოკუმენტების სია/ჩამოტვირთვა, ატვირთვა და ატვირთვის შემოწმება.

კონტრაქტი — MHR HR Sorter API (`docs/api/hr-sorter-openapi.yaml`, OpenAPI 3): base_url + `mhr.endpoints`
შაბლონები (`{employee_id}`, `{doc_id}`, `{external_id}`; ძველი GET-ისთვის `{pn}`). პ/ნ URL-ში არასდროს —
`POST /employees/lookup` JSON-ით. ცარიელი შაბლონი → `MhrError("endpoint not configured: …")`. ტესტები და დემო —
`MockMhr` (ქსელი არ გამოიყენება).

შეცდომები: `MhrUnavailable` — დროებითი (ქსელი, 5xx, 429; მოგვიანებით ხელახლა ცდა), `MhrError` — მუდმივი; მისი
ტექსტი მხოლოდ მანქანური კოდია (`error.code`, მაგ. `forbidden`, `multiple_matches`, `external_id_conflict`) ან
`http_<status>` — სერვერის შეტყობინება (შეიძლება პირად მონაცემს შეიცავდეს) არსად ინახება.
`MhrUploadUncertain` (MhrError-ის ქვეკლასი) — ატვირთვა შეიძლება შესრულდა (მაგ. პასუხის timeout): ავტომატურად
ხელახლა არ ვაგზავნით; რიგი `find_by_external_id`-ით ამოწმებს (სერვერი `external_id`-ით იდემპოტენტურია).
საიდუმლოება, URL-ები და დოკუმენტის მონაცემები ჟურნალში არ იწერება.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import quote

import httpx

from . import secrets
from .config import DEFAULTS, PROJECT_DIR, Config
from .employee_api import _get_path, _str_or_none
from .mhr_models import MhrDocRef, MhrEmployee, MhrError, MhrUnavailable, is_statement

MAX_UPLOAD_BYTES = 20 * 1024 * 1024        # API-ის ლიმიტი (413 file_too_large)
METADATA_MAX_CHARS = 20000
RETRY_AFTER_MAX_SECONDS = 30.0             # 429/503 Retry-After — ერთ ლოდინზე მაქსიმუმ ამდენი
DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
# ატვირთვის multipart ველები (meta-დან ზუსტად ამ სახელებით; file და category ცალკე)
UPLOAD_FIELDS = ("external_id", "sha256", "title", "document_number", "document_date", "signed_copy_of", "metadata")
_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")


class MhrUploadUncertain(MhrError):
    """ატვირთვის შედეგი უცნობია (მოთხოვნა შეიძლება სერვერამდე მივიდა) — საჭიროა შემოწმება external_id-ით."""


class MhrClient(Protocol):
    def get_employee(self, personal_number: str) -> MhrEmployee | None: ...
    def list_documents(self, employee_id: str, category: str | None = None) -> list[MhrDocRef]: ...
    def download(self, ref: MhrDocRef) -> bytes: ...
    def upload(self, employee_id: str, pdf: bytes, filename: str, category: str, meta: dict) -> str: ...
    def find_by_external_id(self, external_id: str) -> MhrDocRef | None: ...
    def get_contract_template(self, employee_id: str) -> MhrDocRef | None: ...
    def download_template(self, ref: MhrDocRef) -> bytes: ...


def _template_ref(row: dict[str, Any]) -> MhrDocRef:
    """თანამდებობის შაბლონი (`GET employees/{id}/contract-template`): position/work_status — `raw`-ში."""
    return MhrDocRef(doc_id=str(row["id"]), category="template", title=row.get("position"),
                     created_at=row.get("updated_at"), filename=row.get("filename"), raw=row)


# --- map-ები ------------------------------------------------------------------------------

def employee_from(raw: dict[str, Any], emap: dict[str, str]) -> MhrEmployee:
    """API ჩანაწერი → MhrEmployee `employee_map`-ის dotted ბილიკებით. პ/ნ ყოველთვის სტრიქონია."""
    def g(key: str) -> str | None:
        return _str_or_none(_get_path(raw, emap.get(key) or ""))
    return MhrEmployee(employee_id=g("employee_id") or "", personal_number=g("personal_number"),
                       first_name=g("first_name"), last_name=g("last_name"), company_id=g("company_id"),
                       position=g("position"), salary=g("salary"), contract_start=g("contract_start"),
                       contract_end=g("contract_end"), status=g("status"), raw=raw)


def docref_from(raw: dict[str, Any], dmap: dict[str, str]) -> MhrDocRef:
    """API Document → MhrDocRef. kind/content_type/document_date/document_number/signed_at/external_id — `raw`-ში."""
    def g(key: str) -> str | None:
        return _str_or_none(_get_path(raw, dmap.get(key) or ""))
    return MhrDocRef(doc_id=g("doc_id") or "", category=g("category"), title=g("title"),
                     created_at=g("created_at"), filename=g("filename"), raw=raw)


def _metadata_str(value: Any) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(s) > METADATA_MAX_CHARS:
        raise MhrError("metadata_too_long")
    return s


def _upload_fields(pdf: bytes, meta: dict) -> dict[str, str]:
    """meta → კონტრაქტის ველები (ცარიელები გამოტოვებულია) + ადგილობრივი შემოწმება გაგზავნამდე."""
    if len(pdf) > MAX_UPLOAD_BYTES:
        raise MhrError("file_too_large")
    out: dict[str, str] = {}
    for k in UPLOAD_FIELDS:
        v = meta.get(k)
        if v is None or v == "":
            continue
        out[k] = _metadata_str(v) if k == "metadata" else str(v)
    if not out.get("external_id"):
        raise MhrError("missing_external_id")
    actual = hashlib.sha256(pdf).hexdigest()
    if out.setdefault("sha256", actual).lower() != actual:
        raise MhrError("sha256_mismatch")
    return out


# --- Mock ------------------------------------------------------------------------------------

class MockMhr:
    """JSON ფაილებზე დაფუძნებული ყალბი MHR (ტესტები/დემო) — რეალური API-ის ქცევის ასლი.

    `<dir>/employees.json` (Employee: ჩადგმული company/salary, employments), `<dir>/documents.json` (Document:
    ორიგინალი `.docx`, kind generated), `<dir>/files/<filename>`; ატვირთვები (kind uploaded) →
    `<dir>/uploads/<employee_id>/<remote_id>__<filename>` + `<dir>/uploads.json`. ატვირთვა იდემპოტენტურია
    external_id-ით; `signed_copy_of` ორიგინალს `signed_at`-ს უწერს. `<dir>/unavailable` ფაილი → ყოველი
    გამოძახება `MhrUnavailable` (გათიშვის იმიტაცია).
    """

    def __init__(self, dir: Path):
        self.dir = Path(dir)
        self.emap = DEFAULTS["mhr"]["employee_map"]
        self.dmap = DEFAULTS["mhr"]["document_map"]

    def _check(self) -> None:
        if (self.dir / "unavailable").exists():
            raise MhrUnavailable("mock MHR marked unavailable")

    def _load(self, name: str) -> list[dict[str, Any]]:
        p = self.dir / name
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise MhrUnavailable(f"mock file unreadable: {name} ({type(e).__name__})") from e
        return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []

    def _dump(self, name: str, data: list[dict[str, Any]]) -> None:
        tmp = self.dir / f"{name}.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.dir / name)

    def get_employee(self, personal_number: str) -> MhrEmployee | None:
        """აქტიური ჩანაწერი; თუ არ არის — ბოლო განთავისუფლებული; ორი აქტიური → MhrError("multiple_matches")."""
        self._check()
        emps = [e for e in (employee_from(r, self.emap) for r in self._load("employees.json"))
                if e.personal_number == personal_number]
        active = [e for e in emps if e.status != "dismissed"]
        if len(active) > 1:
            raise MhrError("multiple_matches")
        if active:
            return active[0]
        return max(emps, key=lambda e: str(e.raw.get("dismissed_at") or ""), default=None)

    def _all_documents(self) -> list[dict[str, Any]]:
        return self._load("documents.json") + self._load("uploads.json")

    def list_documents(self, employee_id: str, category: str | None = None) -> list[MhrDocRef]:
        self._check()
        refs = [docref_from(r, self.dmap) for r in self._all_documents() if str(r.get("employee_id")) == employee_id]
        return [r for r in refs if category is None or r.category == category]

    def download(self, ref: MhrDocRef) -> bytes:
        self._check()
        row = next((r for r in self._all_documents() if str(r.get("id")) == ref.doc_id), None)
        if row is None or not (row.get("path") or row.get("filename")):
            raise MhrError("not_found")
        p = self.dir / row["path"] if row.get("path") else self.dir / "files" / Path(str(row["filename"])).name
        try:
            return p.read_bytes()
        except OSError as e:
            raise MhrError(f"document file missing ({type(e).__name__})") from e

    def find_by_external_id(self, external_id: str) -> MhrDocRef | None:
        self._check()
        row = next((r for r in self._load("uploads.json") if r.get("external_id") == external_id), None)
        return docref_from(row, self.dmap) if row else None

    def get_contract_template(self, employee_id: str) -> MhrDocRef | None:
        """`templates.json`: [{id, employee_id, position, work_status, filename}] — რეალურ MHR-ში შაბლონი თანამდებობით
        + ხელშეკრულების ტიპით ირჩევა; იმიტაციაში პირდაპირ თანამშრომელზეა მიბმული."""
        self._check()
        row = next((r for r in self._load("templates.json") if str(r.get("employee_id")) == employee_id), None)
        return _template_ref(row) if row else None

    def download_template(self, ref: MhrDocRef) -> bytes:
        self._check()
        try:
            return (self.dir / "files" / Path(str(ref.filename or "")).name).read_bytes()
        except OSError as e:
            raise MhrError(f"template file missing ({type(e).__name__})") from e

    def upload(self, employee_id: str, pdf: bytes, filename: str, category: str, meta: dict) -> str:
        self._check()
        if not any(str(r.get("id")) == employee_id for r in self._load("employees.json")):
            raise MhrError("not_found")
        fields = _upload_fields(pdf, meta)
        if not pdf.startswith(b"%PDF-"):
            raise MhrError("not_pdf")
        uploads = self._load("uploads.json")
        same = next((u for u in uploads if u.get("external_id") == fields["external_id"]), None)
        if same is not None:   # იდემპოტენტური: იგივე external_id → არსებული (200), სხვა თანამშრომელზე → 409
            if str(same.get("employee_id")) != employee_id:
                raise MhrError("external_id_conflict")
            return str(same["id"])
        docs = self._load("documents.json")
        original = None
        if fields.get("signed_copy_of"):
            # ნამდვილი MHR-ის იდენტურად: გენერირებული ორიგინალი ან განთავისუფლების განცხადება (მენეჯერის ფოტო)
            original = next((d for d in docs if str(d.get("id")) == fields["signed_copy_of"]
                             and (d.get("kind") == "generated" or is_statement(d))
                             and str(d.get("employee_id")) == employee_id), None)
            if original is None:
                raise MhrError("invalid_signed_copy_of")
        remote_id = str(500000 + len(uploads) + 1)
        name = Path(filename).name
        target = self.dir / "uploads" / employee_id / f"{remote_id}__{name}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(pdf)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        uploads.append({"id": remote_id, "employee_id": employee_id, "category": category,
                        "title": fields.get("title") or name, "document_number": fields.get("document_number"),
                        "document_date": fields.get("document_date"), "created_at": now, "filename": name,
                        "content_type": "application/pdf", "sha256": fields["sha256"], "size": len(pdf),
                        "kind": "uploaded", "signed_at": None, "signed_copy_of": fields.get("signed_copy_of"),
                        "external_id": fields["external_id"], "metadata": fields.get("metadata"),
                        "path": target.relative_to(self.dir).as_posix()})
        self._dump("uploads.json", uploads)
        if original is not None:
            original["signed_at"] = now
            self._dump("documents.json", docs)
        return remote_id


# --- HTTP ------------------------------------------------------------------------------------

class HttpMhr:
    """HTTP MHR: base_url + endpoint შაბლონები, Bearer ავტორიზაცია, map-ები, retry/backoff (+ Retry-After)."""

    def __init__(self, cfg_mhr: dict[str, Any], client: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        d = DEFAULTS["mhr"]
        self.cfg = cfg_mhr
        self.base_url = (cfg_mhr.get("base_url") or "").rstrip("/")
        self.endpoints: dict[str, str] = cfg_mhr.get("endpoints") or {}
        self.emap: dict[str, str] = cfg_mhr.get("employee_map") or d["employee_map"]
        self.dmap: dict[str, str] = cfg_mhr.get("document_map") or d["document_map"]
        self.up: dict[str, Any] = {**d["upload"], **(cfg_mhr.get("upload") or {})}
        self.max_retries = int(cfg_mhr.get("max_retries", 3))
        self.backoff = float(cfg_mhr.get("retry_backoff_seconds", 2))
        self._sleep = sleep
        self._client = client or httpx.Client(timeout=float(cfg_mhr.get("timeout_seconds", 20)))

    # ავტორიზაცია: საიდუმლოება მხოლოდ keyring-იდან; მნიშვნელობა არსად იწერება
    def _auth(self) -> tuple[dict[str, str], httpx.Auth | None]:
        auth = self.cfg.get("auth") or {}
        method = auth.get("method", "none")
        if method == "none":
            return {}, None
        secret = secrets.get_secret(auth.get("secret_name", ""))
        if not secret:
            raise MhrError(f"secret not set for auth method {method}")
        if method == "bearer":
            return {"Authorization": f"Bearer {secret}"}, None
        if method == "basic":
            user, _, pwd = secret.partition(":")
            return {}, httpx.BasicAuth(user, pwd)
        if method == "api_key_header":
            return {auth.get("header") or "X-API-Key": secret}, None
        raise MhrError(f"unsupported auth method {method}")

    def _url(self, key: str, **values: str) -> str:
        template = self.endpoints.get(key) or ""
        if not template:
            raise MhrError(f"endpoint not configured: {key}")
        if not self.base_url:
            raise MhrError("base_url not configured")
        try:
            path = template.format(**{k: quote(v or "", safe="") for k, v in values.items()})
        except (KeyError, IndexError, ValueError) as e:
            raise MhrError(f"bad endpoint template {key}: {type(e).__name__}") from e
        return f"{self.base_url}/{path.lstrip('/')}"

    @staticmethod
    def _error_code(resp: httpx.Response) -> str:
        """`{"error": {"code": ...}}` → კოდი (მხოლოდ მანქანური სიმბოლოები); სხვაგვარად http_<status>."""
        try:
            code = (resp.json().get("error") or {}).get("code")
        except (ValueError, AttributeError):
            code = None
        return code if isinstance(code, str) and _CODE_RE.fullmatch(code) else f"http_{resp.status_code}"

    def _retry_after(self, resp: httpx.Response) -> float | None:
        try:
            return min(RETRY_AFTER_MAX_SECONDS, max(0.0, float(resp.headers.get("Retry-After", ""))))
        except ValueError:
            return None

    def _send(self, method: str, url: str, *, upload: bool = False, headers: dict[str, str] | None = None,
              **kw: Any) -> httpx.Response | None:
        """retry ქსელის შეცდომებზე/5xx/429 → ამოწურვისას MhrUnavailable. 404 → None, სხვა 4xx → MhrError(code).

        429/503-ზე `Retry-After` (წამები, ≤ RETRY_AFTER_MAX_SECONDS) — სხვაგვარად ექსპონენციური backoff.
        ატვირთვისას (upload=True) ვიმეორებთ მხოლოდ მაშინ, როცა მოთხოვნა ცალსახად არ დამუშავებულა
        (კავშირი ვერ დამყარდა, 429, 503); გაურკვეველი შედეგი → MhrUploadUncertain.
        """
        auth_headers, auth = self._auth()
        hdrs = {**auth_headers, **(headers or {})}
        last = ""
        delay: float | None = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                self._sleep(delay if delay is not None else self.backoff * (2 ** (attempt - 1)))
            delay = None
            try:
                resp = self._client.request(method, url, headers=hdrs, auth=auth, **kw)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                last = type(e).__name__
                continue
            except httpx.TransportError as e:   # read timeout, გაწყვეტილი კავშირი
                if upload:
                    raise MhrUploadUncertain(f"upload result unknown ({type(e).__name__})") from e
                last = type(e).__name__
                continue
            except (httpx.HTTPError, httpx.InvalidURL) as e:
                raise MhrError(type(e).__name__) from e
            code = resp.status_code
            if code == 429 or code >= 500:
                if upload and code not in (429, 503):
                    raise MhrUploadUncertain(f"upload result unknown (http_{code})")
                last = f"http_{code}"
                delay = self._retry_after(resp) if code in (429, 503) else None
                continue
            if code == 404:
                return None
            if code >= 400:
                raise MhrError(self._error_code(resp))
            return resp
        raise MhrUnavailable(f"retries exhausted ({last})")

    @staticmethod
    def _json(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except ValueError as e:
            raise MhrError("invalid json") from e

    def get_employee(self, personal_number: str) -> MhrEmployee | None:
        """`POST employee_lookup` {"personal_number"} (ძველი GET `employee_by_pn` — მხოლოდ თუ lookup ცარიელია).
        404 → None; 409 → MhrError("multiple_matches") — ავტომატურად არ ვაბამთ."""
        try:
            if self.endpoints.get("employee_lookup"):
                resp = self._send("POST", self._url("employee_lookup"), json={"personal_number": personal_number})
            else:
                resp = self._send("GET", self._url("employee_by_pn", pn=personal_number))
        except MhrError as e:
            if str(e) == "http_409":
                raise MhrError("multiple_matches") from e
            raise
        if resp is None:
            return None
        body = self._json(resp)
        if isinstance(body, dict):
            emps = [employee_from(body, self.emap)]
        elif isinstance(body, list):
            emps = [e for e in (employee_from(x, self.emap) for x in body if isinstance(x, dict))
                    if e.personal_number == personal_number]
        else:
            raise MhrError("unexpected employee response")
        if not emps:
            return None
        if len(emps) > 1:
            raise MhrError("multiple_matches")
        if not emps[0].employee_id:
            raise MhrError("employee response without id")
        return emps[0]

    def list_documents(self, employee_id: str, category: str | None = None) -> list[MhrDocRef]:
        template = self.endpoints.get("list_documents") or ""
        params = {"category": category} if category and "{category}" not in template else None
        resp = self._send("GET", self._url("list_documents", employee_id=employee_id, category=category or ""),
                          params=params)
        if resp is None:
            raise MhrError("not_found")
        body = self._json(resp)
        items_key = self.dmap.get("items_key") or ""
        items = _get_path(body, items_key) if items_key and isinstance(body, dict) else body
        if not isinstance(items, list):
            raise MhrError("unexpected documents response")
        refs = [docref_from(x, self.dmap) for x in items if isinstance(x, dict)]
        return [r for r in refs if category is None or r.category is None or r.category == category]

    def download(self, ref: MhrDocRef) -> bytes:
        """ფაილის ბაიტები: ორიგინალი (kind generated) — .docx, ატვირთული — PDF."""
        resp = self._send("GET", self._url("download_document", doc_id=ref.doc_id, category=ref.category or ""))
        if resp is None:
            raise MhrError("not_found")
        return resp.content

    def get_contract_template(self, employee_id: str) -> MhrDocRef | None:
        """თანამდებობის + ხელშეკრულების ტიპის შაბლონი. 404 (შაბლონი არ არის) ან endpoint-ის გარეშე → None."""
        if not self.endpoints.get("contract_template"):
            return None
        resp = self._send("GET", self._url("contract_template", employee_id=employee_id))
        if resp is None:
            return None
        body = self._json(resp)
        if not isinstance(body, dict) or not body.get("id"):
            raise MhrError("unexpected template response")
        return _template_ref(body)

    def download_template(self, ref: MhrDocRef) -> bytes:
        resp = self._send("GET", self._url("template_file", template_id=ref.doc_id))
        if resp is None:
            raise MhrError("not_found")
        return resp.content

    def find_by_external_id(self, external_id: str) -> MhrDocRef | None:
        """ატვირთვის შემოწმება (კავშირი ატვირთვისას გაწყდა). 404 → None (ცალსახად არ არის ატვირთული)."""
        resp = self._send("GET", self._url("by_external_id", external_id=external_id))
        if resp is None:
            return None
        body = self._json(resp)
        ref = docref_from(body, self.dmap) if isinstance(body, dict) else None
        if ref is None or not ref.doc_id:
            raise MhrError("unexpected document response")
        return ref

    def upload(self, employee_id: str, pdf: bytes, filename: str, category: str, meta: dict) -> str:
        """multipart: file, category, external_id, sha256 (+ title, document_number, document_date, signed_copy_of,
        metadata) — meta-დან ზუსტად ამ სახელებით; `Idempotency-Key: <external_id>`. 200 (უკვე არსებობს) და 201 —
        წარმატება, აბრუნებს id-ს. 409/413/422 → MhrError(code)."""
        url = self._url("upload_document", employee_id=employee_id, category=category)
        fields = _upload_fields(pdf, meta)
        data = {**{str(k): str(v) for k, v in (self.up.get("extra_fields") or {}).items() if v is not None}, **fields}
        data[self.up["category_field"]] = category
        files = {self.up["file_field"]: (Path(filename).name, pdf, "application/pdf")}
        resp = self._send("POST", url, upload=True, headers={"Idempotency-Key": fields["external_id"]},
                          data=data, files=files)
        if resp is None:
            raise MhrError("not_found")
        try:
            body = resp.json()
        except ValueError as e:
            raise MhrUploadUncertain("upload response is not json") from e
        remote_id = _str_or_none(_get_path(body, self.up.get("remote_id_key") or "id"))
        if not remote_id:
            raise MhrUploadUncertain("upload response without remote id")
        return remote_id


def build_mhr_client(cfg: Config) -> MhrClient:
    """`mhr.mode`: mock (JSON საქაღალდე) | http."""
    m = cfg.section("mhr")
    mode = m.get("mode", "mock")
    if mode == "http":
        return HttpMhr(m)
    if mode == "mock":
        p = Path(m.get("mock_dir") or DEFAULTS["mhr"]["mock_dir"])
        if not p.is_absolute():
            p = (cfg.path.parent if cfg.path else PROJECT_DIR) / p
        return MockMhr(p)
    raise MhrError(f"unknown mhr.mode {mode!r}")
