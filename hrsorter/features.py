"""გვერდის ნიშნები boundary detector-ისთვის და საერთო ტექსტური „მპოვნელები" extractor-ისთვის.

ყველა რეგექსი მუშაობს `textnorm.normalize`-ის ტექსტზე (პატარა ასოები, ერთიანი ბრჭყალები, ხაზები შენარჩუნებული).
სკანირებული ტექსტი მონაცემია — აქ არაფერი არ „ასრულებს" მას.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from .classifier import Classifier
from .config import Company, Config
from .models import PageFeatures, PageText
from .textnorm import normalize

# --- სათაურის ნიშნები (boundary-ისთვის) ------------------------------------------------
TITLE_MIN_WEIGHT = 2.0     # ამაზე სუსტი სათაურის დამთხვევა გვერდის „სათაურად" არ ითვლება
TITLE_TYPE_MIN = 3.0       # ტიპის სათაურად მისაღებად საჭირო ჯამური წონა (და უნდა სჯობდეს მეორეს)

# --- პირადი ნომერი და როლები -----------------------------------------------------------
PN_RE = re.compile(r"(?<!\d)(\d{11})(?!\d)")
# „დირექტორს" (მიცემითი) მიმართვაა („...-ის დირექტორს"), არა პირის იდენტიფიკაცია → cue არ არის.
OTHER_ROLE_CUES = ("დირექტორ", "წარმომადგენ", "დამსაქმებ", "ხელმძღვანელ", "გენერალურ", "უფლებამოსილ",
                   "ხელმომწერ", "დამფუძნებ")
EMPLOYEE_CUES = ("დასაქმებულ", "თანამშრომ", "განმცხადებ", "მოქალაქ", "სტაჟიორ", "სახელი, გვარი", "სახელი და გვარი",
                 "გვარი, სახელი", "სახელი გვარი")
_OTHER_CUE_RE = re.compile("|".join(re.escape(c) + r"(?!ს\b)" for c in OTHER_ROLE_CUES))
_ROLE_CUE_RE = re.compile("|".join([re.escape(c) + r"(?!ს\b)" for c in OTHER_ROLE_CUES]
                                   + [re.escape(c) for c in EMPLOYEE_CUES]))
# ხელშეკრულების პრეამბულა: „(პ/ნ …) (შემდგომში … მოხსენიებული, როგორც „დასაქმებული“)" — პ/ნ-ის მფლობელის როლი
_AS_ROLE_RE = re.compile(r"როგორც[^ა-ჰ\d]{0,6}(?:შემდგომში[^ა-ჰ\d]{0,6})?(?P<cue>"
                         + "|".join(re.escape(c) for c in OTHER_ROLE_CUES + EMPLOYEE_CUES) + ")")
_AS_ROLE_WINDOW = 200

# --- სახელები --------------------------------------------------------------------------
_GEO = r"[ა-ჰ]{2,}"
_CASE_SUFFIX = r"(?:-(?:ის|ს|მ|მა|თან|სთვის))?"
# OCR „პ/ნ"-ს ხშირად „3/6"/„4/6"-ად კითხულობს (რეალური სკანები; ასევე „8/6") — ეს ვარიანტი მხოლოდ 11-ციფრიანი ნომრის წინ ითვლება
_PN_MARKER = r"\(?\s*(?:პ/ნ|პ\.\s*ნ\.|პირადი\s+(?:ნომერი|№|n)|[348პ]\s*/\s*[6ნ](?=\s*\d{11}))"
# 1) სახელი უშუალოდ პ/ნ-ის მარკერის წინ (შეიძლება მომდევნო ხაზზე): „გიორგი კაპანაძე-ის\nპ/ნ 0100…"
_NAME_PN_RE = re.compile(rf"(?P<w1>{_GEO})\s+(?P<w2>{_GEO}){_CASE_SUFFIX}\s*,?\s*\n?\s*{_PN_MARKER}")
# 2) ხელმოწერის ხაზი: „გიორგი კაპანაძე ხელმოწერა: ____"
_NAME_SIG_RE = re.compile(rf"(?P<w1>{_GEO})\s+(?P<w2>{_GEO}){_CASE_SUFFIX}\s*[:\-]?\s*ხელმოწერა")
# 3) როლის მინიშნება + ორწერტილი/ტირე, ან „მე, …": „დასაქმებული: ნინო კაპანაძე", „მე, გიორგი ბერიძე,"
_NAME_CUE_RE = re.compile(
    r"(?:(?P<cue>" + "|".join(re.escape(c) for c in OTHER_ROLE_CUES + EMPLOYEE_CUES) + r")[ა-ჰ]{0,4}\s*[:\-]"
    r"|(?P<me>\bმე)\s*,)\s*(?:ბატონ[ა-ჰ]*\s+|ქალბატონ[ა-ჰ]*\s+|ბ-ნ[ა-ჰ]*\s+|ქ-ნ[ა-ჰ]*\s+)?"
    rf"(?P<w1>{_GEO})\s+(?P<w2>{_GEO}){_CASE_SUFFIX}")
# გვარის დაბოლოებები ბრუნვის ნიშნებით; (ფუძე, სახელობითი) წყვილები ლემატიზაციისთვის.
_SURNAME_FORMS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"ძ(ე|ის|ეს|ემ)$"), "ძე"), (re.compile(r"შვილ(ი|ის|მა|ს)$"), "შვილი"),
    (re.compile(r"ი(ა|ას|ამ)$"), "ია"), (re.compile(r"ავ(ა|ას|ამ)$"), "ავა"), (re.compile(r"უ(ა|ას|ამ)$"), "უა"),
    (re.compile(r"ელ(ი|ის|მა|ს)$"), "ელი"), (re.compile(r"ან(ი|ის|მა|ს)$"), "ანი"),
    (re.compile(r"ურ(ი|ის|მა|ს)$"), "ური"), (re.compile(r"ულ(ი|ის|მა|ს)$"), "ული"),
    (re.compile(r"ოვ(ი|ის|მა|ს)$"), "ოვი"), (re.compile(r"ევ(ი|ის|მა|ს)$"), "ევი"),
    (re.compile(r"ოვ(ა|ას|ამ)$"), "ოვა"), (re.compile(r"ევ(ა|ას|ამ)$"), "ევა"),
    (re.compile(r"იან(ი|ის|მა|ს)$"), "იანი"), (re.compile(r"სკ(ი|ის|იმ)$"), "სკი"),
]
_NOT_NAME_WORDS = {"ვალდებულია", "უფლებამოსილია", "იღებს", "აცხადებს", "შპს", "სს", "დირექტორი", "სამუშაო",
                   "ადგილია", "შრომის", "პირადი", "ნომერი"}
# ტიტულები/პოზიციები, რომლებიც სახელის წინ დგას და სახელის ნაწილი არ არის
_TITLE_WORDS = {"ბატონ", "ბატონი", "ბატონს", "ქალბატონ", "ქალბატონი", "ქალბატონს"}

# --- დოკუმენტის ნომერი -----------------------------------------------------------------
DOCNUM_RE = re.compile(r"(?:№|#|\bn|ნომერი)\s*[:.]?\s*(\d{1,6}(?:[/-][0-9ა-ჰa-z]{1,6})*)(?![\d/-])")

# --- თავფურცელი (header block) ----------------------------------------------------------
HEADER_LINES = 6           # ამდენ ზედა ხაზში ვეძებთ
HEADER_MIN_HITS = 2        # მინიმუმ ამდენი თავფურცლისებური ხაზი
_LEGAL_FORM = r"(?:შპს|სს|ი/მ|ი\.მ\.|ა\(ა\)იპ|სსიპ|შ\.პ\.ს\.|სპს)"
_ORG_LINE_RE = re.compile(r"^" + _LEGAL_FORM + r"\s*\"?[ა-ჰa-z0-9]")
_TAXID_LINE_RE = re.compile(r"^ს/კ\s*[:.]?\s*\d{6,}|საიდენტიფიკაციო\s+(?:კოდი|ნომერი)\s*[:.]?\s*\d{6,}")
_DOCNO_LINE_RE = re.compile(r"^(?:№|#|n)\s*\d")
_HEADER_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}[./]\d{1,2}[./]\d{4}")
# ხელშეკრულების გახსნა (ზოგადი ქართული ფორმულა): პრეამბულა ან „ერთი მხრივ … მეორე მხრივ"
_OPENING_RE = re.compile(r"\bპრეამბულა\b|ერთი\s+მხრივ.{0,600}?მეორე\s+მხრივ", re.S)
_CITY_DATE_LINE_RE = re.compile(r"^(?:ქ\.\s*)?[ა-ჰ]{3,}\s*,?\s+(?:\d{1,2}[./-]\d{1,2}[./-]\d{4}|\d{4}\s*წ)")
_ADDRESSEE_LINE_RE = re.compile(r"(?:დირექტორს|ხელმძღვანელს|გამგებელს|მენეჯერს)\s*$")
_HEADER_MAX_WORDS = 7

# --- კომპანიები (features-ში, რომ extractor-საც და header-საც ერთი წესი ჰქონდეს) --------------
_LEGAL_PREFIX_RE = re.compile(r"^" + _LEGAL_FORM + r"\s+")


def company_core(name: str) -> str:
    return _LEGAL_PREFIX_RE.sub("", normalize(name).replace('"', "").replace("'", "")).strip()


def company_matchers(companies: list[Company]) -> list[tuple[str, re.Pattern[str]]]:
    """(company_id, regex) ყველა სახელზე/alias-ზე; ქართული ბრუნვის ≤3 ასო დაშვებულია („ალფას")."""
    return list(_compile_company_matchers(tuple((c.id, c.name, tuple(c.aliases)) for c in companies)))


@lru_cache(maxsize=8)
def _compile_company_matchers(spec: tuple[tuple[str, str, tuple[str, ...]], ...]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    out = []
    for cid, name, aliases in spec:
        for alias in (name, *aliases):
            core = company_core(alias)
            if len(core) >= 3:
                out.append((cid, re.compile(r"(?<![ა-ჰa-z0-9])" + re.escape(core) + r"[ა-ჰ]{0,3}(?![ა-ჰa-z0-9])")))
    return tuple(out)


# --- გვერდის მარკერები -----------------------------------------------------------------
_PAGE_MARKERS = [
    re.compile(r"^გვ(?:ერდი|\.)?\s*(\d{1,3})(?:\s*(?:/|-?დან|\s)\s*(\d{1,3})(?:-?დან)?)?\.?$"),
    re.compile(r"^(\d{1,3})\s*/\s*(\d{1,3})$"),
    re.compile(r"^-\s*(\d{1,3})\s*-$"),
]
_BARE_NUMBER_RE = re.compile(r"^(\d{1,3})$")  # მხოლოდ ბოლო ხაზზე
_NOISY_FOOTER_NUMBER_RE = re.compile(r"^\S{0,4}\s?(\d{1,3})$")  # „ღა 3": OCR-ის ნაგავი + გვერდის ნომერი

# --- გაგრძელების ნიშნები ---------------------------------------------------------------
_CONT_START_WORDS = {"და", "რომ", "ან", "თუ", "ასევე", "აგრეთვე", "მაგრამ", "რომელიც", "რომლის", "ხოლო",
                     "რადგან", "ვინაიდან", "შესაბამისად", "ამასთან", "აქედან", "შემდეგ", "მათ", "მას", "მის",
                     "იგი", "მდე", "დან", "ჩათვლით", "ოდენობით", "შესახებ", "მიხედვით", "საფუძველზე"}
_NUMBERED_CLAUSE_RE = re.compile(r"^(\d{1,2})(?:\.(\d{1,2}))*\.?\s+\S")
_TERMINAL_RE = re.compile(r"[.:;!?][\"')»]?$")
_CLAUSE_NUM_RE = re.compile(r"^(?:მუხლი\s+)?(\d{1,2})(?:\.(\d{1,2}))*\.?(?=\s|:|$)")  # „2.3", „მუხლი 5:"
_BODY_MIN_WORDS = 6     # სათაურის გარეშე ამდენსიტყვიანი პირველი ხაზი → სხეულის ტექსტი
_MID_MIN_WORDS = 4      # ბოლო ხაზი ამაზე მოკლე → წინადადების შუაში შეწყვეტად არ ჩაითვლება

# --- დასასრულის/ხელმოწერის ბლოკი ---------------------------------------------------------
CLOSING_RE = re.compile(
    r"ხელმოწერ|ხელმომწერ|\bბ\.\s*ა\.|\bმ\.\s*პ\.|_{3,}|\.{6,}"
    r"|(?:დირექტორ|ხელმძღვანელ|დამსაქმებელ|დასაქმებულ|განმცხადებელ|თანამშრომელ)[ა-ჰ]*\s*[:\-]?\s*[_.\-]{2,}")
_CLOSING_ROLE_LINE_RE = re.compile(
    r"^(?:გენერალური\s+)?(?:დირექტორ|ხელმძღვანელ|დამსაქმებელ|დასაქმებულ|განმცხადებელ|თანამშრომელ)[ა-ჰ]*\b")
CLOSING_TAIL_LINES = 12


@dataclass
class PnHit:
    value: str
    role: str        # employee | other
    cue: str | None  # უახლოესი როლის მინიშნება (None = მინიშნება არ იყო, ნაგულისხმევად employee)
    pos: int
    line: str


@dataclass
class NameHit:
    first: str
    last: str
    role: str        # employee | other
    source: str      # pn | signature | cue
    pos: int
    line: str
    pn: str | None = None   # პ/ნ, რომელსაც სახელი უშუალოდ ახლავს (source=pn)


# --- საჯარო მპოვნელები (extractor-იც იყენებს) ---------------------------------------------
def line_at(text: str, pos: int) -> str:
    s = text.rfind("\n", 0, pos) + 1
    e = text.find("\n", pos)
    return text[s:e if e >= 0 else len(text)]


def pn_role(text: str, pos: int) -> tuple[str, str | None]:
    """პ/ნ-ის როლი უახლოესი მინიშნებით წინა და მიმდინარე ხაზში; მინიშნების გარეშე — employee.
    მომდევნო ხაზი არ ითვლება (იქ უკვე სხვა პირი შეიძლება იყოს). გამონაკლისი: პ/ნ-ის შემდეგ, სხვა ნომრამდე,
    „როგორც „დასაქმებული““ — ეს პირდაპირ ამ პირის როლია."""
    tail = text[pos + 11:pos + 11 + _AS_ROLE_WINDOW]
    nxt = PN_RE.search(tail)
    m = _AS_ROLE_RE.search(tail[:nxt.start()] if nxt else tail)
    if m:
        cue = m.group("cue")
        return ("other" if cue in OTHER_ROLE_CUES else "employee"), cue
    line_start = text.rfind("\n", 0, pos) + 1
    lo = text.rfind("\n", 0, max(line_start - 1, 0)) + 1 if line_start > 0 else 0
    eol = text.find("\n", pos)
    window = text[lo:eol if eol >= 0 else len(text)]
    best: tuple[int, str] | None = None
    for m in _ROLE_CUE_RE.finditer(window):
        dist = abs((lo + m.start()) - pos)
        if best is None or dist < best[0]:
            best = (dist, m.group(0))
    if best is None:
        return "employee", None
    cue = best[1]
    return ("other" if cue in OTHER_ROLE_CUES else "employee"), cue


def find_personal_numbers(text: str) -> list[PnHit]:
    out = []
    for m in PN_RE.finditer(text):
        role, cue = pn_role(text, m.start())
        out.append(PnHit(m.group(1), role, cue, m.start(), line_at(text, m.start())))
    return out


def surname_lemma(word: str) -> str | None:
    """გვარის სახელობითი ფორმა, თუ სიტყვა გვარს ჰგავს; სხვაგვარად None."""
    for rx, nom in _SURNAME_FORMS:
        if rx.search(word) and len(word) > len(nom) + 1:
            return rx.sub(nom, word)
    return None


def _name_pair(w1: str, w2: str, surname_first: bool = False) -> tuple[str, str] | None:
    if w1 in _NOT_NAME_WORDS or w2 in _NOT_NAME_WORDS or w1 in _TITLE_WORDS:
        return None
    s1, s2 = surname_lemma(w1), surname_lemma(w2)
    if surname_first:
        return (w2, s1) if s1 else None
    if s2:
        return w1, s2
    return None


def _role_from_prefix(text: str, pos: int) -> str:
    """როლი იმავე ხაზის წინა ნაწილიდან („დირექტორი: ლევან …" → other)."""
    prefix = text[text.rfind("\n", 0, pos) + 1:pos]
    return "other" if _OTHER_CUE_RE.search(prefix) else "employee"


def find_names(text: str) -> list[NameHit]:
    """სახელი+გვარი სამი წყაროდან (პრიორიტეტით): პ/ნ-ის მარკერის წინ, ხელმოწერის ხაზი, როლის მინიშნება+ორწერტილი.
    მეორე სიტყვა გვარს უნდა ჰგავდეს; საეჭვო წყვილი არ ბრუნდება (არასწორი სახელი უარესია, ვიდრე არარსებული)."""
    out: list[NameHit] = []
    for m in _NAME_PN_RE.finditer(text):
        pair = _name_pair(m.group("w1"), m.group("w2"))
        if not pair:
            continue
        pn_m = PN_RE.search(text, m.end(), m.end() + 20)
        if pn_m:
            role, _ = pn_role(text, pn_m.start())
        else:
            role = _role_from_prefix(text, m.start())
        out.append(NameHit(*pair, role, "pn", m.start(), line_at(text, m.start()), pn_m.group(1) if pn_m else None))
    for m in _NAME_SIG_RE.finditer(text):
        pair = _name_pair(m.group("w1"), m.group("w2"))
        if pair:
            out.append(NameHit(*pair, _role_from_prefix(text, m.start()), "signature", m.start(), line_at(text, m.start())))
    for m in _NAME_CUE_RE.finditer(text):
        cue = m.group("cue")
        pair = _name_pair(m.group("w1"), m.group("w2"), surname_first=bool(cue and cue.startswith("გვარი")))
        if not pair:
            continue
        role = "other" if cue in OTHER_ROLE_CUES else "employee"
        out.append(NameHit(*pair, role, "cue", m.start(), line_at(text, m.start())))
    return out


_ADDRESS_CUE = re.compile(r"ქუჩ|გამზირ|ჩიხ|შესახვევ|მისამართ|ადგილ|ბინა|კორპუს|სართულ")   # „ვარდების ქუჩა #17" — ნომერი არაა


def find_doc_number(text: str) -> tuple[str, str] | None:
    """პირველი „№ 15"/„n 15/2"/„ბრძანება № 34-კ" → (ნომერი, ხაზი); 9+ ციფრიანი (პ/ნ და მსგ.) და მისამართის
    ნომერი („… ქუჩა #17", „სტაჟირების ადგილი: … #22") გამორიცხულია."""
    for m in DOCNUM_RE.finditer(text):
        v = m.group(1)
        if sum(ch.isdigit() for ch in v) >= 9:
            continue
        line = line_at(text, m.start())
        if _ADDRESS_CUE.search(line):
            continue
        return v, line
    return None


def header_block(lines: list[str], matchers: list[tuple[str, re.Pattern[str]]]) -> tuple[bool, str | None]:
    """(თავფურცელია?, კომპანიის id): ზედა ხაზებში ორგანიზაცია/ს/კ/№/ქალაქი+თარიღი/მიმართვა — მოკლე ხაზებზე."""
    hits = 0
    company = None
    for i, ln in enumerate(lines[:HEADER_LINES]):
        if len(ln.split()) > _HEADER_MAX_WORDS:
            continue
        is_header = bool(_ORG_LINE_RE.match(ln) or _TAXID_LINE_RE.match(ln) or _DOCNO_LINE_RE.match(ln)
                         or _CITY_DATE_LINE_RE.match(ln) or _ADDRESSEE_LINE_RE.search(ln))
        for cid, rx in matchers:
            if rx.search(ln):
                is_header = True
                company = company or cid
                break
        if is_header:
            hits += 1
    return hits >= HEADER_MIN_HITS, company


def page_marker(lines: list[str]) -> tuple[int | None, int | None]:
    """„გვერდი 2", „გვ. 2/3", „2/3" ცალკე ხაზზე, „- 2 -" → (გვერდი, სულ) header/footer ხაზებიდან."""
    header, footer = lines[:2], lines[-3:]
    for ln in header + footer:
        for rx in _PAGE_MARKERS:
            m = rx.match(ln)
            if m:
                total = m.group(2) if m.lastindex and m.lastindex >= 2 else None
                return int(m.group(1)), (int(total) if total else None)
    if lines:
        m = _BARE_NUMBER_RE.match(lines[-1]) or _NOISY_FOOTER_NUMBER_RE.match(lines[-1])
        if m and 0 < int(m.group(1)) < 500:
            return int(m.group(1)), None
    return None, None


def is_marker_line(line: str) -> bool:
    return any(rx.match(line) for rx in _PAGE_MARKERS) or bool(_BARE_NUMBER_RE.match(line))


def has_closing_block(lines: list[str]) -> bool:
    tail = lines[-CLOSING_TAIL_LINES:]
    if any(CLOSING_RE.search(ln) for ln in tail):
        return True
    return any(_CLOSING_ROLE_LINE_RE.match(ln) and len(ln.split()) <= 5 for ln in lines[-6:])


def _content_lines(lines: list[str]) -> list[str]:
    return [ln for ln in lines if not is_marker_line(ln)]


def starts_with_continuation(lines: list[str], has_title: bool) -> bool:
    content = _content_lines(lines)
    if has_title or not content:
        return False
    first = content[0]
    tok = first.split()[0]
    if tok in _CONT_START_WORDS or first[0] in ",;)»":
        return True
    m = _NUMBERED_CLAUSE_RE.match(first)
    if m and any(int(g) > 1 for g in m.groups() if g):
        return True
    return len(first.split()) >= _BODY_MIN_WORDS and not first.endswith(":")


def header_date(lines: list[str]) -> str | None:
    """პირველი თარიღი ზედა ხაზებში — რეალურ დოკუმენტებში სათაური+თარიღი ხშირად ყოველ გვერდზე მეორდება."""
    for ln in lines[:HEADER_LINES]:
        m = _HEADER_DATE_RE.search(ln)
        if m:
            return m.group(0)
    return None


def body_lines(lines: list[str], title_text: str | None) -> list[str]:
    """ხაზები სათაურის ხაზისა და ქალაქი/თარიღის ხაზების შემდეგ (განმეორებადი გვერდის თავის გარეშე)."""
    start = 0
    for i, ln in enumerate(lines[:HEADER_LINES + 2]):
        in_title = bool(title_text and ln.strip(" _-–") and ln.strip(" _-–") in title_text)  # გადატანილი სათაურის ნაწილიც
        short_dated = len(ln.split()) <= 5 and bool(_HEADER_DATE_RE.search(ln))           # „ქ. თბილისი 2026-09-16"
        if in_title or short_dated or _CITY_DATE_LINE_RE.match(ln):
            start = i + 1
    return lines[start:]


def body_continues(lines: list[str]) -> bool:
    """სათაურის/თავის შემდეგ ტექსტი აშკარად გრძელდება: მაკავშირებელი სიტყვით/სასვენით ან პუნქტის ნომრით > 1
    („2.3", „მუხლი 5"). გრძელი პირველი წინადადება აქ ნიშანი არ არის — სრულ დოკუმენტშიც ასეა."""
    content = _content_lines(lines)
    if not content:
        return False
    first = content[0]
    if first.split()[0] in _CONT_START_WORDS or first[0] in ",;)»":
        return True
    m = _CLAUSE_NUM_RE.match(first)
    return bool(m and any(int(g) > 1 for g in m.groups() if g))


def ends_mid_sentence(lines: list[str]) -> bool:
    content = [ln for ln in _content_lines(lines) if not CLOSING_RE.search(ln) and not _CLOSING_ROLE_LINE_RE.match(ln)]
    if not content:
        return False
    last = content[-1]
    return len(last.split()) >= _MID_MIN_WORDS and not _TERMINAL_RE.search(last)


# --- სათაური ---------------------------------------------------------------------------
def title_signal(classifier: Classifier, text: str) -> tuple[str | None, str | None]:
    """(title_type_id, title_text): სათაურისებურ ხაზებზე title_only დამთხვევები; ტიპი — თუ ცალსახად სჯობს."""
    per_type: dict[str, float] = {}
    best_line: tuple[float, str] | None = None
    for h in classifier.title_hits(text):
        per_type[h.type_id] = per_type.get(h.type_id, 0.0) + h.weight
        if h.weight >= TITLE_MIN_WEIGHT and (best_line is None or h.weight > best_line[0]):
            best_line = (h.weight, h.line)
    if best_line is None:
        return None, None
    ranked = sorted(per_type.items(), key=lambda kv: kv[1], reverse=True)
    type_id = None
    if ranked and ranked[0][1] >= TITLE_TYPE_MIN and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]):
        type_id = ranked[0][0]
    return type_id, best_line[1]


