"""სინთეზური ქართული HR PDF-ების გენერატორი ground truth-ით (ტესტები, შეფასება).

ყველა პირი, კომპანია და ნომერი გამოგონილია. ფონტი: Sylfaen (Windows). კომპანიები: `acme` = შპს „მაგალითი",
`beta` = შპს „ბეტა" (config/mock_employees.example.json-ის მიხედვით).

API:
    make_document(type_id, rng, **overrides) -> SynthDoc
    render_pdf(docs, path, *, scan_like=False, rotate_pages=None, noise=0.0, image_only=False) -> dict (ground truth)
    make_batch(path, n_docs, seed, **opts) -> dict
    make_singles(dir, n, seed) -> list[dict]
    make_corrupt(path); make_encrypted(path)
    blank_doc() -> SynthDoc   # ცარიელი გვერდი დოკუმენტებს შორის (ground truth-ში blank_pages)

CLI: python -m tools.synthetic --out <dir> --batch-docs 40 --singles 30 --seed 1

Ground truth (render_pdf): {"file", "pages", "documents": [{"index","start","end","type_id","employee_id",
"personal_number","first_name","last_name","company_id","doc_number","document_date","year","fields","tags",
"page_tags"}], "blank_pages": [...], "page_tags": {"<page>": [...]}, "duplicates": [[i, j], ...]}.
გვერდის ინდექსები 0-დან, start/end ჩათვლით.
"""
from __future__ import annotations

import argparse
import copy
import io
import json
import math
import random
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from pypdf import PdfReader, PdfWriter
from reportlab import rl_config
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

rl_config.useA85 = 0   # ბინარული stream-ები: სუფთა-Python ASCII85 რასტრულ გვერდებზე ძალიან ნელია

FONT_PATH = "C:/Windows/Fonts/sylfaen.ttf"
FONT = "Sylfaen"
PAGE_W, PAGE_H = A4
MARGIN = 60
PROJECT_DIR = Path(__file__).resolve().parent.parent
MOCK_EMPLOYEES = PROJECT_DIR / "config" / "mock_employees.example.json"

_font_registered = False


def _ensure_font() -> None:
    global _font_registered
    if not _font_registered:
        pdfmetrics.registerFont(TTFont(FONT, FONT_PATH))
        _font_registered = True


# --- ფიქტიური მონაცემები -------------------------------------------------------------
COMPANIES: dict[str, dict[str, Any]] = {
    "acme": {"name": "შპს „მაგალითი“", "short": "შპს მაგალითი", "tax_id": "000000000", "city": "თბილისი",
             "director": {"first_name": "ლევან", "last_name": "მაისურაძე", "personal_number": "01001000101"}},
    "beta": {"name": "შპს „ბეტა“", "short": "შპს ბეტა", "tax_id": "000000001", "city": "ქუთაისი",
             "director": {"first_name": "თამარ", "last_name": "გელაშვილი", "personal_number": "01001000102"}},
}

_FALLBACK_EMPLOYEES = [
    {"id": "E001", "first_name": "ნინო", "last_name": "ბერიძე", "personal_number": "01001000001", "company_id": "acme", "branch": "თბილისი"},
    {"id": "E002", "first_name": "გიორგი", "last_name": "კაპანაძე", "personal_number": "01001000002", "company_id": "acme", "branch": "ბათუმი"},
    {"id": "E003", "first_name": "გიორგი", "last_name": "კაპანაძე", "personal_number": "01001000003", "company_id": "beta", "branch": "ქუთაისი"},
    {"id": "E004", "first_name": "მარიამ", "last_name": "ლომიძე", "personal_number": "00001000004", "company_id": "beta", "branch": "ქუთაისი"},
]
_EXTRA_EMPLOYEES = [
    {"id": "E005", "first_name": "დავით", "last_name": "წიკლაური", "personal_number": "01001000005", "company_id": "acme", "branch": "თბილისი"},
    {"id": "E006", "first_name": "ანა", "last_name": "ხარაიშვილი", "personal_number": "01001000006", "company_id": "acme", "branch": "თბილისი"},
    {"id": "E007", "first_name": "ლუკა", "last_name": "ჯაფარიძე", "personal_number": "01001000007", "company_id": "beta", "branch": "ქუთაისი"},
    {"id": "E008", "first_name": "სალომე", "last_name": "მჭედლიშვილი", "personal_number": "00001000008", "company_id": "acme", "branch": "ბათუმი"},
    {"id": "E009", "first_name": "ირაკლი", "last_name": "ქავთარაძე", "personal_number": "01001000009", "company_id": "beta", "branch": "ქუთაისი"},
    {"id": "E010", "first_name": "ელენე", "last_name": "გოგოლაძე", "personal_number": "01001000010", "company_id": "acme", "branch": "თბილისი"},
]
POSITIONS = ["მენეჯერი", "ბუღალტერი", "ოპერატორი", "კონსულტანტი", "მძღოლი", "ადმინისტრატორი", "ინჟინერი", "დამლაგებელი"]
MONTHS = ["იანვარი", "თებერვალი", "მარტი", "აპრილი", "მაისი", "ივნისი", "ივლისი", "აგვისტო", "სექტემბერი",
          "ოქტომბერი", "ნოემბერი", "დეკემბერი"]
FINE_REASONS = ["სამუშაოზე სისტემატური დაგვიანება", "შინაგანაწესის დარღვევა", "სამუშაო დავალების შეუსრულებლობა",
                "უსაფრთხოების წესების დარღვევა", "მატერიალური ზიანის მიყენება"]


def employees() -> list[dict[str, Any]]:
    base = _FALLBACK_EMPLOYEES
    if MOCK_EMPLOYEES.exists():
        try:
            base = json.loads(MOCK_EMPLOYEES.read_text(encoding="utf-8"))["employees"]
        except (OSError, ValueError, KeyError):
            pass
    return [dict(e) for e in base] + [dict(e) for e in _EXTRA_EMPLOYEES]


def employee_by_id(eid: str) -> dict[str, Any]:
    for e in employees():
        if e["id"] == eid:
            return e
    raise KeyError(eid)


# --- მოდელი ---------------------------------------------------------------------------
@dataclass
class PageSpec:
    lines: list[tuple[str, str]] = field(default_factory=list)   # (style, text); style: title|meta|body|sig|small|gap|hand
    marks: list[str] = field(default_factory=list)               # signature | stamp | hand_date
    blank: bool = False
    tags: list[str] = field(default_factory=list)
    footer: str | None = None                                    # „გვერდი 2/3"

    def text(self) -> str:
        return "\n".join(t for s, t in self.lines if s != "gap" and t)


