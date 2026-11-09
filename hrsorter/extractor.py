"""ველების ამოღება ერთი სეგმენტის გვერდებიდან (წესებზე დაფუძნებული, მნიშვნელობები არ იგონება).

ყველა ველი ყოველთვის არსებობს `Extraction.fields`-ში; ვერნაპოვნი → `FieldValue()` (value=None).
სკანირებული ტექსტი მონაცემია: მასში არსებული „ინსტრუქცია" უბრალოდ ტექსტია.
"""
from __future__ import annotations

import calendar
import datetime as _dt
import re
from collections import Counter
from dataclasses import dataclass

from .config import Config, DocType
from .features import (CLOSING_RE, company_core, company_matchers, find_doc_number, find_names, find_personal_numbers,
                       has_closing_block, line_at)
from .models import Extraction, FieldSource, FieldValue, PageText, SignatureStatus
from .textnorm import normalize, title_zone

FIELD_KEYS = ("first_name", "last_name", "personal_number", "document_number", "document_date", "branch",
              "contract_start", "contract_end", "vacation_start", "vacation_end", "vacation_days",
              "fine_amount", "fine_currency", "fine_reason", "position", "salary")
TYPE_FIELDS = {"contract_start", "contract_end", "vacation_start", "vacation_end", "vacation_days",
               "fine_amount", "fine_currency", "fine_reason"}
EVIDENCE_MAX = 120
REASON_MAX = 200

# --- თარიღები ----------------------------------------------------------------------------
_MONTHS = {"იანვ": 1, "თებერვ": 2, "მარტ": 3, "აპრილ": 4, "მაის": 5, "ივნის": 6, "ივლის": 7, "აგვისტო": 8,
           "სექტემბ": 9, "ოქტომბ": 10, "ნოემბ": 11, "დეკემბ": 12}
_MONTH = r"(?P<mon>იანვ|თებერვ|მარტ|აპრილ|მაის|ივნის|ივლის|აგვისტო|სექტემბ|ოქტომბ|ნოემბ|დეკემბ)(?P<suf>[ა-ჰ]{0,8})"
_YEAR_TAIL = r"(?:\s*(?:წ\.|წელი|წელს|წლის|წ\b))?"
_DATE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ydm", re.compile(r"(?<!\d)(?P<y>\d{4})\s*წლის\s+(?P<d>\d{1,2})\s+" + _MONTH)),
    ("dmy_words", re.compile(r"(?<!\d)(?P<d>\d{1,2})\s+" + _MONTH + r"\s*,?\s*(?P<y>\d{4})" + _YEAR_TAIL)),
    ("dmy", re.compile(r"(?<!\d)(?P<d>\d{1,2})[./-](?P<m>\d{1,2})[./-](?P<y>\d{4})(?!\d)")),
    ("ymd", re.compile(r"(?<!\d)(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})(?!\d)")),
    ("dm", re.compile(r"(?<!\d)(?P<d>\d{1,2})\s+" + _MONTH)),
    ("year", re.compile(r"(?<!\d)(?P<y>(?:19|20)\d{2})\s*(?:წ\.|წელი|წელს|წლის|წ\b)")),
]
_RANGE_START_RE = re.compile(r"^\s*-?\s*(?:ი)?დან\b")
_RANGE_END_RE = re.compile(r"^\s*-?\s*(?:ის\s+|ი)?(?:ა?მდე|ჩათვლით)\b")

_ISSUE_CUE_RE = re.compile(r"თარიღი|შედგენ|გაცემ|გამოცემ")
_CITY_LINE_RE = re.compile(r"^(?:ქ\.\s*[ა-ჰ]+|თბილისი|ბათუმი|ქუთაისი|რუსთავი|ზუგდიდი|გორი|ფოთი|თელავი)\b")
_PERIOD_CUE_RE = re.compile(r"-?\bდან\b|მდე\b|ჩათვლით|ვადით|ძალაში|შვებულებ|დაწყებ|დასრულ|მოქმედ|იდან\b")
_CONTRACT_CUE_RE = re.compile(r"ხელშეკრულებ|ძალაში|მოქმედ|ვად(?:ა|ით)\b|დაწყებ|დასრულ|სამუშაო(?:ს|ზე)\s+დაწყ")
_VACATION_CUE_RE = re.compile(r"შვებულებ")
_START_CUE_RE = re.compile(r"ძალაში\s+შედის|დაწყებ|დან\b")
_END_CUE_RE = re.compile(r"დასრულ|მდე\b|ჩათვლით|ბოლო")

