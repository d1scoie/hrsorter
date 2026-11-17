"""საქართველოს პირადობის მოწმობა (ID ბარათი) და პასპორტი: გვარის, სახელის, პირადი ნომრისა და თარიღების ამოკითხვა.

ბარათზე ყოველი ველი ორჯერ წერია: ქართულად და ლათინურად („გვარი / SURNAME“ → „ბერიძე“, „BERIDZE“), უკანა მხარეს კი
მანქანური ზოლია (MRZ): „BERIDZE<<NINO<<<…“, პირადი ნომერი — პირველ ხაზში. ქართული სახელის ტრანსლიტერაცია ლათინურს/MRZ-ს
უნდა დაემთხვეს — ასე ვამოწმებთ, რომ ქართული სახელი სწორად წაიკითხა (ავტომატური ატვირთვა მხოლოდ ამის შემდეგ).

პასპორტზე იგივე ველებია, ოღონდ სხვანაირად დაწყობილი: მნიშვნელობა ერთ ხაზზე („ბერიძე / BERIDZE“), თარიღი თვის სახელით
(„01 იან / JAN 1990“), მანქანური ზოლი კი „P<GEOBERIDZE<<NINO<<<…“. თანამშრომელს ხშირად პასპორტი მოაქვს პირადობის ნაცვლად —
ორივე ერთი და იგივე პირადობის დოკუმენტია (`document_kind`).

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
_MRZ_PASSPORT = re.compile(r"^P<([A-Z]{3})([A-Z]+(?:<[A-Z]+)*)<<([A-Z]+(?:<[A-Z]+)*)<*$")   # P<GEOBERIDZE<<NINO<<<…
MRZ_MIN_LEN = 20          # ამაზე მოკლე ხაზი მანქანური ზოლი არ არის (სრული ზოლი 30/44 სიმბოლოა, მკითხველი ბოლოს ხშირად ჭრის)
_PASSPORT_TITLE = re.compile(r"(?:პასპორტი\s*/?\s*)?passport", re.I)
_DATE_LABELS = {"birth_date": re.compile(r"დაბადების\s+თარიღი|date\s+of\s+birth", re.I),
                "expiry_date": re.compile(r"მოქმედების\s+ვადა|date\s+of\s+expiry|expiry\s+date", re.I),
                "issue_date": re.compile(r"გაცემის\s+თარიღი|date\s+of\s+issue", re.I)}
_PN_LABEL = re.compile(r"პირადი\s*(n|№|ნომერი)|personal\s*n", re.I)
# პასპორტის თარიღი: „12 მარ / MAR 1985“ — რიცხვი, თვე (ქართულად და/ან ლათინურად), წელი
# (ქართული თვე ხშირად ვერ იკითხება: „12 [?] / MAR 1985“ — ლათინური საკმარისია)
_DATE_WORDS = re.compile(r"(?<!\d)(\d{1,2})\s+([ა-ჰA-Za-z\[][ა-ჰA-Za-z./\s\[\]?]{1,24}?)\s+(\d{4})(?!\d)")
# უხილავი სიმბოლოები, რომლებსაც მკითხველი ხანდახან გრძელ ხაზში სვამს (ნამდვილ სკანზე მანქანური ზოლის ციფრებს შორის
# ნულოვანი სიგანის ჰარი იჯდა) — ზოლი ამის გამო ვერ იცნობოდა და ბარათი „ტიპი სავარაუდოა“-ში რჩებოდა
_INVISIBLE = re.compile(r"[​-‏⁠﻿]")
_MONTHS_EN = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_MONTHS_KA = {m: i for i, m in enumerate(("იან", "თებ", "მარ", "აპრ", "მაი", "ივნ", "ივლ", "აგვ", "სექ", "ოქტ", "ნოე", "დეკ"), 1)}
# ხაზი „ქართული / LATIN“ ლეიბლია, თუ ამ სიტყვებიდან რომელიმეა („სახელი / GIVEN NAME“); სხვაგვარად — მნიშვნელობა.
# მხოლოდ მთლიანი სიტყვა: გვარი „დათეშიძე / DATESHIDZE“ ან „ვადაჭკორია“ ლეიბლი არ არის
_LABEL_WORD = re.compile(
    r"\b(?:surname|given|names?|sex|date|place|personal|citizenship|nationality|issue|issuing|authority|signature|passport|type|"
    r"code|state|number|birth|expiry|owner|of|no|გვარი|სახელი|სქესი|თარიღი|ადგილი|ვადა|პირადი|ნომერი|გამცემი|ორგანო|მოქალაქეობა|"
    r"ხელმოწერა|პასპორტი|პასპორტის|ტიპი|კოდი|სახელმწიფოს|დაბადების|გაცემის|მოქმედების|მფლობელის)\b", re.I)

ID_CARD, PASSPORT = "id_card", "passport"
MALE, FEMALE = "M", "F"
SEX_KA = {MALE: "მამრობითი", FEMALE: "მდედრობითი"}
# მანქანური ზოლის თარიღების ბლოკი (ICAO 9303): დაბადება YYMMDD + საკონტროლო ციფრი + სქესი + ვადა YYMMDD + საკონტროლო ციფრი.
# ბარათზე მეორე ხაზი ამით იწყება („9001011F3501017GEO…“), პასპორტზე ნომრისა და ქვეყნის შემდეგ დგას („…GEO9001011F3501017…“).
# საკონტროლო ციფრები ამოწმებს, რომ თარიღი სწორად წაიკითხა — ამიტომ ეს უფრო სანდოა, ვიდრე ბარათის წინა მხარის წარწერა
# (ნამდვილ სკანზე „დაბადების თარიღის“ ქვეშ მოქმედების ვადა წაიკითხა).
_MRZ_DATES = re.compile(r"(?=(\d{6})(\d)([MF<X])(\d{6})(\d))")
_SEX_LABEL = re.compile(r"სქესი|(?<![a-z])sex(?![a-z])", re.I)
_SEX_LATIN = re.compile(r"(?<![A-Za-z])([MF])(?![A-Za-z])")
_SEX_GEO = {"ქ": FEMALE, "ქალი": FEMALE, "მდ": FEMALE, "მდედრ": FEMALE, "მდედრობითი": FEMALE,
            "კ": MALE, "კაცი": MALE, "მამ": MALE, "მამრ": MALE, "მამრობითი": MALE}


@dataclass
class IdCard:
    kind: str | None = None                # ID_CARD / PASSPORT — მანქანური ზოლით; None — ზოლი არ იკითხება
    last_name: str | None = None
    first_name: str | None = None
    personal_number: str | None = None
    latin_last: str | None = None          # ბარათის ლათინური ხაზი ან MRZ
    latin_first: str | None = None
    mrz_has_pn: bool | None = None         # MRZ-ის პირველ ხაზში ეს პირადი ნომერია (None — MRZ არ იკითხება)
    birth_date: str | None = None          # მანქანური ზოლიდან, თუ საკონტროლო ციფრი ემთხვევა; სხვაგვარად წარწერიდან
    expiry_date: str | None = None         # იგივე წესით
    issue_date: str | None = None
    sex: str | None = None                 # MALE / FEMALE
    birth_sure: bool = False               # დაბადების თარიღი მანქანურმა ზოლმა დაადასტურა (საკონტროლო ციფრი)
    sex_sure: bool = False                 # სქესი ზოლიდანაა და წარწერას არ ეწინააღმდეგება, ან წარწერის ქართული და ლათინური ემთხვევა
    problems: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return bool(self.last_name and self.first_name and self.personal_number)


def transliterate(name: str) -> str:
    """ქართული სახელი → ლათინური ბარათის წესით („ბერიძე“ → „BERIDZE“); სხვა სიმბოლოები — ჰარი/ტირე ინახება."""
    return "".join(_TRANSLIT.get(ch, ch.upper() if ch in " -" else "") for ch in name).strip()


def _latin_key(s: str | None) -> str:
    """შესადარებელი ლათინური სახელი. „ყ“ ბარათზე ორნაირად გვხვდება: ეროვნული სისტემით „Q“, ნამდვილ ბარათებზე კი ხშირად „K“
    („ყაზბეგი“ → „KAZBEGI“) — ორივე ერთია. შემოწმება ამით არ სუსტდება: „კ“ და „ქ“ ისედაც ორივე „K“-ა."""
    return re.sub(r"[^A-Z]", "", (s or "").upper()).replace("Q", "K")


def _value_after(lines: list[str], i: int, label: re.Pattern[str], georgian: bool) -> str | None:
    """ლეიბლის შემდეგ პირველი მნიშვნელობა: იმავე ხაზზე ლეიბლის შემდეგ, ან შემდეგ ხაზებზე (ქართული ან ლათინური)."""
    tail = label.split(lines[i], maxsplit=1)[-1]
    tail = re.sub(r"^[\s/:]*([a-zA-Z\s]*?)(given\s*names?|surname)?[\s/:]*", "", tail, flags=re.I).strip()
    candidates = ([tail] if tail else []) + lines[i + 1: i + 4]
    for c in candidates:
        c = c.strip(" :/|")
        if not c or any(ch.isdigit() for ch in c):
            continue
        pair = [p.strip() for p in c.split("/")]
        if len(pair) == 2 and not any(_LABEL_WORD.search(p) for p in pair):   # პასპორტი: „ბერიძე / BERIDZE“ ერთ ხაზზე
            geo = [p for p in pair if len(p) >= 2 and georgian_ratio(p) >= 0.9]
            lat = [p for p in pair if re.fullmatch(r"[A-Za-z][A-Za-z\s-]+", p)]
            if len(geo) == 1 and len(lat) == 1:
                return geo[0] if georgian else lat[0].upper()
            continue
        if _LABEL.search(c):
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


def _date_in(s: str) -> str | None:
    """ხაზში პირველი თარიღი: „26.10.2036“ (ბარათი) ან „12 მარ / MAR 1985“ (პასპორტი)."""
    m = _DATE.search(s)
    if m:
        return _iso(m)
    m = _DATE_WORDS.search(s)
    if not m:
        return None
    words = re.findall(r"[ა-ჰ]+|[A-Za-z]+", m.group(2))
    month = next((_MONTHS_EN[w[:3].lower()] for w in words if w[:3].lower() in _MONTHS_EN), None) or \
        next((_MONTHS_KA[w[:3]] for w in words if w[:3] in _MONTHS_KA), None)
    if month is None:
        return None
    try:
        return _dt.date(int(m.group(3)), month, int(m.group(1))).isoformat()
    except ValueError:
        return None


def _visible(text: str) -> str:
    return _INVISIBLE.sub("", text or "")


def _mrz_lines(text: str) -> list[str]:
    flat = [re.sub(r"\s", "", ln).upper() for ln in _visible(text).splitlines()]
    return [ln for ln in flat if len(ln) >= MRZ_MIN_LEN and "<" in ln]


def _check_digit(digits: str) -> int:
    """ICAO 9303-ის საკონტროლო ციფრი: წონები 7, 3, 1 რიგრიგობით, ჯამი მოდულით 10."""
    return sum(int(c) * w for c, w in zip(digits, (7, 3, 1) * (len(digits) // 3 + 1))) % 10


def _mrz_date(yymmdd: str, past: bool, today: _dt.date | None = None) -> str | None:
    """ზოლის თარიღი → ISO. საუკუნე: დაბადების თარიღი მომავალში ვერ იქნება (`past`) — მაშინ 19YY; ვადა ყოველთვის 20YY."""
    try:
        d = _dt.date(2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))
        if past and d > (today or _dt.date.today()):
            d = d.replace(year=d.year - 100)
    except ValueError:
        return None
    return d.isoformat()


def mrz_dates(text: str, today: _dt.date | None = None) -> tuple[str, str | None, str] | None:
    """მანქანური ზოლიდან (დაბადების თარიღი, სქესი, მოქმედების ვადა) — მხოლოდ თუ ორივე თარიღის საკონტროლო ციფრი ემთხვევა.
    სქესი None-ია, თუ ზოლში მითითებული არ არის („<“/„X“)."""
    for ln in _mrz_lines(text):
        for m in _MRZ_DATES.finditer(ln):
            birth, bc, sex, expiry, ec = m.groups()
            if _check_digit(birth) != int(bc) or _check_digit(expiry) != int(ec):
                continue
            b, x = _mrz_date(birth, True, today), _mrz_date(expiry, False, today)
            if b and x:
                return b, sex if sex in (MALE, FEMALE) else None, x
    return None


def _printed_sex(lines: list[str]) -> tuple[str | None, bool]:
    """ბარათის წარწერა „სქესი / SEX“ → (სქესი, ქართული და ლათინური ორივე წაიკითხა და ემთხვევა). მნიშვნელობა იმავე ან მომდევნო
    ორ ხაზზეა: „ქ / F“, „მამ / M“, „მდ / F“; ქართული ხშირად არ იკითხება („[?] / F“)."""
    for i, ln in enumerate(lines):
        if not _SEX_LABEL.search(ln):
            continue
        for c in [_SEX_LABEL.split(ln)[-1], *lines[i + 1: i + 3]]:
            lat = {m.group(1) for m in _SEX_LATIN.finditer(c)}
            geo = {_SEX_GEO[w] for w in re.findall(r"[ა-ჰ]+", c) if w in _SEX_GEO}
            if len(lat | geo) == 1:
                return (lat | geo).pop(), bool(lat and geo)
            if lat or geo:        # ქართული და ლათინური ერთმანეთს ეწინააღმდეგება — წაკითხვას არ ვენდობით
                return None, False
    return None, False


def document_kind(text: str) -> str | None:
    """გვერდი პირადობის დოკუმენტია? PASSPORT / ID_CARD / None. ნიშანი მანქანური ზოლია (MRZ, ICAO 9303): „P<GEO…<<…“ პასპორტზე,
    „IDGEO…<<“ ბარათზე — სხვა HR დოკუმენტზე ასეთი ხაზი არ გვხვდება და სათაურის წაკითხვაზე არ არის დამოკიდებული (ახალ ბარათზე
    ქართული წარწერა წვრილია და ხშირად „[?]“-ად იკითხება). ზოლის გარეშე პასპორტი — სათაურით „პასპორტი PASSPORT“ და ველებით."""
    mrz = _mrz_lines(text)
    if any(_MRZ_PASSPORT.match(ln) for ln in mrz):
        return PASSPORT
    if any(_MRZ_DOC.match(ln) for ln in mrz):
        return ID_CARD
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if any(_PASSPORT_TITLE.fullmatch(ln) for ln in lines[:8]) and any(_SURNAME.search(ln) for ln in lines) \
            and any(_GIVEN.search(ln) for ln in lines):
        return PASSPORT
    return None


def parse_id_card(text: str) -> IdCard:
    """ID ბარათის ან პასპორტის ტექსტი (ერთი ან ორი მხარე) → ველები. ვერ წაკითხული ველი — None; `problems` — რატომ არ ენდობა."""
    text = _visible(text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    card = IdCard(kind=document_kind(text))
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
                setattr(card, key, next((d for d in map(_date_in, lines[i: i + 3]) if d), None))
    # MRZ (უკანა მხარე): სახელი და პირადი ნომერი — ბარათის ლათინურ ხაზზე უფრო სანდოა
    flat = [re.sub(r"\s", "", ln).upper() for ln in lines]
    for ln in flat:
        p = _MRZ_PASSPORT.match(ln) if len(ln) >= MRZ_MIN_LEN else None
        if p:   # პასპორტი: „P<GEO“ ტიპი და ქვეყანაა, სახელი მის შემდეგ იწყება
            card.latin_last, card.latin_first = p.group(2).replace("<", " "), p.group(3).replace("<", " ")
            continue
        m = _MRZ_NAME.match(ln)
        if m and len(ln) >= MRZ_MIN_LEN:
            card.latin_last, card.latin_first = m.group(1).replace("<", " "), m.group(2).replace("<", " ")
        if _MRZ_DOC.match(ln) and card.personal_number:
            card.mrz_has_pn = card.personal_number in ln
    if card.personal_number is None:
        pns = {m.group(1) for ln in lines for m in _PN.finditer(ln)}
        if len(pns) == 1:
            card.personal_number = pns.pop()
    printed_sex, both = _printed_sex(lines)
    zone = mrz_dates(text)
    if zone:   # ზოლის თარიღები საკონტროლო ციფრით დადასტურებულია — წარწერაზე სანდოა
        card.birth_date, card.expiry_date, card.birth_sure = zone[0], zone[2], True
    zone_sex = zone[1] if zone else None
    card.sex = zone_sex or printed_sex
    card.sex_sure = bool(zone_sex and printed_sex in (None, zone_sex)) or bool(not zone_sex and printed_sex and both)
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