@dataclass
class SynthDoc:
    type_id: str
    pages: list[PageSpec]
    employee: dict[str, Any] | None
    company_id: str | None
    doc_number: str | None
    document_date: str | None       # ISO ან None
    year: int | None
    fields: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    render_opts: dict[str, Any] = field(default_factory=dict)   # {"scan_like","noise","skew","image_only","dpi"} — ამ დოკისთვის

    def text(self) -> str:
        return "\n\n".join(p.text() for p in self.pages)


def blank_doc() -> SynthDoc:
    return SynthDoc(type_id="blank", pages=[PageSpec(blank=True, tags=["blank"])], employee=None,
                    company_id=None, doc_number=None, document_date=None, year=None, tags=["blank"])


# --- თარიღები -------------------------------------------------------------------------
def _rand_date(rng: random.Random, year: int | None = None) -> date:
    y = year or rng.randint(2019, 2025)
    start = date(y, 1, 1)
    return start + timedelta(days=rng.randint(0, 364))


def fmt_date(d: date, style: str) -> str:
    if style == "dotted":
        return f"{d.day:02d}.{d.month:02d}.{d.year}"
    if style == "words":
        return f"{d.year} წლის {d.day} {MONTHS[d.month - 1]}"
    if style == "year_only":
        return f"{d.year} წ."
    return ""


def _date_block(rng: random.Random, style: str | None) -> tuple[date | None, str | None, str, int | None]:
    """(თარიღი, ISO, ტექსტი, წელი). style: dotted|words|year_only|missing|None(შემთხვევითი)."""
    style = style or rng.choices(["dotted", "words", "year_only", "missing"], weights=[55, 30, 8, 7])[0]
    d = _rand_date(rng)
    if style == "missing":
        return None, None, "", None
    if style == "year_only":
        return None, None, fmt_date(d, style), d.year
    return d, d.isoformat(), fmt_date(d, style), d.year


# --- განლაგება -------------------------------------------------------------------------
_STYLES = {"title": (14, 22), "meta": (10, 14), "body": (11, 15), "sig": (11, 20), "small": (9, 12),
           "gap": (11, 10), "hand": (11, 20)}


def _wrap(text: str, size: float, width: float) -> list[str]:
    _ensure_font()
    words = text.split()
    lines, cur = [], ""
    for w in words:
        cand = f"{cur} {w}".strip()
        if pdfmetrics.stringWidth(cand, FONT, size) <= width or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def _paginate(paragraphs: list[tuple[str, str]], *, min_pages: int = 1, filler: list[str] | None = None,
              rng: random.Random | None = None, page_markers: bool = False) -> list[PageSpec]:
    """აბზაცები → გვერდები (ფიქსირებული სიმაღლის მიხედვით). სიგნატურის ბლოკი (style sig) ბოლო გვერდზე რჩება."""
    paragraphs = list(paragraphs)
    pool = list(filler or [])
    added = 0
    while True:
        pages = _flow(paragraphs)
        if len(pages) >= min_pages or not pool or added >= 40:
            break
        # ფილერი სიგნატურის ბლოკამდე ჩავსვათ (პუნქტების ნუმერაციით, ციკლურად)
        idx = next((i for i, (s, _) in enumerate(paragraphs) if s == "sig"), len(paragraphs))
        paragraphs.insert(idx, ("body", f"{10 + added}. {pool[added % len(pool)]}"))
        added += 1
    if page_markers and len(pages) > 1:
        for i, p in enumerate(pages):
            p.footer = f"გვერდი {i + 1}/{len(pages)}"
    return pages


def _flow(paragraphs: list[tuple[str, str]]) -> list[PageSpec]:
    usable = PAGE_H - 2 * MARGIN - 30
    width = PAGE_W - 2 * MARGIN
    pages: list[PageSpec] = [PageSpec()]
    y = 0.0
    sig_block: list[tuple[str, str]] = []
    for style, text in paragraphs:
        size, leading = _STYLES[style]
        lines = [""] if style == "gap" else (_wrap(text, size, width) if style in ("body",) else [text])
        for ln in lines:
            if y + leading > usable:
                pages.append(PageSpec())
                y = 0.0
            pages[-1].lines.append((style, ln))
            y += leading
    return pages


# --- ტექსტები ტიპების მიხედვით ------------------------------------------------------------
def _full(e: dict[str, Any]) -> str:
    return f"{e['first_name']} {e['last_name']}"