# --- მთავარი -----------------------------------------------------------------------------
def page_features(pt: PageText, classifier: Classifier, cfg: Config) -> PageFeatures:
    text = normalize(pt.text)
    if pt.is_blank or pt.marks_only or not text:
        return PageFeatures(pt.page_index, pt.is_blank, pt.marks_only, None, None, None, [], [], None, None, None,
                            False, False, bool(pt.marks_only and pt.has_marks), len(text), pt.ocr_quality)
    lines = text.split("\n")
    title_type, title_text = title_signal(classifier, text)
    pns = sorted({h.value for h in find_personal_numbers(text) if h.role == "employee"})
    names = sorted({f"{h.first} {h.last}" for h in find_names(text) if h.role == "employee"})
    docnum = find_doc_number(text)
    page_no, page_total = page_marker(lines)
    is_header, header_company = header_block(lines, company_matchers(cfg.companies))
    return PageFeatures(
        page_index=pt.page_index, is_blank=False, marks_only=False,
        title_type_id=title_type, title_text=title_text,
        page_type=classifier.classify_text(pt.text),
        personal_numbers=pns, employee_names=names,
        doc_number=docnum[0] if docnum else None,
        page_number=page_no, page_total=page_total,
        starts_with_continuation=starts_with_continuation(lines, title_text is not None),
        ends_mid_sentence=ends_mid_sentence(lines),
        has_closing_block=has_closing_block(lines),
        text_len=len(text), ocr_quality=pt.ocr_quality,
        has_header_block=is_header, header_company=header_company,
        header_date=header_date(lines), has_opening=bool(_OPENING_RE.search(text[:3000])),
        body_starts_mid=body_continues(body_lines(lines, title_text)),
    )
