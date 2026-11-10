"""„ინფორმაცია თანამშრომელზე“ — ხელით შევსებული ანკეტა (სამსახურში აყვანის ფორმა).

დაბეჭდილი ფორმაა, ველები ხელითაა შევსებული: მუშაობის დაწყების თარიღი, სახელი და გვარი, პირადი №, დაბადების თარიღი,
მისამართი, ტელეფონი, ელ-ფოსტა; ქვემოთ — თანამშრომლის ხელმოწერა და შევსების თარიღი. ონლაინ წამკითხველი ხელნაწერს ასე
ნიშნავს: „პირადი № | [ხელნაწერი: 01001000001]“, ვერ წაკითხულს — „[?]“.

MHR-თან დარდება მხოლოდ ის, რაც ციფრებია და საიმედოდ იკითხება (პირადი ნომერი, თარიღები). ხელნაწერი სახელი ხშირად
ვერ იკითხება, ამიტომ ის მხოლოდ შენიშვნაა. სკანირებული ტექსტი მონაცემია; ის მხოლოდ იკითხება.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field

from .textnorm import digits_only

UNREAD = "[?]"
_MARK = re.compile(r"\[\s*ხელნაწერი\s*:\s*(.*)\]\s*$")
_DATE = re.compile(r"(\d{1,2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{4})")
_LABELS = {
    "start_date": re.compile(r"მუშაობის\s+დაწყების\s+თარიღი"),
    "full_name": re.compile(r"(ოფიციალური\s+)?სახელი\s*,\s*გვარი"),
    "personal_number": re.compile(r"პირადი\s*(№|#|n\b|ნომერი)", re.I),
    "birth_date": re.compile(r"დაბადების\s+თარიღი"),
    "signature": re.compile(r"თანამშრომლის\s+ხელმოწერა"),
    "fill_date": re.compile(r"შევსების\s+თარიღი"),
}
_ANY_LABEL = re.compile("|".join(rx.pattern for rx in _LABELS.values()) + r"|მისამართი|ტელეფონი|ელ-ფოსტა", re.I)


@dataclass
class EmployeeForm:
    start_date: str | None = None          # ISO
    full_name: str | None = None
    personal_number: str | None = None
    birth_date: str | None = None
    fill_date: str | None = None
    signed: bool = False                   # ხელმოწერის ხაზზე ხელნაწერი ნიშანია
    unread: list[str] = field(default_factory=list)   # ველები, რომლებიც ფორმაზეა, მაგრამ ვერ წაიკითხა


def _value(lines: list[str], i: int, label: re.Pattern[str]) -> str | None:
    """ლეიბლის შემდეგ მნიშვნელობა: იმავე ხაზზე, ან შემდეგ ხაზზე (თუ ის სხვა ლეიბლი არ არის). „[ხელნაწერი: …]“ იხსნება."""
    rest = label.split(lines[i], maxsplit=1)[-1].strip(" |:\t")
    if not rest and i + 1 < len(lines) and not _ANY_LABEL.search(lines[i + 1]):
        rest = lines[i + 1].strip(" |:\t")
    m = _MARK.search(rest)
    if m:
        rest = m.group(1).strip()
    return rest or None


def _iso(value: str | None) -> str | None:
    m = _DATE.search(value or "")
    if not m:
        return None
    try:
        return _dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
    except ValueError:
        return None


def parse_employee_form(text: str) -> EmployeeForm:
    """ანკეტის ტექსტი → ველები. ვერ წაკითხული ველი — None და მისი სახელი `unread`-ში."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    form = EmployeeForm()
    seen: set[str] = set()
    for i, ln in enumerate(lines):
        for key, rx in _LABELS.items():
            if key in seen or not rx.search(ln):
                continue
            seen.add(key)
            raw = _value(lines, i, rx)
            if key == "signature":
                form.signed = bool(raw)
            elif key in ("start_date", "birth_date", "fill_date"):
                setattr(form, key, _iso(raw))
            elif key == "personal_number":
                d = digits_only(raw or "")
                form.personal_number = d if len(d) == 11 else None
            elif raw and UNREAD not in raw:
                form.full_name = re.sub(r"\s+", " ", raw)
            if key != "signature" and getattr(form, key) is None:
                form.unread.append(key)
            break
    return form