def _header(company: dict[str, Any], doc_no: str | None, date_txt: str, city: str | None = None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = [("meta", company["name"]), ("meta", f"ს/კ {company['tax_id']}")]
    if doc_no:
        out.append(("meta", f"№ {doc_no}"))
    if date_txt:
        out.append(("meta", f"{city or company['city']}, {date_txt}"))
    out.append(("gap", ""))
    return out


def _director_line(company: dict[str, Any], with_pn: bool) -> str:
    d = company["director"]
    pn = f" (პ/ნ {d['personal_number']})" if with_pn else ""
    return f"დირექტორი: {d['first_name']} {d['last_name']}{pn}    ხელმოწერა: ______________"


_CONTRACT_CLAUSES = [
    ("1. ხელშეკრულების საგანი", "დამსაქმებელი იღებს დასაქმებულს სამუშაოზე, ხოლო დასაქმებული კისრულობს ვალდებულებას, შეასრულოს ამ ხელშეკრულებით, შინაგანაწესითა და თანამდებობრივი ინსტრუქციით განსაზღვრული სამუშაო."),
    ("2. სამუშაო ადგილი და თანამდებობა", "დასაქმებულის სამუშაო ადგილია დამსაქმებლის ფილიალი. დასაქმებული ინიშნება {position} თანამდებობაზე და ექვემდებარება უშუალო ხელმძღვანელს."),
    ("3. ხელშეკრულების ვადა", "{term}"),
    ("4. შრომის ანაზღაურება", "დასაქმებულის ყოველთვიური შრომის ანაზღაურება შეადგენს {salary} ლარს დარიცხული სახით. ხელფასი გაიცემა არაუგვიანეს მომდევნო თვის 5 რიცხვისა, უნაღდო ანგარიშსწორებით, დასაქმებულის საბანკო ანგარიშზე."),
    ("5. სამუშაო დრო და დასვენება", "დასაქმებულს უწესდება კვირაში 40-საათიანი სამუშაო დრო. დასაქმებულს უფლება აქვს ისარგებლოს ანაზღაურებადი შვებულებით წელიწადში არანაკლებ 24 სამუშაო დღის ოდენობით საქართველოს შრომის კოდექსის შესაბამისად."),
    ("6. მხარეთა უფლება-მოვალეობები", "დამსაქმებელი ვალდებულია შექმნას უსაფრთხო სამუშაო პირობები, დროულად გასცეს ანაზღაურება და უზრუნველყოს დასაქმებული სამუშაოს შესრულებისთვის საჭირო საშუალებებით. დასაქმებული ვალდებულია კეთილსინდისიერად შეასრულოს დაკისრებული მოვალეობები, დაიცვას შინაგანაწესი და კონფიდენციალურობის მოთხოვნები."),
    ("7. პასუხისმგებლობა", "ხელშეკრულებით ნაკისრი ვალდებულებების შეუსრულებლობის ან არაჯეროვანი შესრულებისთვის მხარეებს ეკისრებათ პასუხისმგებლობა საქართველოს მოქმედი კანონმდებლობის შესაბამისად. დასაქმებულის მიერ დამსაქმებლისთვის მიყენებული ზიანი ანაზღაურდება კანონით დადგენილი წესით."),
    ("8. ხელშეკრულების შეწყვეტა", "ხელშეკრულება შეიძლება შეწყდეს მხარეთა შეთანხმებით, ვადის გასვლით ან საქართველოს შრომის კოდექსით გათვალისწინებული სხვა საფუძვლით. შეწყვეტის შესახებ მხარე მეორე მხარეს წერილობით აფრთხილებს კანონით დადგენილ ვადაში."),
    ("9. დასკვნითი დებულებები", "ხელშეკრულება შედგენილია ქართულ ენაზე, თანაბარი იურიდიული ძალის მქონე ორ ეგზემპლარად, თითო თითოეული მხარისთვის. ხელშეკრულებაში ცვლილება შეიძლება შევიდეს მხოლოდ მხარეთა წერილობითი შეთანხმებით, რომელიც ხელშეკრულების განუყოფელი ნაწილია."),
]
_FILLER = [
    "დასაქმებული ვალდებულია დაიცვას დამსაქმებლის კომერციული საიდუმლოება როგორც ხელშეკრულების მოქმედების პერიოდში, ასევე მისი შეწყვეტიდან ორი წლის განმავლობაში. კონფიდენციალურ ინფორმაციად ითვლება ყველა ის მონაცემი, რომელიც დასაქმებულისთვის სამსახურებრივი მოვალეობის შესრულებისას გახდა ცნობილი.",
    "დამსაქმებელი უფლებამოსილია საწარმოო აუცილებლობის შემთხვევაში დასაქმებული დროებით, არაუმეტეს ერთი თვის ვადით, გადაიყვანოს სხვა თანაბარი ანაზღაურების სამუშაოზე. ასეთი გადაყვანა არ ითვლება ხელშეკრულების არსებით ცვლილებად.",
    "ზეგანაკვეთური სამუშაო ანაზღაურდება საათობრივი განაკვეთის გაზრდილი ოდენობით მხარეთა შეთანხმების შესაბამისად. ზეგანაკვეთური სამუშაოს შესრულება დასაშვებია მხოლოდ დასაქმებულის თანხმობით, გარდა კანონით გათვალისწინებული შემთხვევებისა.",
    "დასაქმებულს ეძლევა სამუშაო ტანსაცმელი და ინდივიდუალური დაცვის საშუალებები დამსაქმებლის ხარჯით. მათი დაზიანების ან დაკარგვის შემთხვევაში დასაქმებული ვალდებულია აცნობოს უშუალო ხელმძღვანელს არაუგვიანეს მომდევნო სამუშაო დღისა.",
    "მხარეები თანხმდებიან, რომ ხელშეკრულებიდან გამომდინარე ყველა დავა გადაწყდება მოლაპარაკების გზით, ხოლო შეთანხმების მიუღწევლობის შემთხვევაში — საქართველოს სასამართლოების მეშვეობით კანონით დადგენილი წესით.",
    "დასაქმებულის პერსონალური მონაცემები მუშავდება მხოლოდ შრომითი ურთიერთობის მიზნებისთვის პერსონალურ მონაცემთა დაცვის შესახებ საქართველოს კანონის მოთხოვნათა დაცვით და მესამე პირებს არ გადაეცემა კანონით გათვალისწინებული შემთხვევების გარდა.",
    "დასაქმებულის გამოსაცდელი ვადა შეადგენს სამ თვეს. გამოსაცდელი ვადის განმავლობაში დამსაქმებელს უფლება აქვს შეწყვიტოს ხელშეკრულება ნებისმიერ დროს წინასწარი გაფრთხილების გარეშე, ხოლო დასაქმებულს — სამი დღით ადრე წერილობითი შეტყობინებით.",
    "სამუშაო დროის აღრიცხვა წარმოებს დამსაქმებლის მიერ დადგენილი ფორმით. დასაქმებული ვალდებულია სამუშაოზე გამოუცხადებლობის შემთხვევაში დაუყოვნებლივ აცნობოს უშუალო ხელმძღვანელს და წარმოადგინოს დამადასტურებელი დოკუმენტი.",
]


def _signature_block(e: dict[str, Any] | None, company: dict[str, Any], rng: random.Random, *, director_pn: bool,
                     employee_name: bool = True, employee_signs: bool = True) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = [("gap", ""), ("sig", _director_line(company, director_pn))]
    if e is not None and employee_signs:
        nm = f"{_full(e)} " if employee_name else ""
        out.append(("sig", f"დასაქმებული: {nm}ხელმოწერა: ______________"))
    return out


def make_document(type_id: str, rng: random.Random, **ov: Any) -> SynthDoc:
    """ერთი დოკუმენტი. overrides: employee, company_id, date_style, n_pages, page_markers, director_pn,
    extra_employees, company_conflict, handwritten_pn, marks_page, signature(bool), stamp(bool), variant,
    doc_number(str|None|'auto'), no_employee_name_on_sig(bool)."""
    emps = employees()
    e = ov.get("employee") or rng.choice(emps)
    company_id = ov.get("company_id") or e.get("company_id") or "acme"
    company = COMPANIES.get(company_id, COMPANIES["acme"])
    d, iso, date_txt, year = _date_block(rng, ov.get("date_style"))
    doc_no_ov = ov.get("doc_number", "auto")
    doc_no = (f"{rng.randint(1, 250)}/{year or rng.randint(2019, 2025)}" if rng.random() < 0.7 else f"{rng.randint(1, 999):04d}") \
        if doc_no_ov == "auto" else doc_no_ov
    director_pn = bool(ov.get("director_pn", rng.random() < 0.3))
    tags: list[str] = []
    fields: dict[str, Any] = {}
    employee: dict[str, Any] | None = e
    paras: list[tuple[str, str]] = []
    sig_opts = {"director_pn": director_pn}
    position = rng.choice(POSITIONS)
    page_markers = bool(ov.get("page_markers", rng.random() < 0.5))
    min_pages = 1

    if type_id == "contract":
        start = d or _rand_date(rng)
        indefinite = rng.random() < 0.3
        end = None if indefinite else start + timedelta(days=rng.choice([365, 730, 180]))
        fields.update(contract_start=start.isoformat(), contract_end=end.isoformat() if end else None, position=position)
        term = ("ხელშეკრულება უვადოა და ძალაში შედის ხელმოწერის დღიდან." if indefinite else
                f"ხელშეკრულება ძალაში შედის {fmt_date(start, 'dotted')}-დან და მოქმედებს {fmt_date(end, 'dotted')}-ის ჩათვლით.")
        paras += [("meta", company["name"]), ("meta", f"ს/კ {company['tax_id']}"), ("gap", ""),
                  ("title", "შრომითი ხელშეკრულება"), ("meta", f"№ {doc_no}" if doc_no else ""),
                  ("meta", f"{company['city']}, {date_txt}" if date_txt else company["city"]), ("gap", ""),
                  ("body", f"ერთი მხრივ, {company['name']} (ს/კ {company['tax_id']}), წარმოდგენილი დირექტორის "
                           f"{company['director']['first_name']} {company['director']['last_name']}-ის სახით (შემდგომში „დამსაქმებელი“), და მეორე მხრივ, "
                           f"{_full(e)} (პირადი ნომერი {e['personal_number']}) (შემდგომში „დასაქმებული“), დებენ წინამდებარე შრომით ხელშეკრულებას შემდეგზე:")]
        for head, body in _CONTRACT_CLAUSES:
            paras += [("gap", ""), ("body", head), ("body", body.format(position=position, term=term, salary=rng.choice([900, 1200, 1500, 2000, 2500])))]
        paras += _signature_block(e, company, rng, employee_name=not ov.get("no_employee_name_on_sig", rng.random() < 0.4), **sig_opts)
        min_pages = int(ov.get("n_pages", rng.choice([2, 2, 3])))
    elif type_id == "contract_annex":
        n = rng.randint(1, 4)
        paras += _header(company, None, date_txt) + [("title", f"ხელშეკრულების დანართი № {n}"),
                  ("meta", f"შრომით ხელშეკრულებასთან № {doc_no}" if doc_no else ""), ("gap", ""),
                  ("body", f"{company['name']} (დამსაქმებელი) და {_full(e)} (პ/ნ {e['personal_number']}, დასაქმებული) თანხმდებიან შრომით ხელშეკრულებაში ცვლილების შეტანის შესახებ:"),
                  ("body", f"1. დასაქმებულის თანამდებობა განისაზღვროს როგორც {position}."),
                  ("body", f"2. შრომის ანაზღაურება განისაზღვროს {rng.choice([1300, 1700, 2200])} ლარის ოდენობით."),
                  ("body", "3. ხელშეკრულების სხვა პირობები რჩება უცვლელი. დანართი ხელშეკრულების განუყოფელი ნაწილია და ძალაში შედის ხელმოწერის დღიდან.")]
        paras += _signature_block(e, company, rng, **sig_opts)
    elif type_id == "fine":
        amount = rng.choice([50, 100, 150, 200, 300])
        reason = rng.choice(FINE_REASONS)
        fields.update(fine_amount=amount, fine_currency="GEL", fine_reason=reason)
        paras += _header(company, doc_no, date_txt) + [("title", "ბრძანება"), ("title", "დაჯარიმების შესახებ"), ("gap", ""),
                  ("body", f"{reason}ს გამო, შინაგანაწესის და შრომითი ხელშეკრულების პირობების შესაბამისად,"),
                  ("body", "ვბრძანებ:"),
                  ("body", f"1. {position} {_full(e)}-ს (პ/ნ {e['personal_number']}) დაეკისროს დისციპლინური სახდელი — ჯარიმა {amount} (ას{'ი' if amount == 100 else 'ი'}) ლარის ოდენობით."),
                  ("body", "2. ჯარიმის თანხა დაიკავოს დასაქმებულის მიმდინარე თვის შრომის ანაზღაურებიდან."),
                  ("body", "3. ბრძანება გაეცნოს დასაქმებულს ხელმოწერით.")]
        paras += _signature_block(e, company, rng, **sig_opts)
    elif type_id in ("vacation", "vacation_application", "vacation_order"):
        variant = ov.get("variant") or ("application" if type_id == "vacation_application" else
                                        "order" if type_id == "vacation_order" else rng.choice(["application", "order"]))
        start = d or _rand_date(rng)
        days = rng.choice([5, 10, 12, 14, 20, 24])
        end = start + timedelta(days=days - 1)
        fields.update(vacation_start=start.isoformat(), vacation_end=end.isoformat(), vacation_days=days, variant=variant)
        if variant == "application":
            paras += [("meta", f"{company['name']}-ის დირექტორს"), ("meta", f"{company['director']['first_name']} {company['director']['last_name']}-ს"),
                      ("meta", f"{position} {_full(e)}-ის"), ("meta", f"პ/ნ {e['personal_number']}"), ("gap", ""), ("title", "განცხადება"), ("gap", ""),
                      ("body", f"გთხოვთ, მომცეთ ანაზღაურებადი შვებულება {fmt_date(start, 'words')}-დან {fmt_date(end, 'words')}-ის ჩათვლით ({days} კალენდარული დღე)."),
                      ("gap", ""), ("meta", date_txt), ("sig", f"{_full(e)}    ხელმოწერა: ______________")]
            fields["signer"] = "employee"
        else:
            paras += _header(company, doc_no, date_txt) + [("title", "ბრძანება"), ("title", "შვებულების მიცემის შესახებ"), ("gap", ""),
                      ("body", f"{position} {_full(e)}-ის (პ/ნ {e['personal_number']}) განცხადების საფუძველზე,"), ("body", "ვბრძანებ:"),
                      ("body", f"1. {_full(e)}-ს მიეცეს ანაზღაურებადი შვებულება {fmt_date(start, 'dotted')}-დან {fmt_date(end, 'dotted')}-ის ჩათვლით, {days} კალენდარული დღის ოდენობით."),
                      ("body", "2. ბუღალტერიამ უზრუნველყოს საშვებულებო თანხის დარიცხვა.")]
            extra = ov.get("extra_employees") or []
            for i, x in enumerate(extra, start=3):
                paras.append(("body", f"{i}. {_full(x)}-ს (პ/ნ {x['personal_number']}) მიეცეს ანაზღაურებადი შვებულება იმავე პერიოდში."))
            if extra:
                tags.append("multiple_employees")
                fields["employees"] = [{"personal_number": x["personal_number"], "first_name": x["first_name"], "last_name": x["last_name"]} for x in [e, *extra]]
            paras += _signature_block(e, company, rng, employee_signs=False, **sig_opts)
    elif type_id == "application":
        topic = rng.choice([("გთხოვთ, გამომიწეროთ ცნობა სამუშაო ადგილიდან ხელფასის მითითებით საბანკო დაწესებულებაში წარსადგენად.", "ცნობა"),
                            ("გთხოვთ, შემიცვალოთ სამუშაო გრაფიკი და გადამიყვანოთ დილის ცვლაში საოჯახო გარემოებების გამო.", "გრაფიკი"),
                            ("გთხოვთ, გამათავისუფლოთ დაკავებული თანამდებობიდან პირადი განცხადების საფუძველზე ორი კვირის გაფრთხილების ვადის დაცვით.", "გათავისუფლება")])
        fields["topic"] = topic[1]
        paras += [("meta", f"{company['name']}-ის დირექტორს"), ("meta", f"{position} {_full(e)}-ის"), ("meta", f"პ/ნ {e['personal_number']}"),
                  ("gap", ""), ("title", "განცხადება"), ("gap", ""), ("body", topic[0]), ("gap", ""), ("meta", date_txt),
                  ("sig", f"{_full(e)}    ხელმოწერა: ______________")]
        fields["signer"] = "employee"
    elif type_id == "hire_order":
        start = d or _rand_date(rng)
        fields.update(position=position, start_date=start.isoformat())
        paras += _header(company, doc_no, date_txt) + [("title", "ბრძანება"), ("title", "სამსახურში მიღების შესახებ"), ("gap", ""),
                  ("body", "საქართველოს შრომის კოდექსისა და შრომითი ხელშეკრულების საფუძველზე,"), ("body", "ვბრძანებ:"),
                  ("body", f"1. {_full(e)} (პ/ნ {e['personal_number']}) მიღებულ იქნეს სამსახურში {position} თანამდებობაზე {fmt_date(start, 'words')}-დან."),
                  ("body", f"2. დასაქმებულს დაუწესდეს შრომის ანაზღაურება {rng.choice([1000, 1400, 1800])} ლარის ოდენობით თვეში.")]
        if ov.get("company_conflict"):
            other = COMPANIES["beta" if company_id == "acme" else "acme"]
            paras.insert(1, ("meta", other["name"]))
            paras.append(("body", f"3. {other['name']}-მ უზრუნველყოს დასაქმებულის დაშვება სამუშაო სივრცეში და აღჭურვილობის გადაცემა."))
            tags.append("company_conflict")
            fields["companies_mentioned"] = sorted([company_id, "beta" if company_id == "acme" else "acme"])
        paras += _signature_block(e, company, rng, employee_signs=False, **sig_opts)
    elif type_id == "dismissal_order":
        end = d or _rand_date(rng)
        fields.update(dismissal_date=end.isoformat())
        paras += _header(company, doc_no, date_txt) + [("title", "ბრძანება"), ("title", "სამსახურიდან გათავისუფლების შესახებ"), ("gap", ""),
                  ("body", f"{_full(e)}-ის პირადი განცხადების საფუძველზე, საქართველოს შრომის კოდექსის შესაბამისად,"), ("body", "ვბრძანებ:"),
                  ("body", f"1. {position} {_full(e)} (პ/ნ {e['personal_number']}) გათავისუფლდეს დაკავებული თანამდებობიდან {fmt_date(end, 'dotted')}-დან და შეწყდეს მასთან დადებული შრომითი ხელშეკრულება."),
                  ("body", "2. ბუღალტერიამ უზრუნველყოს საბოლოო ანგარიშსწორება კანონით დადგენილ ვადაში.")]
        paras += _signature_block(e, company, rng, employee_signs=False, **sig_opts)
    elif type_id == "explanation":
        paras += [("meta", f"{company['name']}-ის დირექტორს"), ("meta", f"{position} {_full(e)}-ის"), ("meta", f"პ/ნ {e['personal_number']}"),
                  ("gap", ""), ("title", "ახსნა-განმარტება"), ("gap", ""),
                  ("body", f"{fmt_date(d or _rand_date(rng), 'dotted')}-ს სამუშაოზე დავაგვიანე {rng.choice([20, 35, 50])} წუთით საზოგადოებრივი ტრანსპორტის შეფერხების გამო. ვაცნობიერებ შინაგანაწესის დარღვევას და გპირდებით, რომ მომავალში მსგავსი შემთხვევა არ განმეორდება."),
                  ("gap", ""), ("meta", date_txt), ("sig", f"{_full(e)}    ხელმოწერა: ______________")]
        fields["signer"] = "employee"
    elif type_id == "unknown":
        kind = ov.get("variant") or rng.choice(["invoice", "letter"])
        fields["variant"] = kind
        if kind == "invoice":
            employee = None
            paras += _header(company, doc_no, date_txt) + [("title", "ანგარიშ-ფაქტურა"), ("gap", ""),
                      ("body", f"მიმწოდებელი: {company['name']}, ს/კ {company['tax_id']}. მყიდველი: შპს „გამა“, ს/კ 000000002."),
                      ("body", f"1. საკანცელარიო ნივთები — 25 ერთეული — {rng.randint(100, 900)}.00 ლარი."),
                      ("body", f"2. საოფისე ქაღალდი A4 — 10 შეკვრა — {rng.randint(100, 400)}.00 ლარი."),
                      ("body", "სულ გადასახდელი თანხა დღგ-ს ჩათვლით მითითებულია ქვემოთ. გადახდის ვადა: 10 საბანკო დღე."),
                      ("gap", ""), ("sig", "მიმწოდებლის წარმომადგენელი: ______________")]
        else:
            paras += _header(company, doc_no, date_txt) + [("title", "საინფორმაციო წერილი"), ("gap", ""),
                      ("body", "გაცნობებთ, რომ მომავალი კვირიდან ოფისში იგეგმება სავენტილაციო სისტემის პროფილაქტიკური სამუშაოები. გთხოვთ, გაითვალისწინოთ შესაძლო ხმაური და დროებითი შეზღუდვები."),
                      ("body", f"საკონტაქტო პირი: {_full(e)}."), ("gap", ""), ("sig", _director_line(company, director_pn))]
    else:
        raise ValueError(f"unknown synthetic type: {type_id}")

    if director_pn:
        tags.append("director_pn")
        fields["director_personal_number"] = company["director"]["personal_number"]
    if ov.get("handwritten_pn") and employee is not None:
        # პირადი ნომერი ტექსტურ ფენაში არ ჩანს — ხელით ჩაწერილ სურათად იხატება
        paras = [(s, t.replace(f"პ/ნ {e['personal_number']}", "პ/ნ").replace(f"(პირადი ნომერი {e['personal_number']})", "(პირადი ნომერი)")) for s, t in paras]
        paras.insert(next((i for i, (s, _) in enumerate(paras) if s == "sig"), len(paras)), ("hand", f"პირადი ნომერი: {e['personal_number']}"))
        tags.append("handwritten")
        fields["personal_number_handwritten"] = True

    pages = _paginate(paras, min_pages=min_pages, filler=_FILLER if type_id == "contract" else None, rng=rng, page_markers=page_markers)
    if len(pages) > 1:
        tags.append("multi_page")
        if page_markers:
            tags.append("page_markers")
    sig_marks = []
    if ov.get("signature", True):
        sig_marks.append("signature")
    if ov.get("stamp", rng.random() < 0.5) and type_id not in ("application", "explanation", "vacation_application"):
        sig_marks.append("stamp")
    pages[-1].marks.extend(sig_marks)
    fields["signature_present"] = "signature" in sig_marks
    if ov.get("marks_page"):
        pages.append(PageSpec(lines=[], marks=["signature", "stamp", "hand_date"], tags=["marks_only"]))
        tags.append("marks_only_page")
    if not date_txt:
        tags.append("date_missing")
    elif iso is None:
        tags.append("year_only")

    return SynthDoc(type_id=type_id, pages=pages, employee=employee, company_id=company_id, doc_number=doc_no,
                    document_date=iso, year=year, fields=fields, tags=tags)


# --- ხატვა ------------------------------------------------------------------------------
def _pil_font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_PATH, size)


