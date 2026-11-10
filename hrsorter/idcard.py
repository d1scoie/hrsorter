"""საქართველოს პირადობის მოწმობა (ID ბარათი): გვარის, სახელის, პირადი ნომრისა და თარიღების ამოკითხვა.

ბარათზე ყოველი ველი ორჯერ წერია: ქართულად და ლათინურად („გვარი / SURNAME“ → „ბერიძე“, „BERIDZE“), უკანა მხარეს კი
მანქანური ზოლია (MRZ): „BERIDZE<<NINO<<<…“, პირადი ნომერი — პირველ ხაზში. ქართული სახელის ტრანსლიტერაცია ლათინურს/MRZ-ს
უნდა დაემთხვეს — ასე ვამოწმებთ, რომ ქართული სახელი სწორად წაიკითხა (ავტომატური ატვირთვა მხოლოდ ამის შემდეგ).

სკანირებული ტექსტი მონაცემია; ის მხოლოდ იკითხება.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field

from .textnorm import georgian_ratio

# საქართველოს ეროვნული ტრანსლიტერაციის სისტემა (2002) — პირადობის მოწმობაზე ასე იწერება ლათინურად
_TRANSLIT = {"ა": "A", "ბ": "B", "გ": "G", "დ": "D", "ე": "E", "ვ": "V", "ზ": "Z", "თ": "T", "ი": "I", "კ": "K", "ლ": "L",
             "მ": "M", "ნ": "N", "ო": "O", "პ": "P", "ჟ": "ZH", "რ": "R", "ს": "S", "ტ": "T", "უ": "U", "ფ": "P", "ქ": "K",
             "ღ": "GH", "ყ": "Q", "შ": "SH", "ჩ": "CH", "ც": "TS", "ძ": "DZ", "წ": "TS", "ჭ": "CH", "ხ": "KH", "ჯ": "J", "ჰ": "H"}

_SURNAME = re.compile(r"გვარი|surname", re.I)
_GIVEN = re.compile(r"სახელი|given\s*names?", re.I)
_LABEL = re.compile(r"/|surname|given|sex|date|place|personal|citiz|cit\.|issu|სქესი|თარიღი|ადგილი|ვადა|მოქ\.|პირადი|გამცემი|ორგანო",
                    re.I)
_PN = re.compile(r"(?<!\d)(\d{11})(?!\d)")
_DATE = re.compile(r"(\d{2})[./-](\d{2})[./-](\d{4})")
_MRZ_NAME = re.compile(r"^([A-Z]+(?:<[A-Z]+)*)<<([A-Z]+(?:<[A-Z]+)*)<*$")
_MRZ_DOC = re.compile(r"^I[A-Z<]GEO?[A-Z0-9<]{10,}$")
_DATE_LABELS = {"birth_date": re.compile(r"დაბადების\s+თარიღი|date\s+of\s+birth", re.I),
                "expiry_date": re.compile(r"მოქმედების\s+ვადა|date\s+of\s+expiry", re.I),
                "issue_date": re.compile(r"გაცემის\s+თარიღი|date\s+of\s+issue", re.I)}
_PN_LABEL = re.compile(r"პირადი\s*(n|№|ნომერი)|personal\s*n", re.I)


@dataclass
class IdCard:
    last_name: str | None = None
    first_name: str | None = None
    personal_number: str | None = None
    latin_last: str | None = None          # ბარათის ლათინური ხაზი ან MRZ
    latin_first: str | None = None
    mrz_has_pn: bool | None = None         # MRZ-ის პირველ ხაზში ეს პირადი ნომერია (None — MRZ არ იკითხება)
    birth_date: str | None = None
    expiry_date: str | None = None
    issue_date: str | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return bool(self.last_name and self.first_name and self.personal_number)


def transliterate(name: str) -> str:
    """ქართული სახელი → ლათინური ბარათის წესით („ბერიძე“ → „BERIDZE“); სხვა სიმბოლოები — ჰარი/ტირე ინახება."""
    return "".join(_TRANSLIT.get(ch, ch.upper() if ch in " -" else "") for ch in name).strip()


def _latin_key(s: str | None) -> str:
    return re.sub(r"[^A-Z]", "", (s or "").upper())


def _value_after(lines: list[str], i: int, label: re.Pattern[str], georgian: bool) -> str | None:
    """ლეიბლის შემდეგ პირველი მნიშვნელობა: იმავე ხაზზე ლეიბლის შემდეგ, ან შემდეგ ხაზებზე (ქართული ან ლათინური)."""
    tail = label.split(lines[i], maxsplit=1)[-1]
    tail = re.sub(r"^[\s/:]*([a-zA-Z\s]*?)(given\s*names?|surname)?[\s/:]*", "", tail, flags=re.I).strip()
    candidates = ([tail] if tail else []) + lines[i + 1: i + 4]
    for c in candidates:
        c = c.strip(" :/|")
        if not c or _LABEL.search(c) or any(ch.isdigit() for ch in c):
            continue
        is_geo = georgian_ratio(c) >= 0.9
        if georgian and is_geo:
            return c
        if not georgian and not is_geo and re.fullmatch(r"[A-Za-z][A-Za-z\s-]*", c):
            return c.upper()
    return None


def _iso(d: re.Match[str]) -> str | None:
    try:
        return _dt.date(int(d.group(3)), int(d.group(2)), int(d.group(1))).isoformat()
    except ValueError:
        return None


def parse_id_card(text: str) -> IdCard:
    """ID ბარათის ტექსტი (ერთი ან ორი მხარე) → ველები. ვერ წაკითხული ველი — None; `problems` — რატომ არ ენდობა."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    card = IdCard()
    for i, ln in enumerate(lines):
        if card.last_name is None and _SURNAME.search(ln):
            card.last_name = _value_after(lines, i, _SURNAME, georgian=True)
            card.latin_last = _value_after(lines, i, _SURNAME, georgian=False)
        elif card.first_name is None and _GIVEN.search(ln) and not _SURNAME.search(ln):
            card.first_name = _value_after(lines, i, _GIVEN, georgian=True)
            card.latin_first = _value_after(lines, i, _GIVEN, georgian=False)
        elif card.personal_number is None and _PN_LABEL.search(ln):
            m = _PN.search(ln) or next((_PN.search(x) for x in lines[i + 1: i + 3] if _PN.search(x)), None)
            card.personal_number = m.group(1) if m else None
        for key, rx in _DATE_LABELS.items():
            if getattr(card, key) is None and rx.search(ln):
                m = _DATE.search(ln) or next((_DATE.search(x) for x in lines[i + 1: i + 3] if _DATE.search(x)), None)
                setattr(card, key, _iso(m) if m else None)
    # MRZ (უკანა მხარე): სახელი და პირადი ნომერი — ბარათის ლათინურ ხაზზე უფრო სანდოა
    flat = [re.sub(r"\s", "", ln).upper() for ln in lines]
    for ln in flat:
        m = _MRZ_NAME.match(ln)
        if m and len(ln) >= 20:
            card.latin_last, card.latin_first = m.group(1).replace("<", " "), m.group(2).replace("<", " ")
        if _MRZ_DOC.match(ln) and card.personal_number:
            card.mrz_has_pn = card.personal_number in ln
    if card.personal_number is None:
        pns = {m.group(1) for ln in lines for m in _PN.finditer(ln)}
        if len(pns) == 1:
            card.personal_number = pns.pop()
    _self_check(card)
    return card


