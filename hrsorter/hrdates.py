"""თარიღების გაფრთხილებები: ასაკი, ხელმოწერა მუშაობის დაწყების შემდეგ, პირადობის ვადა.

გაფრთხილება არაფერს აჩერებს — HR-ს აჩვენებს, რაზე ღირს შეხედვა. ზღვრები: `mhr.warnings` (`adult_age`, `id_expiry_days`).
ფუნქციები სუფთაა: თარიღები ISO სტრიქონებად („2026-11-05“), წაუკითხავი ან ცარიელი თარიღი → გაფრთხილება არ არის.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

EXPIRED, SOON = "expired", "soon"


def _d(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def limits(cfg) -> tuple[int, int]:
    """(სრულწლოვანების ასაკი, პირადობის ვადამდე რამდენი დღით ადრე გაფრთხილდეს)."""
    w = cfg.section("mhr").get("warnings") or {}
    return int(w.get("adult_age", 18)), int(w.get("id_expiry_days", 30))


def age_on(birth: Any, on: Any) -> int | None:
    """სრული წლები `on` დღეს."""
    b, d = _d(birth), _d(on)
    if b is None or d is None or d < b:
        return None
    return d.year - b.year - ((d.month, d.day) < (b.month, b.day))


def comes_of_age(birth: Any, adult: int = 18) -> str | None:
    """დღე, როცა `adult` წელი უსრულდება (ISO)."""
    b = _d(birth)
    if b is None:
        return None
    try:
        return b.replace(year=b.year + adult).isoformat()
    except ValueError:   # 29 თებერვალი
        return b.replace(year=b.year + adult, day=28).isoformat()


def minor_note(birth: Any, start: Any, adult: int = 18) -> str | None:
    """მუშაობის დაწყების დღეს სრულწლოვანი არ არის."""
    age = age_on(birth, start)
    if age is None or age >= adult:
        return None
    return (f"მუშაობის დაწყების დღეს ({_d(start).isoformat()}) თანამშრომელი {age} წლისაა — არასრულწლოვანია "
            f"(დაბადებულია {_d(birth).isoformat()}). შეამოწმე, რომ არასრულწლოვანის წესები დაცულია")


def signed_late_note(signed: Any, start: Any) -> str | None:
    """ხელშეკრულებას თარიღად მუშაობის დაწყების შემდგომი დღე აწერია."""
    s, w = _d(signed), _d(start)
    if s is None or w is None or s <= w:
        return None
    return (f"ხელშეკრულებას თარიღად {s.isoformat()} აწერია, მუშაობის დაწყება კი {w.isoformat()} — "
            f"ხელი მუშაობის დაწყებიდან {(s - w).days} დღის შემდეგ მოეწერა")


def id_expiry(expiry: Any, today: Any = None, days: int = 30, passport: bool = False) -> tuple[str, str] | None:
    """პირადობის (ან პასპორტის) ვადა: (EXPIRED | SOON, ტექსტი) ან None — ჯერ შორია."""
    e, t = _d(expiry), _d(today) or dt.date.today()
    if e is None:
        return None
    what = "პასპორტს" if passport else "პირადობის მოწმობას"
    left = (e - t).days
    if left < 0:
        return EXPIRED, f"{what} ვადა გაუვიდა ({e.isoformat()}) — ახალი მოითხოვე"
    if left <= days:
        when = "დღეს" if left == 0 else f"{left} დღეში"
        return SOON, f"{what} ვადა {when} გასდის ({e.isoformat()}) — ახალი მოითხოვე"
    return None


def person_warnings(cfg, facts: dict[str, Any] | None, today: Any = None) -> list[str]:
    """თანამშრომლის გაფრთხილებები დადასტურებული პირადობის მონაცემებით (`birth_date`, `id_expiry`): რაც **დღეს** აქტუალურია."""
    facts = facts or {}
    adult, days = limits(cfg)
    t = _d(today) or dt.date.today()
    out = []
    exp = id_expiry(facts.get("id_expiry"), t, days, passport=facts.get("id_kind") == "passport")
    if exp:
        out.append(exp[1])
    age = age_on(facts.get("birth_date"), t)
    if age is not None and age < adult:
        out.append(f"არასრულწლოვანია: {age} წლის ({adult} შეუსრულდება {comes_of_age(facts['birth_date'], adult)})")
    return out
