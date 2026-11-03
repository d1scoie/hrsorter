"""დოკუმენტის ვალიდაცია MHR-თან: ველების შედარება თანამშრომლის ბარათთან და (ხელშეკრულებისას) ტექსტის
შედარება MHR-ში არსებულ ორიგინალთან. შედეგი — `ValidationResult` (status, checks, diffs, summary ქართულად).

წესები (docs/MHR_DESIGN.md, „ვალიდაციის წესები"):
  * პ/ნ არ არის / ტიპი უცნობია / ტიპი არ იტვირთება → skipped.
  * MHR მიუწვდომელია (MhrUnavailable) → unavailable; MhrError (მუდმივი) → failed, error = გამონაკლისის ტიპი.
    MhrError("multiple_matches") (ორი აქტიური ჩანაწერი) → failed, ბლოკირებადი personal_number — ხელით გადასაწყვეტი.
  * თანამშრომელი ვერ მოიძებნა → failed (personal_number: missing_in_mhr, ბლოკირებადი).
  * ველები: first_name/last_name — ბრუნვის დაბოლოებები (-ის/-ს/-მა/-მ/-ო), გვარის ლემა (features.surname_lemma)
    და OCR-ის მცირე შეცდომა (მსგავსება ≥ mhr.name_similarity, მხოლოდ ≥5 ასოიან ფორმებზე — „ნინ"≈„ნინა" არ ჩაითვალოს);
    personal_number — ზუსტი; company — config-ის კომპანიის tax_id ↔ MHR company.tax_id, შემდეგ სახელი/aliases
    (raw id-ით არასდროს, თუ MHR სახელს/tax_id-ს აბრუნებს; პროფილის ნაგულისხმევი → skipped);
    position — ბრუნვის მოჭრის შემდეგ მსგავსება ≥ POSITION_SIMILARITY; salary — რიცხვითი (ვალუტა/ფორმატი იგნორირდება);
    contract_start/end — ISO თარიღის ტოლობა. ბლოკირებადია მხოლოდ mismatch (და პ/ნ-ის/ორიგინალის არარსებობა).
    position/salary/contract_*/company — `employments`-იდან იმ პერიოდისა, რომელიც დოკუმენტის თარიღს (contract_start
    ველს, თუ არის, თორემ document_date-ს) მოიცავს (არცერთი — უახლოესი დაწყებით); employments არ არის → მიმდინარე ველები.
  * ხელშეკრულება (mhr.contract_type_ids): ორიგინალი MHR-დან — მხოლოდ `kind: generated` (.docx; ატვირთული სკანი
    ორიგინალი არ არის), იგივე № > დასკანერებულის თარიღთან უახლოესი document_date > უახლესი → ტექსტი: docx —
    zipfile+xml (დაუნომრავი, header/footer ცალკე), PDF — ტექსტური ფენა, უხარისხო გვერდი OCR ჯაჭვით → `compare_texts`;
    + ხელნაწერის ეჭვი page_texts-დან.
  * passed ⇔ არც ერთი ბლოკირებადი შემოწმება და არც ერთი significant diff.

კონსერვატიულობა: წაუკითხავი ორიგინალის გვერდი, ხელნაწერის ეჭვი, ნიშნები ხელმოწერის ზონის გარეთ — ყველა „ცვლილებაა"
(ოპერატორი ნახავს). ცნობილი შეზღუდვა: ლოგო/ბეჭედი გვერდის შუაში has_marks-ს იძლევა და failed-ს გამოიწვევს.
პირადი მონაცემები ტექნიკურ ჟურნალში არ იწერება; დასკანერებული ტექსტი მონაცემია.
"""
from __future__ import annotations

import datetime as _dt
import io
import json
import logging
import re
import uuid
import zipfile
from dataclasses import dataclass, field as dc_field
from difflib import SequenceMatcher
from itertools import zip_longest
from typing import Any
from xml.etree import ElementTree as ET

from . import textquality
from .config import Config
from .db import Repository, now_iso
from .employee_api import _get_path
from .features import company_core, surname_lemma
from .mhr_models import Check, MhrDocRef, MhrEmployee, MhrError, MhrUnavailable, TextDiff, ValidationResult
from .ocr.base import OcrError
from .ocr.chain import build_ocr_chain
from .pdfio import PdfDocument, validate_pdf
from .preprocess import apply_rotation, enhance
from .textcompare import _PUNCT, compare_texts, mark_placeholders
from .textnorm import digits_only, normalize, one_line

log = logging.getLogger(__name__)

LABELS_KA = {
    "first_name": "სახელი", "last_name": "გვარი", "personal_number": "პირადი ნომერი", "company": "კომპანია",
    "position": "თანამდებობა", "salary": "ხელფასი", "contract_start": "ხელშეკრულების დაწყება",
    "contract_end": "ხელშეკრულების დასრულება", "original_contract": "ორიგინალი MHR-ში",
}
POSITION_SIMILARITY = 0.8
_FUZZY_MIN_LEN = 5                                   # მსგავსებით შედარება მხოლოდ ამ სიგრძის ფორმებზე
_CASE_SUFFIXES = ("ისა", "ის", "მა", "ს", "მ", "ო")  # ქართული ბრუნვის დაბოლოებები (სახელები/თანამდებობა)
_PN_RE = re.compile(r"^\d{11}$")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NUMBER_RE = re.compile(r"\d[\d\s.,]*\d|\d")


# --- ველების შედარების დამხმარეები ---------------------------------------------------------------
def _forms(word: str) -> set[str]:
    """სიტყვის შესაძლო ფუძეები: თავად, ბრუნვის დაბოლოების გარეშე (≥3 ასო რჩება), გვარის ლემა."""
    w = one_line(word)
    out = {w}
    for s in _CASE_SUFFIXES:
        if w.endswith(s) and len(w) - len(s) >= 3:
            out.add(w[: -len(s)])
    lem = surname_lemma(w)
    if lem:
        out.add(lem)
    return out


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def _name_match(doc: str, mhr: str, threshold: float) -> bool:
    df, mf = _forms(doc), _forms(mhr)
    if df & mf:
        return True
    return any(len(x) >= _FUZZY_MIN_LEN and len(y) >= _FUZZY_MIN_LEN and _ratio(x, y) >= threshold
               for x in df for y in mf)


def employee_name_matches(first: str | None, last: str | None, employee: MhrEmployee, threshold: float) -> bool:
    """სახელიც და გვარიც ემთხვევა MHR-ის ჩანაწერს (ბრუნვის/OCR-ის ტოლერანტობით — როგორც ვალიდაციაში).
    ცარიელი მხარე — არ ემთხვევა (ხელით ატვირთვისას ორივე სავალდებულოა)."""
    pairs = ((first, employee.first_name), (last, employee.last_name))
    return all(bool(d and m) and _name_match(str(d), str(m), threshold) for d, m in pairs)


def _strip_case_words(text: str) -> str:
    out = []
    for w in one_line(text).split():
        for s in _CASE_SUFFIXES:
            if w.endswith(s) and len(w) - len(s) >= 3:
                w = w[: -len(s)]
                break
        out.append(w)
    return " ".join(out)


def _position_match(doc: str, mhr: str) -> bool:
    a, b = _strip_case_words(doc), _strip_case_words(mhr)
    return a == b or _ratio(a, b) >= POSITION_SIMILARITY