# --- სხვა ველები ------------------------------------------------------------------------------
_VACATION_DAYS_RE = re.compile(r"(?<!\d)(\d{1,3})\s*(?:\([^)]{0,40}\)\s*)?(?:კალენდარული|სამუშაო)?\s*დღ")
_AMOUNT_RE = re.compile(
    r"(?<![\d.,])(?P<amt>\d{1,7}(?:[.,]\d{1,2})?)\s*(?:\([^)]{0,80}\)\s*)?"
    r"(?P<cur>ლარ[ა-ჰ]*|₾|gel|აშშ\s+დოლარ[ა-ჰ]*|დოლარ[ა-ჰ]*|usd|\$|ევრო[ა-ჰ]*|eur|€)")
# „დოლარ" შეიცავს „ლარ"-ს — ამიტომ USD/EUR ლარზე ადრე მოწმდება
_CURRENCY = (("დოლარ", "USD"), ("usd", "USD"), ("$", "USD"), ("ევრო", "EUR"), ("eur", "EUR"), ("€", "EUR"),
             ("ლარ", "GEL"), ("₾", "GEL"), ("gel", "GEL"))
_FINE_CUE_RE = re.compile(r"ჯარიმ|დააკისრ|დაკავ|სახდელ|დაქვით")
# მიზეზის წყაროები პრიორიტეტით: „მიზეზი:" > „...დარღვევისთვის" ფრაზა > „საფუძველი:"
_REASON_SOURCES: list[tuple[re.Pattern[str], float]] = [
    (re.compile(r"მიზეზი\s*[:\-]\s*(?P<r>.{3,200})"), 0.8),
    (re.compile(r"(?P<r>[^.;:\n]{3,200}?დარღვევ[ა-ჰ]*)"), 0.6),
    (re.compile(r"საფუძველი\s*[:\-]\s*(?P<r>.{3,200})"), 0.5),
]
_BRANCH_RE = re.compile(r"ფილიალ[ა-ჰ]*\s*[:\-]\s*\"?(?P<b>[ა-ჰa-z0-9][^\"\n,;]{1,40}?)\"?\s*(?:[,;]|$)")
_BRANCH_BEFORE_RE = re.compile(r"(?P<b>[ა-ჰ]{3,}\s+ფილიალ[ა-ჰ]*)")

# --- თანამდებობა / ხელფასი (MHR-თან შედარებისთვის; მხოლოდ აშკარა მინიშნებით) ----------------------
# „თანამდებობა: მენეჯერი", „პოზიცია - უფროსი სპეციალისტი" — ხაზის/სასვენის ბოლომდე, ≤5 სიტყვა
_POSITION_LABEL_RE = re.compile(r"(?:თანამდებობა|პოზიცია)\s*[:\-]\s*\"?(?P<p>[ა-ჰ][ა-ჰ\- ]{2,60}?)\"?\s*(?:[,;.\n()]|$)")
# „… მენეჯერის თანამდებობაზე" / „… სპეციალისტის პოზიციაზე": წინ მდგომი ნათესაობითი ფრაზა
_POSITION_BEFORE_RE = re.compile(r"(?<![ა-ჰ])(?P<p>(?:[ა-ჰ]+(?:ის|ი)\s+){0,2}[ა-ჰ]{3,}ის)\s+(?:თანამდებობაზე|პოზიციაზე)")
_POSITION_MAX_WORDS = 5
_NOT_POSITION_WORDS = ("დასაქმებულ", "თანამშრომ", "განმცხადებ", "მოქალაქ", "შემდეგ", "ამავე", "იგივე", "აღნიშნულ")
_SALARY_CUE_RE = re.compile(r"ხელფას|ანაზღაურებ|სარგო|საზღაურ")
_SALARY_PARA_LINES = 3   # მინიშნება შეიძლება თანხის ხაზამდე ამდენი ხაზით ადრე იყოს (ჯარიმის ხაზზე — არა)
# „ძალაში შედის … ხელმოწერის დღიდან და მოქმედებს 12 (თორმეტი) თვის ვადით" → დაწყება = დოკუმენტის თარიღი
_TERM_FROM_SIGNING_RE = re.compile(
    r"ხელმოწერის\s+დღიდან.{0,80}?(?<!\d)(?P<n>\d{1,2})\s*(?:\([^)]{0,30}\)\s*)?(?P<unit>თვის|წლის)\s+ვადით", re.S)