def _signature_image(rng: random.Random) -> Image.Image:
    w, h = 320, 120
    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)
    pts = []
    x = 10.0
    while x < w - 10:
        pts.append((x, h / 2 + rng.uniform(-45, 45)))
        x += rng.uniform(8, 22)
    d.line(pts, fill=20, width=rng.randint(3, 5), joint="curve")
    # ხაზგასმის ტალღა
    d.line([(20, h - 20), (w // 2, h - 30 + rng.uniform(-6, 6)), (w - 20, h - 15)], fill=20, width=2)
    return img.filter(ImageFilter.GaussianBlur(0.6))


def _stamp_image(rng: random.Random, text: str) -> Image.Image:
    s = 260
    img = Image.new("L", (s, s), 255)
    d = ImageDraw.Draw(img)
    d.ellipse([6, 6, s - 6, s - 6], outline=70, width=5)
    d.ellipse([30, 30, s - 30, s - 30], outline=70, width=2)
    f = _pil_font(26)
    d.text((s / 2, s / 2 - 16), text, font=f, fill=70, anchor="mm")
    d.text((s / 2, s / 2 + 18), f"ს/კ {rng.randint(100000000, 999999999)}", font=_pil_font(18), fill=70, anchor="mm")
    img = img.rotate(rng.uniform(-15, 15), resample=Image.BICUBIC, fillcolor=255)
    return img.filter(ImageFilter.GaussianBlur(0.8))


def _handwritten_image(rng: random.Random, text: str) -> Image.Image:
    """„ხელნაწერისმაგვარი" ტექსტი: ყოველი სიმბოლო სხვადასხვა ზომით/გადახრით/დახრით (სუსტი იმიტაცია)."""
    base = 44
    canvas_img = Image.new("L", (max(60, int(len(text) * base * 0.8)), base * 2), 255)
    x = 6
    for ch in text:
        f = _pil_font(base + rng.randint(-6, 6))
        cw = int(f.getlength(ch)) + 10
        glyph = Image.new("L", (cw + 10, base * 2), 255)
        ImageDraw.Draw(glyph).text((5, base * 0.4 + rng.randint(-4, 4)), ch, font=f, fill=25)
        glyph = glyph.rotate(rng.uniform(-9, 9), resample=Image.BICUBIC, fillcolor=255)
        canvas_img.paste(Image.fromarray(np.minimum(np.asarray(canvas_img.crop((x, 0, x + glyph.width, glyph.height))), np.asarray(glyph))), (x, 0))
        x += cw - 8
    return canvas_img.crop((0, 0, min(canvas_img.width, x + 10), base * 2)).filter(ImageFilter.GaussianBlur(0.5))


def _draw_page(c: canvas.Canvas, spec: PageSpec, rng: random.Random, company_name: str) -> None:
    _ensure_font()
    y = PAGE_H - MARGIN
    width = PAGE_W - 2 * MARGIN
    sig_positions: list[tuple[float, float]] = []
    for style, text in spec.lines:
        size, leading = _STYLES[style]
        y -= leading
        if style == "gap" or not text:
            continue
        c.setFont(FONT, size)
        if style == "title":
            c.drawCentredString(PAGE_W / 2, y, text)
        elif style == "meta":
            c.drawRightString(PAGE_W - MARGIN, y, text)
        elif style == "hand":
            label, _, value = text.partition(": ")
            c.drawString(MARGIN, y, label + ":")
            lw = pdfmetrics.stringWidth(label + ": ", FONT, size)
            img = _handwritten_image(rng, value)
            h_pt = 16
            w_pt = img.width * h_pt / img.height
            c.drawImage(ImageReader(img), MARGIN + lw, y - 4, w_pt, h_pt, mask=None)
        else:
            c.drawString(MARGIN, y, text)
            if style == "sig" and "____" in text:
                tw = pdfmetrics.stringWidth(text, FONT, size)
                sig_positions.append((MARGIN + tw - 90, y))
    if spec.footer:
        c.setFont(FONT, 9)
        c.drawCentredString(PAGE_W / 2, 30, spec.footer)
    # ნიშნები (სურათები — ტექსტურ ფენაში არ ხვდება)
    if "signature" in spec.marks:
        if sig_positions:
            x, sy = sig_positions[-1]
        else:
            x, sy = PAGE_W / 2 - 60 + rng.uniform(-40, 40), PAGE_H * 0.3 + rng.uniform(-40, 40)
        c.drawImage(ImageReader(_signature_image(rng)), x, sy - 12, 120, 45, mask=None)
    if "stamp" in spec.marks:
        if sig_positions:
            x, sy = PAGE_W - MARGIN - 150 + rng.uniform(-20, 20), sig_positions[0][1] - 110
        else:
            x, sy = PAGE_W / 2 + 40 + rng.uniform(-30, 30), PAGE_H * 0.3 - 60
        c.drawImage(ImageReader(_stamp_image(rng, company_name)), x, max(40, sy), 110, 110, mask=None)
    if "hand_date" in spec.marks:
        img = _handwritten_image(rng, fmt_date(_rand_date(rng), "dotted"))
        c.drawImage(ImageReader(img), MARGIN + 40, PAGE_H * 0.3 + 70, img.width * 18 / img.height, 18, mask=None)


def _page_pdf(spec: PageSpec, rng: random.Random, company_name: str) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle("")
    if not spec.blank:
        _draw_page(c, spec, rng, company_name)
    c.showPage()
    c.save()
    return buf.getvalue()


def _rasterize(pdf_bytes: bytes, *, dpi: int = 150, rotate: int = 0, noise: float = 0.0, skew: float = 0.0,
               rng: random.Random | None = None) -> Image.Image:
    rng = rng or random.Random(0)
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        page = doc[0]
        bmp = page.render(scale=dpi / 72.0, grayscale=True)
        img = bmp.to_pil().convert("L")
        bmp.close()
        page.close()
    finally:
        doc.close()
    if skew:
        img = img.rotate(skew, resample=Image.BICUBIC, expand=False, fillcolor=255)
    if noise > 0:
        arr = np.asarray(img).astype(np.float32)
        nrng = np.random.default_rng(rng.randrange(2**32))
        arr = arr * (1 - 0.15 * noise) + nrng.normal(0, 255 * noise, arr.shape)   # ფონის „ჭუჭყი" + გაფერმკრთალება
        img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.5 + noise))
    if rotate % 360:
        img = img.rotate(-(rotate % 360), expand=True, fillcolor=255)   # შიგთავსი საათის ისრით `rotate`-ით
    return img


def _image_page_pdf(img: Image.Image, dpi: int) -> bytes:
    w_pt, h_pt = img.width * 72.0 / dpi, img.height * 72.0 / dpi
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(w_pt, h_pt))
    c.drawImage(ImageReader(img), 0, 0, w_pt, h_pt, mask=None)
    c.showPage()
    c.save()
    return buf.getvalue()


