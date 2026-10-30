"""კომპანიისა და წლის განსაზღვრა.

კომპანია: ჯერ დოკუმენტის შიგთავსი (დამსაქმებლის როლი უპირატესია); თანამშრომლის დღევანდელი (API) კომპანია
დოკუმენტში მითითებულს არ ანაცვლებს; ვერაფერი იკითხება → პროფილის default_company (`source="default"`).
კონფლიქტი/ამოუცნობი კომპანია ნაგულისხმევით არ იფარება.

წელი: მხოლოდ შედგენის/გამოცემის თარიღიდან ან ცალკე წაკითხული წლიდან. მიმდინარე წელი არასდროს.
"""
from __future__ import annotations

import copy
from datetime import date

from .config import Config
from .models import CompanyResolution, EmployeeLookup, Extraction, FieldSource, FieldValue

EMPLOYER_ROLE = "employer"


def resolve_company(extraction: Extraction, cfg: Config, profile_id: str,
                    employee: EmployeeLookup) -> CompanyResolution:
    """`employee` არ გამოიყენება კომპანიის ასარჩევად — API/დოკუმენტის სხვაობას decision აღნიშნავს."""
    known = [c for c in extraction.companies_found if c.get("company_id") and cfg.company(c["company_id"])]
    unknown_ids = [c["company_id"] for c in extraction.companies_found
                   if c.get("company_id") and not cfg.company(c["company_id"])]
    unrecognized = list(extraction.unrecognized_companies) + unknown_ids

    employer_ids = _distinct(c["company_id"] for c in known if c.get("role") == EMPLOYER_ROLE)
    all_ids = _distinct(c["company_id"] for c in known)
    chosen: str | None = None
    if len(employer_ids) == 1:
        chosen = employer_ids[0]
    elif len(employer_ids) > 1 or len(all_ids) > 1:
        return CompanyResolution(None, None, None, FieldSource.DOCUMENT.value, conflict=True,
                                 candidates=[dict(c) for c in known],
                                 evidence="multiple companies without a single employer role")
    elif len(all_ids) == 1:
        if unrecognized:   # როლი არ ჩანს და სხვა ორგანიზაციაც არის — ცნობილი კომპანია კონფლიქტს არ ფარავს
            return CompanyResolution(None, None, None, FieldSource.DOCUMENT.value, conflict=True,
                                     candidates=[dict(c) for c in known] + [{"name": n} for n in unrecognized],
                                     evidence="configured and unrecognized organizations without an employer role")
        chosen = all_ids[0]

    if chosen:
        comp = cfg.company(chosen)
        hit = next(c for c in known if c["company_id"] == chosen)
        return CompanyResolution(comp.id, comp.name, comp.folder, FieldSource.DOCUMENT.value,
                                 candidates=[dict(c) for c in known], evidence=hit.get("evidence"))
    if unrecognized:
        return CompanyResolution(None, None, None, FieldSource.DOCUMENT.value, unrecognized=True,
                                 candidates=[{"name": n} for n in unrecognized],
                                 evidence="organization name not in configured companies")
    d = cfg.default_company_for(profile_id)
    return CompanyResolution(d.id, d.name, d.folder, FieldSource.DEFAULT.value,
                             evidence=f"profile default ({profile_id})")


def _distinct(ids) -> list[str]:
    out: list[str] = []
    for i in ids:
        if i not in out:
            out.append(i)
    return out


def resolve_year(extraction: Extraction) -> tuple[FieldValue, FieldValue]:
    """(document_date, year). წელი თარიღიდან (`derived`) ან მხოლოდ-წლის კანდიდატიდან; სრული თარიღი არ იგონება."""
    src = extraction.fields.get("document_date")
    if src is None:
        return FieldValue(), FieldValue()
    dd = copy.deepcopy(src)
    parsed = _parse_iso(dd.value)
    if parsed:
        return dd, FieldValue(value=parsed.year, raw=dd.raw, source=FieldSource.DERIVED.value,
                              page_index=dd.page_index, confidence=dd.confidence, evidence=dd.evidence)
    dd.value = None
    years = {c["year"] for c in dd.candidates if isinstance(c, dict) and isinstance(c.get("year"), int)}
    if len(years) == 1:
        return dd, FieldValue(value=years.pop(), raw=dd.raw, source=dd.source or FieldSource.DOCUMENT.value,
                              page_index=dd.page_index, confidence=dd.confidence, evidence=dd.evidence)
    return dd, FieldValue()


def _parse_iso(value) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