def _self_check(card: IdCard) -> None:
    """ქართული სახელი ლათინურს/MRZ-ს უნდა ემთხვეოდეს — სხვაგვარად წაკითხვა საეჭვოა."""
    for geo, lat, what in ((card.last_name, card.latin_last, "გვარი"), (card.first_name, card.latin_first, "სახელი")):
        if geo and lat and _latin_key(transliterate(geo)) != _latin_key(lat):
            card.problems.append(f"{what}: ქართულად „{geo}“, ლათინურად „{lat}“ — ერთმანეთს არ ემთხვევა, წაკითხვა საეჭვოა")
    if card.mrz_has_pn is False:
        card.problems.append("პირადი ნომერი ბარათის ქვედა ზოლში (MRZ) სხვანაირად წერია — წაკითხვა საეჭვოა")
    if not (card.latin_last or card.latin_first):
        card.problems.append("ლათინური სახელი/MRZ არ იკითხება — ქართული სახელის წაკითხვა ვერ გადამოწმდა")


def same_name(a: str | None, b: str | None) -> bool:
    """ზუსტი დამთხვევა (ჰარები/ტირის გარშემო ჰარები/რეგისტრი არ ითვლება) — პირადობისთვის „ერთი ერთში“."""
    def norm(s: str | None) -> str:
        return re.sub(r"\s*-\s*", "-", re.sub(r"\s+", " ", (s or "").strip().casefold()))
    return bool(a and b) and norm(a) == norm(b)