def parse_amount(value: Any) -> float | None:
    """„2 000,50 ლარი" / „2,000.00" / „1500" → რიცხვი; ვალუტა და ფორმატი იგნორირდება."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = _NUMBER_RE.search(str(value))
    if not m:
        return None
    s = m.group(0).replace(" ", "")
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec == "," else ",", "").replace(",", ".")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = head.replace(",", "") + ("." + tail if len(tail) == 2 and "," not in head else tail)
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def _amount_match(doc: Any, mhr: Any) -> bool:
    a, b = parse_amount(doc), parse_amount(mhr)
    return a is not None and b is not None and abs(a - b) < 0.005


def _iso(value: Any) -> str | None:
    m = _ISO_DATE_RE.search(str(value or ""))
    return m.group(0) if m else None


def _date_match(doc: Any, mhr: Any) -> bool:
    return _iso(doc) is not None and _iso(doc) == _iso(mhr)


def _check(field: str, doc_val: Any, mhr_val: Any, match) -> Check:
    label = LABELS_KA[field]
    if doc_val is None or str(doc_val).strip() == "":
        return Check(field, label, doc_val, mhr_val, "missing_in_document", False)
    if mhr_val is None or str(mhr_val).strip() == "":
        return Check(field, label, doc_val, mhr_val, "missing_in_mhr", False)
    if match(doc_val, mhr_val):
        return Check(field, label, doc_val, mhr_val, "match", False)
    return Check(field, label, doc_val, mhr_val, "mismatch", True)


def _field(meta: dict[str, Any], key: str) -> Any:
    f = (meta.get("fields") or {}).get(key) or {}
    return f.get("value")


# --- ორიგინალის არჩევა და ტექსტი --------------------------------------------------------------------
def _date(value: Any) -> _dt.date | None:
    iso = _iso(value)
    try:
        return _dt.date.fromisoformat(iso) if iso else None
    except ValueError:
        return None


def _ref_date(ref: MhrDocRef) -> _dt.date | None:
    """ორიგინალის თარიღი: document_date (API) > created_at."""
    return _date((ref.raw or {}).get("document_date")) or _date(ref.created_at)


def _is_generated(ref: MhrDocRef) -> bool:
    """MHR-ის შექმნილი ორიგინალი (kind generated). kind-ის გარეშე (ძველი/სხვა API) — ითვლება ორიგინალად."""
    raw = ref.raw or {}
    return "kind" not in raw or raw.get("kind") == "generated"


ORIGINAL_DATE_TOLERANCE_DAYS = 7


def original_candidates(refs: list[MhrDocRef], doc_number: str | None, doc_date: str | None) -> list[MhrDocRef]:
    """ორიგინალის კანდიდატები უპირატესობის რიგით. მხოლოდ `kind: generated`; იგივე დოკ. ნომერი სათაურში/ფაილის სახელში
    (MHR-ის ორიგინალს ნომერი არ აქვს — უვნებელი) > დასკანერებულის თარიღთან უახლოესი document_date (თანაბარზე —
    უახლესი) > უახლესი. ცარიელი — ორიგინალი არ არის (ან სხვა თარიღისაა)."""
    pool = [r for r in refs if _is_generated(r)]
    if not pool:
        return []
    if doc_number:
        rx = re.compile(r"(?<![\d/-])" + re.escape(str(doc_number)) + r"(?![\d/-])")
        same = [r for r in pool if rx.search(f"{r.title or ''} {r.filename or ''}")]
        if same:   # ნომერი ზუსტი იდენტიფიკატორია — თარიღის ზღვარი აღარ საჭიროა
            return sorted(same, key=lambda r: (_ref_date(r) or _dt.date.min, r.created_at or ""), reverse=True)

    def newest(r: MhrDocRef) -> tuple[_dt.date, str]:
        return _ref_date(r) or _dt.date.min, r.created_at or ""

    target = _date(doc_date)
    if target is not None:
        # მხოლოდ იმავე თარიღის (±ORIGINAL_DATE_TOLERANCE_DAYS) ორიგინალი — თორემ სხვა (მაგ. წინა) ხელშეკრულებას შევადარებდით
        dated = [r for r in pool if _ref_date(r) is not None
                 and abs((_ref_date(r) - target).days) <= ORIGINAL_DATE_TOLERANCE_DAYS]
        if dated:
            return sorted(dated, key=lambda r: (-abs((_ref_date(r) - target).days), newest(r)), reverse=True)
        if any(_ref_date(r) is not None for r in pool):
            return []
    return sorted(pool, key=newest, reverse=True)


def pick_original(refs: list[MhrDocRef], doc_number: str | None, doc_date: str | None) -> MhrDocRef | None:
    cands = original_candidates(refs, doc_number, doc_date)
    return cands[0] if cands else None


def _content_tokens(text: str) -> set[str]:
    return {w.strip(_PUNCT) for w in normalize(text).split() if len(w.strip(_PUNCT)) >= 3}


@dataclass
class OriginalSet:
    """სკანის დოკუმენტები MHR-ის ორიგინალებთან: `primary` — ხელშეკრულება (სკანს ყველაზე მეტად ჰგავს), `extras` —
    იმავე პაკეტის დანართები (ინსტრუქცია, წესები…), რომელთა გვერდებიც სკანშია; `owner[i]` — i-ური გვერდის დოკუმენტი
    (doc_id; None — ხელშეკრულებისა)."""
    primary: MhrDocRef | None
    data: bytes | None = None
    orig: Any = None
    extras: list[tuple[MhrDocRef, Any]] = dc_field(default_factory=list)
    owner: list[str | None] = dc_field(default_factory=list)


def match_originals(client, cfg: Config, cands: list[MhrDocRef], scanned_texts: list[str]) -> OriginalSet:
    """ერთი კატეგორიის რამდენიმე კანდიდატი (MHR ხელშეკრულებასთან ერთად ინსტრუქციასა და დანართებსაც „contract"-ად
    ინახავს; HR კი ყველას ერთად ბეჭდავს და ერთ ფაილად ასკანერებს): ყოველი გვერდი მიეკუთვნება იმ დოკუმენტს, რომლის
    სიტყვებსაც ყველაზე მეტად შეიცავს; ხელშეკრულება = ყველაზე მეტი გვერდის მფლობელი (თანაბარზე — Jaccard-ით
    უახლოესი). ერთი კანდიდატი — ჩამოტვირთვის გარეშე."""
    owner: list[str | None] = [None] * len(scanned_texts)
    if len(cands) <= 1:
        return OriginalSet(cands[0] if cands else None, owner=owner)
    loaded: list[tuple[MhrDocRef, bytes, Any, set[str]]] = []
    for ref in cands[:ORIGINAL_CANDIDATES_MAX]:
        try:
            data = client.download(ref)
        except MhrError:
            continue
        orig = original_pages(data, cfg)
        if orig is not None:
            loaded.append((ref, data, orig, _content_tokens("\n".join(orig.pages))))
    if not loaded:
        return OriginalSet(cands[0], owner=owner)
    pages = [_content_tokens(t) for t in scanned_texts]
    score = [{x[0].doc_id: (len(pt & x[3]) / len(pt) if pt else 0.0) for x in loaded} for pt in pages]
    scan = set().union(*pages) if pages else set()

    def rank(x):
        best_on = sum(1 for sc in score if sc and max(sc, key=sc.get) == x[0].doc_id and sc[x[0].doc_id] >= PAGE_OWNER_MIN_OVERLAP)
        return (best_on, len(x[3] & scan) / max(1, len(x[3] | scan)))

    primary = max(loaded, key=rank)
    pid = primary[0].doc_id
    for i, sc in enumerate(score):   # გვერდი დანართისაა, თუ მას აშკარად უკეთ ემთხვევა, ვიდრე ხელშეკრულებას
        if not sc:
            continue
        best = max(sc, key=sc.get)
        if best != pid and sc[best] >= PAGE_OWNER_MIN_OVERLAP and sc[best] >= sc[pid] + PAGE_OWNER_MARGIN:
            owner[i] = best
    extras = [(ref, orig) for ref, _d, orig, _t in loaded if ref.doc_id != primary[0].doc_id and ref.doc_id in owner]
    return OriginalSet(primary[0], primary[1], primary[2], extras, owner)


# --- docx (MHR-ის ორიგინალი) -------------------------------------------------------------------------
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
_DOCX_PART_MAX = 50 * 1024 * 1024    # გაშლილი XML-ის ზღვარი (zip-ბომბისგან დაცვა)


def _para_text(p: ET.Element) -> str:
    """w:p → w:t-ების გადაბმა; w:tab → ჰარი; w:br/w:cr → ახალი ხაზი. mc:Fallback (დუბლიკატი) — გამოტოვებულია."""
    out: list[str] = []

    def walk(el: ET.Element) -> None:
        for ch in el:
            if ch.tag == _MC_FALLBACK:
                continue
            if ch.tag == _W + "t":
                out.append(ch.text or "")
            elif ch.tag == _W + "tab":
                out.append(" ")
            elif ch.tag in (_W + "br", _W + "cr"):
                out.append("\n")
            elif ch.tag == _W + "p":   # ჩადგმული აბზაცი (მაგ. text box)
                out.append("\n" + _para_text(ch) + "\n")
            else:
                walk(ch)
    walk(p)
    return "".join(out)


_ROMAN = [(1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"), (50, "l"), (40, "xl"),
          (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]


def _fmt_number(n: int, fmt: str) -> str:
    if fmt in ("lowerLetter", "upperLetter"):
        s = ""
        while n > 0:
            n, r = divmod(n - 1, 26)
            s = chr(ord("a") + r) + s
        return s.upper() if fmt == "upperLetter" else s
    if fmt in ("lowerRoman", "upperRoman"):
        s = ""
        for v, sym in _ROMAN:
            while n >= v:
                s, n = s + sym, n - v
        return s.upper() if fmt == "upperRoman" else s
    return str(n)   # decimal და სხვა რიცხვითი ფორმატები


class _Numbering:
    """Word-ის ავტომატური ნუმერაცია (word/numbering.xml; აბზაცის ან სტილის numPr). ნომერი („1.5.2.“) ტექსტში არ წერია —
    Word-ი ბეჭდვისას ითვლის, სკანზე კი ჩანს; მის გარეშე ყოველი პუნქტი „ციფრები განსხვავდება“ იქნებოდა.
    მრიცხველი abstractNum-ზეა (იგივე სიის სხვადასხვა numId აგრძელებს ნუმერაციას), ზედა დონე ქვედებს ანულებს."""

    def __init__(self, numbering: ET.Element | None, styles: ET.Element | None):
        self.levels: dict[str, dict[int, tuple[int, str, str]]] = {}   # abstractNumId → ilvl → (start, numFmt, lvlText)
        self.num_abs: dict[str, str] = {}
        self.style_num: dict[str, tuple[str, int]] = {}
        self.counters: dict[str, dict[int, int]] = {}
        val = _W + "val"
        for an in (numbering.findall(_W + "abstractNum") if numbering is not None else []):
            lv = {}
            for lvl in an.findall(_W + "lvl"):
                def get(tag: str, default: str) -> str:
                    el = lvl.find(_W + tag)
                    return el.get(val, default) if el is not None else default
                lv[int(lvl.get(_W + "ilvl", "0"))] = (int(get("start", "1") or 1), get("numFmt", "decimal"), get("lvlText", ""))
            self.levels[an.get(_W + "abstractNumId", "")] = lv
        for n in (numbering.findall(_W + "num") if numbering is not None else []):
            a = n.find(_W + "abstractNumId")
            if a is not None:
                self.num_abs[n.get(_W + "numId", "")] = a.get(val, "")
        for st in (styles.findall(_W + "style") if styles is not None else []):
            np_ = st.find(f"{_W}pPr/{_W}numPr")
            if np_ is not None and np_.find(_W + "numId") is not None:
                il = np_.find(_W + "ilvl")
                self.style_num[st.get(_W + "styleId", "")] = (np_.find(_W + "numId").get(val, ""),
                                                             int(il.get(val, "0")) if il is not None else 0)

    def label(self, p: ET.Element) -> str:
        val = _W + "val"
        ppr = p.find(_W + "pPr")
        np_ = ppr.find(_W + "numPr") if ppr is not None else None
        if np_ is not None and np_.find(_W + "numId") is not None:
            il = np_.find(_W + "ilvl")
            num_id, ilvl = np_.find(_W + "numId").get(val, ""), (int(il.get(val, "0")) if il is not None else 0)
        else:
            ps = ppr.find(_W + "pStyle") if ppr is not None else None
            num_id, ilvl = self.style_num.get(ps.get(val, "") if ps is not None else "", ("", 0))
        abs_id = self.num_abs.get(num_id)
        lv = self.levels.get(abs_id or "", {})
        if num_id in ("", "0") or ilvl not in lv:
            return ""
        start, fmt, text = lv[ilvl]
        cnt = self.counters.setdefault(abs_id, {})
        cnt[ilvl] = cnt.get(ilvl, start - 1) + 1
        for deeper in [k for k in cnt if k > ilvl]:
            del cnt[deeper]
        if fmt in ("bullet", "none"):
            return ""

        def part(m: re.Match) -> str:
            k = int(m.group(1)) - 1
            k_start, k_fmt, _ = lv.get(k, (1, "decimal", ""))
            return _fmt_number(cnt.get(k, k_start), k_fmt)
        return re.sub(r"%(\d)", part, text)


def _block_lines(el: ET.Element, num: _Numbering | None = None) -> list[str]:
    """ბლოკის ხაზები დოკუმენტის რიგით: აბზაცი = ხაზ(ებ)ი (ავტო-ნომრით, თუ აქვს); ცხრილი — სტრიქონ-სტრიქონ, უჯრების
    ხაზები გვერდიგვერდ (როგორც დაბეჭდილზე იკითხება: „დამსაქმებელი   სტაჟიორი")."""
    lines: list[str] = []
    for ch in el:
        if ch.tag == _MC_FALLBACK:
            continue
        if ch.tag == _W + "p":
            text = _para_text(ch)
            label = num.label(ch) if num else ""
            lines.extend((f"{label} {text}" if label and text.strip() else text).split("\n"))
        elif ch.tag == _W + "tbl":
            for tr in (x for x in ch if x.tag == _W + "tr"):
                cells = [[ln for ln in _block_lines(tc, num) if ln.strip()] for tc in tr if tc.tag == _W + "tc"]
                lines.extend(" ".join(x for x in row if x) for row in zip_longest(*cells, fillvalue=""))
        else:
            lines.extend(_block_lines(ch, num))
    return lines


def _xml_root(zf: zipfile.ZipFile, name: str) -> ET.Element | None:
    if name not in zf.namelist():
        return None
    info = zf.getinfo(name)
    if info.file_size > _DOCX_PART_MAX:
        raise ValueError("docx part too large")
    return ET.fromstring(zf.read(info))


def _xml_lines(zf: zipfile.ZipFile, name: str, num: _Numbering | None = None) -> list[str]:
    return [ln.strip() for ln in _block_lines(_xml_root(zf, name), num) if ln.strip()]


def docx_text(data: bytes) -> tuple[str, list[str]] | None:
    """docx → (სხეულის ტექსტი დოკუმენტის რიგით, header/footer ხაზები). None — docx არ არის / დაზიანებულია."""
    try:
        with zipfile.ZipFile(io.BytesIO(data or b"")) as zf:
            names = zf.namelist()
            if "word/document.xml" not in names:
                return None
            num = _Numbering(_xml_root(zf, "word/numbering.xml"), _xml_root(zf, "word/styles.xml"))
            body = _xml_lines(zf, "word/document.xml", num)
            extra: list[str] = []
            for n in sorted(names, key=lambda x: ("footer" in x, x)):   # ჯერ header-ები, მერე footer-ები
                if re.fullmatch(r"word/(header|footer)\d*\.xml", n):
                    extra.extend(_xml_lines(zf, n))
    except (zipfile.BadZipFile, ET.ParseError, ValueError, KeyError, OSError, EOFError):
        return None
    return "\n".join(body), list(dict.fromkeys(extra))


@dataclass
class OriginalText:
    pages: list[str]                    # PDF — გვერდები; docx — ერთი ელემენტი (დაუნომრავი)
    unreadable: list[int] = dc_field(default_factory=list)   # წაუკითხავი PDF გვერდები (1-დან)
    paginated: bool = True
    strip_lines: list[str] = dc_field(default_factory=list)  # docx header/footer — შედარებიდან ორივე მხრიდან იშლება


def original_pages(data: bytes, cfg: Config) -> OriginalText | None:
    """MHR-ის ორიგინალი → ტექსტი. docx (zip + word/document.xml) — დაუნომრავი ერთი ტექსტი + header/footer ხაზები;
    PDF — გვერდები (ტექსტური ფენა; უხარისხო → OCR). None — არც docx-ია, არც PDF."""
    if data[:2] == b"PK":
        docx = docx_text(data)
        if docx is None:
            return None
        return OriginalText([docx[0]], paginated=False, strip_lines=docx[1])
    pdf = _pdf_pages(data, cfg)
    return OriginalText(pdf[0], pdf[1]) if pdf else None


def _pdf_pages(pdf_bytes: bytes, cfg: Config) -> tuple[list[str], list[int]] | None:
    """PDF → გვერდების ტექსტი (ტექსტური ფენა; უხარისხო → OCR). (გვერდები, წაუკითხავი 1-დან);
    None — ფაილი PDF არ არის. დროებითი ფაილი paths.work-ში, ბოლოს იშლება."""
    work = cfg.paths.work
    work.mkdir(parents=True, exist_ok=True)
    tmp = work / f"mhr_original_{uuid.uuid4().hex}.pdf"
    min_q = float(cfg.thresholds.get("text_layer_min_quality", 0.6))
    dpi = int(cfg.data["processing"].get("dpi", 300))
    pages: list[str] = []
    unreadable: list[int] = []
    chain = None
    try:
        tmp.write_bytes(pdf_bytes or b"")
        if not validate_pdf(tmp).ok:
            return None
        with PdfDocument(tmp) as doc:
            for i in range(doc.page_count):
                text = doc.text_layer(i)
                if textquality.score(text) >= min_q:
                    pages.append(text)
                    continue
                if chain is None:
                    chain = build_ocr_chain(cfg)
                res = None
                if chain.available():
                    img = doc.render(i, dpi=dpi)
                    try:
                        res = chain.recognize(enhance(img), chain.lang)
                    finally:
                        img.close()
                if res is not None and res.ok and res.result is not None and res.result.text.strip():
                    pages.append(res.result.text)
                else:
                    pages.append("")
                    unreadable.append(i + 1)
    finally:
        tmp.unlink(missing_ok=True)
    return pages, unreadable


# --- ხელნაწერი / ნიშნები ------------------------------------------------------------------------------
def handwriting_diffs(scanned: list[dict[str, Any]]) -> list[TextDiff]:
    """page_texts-ის ნიშნებიდან: ხელნაწერის ეჭვი, ან ნიშნები ხელმოწერის ზონის გარეთ. მხოლოდ-ხელმოწერის გვერდი
    (marks_only, ან signature_hint=present) მოსალოდნელია და ცვლილება არ არის."""
    out = []
    for i, pt in enumerate(scanned, start=1):
        if pt.get("handwriting_suspected"):
            out.append(TextDiff(i, "handwriting", "", "", True, "ხელნაწერის ეჭვი"))
        elif pt.get("has_marks") and not pt.get("marks_only") \
                and pt.get("signature_hint") not in ("present", "not_applicable") \
                and not any((pt.get("signature_parties") or {}).values()):   # მხარის ხელმოწერა/ბეჭედი ნაპოვნია → ეს ისაა
            out.append(TextDiff(i, "handwriting", "", "", True, "ნიშნები ხელმოწერის ზონის გარეთ"))
    return out


_PARTY_KA = {"employer": "დამსაქმებელი", "employee": "დასაქმებული"}
NO_SIGNATURE_BLOCK_MIN_QUALITY = 0.8   # ტექსტი კარგად იკითხება, წარწერა კი არ არის → გვერდზე ხელმოწერის ხაზი არ იბეჭდება
ORIGINAL_CANDIDATES_MAX = 6            # ამდენ კანდიდატ ორიგინალს ჩამოვტვირთავთ შინაარსით ასარჩევად
PAGE_OWNER_MIN_OVERLAP = 0.3           # გვერდის სიტყვების ამ წილზე მეტი ერთ დოკუმენტშია → გვერდი მისია
PAGE_OWNER_MARGIN = 0.1                # დანართს გვერდი მხოლოდ ხელშეკრულებაზე ამდენით უკეთესი დამთხვევით ეკუთვნის


def signature_diffs(scanned: list[dict[str, Any]], already: set[int]) -> list[TextDiff]:
    """ხელშეკრულება: ხელმოწერა ყველა გვერდზე (mhr.signature_every_page) — **ორივე მხარის**, თუ გვერდზე ხელმოწერის
    წარწერები („დამსაქმებელი"/„დასაქმებული") მოიძებნა; სხვაგვარად — გვერდის ზოგადი შეფასება. ცარიელი და
    მხოლოდ-ხელმოწერის გვერდი — გამონაკლისი; `already` — ნიშნების გამო უკვე მონიშნული გვერდები (ორჯერ არ ვწერთ)."""
    out = []
    for i, pt in enumerate(scanned, start=1):
        if i in already or pt.get("is_blank") or pt.get("marks_only"):
            continue
        parties = pt.get("signature_parties") or {}
        missing = [_PARTY_KA[p] for p in ("employer", "employee") if p in parties and not parties[p]]
        if missing:
            out.append(TextDiff(i, "signature", "", "", True, "ხელმოწერა აკლია: " + ", ".join(missing)))
        elif not parties and pt.get("signature_hint") not in ("present", "not_applicable"):
            if pt.get("signature_hint") == "not_detected" and (pt.get("ocr_quality") or 0) >= NO_SIGNATURE_BLOCK_MIN_QUALITY:
                out.append(TextDiff(i, "signature", "", "", False, "ხელმოწერის ადგილი ამ გვერდზე არ არის"))
            else:
                out.append(TextDiff(i, "signature", "", "", True, "ხელმოწერა ვერ მოიძებნა"))
    return out


def _already_uploaded(refs: list[MhrDocRef], original: MhrDocRef | None, document_id: str) -> Check | None:
    """MHR-ში ამ ხელშეკრულების ხელმოწერილი სკანი უკვე არის (signed_copy_of = ორიგინალი) — გაფრთხილება, არა ბლოკი."""
    if original is None:
        return None
    for r in refs:
        raw = r.raw or {}
        if raw.get("kind") == "uploaded" and str(raw.get("signed_copy_of")) == original.doc_id                 and raw.get("external_id") != document_id:
            when = (r.created_at or "")[:10]
            return Check("already_uploaded", "უკვე ატვირთულია", None, r.doc_id, "info", False,
                         note=f"ეს ხელშეკრულება ამ თანამშრომელზე MHR-ში უკვე ატვირთულია{f' ({when})' if when else ''} "
                              "— ხელახლა ატვირთვამდე დარწმუნდი, რომ ეს ახალი ვერსიაა")
    signed = (original.raw or {}).get("signed_at")   # MHR: ატვირთვისას ან HR-ის მიერ ხელით მონიშვნისას ეწერება
    if signed:
        return Check("already_uploaded", "ხელმოწერილად მონიშნული", None, original.doc_id, "info", False,
                     note=f"MHR-ში ორიგინალი ხელმოწერილად მოინიშნა {str(signed)[:10]}, ხელმოწერილი ასლი კი ატვირთული არ ჩანს "
                          "— ატვირთვა შეიძლება; თუ ასლი სხვა გზით უკვე აიტვირთა, ორჯერ არ ატვირთო")
    return None


_CURRENT_STATE_FIELDS = {"position", "salary", "contract_start", "contract_end"}


# --- შეჯამება ------------------------------------------------------------------------------------------------
def _summary(result: ValidationResult, compared_original: bool, compared_template: bool = False) -> list[str]:
    lines: list[str] = []
    for c in result.checks:
        if c.status == "info":
            lines.append(c.note)
        elif c.status == "mismatch" and not c.blocking:
            lines.append(f"შენიშვნა — {c.label_ka}: დოკუმენტში {c.document_value}, MHR-ში ამჟამად {c.mhr_value} "
                         "(ხელშეკრულება ორიგინალს ემთხვევა)")
        elif c.status == "mismatch" and c.mhr_value is None and c.note:
            lines.append(c.note)
        elif c.status == "mismatch":
            lines.append(f"{c.label_ka} განსხვავდება: დოკუმენტში {c.document_value}, MHR-ში {c.mhr_value}")
        elif c.blocking:
            lines.append(c.note or f"{c.label_ka}: MHR-ში ვერ მოიძებნა")
    noise = 0
    for d in result.diffs:
        if not d.significant:
            noise += 1
        elif d.kind == "page_count":
            lines.append(d.reason)
        elif d.kind in ("handwriting", "signature"):
            lines.append(f"გვერდი {d.page}: {d.reason}")
        else:
            lines.append(f"გვერდი {d.page}: ტექსტი შეცვლილია ({d.reason})")
    if compared_template and not result.significant_diffs:
        lines.append("შაბლონთან შედარება: შინაარსობრივი განსხვავება არ არის (თანამშრომლის მონაცემები ცალკე შემოწმდა)")
    elif compared_original and not result.significant_diffs:
        lines.append("ორიგინალთან შედარება: შინაარსობრივი განსხვავება არ არის")
    missing = [c.label_ka for c in result.checks if c.status == "missing_in_document"]   # ნაკლებად მნიშვნელოვანი — ბოლოში
    if missing:
        lines.append("დოკუმენტში ვერ მოიძებნა (შემოწმება გამოტოვებულია): " + ", ".join(missing))
    if noise:
        lines.append(f"OCR-ის ხმაური: {noise} უმნიშვნელო განსხვავება (იგნორირებულია)")
    if result.status == "passed":
        lines.insert(0, "ყველა შემოწმება გავიდა — მზადაა ატვირთვისთვის")
    return lines


def _finish(result: ValidationResult, compared_original: bool = False, compared_template: bool = False) -> ValidationResult:
    blocked = any(c.blocking for c in result.checks) or bool(result.significant_diffs)
    result.status = "failed" if blocked else "passed"
    result.summary = _summary(result, compared_original, compared_template)
    result.validated_at = now_iso()
    return result


# --- საჯარო API -----------------------------------------------------------------------------------------------
def validate_document(repo: Repository, cfg: Config, document_id: str, client) -> ValidationResult:
    """ერთი დოკუმენტის ვალიდაცია MHR-თან. გამონაკლისი არ გამოდის MHR-ის შეცდომებზე — სტატუსში აისახება."""
    row = repo.get_document(document_id)
    if row is None:
        return ValidationResult("skipped", summary=["დოკუმენტი ბაზაში ვერ მოიძებნა"], validated_at=now_iso(),
                                error="document_not_found")
    meta: dict[str, Any] = json.loads(row["metadata"]) if row["metadata"] else {}
    mhr = cfg.data["mhr"]

    pn = row["personal_number"] or meta.get("personal_number") or _field(meta, "personal_number")
    pn = digits_only(str(pn)) if pn else None
    if not pn or not _PN_RE.match(pn):
        return ValidationResult("skipped", summary=["პირადი ნომერი დოკუმენტში არ არის — MHR-თან შედარება გამოტოვებულია"],
                                validated_at=now_iso())
    type_id = row["type_id"] or meta.get("document_type_id")
    if not type_id or type_id == "unknown":
        return ValidationResult("skipped", summary=["დოკუმენტის ტიპი უცნობია — MHR-ზე არ იტვირთება"], validated_at=now_iso())
    upload_types = mhr.get("upload_type_ids") or []
    if upload_types and type_id not in upload_types:
        return ValidationResult("skipped", summary=[f"ტიპი „{type_id}“ MHR-ზე არ იტვირთება"], validated_at=now_iso())

    try:
        return _validate(repo, cfg, row, meta, pn, type_id, client)
    except MhrUnavailable as e:
        log.info("mhr validation %s: unavailable (%s)", document_id, type(e).__name__)
        return ValidationResult("unavailable", summary=["MHR მიუწვდომელია — მოგვიანებით ხელახლა შემოწმდება"],
                                validated_at=now_iso(), error=type(e).__name__)
    except MhrError as e:   # ჩვენი კლიენტის ტექსტი მხოლოდ მანქანური კოდია (პირადი მონაცემის გარეშე)
        log.warning("mhr validation %s: error %s (%s)", document_id, type(e).__name__, str(e)[:80])
        if str(e) == "multiple_matches":
            res = ValidationResult("failed", error="multiple_matches")
            res.checks.append(Check("personal_number", LABELS_KA["personal_number"], pn, None, "mismatch", True,
                                    note="MHR-ში ამ პირადი ნომრით რამდენიმე აქტიური ჩანაწერია — ხელით გადაწყვიტე"))
            return _finish(res)
        return ValidationResult("failed", summary=["MHR-ის შეცდომა (კონფიგურაცია/უფლება) — იხილეთ ტექნიკური ჟურნალი"],
                                validated_at=now_iso(), error=type(e).__name__)


def _validate(repo: Repository, cfg: Config, row, meta: dict[str, Any], pn: str, type_id: str, client) -> ValidationResult:
    mhr = cfg.data["mhr"]
    result = ValidationResult("failed")
    employee: MhrEmployee | None = client.get_employee(pn)
    if employee is None:
        result.checks.append(Check("personal_number", LABELS_KA["personal_number"], pn, None, "missing_in_mhr", True,
                                   note="თანამშრომელი ამ პირადი ნომრით MHR-ში ვერ მოიძებნა"))
        return _finish(result)
    result.employee = employee

    threshold = float(mhr.get("name_similarity", 0.85))
    checks = result.checks
    checks.append(_check("first_name", _field(meta, "first_name"), employee.first_name,
                         lambda d, m: _name_match(d, m, threshold)))
    checks.append(_check("last_name", _field(meta, "last_name"), employee.last_name,
                         lambda d, m: _name_match(d, m, threshold)))
    checks.append(_check("personal_number", pn, employee.personal_number,
                         lambda d, m: digits_only(str(d)) == digits_only(str(m))))
    doc_company = row["company_id"] or meta.get("company_id")
    co = cfg.company(str(doc_company)) if doc_company else None
    ref_date = _date(_field(meta, "contract_start")) or _date(row["document_date"] or meta.get("document_date"))
    period = _period(employee, mhr.get("employee_map") or {}, ref_date, co.tax_id if co else None)
    period_checks = [
        _company_check(cfg, meta, doc_company, co, period.company),
        _check("position", _field(meta, "position"), period.position, _position_match),
        _check("salary", _field(meta, "salary"), period.salary, _amount_match),
        _check("contract_start", _field(meta, "contract_start"), period.contract_start, _date_match),
        _check("contract_end", _field(meta, "contract_end"), period.contract_end, _date_match),
    ]
    for c in period_checks:
        if period.note and c.status != "skipped":
            c.note = f"{c.note}; {period.note}" if c.note else period.note
    checks.extend(period_checks)

    if type_id not in (mhr.get("contract_type_ids") or []):
        return _finish(result)

    # --- ხელშეკრულება: ხელმოწერები (ორიგინალის გარეშეც მოწმდება) ---
    scanned = _scanned_pages(repo, row)
    marked = handwriting_diffs(scanned)
    result.diffs.extend(marked)
    if mhr.get("signature_every_page", True):
        result.diffs.extend(signature_diffs(scanned, {d.page for d in marked}))

    # --- ორიგინალი MHR-დან (მხოლოდ kind generated — .docx) ---
    category = (mhr.get("categories") or {}).get(type_id, type_id)
    refs = client.list_documents(employee.employee_id, category)
    cands = original_candidates(refs, row["doc_number"] or _field(meta, "document_number"),
                                row["document_date"] or meta.get("document_date"))
    chosen = match_originals(client, cfg, cands, [p.get("text") or "" for p in scanned])
    ref, data, orig = chosen.primary, chosen.data, chosen.orig
    uploaded = _already_uploaded(refs, ref, row["id"])
    if uploaded:
        checks.append(uploaded)
    if ref is None:
        return _compare_with_template(result, cfg, client, employee, scanned, repo, row)
    result.original = ref
    missing_file = Check("original_contract", LABELS_KA["original_contract"], None, ref.doc_id, "missing_in_mhr", True,
                         note="ორიგინალის ფაილი MHR-ის სერვერზე არ არის (ჩანაწერი არსებობს, ფაილი — არა)")
    if "sha256" in (ref.raw or {}) and not ref.raw.get("sha256"):   # API: sha256 null = ფაილი სერვერზე ვერ მოიძებნა
        checks.append(missing_file)
        return _finish(result)
    if data is None:
        try:
            data = client.download(ref)
        except MhrError:
            checks.append(missing_file)
            return _finish(result)
        orig = original_pages(data, cfg)
    if orig is None:
        checks.append(Check("original_contract", LABELS_KA["original_contract"], None, ref.doc_id, "missing_in_mhr", True,
                            note="MHR-ის ორიგინალი (docx/PDF) ვერ წაიკითხა"))
        return _finish(result)
    checks.append(Check("original_contract", LABELS_KA["original_contract"], None, ref.doc_id, "match", False,
                        note=f"ორიგინალი: {ref.filename or ref.title or ref.doc_id}"))

    scanned_texts = [p.get("text") or "" for p in scanned]
    # პაკეტის დანართები (ინსტრუქცია, წესები…): თითოეული თავის გვერდებს შედარდება, ხელშეკრულება — დანარჩენს
    for ex_ref, ex_orig in chosen.extras:
        ex_pages = [i + 1 for i, o in enumerate(chosen.owner) if o == ex_ref.doc_id]
        ex_texts = [t if chosen.owner[i] == ex_ref.doc_id else "" for i, t in enumerate(scanned_texts)]
        ex_diffs = compare_texts(ex_orig.pages, ex_texts, cfg, original_paginated=ex_orig.paginated,
                                 strip_lines=ex_orig.strip_lines, labels=clause_labels(ex_orig.pages))
        found = sum(1 for d in ex_diffs if d.significant)
        for d in ex_diffs:   # დანართის სხვაობა ატვირთვას არ აჩერებს (შაბლონის დაცვა ხელშეკრულებას ეხება) — ჩანს სიაში
            if d.significant:
                d.significant, d.reason = False, f"დანართი — {d.reason}"
        result.diffs.extend(ex_diffs)
        checks.append(Check("original_annex", "დანართი", None, ex_ref.doc_id, "info", False,
                            note=f"{ex_ref.filename or ex_ref.title or ex_ref.doc_id}: გვ. {', '.join(map(str, ex_pages))}"
                                 + (f"; {found} სხვაობა — ატვირთვას არ აჩერებს, ნახე სიაში" if found else "; ემთხვევა")))
    scanned_texts = [t if chosen.owner[i] is None else "" for i, t in enumerate(scanned_texts)]
    for d in result.diffs:   # დანართის გვერდზე ბეჭედი/ნიშანი ზონის გარეთ („დამტკიცებულია" ბეჭედი) — შენიშვნა
        if d.kind == "handwriting" and d.significant and 0 < d.page <= len(chosen.owner) and chosen.owner[d.page - 1]:
            d.significant, d.reason = False, f"დანართი — {d.reason}"
    for p in orig.unreadable:   # წაუკითხავი ორიგინალის გვერდი ცალკე diff-ია; ტექსტური შედარება მასზე აზრს მოკლებულია
        if p - 1 < len(scanned_texts):
            scanned_texts[p - 1] = ""
        result.diffs.append(TextDiff(p, "changed", "", "", True, "ორიგინალის გვერდი წაუკითხავია — შედარება ვერ მოხერხდა"))
    known = known_tokens(employee)   # ბეჭდის ციფრები („2009", ს/კ) სუფთა ჩამატებად არ ჩაითვალოს

    def compare(texts: list[str]) -> list[TextDiff]:
        return compare_texts(orig.pages, texts, cfg, original_paginated=orig.paginated,
                             strip_lines=orig.strip_lines, labels=clause_labels(orig.pages), known=known)
    primary = compare(scanned_texts)
    result.diffs.extend(primary)
    _recheck_diffs(result, cfg, repo, row, scanned, scanned_texts, primary, compare)
    if not result.significant_diffs:
        # ხელშეკრულება ორიგინალს ემთხვევა ⇒ MHR-ის ამჟამინდელი ხელფასი/თანამდებობა/ვადა მოგვიანებით შეიცვალა,
        # დოკუმენტი კი არა (სპეც. §12: დღევანდელი მონაცემი ძველ დოკუმენტს არ ანაცვლებს) — შენიშვნა, არა ბლოკი
        for c in checks:
            if c.field in _CURRENT_STATE_FIELDS and c.status == "mismatch":
                c.blocking = False
                c.note = "ხელშეკრულება ორიგინალს ემთხვევა — MHR-ში ამჟამინდელი მონაცემი განსხვავდება" + \
                    (f"; {c.note}" if c.note else "")
    return _finish(result, compared_original=True)


_LABEL_RE = re.compile(r"^\s*(\d+(?:\.\d+)+|\d+(?=\.\s))\.?\s", re.MULTILINE)


def clause_labels(pages: list[str]) -> set[str]:
    """ორიგინალის/შაბლონის პუნქტის ნომრები ხაზის თავში („1.5.1.", „2.") — მხოლოდ წერტილიანი ფორმა, რომ თანხა
    („1800 ლარი" ხაზის თავში) ნომრად არ ჩაითვალოს."""
    return {m.group(1) for p in pages for m in _LABEL_RE.finditer(p)}


def _compare_with_template(result: ValidationResult, cfg: Config, client, employee: MhrEmployee,
                           scanned: list[dict[str, Any]], repo: Repository | None = None, row=None) -> ValidationResult:
    """ორიგინალი არ არის → თანამდებობის + ხელშეკრულების ტიპის შაბლონი MHR-დან. `${…}` ცვლადები (სახელი, პ/ნ, თარიღი,
    ხელფასი) შედარებისას გამოტოვებულია — ისინი MHR-ის მონაცემებს ზემოთ ველებად შედარდა; ხელფასის სხვაობა ბლოკავს."""
    missing = (f"ამ თარიღის (±{ORIGINAL_DATE_TOLERANCE_DAYS} დღე) ხელშეკრულების ორიგინალი MHR-ში ვერ მოიძებნა "
               "(ხელახლა აყვანილის შემთხვევაში ძველი შეიძლება MHR-ის წინა ჩანაწერზე იყოს)")
    template = client.get_contract_template(employee.employee_id)
    orig, why = None, "არ არის"
    if template and "sha256" in (template.raw or {}) and not template.raw.get("sha256"):
        why = "— შაბლონის ფაილი MHR-ის სერვერზე არ არის"   # API: sha256 null = ჩანაწერი არის, ფაილი — არა
    elif template:
        try:
            orig = original_pages(client.download_template(template), cfg)
            why = "ვერ წაიკითხა"
        except MhrError:
            why = "— შაბლონის ფაილი MHR-ის სერვერზე არ არის"
    if orig is None:
        result.checks.append(Check("original_contract", LABELS_KA["original_contract"], None, None, "missing_in_mhr", True,
                                   note=f"{missing}; თანამდებობის შაბლონიც {why}"))
        return _finish(result)
    raw = template.raw or {}
    which = ", ".join(x for x in (raw.get("position"), raw.get("work_status")) if x) or template.title or template.doc_id
    result.checks.append(Check("contract_template", "შაბლონი", None, template.doc_id, "info", False,
                               note=f"MHR-ში ამ თანამშრომლის ხელშეკრულება არ არის — სკანი შედარდა თანამდებობის შაბლონთან ({which})"))
    tpl = [mark_placeholders(p) for p in orig.pages]

    def compare(texts: list[str]) -> list[TextDiff]:
        return compare_texts(tpl, texts, cfg, original_paginated=orig.paginated, strip_lines=orig.strip_lines,
                             labels=clause_labels(orig.pages), known=known_tokens(employee))
    scanned_texts = [p.get("text") or "" for p in scanned]
    primary = compare(scanned_texts)
    result.diffs.extend(primary)
    if repo is not None and row is not None:
        _recheck_diffs(result, cfg, repo, row, scanned, scanned_texts, primary, compare)
    return _finish(result, compared_template=True)


# --- საეჭვო გვერდის ხელახალი წაკითხვა ონლაინ OCR-ით ------------------------------------------------------------
RECHECK_KINDS = ("changed", "added", "removed")
RECHECK_NOTE = "გვერდი Claude-მა ხელახლა წაიკითხა — სხვაობა Tesseract-ის წაკითხვის შეცდომა იყო"
RECHECK_STILL = "ხელახალი წაკითხვითაც"


_RECHECK_MARKER = re.compile(r"\[[^\]\n]{0,200}\]")   # „[ბეჭედი: …]", „[ხელნაწერი: …]" — მოდელის ნიშნები, არა ტექსტი


def _clean_recheck_text(text: str) -> str:
    return _RECHECK_MARKER.sub(" ", text or "")


def _online_page_text(cfg: Config, provider, output_path: str, page_index: int, rotation: int) -> str:
    """დოკუმენტის PDF-ის გვერდი → ონლაინ OCR მხოლოდ ბეჭდური ტექსტისთვის (იგივე რენდერი/გაუმჯობესება, რაც ანალიზისას)."""
    dpi = int(cfg.section("processing").get("dpi", 300))
    with PdfDocument(output_path) as doc:
        img = doc.render(page_index, dpi=dpi)
    if rotation:
        img = apply_rotation(img, int(rotation))
    recognize = getattr(provider, "recognize_printed", None) or provider.recognize
    return _clean_recheck_text(recognize(enhance(img), cfg.data["ocr"].get("tesseract_lang") or "kat+eng").text)


def _recheck_diffs(result: ValidationResult, cfg: Config, repo: Repository, row, scanned: list[dict[str, Any]],
                   scanned_texts: list[str], primary: list[TextDiff], compare) -> None:
    """წესებმა ტექსტური სხვაობა იპოვა Tesseract-ით წაკითხულ გვერდზე (დაკარგული ციფრი „2 კვირით" → „კვირით", „მე-5" →
    „მე-3") → ეს გვერდი ონლაინ OCR-ით (Claude) ხელახლა იკითხება და შედარება მეორდება: სხვაობა გაქრა — წაკითხვის შეცდომა
    იყო (სიაში ხმაურად რჩება); დარჩა — ნამდვილი სხვაობაა. ახალი ტექსტი page_texts-ში ინახება, რომ AI ანალიზი და
    ხელახალი შედარება უკეთეს ტექსტს ხედავდეს. ≤ `mhr.recheck.max_pages` გვერდი ერთ დოკუმენტზე (ხარჯის კონტროლი)."""
    rc = cfg.section("mhr").get("recheck") or {}
    if not rc.get("enabled", True):
        return
    bad = {d.page for d in primary if d.significant and d.kind in RECHECK_KINDS and 0 < d.page <= len(scanned)}
    pages = sorted(p for p in bad if (scanned[p - 1].get("text_source") or "").startswith("ocr:tesseract")
                   and scanned_texts[p - 1])
    pages = pages[:int(rc.get("max_pages", 4))]
    if not pages:
        return
    provider = getattr(build_ocr_chain(cfg), "online", None)
    if provider is None or not provider.available():
        return
    rechecked: list[int] = []
    for p in pages:
        pt = scanned[p - 1]
        try:
            text = _online_page_text(cfg, provider, row["output_path"], int(pt["page_index"]) - int(row["page_start"]),
                                     int(pt.get("rotation_applied") or 0))
        except (OcrError, OSError, ValueError, KeyError, TypeError) as e:
            log.warning("recheck page %s of %s failed: %s", p, row["id"], type(e).__name__)
            continue
        if not (text or "").strip():
            continue
        new_pt = dict(pt, text=text, text_source=f"ocr:{getattr(provider, 'name', 'online')}:recheck",
                      ocr_quality=round(float(textquality.score(text)), 4), provider=getattr(provider, "name", "online"))
        scanned[p - 1] = new_pt
        scanned_texts[p - 1] = text
        repo.save_page_text(row["job_id"], int(pt["page_index"]), new_pt)
        rechecked.append(p)
    if not rechecked:
        return
    log.info("recheck %s: pages %s re-read online", row["id"], rechecked)
    fresh = compare(scanned_texts)
    old = {id(d) for d in primary}
    result.diffs = [d for d in result.diffs if id(d) not in old]
    still = {d.page for d in fresh if d.significant and d.kind in RECHECK_KINDS}
    for d in fresh:
        if d.significant and d.page in rechecked and d.kind in RECHECK_KINDS:
            d.reason = f"{d.reason} — {RECHECK_STILL}" if d.reason else RECHECK_STILL
    result.diffs.extend(fresh)
    for p in rechecked:
        if p not in still:
            result.diffs.append(TextDiff(p, "changed", "", "", False, RECHECK_NOTE))


def known_tokens(employee: MhrEmployee) -> list[str]:
    """MHR-ის თანამშრომლის ჩანაწერის ყველა ტექსტური მნიშვნელობა სიტყვებად (სახელი, გვარი, პ/ნ, მისამართი, ტელეფონი,
    ელ. ფოსტა, ანგარიში…): შაბლონის ცვლადების ადგილას სკანში სწორედ ესენი წერია."""
    vals: list[str] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for y in x.values():
                walk(y)
        elif isinstance(x, list):
            for y in x:
                walk(y)
        elif isinstance(x, (str, int, float)) and not isinstance(x, bool):
            vals.append(str(x))

    walk(employee.raw or {})
    vals += [str(x) for x in (employee.first_name, employee.last_name, employee.personal_number) if x]
    return [w.strip(_PUNCT) for v in vals for w in normalize(v).split() if w.strip(_PUNCT)]


# --- MHR-ის დასაქმების პერიოდი (employments) -------------------------------------------------------------------
@dataclass
class _Period:
    position: Any = None
    salary: Any = None
    contract_start: Any = None
    contract_end: Any = None
    company: dict[str, Any] = dc_field(default_factory=dict)   # {id, name, tax_id}
    note: str = ""


def _amount(v: Any) -> Any:
    return v.get("amount") if isinstance(v, dict) else v


def _period(employee: MhrEmployee, emap: dict[str, str], ref_date: _dt.date | None, doc_tax_id: str | None) -> _Period:
    """employments-იდან ref_date-ის მომცველი პერიოდი (start ≤ date ≤ end ან end null; რამდენიმე — დოკუმენტის
    კომპანიისა, შემდეგ უგვიანესი დაწყებით); არცერთი — უახლოესი დაწყებით. employments/თარიღი არ არის → მიმდინარე ველები."""
    raw = employee.raw or {}
    current = _Period(employee.position, employee.salary, employee.contract_start, employee.contract_end,
                      {"id": employee.company_id, "name": _get_path(raw, emap.get("company_name") or ""),
                       "tax_id": _get_path(raw, emap.get("company_tax_id") or "")})
    emps = _get_path(raw, emap.get("employments") or "")
    emps = [e for e in emps if isinstance(e, dict)] if isinstance(emps, list) else []
    if not emps:
        return current
    if ref_date is None:
        current.note = "შედარდა MHR-ის მიმდინარე მონაცემებთან (დოკუმენტის თარიღი უცნობია)"
        return current

    def same_company(e: dict[str, Any]) -> bool:
        tax = (e.get("company") or {}).get("tax_id") if isinstance(e.get("company"), dict) else None
        return bool(doc_tax_id and tax) and digits_only(str(tax)) == digits_only(str(doc_tax_id))

    spans = [(e, _date(e.get("start")), _date(e.get("end"))) for e in emps]
    covering = [(e, s, en) for e, s, en in spans if (s is None or s <= ref_date) and (en is None or ref_date <= en)]
    if covering:
        e, s, en = max(covering, key=lambda t: (same_company(t[0]), t[1] or _dt.date.min))
    else:
        started = [t for t in spans if t[1] is not None]
        if not started:
            current.note = "შედარდა MHR-ის მიმდინარე მონაცემებთან"
            return current
        e, s, en = min(started, key=lambda t: (abs((t[1] - ref_date).days), -t[1].toordinal()))
    company = e.get("company") if isinstance(e.get("company"), dict) else {}
    return _Period(e.get("position"), _amount(e.get("salary")), e.get("start"), e.get("end"), company,
                   note=f"MHR-ის დასაქმების პერიოდი: {e.get('start') or '?'} – {e.get('end') or 'უვადო'}"
                        + ("" if covering else " (დოკუმენტის თარიღთან უახლოესი)"))


def _company_check(cfg: Config, meta: dict[str, Any], doc_company: Any, co, mco: dict[str, Any]) -> Check:
    """config-ის კომპანია ↔ MHR-ის კომპანია: tax_id (ორივე მხარეს თუ არის) > სახელი/aliases > (MHR-მა მხოლოდ id
    დააბრუნა — ძველი ფორმატი) id/სახელის ტოლობა."""
    label = LABELS_KA["company"]
    name, tax, mid = mco.get("name"), mco.get("tax_id"), mco.get("id")
    shown = f"{name} (ს/კ {tax})" if name and tax else (name or (f"ს/კ {tax}" if tax else mid))
    source = meta.get("company_source")
    if source == "default" or (not source and doc_company == cfg.default_company.id):
        return Check("company", label, doc_company, shown, "skipped", False,
                     note="კომპანია დოკუმენტიდან არ წაკითხულა (პროფილის ნაგულისხმევი)")
    names = {company_core(n) for n in (co.name, *co.aliases)} if co else set()

    def match(d: str, _m: str) -> bool:
        if co and co.tax_id and tax:
            return digits_only(str(co.tax_id)) == digits_only(str(tax))
        if name:
            return company_core(str(name)) in names
        if tax:
            return False
        return one_line(str(d)) == one_line(str(mid)) or company_core(str(mid)) in names

    return _check("company", doc_company, shown, match)


def _scanned_pages(repo: Repository, row) -> list[dict[str, Any]]:
    """დოკუმენტის გვერდების PageText-ები (job_id, page_start..page_end; ინდექსები 0-დან). დადასტურებულად ცარიელი
    გვერდები (is_blank) შედარებაში არ მონაწილეობს — ცვლილებას ვერ შეიცავს."""
    texts = repo.load_page_texts(row["job_id"])
    out = []
    for i in range(int(row["page_start"]), int(row["page_end"]) + 1):
        pt = texts.get(i) or {}
        if pt.get("is_blank"):
            continue
        out.append(pt)
    return out
