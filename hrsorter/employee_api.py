"""თანამშრომლების API ადაპტერი: mock ფაილი ან HTTP API, ადგილობრივი ქეშით.

რეალური API-ის endpoint-ები ცნობილი არ არის, ამიტომ ყველა მისამართი კონფიგურაციის შაბლონია
(`employee_api.endpoints`): ცარიელი შაბლონი = შესაძლებლობა მხარდაჭერილი არ არის → `not_checked`.
შაბლონის ცვლადები: `{pn}`, `{id}`, `{first}`, `{last}`, `{name}` (URL-კოდირებით).

ძიების მეთოდები გამონაკლისს არ ისვრის: API-ის პრობლემა → სტატუსი `api_unavailable`
(CachedDirectory-ში — ქეშიდან პასუხი, თუ არსებობს). საიდუმლოება და დოკუმენტის მონაცემები ჟურნალში არ იწერება.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import quote, urljoin

import httpx

from . import secrets
from .config import DEFAULTS, PROJECT_DIR, Config
from .models import EmployeeLookup, EmployeeRecord, Extraction
from .textnorm import one_line

log = logging.getLogger(__name__)

VERIFIED, NOT_FOUND, AMBIGUOUS = "verified", "not_found", "ambiguous"
API_UNAVAILABLE, NOT_CHECKED = "api_unavailable", "not_checked"
_MAX_PAGES = 50          # გვერდობრივი ჩამოტვირთვის ზედა ზღვარი (უსასრულო ციკლისგან დაცვა)
_MAX_CANDIDATES = 10


class EmployeeApiUnavailable(Exception):
    """API მიუწვდომელია (ქსელი, 5xx/429 retry-ის შემდეგ, ავტორიზაცია). მხოლოდ ადაპტერის შიგნით."""


class EmployeeDirectory(Protocol):
    def find_by_personal_number(self, pn: str) -> EmployeeLookup: ...
    def find_by_id(self, employee_id: str) -> EmployeeLookup: ...
    def search_by_name(self, first: str | None, last: str | None) -> EmployeeLookup: ...
    def companies(self) -> list[dict]: ...


# --- საერთო დამხმარეები ----------------------------------------------------------

def _get_path(d: Any, path: str) -> Any:
    """`a.b.c` ბილიკით მნიშვნელობა ჩადგმული dict-იდან; არარსებული → None."""
    if not path:
        return None
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _str_or_none(v: Any) -> str | None:
    return None if v is None or v == "" else str(v)


def record_from(raw: dict[str, Any], field_map: dict[str, str]) -> EmployeeRecord:
    """API ჩანაწერი → EmployeeRecord `field_map`-ით. history მხოლოდ თუ map-ში მითითებულია და სიაა."""
    history = _get_path(raw, field_map.get("history") or "")
    return EmployeeRecord(
        employee_id=str(_get_path(raw, field_map.get("employee_id", "id")) or ""),
        first_name=_str_or_none(_get_path(raw, field_map.get("first_name", ""))),
        last_name=_str_or_none(_get_path(raw, field_map.get("last_name", ""))),
        personal_number=_str_or_none(_get_path(raw, field_map.get("personal_number", ""))),
        company_id=_str_or_none(_get_path(raw, field_map.get("company_id", ""))),
        branch=_str_or_none(_get_path(raw, field_map.get("branch", ""))),
        status=_str_or_none(_get_path(raw, field_map.get("status", ""))),
        history=history if isinstance(history, list) else [],
        raw=raw,
    )


def _pn_result(pn: str, records: list[EmployeeRecord]) -> EmployeeLookup:
    """verified მხოლოდ მაშინ, როცა ზუსტად ერთ ჩანაწერს აქვს იგივე პ/ნ. სხვა დაბრუნებული — კანდიდატები."""
    exact = [r for r in records if r.personal_number == pn]
    if len(exact) == 1:
        return EmployeeLookup(VERIFIED, record=exact[0], matched_by="personal_number")
    if len(exact) > 1:
        return EmployeeLookup(AMBIGUOUS, candidates=exact[:_MAX_CANDIDATES], matched_by="personal_number",
                              detail="duplicate_personal_number")
    return EmployeeLookup(NOT_FOUND, candidates=records[:_MAX_CANDIDATES])


def _id_result(employee_id: str, records: list[EmployeeRecord]) -> EmployeeLookup:
    exact = [r for r in records if r.employee_id == employee_id]
    if len(exact) == 1:
        return EmployeeLookup(VERIFIED, record=exact[0], matched_by="employee_id")
    if len(exact) > 1:
        return EmployeeLookup(AMBIGUOUS, candidates=exact[:_MAX_CANDIDATES], matched_by="employee_id")
    return EmployeeLookup(NOT_FOUND)


def _name_result(records: list[EmployeeRecord]) -> EmployeeLookup:
    """სახელით: ერთი → verified (matched_by=name, დაბალი ნდობა); რამდენიმე → ambiguous (მიბმა არ ხდება)."""
    if len(records) == 1:
        return EmployeeLookup(VERIFIED, record=records[0], matched_by="name", detail="name_only")
    if len(records) > 1:
        return EmployeeLookup(AMBIGUOUS, candidates=records[:_MAX_CANDIDATES], matched_by="name")
    return EmployeeLookup(NOT_FOUND)


def _name_matches(rec: EmployeeRecord, first: str | None, last: str | None) -> bool:
    if first and one_line(rec.first_name or "") != one_line(first):
        return False
    if last and one_line(rec.last_name or "") != one_line(last):
        return False
    return bool(first or last)


# --- გამორთული API ------------------------------------------------------------------

class DisabledDirectory:
    """employee_api.enabled=false: ყველაფერი not_checked."""

    def find_by_personal_number(self, pn: str) -> EmployeeLookup:
        return EmployeeLookup(NOT_CHECKED, detail="api_disabled")

    def find_by_id(self, employee_id: str) -> EmployeeLookup:
        return EmployeeLookup(NOT_CHECKED, detail="api_disabled")

    def search_by_name(self, first: str | None, last: str | None) -> EmployeeLookup:
        return EmployeeLookup(NOT_CHECKED, detail="api_disabled")

    def companies(self) -> list[dict]:
        return []


# --- Mock ------------------------------------------------------------------------------

class MockDirectory:
    """სინთეზური JSON ფაილი ({companies: [...], employees: [...]}) — ტესტებისა და დემოსთვის."""

    def __init__(self, path: str | Path, field_map: dict[str, str] | None = None):
        p = Path(path)
        self.path = p if p.is_absolute() else PROJECT_DIR / p
        self.field_map = field_map or DEFAULTS["employee_api"]["field_map"]
        self._data: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._data is None:
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise EmployeeApiUnavailable(f"mock file unreadable: {type(e).__name__}") from e
        return self._data

    def _records(self) -> list[EmployeeRecord]:
        return [record_from(r, self.field_map) for r in self._load().get("employees", [])]

    def _safe(self, fn: Callable[[], EmployeeLookup]) -> EmployeeLookup:
        try:
            return fn()
        except EmployeeApiUnavailable as e:
            return EmployeeLookup(API_UNAVAILABLE, detail=str(e))

    def find_by_personal_number(self, pn: str) -> EmployeeLookup:
        return self._safe(lambda: _pn_result(pn, [r for r in self._records() if r.personal_number == pn]))

    def find_by_id(self, employee_id: str) -> EmployeeLookup:
        return self._safe(lambda: _id_result(employee_id, self._records()))

    def search_by_name(self, first: str | None, last: str | None) -> EmployeeLookup:
        return self._safe(lambda: _name_result([r for r in self._records() if _name_matches(r, first, last)]))

    def companies(self) -> list[dict]:
        return list(self._load().get("companies", []))


# --- HTTP ------------------------------------------------------------------------------

class HttpDirectory:
    """კონფიგურირებადი HTTP API: base_url + endpoint შაბლონები, ავტორიზაცია, field_map, გვერდობრიობა, retry."""

    def __init__(self, api_cfg: dict[str, Any], *, client: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.cfg = api_cfg
        self.base_url = (api_cfg.get("base_url") or "").rstrip("/")
        self.endpoints: dict[str, str] = api_cfg.get("endpoints") or {}
        self.field_map: dict[str, str] = api_cfg.get("field_map") or DEFAULTS["employee_api"]["field_map"]
        self.pagination: dict[str, Any] = {**DEFAULTS["employee_api"]["pagination"], **(api_cfg.get("pagination") or {})}
        self.max_retries = int(api_cfg.get("max_retries", 3))
        self.backoff = float(api_cfg.get("retry_backoff_seconds", 2))
        self._sleep = sleep
        self._client = client or httpx.Client(timeout=float(api_cfg.get("timeout_seconds", 10)))

    # ავტორიზაცია: საიდუმლოება მხოლოდ keyring-იდან; მნიშვნელობა არსად იწერება
    def _auth(self) -> tuple[dict[str, str], httpx.Auth | None]:
        auth = self.cfg.get("auth") or {}
        method = auth.get("method", "none")
        if method == "none":
            return {}, None
        secret = secrets.get_secret(auth.get("secret_name", ""))
        if not secret:
            raise EmployeeApiUnavailable(f"secret not set for auth method {method}")
        if method == "bearer":
            return {"Authorization": f"Bearer {secret}"}, None
        if method == "basic":
            user, _, pwd = secret.partition(":")
            return {}, httpx.BasicAuth(user, pwd)
        if method == "api_key_header":
            return {auth.get("header") or "X-API-Key": secret}, None
        raise EmployeeApiUnavailable(f"unsupported auth method {method}")

    def _request(self, url: str, params: dict[str, Any] | None = None) -> Any:
        """GET retry-ით (ქსელი, 5xx, 429). 404 → None (არ მოიძებნა). სხვა 4xx → მიუწვდომელი."""
        headers, auth = self._auth()
        last = ""
        for attempt in range(self.max_retries + 1):
            if attempt:
                self._sleep(self.backoff * (2 ** (attempt - 1)))
            try:
                resp = self._client.get(url, params=params, headers=headers, auth=auth)
            except httpx.TransportError as e:   # timeout, connection error
                last = type(e).__name__
                continue
            except (httpx.HTTPError, httpx.InvalidURL) as e:
                raise EmployeeApiUnavailable(type(e).__name__) from e
            if resp.status_code == 429 or resp.status_code >= 500:
                last = f"http_{resp.status_code}"
                continue
            if resp.status_code == 404:
                return None
            if resp.status_code >= 400:
                raise EmployeeApiUnavailable(f"http_{resp.status_code}")
            try:
                return resp.json()
            except ValueError as e:
                raise EmployeeApiUnavailable("invalid json") from e
        raise EmployeeApiUnavailable(f"retries exhausted ({last})")

    def _items(self, body: Any) -> list[dict[str, Any]]:
        if body is None:
            return []
        if isinstance(body, list):
            return [x for x in body if isinstance(x, dict)]
        if isinstance(body, dict):
            items = _get_path(body, self.pagination.get("items_key") or "")
            if isinstance(items, list):
                return [x for x in items if isinstance(x, dict)]
            return [body]   # ერთი ჩანაწერი
        return []

    def _fetch(self, endpoint_key: str, **values: str) -> list[dict[str, Any]] | None:
        """endpoint-ის ყველა ჩანაწერი (გვერდობრივად). None — შესაძლებლობა კონფიგურირებული არ არის."""
        template = self.endpoints.get(endpoint_key) or ""
        if not template or not self.base_url:
            return None
        try:
            url = self.base_url + template.format(**{k: quote(v or "", safe="") for k, v in values.items()})
        except (KeyError, IndexError, ValueError) as e:
            raise EmployeeApiUnavailable(f"bad endpoint template {endpoint_key}: {type(e).__name__}") from e
        style = self.pagination.get("style", "none")
        if style == "page":
            size = int(self.pagination.get("page_size", 100))
            out: list[dict[str, Any]] = []
            for page in range(1, _MAX_PAGES + 1):
                items = self._items(self._request(url, {self.pagination["page_param"]: page,
                                                        self.pagination["size_param"]: size}))
                out += items
                if len(items) < size:
                    break
            return out
        if style == "next_url":
            out, next_url = [], url
            for _ in range(_MAX_PAGES):
                body = self._request(next_url)
                out += self._items(body)
                nxt = _get_path(body, self.pagination.get("next_key") or "") if isinstance(body, dict) else None
                if not nxt:
                    break
                next_url = urljoin(next_url, str(nxt))
            return out
        return self._items(self._request(url))

    def _lookup(self, endpoint_key: str, to_result: Callable[[list[EmployeeRecord]], EmployeeLookup],
                **values: str) -> EmployeeLookup:
        try:
            raw = self._fetch(endpoint_key, **values)
        except EmployeeApiUnavailable as e:
            log.warning("employee api unavailable (%s): %s", endpoint_key, e)
            return EmployeeLookup(API_UNAVAILABLE, detail=str(e))
        if raw is None:
            return EmployeeLookup(NOT_CHECKED, detail=f"endpoint_not_configured:{endpoint_key}")
        return to_result([record_from(r, self.field_map) for r in raw])

    def find_by_personal_number(self, pn: str) -> EmployeeLookup:
        return self._lookup("by_personal_number", lambda recs: _pn_result(pn, recs), pn=pn)

    def find_by_id(self, employee_id: str) -> EmployeeLookup:
        return self._lookup("by_id", lambda recs: _id_result(employee_id, recs), id=employee_id)

    def search_by_name(self, first: str | None, last: str | None) -> EmployeeLookup:
        name = " ".join(x for x in (first, last) if x)
        return self._lookup("search_by_name",
                            lambda recs: _name_result([r for r in recs if _name_matches(r, first, last)]),
                            first=first or "", last=last or "", name=name)

    def companies(self) -> list[dict]:
        """raises EmployeeApiUnavailable — CachedDirectory ქეშიდან პასუხობს."""
        return self._fetch("companies") or []


# --- ქეში -----------------------------------------------------------------------------

def _lookup_to_payload(lk: EmployeeLookup) -> dict[str, Any]:
    d = asdict(lk)
    d.pop("from_cache", None)
    d.pop("synced_at", None)
    return d


def _lookup_from_payload(d: dict[str, Any], synced_at: str) -> EmployeeLookup:
    rec = EmployeeRecord(**d["record"]) if d.get("record") else None
    cands = [EmployeeRecord(**c) for c in d.get("candidates") or []]
    return EmployeeLookup(d["status"], record=rec, candidates=cands, matched_by=d.get("matched_by"),
                          from_cache=True, synced_at=synced_at, detail=d.get("detail"))


class CachedDirectory:
    """წარმატებული პასუხები ინახება employee_cache-ში; API-ის მიუწვდომლობისას — ქეში (from_cache=True)."""

    _CACHEABLE = {VERIFIED, NOT_FOUND, AMBIGUOUS}

    def __init__(self, inner: EmployeeDirectory, repo):
        self.inner = inner
        self.repo = repo

    def _cached(self, key: str, fn: Callable[[], EmployeeLookup]) -> EmployeeLookup:
        lk = fn()
        if lk.status in self._CACHEABLE:
            self.repo.cache_put(key, _lookup_to_payload(lk))
            return lk
        if lk.status == API_UNAVAILABLE:
            row = self.repo.cache_get(key)
            if row:
                return _lookup_from_payload(json.loads(row["payload"]), row["synced_at"])
        return lk

    def find_by_personal_number(self, pn: str) -> EmployeeLookup:
        return self._cached(f"pn:{pn}", lambda: self.inner.find_by_personal_number(pn))

    def find_by_id(self, employee_id: str) -> EmployeeLookup:
        return self._cached(f"id:{employee_id}", lambda: self.inner.find_by_id(employee_id))

    def search_by_name(self, first: str | None, last: str | None) -> EmployeeLookup:
        key = f"name:{one_line(first or '')}|{one_line(last or '')}"
        return self._cached(key, lambda: self.inner.search_by_name(first, last))

    def companies(self) -> list[dict]:
        try:
            data = self.inner.companies()
        except EmployeeApiUnavailable:
            row = self.repo.cache_get("companies")
            return json.loads(row["payload"]) if row else []
        if data:
            self.repo.cache_put("companies", data)
        return data


def build_directory(cfg: Config, repo) -> EmployeeDirectory:
    api = cfg.section("employee_api")
    if not api.get("enabled"):
        return DisabledDirectory()
    if api.get("mode") == "http":
        inner: EmployeeDirectory = HttpDirectory(api)
    else:
        inner = MockDirectory(api.get("mock_file") or DEFAULTS["employee_api"]["mock_file"], api.get("field_map"))
    return CachedDirectory(inner, repo) if repo is not None else inner


# --- თანამშრომლის განსაზღვრა დოკუმენტისთვის -------------------------------------------

def _field(extraction: Extraction, key: str) -> str | None:
    f = extraction.fields.get(key)
    return _str_or_none(f.value) if f is not None else None


def lookup_employee(directory: EmployeeDirectory, extraction: Extraction) -> EmployeeLookup:
    """პ/ნ > employee_id > სახელი. რამდენიმე თანამშრომელი/კანდიდატი → ambiguous, ავტომატური მიბმის გარეშე.

    ვერნაპოვნი პ/ნ (შესაძლოა OCR შეცდომა) სხვა ჩანაწერით არ ჩანაცვლდება: სტატუსი რჩება `not_found`,
    ხოლო API-ის მიერ დაბრუნებული/სახელით ნაპოვნი პირები მხოლოდ კანდიდატებად ემატება.
    """
    if len(set(extraction.employee_personal_numbers)) > 1:
        return EmployeeLookup(AMBIGUOUS, detail="multiple_employees")
    pn = _field(extraction, "personal_number") or next(iter(extraction.employee_personal_numbers), None)
    emp_id = _field(extraction, "employee_id")
    first, last = _field(extraction, "first_name"), _field(extraction, "last_name")

    pn_result: EmployeeLookup | None = None
    if pn:
        pn_result = directory.find_by_personal_number(pn)
        if pn_result.status != NOT_FOUND:
            return pn_result
    if emp_id:
        r = directory.find_by_id(emp_id)
        if r.status != NOT_FOUND:
            return r
    if first or last:
        r = directory.search_by_name(first, last)
        if pn_result is None:
            return r
        # პ/ნ ვერ მოიძებნა — სახელით ნაპოვნი მხოლოდ კანდიდატია (მტკიცებულების გარეშე ჩანაცვლება დაუშვებელია)
        extra = ([r.record] if r.record else []) + r.candidates
        seen = {c.employee_id for c in pn_result.candidates}
        pn_result.candidates += [c for c in extra if c.employee_id not in seen][:_MAX_CANDIDATES]
    if pn_result is not None:
        pn_result.detail = pn_result.detail or "personal_number_not_found"
        return pn_result
    if emp_id:
        return EmployeeLookup(NOT_FOUND, detail="employee_id_not_found")
    return EmployeeLookup(NOT_CHECKED, detail="no_identifiers")