def render_pdf(docs: list[SynthDoc], path: str | Path, *, scan_like: bool = False,
               rotate_pages: dict[int, int] | None = None, noise: float = 0.0, image_only: bool = False,
               seed: int = 0) -> dict[str, Any]:
    """დოკუმენტები თანმიმდევრობით ერთ PDF-ში. აბრუნებს ground truth-ს (გვერდის ზუსტი დიაპაზონებით).

    scan_like/noise/image_only — ყველა გვერდზე; `doc.render_opts` — კონკრეტულ დოკზე; rotate_pages —
    {გლობალური გვერდის ინდექსი: 90|180|270} (გვერდი რასტერიზდება და მოაბრუნდება).
    """
    rotate_pages = rotate_pages or {}
    rng = random.Random(seed)
    writer = PdfWriter()
    gt_docs: list[dict[str, Any]] = []
    blank_pages: list[int] = []
    page_tags: dict[str, list[str]] = {}
    page_no = 0
    for di, doc in enumerate(docs):
        start = page_no
        company_name = COMPANIES.get(doc.company_id or "", {}).get("short", "შპს მაგალითი")
        ro = doc.render_opts
        doc_scan = scan_like or bool(ro.get("scan_like"))
        doc_noise = max(noise, float(ro.get("noise", 0.0)))
        doc_image_only = image_only or bool(ro.get("image_only"))
        doc_dpi = int(ro.get("dpi", 150))
        doc_page_tags: dict[str, list[str]] = {}
        for spec in doc.pages:
            tags = list(spec.tags)
            pdf_bytes = _page_pdf(spec, rng, company_name)
            rot = rotate_pages.get(page_no, 0)
            if spec.blank:
                blank_pages.append(page_no)
            if rot or doc_scan or doc_image_only or doc_noise > 0:
                skew = rng.uniform(-1.5, 1.5) if doc_scan else float(ro.get("skew", 0.0))
                img = _rasterize(pdf_bytes, dpi=doc_dpi, rotate=rot, noise=doc_noise if doc_scan or doc_noise else 0.0, skew=skew, rng=rng)
                pdf_bytes = _image_page_pdf(img, doc_dpi)
                img.close()
                tags.append("image_only")
                if rot:
                    tags.append(f"rotated_{rot}")
                if doc_scan or doc_noise > 0:
                    tags.append("low_quality")
            writer.append(PdfReader(io.BytesIO(pdf_bytes)))
            if tags:
                page_tags[str(page_no)] = tags
                doc_page_tags[str(page_no)] = tags
            page_no += 1
        if doc.type_id != "blank":
            e = doc.employee
            gt_docs.append({
                "index": len(gt_docs), "start": start, "end": page_no - 1, "type_id": doc.type_id,
                "employee_id": e.get("id") if e else None, "personal_number": e.get("personal_number") if e else None,
                "first_name": e.get("first_name") if e else None, "last_name": e.get("last_name") if e else None,
                "company_id": doc.company_id, "doc_number": doc.doc_number, "document_date": doc.document_date,
                "year": doc.year, "fields": doc.fields, "tags": sorted(set(doc.tags + list(ro.get("tags", [])))),
                "page_tags": doc_page_tags, "text_hash": _text_hash(doc),
            })
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        writer.write(f)
    dups = []
    seen: dict[str, int] = {}
    for g in gt_docs:
        if g["text_hash"] in seen:
            dups.append([seen[g["text_hash"]], g["index"]])
        else:
            seen[g["text_hash"]] = g["index"]
    return {"file": str(path), "pages": page_no, "documents": gt_docs, "blank_pages": blank_pages,
            "page_tags": page_tags, "duplicates": dups, "seed": seed}