# --- კომპანიები -------------------------------------------------------------------------------
_LEGAL_FORM = r"(?:შპს|სს|ი/მ|ა\(ა\)იპ|სსიპ|შ\.პ\.ს\.|სპს)"
_ORG_QUOTED_RE = re.compile(_LEGAL_FORM + r"\s*\"(?P<n>[^\"\n]{2,60})\"")
_ORG_PLAIN_RE = re.compile(r"(?<![ა-ჰ])" + _LEGAL_FORM + r"\s+(?P<n>[ა-ჰa-z][ა-ჰa-z0-9\-]{2,})")
_EMPLOYER_CUE_RE = re.compile(r"დამსაქმებელ|დირექტორ|ხელმძღვანელ|სახელით|წარმოდგენილ")
_OTHER_CUE_RE = re.compile(r"ყოფილი|წინა\s+სამუშაო|მესამე\s+პირ|კონტრაგენტ|მომწოდებ|კლიენტ")
LETTERHEAD_LINES = 3

_OCR_PN_CANDIDATE_RE = re.compile(r"(?<![0-9a-z])[0-9oli]{11}(?![0-9a-z])")
_OCR_MAP = str.maketrans({"o": "0", "l": "1", "i": "1"})


@dataclass
class _DateTok:
    start: int
    end: int
    raw: str
    kind: str                 # full | partial | year
    y: int | None
    m: int | None
    d: int | None
    suffix: str = ""

    def iso(self, year: int | None = None) -> str | None:
        y = self.y if self.y is not None else year
        if y is None or self.m is None or self.d is None:
            return None
        try:
            return _dt.date(y, self.m, self.d).isoformat()
        except ValueError:
            return None


def _snippet(s: str) -> str:
    s = " ".join(s.split())
    return s if len(s) <= EVIDENCE_MAX else s[:EVIDENCE_MAX - 1] + "…"


def _fv(value, raw, page, conf, evidence, **kw) -> FieldValue:
    return FieldValue(value=value, raw=raw, source=FieldSource.DOCUMENT.value, page_index=page,
                      confidence=round(conf, 3), evidence=_snippet(evidence), **kw)


# --- თარიღების ტოკენიზაცია -----------------------------------------------------------------------
def date_tokens(text: str) -> list[_DateTok]:
    """ყველა თარიღის მსგავსი ჩანაწერი (გადაფარვების გარეშე), პოზიციის მიხედვით დალაგებული."""
    taken: list[tuple[int, int]] = []
    out: list[_DateTok] = []
    for kind, rx in _DATE_PATTERNS:
        for m in rx.finditer(text):
            if any(m.start() < e and m.end() > s for s, e in taken):
                continue
            gd = m.groupdict()
            y = int(gd["y"]) if gd.get("y") else None
            d = int(gd["d"]) if gd.get("d") else None
            mon = _MONTHS[gd["mon"]] if gd.get("mon") else (int(gd["m"]) if gd.get("m") else None)
            if y is not None and not 1900 <= y <= 2100:
                continue
            tok = _DateTok(m.start(), m.end(), m.group(0).strip(), "year" if kind == "year" else
                           "partial" if kind == "dm" else "full", y, mon, d, gd.get("suf") or "")
            if tok.kind == "full" and tok.iso() is None:
                continue  # არასწორი თარიღი (31.02 და მსგ.) უარყოფილია
            if tok.kind == "partial" and (d is None or not 1 <= d <= 31):
                continue
            taken.append((m.start(), m.end()))
            out.append(tok)
    out.sort(key=lambda t: t.start)
    return out


def _range_role(text: str, tok: _DateTok) -> str | None:
    tail = text[tok.end:tok.end + 12]
    if tok.suffix.endswith("დან") or _RANGE_START_RE.match(tail):
        return "start"
    if tok.suffix.endswith("მდე") or _RANGE_END_RE.match(tail):
        return "end"
    return None


