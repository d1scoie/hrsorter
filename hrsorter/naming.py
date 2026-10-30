"""საბოლოო ბილიკის აგება და Windows-ისთვის უსაფრთხო სანიტიზაცია.

Sorted/ და Proposed/: <კომპანიის საქაღალდე>/<ტიპის საქაღალდე>/<წელი|წელი-უცნობია>/<ფაილი>.pdf
NeedsReview/:         <პირველი მიზეზის კოდი>/<ფაილი>.pdf
საქაღალდეები მხოლოდ კონფიგურაციიდან (Company.folder / DocType.folder); დოკუმენტის ტექსტი ბილიკში მხოლოდ
სანიტიზებული ფაილის სახელის კომპონენტად ხვდება.
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from .config import Config
from .models import DocumentOutcome, DocumentStatus, ReviewReason

_FORBIDDEN = re.compile(r'[<>:"/\\|?*]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_PN = re.compile(r"\d{11}")
PN_MIN_CONFIDENCE = 0.8
_EMPTY = "\x00"


def sanitize_component(text: str, max_len: int, fallback: str = "_") -> str:
    """NFC; აკრძალული და მართვის სიმბოლოები → ჰარი; ჰარები → `-`; `_` → `-` (შაბლონის გამყოფია);
    ბოლო წერტილები/ჰარები იჭრება; რეზერვირებულ სახელს (CON, COM1.txt …) `_` ემატება წინ."""
    t = unicodedata.normalize("NFC", str(text))
    t = _FORBIDDEN.sub(" ", t)
    t = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in t)
    t = re.sub(r"[\s_]+", "-", t.strip())
    t = re.sub(r"-{2,}", "-", t)
    t = t[:max_len].strip("-").rstrip(". ")
    if not t:
        t = fallback
    if t.split(".")[0].strip().upper() in _RESERVED:
        t = ("_" + t)[:max_len]
    return t


def short_id(document_id: str) -> str:
    """document_id-ის uuid ნაწილის პირველი 8 hex სიმბოლო (`doc_<uuid>` → 8 სიმბოლო)."""
    return document_id.split("_", 1)[-1].replace("-", "")[:8]


def trusted_personal_number(outcome: DocumentOutcome) -> str | None:
    """ფაილის სახელისთვის სანდო პ/ნ: API-მ დაადასტურა პ/ნ-ით, ან დოკუმენტიდან წაკითხულია confidence ≥ 0.8-ით
    და არ არის ხელნაწერი. სახელით დამთხვევის პ/ნ ფაილის სახელში არ ხვდება."""
    ext, emp = outcome.extraction, outcome.employee
    if len(set(ext.employee_personal_numbers)) > 1:
        return None
    if emp.status == "verified" and emp.matched_by == "personal_number" and emp.record \
            and emp.record.personal_number and _PN.fullmatch(emp.record.personal_number):
        return emp.record.personal_number
    f = ext.fields.get("personal_number")
    if f and isinstance(f.value, str) and _PN.fullmatch(f.value) and f.confidence >= PN_MIN_CONFIDENCE \
            and not f.handwritten:
        return f.value
    return None


def _person_name(outcome: DocumentOutcome) -> str | None:
    fields = outcome.extraction.fields
    first = fields["first_name"].value if fields.get("first_name") else None
    last = fields["last_name"].value if fields.get("last_name") else None
    emp = outcome.employee
    if not (first or last) and emp.status == "verified" and emp.matched_by == "personal_number" and emp.record:
        first, last = emp.record.first_name, emp.record.last_name
    name = " ".join(str(x) for x in (first, last) if x)
    return name or None


def _type_folder(outcome: DocumentOutcome, cfg: Config) -> str:
    t = cfg.doc_type(outcome.classification.type_id) or cfg.doc_type("unknown")
    return t.folder


def _company_folder(outcome: DocumentOutcome, cfg: Config) -> str:
    c = cfg.company(outcome.company.company_id) if outcome.company.company_id else None
    return (c or cfg.default_company_for(outcome.profile_id)).folder


def _render(template: str, values: dict[str, str]) -> str:
    """ცარიელი კომპონენტი (მაგ. doc_number) მის წინა გამყოფთან ერთად ამოიშლება."""
    s = template.format(**values)
    s = re.sub(r"[_\-]?" + _EMPTY, "", s)
    return s.lstrip("_-")


def build_output_path(base_dir: Path, outcome: DocumentOutcome, cfg: Config) -> Path:
    n = cfg.section("naming")
    comp_len, file_len, path_len = int(n["max_component_len"]), int(n["max_filename_len"]), int(n["max_path_len"])

    if outcome.decision.outcome == DocumentStatus.NEEDS_REVIEW.value:
        reason = outcome.decision.review_reasons[0] if outcome.decision.review_reasons else ""
        if reason not in {r.value for r in ReviewReason}:   # საქაღალდე მხოლოდ ცნობილი კოდით
            reason = ReviewReason.OTHER.value
        folder = Path(base_dir) / reason
    else:
        year = outcome.year.value
        folder = (Path(base_dir) / sanitize_component(_company_folder(outcome, cfg), comp_len)
                  / sanitize_component(_type_folder(outcome, cfg), comp_len)
                  / (str(year) if isinstance(year, int) else sanitize_component(n["unknown_year"], comp_len)))

    doc_no = outcome.extraction.fields.get("document_number")
    values = {
        "date": sanitize_component(outcome.document_date.value or n["unknown_date"], comp_len),
        "type": sanitize_component(_type_folder(outcome, cfg), comp_len),
        "name": sanitize_component(_person_name(outcome) or n["unknown_name"], comp_len),
        "personal_number": sanitize_component(trusted_personal_number(outcome) or n["unknown_personal_number"],
                                              comp_len),
        "doc_number": sanitize_component(doc_no.value, comp_len) if doc_no and doc_no.value else _EMPTY,
        "short_id": short_id(outcome.document_id),
    }

    def fits(stem: str) -> int:
        """რამდენი სიმბოლოთი აჭარბებს (≤0 — ეტევა). +3 — კოლიზიის სუფიქსის (-NN) მარაგი."""
        name_len = len(stem) + len(".pdf") + 3
        return max(name_len - file_len, len(str(folder)) + 1 + name_len - path_len)

    stem = _render(n["template"], values)
    over = fits(stem)
    if over > 0:  # ჯერ სახელის კომპონენტი იკვეცება
        values["name"] = values["name"][:max(1, len(values["name"]) - over)].rstrip("-. ")
        stem = _render(n["template"], values)
        over = fits(stem)
    if over > 0:  # შემდეგ დანარჩენი — short_id ყოველთვის რჩება
        sid = values["short_id"]
        rest = stem.replace(sid, "").strip("_-")
        stem = rest[:max(0, len(rest) - over)].rstrip("_-. ") + "_" + sid
    stem = stem.rstrip(". ")
    if stem.split(".")[0].strip().upper() in _RESERVED:   # მთლიანი სახელიც არ უნდა იყოს რეზერვირებული
        stem = "_" + stem

    candidate = folder / f"{stem}.pdf"
    i = 2
    while candidate.exists() or candidate.with_suffix(".json").exists():
        candidate = folder / f"{stem}-{i}.pdf"
        i += 1
    return candidate