def _text_hash(doc: SynthDoc) -> str:
    import hashlib
    return hashlib.sha256(doc.text().encode("utf-8")).hexdigest()[:16]


# --- ნაკრებები ---------------------------------------------------------------------------
_TYPES = ["contract", "contract_annex", "fine", "vacation", "application", "hire_order", "dismissal_order", "explanation"]


def _scenario_docs(rng: random.Random, n_docs: int) -> tuple[list[SynthDoc], dict[int, int]]:
    """სპეცის §20 სატესტო შემთხვევების დაფარვა + შემთხვევითი დოკუმენტები n_docs-მდე. აბრუნებს (docs, rotate_pages)."""
    emps = employees()
    e1, e2, e3, e4 = (employee_by_id(x) for x in ("E001", "E002", "E003", "E004"))
    docs: list[SynthDoc] = []
    docs.append(make_document("contract", rng, employee=e1, n_pages=3, page_markers=True, date_style="dotted"))
    docs.append(make_document("contract", rng, employee=e4, n_pages=2, page_markers=False, date_style="words"))
    # ზედიზედ ორი სხვადასხვა დოკუმენტი ერთ თანამშრომელზე
    docs.append(make_document("hire_order", rng, employee=e2, director_pn=True))
    docs.append(make_document("contract_annex", rng, employee=e2))
    docs.append(make_document("fine", rng, employee=e1, date_style="missing"))
    docs.append(make_document("vacation", rng, employee=e3, variant="order", date_style="year_only"))
    docs.append(make_document("vacation", rng, employee=e2, variant="application"))   # ერთნაირი სახელი E002/E003
    docs.append(make_document("unknown", rng, variant="invoice"))
    docs.append(make_document("vacation", rng, employee=e1, variant="order", extra_employees=[e2]))
    docs.append(make_document("dismissal_order", rng, employee=e4, marks_page=True))
    docs.append(blank_doc())
    docs.append(make_document("explanation", rng, employee=e3))
    low = make_document("application", rng, employee=e1)
    low.render_opts = {"scan_like": True, "noise": 0.08, "tags": ["low_quality"]}
    docs.append(low)
    img_only = make_document("fine", rng, employee=e2)
    img_only.render_opts = {"image_only": True, "dpi": 200, "tags": ["image_only"]}
    docs.append(img_only)
    docs.append(make_document("hire_order", rng, employee=e4, handwritten_pn=True))
    original = make_document("dismissal_order", rng, employee=e1, director_pn=True)
    docs.append(original)
    docs.append(make_document("hire_order", rng, employee=e3, company_conflict=True))
    rescan = copy.deepcopy(original)
    rescan.render_opts = {"scan_like": True, "noise": 0.05, "tags": ["rescan_duplicate"]}
    docs.append(rescan)
    rotated = make_document("contract_annex", rng, employee=e2)
    docs.append(rotated)
    docs.append(blank_doc())
    while sum(1 for d in docs if d.type_id != "blank") < n_docs:
        t = rng.choice(_TYPES)
        docs.append(make_document(t, rng, employee=rng.choice(emps)))
    # მობრუნებული გვერდი: `rotated` დოკის პირველი გვერდი
    page_no = 0
    rotate_pages: dict[int, int] = {}
    for d in docs:
        if d is rotated:
            rotate_pages[page_no] = rng.choice([90, 180, 270])
        page_no += len(d.pages)
    return docs, rotate_pages


