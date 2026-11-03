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

from . import PIPELINE_VERSION
from .db import now_iso
from .mhr_client import MhrClient, MhrUploadUncertain, build_mhr_client
from .mhr_models import MhrEmployee, MhrError, MhrStatus as S, MhrUnavailable, ValidationResult

log = logging.getLogger(__name__)

SWEEP_LIMIT = 20                  # ერთ გავლაზე მაქსიმუმ ამდენი ვალიდაცია / ატვირთვა
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
MANUAL_BLOCKED = (S.APPROVED.value, S.UPLOADING.value, S.UPLOADED.value, S.NEEDS_CHECK.value)
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
    """ერთი გავლა: stale uploading → needs_check; unavailable (ვადა გავიდა) → pending; pending → ვალიდაცია;
    ready|failed AI ანალიზის გარეშე → AI ანალიზი; approved → ატვირთვა. აბრუნებს მთვლელებს.
    `mhr.enabled=false` → არაფერს აკეთებს."""
    counts = {k: 0 for k in ("requeued", "ready", "approved", "failed", "skipped", "unavailable", "uploaded",
                             "upload_retry", "upload_failed", "needs_check", "rejected", "recovered", "ai_reviewed")}
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

    # (b) ვალიდაცია
    for row in repo.q("SELECT * FROM mhr_items WHERE status=? ORDER BY updated_at LIMIT ?",
                      (S.PENDING.value, SWEEP_LIMIT)):
        _validate_one(repo, cfg, m, client, validate_fn, row, retry_at, counts)

    # (b2) AI ანალიზი — წესების შედეგი უკვე ბაზაშია (UI-ში ჩანს), Opus-ის „მეორე აზრი“ ემატება
    _ai_review_pending(repo, cfg, review_fn, counts)

    # (c) ატვირთვა
    for row in repo.q("SELECT * FROM mhr_items WHERE status=? AND (next_try IS NULL OR next_try <= ?)"
                      " ORDER BY updated_at LIMIT ?", (S.APPROVED.value, t, SWEEP_LIMIT)):
        _upload_one(repo, cfg, m, client, row, retry_at, counts)
    return counts


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
    if not _uploadable(m, type_id):
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
    fields: dict[str, Any] = {"validation": res.to_dict(), "error": res.error, "ai_review": None,
                              "employee_id": res.employee.employee_id if res.employee else None}
    if res.status == "passed":
        to = S.APPROVED if not m.get("require_approval", True) else S.READY
        fields.update(attempts=0, next_try=None)
    elif res.status in ("failed", "skipped"):
        to = S(res.status)
    else:   # unavailable (ან უცნობი სტატუსი) — მოგვიანებით ხელახლა
        to = S.UNAVAILABLE
        fields.update(attempts=row["attempts"] + 1, next_try=retry_at)
    if repo.mhr_transition(doc_id, pending, to.value, **fields):
        counts[to.value] += 1
        _event(repo, doc_id, to.value, type_id, level="warning" if to == S.UNAVAILABLE else "info")


def _upload_meta(repo, cfg, row, doc, type_id: str, sha: str) -> dict[str, Any]:
    """MHR-ის upload ველები (კონტრაქტის სახელებით): external_id = ჩვენი document_id, sha256 — გასაგზავნი ბაიტების,
    signed_copy_of — ვალიდაციის ორიგინალი (kind generated, იგივე თანამშრომელი)."""
    t = cfg.doc_type(type_id)
    v = json.loads(row["validation"] or "{}") if row["validation"] else {}
    orig = v.get("original") or {}
    oraw = orig.get("raw") or {}
    signed_copy_of = None
    if orig.get("doc_id") and oraw.get("kind") == "generated" and \
            str(oraw.get("employee_id") or row["employee_id"]) == str(row["employee_id"]):
        signed_copy_of = str(orig["doc_id"])
    src = repo.get_source(doc["source_id"])
    date = doc["document_date"] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", doc["document_date"] or "") else None
    number = (doc["doc_number"] or "").strip()
    return {
        "external_id": doc["id"],
        "sha256": sha,
        "title": f"{t.name_ka if t else type_id} — ხელმოწერილი სკანი"[:255],
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
    category = (m.get("categories") or {}).get(type_id, type_id)
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
