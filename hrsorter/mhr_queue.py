"""MHR რიგი: ვალიდაცია და ატვირთვა `mhr_items` ცხრილის სტატუსების მანქანით (docs/MHR_DESIGN.md).

ყოველი ცვლილება `repo.mhr_transition`-ით — მხოლოდ მოსალოდნელი წინა სტატუსიდან (ატომური), ამიტომ
ორმაგი დადასტურება/sweep ორმაგ ატვირთვას ვერ გამოიწვევს. `uploading`-ში გაჭედილი ჩანაწერი (ავარია) ან
გაურკვეველი შედეგის ატვირთვა → `needs_check`; შემდეგ `client.find_by_external_id(document_id)`: ნაპოვნია →
`uploaded`; ცალსახად არ არის (None) → `approved` (ხელახლა ატვირთვა უსაფრთხოა — MHR `external_id`-ით
იდემპოტენტურია); შემოწმება ვერ მოხერხდა → რჩება `needs_check`. ჟურნალში (events) მხოლოდ document_id/ტიპი/სტატუსი.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import PIPELINE_VERSION, mhr_missing
from .db import now_iso
from .mhr_client import MhrClient, MhrUploadUncertain, build_mhr_client
from .mhr_models import MhrEmployee, MhrError, MhrStatus as S, MhrUnavailable, ValidationResult, clean_file_title

log = logging.getLogger(__name__)

SWEEP_LIMIT = 20                  # ერთ გავლაზე მაქსიმუმ ამდენი ვალიდაცია / ატვირთვა
MISSING_LIMIT = 200               # ერთ გავლაზე ამდენ „MHR-ში აკლია“ ჩანაწერს ეკითხება MHR-ს (თითო თანამშრომელზე ორი GET)
MISSING_CHECKED_KEY = "mhr_missing_checked"   # control: ბოლოს როდის შეეკითხა MHR-ს (UI-ში „ბოლოს შემოწმდა“)
STALE_UPLOADING_SECONDS = 600     # ამაზე დიდხანს `uploading` → ავარია → needs_check
UPLOAD_MAX_ATTEMPTS = 5           # MHR მიუწვდომლობისას ატვირთვის ცდები, შემდეგ upload_failed

ValidateFn = Callable[[Any, Any, str, MhrClient], ValidationResult]
ReviewFn = Callable[[Any, Any, str], Any]          # ai_review.review_document(repo, cfg, doc_id) -> AiReview
AI_REVIEW_STATUSES = (S.READY.value, S.FAILED.value)   # რომელ შედეგებზე ეშვება AI ანალიზი


# --- ოპერატორის მოქმედებები ----------------------------------------------------------------

def enqueue(repo, document_id: str, personal_number: str | None) -> bool:
    """ახალი ჩანაწერი (pending). უკვე არსებულს არ ცვლის."""
    return repo.mhr_enqueue(document_id, personal_number)


def approve(repo, document_id: str, who: str = "operator") -> bool:
    ok = repo.mhr_transition(document_id, (S.READY.value,), S.APPROVED.value, decided_by=who,
                             attempts=0, next_try=None, error=None)
    if ok:
        _event(repo, document_id, "approved")
    return ok


def approve_override(repo, document_id: str, who: str = "operator") -> bool:
    """შედარება ვერ გაიარა, მაგრამ ოპერატორმა სხვაობები ნახა და მაინც ატვირთვა გადაწყვიტა (მაგ. ცრუ განგაში
    ბეჭედზე). მხოლოდ თუ თანამშრომელი MHR-ში მოიძებნა — თორემ უცნობია, ვის ბარათზე აიტვირთოს."""
    row = repo.mhr_get(document_id)
    if row is None or not row["employee_id"]:
        return False
    ok = repo.mhr_transition(document_id, (S.FAILED.value,), S.APPROVED.value, decided_by=f"{who} (override)",
                             attempts=0, next_try=None, error=None)
    if ok:
        _event(repo, document_id, "approved despite failed validation (operator override)", level="warning")
    return ok


def reject(repo, document_id: str, who: str = "operator") -> bool:
    ok = repo.mhr_transition(document_id, (S.READY.value, S.APPROVED.value, S.FAILED.value, S.UPLOAD_FAILED.value,
                                           S.NEEDS_CHECK.value), S.REJECTED.value, decided_by=who)
    if ok:
        _event(repo, document_id, "rejected")
    return ok


def revalidate(repo, document_id: str) -> bool:
    """failed|unavailable|upload_failed|needs_check|rejected → pending (ხელახალი ვალიდაცია)."""
    ok = repo.mhr_transition(document_id, (S.FAILED.value, S.UNAVAILABLE.value, S.UPLOAD_FAILED.value,
                                           S.NEEDS_CHECK.value, S.REJECTED.value), S.PENDING.value,
                             attempts=0, next_try=None, error=None, ai_review=None)
    if ok:
        _event(repo, document_id, "revalidate")
    return ok


def revalidate_refused(repo) -> int:
    """ახალი ტოკენი შეინახეს: MHR-მა ვისზეც უარი თქვა (შედარება არ მომხდარა), თავიდან მოწმდება. რამდენი ფაილი."""
    rows = repo.q("SELECT document_id FROM mhr_items WHERE status=? AND error='MhrError'", (S.FAILED.value,))
    return sum(revalidate(repo, r["document_id"]) for r in rows)


def recheck_missing_now(repo) -> int:
    """„MHR-ში აკლია“: ოპერატორმა ფაილი MHR-ში ახლახან შექმნა და ლოდინი არ უნდა — შემდეგი გავლა (≤ წუთი) MHR-ს ყველაზე
    შეეკითხება. თვითონ შედარებას არ უშვებს: ის მხოლოდ იქ იწყება, სადაც MHR-ში რამე შეიცვალა. აბრუნებს ჩანაწერების რაოდენობას."""
    cur = repo.conn.execute("UPDATE mhr_items SET next_try=NULL WHERE status=? AND error IS NULL AND validation LIKE ?",
                            (S.FAILED.value, mhr_missing.SQL_LIKE))
    return cur.rowcount


def rerun_ai_review(repo, document_id: str) -> bool:
    """ready|failed: AI ანალიზის გასუფთავება — worker შემდეგ გავლაზე თავიდან გააკეთებს."""
    m = repo.mhr_get(document_id)
    if m is None or m["status"] not in AI_REVIEW_STATUSES:
        return False
    repo.mhr_set_ai_review(document_id, None)
    _event(repo, document_id, "ai_review requested")
    return True


def retry_upload(repo, document_id: str) -> bool:
    """upload_failed → approved (worker ხელახლა ატვირთავს)."""
    ok = repo.mhr_transition(document_id, (S.UPLOAD_FAILED.value,), S.APPROVED.value,
                             attempts=0, next_try=None, error=None)
    if ok:
        _event(repo, document_id, "retry_upload")
    return ok


# ხელით ატვირთვა ამ სტატუსებიდან არ იწყება — ატვირთვა უკვე მიდის ან დასრულდა
MANUAL_BLOCKED = (S.APPROVED.value, S.UPLOADING.value, S.UPLOADED.value, S.NEEDS_CHECK.value, S.SPLIT.value)
_MANUAL_FROM = tuple(s.value for s in S if s.value not in MANUAL_BLOCKED)


def approve_manual(repo, document_id: str, employee: MhrEmployee, personal_number: str,
                   who: str = "operator (manual)", type_id: str | None = None) -> str | None:
    """ხელით ატვირთვა: ოპერატორმა თანამშრომელი პირადი ნომრითა და სახელ-გვარით დაადასტურა — ავტომატური შედარება
    გამოიტოვება, ჩანაწერი პირდაპირ approved ხდება (ატვირთვა — `upload_now` ან worker). ტიპი უცნობ დოკუმენტს
    ოპერატორი არჩევს (`type_id`) — MHR-ის კატეგორია ტიპიდან დგება. აბრუნებს შეცდომის კოდს ან None."""
    doc = repo.get_document(document_id)
    if doc is None or not doc["active"]:
        return "document_not_found"
    if not doc["output_path"] or doc["purged_at"]:
        return "no_output_file"
    cur_type = doc["type_id"] or "unknown"
    if cur_type == "unknown":
        if not type_id or type_id == "unknown":
            return "type_unknown"
        repo.conn.execute("UPDATE documents SET type_id=?, updated_at=? WHERE id=?", (type_id, now_iso(), document_id))
        cur_type = type_id
    repo.mhr_enqueue(document_id, personal_number)
    row = repo.mhr_get(document_id)
    if row["status"] in MANUAL_BLOCKED:
        return f"already_{row['status']}"
    # წინა ვალიდაციის ანგარიში (ველები, სხვაობები) რჩება — მხოლოდ სტატუსი/თანამშრომელი/შეჯამება იცვლება
    v = json.loads(row["validation"]) if row["validation"] else {}
    v.update(status="manual", employee=asdict(employee), validated_at=now_iso(),
             summary=["ხელით ატვირთვა — ავტომატური შედარება გამოტოვებულია (ოპერატორის გადაწყვეტილება)"]
             + [x for x in (v.get("summary") or []) if not x.startswith("ხელით ატვირთვა")])
    ok = repo.mhr_transition(document_id, _MANUAL_FROM, S.APPROVED.value, decided_by=who,
                             employee_id=employee.employee_id, personal_number=personal_number,
                             validation=v, attempts=0, next_try=None, error=None)
    if not ok:
        return "state_changed"
    _event(repo, document_id, "approved manually (validation skipped)", cur_type, level="warning")
    return None


# --- MHR-ის ჩანაწერის გასწორება დადასტურებული პირადობიდან (დაბადების თარიღი, სქესი) ---------------------------------------

def open_suggestions(validation: dict[str, Any] | None) -> list[dict[str, Any]]:
    """შეთავაზებები, რომლებიც MHR-ში ჯერ არ ჩაწერილა (ელოდება ოპერატორს ან ჩაწერა ვერ მოხერხდა)."""
    return [s for s in (validation or {}).get("suggestions") or [] if s.get("state") != "applied"]


def _write_suggestion(client: MhrClient, employee_id: str, document_id: str, s: dict[str, Any], by: str) -> bool:
    """ერთი შეთავაზება MHR-ში იწერება; `s` ადგილზე იცვლება (state applied | failed | pending). შეცდომა მხოლოდ კოდად ინახება."""
    try:
        client.update_employee(str(employee_id), {s["field"]: s["value"]}, source=document_id)
    except MhrUnavailable:           # MHR ახლა არ პასუხობს — შეთავაზება რჩება, ოპერატორი ღილაკით გაიმეორებს
        s.update(state="pending", error="unavailable")
        return False
    except MhrError as e:
        s.update(state="failed", error=str(e)[:100])
        return False
    s.update(state="applied", applied_at=now_iso(), by=by, error=None)
    return True


def apply_suggestion(repo, cfg, document_id: str, field: str, client: MhrClient | None = None,
                     who: str = "operator") -> str | None:
    """ოპერატორის ღილაკი „MHR-ში გასწორება“: პირადობიდან წაკითხული მნიშვნელობა იწერება MHR-ის თანამშრომლის ჩანაწერში.
    აბრუნებს შეცდომის კოდს ან None. შეთავაზება მხოლოდ დადასტურებულ პირადობაზე არსებობს (`validation._identity_data`)."""
    row = repo.mhr_get(document_id)
    if row is None or not row["employee_id"]:
        return "not_found"
    v = json.loads(row["validation"] or "{}")
    s = next((x for x in open_suggestions(v) if x.get("field") == field), None)
    if s is None:
        return "no_suggestion"
    ok = _write_suggestion(client or build_mhr_client(cfg), row["employee_id"], document_id, s, who)
    repo.conn.execute("UPDATE mhr_items SET validation=? WHERE document_id=?", (json.dumps(v, ensure_ascii=False), document_id))
    _event(repo, document_id, f"mhr {field} {'updated' if ok else 'update failed'} ({who})", level="info" if ok else "warning")
    if ok and not open_suggestions(v):   # MHR გასწორდა — პირადობა თავიდან შედარდება (ახლა უკვე ემთხვევა)
        repo.mhr_transition(document_id, (S.READY.value, S.FAILED.value), S.PENDING.value,
                            attempts=0, next_try=None, error=None, ai_review=None)
    return None if ok else str(s.get("error") or "failed")


# --- „მხოლოდ AI“ რეჟიმი (`mhr.code_check_stops=false`) ------------------------------------------------------------------
# რასაც AI ვერ მოხსნის: ეს წაკითხვის საკითხი კი არა, ფაქტია (MHR-ში ფაილი არ არის, უკვე ატვირთულია, ვადა გასულია)
HARD_STOPS = frozenset({"original_contract", "contract_template", "already_uploaded", "id_expiry", "scan_hint"})
GATE_LIFTED, GATE_STOPPED = "lifted", "stopped"


def ai_decides(cfg) -> bool:
    """ფაილს AI აჩერებს და არა წესები: `mhr.code_check_stops` გამორთულია და AI ანალიზი აქტიურია. AI თუ არ არის
    (გამორთულია, ონლაინ სერვისი აკრძალულია), წესების შედეგი მოქმედებს — შეუმოწმებელი არაფერი გადის."""
    from . import ai_review
    return not cfg.section("mhr").get("code_check_stops", False) and ai_review.enabled(cfg)


def ai_gate(validation: dict[str, Any], review: dict[str, Any] | None) -> str | None:
    """„მხოლოდ AI“ რეჟიმში რა სტატუსი უნდა ჰქონდეს ჩანაწერს: ready | failed; None — წესებმა ვერც გაიარა და ვერც ჩააგდო
    (skipped, split, MHR-ის შეცდომა) და AI-ს აქ სათქმელი არაფერი აქვს.
    წესებმა გაიარა → AI „ატვირთე“ → ready, სხვა პასუხი → failed. წესებმა გააჩერა → ready მხოლოდ თუ AI ამბობს „ატვირთე“,
    გაჩერების ყოველი მიზეზი ცალკე ცრუ განგაშად შეაფასა და არცერთი მიზეზი ფაქტი არ არის (`HARD_STOPS`).
    AI-ის პასუხი არ არის ან შეცდომაა → წესების შედეგი."""
    from . import ai_review
    code = {"passed": S.READY.value, "failed": S.FAILED.value}.get(str(validation.get("status")))
    if code is None or validation.get("error"):
        return None
    verdict = (review or {}).get("verdict")
    if verdict not in ai_review.VERDICTS:
        return code
    if code == S.READY.value:
        return code if verdict == "upload" else S.FAILED.value
    stops = ai_review.stop_reasons(validation)

    def key(k: Any) -> str:
        return json.dumps(k, ensure_ascii=False, sort_keys=True)
    cleared = {key(f.get("key")) for f in (review or {}).get("findings") or [] if f.get("status") == "ok"}
    lifted = (verdict == "upload" and bool(stops) and not any(x.get("field") in HARD_STOPS for x in stops)
              and all(key(ai_review.finding_key(x)) in cleared for x in stops))
    return S.READY.value if lifted else code


def _apply_gate(repo, cfg, document_id: str, status: str, validation: dict[str, Any], review: dict[str, Any],
                counts: dict[str, int]) -> None:
    """AI-ის პასუხის შემდეგ: სტატუსი `ai_gate`-ის მიხედვით (ready ↔ failed). `review["gate"]` ინახავს, AI-მ რა შეცვალა
    წესების შედეგთან შედარებით: lifted — წესებმა გააჩერა, AI-მ მოხსნა; stopped — წესებმა გაიარა, AI-მ გააჩერა."""
    want = ai_gate(validation, review)
    if want is None:
        return
    passed = validation.get("status") == "passed"
    gate = GATE_STOPPED if passed and want == S.FAILED.value else GATE_LIFTED if not passed and want == S.READY.value else None
    if review.get("gate") != gate:
        review["gate"] = gate
        repo.mhr_set_ai_review(document_id, review)
    to = want
    fields: dict[str, Any] = {}
    if want == S.READY.value and not cfg.section("mhr").get("require_approval", True):
        to, fields = S.APPROVED.value, {"decided_by": "auto:ai", "attempts": 0, "next_try": None}   # დადასტურება გამორთულია
    if to != status and repo.mhr_transition(document_id, (status,), to, **fields):
        counts[to] = counts.get(to, 0) + 1
        _event(repo, document_id, f"{to} (AI: {review.get('verdict')}{', ' + gate if gate else ''})")


def _regate(repo, cfg, counts: dict[str, int]) -> None:
    """რეჟიმის გადართვა უკვე შემოწმებულ ფაილებზეც მოქმედებს: „მხოლოდ AI“ ჩართულია → ready/failed ჩანაწერს, რომელსაც AI-ის
    პასუხი უკვე აქვს, სტატუსი `ai_gate`-ით უსწორდება; გამორთულია → რაც AI-მ შეცვალა, წესების შედეგს უბრუნდება."""
    by_ai = ai_decides(cfg)
    q = ",".join("?" for _ in AI_REVIEW_STATUSES)
    for row in repo.q(f"SELECT document_id, status, validation, ai_review FROM mhr_items WHERE status IN ({q})"
                      " AND ai_review IS NOT NULL AND error IS NULL", AI_REVIEW_STATUSES):
        try:
            review, v = json.loads(row["ai_review"]), json.loads(row["validation"] or "{}")
        except ValueError:
            continue
        if by_ai:
            _apply_gate(repo, cfg, row["document_id"], row["status"], v, review, counts)
        elif review.get("gate"):
            review["gate"] = None
            repo.mhr_set_ai_review(row["document_id"], review)
            code = {"passed": S.READY.value, "failed": S.FAILED.value}.get(str(v.get("status")))
            if code and code != row["status"] and repo.mhr_transition(row["document_id"], (row["status"],), code):
                counts[code] = counts.get(code, 0) + 1
                _event(repo, row["document_id"], f"{code} (code check decides again)")


def upload_now(repo, cfg, document_id: str, client: MhrClient | None = None) -> str:
    """approved ჩანაწერის ატვირთვა ახლავე (worker-ის გავლის ლოდინის გარეშე). აბრუნებს შედეგად მიღებულ სტატუსს:
    uploaded | upload_failed | approved (MHR მიუწვდომელია — worker მოგვიანებით სცდის) | needs_check | …"""
    m = cfg.section("mhr")
    row = repo.mhr_get(document_id)
    if row is not None and row["status"] == S.APPROVED.value:
        if client is None:
            client = build_mhr_client(cfg)
        counts: dict[str, int] = defaultdict(int)
        _upload_one(repo, cfg, m, client, row, time.time() + float(m.get("retry_minutes", 10)) * 60, counts)
        row = repo.mhr_get(document_id)
    return row["status"] if row else "missing"


# --- worker-ის გავლა ----------------------------------------------------------------------------

def sweep(repo, cfg, client: MhrClient | None = None, validate_fn: ValidateFn | None = None,
          now: Callable[[], float] = time.time, review_fn: ReviewFn | None = None) -> dict[str, int]:
    """ერთი გავლა: stale uploading → needs_check; unavailable (ვადა გავიდა) → pending; „MHR-ში აკლია“ და იქ რამე შეიცვალა →
    pending; pending → ვალიდაცია; ready|failed AI ანალიზის გარეშე → AI ანალიზი; approved → ატვირთვა. აბრუნებს მთვლელებს.
    `mhr.enabled=false` → არაფერს აკეთებს."""
    counts = {k: 0 for k in ("requeued", "ready", "approved", "failed", "skipped", "unavailable", "uploaded",
                             "upload_retry", "upload_failed", "needs_check", "rejected", "recovered", "ai_reviewed", "split",
                             "rechecked")}
    m = cfg.section("mhr")
    if not m.get("enabled"):
        return counts
    t = now()
    retry_at = t + float(m.get("retry_minutes", 10)) * 60

    # (d) ავარია ატვირთვისას: შეიძლება უკვე ატვირთულია — ავტომატურად აღარ ვცდით
    for row in repo.q("SELECT document_id, updated_at FROM mhr_items WHERE status=?", (S.UPLOADING.value,)):
        if t - _ts(row["updated_at"]) > STALE_UPLOADING_SECONDS and repo.mhr_transition(
                row["document_id"], (S.UPLOADING.value,), S.NEEDS_CHECK.value, error="upload interrupted"):
            counts["needs_check"] += 1
            _event(repo, row["document_id"], "needs_check (stale uploading)", level="warning")

    # (a) MHR მიუწვდომელი იყო — ვადის გასვლის შემდეგ თავიდან
    for row in repo.q("SELECT document_id FROM mhr_items WHERE status=? AND (next_try IS NULL OR next_try <= ?)",
                      (S.UNAVAILABLE.value, t)):
        if repo.mhr_transition(row["document_id"], (S.UNAVAILABLE.value,), S.PENDING.value):
            counts["requeued"] += 1

    if client is None:
        client = build_mhr_client(cfg)

    # (e) needs_check: აიტვირთა თუ არა — external_id-ით შემოწმება (ცდების ლიმიტამდე; შემდეგ — მხოლოდ ოპერატორი)
    for row in repo.q("SELECT * FROM mhr_items WHERE status=? AND (next_try IS NULL OR next_try <= ?) AND attempts < ?"
                      " ORDER BY updated_at LIMIT ?", (S.NEEDS_CHECK.value, t, UPLOAD_MAX_ATTEMPTS, SWEEP_LIMIT)):
        _resolve_needs_check(repo, client, row, retry_at, counts)

    # (a2) MHR-ში ფაილი/თანამშრომელი აკლდა: გაჩნდა? — მაშინ ქვემოთ, ამავე გავლაზე, თავიდან შედარდება
    _recheck_missing(repo, cfg, m, client, t, counts)

    # (b) ვალიდაცია
    for row in repo.q("SELECT * FROM mhr_items WHERE status=? ORDER BY updated_at LIMIT ?",
                      (S.PENDING.value, SWEEP_LIMIT)):
        _validate_one(repo, cfg, m, client, validate_fn, row, retry_at, counts)

    # (b2) AI ანალიზი — წესების შედეგი უკვე ბაზაშია (UI-ში ჩანს), Opus-ის „მეორე აზრი“ ემატება
    _ai_review_pending(repo, cfg, review_fn, counts)
    # (b3) ვინ აჩერებს ფაილს (`mhr.code_check_stops`): პარამეტრის შეცვლა უკვე შემოწმებულ ფაილებზეც აისახება
    _regate(repo, cfg, counts)

    # (c) ატვირთვა
    for row in repo.q("SELECT * FROM mhr_items WHERE status=? AND (next_try IS NULL OR next_try <= ?)"
                      " ORDER BY updated_at LIMIT ?", (S.APPROVED.value, t, SWEEP_LIMIT)):
        _upload_one(repo, cfg, m, client, row, retry_at, counts)
    return counts


def _recheck_missing(repo, cfg, m: dict[str, Any], client: MhrClient, t: float, counts: dict[str, int]) -> None:
    """„MHR-ში აკლია“ ჩანაწერები: MHR-ს ეკითხება (მხოლოდ კითხვა — თანამშრომელი და მისი ფაილების სია), შეიცვალა თუ არა
    რამე იმის შემდეგ, რაც შედარებამ ნახა (`validation.mhr_snapshot`). შეიცვალა → failed → pending, ოპერატორის დაჭერის გარეშე;
    არ შეიცვალა → შემდეგი კითხვა `mhr.missing_recheck_minutes` წუთში. AI ანალიზი არ იშლება: თუ ახალი შედარება იგივეს
    იტყვის, `_validate_one` ძველს დატოვებს. ძველი ვერსიის ჩანაწერს ანაბეჭდი არ აქვს — ის ერთხელ მოწმდება თავიდან."""
    minutes = float(m.get("missing_recheck_minutes") or 0)
    if minutes <= 0:
        return
    rows = repo.q("SELECT document_id, personal_number, validation, attempts FROM mhr_items WHERE status=? AND error IS NULL"
                  " AND validation LIKE ? AND (next_try IS NULL OR next_try <= ?) ORDER BY updated_at LIMIT ?",
                  (S.FAILED.value, mhr_missing.SQL_LIKE, t, MISSING_LIMIT))
    by_person: dict[str, list[tuple[Any, dict[str, Any]]]] = defaultdict(list)
    for row in rows:
        v = json.loads(row["validation"] or "{}")
        if row["personal_number"] and mhr_missing.missing_kind(v):
            by_person[row["personal_number"]].append((row, v))
    if not by_person:
        return
    from .validation import mhr_snapshot
    later = t + minutes * 60
    for pn, items in by_person.items():
        try:
            emp = client.get_employee(pn)
            now_in_mhr = mhr_snapshot(cfg, emp, client.list_documents(emp.employee_id) if emp else [])
        except (MhrUnavailable, MhrError) as e:   # MHR ახლა არ პასუხობს — ჩანაწერი ადგილზე რჩება, მოგვიანებით ისევ
            log.info("missing-in-MHR recheck postponed: %s", type(e).__name__)
            now_in_mhr = None
        for row, v in items:
            doc_id, seen = row["document_id"], v.get("mhr_snapshot")
            changed = now_in_mhr is not None and (seen != now_in_mhr if seen else row["attempts"] == 0)
            if changed and repo.mhr_transition(doc_id, (S.FAILED.value,), S.PENDING.value, attempts=1, next_try=None):
                counts["rechecked"] += 1
                _event(repo, doc_id, "recheck (MHR changed)")
            elif not changed:   # updated_at-ს არ ვეხებით: სიაში რიგი და „როდის შემოწმდა“ არ უნდა იცვლებოდეს ყოველ კითხვაზე
                repo.conn.execute("UPDATE mhr_items SET next_try=? WHERE document_id=? AND status=?",
                                  (later, doc_id, S.FAILED.value))
    repo.set_control(MISSING_CHECKED_KEY, now_iso())


def _same_outcome(old_validation: str | None, res: ValidationResult) -> bool:
    """ახალი შედარება იმავეს ამბობს, რასაც ძველი (იგივე შემოწმებები და სხვაობები) — AI ანალიზის თავიდან გაკეთება ზედმეტი ხარჯია.
    შენიშვნის ტექსტი არ ითვლება: ის ვერსიიდან ვერსიამდე იცვლება, შედეგი კი იგივე რჩება."""
    try:
        old = json.loads(old_validation or "{}")
    except ValueError:
        return False

    def essence(v: dict[str, Any]) -> tuple:
        return (v.get("status"),
                [(c.get("field"), c.get("status"), c.get("blocking")) for c in v.get("checks") or []],
                [(d.get("page"), d.get("kind"), d.get("significant"), d.get("original"), d.get("scanned")) for d in v.get("diffs") or []])
    return essence(old) == essence(res.to_dict())


def _ai_review_pending(repo, cfg, review_fn: ReviewFn | None, counts: dict[str, int]) -> None:
    from . import ai_review
    if not ai_review.enabled(cfg):
        return
    limit = int(ai_review.settings(cfg).get("per_sweep") or 3)
    q = ",".join("?" for _ in AI_REVIEW_STATUSES)
    # error (MhrError/unauthenticated …) — შედარება არ შესრულებულა, გასაანალიზებელი არაფერია; revalidate-ის შემდეგ ეშვება
    for row in repo.q(f"SELECT document_id, status FROM mhr_items WHERE status IN ({q}) AND ai_review IS NULL"
                      f" AND error IS NULL ORDER BY updated_at LIMIT ?", (*AI_REVIEW_STATUSES, limit)):
        doc_id = row["document_id"]
        try:
            res = (review_fn or ai_review.review_document)(repo, cfg, doc_id)
            data = res.to_dict() if hasattr(res, "to_dict") else dict(res)
        except Exception as e:   # შედეგი არასდროს არ იკარგება უსასრულო ციკლში — შეცდომაც იწერება
            log.warning("ai review of %s failed: %s", doc_id, type(e).__name__)
            data = ai_review.AiReview("error", error=type(e).__name__, reviewed_at=now_iso()).to_dict()
        m = repo.mhr_get(doc_id)
        if m is None or m["status"] != row["status"]:   # ამასობაში სტატუსი შეიცვალა (მაგ. ოპერატორმა ატვირთა)
            continue
        repo.mhr_set_ai_review(doc_id, data)
        counts["ai_reviewed"] += 1
        _event(repo, doc_id, f"ai_review: {data.get('verdict')} ({data.get('confidence')})"
               + (f" — {data.get('error')}" if data.get("error") else ""),
               level="warning" if data.get("verdict") == "error" else "info")
        if ai_decides(cfg):   # „მხოლოდ AI“ რეჟიმი: ფაილი ჩერდება ან გადის AI-ის პასუხით
            _apply_gate(repo, cfg, doc_id, m["status"], json.loads(m["validation"] or "{}"), data, counts)


def _find_upload(client: MhrClient, row) -> tuple[str, str | None]:
    """("found", remote_id) | ("absent", None) | ("unknown", None) — შემოწმება ვერ მოხერხდა / სხვა თანამშრომელზეა."""
    try:
        ref = client.find_by_external_id(row["document_id"])
    except (MhrUnavailable, MhrError):
        return "unknown", None
    if ref is None:
        return "absent", None
    owner = (ref.raw or {}).get("employee_id")
    if owner is not None and row["employee_id"] and str(owner) != str(row["employee_id"]):
        return "unknown", None
    return "found", ref.doc_id


def _resolve_needs_check(repo, client: MhrClient, row, retry_at: float, counts: dict[str, int]) -> None:
    doc_id = row["document_id"]
    state, remote_id = _find_upload(client, row)
    nc = (S.NEEDS_CHECK.value,)
    if state == "found":
        if repo.mhr_transition(doc_id, nc, S.UPLOADED.value, remote_id=remote_id, uploaded_at=now_iso(), error=None):
            counts["recovered"] += 1
            _event(repo, doc_id, "uploaded (confirmed by external_id)")
    elif state == "absent":   # ცალსახად არ აიტვირთა — ხელახლა ატვირთვა (სერვერი external_id-ით იდემპოტენტურია)
        if repo.mhr_transition(doc_id, nc, S.APPROVED.value, next_try=None, error="not uploaded — retry"):
            counts["upload_retry"] += 1
            _event(repo, doc_id, "needs_check → approved (not found by external_id)")
    else:
        repo.mhr_transition(doc_id, nc, S.NEEDS_CHECK.value, next_try=retry_at)


def _validate_one(repo, cfg, m: dict[str, Any], client: MhrClient, validate_fn: ValidateFn | None,
                  row, retry_at: float, counts: dict[str, int]) -> None:
    doc_id = row["document_id"]
    pending = (S.PENDING.value,)
    doc = repo.get_document(doc_id)
    if doc is None or not doc["active"]:
        if repo.mhr_transition(doc_id, pending, S.REJECTED.value, error="document superseded"):
            counts["rejected"] += 1
            _event(repo, doc_id, "rejected (document superseded)")
        return
    type_id = doc["type_id"] or "unknown"
    if m.get("upload_type_ids") and not _uploadable(m, type_id):   # უცნობი ტიპი MHR-ის ფაილებს შეედრება — დამთხვევისას ტიპი იქიდან დგება
        res = ValidationResult(status="skipped", summary=["დოკუმენტის ეს ტიპი MHR-ზე არ იტვირთება"],
                               validated_at=now_iso())
        if repo.mhr_transition(doc_id, pending, S.SKIPPED.value, validation=res.to_dict()):
            counts["skipped"] += 1
            _event(repo, doc_id, "skipped (type not uploadable)", type_id)
        return
    try:
        if validate_fn is None:
            from .validation import validate_document   # აგენტი B-ის მოდული — lazy import
            validate_fn = validate_document
        res = validate_fn(repo, cfg, doc_id, client)
    except Exception as e:   # შეტყობინება შეიძლება პირად მონაცემს შეიცავდეს — ვინახავთ მხოლოდ ტიპს
        log.warning("mhr validation of %s failed: %s", doc_id, type(e).__name__)
        if repo.mhr_transition(doc_id, pending, S.UNAVAILABLE.value, error=type(e).__name__,
                               attempts=row["attempts"] + 1, next_try=retry_at):
            counts["unavailable"] += 1
            _event(repo, doc_id, f"unavailable ({type(e).__name__})", type_id, level="warning")
        return
    # AI ანალიზი აქამდე მხოლოდ თავისით გადამოწმებისას აღწევს (ოპერატორის „ხელახლა შედარება“ მას წინასწარ შლის):
    # MHR-ში სხვა რამ შეიცვალა და შედეგი იგივე დარჩა → ძველი ანალიზი ძალაშია
    keep_ai = row["ai_review"] if row["ai_review"] and _same_outcome(row["validation"], res) else None
    # დადასტურებული პირადობა: MHR-ში ცარიელი სქესი თავისით იწერება (მომხმარებელმა ასე ითხოვა); სხვა შეთავაზებას ოპერატორი ადასტურებს
    if res.employee is not None and res.status in ("passed", "failed"):
        for s in res.suggestions:
            if s.get("auto") and s.get("state") == "pending":
                ok = _write_suggestion(client, res.employee.employee_id, doc_id, s, "auto")
                _event(repo, doc_id, f"mhr {s.get('field')} {'written' if ok else 'write failed'} (auto)", type_id,
                       level="info" if ok else "warning")
    validation = res.to_dict()
    fields: dict[str, Any] = {"validation": validation, "error": res.error, "ai_review": keep_ai,
                              "employee_id": res.employee.employee_id if res.employee else None}
    by_ai = ai_decides(cfg)
    if res.status == "passed":
        dt = cfg.doc_type(type_id)

        def matched(field: str) -> bool:
            return any(c.field == field and c.status == "match" for c in res.checks)
        # პირადობა MHR-ს „ერთი ერთში“ ემთხვევა / ანკეტა MHR-ს და პირადობას ემთხვევა — მომხმარებელმა ასე ითხოვა: დადასტურების გარეშე.
        # ღია შეთავაზება (MHR-ში სხვა დაბადების თარიღი/სქესი) ოპერატორმა უნდა ნახოს — მაშინ ფაილი დადასტურებას ელოდება
        auto = ("identity" if dt and dt.identity and m.get("identity_auto_upload", True) and matched("identity")
                and not open_suggestions(validation) else
                "employee_form" if dt and dt.employee_form and m.get("employee_form_auto_upload", True) and matched("employee_form")
                else None)
        # „მხოლოდ AI“ რეჟიმში დადასტურების გარეშე ატვირთვაც AI-ის პასუხს ელოდება (`_apply_gate` გადაიყვანს approved-ში)
        to = S.APPROVED if auto or (not m.get("require_approval", True) and not by_ai) else S.READY
        fields.update(attempts=0, next_try=None)
        if auto:
            fields["decided_by"] = f"auto:{auto}"
    elif res.status == "split":   # ნაწილები უკვე რიგშია — თითოეული შემდეგ გავლაზე ცალკე მოწმდება
        to = S.SPLIT
    elif res.status in ("failed", "skipped"):
        to = S(res.status)
    else:   # unavailable (ან უცნობი სტატუსი) — მოგვიანებით ხელახლა
        to = S.UNAVAILABLE
        fields.update(attempts=row["attempts"] + 1, next_try=retry_at)
    if by_ai and keep_ai and to in (S.READY, S.FAILED):   # იგივე შედეგი, AI-ის ძველი პასუხი ძალაშია → სტატუსიც მისი
        want = ai_gate(validation, json.loads(keep_ai))
        if want == S.READY.value and not m.get("require_approval", True):
            to, fields["decided_by"] = S.APPROVED, "auto:ai"
        elif want:
            to = S(want)
    if repo.mhr_transition(doc_id, pending, to.value, **fields):
        counts[to.value] += 1
        _event(repo, doc_id, to.value, type_id, level="warning" if to == S.UNAVAILABLE else "info")


def _signed_original(row) -> dict[str, Any] | None:
    """ვალიდაციის ორიგინალი, თუ ის MHR-ის დაგენერირებული ფაილია და იმავე თანამშრომლისაა — სკანი მისი ხელმოწერილი ასლია."""
    v = json.loads(row["validation"] or "{}") if row["validation"] else {}
    orig = v.get("original") or {}
    oraw = orig.get("raw") or {}
    if orig.get("doc_id") and oraw.get("kind") == "generated" and \
            str(oraw.get("employee_id") or row["employee_id"]) == str(row["employee_id"]):
        return orig
    return None


def _upload_meta(repo, cfg, row, doc, type_id: str, sha: str) -> dict[str, Any]:
    """MHR-ის upload ველები (კონტრაქტის სახელებით): external_id = ჩვენი document_id, sha256 — გასაგზავნი ბაიტების,
    signed_copy_of — ვალიდაციის ორიგინალი (kind generated, იგივე თანამშრომელი)."""
    t = cfg.doc_type(type_id)
    v = json.loads(row["validation"] or "{}") if row["validation"] else {}
    orig = _signed_original(row)
    signed_copy_of = str(orig["doc_id"]) if orig else None
    name = clean_file_title(orig.get("filename")) if orig else ""   # ფაილის სახელი უფრო ზუსტია: „… თანამდებობრივი ინსტრუქცია"
    src = repo.get_source(doc["source_id"])
    date = doc["document_date"] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", doc["document_date"] or "") else None
    number = (doc["doc_number"] or "").strip()
    title = f"{name or (t.name_ka if t else type_id)} — ხელმოწერილი სკანი"
    if t is not None and t.identity:   # სხვა კომპიუტერიც სათაურით ხედავს, რომ პირადობა MHR-ს ემთხვევა (validation.verified_identity)
        from .validation import IDENTITY_TITLE, PASSPORT_TITLE
        verified = any(c.get("field") == "identity" and c.get("status") == "match" for c in v.get("checks", []))
        passport = (v.get("facts") or {}).get("id_kind") == "passport"
        title = (PASSPORT_TITLE if passport else IDENTITY_TITLE) if verified else ("პასპორტი" if passport else t.name_ka)
    return {
        "external_id": doc["id"],
        "sha256": sha,
        "title": title[:255],
        "document_date": date,
        "document_number": number if 0 < len(number) <= 100 else None,
        "signed_copy_of": signed_copy_of,
        "metadata": json.dumps({"type_id": type_id, "source_sha256": src["sha256"] if src else None,
                                "validation_status": v.get("status"), "pipeline_version": PIPELINE_VERSION},
                               ensure_ascii=False, separators=(",", ":")),
    }


def _upload_one(repo, cfg, m: dict[str, Any], client: MhrClient, row, retry_at: float, counts: dict[str, int]) -> None:
    doc_id = row["document_id"]
    up = (S.UPLOADING.value,)
    # ჯერ ატომური approved → uploading: მეორე გავლა/პროცესი ამავე ჩანაწერს ვეღარ აიღებს
    if not repo.mhr_transition(doc_id, (S.APPROVED.value,), S.UPLOADING.value):
        return
    doc = repo.get_document(doc_id)
    if doc is None or not doc["active"]:
        repo.mhr_transition(doc_id, up, S.REJECTED.value, error="document superseded")
        counts["rejected"] += 1
        _event(repo, doc_id, "rejected (document superseded)")
        return
    type_id = doc["type_id"] or "unknown"

    def fail(error: str) -> None:
        repo.mhr_transition(doc_id, up, S.UPLOAD_FAILED.value, error=error)
        counts["upload_failed"] += 1
        _event(repo, doc_id, f"upload_failed ({error})", type_id, level="warning")

    if not row["employee_id"]:
        return fail("no employee id")
    if not doc["output_path"]:
        return fail("no output file")
    try:
        pdf = Path(doc["output_path"]).read_bytes()
    except OSError as e:
        return fail(f"file unreadable ({type(e).__name__})")
    sha = hashlib.sha256(pdf).hexdigest()
    if doc["output_sha256"] and doc["output_sha256"] != sha:
        return fail("output file changed")
    meta = _upload_meta(repo, cfg, row, doc, type_id, sha)
    orig = _signed_original(row)   # ხელმოწერილი ასლი ორიგინალის კატეგორიაში ჯდება
    category = (orig or {}).get("category") or (m.get("categories") or {}).get(type_id, type_id)
    try:
        remote_id = client.upload(row["employee_id"], pdf, filename=Path(doc["output_path"]).name,
                                  category=category, meta=meta)
    except MhrUploadUncertain as e:
        # შეიძლება აიტვირთა: ნაპოვნია → uploaded; სხვაგვარად needs_check (მოთხოვნა შეიძლება ჯერ კიდევ მუშავდება —
        # „არ არის" ახლა ცალსახა არ არის), შემდეგი შემოწმება retry_at-ზე
        state, found_id = _find_upload(client, row)
        if state == "found":
            repo.mhr_transition(doc_id, up, S.UPLOADED.value, remote_id=found_id, uploaded_at=now_iso(), error=None)
            counts["uploaded"] += 1
            _event(repo, doc_id, "uploaded (confirmed by external_id)", type_id)
            return
        repo.mhr_transition(doc_id, up, S.NEEDS_CHECK.value, error=str(e)[:500], attempts=row["attempts"] + 1,
                            next_try=retry_at)
        counts["needs_check"] += 1
        _event(repo, doc_id, "needs_check (upload result unknown)", type_id, level="warning")
        return
    except MhrUnavailable:
        attempts = row["attempts"] + 1
        if attempts >= UPLOAD_MAX_ATTEMPTS:
            return fail(f"MHR unavailable ({attempts} attempts)")
        repo.mhr_transition(doc_id, up, S.APPROVED.value, attempts=attempts, next_try=retry_at,
                            error="MHR unavailable")
        counts["upload_retry"] += 1
        _event(repo, doc_id, "upload postponed (MHR unavailable)", type_id, level="warning")
        return
    except MhrError as e:   # ჩვენი კლიენტის ტექსტი ტექნიკურია (მაგ. http_400)
        return fail(str(e)[:500])
    except Exception as e:
        log.warning("mhr upload of %s failed: %s", doc_id, type(e).__name__)
        return fail(type(e).__name__)
    repo.mhr_transition(doc_id, up, S.UPLOADED.value, remote_id=remote_id, uploaded_at=now_iso(), error=None)
    counts["uploaded"] += 1
    _event(repo, doc_id, "uploaded", type_id)


# --- დამხმარეები ----------------------------------------------------------------------------------

def _uploadable(m: dict[str, Any], type_id: str) -> bool:
    ids = m.get("upload_type_ids") or []
    return type_id in ids if ids else type_id != "unknown"


def _ts(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _event(repo, document_id: str, what: str, type_id: str | None = None, level: str = "info") -> None:
    repo.log_event("mhr", f"{document_id}{f' [{type_id}]' if type_id else ''}: {what}", level=level)