def make_batch(path: str | Path, n_docs: int = 40, seed: int = 1, **opts: Any) -> dict[str, Any]:
    """ერთი დიდი ნაკრები PDF ყველა სპეციალური შემთხვევით (≥18 დოკი ყოველთვის იქმნება, თუ n_docs ნაკლებია — მეტი გამოდის)."""
    rng = random.Random(seed)
    docs, rotate_pages = _scenario_docs(rng, n_docs)
    gt = render_pdf(docs, path, rotate_pages=rotate_pages, seed=seed, **opts)
    gt["mode"] = "batch_pdf"
    return gt


def make_singles(dir: str | Path, n: int = 30, seed: int = 1) -> list[dict[str, Any]]:
    """n ცალკეული PDF (თითო დოკუმენტი). ყოველ მე-5-ზე სპეციალური ვარიანტი."""
    rng = random.Random(seed)
    out_dir = Path(dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    emps = employees()
    results = []
    for i in range(n):
        t = _TYPES[i % len(_TYPES)]
        kw: dict[str, Any] = {"employee": rng.choice(emps)}
        render_kw: dict[str, Any] = {}
        special = i % 5
        if special == 1:
            kw["date_style"] = rng.choice(["missing", "year_only"])
        elif special == 2:
            render_kw["scan_like"] = True
            render_kw["noise"] = 0.06
        elif special == 3:
            kw["director_pn"] = True
            kw["marks_page"] = rng.random() < 0.5
        elif special == 4 and i % 10 == 9:
            t = "unknown"
            kw = {}
        doc = make_document(t, rng, **kw)
        rotate = {0: 90} if (i % 7 == 6) else None
        gt = render_pdf([doc], out_dir / f"single_{i:03d}.pdf", rotate_pages=rotate, seed=seed + i, **render_kw)
        gt["mode"] = "single_document_pdf"
        results.append(gt)
    return results


def make_corrupt(path: str | Path) -> Path:
    """დაზიანებული PDF: სწორი header და %%EOF, მაგრამ უაზრო სხეული (validate_pdf → pdf_corrupt)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
                  b"garbage garbage garbage\nxref\n0 1\n0000000000 65535 f \ntrailer\n<< /Root 1 0 R >>\n%%EOF\n")
    return p


def make_encrypted(path: str | Path, password: str = "secret") -> Path:
    """პაროლით დაშიფრული ერთგვერდიანი PDF (validate_pdf → pdf_encrypted)."""
    rng = random.Random(7)
    doc = make_document("application", rng, employee=employee_by_id("E001"))
    pdf_bytes = _page_pdf(doc.pages[0], rng, "შპს მაგალითი")
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(pdf_bytes)))
    writer.encrypt(password)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("wb") as f:
        writer.write(f)
    return p


# --- CLI ----------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="სინთეზური ქართული HR PDF-ების გენერაცია ground truth-ით")
    ap.add_argument("--out", required=True)
    ap.add_argument("--batch-docs", type=int, default=40)
    ap.add_argument("--singles", type=int, default=30)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    gt: dict[str, Any] = {"seed": a.seed, "batch": None, "singles": [], "corrupt": None, "encrypted": None}
    if a.batch_docs > 0:
        gt["batch"] = make_batch(out / "batch.pdf", a.batch_docs, a.seed)
    if a.singles > 0:
        gt["singles"] = make_singles(out / "singles", a.singles, a.seed)
    gt["corrupt"] = str(make_corrupt(out / "corrupt.pdf"))
    gt["encrypted"] = str(make_encrypted(out / "encrypted.pdf"))
    (out / "ground_truth.json").write_text(json.dumps(gt, ensure_ascii=False, indent=2), encoding="utf-8")
    n_pages = (gt["batch"] or {}).get("pages", 0) + sum(s["pages"] for s in gt["singles"])
    print(f"wrote {out} — batch docs: {len((gt['batch'] or {}).get('documents', []))}, singles: {len(gt['singles'])}, pages: {n_pages}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
