"""ვერსიონირებადი JSON გვერდითი ფაილი (sidecar) — ატვირთვის მოდულის კონტრაქტი, DB-ის შიდა სტრუქტურისგან დამოუკიდებელი.

სქემა: `hrsorter/schema/document_sidecar.schema.json` (JSON Schema 2020-12). გვერდები sidecar-ში 1-დან.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from . import PIPELINE_VERSION, SIDECAR_SCHEMA_VERSION
from .config import Config
from .db import now_iso
from .models import DocumentOutcome, FieldValue

SCHEMA_PATH = Path(__file__).resolve().parent / "schema" / "document_sidecar.schema.json"
_PN = re.compile(r"\d{11}")


def page_ranges(pages: list[int]) -> list[dict[str, int]]:
    """0-დან ინდექსები → 1-დან დანომრილი, ჩათვლითი მიმდევრობითი დიაპაზონები."""
    out: list[dict[str, int]] = []
    for p in sorted(set(pages)):
        if out and out[-1]["end"] == p:   # end უკვე 1-დანაა: p (0-დან) == წინა end ნიშნავს მომდევნოს
            out[-1]["end"] = p + 1
        else:
            out.append({"start": p + 1, "end": p + 1})
    return out


def _page1(i: int | None) -> int | None:
    return None if i is None else i + 1


def _field(f: FieldValue) -> dict[str, Any]:
    return {"value": f.value, "raw": f.raw, "source": f.source, "page": _page1(f.page_index),
            "confidence": f.confidence, "evidence": f.evidence, "handwritten": f.handwritten,
            "candidates": list(f.candidates)}


def best_personal_number(outcome: DocumentOutcome) -> str | None:
    """API-ით პ/ნ-ით დადასტურებული, თორემ დოკუმენტიდან წაკითხული 11-ნიშნა ნომერი (წყარო — fields-ში)."""
    emp = outcome.employee
    if emp.status == "verified" and emp.matched_by == "personal_number" and emp.record \
            and emp.record.personal_number and _PN.fullmatch(emp.record.personal_number):
        return emp.record.personal_number
    if len(set(outcome.extraction.employee_personal_numbers)) > 1:
        return None
    f = outcome.extraction.fields.get("personal_number")
    return f.value if f and isinstance(f.value, str) and _PN.fullmatch(f.value) else None


def build_sidecar(outcome: DocumentOutcome, cfg: Config | None = None) -> dict[str, Any]:
    """`cfg` (არასავალდებულო) — მხოლოდ `document_type_name`-ისთვის (ტიპის სახელი outcome-ში არ ინახება)."""
    o, c, s, emp = outcome, outcome.classification, outcome.segment, outcome.employee
    doc_type = cfg.doc_type(c.type_id) if cfg else None
    doc_type_name = doc_type.name_ka if doc_type else None
    return {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "document_id": o.document_id,
        "job_id": o.job_id,
        "source_id": o.source_id,
        "source_sha256": o.source_sha256,
        "source_archive_path": o.source_path,
        "source_page_ranges": page_ranges(o.pages),
        "page_count": len(o.pages),
        "profile_id": o.profile_id,
        "mode": o.mode,
        "output_path": o.output_path,
        "output_sha256": o.output_sha256,
        "document_type_id": c.type_id,
        "document_type_name": doc_type_name,
        "company_id": o.company.company_id,
        "company_name": o.company.company_name,
        "company_source": o.company.source,
        "company_conflict": o.company.conflict,
        "company_unrecognized": o.company.unrecognized,
        "employee_id": emp.record.employee_id if emp.status == "verified" and emp.record else None,
        "personal_number": best_personal_number(o),
        "document_date": o.document_date.value,
        "document_date_raw": o.document_date.raw,
        "year": o.year.value,
        "fields": {k: _field(v) for k, v in o.extraction.fields.items()},
        "classification": {
            "type_id": c.type_id, "confidence": c.confidence, "margin": c.margin, "evidence": list(c.evidence),
            "scores": [{"type_id": t.type_id, "score": t.score, "matched_positive": list(t.matched_positive),
                        "matched_negative": list(t.matched_negative)} for t in c.scores],
        },
        "boundary": {"kind": s.kind.value if hasattr(s.kind, "value") else s.kind,
                     "confidence": s.boundary_confidence, "evidence": list(s.evidence),
                     "blank_pages": [p + 1 for p in s.blank_pages]},
        "signature_status": o.signature_status,
        "employee_verification": {
            "status": emp.status, "matched_by": emp.matched_by, "from_cache": emp.from_cache,
            "synced_at": emp.synced_at, "candidates": [r.employee_id for r in emp.candidates], "detail": emp.detail,
        },
        "duplicate": {"status": o.duplicate.status, "matches": [dict(m) for m in o.duplicate.matches]},
        "review_reasons": list(o.decision.review_reasons),
        "flags": list(o.decision.flags),
        "decision_explanation": list(o.decision.explanation),
        "processing_status": o.decision.outcome,
        "superseded_by": None,
        "content_fingerprint": o.content_fingerprint or None,
        "text_fingerprint": o.text_fingerprint or None,
        "provider": o.provider,
        "model_version": o.model_version,
        "pipeline_version": o.pipeline_version or PIPELINE_VERSION,
        "config_version": o.config_version,
        "processed_at": now_iso(),
        "attempt": o.attempt,
        "attempt_history": list(o.attempt_history),
        "error_codes": list(o.error_codes),
    }


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def validate_sidecar(data: dict[str, Any]) -> None:
    """raises jsonschema.ValidationError (პირველი შეცდომა)."""
    _validator().validate(data)