# --- თარიღის ველები ------------------------------------------------------------------------------
def _document_date(pages: list[tuple[int, str]]) -> FieldValue:
    """გამოცემის/შედგენის თარიღი: მინიშნებები, ქალაქის ხაზი, სათაურის ზონა; პერიოდის თარიღები გამორიცხულია."""
    cands: list[tuple[int, int, _DateTok, str, int]] = []  # (score, order, tok, line, page)
    years: Counter[int] = Counter()          # სათაურის/თავფურცლის „2023 წ." ტიპის ჩანაწერები
    years_full: Counter[int] = Counter()     # არაპერიოდული სრული თარიღების წლები (სათადარიგო)
    year_raw: dict[int, tuple[int, str]] = {}
    for page, text in pages:
        lines = text.split("\n")
        tz_len = len(title_zone(text))
        for tok in date_tokens(text):
            line = line_at(text, tok.start)
            if tok.kind == "year":
                years[tok.y] += 1
                year_raw.setdefault(tok.y, (page, tok.raw))
                continue
            if tok.kind != "full":
                continue
            score = 0
            if _PERIOD_CUE_RE.search(line) or _range_role(text, tok):
                score -= 4   # პერიოდის თარიღი (-დან/-მდე/ჩათვლით) არასდროს არის გამოცემის თარიღი
            else:
                years_full[tok.y] += 1
                year_raw.setdefault(tok.y, (page, tok.raw))
            if _ISSUE_CUE_RE.search(line):
                score += 3
            if _CITY_LINE_RE.match(line):
                score += 2
            if page == pages[0][0] and tok.start < tz_len:
                score += 2
            if page == pages[-1][0] and line in lines[-5:]:
                score += 1
            if "№" in line or " n " in f" {line} ":
                score += 1
            cands.append((score, len(cands), tok, line, page))
    cands.sort(key=lambda c: (-c[0], c[1]))
    best = next((c for c in cands if c[0] >= 0), None)
    if best is None:
        # სრული გამოცემის თარიღი არ არის → წელი კანდიდატად (თავფურცლის „2023 წ." სჯობს სხვა წლებს);
        # პერიოდის თარიღების წლები არ გამოიყენება; სრული თარიღი არ იგონება
        pool = years or years_full
        if not pool:
            return FieldValue()
        y, _ = pool.most_common(1)[0]
        page, raw = year_raw.get(y, (pages[0][0], str(y)))
        return _fv(None, raw, page, 0.4 if years else 0.3, raw, candidates=[{"year": y}])
    score, _, tok, line, page = best
    conf = {3: 0.9, 2: 0.75, 1: 0.6}.get(min(score, 3), 0.5) if score >= 1 else 0.5
    others = sorted({c[2].iso() for c in cands if c[0] >= score - 1 and c[2].iso() != tok.iso()})
    if others:
        conf = max(0.3, conf - 0.2)
    return _fv(tok.iso(), tok.raw, page, conf, line, candidates=others)


def _year_only(pages: list[tuple[int, str]]) -> FieldValue:
    for page, text in pages:
        for tok in date_tokens(text):
            if tok.kind == "year":
                return _fv(None, tok.raw, page, 0.4, line_at(text, tok.start), candidates=[{"year": tok.y}])
    return FieldValue()


def _date_range(pages: list[tuple[int, str]], cue_re: re.Pattern[str]) -> tuple[FieldValue, FieldValue]:
    """„-დან ... -მდე" დიაპაზონი მინიშნების ხაზზე (+ მომდევნო ხაზი); წელი ნაწილობრივ თარიღს მეზობლიდან."""
    for page, text in pages:
        lines = text.split("\n")
        for i, ln in enumerate(lines):
            if not cue_re.search(ln):
                continue
            block = ln + ("\n" + lines[i + 1] if i + 1 < len(lines) else "")
            toks = [t for t in date_tokens(block) if t.kind != "year"]
            if not toks:
                continue
            start = next((t for t in toks if _range_role(block, t) == "start"), None)
            end = next((t for t in toks if _range_role(block, t) == "end"), None)
            if start is None and end is None:
                if len(toks) >= 2:
                    start, end = toks[0], toks[1]
                elif _START_CUE_RE.search(ln) and not _END_CUE_RE.search(ln):
                    start = toks[0]
                elif _END_CUE_RE.search(ln):
                    end = toks[0]
            if start is None and end is None:
                continue
            year = next((t.y for t in (start, end) if t is not None and t.y is not None), None)
            fvs = []
            for t in (start, end):
                iso = t.iso(year) if t is not None else None
                conf = 0.85 if t is not None and t.kind == "full" else 0.65
                fvs.append(_fv(iso, t.raw, page, conf, block) if iso else FieldValue())
            if fvs[0].value or fvs[1].value:
                return fvs[0], fvs[1]
    return FieldValue(), FieldValue()


# --- სხვა ველები -----------------------------------------------------------------------------------
def _vacation_days(pages: list[tuple[int, str]]) -> FieldValue:
    fallback = None
    for page, text in pages:
        for ln in text.split("\n"):
            m = _VACATION_DAYS_RE.search(ln)
            if not m:
                continue
            if _VACATION_CUE_RE.search(ln):
                return _fv(int(m.group(1)), m.group(0), page, 0.85, ln)
            fallback = fallback or _fv(int(m.group(1)), m.group(0), page, 0.55, ln)
    return fallback or FieldValue()


