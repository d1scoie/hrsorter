"""სანდოობის წესები: ავტომატური შენახვა / შემოთავაზება / გადამოწმება.

გადაწყვეტილება ეყრდნობა მხოლოდ წესებზე დაფუძნებულ შეფასებებს (საზღვარი, ტიპი, კონფლიქტები, დუბლიკატი,
რეჟიმი, წაკითხვადობა). მოდელის თვითშეფასება ცალკე არ გამოიყენება. ბლოკირებადი მიზეზი → needs_review;
დანარჩენი (წელი, ხელმოწერა, თანამშრომლის ველები და სხვ.) მხოლოდ flag-ია და სწორ ტიპით გადარჩევას არ აფერხებს.
"""
from __future__ import annotations

from .config import Config, DocType
from .models import (Classification, CompanyResolution, Decision, DocumentStatus, DuplicateInfo, EmployeeLookup,
                     Extraction, FieldSource, Flag, PageText, ReviewReason, Segment, SegmentKind, SignatureStatus)
from .resolve import resolve_year

R = ReviewReason


def decide(*, segment: Segment, classification: Classification, doc_type: DocType | None,
           company: CompanyResolution, extraction: Extraction, employee: EmployeeLookup,
           duplicate: DuplicateInfo, mode_mismatch: bool, cfg: Config,
           pages: list[PageText] | None = None) -> Decision:
    """`pages` (არასავალდებულო) — სეგმენტის გვერდები OCR-ის წაკითხვადობის შესამოწმებლად."""
    th = cfg.thresholds
    reasons: list[str] = []
    flags: list[str] = []
    expl: list[str] = []

    def block(reason: ReviewReason, why: str) -> None:
        if reason.value not in reasons:
            reasons.append(reason.value)
            expl.append(f"{reason.value}: {why}")

    def flag(f: Flag) -> None:
        if f.value not in flags:
            flags.append(f.value)

    # --- ბლოკირებადი მიზეზები ---------------------------------------------------------
    if segment.kind == SegmentKind.UNCERTAIN or segment.boundary_confidence < th["boundary_auto"]:
        block(R.UNCERTAIN_BOUNDARY, f"boundary {segment.boundary_confidence:.2f} < {th['boundary_auto']}"
              if segment.kind != SegmentKind.UNCERTAIN else "segment kind uncertain")
    if doc_type is None or classification.type_id == "unknown":
        block(R.UNKNOWN_TYPE, "document type not recognized")
    elif classification.confidence < th["type_auto"] or classification.margin < th["type_min_margin"]:
        block(R.LOW_TYPE_CONFIDENCE, f"type confidence {classification.confidence:.2f} (min {th['type_auto']}),"
              f" margin {classification.margin:.2f} (min {th['type_min_margin']})")
    if company.conflict:
        block(R.COMPANY_CONFLICT, "conflicting companies without a clear employer")
    if company.unrecognized:
        block(R.COMPANY_UNRECOGNIZED, "organization not in configured companies")
    if len(set(extraction.employee_personal_numbers)) > 1 or employee.detail == "multiple_employees":
        block(R.MULTIPLE_EMPLOYEES, "more than one employee personal number in document")
    if duplicate.status in ("exact", "possible"):
        block(R.POSSIBLE_DUPLICATE, f"{duplicate.status} duplicate of "
              + ", ".join(m["document_id"] for m in duplicate.matches[:3]))
    if mode_mismatch:
        block(R.MODE_MISMATCH, "input mode does not match detected content")
    readable = [p for p in pages or [] if not p.is_blank]
    low = [p for p in readable if p.ocr_quality < th["ocr_min_quality"]]
    if readable and len(low) == len(readable):
        block(R.OCR_UNREADABLE, f"all pages below ocr_min_quality {th['ocr_min_quality']}")
    elif low:
        flag(Flag.LOW_OCR_QUALITY)

    # --- არაბლოკირებადი flag-ები --------------------------------------------------------
    document_date, year = resolve_year(extraction)
    if year.value is None:
        flag(Flag.YEAR_UNKNOWN)
    if document_date.value is None:
        flag(Flag.DATE_UNKNOWN)
    if doc_type is None or doc_type.signature_expected:
        if extraction.signature_status == SignatureStatus.UNCERTAIN.value:
            flag(Flag.SIGNATURE_UNCERTAIN)
        elif extraction.signature_status == SignatureStatus.NOT_DETECTED.value:
            flag(Flag.SIGNATURE_NOT_DETECTED)
    if employee.status == "api_unavailable":
        flag(Flag.EMPLOYEE_API_UNAVAILABLE)
    if employee.status != "verified":
        flag(Flag.EMPLOYEE_UNVERIFIED)
    if employee.matched_by == "name" or (employee.status != "verified" and employee.candidates):
        flag(Flag.EMPLOYEE_NAME_CANDIDATES)
        if employee.status == "verified":
            expl.append("employee matched by name only (lower trust)")
    pn = extraction.fields.get("personal_number")
    if not (pn and pn.value) and not extraction.employee_personal_numbers:
        flag(Flag.PERSONAL_NUMBER_MISSING)
    elif pn and pn.handwritten:
        flag(Flag.PERSONAL_NUMBER_HANDWRITTEN)
    if company.source == FieldSource.DEFAULT.value:
        flag(Flag.COMPANY_DEFAULT)
    if doc_type is not None and "fine_amount" in doc_type.fields:
        amount = extraction.fields.get("fine_amount")
        if not (amount and amount.value is not None):
            flag(Flag.AMOUNT_MISSING)
    api_company = employee.record.company_id if employee.status == "verified" and employee.record else None
    if api_company and company.company_id and api_company != company.company_id:
        flag(Flag.API_COMPANY_DIFFERS)

    # --- შედეგი ----------------------------------------------------------------------------
    if reasons:
        outcome = DocumentStatus.NEEDS_REVIEW.value
    elif cfg.section("processing").get("trial_mode", True):
        outcome = DocumentStatus.PROPOSED.value
        expl.append("trial_mode: processing.trial_mode=true — final auto-save disabled, result proposed")
    elif not doc_type.auto_save_enabled:
        outcome = DocumentStatus.PROPOSED.value
        expl.append(f"type_not_auto_enabled: auto_save_enabled=false for type {doc_type.id!r} — result proposed")
    else:
        outcome = DocumentStatus.AUTO_SAVED.value
        expl.append("all rule checks passed")
    return Decision(outcome=outcome, review_reasons=reasons, flags=flags, explanation=expl)