def _currency(code_raw: str) -> str | None:
    return next((code for key, code in _CURRENCY if key in code_raw), None)


def _fine(pages: list[tuple[int, str]], is_fine_type: bool) -> tuple[FieldValue, FieldValue, FieldValue]:
    amount = currency = FieldValue()
    fallback: tuple[FieldValue, FieldValue] | None = None
    reason = FieldValue()
    for page, text in pages:
        for ln in text.split("\n"):
            m = _AMOUNT_RE.search(ln)
            if m and not amount.value:
                val = float(m.group("amt").replace(",", "."))
                val = int(val) if val.is_integer() else val
                cur = _currency(m.group("cur"))
                if _FINE_CUE_RE.search(ln):
                    amount, currency = _fv(val, m.group("amt"), page, 0.85, ln), _fv(cur, m.group("cur"), page, 0.85, ln)
                elif is_fine_type and fallback is None:
                    fallback = (_fv(val, m.group("amt"), page, 0.5, ln), _fv(cur, m.group("cur"), page, 0.5, ln))
            for rx, conf in _REASON_SOURCES:
                r = rx.search(ln)
                if r and conf > reason.confidence:
                    reason = _fv(r.group("r").strip()[:REASON_MAX], r.group("r"), page, conf, ln)
                    break
    if not amount.value and fallback:
        amount, currency = fallback
    return amount, currency, reason


def _branch(pages: list[tuple[int, str]]) -> FieldValue:
    for page, text in pages:
        for ln in text.split("\n"):
            if "ფილიალ" not in ln:
                continue
            m = _BRANCH_RE.search(ln) or _BRANCH_BEFORE_RE.search(ln)
            if m:
                return _fv(m.group("b").strip(), m.group(0), page, 0.6, ln)
    return FieldValue()


def _position_value(phrase: str) -> str | None:
    """„გაყიდვების მენეჯერის" → „გაყიდვების მენეჯერი" (მხოლოდ ბოლო სიტყვა გადადის სახელობითში)."""
    words = phrase.split()
    while words and words[0].startswith(_NOT_POSITION_WORDS):   # „დასაქმებული მენეჯერის" → „მენეჯერის"
        words.pop(0)
    if not words or len(words) > _POSITION_MAX_WORDS or words[-1].startswith(_NOT_POSITION_WORDS):
        return None
    last = words[-1]
    words[-1] = last[:-2] + "ი" if last.endswith("ის") and len(last) > 4 else last
    return " ".join(words)


def _position(pages: list[tuple[int, str]]) -> FieldValue:
    for page, text in pages:
        for ln in text.split("\n"):
            if "თანამდებობ" not in ln and "პოზიცი" not in ln:
                continue
            m = _POSITION_LABEL_RE.search(ln)
            if m:
                val = _position_value(m.group("p"))
                if val:
                    return _fv(val, m.group("p").strip(), page, 0.8, ln)
            m = _POSITION_BEFORE_RE.search(ln)
            if m:
                val = _position_value(m.group("p"))
                if val:
                    return _fv(val, m.group(0), page, 0.7, ln)
    return FieldValue()


def _amount_str(amt: str) -> str:
    val = float(amt.replace(",", "."))
    return str(int(val)) if val.is_integer() else f"{val:g}"


def _salary(pages: list[tuple[int, str]]) -> FieldValue:
    """თანხა ვალუტით ხელფასის მინიშნების ხაზზე („ხელფასი შეადგენს 2000 (ორი ათასი) ლარს"); რამდენიმე → პირველი, დანარჩენი candidates."""
    for page, text in pages:
        lines = text.split("\n")
        for i, ln in enumerate(lines):
            on_line = bool(_SALARY_CUE_RE.search(ln))
            if not on_line and (_FINE_CUE_RE.search(ln)
                                or not any(_SALARY_CUE_RE.search(p) for p in lines[max(0, i - _SALARY_PARA_LINES):i])):
                continue
            amts = [m for m in _AMOUNT_RE.finditer(ln)]
            if not amts:
                continue
            cands = [_amount_str(m.group("amt")) for m in amts[1:]]
            conf = 0.55 if cands else (0.8 if on_line else 0.7)
            return _fv(_amount_str(amts[0].group("amt")), amts[0].group(0), page, conf, ln, candidates=cands)
    return FieldValue()


def _add_months(iso: str, n: int) -> str:
    y, m, d = (int(x) for x in iso.split("-"))
    y, m = y + (m - 1 + n) // 12, (m - 1 + n) % 12 + 1
    return _dt.date(y, m, min(d, calendar.monthrange(y, m)[1])).isoformat()


def _term_from_signing(pages: list[tuple[int, str]], doc_date: FieldValue) -> tuple[FieldValue, FieldValue]:
    """ვადა თარიღების გარეშე: „ხელმოწერის დღიდან … N თვის/წლის ვადით" → (დოკუმენტის თარიღი, +N თვე)."""
    if not (isinstance(doc_date.value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", doc_date.value)):
        return FieldValue(), FieldValue()
    for page, text in pages:
        m = _TERM_FROM_SIGNING_RE.search(text)
        if m:
            months = int(m.group("n")) * (12 if m.group("unit") == "წლის" else 1)
            line = line_at(text, m.start())
            conf = min(doc_date.confidence, 0.7)
            return (_fv(doc_date.value, doc_date.raw, doc_date.page_index, conf, line),
                    _fv(_add_months(doc_date.value, months), m.group(0), page, conf, line))
    return FieldValue(), FieldValue()


# --- პირები --------------------------------------------------------------------------------------------
def _people(pages: list[PageText], norm: list[tuple[int, str]], ext: Extraction) -> None:
    emp: Counter[str] = Counter()
    first_hit: dict[str, tuple[int, str, str | None, bool]] = {}
    others: list[str] = []
    ocr_cands: list[str] = []
    for pt, (page, text) in zip(pages, norm):
        from_ocr = pt.text_source.startswith("ocr")
        for h in find_personal_numbers(text):
            if h.role == "other":
                if h.value not in others:
                    others.append(h.value)
                continue
            emp[h.value] += 1
            first_hit.setdefault(h.value, (page, h.line, h.cue, from_ocr and pt.handwriting_suspected))
        if from_ocr:
            for m in _OCR_PN_CANDIDATE_RE.finditer(text):
                raw = m.group(0)
                if any(c.isdigit() for c in raw) and any(c.isalpha() for c in raw):
                    ocr_cands.append(raw.translate(_OCR_MAP))
    ext.employee_personal_numbers = [v for v, _ in emp.most_common()]
    ext.other_personal_numbers = [v for v in others if v not in emp]
    if emp:
        pn, _ = emp.most_common(1)[0]
        page, line, cue, handwritten = first_hit[pn]
        conf = 0.9 if cue else 0.7
        if len(emp) > 1:
            conf = min(conf, 0.5)
            ext.warnings.append("multiple_employee_personal_numbers")
        if handwritten:
            conf -= 0.2
        cands = [v for v in ext.employee_personal_numbers if v != pn] + [c for c in ocr_cands if c != pn]
        ext.fields["personal_number"] = _fv(pn, pn, page, conf, line, handwritten=handwritten, candidates=cands)
    elif ocr_cands:
        ext.fields["personal_number"] = FieldValue(candidates=list(dict.fromkeys(ocr_cands)))

    # სახელები: პრიორიტეტი თანამშრომლის პ/ნ-თან უშუალოდ მიბმულ სახელს; შემდეგ ხელმოწერის ხაზი/მინიშნება
    pn_value = ext.fields["personal_number"].value
    names: Counter[tuple[str, str]] = Counter()
    name_hit: dict[tuple[str, str], tuple[int, str, str]] = {}
    bound: tuple[str, str] | None = None
    for page, text in norm:
        for h in find_names(text):
            if h.role != "employee":
                continue
            key = (h.first, h.last)
            names[key] += 1
            name_hit.setdefault(key, (page, h.line, h.source))
            if bound is None and h.source == "pn" and pn_value and h.pn == pn_value:
                bound = key
    if names:
        key = bound or names.most_common(1)[0][0]
        first, last = key
        page, line, source = name_hit[key]
        conf = 0.9 if bound else {"pn": 0.8, "signature": 0.7, "cue": 0.65}[source]
        cands = [f"{f} {l}" for (f, l), _ in names.most_common() if (f, l) != key]
        if cands:
            conf = min(conf, 0.5) if not bound else conf - 0.1
            ext.warnings.append("multiple_employee_names")
        ext.fields["first_name"] = _fv(first, first, page, conf, line, candidates=cands)
        ext.fields["last_name"] = _fv(last, last, page, conf, line, candidates=cands)


# --- კომპანიები ------------------------------------------------------------------------------------------
# „ერთი მხრივ, შპს X (ს/ნ …)\n(შემდგომში … „დამსაქმებელი“) და მეორე მხრივ …“ — როლი მომდევნო ხაზზეა
_ROLE_INTRO_EMPLOYER_RE = re.compile(r"შემდგომში[^)\n]{0,120}დამსაქმებელ|შემდეგში[^)\n]{0,120}დამსაქმებელ")
_OTHER_SIDE_RE = re.compile(r"მეორე\s+მხრივ|მეორე\s+მხარე")
_ROLE_LOOKAHEAD = 260                     # სიმბოლო; მეორე მხარის/სხვა ორგანიზაციის ხსენებამდე
# ხელმოწერის ბლოკი: ცალკე ხაზზე სათაური „დამსაქმებელი“, მის ქვეშ — კომპანია
_EMPLOYER_HEADER_RE = re.compile(r"^[\s\"'„“”«»:]*დამსაქმებელი[\s\"'„“”«»:]*$")
_TAXID_NEAR_RE = re.compile(r"(?:ს/ნ|ს/კ|საიდენტიფიკაციო\s+(?:კოდი|ნომერი))\s*[:.]?\s*(?:ს/[ნკ]\s*)?(\d{9})(?!\d)")
_FUZZY_MIN_LEN = 6                        # ერთი ასოს სხვაობა მხოლოდ გრძელ სახელზე („არზო 2009“ ≈ „არბო 2009“)


def _company_role(text: str, lines: list[str], first_page: bool, start: int, end: int) -> str:
    line = line_at(text, start)
    if _EMPLOYER_CUE_RE.search(line) or (first_page and line in lines[:LETTERHEAD_LINES]):
        return "employer"
    if _OTHER_CUE_RE.search(line):
        return "other"
    # მიმდინარე ხაზის დარჩენილი ნაწილი + მაქსიმუმ 2 მომდევნო ხაზი, მეორე მხარის/სხვა ორგანიზაციის ხსენებამდე
    ahead = "\n".join(text[end:end + _ROLE_LOOKAHEAD].split("\n")[:3])
    for stop in (_OTHER_SIDE_RE.search(ahead), _ORG_QUOTED_RE.search(ahead), _ORG_PLAIN_RE.search(ahead)):
        if stop:
            ahead = ahead[:stop.start()]
    if _ROLE_INTRO_EMPLOYER_RE.search(ahead):
        return "employer"
    s = text.rfind("\n", 0, start)
    prev = line_at(text, s - 1) if s > 0 else ""
    if _EMPLOYER_HEADER_RE.match(prev):
        return "employer"
    return "unknown"


def _tax_ids_near(text: str, start: int) -> list[str]:
    """ს/ნ კომპანიის ხსენების ხაზზე ან მომდევნო ხაზზე (ხელმოწერის ბლოკში ნომერი სახელის ქვეშაა)."""
    s = text.rfind("\n", 0, start) + 1
    e = text.find("\n", start)
    e2 = text.find("\n", e + 1) if e >= 0 else -1
    return [m.group(1) for m in _TAXID_NEAR_RE.finditer(text[s:e2 if e2 >= 0 else len(text)])]


def _one_edit_apart(a: str, b: str) -> bool:
    """ზუსტად ერთი ასოს ჩანაცვლება/დამატება/წაშლა (OCR-ის ტიპური შეცდომა: „ბ“ → „ზ“)."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long_ = sorted((a, b), key=len)
    i = next((k for k, (x, y) in enumerate(zip(short, long_)) if x != y), len(short))
    return short[i:] == long_[i + 1:]


def _companies(norm: list[tuple[int, str]], cfg: Config, ext: Extraction) -> None:
    matchers = company_matchers(cfg.companies)
    seen: set[tuple[str, str]] = set()
    # კომპანიის ს/ნ: პარამეტრებიდან და იმ ადგილებიდან, სადაც ცნობილი კომპანია დოკუმენტში ს/ნ-ით წერია
    tax_owner: dict[str, str] = {re.sub(r"\D", "", c.tax_id): c.id for c in cfg.companies if c.tax_id and re.sub(r"\D", "", c.tax_id)}
    cores = [(c.id, company_core(a)) for c in cfg.companies for a in (c.name, *c.aliases)]

    def add(cid: str, role: str, page: int, line: str, via: str | None = None) -> None:
        if (cid, role) in seen:
            return
        seen.add((cid, role))
        hit = {"company_id": cid, "role": role, "page_index": page, "evidence": _snippet(line)}
        if via:
            hit["via"] = via
        ext.companies_found.append(hit)

    for page, text in norm:
        lines = text.split("\n")
        for cid, rx in matchers:
            for m in rx.finditer(text):
                for tid in _tax_ids_near(text, m.start()):
                    tax_owner.setdefault(tid, cid)
                add(cid, _company_role(text, lines, page == norm[0][0], m.start(), m.end()), page, line_at(text, m.start()))

    for page, text in norm:
        lines = text.split("\n")
        for rx in (_ORG_QUOTED_RE, _ORG_PLAIN_RE):
            for m in rx.finditer(text):
                name = m.group("n").strip()
                if any(mx.search(name) for _, mx in matchers) or name in ext.unrecognized_companies:
                    continue
                # იგივე კომპანია, ოღონდ სახელი სხვანაირად წაიკითხა: იგივე ს/ნ ან ერთი ასოს სხვაობა გრძელ სახელში
                owner = next((tax_owner[t] for t in _tax_ids_near(text, m.start()) if t in tax_owner), None)
                via = "tax_id" if owner else None
                if owner is None:
                    core = company_core(name)
                    owner = next((cid for cid, k in cores if len(k) >= _FUZZY_MIN_LEN and _one_edit_apart(core, k)), None)
                    via = "spelling" if owner else None
                if owner:
                    add(owner, _company_role(text, lines, page == norm[0][0], m.start(), m.end()), page,
                        line_at(text, m.start()), via)
                    continue
                ext.unrecognized_companies.append(name)


# --- ხელმოწერა -------------------------------------------------------------------------------------------
def _signature_status(pages: list[PageText], norm: list[tuple[int, str]], doc_type: DocType | None) -> str:
    if doc_type is not None and not doc_type.signature_expected:
        return SignatureStatus.NOT_APPLICABLE.value
    if any(p.signature_hint == SignatureStatus.PRESENT.value for p in pages):
        return SignatureStatus.PRESENT.value
    closing = any(has_closing_block(t.split("\n")) or CLOSING_RE.search(t) for _, t in norm)
    if closing and not any(p.has_marks or p.marks_only for p in pages):
        return SignatureStatus.NOT_DETECTED.value
    return SignatureStatus.UNCERTAIN.value


# --- საჯარო API ------------------------------------------------------------------------------------------
def extract(pages: list[PageText], doc_type: DocType | None, cfg: Config) -> Extraction:
    ext = Extraction(fields={k: FieldValue() for k in FIELD_KEYS})
    norm = [(p.page_index, normalize(p.text)) for p in pages if not p.is_blank and (p.text or "").strip()]
    if not norm:
        ext.signature_status = _signature_status(pages, [], doc_type)
        ext.warnings.append("no_text")
        return ext

    _people(pages, norm, ext)
    for page, text in norm:
        dn = find_doc_number(text)
        if dn:
            ext.fields["document_number"] = _fv(dn[0], dn[0], page, 0.8 if page == norm[0][0] else 0.6, dn[1])
            break
    dd = _document_date(norm)
    ext.fields["document_date"] = dd if (dd.value or dd.candidates) else _year_only(norm)
    ext.fields["branch"] = _branch(norm)
    ext.fields["position"] = _position(norm)
    ext.fields["salary"] = _salary(norm)

    # ტიპის ველები: თუ ტიპი ცნობილია, მხოლოდ მისი სიიდან; უცნობი ტიპისთვის — ყველა
    wanted = TYPE_FIELDS if doc_type is None or doc_type.id == "unknown" or not doc_type.fields else set(doc_type.fields)
    if {"contract_start", "contract_end"} & wanted:
        ext.fields["contract_start"], ext.fields["contract_end"] = _date_range(norm, _CONTRACT_CUE_RE)
        if not (ext.fields["contract_start"].value or ext.fields["contract_end"].value):
            ext.fields["contract_start"], ext.fields["contract_end"] = _term_from_signing(norm, ext.fields["document_date"])
    if {"vacation_start", "vacation_end"} & wanted:
        ext.fields["vacation_start"], ext.fields["vacation_end"] = _date_range(norm, _VACATION_CUE_RE)
    if "vacation_days" in wanted:
        ext.fields["vacation_days"] = _vacation_days(norm)
    if {"fine_amount", "fine_currency", "fine_reason"} & wanted:
        amount, currency, reason = _fine(norm, doc_type is not None and doc_type.id == "fine")
        ext.fields["fine_amount"], ext.fields["fine_currency"], ext.fields["fine_reason"] = amount, currency, reason

    _companies(norm, cfg, ext)
    ext.signature_status = _signature_status(pages, norm, doc_type)
    return ext
