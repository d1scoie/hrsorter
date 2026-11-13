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
import hashlib
import io
import json
import logging
import re
import uuid
import zipfile
from dataclasses import dataclass, field as dc_field
from difflib import SequenceMatcher
from itertools import zip_longest
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from . import hrdates, readermarks, textquality
from .config import Config
from .db import Repository, now_iso
from .employee_api import _get_path
from .features import company_core, surname_lemma
from .mhr_models import Check, MhrDocRef, MhrEmployee, MhrError, MhrUnavailable, TextDiff, ValidationResult, clean_file_title
from .ocr.base import OcrError
from .output import MATCH_RESOLVED_REASONS, PartSpec, adopt_match, split_document
from .empform import parse_employee_form
from .idcard import PASSPORT, parse_id_card, same_name
from .stages import STAGE_KA, file_stage, strip_stage, text_stage
from .ocr.chain import build_ocr_chain
from .pdfio import PdfDocument, validate_pdf
from .preprocess import apply_rotation, enhance
from .textcompare import _PUNCT, compare_texts, locate, mark_placeholders
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


def mhr_snapshot(cfg: Config, employee: MhrEmployee | None, refs: list[MhrDocRef]) -> str:
    """რა ნახა შედარებამ MHR-ში, მოკლე ანაბეჭდად: თანამშრომლის ძირითადი მონაცემები და დაგენერირებული ფაილების სია.
    ამით ხვდება `mhr_queue`, რომ MHR-ში რამე შეიცვალა (ფაილი დაგენერირდა, თანამშრომელი დაემატა, თარიღი ჩაიწერა) და
    „MHR-ში აკლია“-ში მდგარი ფაილის შედარება თავიდან ღირს. ატვირთული სკანები აქ არ შედის — ისინი ორიგინალი არ არის."""
    person = None
    if employee is not None:
        jobs = _get_path(employee.raw or {}, (cfg.data["mhr"].get("employee_map") or {}).get("employments") or "")
        jobs = [[j.get("start"), j.get("end")] for j in jobs if isinstance(j, dict)] if isinstance(jobs, list) else []
        person = [employee.employee_id, employee.personal_number, employee.first_name, employee.last_name,
                  employee.contract_start, employee.contract_end, jobs]
    files = sorted([r.doc_id, r.filename or r.title or "", r.created_at or "", str((r.raw or {}).get("sha256") or "")]
                   for r in refs if _is_generated(r))
    return hashlib.sha256(json.dumps([person, files], ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:16]


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
        # უპირატესობა იმავე თარიღის (±ORIGINAL_DATE_TOLERANCE_DAYS) ორიგინალს; ასეთი რომ არ იყოს, მაინც უახლესი დაგენერირებული
        # აიღება (ხელშეკრულება MHR-შია და მხოლოდ მას ვადარებთ) — თარიღის სხვაობა შენიშვნად ჩნდება (original_date_note)
        dated = [r for r in pool if _ref_date(r) is not None
                 and abs((_ref_date(r) - target).days) <= ORIGINAL_DATE_TOLERANCE_DAYS]
        if dated:
            return sorted(dated, key=lambda r: (-abs((_ref_date(r) - target).days), newest(r)), reverse=True)
    return sorted(pool, key=newest, reverse=True)


NO_ORIGINAL_NOTE = ("ხელშეკრულების ორიგინალი MHR-ში ვერ მოიძებნა: ამ თანამშრომელს დაგენერირებული ხელშეკრულება არ აქვს — ჯერ MHR-ში "
                    "დააგენერირე, მერე დაბეჭდე, მოაწერინე და დაასკანერე (ატვირთული სკანი ორიგინალად არ ითვლება; ხელახლა აყვანილის "
                    "შემთხვევაში ხელშეკრულება შეიძლება MHR-ის წინა ჩანაწერზე იყოს)")


def original_date_note(ref: MhrDocRef, doc_date: str | None) -> Check | None:
    """ორიგინალი სკანის თარიღის ±ORIGINAL_DATE_TOLERANCE_DAYS-ის გარეთ შეიქმნა → შენიშვნა (არ ბლოკავს): შეიძლება HR-მა
    ძველი ვერსია დაბეჭდა, ან ხელშეკრულება ხელახლა დააგენერირა ხელმოწერის შემდეგ."""
    target, created = _date(doc_date), _ref_date(ref)
    if target is None or created is None:
        return None
    delta = (created - target).days
    if abs(delta) <= ORIGINAL_DATE_TOLERANCE_DAYS:
        return None
    return Check("original_date", "ორიგინალის თარიღი", doc_date, created.isoformat(), "info", False,
                 note=f"ორიგინალი MHR-ში {abs(delta)} დღით {'გვიან' if delta > 0 else 'ადრე'} შეიქმნა ({created.isoformat()}), ვიდრე "
                      f"დოკუმენტის თარიღია ({target.isoformat()}) — დარწმუნდი, რომ სწორი ვერსია დაიბეჭდა")


def latest_versions(refs: list[MhrDocRef]) -> list[MhrDocRef]:
    """ერთი და იგივე ფაილი (იგივე სახელი) რამდენჯერმე თუ დაგენერირდა — მხოლოდ ბოლო ცვლილება რჩება."""
    newest: dict[str, MhrDocRef] = {}
    for r in refs:
        key = (clean_file_title(r.filename) or r.title or r.doc_id).casefold()
        cur = newest.get(key)
        if cur is None or (_ref_date(r) or _dt.date.min, r.created_at or "") > (_ref_date(cur) or _dt.date.min, cur.created_at or ""):
            newest[key] = r
    keep = {id(r) for r in newest.values()}
    return [r for r in refs if id(r) in keep]


def same_stage(refs: list[MhrDocRef], stage: str | None) -> list[MhrDocRef]:
    """ქაღალდის ეტაპის ფაილები (და ეტაპის გარეშე დასახელებულები). ეტაპი უცნობია — ყველა."""
    if stage is None:
        return list(refs)
    return [r for r in refs if file_stage(r.filename or r.title) in (None, stage)]


def scan_stage(repo: Repository, row, scanned: list[dict[str, Any]]) -> str | None:
    """ქაღალდის ეტაპი: დოკუმენტის ტექსტიდან; არ წერია და სკანის ნაწილია — მთელი სკანიდან (პაკეტში ხელშეკრულებაა)."""
    stage = text_stage("\n".join(p.get("text") or "" for p in scanned))
    parent = row["part_of"] if "part_of" in row.keys() else None
    if stage is None and parent:
        whole = repo.get_document(parent)
        if whole is not None:
            stage = text_stage("\n".join(p.get("text") or "" for p in document_pages(repo, whole)))
    return stage


def type_from_filename(cfg: Config, filename: str | None) -> str | None:
    """MHR-ის ფაილის ტიპი სახელიდან, ტიპების სათაურის წესებით („… - თანამდებობრივი ინსტრუქცია“ → job_description).
    None — სახელი ტიპს არ ამბობს (მაგ. „ლიფტით სარგებლობის წესები“)."""
    from .classifier import Classifier
    name = strip_stage(clean_file_title(filename))
    if not name:
        return None
    clf = Classifier(cfg)
    score: dict[str, float] = {}
    for seg in dict.fromkeys([name, *(s for s in re.split(r"\s*-\s*", name) if s)]):
        seg_score: dict[str, float] = {}
        for h in clf.title_hits(seg):
            seg_score[h.type_id] = seg_score.get(h.type_id, 0.0) + h.weight
        for k, v in seg_score.items():
            score[k] = max(score.get(k, v), v)
    best = max(score, key=score.get, default=None)
    return best if best is not None and score[best] > 0 else None


def _fallback_pool(cfg: Config, refs: list[MhrDocRef], type_id: str, category: str) -> list[MhrDocRef]:
    """გვერდებით ვერ დადგინდა, რომელ ფაილს ეკუთვნის: ბარათის ფაილების სახელები თუ ტიპს ამბობს — მხოლოდ ამ ტიპის ფაილები
    (თორემ ხელშეკრულებად „ლიფტის წესები“ შეიძლება აიღოს); სახელები არაფერს ამბობს — MHR-ის კატეგორიით, როგორც ადრე."""
    named = {id(r): type_from_filename(cfg, r.filename or r.title) for r in refs}
    if any(named.values()):
        return [r for r in refs if named[id(r)] == type_id]
    return [r for r in refs if r.category == category]


def missing_original_note(cfg: Config, type_id: str, stage: str | None, generated: list[MhrDocRef]) -> str:
    """რატომ ვერ მოიძებნა ორიგინალი: ეტაპი ცნობილია და ბარათზე მხოლოდ სხვა ეტაპის ვერსიაა — ეს პირდაპირ წერია."""
    if stage is None and type_id == "contract":
        return NO_ORIGINAL_NOTE
    dt = cfg.doc_type(type_id)
    what = dt.name_ka if dt else "ფაილი"
    others = sorted({s for r in generated if (s := file_stage(r.filename or r.title)) not in (None, stage)
                     and type_from_filename(cfg, r.filename or r.title) == type_id}, key=list(STAGE_KA).index)
    note = f"MHR-ში ამ თანამშრომლის {STAGE_KA[stage] + ' ' if stage else ''}{what} ვერ მოიძებნა"
    if others:
        note += f" — ბარათზე მხოლოდ {' და '.join(STAGE_KA[s] for s in others)} ვერსიაა"
    return note + (". ჯერ MHR-ში დააგენერირე, მერე დაბეჭდე, მოაწერინე და დაასკანერე; თუ ქაღალდი უკვე ხელმოწერილია — "
                   "დააგენერირე, პროგრამა რამდენიმე წუთში თავად შეადარებს თავიდან")


# --- პირადობა ------------------------------------------------------------------------------------------------
IDENTITY_TITLE = "პირადობის მოწმობა — MHR-ს ემთხვევა"   # ატვირთვის სათაური: სხვა კომპიუტერიც ხედავს, რომ დადასტურებულია
PASSPORT_TITLE = f"{IDENTITY_TITLE} (პასპორტი)"          # იგივე ნიშანი შიგნით აქვს — დადასტურებად ითვლება
PASSPORT_NOTE = "ეს პასპორტია — პირადობის მოწმობის ნაცვლად ითვლება და ისევე მოწმდება"


def identity_type_ids(cfg: Config) -> list[str]:
    return [t.id for t in cfg.types if t.identity]


def verified_identity(repo: Repository, cfg: Config, employee_id: str | None, refs: list[MhrDocRef]) -> dict[str, Any] | None:
    """ამ თანამშრომლის პირადობა უკვე დადასტურდა: აქ (ვალიდაციაში „identity: match“) ან MHR-ში ჩვენ მიერ ატვირთული
    დადასტურებული პირადობა (სხვა კომპიუტერიდან). None — არ დადასტურებულა."""
    ids = identity_type_ids(cfg)
    if not employee_id or not ids:
        return None
    q = ",".join("?" * len(ids))
    for r in repo.q(f"SELECT m.document_id, m.status, m.validation FROM mhr_items m JOIN documents d ON d.id = m.document_id "
                    f"WHERE m.employee_id = ? AND d.active = 1 AND d.type_id IN ({q}) ORDER BY m.updated_at DESC",
                    (str(employee_id), *ids)):
        v = json.loads(r["validation"] or "{}")
        if any(c.get("field") == "identity" and c.get("status") == "match" for c in v.get("checks", [])):
            return {"document_id": r["document_id"], "status": r["status"], "where": "local"}
    cats = {(cfg.data["mhr"].get("categories") or {}).get(t, t) for t in ids}
    for ref in refs:
        if ref.category in cats and (ref.raw or {}).get("kind") == "uploaded" and IDENTITY_TITLE in (ref.title or ""):
            return {"document_id": None, "status": "uploaded", "where": "mhr", "remote_id": ref.doc_id}
    return None


def _validate_identity(result: ValidationResult, cfg: Config, row, type_id: str, scanned: list[dict[str, Any]],
                       employee: MhrEmployee, refs: list[MhrDocRef]) -> ValidationResult:
    """პირადობის მოწმობა: გვარი, სახელი, პირადი ნომერი — MHR-ს „ერთი ერთში“. ქართული სახელი ლათინურით/MRZ-ით უნდა
    დადასტურდეს (წაკითხვის შეცდომა ავტომატურად არ აიტვირთოს). ყველაფერი ემთხვევა → `identity: match` (დადასტურებულია)."""
    card = parse_id_card("\n".join(p.get("text") or "" for p in scanned))
    checks = result.checks
    passport = card.kind == PASSPORT   # პასპორტი პირადობის მოწმობის ნაცვლად: იგივე შემოწმება, ოღონდ თავისი სახელით
    on, out_of = ("პასპორტზე", "პასპორტიდან") if passport else ("პირადობაზე", "პირადობიდან")

    def exact(field: str, doc_val: str | None, mhr_val: str | None, same) -> Check:
        label = LABELS_KA[field]
        if not doc_val:
            return Check(field, label, None, mhr_val, "missing_in_document", True, note=f"{label} {out_of} ვერ წაიკითხა")
        if not mhr_val:
            return Check(field, label, doc_val, None, "missing_in_mhr", True, note=f"{label} MHR-ში ცარიელია")
        if same(doc_val, mhr_val):
            return Check(field, label, doc_val, mhr_val, "match", False)
        return Check(field, label, doc_val, mhr_val, "mismatch", True,
                     note=f"{on} „{doc_val}“, MHR-ში „{mhr_val}“ — თუ MHR-ში შეცდომაა, შეასწორე და „ხელახლა შედარება“")

    names = [exact("last_name", card.last_name, employee.last_name, same_name),
             exact("first_name", card.first_name, employee.first_name, same_name),
             exact("personal_number", card.personal_number, employee.personal_number,
                   lambda d, m: digits_only(str(d)) == digits_only(str(m)))]
    checks.extend(names)
    for p in card.problems:
        checks.append(Check("id_reading", "წაკითხვა", None, None, "mismatch", True, note=p))
    # დაბადების თარიღი და ვადა სხვა შემოწმებებს სჭირდება (ასაკი, „ვადა გასდის“) — ტექსტის ხელახლა კითხვის გარეშე
    result.facts = {k: v for k, v in (("birth_date", card.birth_date), ("id_expiry", card.expiry_date),
                                      ("id_kind", card.kind)) if v}
    expiry = hrdates.id_expiry(card.expiry_date, None, hrdates.limits(cfg)[1], passport=passport)
    if expiry and expiry[0] == hrdates.EXPIRED:
        checks.append(Check("id_expiry", "მოქმედების ვადა", card.expiry_date, None, "info", True,
                            note=f"{expiry[1]}; თუ მაინც გინდა, „ატვირთვა მაინც“"))
    elif expiry:   # მალე გასდის: ატვირთვას არ აჩერებს, მხოლოდ აფრთხილებს
        checks.append(Check("id_expiry", "მოქმედების ვადა", card.expiry_date, None, "info", False, note=expiry[1]))
    category = (cfg.data["mhr"].get("categories") or {}).get(type_id, type_id)
    other = next((r for r in refs if r.category == category and (r.raw or {}).get("kind") == "uploaded"
                  and (r.raw or {}).get("external_id") != row["id"]), None)
    if other is not None:
        when = (other.created_at or "")[:10]
        checks.append(Check("already_uploaded", "უკვე ატვირთულია", None, other.doc_id, "info", True,
                            note=f"ამ თანამშრომლის პირადობა MHR-ში უკვე ატვირთულია{f' ({when})' if when else ''} — მეორედ არ "
                                 "ავტვირთავთ; თუ ეს ახალი პირადობაა, „ატვირთვა მაინც“"))
    if all(c.status == "match" for c in names) and not card.problems:
        checks.append(Check("identity", "პირადობა", None, None, "match", False,
                            note="გვარი, სახელი და პირადი ნომერი ზუსტად ემთხვევა MHR-ს — პირადობა დადასტურებულია"))
    res = _finish(result)
    if res.status == "passed":
        res.summary.insert(0, "პირადობა MHR-ს ზუსტად ემთხვევა — დადასტურების გარეშე აიტვირთება")
    elif any(c.field == "identity" for c in checks):
        res.summary.insert(0, "პირადობა MHR-ს ემთხვევა (სახელი დადასტურებულია), მაგრამ ატვირთვა გაჩერდა — მიზეზი ქვემოთაა")
    if passport:
        res.summary.insert(0, PASSPORT_NOTE)
    return res


def identity_confirmed(validation: dict[str, Any] | None) -> bool:
    return any(c.get("field") == "identity" and c.get("status") == "match" for c in (validation or {}).get("checks") or [])


def settle_confirmed_identities(repo: Repository, cfg: Config) -> int:
    """პირადობა, რომელიც „გადასამოწმებელში“ მხოლოდ ტიპის/საზღვრის ეჭვით დგას, მაგრამ MHR-ს უკვე ზუსტად დაემთხვა (სახელი,
    გვარი, პ/ნ): ეჭვი აღარ არის, სიიდან გადის (თუ ატვირთვა სხვა მიზეზით გაჩერდა, ის MHR-ის გვერდზე ჩანს). აბრუნებს რაოდენობას."""
    ids = identity_type_ids(cfg)
    if not ids:
        return 0
    q = ",".join("?" * len(ids))
    rows = repo.q(f"SELECT d.*, m.validation AS mhr_validation FROM documents d JOIN mhr_items m ON m.document_id = d.id "
                  f"WHERE d.active = 1 AND d.status = 'needs_review' AND d.type_id IN ({q}) "
                  f"AND m.status IN ('ready', 'approved', 'uploading', 'uploaded', 'failed')", tuple(ids))
    done = 0
    for row in rows:
        reasons = set(json.loads(row["review_reasons"] or "[]"))
        if not reasons <= MATCH_RESOLVED_REASONS or not identity_confirmed(json.loads(row["mhr_validation"] or "{}")):
            continue   # სხვა მიზეზი (დუბლიკატი, ორი თანამშრომელი) ოპერატორს რჩება
        try:
            adopt_match(repo, row, json.loads(row["metadata"]) if row["metadata"] else {}, row["type_id"])
        except OSError as e:   # sidecar ასლია (ქსელის დისკზე ჩაწერა ვერ მოხერხდა) — ბაზა უკვე სწორია, გავლა არ ჩერდება
            log.warning("sidecar of %s not updated: %s", row["id"], type(e).__name__)
        repo.log_event("mhr", f"{row['id']}: review settled (identity confirmed by MHR)")
        done += 1
    return done


def _name_check(field: str, doc_val: Any, mhr_val: Any, threshold: float, verified: dict[str, Any] | None) -> Check:
    """სახელი/გვარი. პირადობით დადასტურებულ თანამშრომელზე MHR-ის სახელი სწორია: წაკითხვის მცირე შეცდომა აღარ აჩერებს,
    სხვა სახელი კი ისევ აჩერებს და პირდაპირ წერია, რომელია სწორი."""
    c = _check(field, doc_val, mhr_val, lambda d, m: _name_match(d, m, threshold))
    if not verified:
        return c
    if c.status == "match":
        c.note = "პირადობით დადასტურებული"
    elif c.status == "mismatch":
        if _name_close(str(doc_val), str(mhr_val)):
            c.status, c.blocking = "match", False
            c.note = f"ქაღალდზე წაიკითხა „{doc_val}“ — წაკითხვის შეცდომაა: პირადობით დადასტურებულია „{mhr_val}“"
        else:
            c.note = f"პირადობით დადასტურებულია „{mhr_val}“ — ქაღალდზე სხვა {c.label_ka.lower()} წერია"
    return c


# --- ანკეტა („ინფორმაცია თანამშრომელზე“) -------------------------------------------------------------------------
def _employment_starts(cfg: Config, employee: MhrEmployee) -> list[str]:
    """MHR-ში ჩაწერილი მუშაობის დაწყების თარიღები (ყველა დასაქმების პერიოდი + მიმდინარე), ISO."""
    emap = cfg.data["mhr"].get("employee_map") or {}
    emps = _get_path(employee.raw or {}, emap.get("employments") or "")
    starts = {_iso(e.get("start")) for e in emps if isinstance(e, dict)} if isinstance(emps, list) else set()
    starts.add(_iso(employee.contract_start))
    return sorted(s for s in starts if s)


def _verified_birth_date(repo: Repository, verified: dict[str, Any] | None) -> str | None:
    """დადასტურებული პირადობის დაბადების თარიღი (პირადობა ამ კომპიუტერზეა დამუშავებული); სხვაგვარად None."""
    if not verified or not verified.get("document_id"):
        return None
    doc = repo.get_document(verified["document_id"])
    if doc is None:
        return None
    return parse_id_card("\n".join(p.get("text") or "" for p in document_pages(repo, doc))).birth_date


def _validate_employee_form(result: ValidationResult, cfg: Config, repo: Repository, row, type_id: str,
                            scanned: list[dict[str, Any]], employee: MhrEmployee, refs: list[MhrDocRef],
                            verified: dict[str, Any] | None) -> ValidationResult:
    """ხელით შევსებული ანკეტა. MHR-ს ედარება ის, რაც ციფრებია: პირადი ნომერი და მუშაობის დაწყების თარიღი; დაბადების თარიღი —
    დადასტურებულ პირადობას. ხელნაწერი სახელი მხოლოდ შენიშვნაა (ხშირად ვერ იკითხება). ყველაფერი ემთხვევა და ხელმოწერილია →
    `employee_form: match` (ავტომატური ატვირთვა); პირადობა ჯერ არ დადასტურებულა → გადის, მაგრამ ოპერატორი ადასტურებს."""
    form = parse_employee_form("\n".join(p.get("text") or "" for p in scanned))
    checks = result.checks
    pn = form.personal_number or digits_only(str(row["personal_number"] or ""))
    pn_check = _check("personal_number", pn, employee.personal_number, lambda d, m: digits_only(str(d)) == digits_only(str(m)))
    checks.append(pn_check)

    starts = _employment_starts(cfg, employee)
    label = "მუშაობის დაწყების თარიღი"
    if not form.start_date:
        start = Check("start_date", label, None, ", ".join(starts) or None, "missing_in_document", True,
                      note="მუშაობის დაწყების თარიღი ანკეტიდან ვერ წაიკითხა — ნახე ქაღალდი; თუ სწორია, „ატვირთვა მაინც“")
    elif not starts:
        start = Check("start_date", label, form.start_date, None, "missing_in_mhr", True,
                      note="MHR-ში ამ თანამშრომლის მუშაობის დაწყების თარიღი არ წერია")
    elif form.start_date in starts:
        start = Check("start_date", label, form.start_date, form.start_date, "match", False)
    else:
        start = Check("start_date", label, form.start_date, ", ".join(starts), "mismatch", True,
                      note="თუ MHR-ში შეცდომაა, შეასწორე და „ხელახლა შედარება“; თუ ხელნაწერი არასწორად წაიკითხა — „ატვირთვა მაინც“")
    checks.append(start)

    id_birth = _verified_birth_date(repo, verified)
    birth: Check | None = None
    if id_birth and form.birth_date:
        same = id_birth == form.birth_date
        birth = Check("birth_date", "დაბადების თარიღი", form.birth_date, id_birth, "match" if same else "mismatch", not same,
                      note="" if same else "ანკეტაზე და დადასტურებულ პირადობაზე დაბადების თარიღი სხვადასხვაა — ნახე ქაღალდი")
    elif id_birth:
        birth = Check("birth_date", "დაბადების თარიღი", None, id_birth, "missing_in_document", True,
                      note="დაბადების თარიღი ანკეტიდან ვერ წაიკითხა — ნახე ქაღალდი; თუ სწორია, „ატვირთვა მაინც“")
    if birth:
        checks.append(birth)
    if birth is not None and birth.status == "match" and start.status == "match":   # ორივე თარიღი დადასტურებულია
        minor = hrdates.minor_note(id_birth, form.start_date, hrdates.limits(cfg)[0])
        if minor:
            checks.append(Check("age", "ასაკი", id_birth, form.start_date, "info", False, note=minor))

    threshold = float(cfg.data["mhr"].get("name_similarity", 0.85))
    mhr_name = " ".join(x for x in (employee.first_name, employee.last_name) if x)
    if form.full_name and mhr_name:   # ხელნაწერი: სხვაობა არ აჩერებს — ციფრები უკვე შედარდა
        words = form.full_name.split()
        ok = any(employee_name_matches(a, b, employee, threshold) for a, b in ((words[0], words[-1]), (words[-1], words[0])))
        checks.append(Check("full_name", "სახელი, გვარი", form.full_name, mhr_name, "match" if ok else "info", False,
                            note="" if ok else f"ხელნაწერი სახელი ასე წაიკითხა: „{form.full_name}“, MHR-ში „{mhr_name}“ — შეამოწმე ქაღალდზე"))

    # მკითხველმა პირდაპირ თქვა, მოაწერა თუ არა თანამშრომელმა → ეს წყვეტს; არ უთქვამს → გვერდის ზოგადი შეფასება
    said = [x for x in ((p.get("signature_parties") or {}).get("employee") for p in scanned) if x is not None]
    signed = form.signed or (any(said) if said else any(p.get("signature_hint") == "present" for p in scanned))
    if not signed:
        checks.append(Check("signature", "ხელმოწერა", None, None, "missing_in_document", True,
                            note="ანკეტაზე თანამშრომლის ხელმოწერა არ ჩანს — მოაწერინე და ხელახლა დაასკანერე"))

    category = (cfg.data["mhr"].get("categories") or {}).get(type_id, type_id)
    other = next((r for r in refs if r.category == category and (r.raw or {}).get("kind") == "uploaded"
                  and (r.raw or {}).get("external_id") != row["id"]), None)
    if other is not None:
        when = (other.created_at or "")[:10]
        checks.append(Check("already_uploaded", "უკვე ატვირთულია", None, other.doc_id, "info", True,
                            note=f"ამ თანამშრომლის ანკეტა MHR-ში უკვე ატვირთულია{f' ({when})' if when else ''} — მეორედ არ "
                                 "ავტვირთავთ; თუ ეს ახალი ანკეტაა, „ატვირთვა მაინც“"))

    full = pn_check.status == "match" and start.status == "match" and signed and birth is not None and birth.status == "match"
    if full:
        checks.append(Check("employee_form", "ანკეტა", None, None, "match", False,
                            note="პირადი ნომერი და მუშაობის დაწყების თარიღი MHR-ს ემთხვევა, დაბადების თარიღი — დადასტურებულ პირადობას"))
    elif birth is None:
        checks.append(Check("identity", "პირადობა", None, None, "info", False,
                            note="ამ თანამშრომლის პირადობა ჯერ არ დადასტურებულა — დაბადების თარიღი ვერ შედარდა; ანკეტა თავისით არ "
                                 "აიტვირთება (დაასკანერე პირადობა, ან ატვირთვა შენ დაადასტურე)"))
    res = _finish(result)
    if res.status == "passed" and full:
        res.summary.insert(0, "ანკეტა MHR-ს და პირადობას ემთხვევა — დადასტურების გარეშე აიტვირთება")
    return res


def _contract_date_warnings(result: ValidationResult, cfg: Config, repo: Repository, meta: dict[str, Any], doc_date: Any,
                            verified: dict[str, Any] | None) -> None:
    """ხელშეკრულების თარიღების გაფრთხილებები (არ აჩერებს): ხელი მუშაობის დაწყების შემდეგ მოეწერა; მუშაობის დაწყებისას
    არასრულწლოვანია (დაბადების თარიღი — დადასტურებული პირადობიდან). მუშაობის დაწყება — ის, რაც თვითონ ქაღალდზე წერია."""
    start, signed = _iso(_field(meta, "contract_start")), _iso(doc_date)
    late = hrdates.signed_late_note(signed, start)
    if late:
        result.checks.append(Check("signed_late", "ხელმოწერის თარიღი", signed, start, "info", False, note=late))
    birth = _verified_birth_date(repo, verified)
    minor = hrdates.minor_note(birth, start or signed, hrdates.limits(cfg)[0]) if birth else None
    if minor:
        result.checks.append(Check("age", "ასაკი", birth, start or signed, "info", False, note=minor))


VERIFIED_NAME_SIMILARITY = 0.7   # დადასტურებულ სახელთან ამდენი მსგავსება (მოკლე სახელებზეც) — წაკითხვის შეცდომაა


def _name_close(doc: str, mhr: str) -> bool:
    return any(len(x) >= 3 and len(y) >= 3 and _ratio(x, y) >= VERIFIED_NAME_SIMILARITY for x in _forms(doc) for y in _forms(mhr))


def pick_original(refs: list[MhrDocRef], doc_number: str | None, doc_date: str | None) -> MhrDocRef | None:
    cands = original_candidates(refs, doc_number, doc_date)
    return cands[0] if cands else None


def _content_tokens(text: str) -> set[str]:
    return {w.strip(_PUNCT) for w in normalize(text).split() if len(w.strip(_PUNCT)) >= 3}


@dataclass
class OriginalFile:
    """ჩამოტვირთული და წაკითხული MHR-ის ფაილი."""
    ref: MhrDocRef
    data: bytes
    orig: Any
    tokens: set[str]


@dataclass
class PagePlan:
    """სკანის გვერდები თანამშრომლის დაგენერირებულ ფაილებზე: `owner[i]` — გვერდის ფაილის doc_id (None — არცერთს არ ემთხვევა)."""
    files: dict[str, OriginalFile] = dc_field(default_factory=dict)
    owner: list[str | None] = dc_field(default_factory=list)
    best: list[tuple[str, float] | None] = dc_field(default_factory=list)   # გვერდისთვის საუკეთესო ფაილი და დამთხვევის წილი

    @property
    def owners(self) -> list[str]:
        return list(dict.fromkeys(o for o in self.owner if o))

    @property
    def needs_split(self) -> bool:
        """სკანში ერთზე მეტი ფაილია, ან ერთ ფაილს ზედმეტი გვერდები ახლავს."""
        return len(self.owners) > 1 or (len(self.owners) == 1 and None in self.owner)


def plan_pages(client, cfg: Config, cands: list[MhrDocRef], scanned_texts: list[str]) -> PagePlan:
    """HR ხშირად ხელშეკრულებას, ინსტრუქციას, წესებსა და დანართებს ერთად ბეჭდავს და ერთ ფაილად ასკანერებს: ყოველი გვერდი მიეკუთვნება
    იმ დაგენერირებულ ფაილს, რომლის სიტყვებსაც ყველაზე მეტად შეიცავს. ერთი ფაილის გვერდები მიმდევრობითაა (სპეც. §2), ამიტომ
    ცუდად წაკითხული ან შეცვლილი გვერდი მეზობელთან რჩება და შედარება სხვაობას პირდაპირ აჩვენებს.

    ფაილის სიტყვებში მისი header/footer-იც ითვლება — ის ყოველ გვერდზე იბეჭდება. მოკლე გვერდი (ხელმოწერები და რეკვიზიტები)
    თითქმის ყველა ფაილს ჰგავს, დიდი ფაილი კი ერთი-ორი სიტყვით ყოველთვის იგებს: ასეთი გვერდი წინა გვერდის ფაილთან რჩება."""
    plan = PagePlan(owner=[None] * len(scanned_texts), best=[None] * len(scanned_texts))
    for ref in cands[:ORIGINAL_CANDIDATES_MAX]:
        if "sha256" in (ref.raw or {}) and not ref.raw.get("sha256"):   # API: ჩანაწერი არის, ფაილი სერვერზე — არა
            continue
        try:
            data = client.download(ref)
        except MhrError:
            continue
        orig = original_pages(data, cfg)
        if orig is not None:
            plan.files[ref.doc_id] = OriginalFile(ref, data, orig, _content_tokens("\n".join([*orig.pages, *orig.strip_lines])))
    if not plan.files:
        return plan
    owner, thin, weak, prev = plan.owner, set(), {}, None
    for i, text in enumerate(scanned_texts):
        toks = _content_tokens(text)
        if len(toks) < PAGE_MIN_TOKENS:
            thin.add(i)
            owner[i] = prev
            continue
        score = {k: len(toks & f.tokens) / len(toks) for k, f in plan.files.items()}
        best = max(score, key=score.get)
        plan.best[i] = (best, score[best])
        if score[best] < PAGE_OWNER_MIN_OVERLAP:
            if score[best] >= PAGE_OWNER_WEAK:
                weak[i] = best
            prev = None
            continue
        tie = PAGE_OWNER_TIE
        if score.get(prev, 0.0) >= PAGE_OWNER_MIN_OVERLAP:   # წინა ფაილიც კარგად ხსნის გვერდს: მოკლე გვერდზე წილი რამდენიმე სიტყვაა
            tie = max(tie, PAGE_OWNER_TIE_WORDS / len(toks))
        if prev in score and score[prev] >= score[best] - tie:
            best = prev
            plan.best[i] = (best, score[best])   # გვერდი წინა ფაილში დარჩა — „რატომ" მისსავე წილს აჩვენებს
        owner[i] = prev = best
    for i, o in enumerate(owner):
        if o is None:
            left_i = next((k for k in range(i - 1, -1, -1) if owner[k]), None)
            right_i = next((k for k in range(i + 1, len(owner)) if owner[k]), None)
            left = owner[left_i] if left_i is not None else None
            right = owner[right_i] if right_i is not None else None
            if i in weak and weak[i] in (left, right):
                owner[i] = weak[i]
            elif right and left == right and right_i - left_i - 1 <= PAGE_OWNER_GAP:
                owner[i] = right
            elif right and left is None and i in thin:
                owner[i] = right
    return plan


_CONTRACT_FILE = re.compile(r"ხელშეკრულება")


def _matched_type(cfg: Config, ref: MhrDocRef, refs: list[MhrDocRef], is_largest: bool) -> str:
    """დამთხვეული ფაილის ტიპი MHR-ის კატეგორიითა და სახელით: ძირითადი ხელშეკრულება → contract, სხვა იმავე კატეგორიაში (ინსტრუქცია,
    წესები, დანართი) → contract_annex. სახელი არაფერს ამბობს → ყველაზე დიდი ფაილი ხელშეკრულებაა."""
    cats = cfg.data["mhr"].get("categories") or {}
    if ref.category and ref.category != cats.get("contract", "contract"):
        tid = {v: k for k, v in cats.items()}.get(ref.category, ref.category)
        return tid if cfg.doc_type(tid) else "contract_annex"
    named = type_from_filename(cfg, ref.filename or ref.title)   # ნასწავლი ტიპი სახელში (მაგ. თანამდებობრივი ინსტრუქცია)
    if named and named != "contract" and cfg.doc_type(named):
        return named
    if any(_CONTRACT_FILE.search(r.filename or "") for r in refs):
        return "contract" if _CONTRACT_FILE.search(ref.filename or "") else "contract_annex"
    return "contract" if is_largest else "contract_annex"


def _split_scan(repo: Repository, cfg: Config, row, meta: dict[str, Any], plan: PagePlan, scanned: list[dict[str, Any]],
                employee: MhrEmployee, pn: str) -> ValidationResult:
    """სკანი → თითო ჩანაწერი ყოველ დამთხვეულ ფაილზე (საკუთარი გვერდებით); არცერთს რომ არ ემთხვევა, ის გვერდები — გადასამოწმებელში."""
    if not row["output_path"] or not Path(row["output_path"]).exists():
        return ValidationResult("failed", employee=employee, summary=["სკანის PDF აღარ არსებობს — ფაილებად ვერ დაიყო"],
                                validated_at=now_iso(), error="no_output_file")
    refs = [f.ref for f in plan.files.values()]
    largest = max(plan.owners, key=lambda k: len(plan.files[k].tokens))
    specs = []
    for key in plan.owners:
        ref = plan.files[key].ref
        specs.append(PartSpec(key, clean_file_title(ref.filename) or ref.title or key, _matched_type(cfg, ref, refs, key == largest),
                              [int(scanned[i]["page_index"]) for i, o in enumerate(plan.owner) if o == key], key, ref.category))
    rest = [int(scanned[i]["page_index"]) for i, o in enumerate(plan.owner) if o is None]
    if rest:
        specs.append(PartSpec("other", "MHR-ის ფაილებს არ ემთხვევა", "unknown", rest, None, None))
    parts = split_document(repo, cfg, row, meta, specs, pn)
    titles = {k: clean_file_title(f.ref.filename) or f.ref.title or k for k, f in plan.files.items()}
    for part in parts:   # რატომ მოხვდა გვერდი ამ ფაილში: სიტყვების რა წილია იმ ფაილში, რომელსაც ყველაზე მეტად ჰგავს
        part["page_match"] = {}
        for i, pt in enumerate(scanned):
            if int(pt["page_index"]) + 1 in part["pages"] and plan.best[i]:
                part["page_match"][str(int(pt["page_index"]) + 1)] = [titles[plan.best[i][0]], round(100 * plan.best[i][1])]
    return ValidationResult("split", employee=employee, parts=parts, validated_at=now_iso(),
                            summary=[f"სკანში {len(plan.owners)} ფაილი აღმოჩნდა — თითოეული ცალკე მოწმდება"
                                     + ("; ზოგი გვერდი MHR-ის ფაილებს არ ემთხვევა" if rest else "")])


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
        for upper in range(ilvl):   # სია ქვე-პუნქტით იწყება („3.1" „3"-ის გარეშე): Word ზედა დონეს გამოყენებულად თვლის, შემდეგი „4"-ია
            if upper in lv:
                cnt.setdefault(upper, lv[upper][0])
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


_PICTURE = re.compile(rb"<(?:w:drawing|w:pict|v:imagedata)\b")


def docx_header_picture(data: bytes) -> bool:
    """docx-ის header-ში სურათია (ბლანკის ლოგო). სკანზე ლოგო ხანდახან სიტყვებად იკითხება — ორიგინალის ტექსტში ისინი არ არის."""
    try:
        with zipfile.ZipFile(io.BytesIO(data or b"")) as zf:
            return any(zf.getinfo(n).file_size <= _DOCX_PART_MAX and _PICTURE.search(zf.read(n)) is not None
                       for n in zf.namelist() if re.fullmatch(r"word/header\d*\.xml", n))
    except (zipfile.BadZipFile, ValueError, KeyError, OSError, EOFError):
        return False


@dataclass
class OriginalText:
    pages: list[str]                    # PDF — გვერდები; docx — ერთი ელემენტი (დაუნომრავი)
    unreadable: list[int] = dc_field(default_factory=list)   # წაუკითხავი PDF გვერდები (1-დან)
    paginated: bool = True
    strip_lines: list[str] = dc_field(default_factory=list)  # docx header/footer — შედარებიდან ორივე მხრიდან იშლება
    header_picture: bool = False        # docx-ის header-ში სურათია: გვერდის თავში წაკითხული ლოგოს სიტყვები ჩამატებული ტექსტი არ არის


def original_pages(data: bytes, cfg: Config) -> OriginalText | None:
    """MHR-ის ორიგინალი → ტექსტი. docx (zip + word/document.xml) — დაუნომრავი ერთი ტექსტი + header/footer ხაზები;
    PDF — გვერდები (ტექსტური ფენა; უხარისხო → OCR). None — არც docx-ია, არც PDF."""
    if data[:2] == b"PK":
        docx = docx_text(data)
        if docx is None:
            return None
        return OriginalText([docx[0]], paginated=False, strip_lines=docx[1], header_picture=docx_header_picture(data))
    pdf = _pdf_pages(data, cfg)
    return OriginalText(pdf[0], pdf[1]) if pdf else None


def _pdf_pages(pdf_bytes: bytes, cfg: Config, dpi: int | None = None, local_only: bool = False) -> tuple[list[str], list[int]] | None:
    """PDF → გვერდების ტექსტი (ტექსტური ფენა; უხარისხო → OCR, `local_only` — მხოლოდ Tesseract). (გვერდები, წაუკითხავი გვერდების
    ნომრები 1-დან). None — ფაილი PDF არ არის. დროებითი ფაილი paths.work-ში, ბოლოს იშლება."""
    work = cfg.paths.work
    work.mkdir(parents=True, exist_ok=True)
    tmp = work / f"mhr_original_{uuid.uuid4().hex}.pdf"
    min_q = float(cfg.thresholds.get("text_layer_min_quality", 0.6))
    dpi = dpi or int(cfg.data["processing"].get("dpi", 300))
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
                read = ""
                if chain.local.available() if local_only else chain.available():
                    img = doc.render(i, dpi=dpi)
                    try:
                        if local_only:
                            read = chain.local.recognize(enhance(img), chain.lang).text
                        else:
                            res = chain.recognize(enhance(img), chain.lang)
                            read = res.result.text if res.ok and res.result is not None else ""
                    finally:
                        img.close()
                pages.append(read if read.strip() else "")
                if not read.strip():
                    unreadable.append(i + 1)
    finally:
        tmp.unlink(missing_ok=True)
    return pages, unreadable


# --- ხელნაწერი / ნიშნები ------------------------------------------------------------------------------
HANDWRITTEN_NOTE = "ხელით მიწერილი ტექსტი"
HANDWRITTEN_SHOWN = 200   # ამდენი სიმბოლო ჩანს ვებ-გვერდზე მიწერილი ტექსტიდან


def handwriting_diffs(scanned: list[dict[str, Any]], names: list[str] | None = None) -> list[TextDiff]:
    """page_texts-ის ნიშნებიდან: ხელნაწერის ეჭვი, ან ნიშნები ხელმოწერის ზონის გარეთ. მხოლოდ-ხელმოწერის გვერდი
    (marks_only, ან signature_hint=present) მოსალოდნელია და ცვლილება არ არის.

    ონლაინ მკითხველით (კოორდინატების გარეშე) წაკითხულ გვერდზე გამოსახულების ნიშნები არაფერს ნიშნავს — ნიღბის გარეშე ნაბეჭდი
    ტექსტიც „ნიშანია“ და ხელშეკრულების ყოველი გვერდი ჩერდებოდა. იქ ხელნაწერი ის არის, რაც მკითხველმა მონიშნა („[ხელნაწერი: …]“),
    ხელმოწერის, ბეჭდისა და ხელმომწერის სახელის (`names`) გარდა; diff-ში ჩანს, რა წერია."""
    out = []
    for i, pt in enumerate(scanned, start=1):
        if readermarks.reader_only(pt):
            notes = readermarks.handwritten(pt.get("text"), names or [])
            if notes:
                out.append(TextDiff(i, "handwriting", "", one_line("; ".join(notes))[:HANDWRITTEN_SHOWN], True, HANDWRITTEN_NOTE))
            continue
        if pt.get("handwriting_suspected"):
            out.append(TextDiff(i, "handwriting", "", "", True, "ხელნაწერის ეჭვი"))
        elif pt.get("has_marks") and not pt.get("marks_only") \
                and pt.get("signature_hint") not in ("present", "not_applicable") \
                and not any((pt.get("signature_parties") or {}).values()):   # მხარის ხელმოწერა/ბეჭედი ნაპოვნია → ეს ისაა
            out.append(TextDiff(i, "handwriting", "", "", True, "ნიშნები ხელმოწერის ზონის გარეთ"))
    return out


_PARTY_KA = {"employer": "დამსაქმებელი", "employee": "დასაქმებული"}
NO_SIGNATURE_BLOCK_MIN_QUALITY = 0.8   # ტექსტი კარგად იკითხება, წარწერა კი არ არის → გვერდზე ხელმოწერის ხაზი არ იბეჭდება
ORIGINAL_CANDIDATES_MAX = 12           # ამდენ დაგენერირებულ ფაილს ჩამოვტვირთავთ გვერდების მისაკუთვნებლად
PAGE_OWNER_MIN_OVERLAP = 0.7           # გვერდის სიტყვების ამ წილზე მეტი ერთ ფაილშია → გვერდი მისია (ნამდვილ სკანებზე სწორი ≥0.79, სხვა ≤0.74)
PAGE_OWNER_WEAK = 0.6                  # ამაზე მეტი და მეზობელი გვერდიც ამ ფაილისაა → მისია (ცუდად წაკითხული ან ჩამატებული ტექსტის მქონე გვერდი)
PAGE_OWNER_GAP = 2                     # ორ იმავე ფაილის გვერდს შორის ამდენამდე გვერდი იმ ფაილისაა; მეტი — სხვა დოკუმენტია
PAGE_OWNER_TIE = 0.05                  # ამაზე ახლო დამთხვევისას წინა გვერდის ფაილი რჩება (ერთი ფაილის გვერდები მიმდევრობითაა)
PAGE_OWNER_TIE_WORDS = 5               # მოკლე გვერდზე ამდენ სიტყვამდე უპირატესობაც „ახლოა" (ნამდვილ სკანებზე: ცრუ — 2 სიტყვა, ნამდვილი — 10+)
PAGE_MIN_TOKENS = 8                    # ამაზე ნაკლები სიტყვა (ტექსტის გარეშე გვერდი) — მეზობელ ფაილს მიეწერება


def party_name(party: str, pt: dict[str, Any]) -> str:
    """მხარე ისე, როგორც ქაღალდზე წერია: სტაჟირების ფაილში მეორე მხარე „სტაჟიორია“."""
    if party == "employee" and any(str(w[0]).strip("„“\"':,.").startswith("სტაჟიორ") for w in pt.get("words") or []):
        return "სტაჟიორი"
    return _PARTY_KA.get(party, party)


def signature_diffs(scanned: list[dict[str, Any]], already: set[int]) -> list[TextDiff]:
    """ხელშეკრულება: ხელმოწერა ყველა გვერდზე (mhr.signature_every_page) — **ორივე მხარის**, თუ გვერდზე ხელმოწერის
    წარწერები („დამსაქმებელი"/„დასაქმებული") მოიძებნა; სხვაგვარად — გვერდის ზოგადი შეფასება. ცარიელი და
    მხოლოდ-ხელმოწერის გვერდი — გამონაკლისი; `already` — ნიშნების გამო უკვე მონიშნული გვერდები (ორჯერ არ ვწერთ)."""
    out = []
    for i, pt in enumerate(scanned, start=1):
        if i in already or pt.get("is_blank") or pt.get("marks_only"):
            continue
        parties = pt.get("signature_parties") or {}
        missing = [party_name(p, pt) for p in ("employer", "employee") if p in parties and not parties[p]]
        if missing:
            out.append(TextDiff(i, "signature", "", "", True, "ხელმოწერა აკლია: " + ", ".join(missing)))
        elif not parties and pt.get("signature_hint") not in ("present", "not_applicable"):
            if pt.get("signature_hint") == "not_detected" and (pt.get("ocr_quality") or 0) >= NO_SIGNATURE_BLOCK_MIN_QUALITY:
                out.append(TextDiff(i, "signature", "", "", False, "ხელმოწერის ადგილი ამ გვერდზე არ არის"))
            else:
                out.append(TextDiff(i, "signature", "", "", True, "ხელმოწერა ვერ მოიძებნა"))
    return out


def last_page_signature_diffs(scanned: list[dict[str, Any]]) -> list[TextDiff]:
    """ხელშეკრულების გარდა სხვა ფაილები (ინსტრუქცია, წესები, დანართი): ხელმოწერა ბოლო გვერდზე მოწმდება — ნამდვილ სკანებზე ასეთ
    ფაილებს ყველა გვერდზე ხელმოწერა არ აქვთ."""
    if not scanned:
        return []
    n, pt = len(scanned), scanned[-1]
    if pt.get("marks_only"):
        return []
    parties = pt.get("signature_parties") or {}
    missing = [party_name(p, pt) for p in ("employer", "employee") if p in parties and not parties[p]]
    if missing:
        return [TextDiff(n, "signature", "", "", True, "ბოლო გვერდზე ხელმოწერა აკლია: " + ", ".join(missing))]
    if not parties and pt.get("signature_hint") != "present":
        return [TextDiff(n, "signature", "", "", True, "ბოლო გვერდზე ხელმოწერა ვერ მოიძებნა")]
    return []


UPLOADED_READ_DPI = 150      # ფაილების გასარჩევად საკმარისია: ნამდვილ 22-გვერდიან პაკეტზე ყველა გვერდი სწორად, ~5 წამი გვერდზე
UPLOADED_CACHE_MAX = 32
_uploaded_texts: dict[str, list[str]] = {}   # ატვირთული PDF-ის ტექსტი: ერთი პაკეტის ფაილები მას ერთხელ კითხულობენ (მხოლოდ მეხსიერებაში)


def _uploaded_pages(client, cfg: Config, u: MhrDocRef) -> list[str]:
    key = str((u.raw or {}).get("sha256") or u.doc_id)
    if key not in _uploaded_texts:
        pdf = _pdf_pages(client.download(u), cfg, dpi=UPLOADED_READ_DPI, local_only=True)
        while len(_uploaded_texts) >= UPLOADED_CACHE_MAX:
            _uploaded_texts.pop(next(iter(_uploaded_texts)))
        _uploaded_texts[key] = pdf[0] if pdf else []
    return _uploaded_texts[key]


def _page_ranges(pages: list[int]) -> str:
    return f"{pages[0]}–{pages[-1]}" if len(pages) > 1 else str(pages[0])


def _inside_uploaded(repo: Repository, cfg: Config, client, refs: list[MhrDocRef], original: MhrDocRef, row,
                     cands: list[MhrDocRef]) -> Check | None:
    """ამ ფაილის ტექსტი უკვე დევს ბარათზე ატვირთულ სხვა PDF-ში: მთელი პაკეტი ერთ ფაილად აიტვირთა (signed_copy_of მხოლოდ ერთ
    ორიგინალს უთითებს) ან ხელით, ორიგინალთან კავშირის გარეშე. სხვაგვარად „მზადაა" მას მეორედ ატვირთავდა. ამ პროგრამის დაყოფილი
    ნაწილი ერთ ფაილს შეიცავს და არ იკითხება; „ატვირთვა მაინც" ღიაა."""
    unread: Check | None = None
    read = 0
    for u in refs:
        raw = u.raw or {}
        external = raw.get("external_id")
        if raw.get("kind") != "uploaded" or external == row["id"]:
            continue
        local = repo.get_document(external) if external else None
        if local is not None and json.loads(local["metadata"] or "{}").get("part"):
            continue
        try:
            texts = _uploaded_pages(client, cfg, u)
        except (MhrError, OcrError, OSError, ValueError) as e:
            log.warning("uploaded %s not read: %s", u.doc_id, type(e).__name__)
            unread = Check("already_uploaded", "უკვე ატვირთულია", None, u.doc_id, "info", False,
                           note="ბარათზე ატვირთული PDF ვერ წაიკითხა — ორჯერ ატვირთვა ვერ შემოწმდა, ატვირთვამდე ნახე ბარათი")
            continue
        if not any(t.strip() for t in texts):
            continue
        read += 1
        pages = [i for i, o in enumerate(plan_pages(client, cfg, cands, texts).owner, start=1) if o == original.doc_id]
        if pages:
            when = (u.created_at or "")[:10]
            return Check("already_uploaded", "უკვე ატვირთულია", None, u.doc_id, "info", True,
                         note=f"ეს ფაილი ბარათზე უკვე დევს ატვირთულ PDF-ში{f' ({when})' if when else ''}, გვერდი {_page_ranges(pages)} "
                              "— ხელახლა ატვირთვამდე დარწმუნდი, რომ ეს ახალი ვერსიაა")
    return unread or Check("already_uploaded", "ორჯერ ატვირთვა", None, None, "match", False,
                           note=f"ბარათზე {read} სხვა ატვირთული PDF წავიკითხე, ეს ფაილი არცერთში არ არის" if read
                           else "ბარათზე ამ ფაილის ატვირთული ასლი არ არის")


def _already_uploaded(refs: list[MhrDocRef], original: MhrDocRef | None, document_id: str) -> Check | None:
    """ამ ფაილის ხელმოწერილი სკანი MHR-ში უკვე არის (signed_copy_of = ორიგინალი) → აჩერებს: „ყველას ატვირთვა" მეორედ არ უნდა
    ატვირთავდეს; „ატვირთვა მაინც" ღიაა. მხოლოდ ხელით მონიშნული ხელმოწერილი (ფაილის გარეშე) — შენიშვნა."""
    if original is None:
        return None
    for r in refs:
        raw = r.raw or {}
        if raw.get("kind") == "uploaded" and str(raw.get("signed_copy_of")) == original.doc_id                 and raw.get("external_id") != document_id:
            when = (r.created_at or "")[:10]
            return Check("already_uploaded", "უკვე ატვირთულია", None, r.doc_id, "info", True,
                         note=f"ეს ფაილი ამ თანამშრომელზე MHR-ში უკვე ატვირთულია{f' ({when})' if when else ''} "
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
    disagree = sorted({d.page for d in result.diffs if d.suspect and not d.significant and RECHECK_DISAGREE in d.reason})
    if disagree:
        lines.append(f"ყურადღება — გვერდი {', '.join(map(str, disagree))}: ორმა მკითხველმა ერთი ადგილი სხვადასხვანაირად წაიკითხა "
                     "(Tesseract-მა — როგორც ორიგინალშია), ატვირთვამდე თვალით შეადარე ქაღალდს")
    unverified = sorted({d.page for d in result.diffs if d.suspect and not d.significant} - set(disagree))
    if unverified:
        lines.append(f"ყურადღება — გვერდი {', '.join(map(str, unverified))}: პატარა სხვაობა Claude-ით ვერ გადამოწმდა, "
                     "ატვირთვამდე თვალით შეადარე ორიგინალს")
    if result.status == "passed":
        lines.insert(0, "ყველა შემოწმება გავიდა")
    return lines


def _finish(result: ValidationResult, compared_original: bool = False, compared_template: bool = False) -> ValidationResult:
    blocked = any(c.blocking for c in result.checks) or bool(result.significant_diffs)
    result.status = "failed" if blocked else "passed"
    result.summary = _summary(result, compared_original, compared_template)
    result.validated_at = now_iso()
    return result


# --- საჯარო API -----------------------------------------------------------------------------------------------
MHR_ERROR_KA = {   # MHR-ის პასუხის კოდი → რა მოხდა და რა ვქნა, მარტივად
    "unauthenticated": "MHR-მა ტოკენი არ მიიღო: ტოკენი არასწორია ან ვადაგასულია. ჩაწერე ახალი ტოკენი პარამეტრებში",
    "forbidden": "MHR-მა უარი თქვა: ტოკენს ამის უფლება არ აქვს, ან ეს კომპიუტერი დაშვებულ მისამართებში არ არის",
    "not_pdf": "MHR-მა ფაილი არ მიიღო: ის PDF არ არის",
    "sha256_mismatch": "MHR-მა ფაილი არ მიიღო: გზაში დაზიანდა. სცადე ხელახლა",
    "file_too_large": "MHR-მა ფაილი არ მიიღო: ზომა ლიმიტზე დიდია",
    "unknown_category": "MHR-მა ფაილი არ მიიღო: ასეთი დოკუმენტის კატეგორია იქ არ არსებობს. შეამოწმე კატეგორიები პარამეტრებში",
    "invalid_signed_copy_of": "MHR-მა ფაილი არ მიიღო: ორიგინალი ამ თანამშრომლის დაგენერირებული ფაილი აღარ არის. „ხელახლა შედარება“",
    "external_id_conflict": "MHR-ში ამ ნომრით სხვა ფაილი უკვე დევს. ნახე თანამშრომლის ბარათი",
    "multiple_matches": "MHR-ში ამ პირადი ნომრით რამდენიმე აქტიური ჩანაწერია. ჯერ HR-მა უნდა მოაგვაროს",
    "rate_limited": "MHR-მა მოთხოვნების რაოდენობა შეზღუდა. პროგრამა მოგვიანებით თავად სცდის",
    "validation_failed": "MHR-მა მოთხოვნის ველები არ მიიღო. იხილე ტექნიკური ჟურნალი",
}


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
    type_id = row["type_id"] or meta.get("document_type_id") or "unknown"
    upload_types = mhr.get("upload_type_ids") or []
    if upload_types and type_id not in upload_types:
        return ValidationResult("skipped", summary=[f"ტიპი „{type_id}“ MHR-ზე არ იტვირთება"], validated_at=now_iso())

    try:
        result = _validate(repo, cfg, row, meta, pn, type_id, client)
        _place_diffs(result, document_pages(repo, repo.get_document(document_id) or row))
        return result
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
        return ValidationResult("failed", summary=[MHR_ERROR_KA.get(str(e), "MHR-ის შეცდომა (კონფიგურაცია/უფლება) — იხილეთ ტექნიკური ჟურნალი")],
                                validated_at=now_iso(), error=type(e).__name__)


PAGE_TWIN_MIN = 0.9          # ორი გვერდის სიტყვების ამ წილი საერთოა → ერთი ფურცელი ორჯერ გავიდა
MIN_TWIN_WORDS = 30          # მოკლე გვერდები შეიძლება მართლა ჰგავდეს ერთმანეთს
ORDER_ANCHOR_WORDS = 8       # გვერდის ადგილი ორიგინალში ამდენი ზედიზედ დამთხვეული სიტყვით დგინდება


def _pages_out_of_order(pages: list[dict[str, Any]], orig_text: str) -> bool:
    """ყოველი გვერდი თავის ადგილს ეძებს ორიგინალში: ადგილი უკან თუ წავიდა, ფურცლები სხვა რიგითაა."""
    orig = [w.strip(_PUNCT) for w in normalize(orig_text).split()]
    where = []
    for p in pages:
        words = [w.strip(_PUNCT) for w in normalize(p.get("text") or "").split()]
        block = SequenceMatcher(None, orig, words, autojunk=False).find_longest_match(0, len(orig), 0, len(words))
        if block.size >= ORDER_ANCHOR_WORDS:
            where.append(block.a - block.b)
    return any(b < a for a, b in zip(where, where[1:]))


def _scan_hints(result: ValidationResult, pages: list[dict[str, Any]], orig_text: str) -> None:
    """ტექსტის სხვაობის ხშირი მიზეზი სკანერია და არა დოკუმენტი: ფურცელი ორჯერ გავიდა ან რიგი აირია. ეს ახსნაა და არ აჩერებს."""
    if not any(d.significant and d.kind in RECHECK_KINDS for d in result.diffs):
        return
    tokens = [_content_tokens(p.get("text") or "") for p in pages]
    twins = [(i + 1, j + 1) for i, a in enumerate(tokens) for j, b in enumerate(tokens[i + 1:], start=i + 1)
             if min(len(a), len(b)) >= MIN_TWIN_WORDS and len(a & b) >= PAGE_TWIN_MIN * max(len(a), len(b))]
    if twins:
        i, j = twins[0]
        result.checks.append(Check("scan_hint", "სკანი", None, None, "info", False,
                                   note=f"გვერდი {i} და გვერდი {j} თითქმის ერთნაირია: ალბათ ერთი ფურცელი სკანერში ორჯერ გავიდა"))
        return
    if len(pages) > 1 and _pages_out_of_order(pages, orig_text):
        result.checks.append(Check("scan_hint", "სკანი", None, None, "info", False,
                                   note="გვერდები ორიგინალისგან სხვა რიგითაა: ალბათ ფურცლები სკანერში არეული რიგით ჩაიდო. შეამოწმე რიგი და ხელახლა დაასკანერე"))


def _place_diffs(result: ValidationResult, pages: list[dict[str, Any]]) -> None:
    """სად არის ყოველი პრობლემა გვერდზე — ვებ-გვერდი იმ ადგილს მონიშნავს."""
    for d in result.diffs:
        if not (d.significant or d.suspect) or not 0 < d.page <= len(pages):
            continue
        pt = pages[d.page - 1]
        if d.kind == "signature":
            zones = pt.get("signature_boxes") or {}
            d.boxes = [zones[p] for p, found in (pt.get("signature_parties") or {}).items() if not found and p in zones]
        elif d.kind == "handwriting":   # კოორდინატების გარეშე წაკითხულ გვერდზე „ნიშნები" ნაბეჭდი ტექსტის ბლოკებია — არაფერი მოინიშნება
            d.boxes = [] if readermarks.reader_only(pt) else list(pt.get("marks") or [])
        elif d.scan_parts:
            d.boxes = locate(pt.get("words") or [], d.scan_parts)


def _field_checks(result: ValidationResult, cfg: Config, row, meta: dict[str, Any], pn: str, employee: MhrEmployee,
                  verified: dict[str, Any] | None = None) -> None:
    mhr = cfg.data["mhr"]
    threshold = float(mhr.get("name_similarity", 0.85))
    checks = result.checks
    checks.append(_name_check("first_name", _field(meta, "first_name"), employee.first_name, threshold, verified))
    checks.append(_name_check("last_name", _field(meta, "last_name"), employee.last_name, threshold, verified))
    if verified:
        checks.append(Check("identity", "პირადობა", None, None, "match", False,
                            note="თანამშრომლის პირადობა დადასტურებულია — MHR-ში სახელი და გვარი სწორია"))
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


def _validate(repo: Repository, cfg: Config, row, meta: dict[str, Any], pn: str, type_id: str, client) -> ValidationResult:
    mhr = cfg.data["mhr"]
    result = ValidationResult("failed")
    employee: MhrEmployee | None = client.get_employee(pn)
    if employee is None:
        if type_id == "unknown":   # ტიპიც უცნობია, პ/ნ-იც შეიძლება არასწორია — გადასამოწმებელში რჩება, MHR-ის შეცდომად არ ითვლება
            return ValidationResult("skipped", summary=["ამ პირადი ნომრით თანამშრომელი MHR-ში ვერ მოიძებნა, დოკუმენტის ტიპიც უცნობია"],
                                    validated_at=now_iso())
        result.mhr_snapshot = mhr_snapshot(cfg, None, [])
        result.checks.append(Check("personal_number", LABELS_KA["personal_number"], pn, None, "missing_in_mhr", True,
                                   note="თანამშრომელი ამ პირადი ნომრით MHR-ში ვერ მოიძებნა"))
        return _finish(result)
    result.employee = employee

    # --- რომელ დაგენერირებულ ფაილს ეკუთვნის სკანი ---
    doc_date = row["document_date"] or meta.get("document_date")
    scanned = document_pages(repo, row, meta)
    refs = client.list_documents(employee.employee_id)
    result.mhr_snapshot = mhr_snapshot(cfg, employee, refs)
    dt = cfg.doc_type(type_id)
    if dt is not None and dt.identity:   # პირადობა: MHR-ის ფაილებთან კი არა — თანამშრომლის სახელთან და პ/ნ-თან, ზუსტად
        res = _validate_identity(result, cfg, row, type_id, scanned, employee, refs)
        if row["status"] == "needs_review" and any(c.field == "identity" and c.status == "match" for c in res.checks):
            adopt_match(repo, row, meta, type_id)   # სახელი, გვარი და პ/ნ ზუსტად ემთხვევა → „ტიპი სავარაუდოა“ აღარ არის ეჭვი
        return res
    verified = verified_identity(repo, cfg, employee.employee_id, refs)
    if dt is not None and dt.employee_form:   # ხელით შევსებული ანკეტა: MHR-ის ფაილებთან კი არა — თანამშრომლის მონაცემებთან
        return _validate_employee_form(result, cfg, repo, row, type_id, scanned, employee, refs, verified)
    all_generated = [r for r in refs if _is_generated(r)]
    # თითო ეტაპს (სტაჟიორი / გამოსაცდელი / შრომითი) თავისი ფაილი აქვს: ქაღალდს მხოლოდ მისივე ეტაპის, ბოლო ცვლილების ფაილი ედარება
    stage = scan_stage(repo, row, scanned)
    generated = same_stage(latest_versions(all_generated), stage)
    category = (mhr.get("categories") or {}).get(type_id, type_id)
    pinned = str((meta.get("part") or {}).get("original_id") or "")   # სკანის ნაწილი: ფაილი დაყოფისას უკვე დადგინდა
    pool = [r for r in generated if (r.raw or {}).get("kind") == "generated" or r.category == category]
    plan = PagePlan()
    found: OriginalFile | None = None
    ref: MhrDocRef | None = None
    if pinned:
        ref = next((r for r in all_generated if r.doc_id == pinned), None)
    else:
        plan = plan_pages(client, cfg, original_candidates(pool, None, doc_date), [p.get("text") or "" for p in scanned])
        if plan.needs_split:
            return _split_scan(repo, cfg, row, meta, plan, scanned, employee, pn)
        if plan.owners:
            found = plan.files[plan.owners[0]]
            ref = found.ref
            biggest = max(len(f.tokens) for f in plan.files.values())
            new_type = _matched_type(cfg, ref, [f.ref for f in plan.files.values()], len(found.tokens) == biggest)
            if new_type != type_id or row["status"] == "needs_review":
                adopt_match(repo, row, meta, new_type)
                type_id = new_type

    is_contract = type_id in (mhr.get("contract_type_ids") or [])
    dt = cfg.doc_type(type_id)
    needs_original = is_contract or bool(dt and dt.mhr_original)   # ორიგინალის გარეშე — „დააგენერირე“, არა მხოლოდ ველები
    if ref is None and not pinned and not needs_original:
        if type_id == "unknown":
            return ValidationResult("skipped", employee=employee, validated_at=now_iso(),
                                    summary=["ამ თანამშრომლის დაგენერირებულ ფაილებს არ ემთხვევა და ტიპიც უცნობია — გადასამოწმებელში რჩება"])
        _field_checks(result, cfg, row, meta, pn, employee, verified)   # სხვა ტიპი, ორიგინალის გარეშე — მხოლოდ ველები
        return _finish(result)

    checks = result.checks
    if is_contract:
        _field_checks(result, cfg, row, meta, pn, employee, verified)
        _contract_date_warnings(result, cfg, repo, meta, doc_date, verified)
    else:   # ინსტრუქცია, წესები, დანართი: სახელი და მონაცემები ფაილის ტექსტშია — შედარება ამას ამოწმებს
        checks.append(_check("personal_number", pn, employee.personal_number,
                             lambda d, m: digits_only(str(d)) == digits_only(str(m))))
        if verified:
            checks.append(Check("identity", "პირადობა", None, None, "match", False,
                                note="თანამშრომლის პირადობა დადასტურებულია — MHR-ში სახელი და გვარი სწორია"))

    # --- ხელმოწერები (ორიგინალის გარეშეც მოწმდება) ---
    marked = handwriting_diffs(scanned, _name_tokens(employee))
    if not is_contract:   # ბეჭედი/ნიშანი ზონის გარეთ („დამტკიცებულია" ბეჭედი) ასეთ ფაილებზე ჩვეულებრივია — შენიშვნა
        for d in marked:
            d.significant, d.reason = False, f"დანართი — {d.reason}"
    result.diffs.extend(marked)
    if mhr.get("signature_every_page", True):
        result.diffs.extend(signature_diffs(scanned, {d.page for d in marked}) if is_contract else last_page_signature_diffs(scanned))

    # --- ორიგინალი MHR-დან (მხოლოდ kind generated — .docx) ---
    if ref is None and not pinned:
        fallback = original_candidates(_fallback_pool(cfg, generated, type_id, category),
                                       row["doc_number"] or _field(meta, "document_number"), doc_date)
        ref = fallback[0] if fallback else None
        found = plan.files.get(ref.doc_id) if ref else None
    uploaded = _already_uploaded(refs, ref, row["id"])
    if uploaded:
        checks.append(uploaded)
    if ref is None:
        if pinned:
            checks.append(Check("original_contract", LABELS_KA["original_contract"], None, pinned, "missing_in_mhr", True,
                                note="ეს ფაილი MHR-ის ბარათზე აღარ არის — ის შეიძლება წაიშალა ან ხელახლა დაგენერირდა"))
            return _finish(result)
        if is_contract and mhr.get("template_fallback"):   # ძველი ქცევა, მხოლოდ კონფიგით: თანამდებობის შაბლონთან შედარება
            return _compare_with_template(result, cfg, client, employee, scanned, repo, row)
        checks.append(Check("original_contract", LABELS_KA["original_contract"], None, None, "missing_in_mhr", True,
                            note=missing_original_note(cfg, type_id, stage, latest_versions(all_generated))))
        return _finish(result)
    result.original = ref
    date_note = original_date_note(ref, doc_date)
    if date_note:
        checks.append(date_note)
    missing_file = Check("original_contract", LABELS_KA["original_contract"], None, ref.doc_id, "missing_in_mhr", True,
                         note="ორიგინალის ფაილი MHR-ის სერვერზე არ არის (ჩანაწერი არსებობს, ფაილი — არა)")
    if "sha256" in (ref.raw or {}) and not ref.raw.get("sha256"):   # API: sha256 null = ფაილი სერვერზე ვერ მოიძებნა
        checks.append(missing_file)
        return _finish(result)
    orig = found.orig if found else None
    if found is None:
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

    scanned_texts = [_clean_recheck_text(p.get("text") or "") if (p.get("text_source") or "").startswith("ocr:online")
                     else p.get("text") or "" for p in scanned]   # „[ხელნაწერი: …]", „[?]" — მოდელის ნიშნები, არა ტექსტი
    for p in orig.unreadable:   # წაუკითხავი ორიგინალის გვერდი ცალკე diff-ია; ტექსტური შედარება მასზე აზრს მოკლებულია
        if p - 1 < len(scanned_texts):
            scanned_texts[p - 1] = ""
        result.diffs.append(TextDiff(p, "changed", "", "", True, "ორიგინალის გვერდი წაუკითხავია — შედარება ვერ მოხერხდა"))
    known = known_tokens(employee)   # ბეჭდის ციფრები („2009", ს/კ) სუფთა ჩამატებად არ ჩაითვალოს
    stamp = readermarks.stamp_words(p.get("text") for p in scanned)   # ბეჭედი, რომელიც მკითხველმა ზოგ გვერდზე წინადადების შუაში ჩაწერა

    def compare(texts: list[str]) -> list[TextDiff]:
        return compare_texts(orig.pages, texts, cfg, original_paginated=orig.paginated, strip_lines=orig.strip_lines,
                             labels=clause_labels(orig.pages), known=known, trusted_pages=trusted_pages(scanned),
                             header_picture=orig.header_picture, stamp=stamp)
    primary = compare(scanned_texts)
    result.diffs.extend(primary)
    _recheck_diffs(result, cfg, repo, row, scanned, scanned_texts, primary, compare)
    if not result.significant_diffs:
        # ფაილი ორიგინალს ემთხვევა ⇒ MHR-ის ამჟამინდელი ხელფასი/თანამდებობა/ვადა მოგვიანებით შეიცვალა,
        # დოკუმენტი კი არა (სპეც. §12: დღევანდელი მონაცემი ძველ დოკუმენტს არ ანაცვლებს) — შენიშვნა, არა ბლოკი
        for c in checks:
            if c.field in _CURRENT_STATE_FIELDS and c.status == "mismatch":
                c.blocking = False
                c.note = "ხელშეკრულება ორიგინალს ემთხვევა — MHR-ში ამჟამინდელი მონაცემი განსხვავდება" + \
                    (f"; {c.note}" if c.note else "")
    _scan_hints(result, scanned, "\n".join(orig.pages))
    if not result.significant_diffs and not any(c.blocking for c in checks):   # მხოლოდ „მზადა" ფაილზე: წაკითხვას დრო უნდა
        cands = original_candidates(pool, None, doc_date)
        inside = _inside_uploaded(repo, cfg, client, refs, ref, row, cands if ref in cands else [ref, *cands])
        if inside:
            checks.append(inside)
    return _finish(result, compared_original=True)


_LABEL_RE = re.compile(r"^\s*(\d+(?:\.\d+)+|\d+(?=\.\s))\.?\s", re.MULTILINE)


def clause_labels(pages: list[str]) -> set[str]:
    """ორიგინალის/შაბლონის პუნქტის ნომრები ხაზის თავში („1.5.1.", „2.") — მხოლოდ წერტილიანი ფორმა, რომ თანხა
    („1800 ლარი" ხაზის თავში) ნომრად არ ჩაითვალოს."""
    return {m.group(1) for p in pages for m in _LABEL_RE.finditer(p)}


def _compare_with_template(result: ValidationResult, cfg: Config, client, employee: MhrEmployee,
                           scanned: list[dict[str, Any]], repo: Repository | None = None, row=None) -> ValidationResult:
    """მხოლოდ `mhr.template_fallback=true`-ზე (ნაგულისხმევად გამორთულია): დაგენერირებული ორიგინალი არ არის → თანამდებობის +
    ხელშეკრულების ტიპის შაბლონი MHR-დან. `${…}` ცვლადები (სახელი, პ/ნ, თარიღი, ხელფასი) შედარებისას გამოტოვებულია —
    ისინი MHR-ის მონაცემებს ზემოთ ველებად შედარდა; ხელფასის სხვაობა ბლოკავს."""
    missing = NO_ORIGINAL_NOTE
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
                             labels=clause_labels(orig.pages), known=known_tokens(employee), trusted_pages=trusted_pages(scanned),
                             header_picture=orig.header_picture)
    scanned_texts = [p.get("text") or "" for p in scanned]
    primary = compare(scanned_texts)
    result.diffs.extend(primary)
    if repo is not None and row is not None:
        _recheck_diffs(result, cfg, repo, row, scanned, scanned_texts, primary, compare)
    return _finish(result, compared_template=True)


# --- საეჭვო გვერდის ხელახალი წაკითხვა ონლაინ OCR-ით ------------------------------------------------------------
RECHECK_KINDS = ("changed", "added", "removed")
RECHECK_STILL = "ხელახალი წაკითხვითაც"
RECHECK_DISAGREE = "Tesseract-მა ორიგინალი წაიკითხა, Claude-მა სხვა"
RECHECK_DOUBT_REASONS = ("სიტყვა შეცვლილია", "ამოღებული ტექსტი")   # ციფრი და „არ“ აქ არ არის: ისინი ყოველთვის აჩერებს
RECHECK_REPEAT_WORDS = 40     # ამდენი სიტყვა ზედიზედ მეორდება → Claude-მა გვერდი გაიმეორა („უკაცრავად, … თავიდან"); ნამდვილ ხელშეკრულებაში 12-სიტყვიანი ფრაზა ორჯერ წერია


_RECHECK_MARKER = re.compile(r"\[[^\]\n]{0,200}\]")   # „[ბეჭედი: …]", „[ხელნაწერი: …]" — მოდელის ნიშნები, არა ტექსტი


def trusted_pages(scanned: list[dict[str, Any]]) -> set[int]:
    """გვერდები (1-დან), რომელთა ტექსტი Tesseract-ის არ არის (Claude, ტექსტური ფენა): მათზე პატარა სხვაობაც ნამდვილია."""
    return {i for i, p in enumerate(scanned, start=1)
            if (p.get("text") or "").strip() and not (p.get("text_source") or "").startswith("ocr:tesseract")}


def _clean_recheck_text(text: str) -> str:
    return _RECHECK_MARKER.sub(" ", text or "")


def _reading_broke(text: str) -> bool:
    """Claude-ის პასუხი გვერდის ტექსტი არ არის: ცარიელია, ან გვერდი გაიმეორა."""
    words = text.split()
    seen: set[tuple[str, ...]] = set()
    for i in range(len(words) - RECHECK_REPEAT_WORDS + 1):
        run = tuple(words[i:i + RECHECK_REPEAT_WORDS])
        if run in seen:
            return True
        seen.add(run)
    return not words


def _online_page_text(cfg: Config, provider, output_path: str, page_index: int, rotation: int) -> str:
    """დოკუმენტის PDF-ის გვერდი → ონლაინ OCR მხოლოდ ბეჭდური ტექსტისთვის (იგივე რენდერი/გაუმჯობესება, რაც ანალიზისას)."""
    dpi = int(cfg.section("processing").get("dpi", 300))
    with PdfDocument(output_path) as doc:
        img = doc.render(page_index, dpi=dpi)
    if rotation:
        img = apply_rotation(img, int(rotation))
    recognize = getattr(provider, "recognize_printed", None) or provider.recognize
    return _clean_recheck_text(recognize(enhance(img), cfg.data["ocr"].get("tesseract_lang") or "kat+eng").text)


def _only_claude_differs(d: TextDiff, first: list[TextDiff]) -> bool:
    """ორიგინალის ეს სიტყვები პირველ (Tesseract) წაკითხვაში სხვაობად არ ჩანდა: იქ ორიგინალს დაემთხვა, სხვა მხოლოდ Claude-მა წაიკითხა.
    ნამდვილი გვერდი: ქაღალდზე „თანხმობას“, Claude-მა კი „თანხმდება“. Tesseract ორიგინალს არ იცნობს, ამიტომ ზუსტ დამთხვევას შემთხვევით ვერ მოიგონებს."""
    def words(parts: list[str], whole: str) -> set[str]:
        return set((parts[1] if len(parts) == 3 else whole).split())
    # მხოლოდ იგივე ადგილი ითვლება: იგივე სიტყვა სხვა გვერდის სხვაობაში (ნამდვილი სკანი: „ყოველი“ მე-13 გვერდზე „ყველა“, მე-8-ზე
    # „ყველი“) ამ ადგილზე არაფერს ამბობს. მეზობელი გვერდი რჩება — გვერდის ბოლოს ამოღებული ტექსტი შემდეგ გვერდს მიეწერება
    near = [p for p in first if abs(p.page - d.page) <= 1]
    lost, new = words(d.orig_parts, d.original), words(d.scan_parts, d.scanned)
    if lost:
        return not any(lost & words(p.orig_parts, p.original) for p in near)
    return bool(new) and not any(new & words(p.scan_parts, p.scanned) for p in near)


def _recheck_diffs(result: ValidationResult, cfg: Config, repo: Repository, row, scanned: list[dict[str, Any]],
                   scanned_texts: list[str], primary: list[TextDiff], compare) -> None:
    """წესებმა ტექსტური სხვაობა იპოვა Tesseract-ით წაკითხულ გვერდზე (დაკარგული ციფრი „2 კვირით" → „კვირით", „მე-5" →
    „მე-3") → ეს გვერდი ონლაინ OCR-ით (Claude) ხელახლა იკითხება და შედარება მეორდება: სხვაობა გაქრა — წაკითხვის შეცდომა
    იყო (სიაში ხმაურად რჩება); დარჩა — ნამდვილი სხვაობაა. ახალი ტექსტი page_texts-ში ინახება, რომ AI ანალიზი და
    ხელახალი შედარება უკეთეს ტექსტს ხედავდეს. ≤ `mhr.recheck.max_pages` გვერდი ერთ დოკუმენტზე (ხარჯის კონტროლი): ჯერ
    მნიშვნელოვანი სხვაობის გვერდები, მერე საეჭვო პატარა სხვაობისა (`suspect`), რომელიც წაკითხვის შემდეგ ან ქრება, ან ნამდვილია."""
    rc = cfg.section("mhr").get("recheck") or {}
    if not rc.get("enabled", True):
        return
    budget = int(rc.get("max_pages", 4))
    provider = None
    tried: set[int] = set()
    rechecked: list[int] = []
    current = primary
    while True:   # ერთი გვერდის გასწორებამ შეიძლება შედარება გადაალაგოს და ახალი გვერდი გამოაჩინოს — ბიუჯეტის ფარგლებში ისიც იკითხება
        flagged = [d for d in current if (d.significant or d.suspect) and d.kind in RECHECK_KINDS and 0 < d.page <= len(scanned)]
        must = {d.page for d in flagged if d.significant}
        pages = [p for p in [*sorted(must), *sorted({d.page for d in flagged} - must)]
                 if p not in tried and (scanned[p - 1].get("text_source") or "").startswith("ocr:tesseract")
                 and scanned_texts[p - 1]][:max(0, budget - len(tried))]
        if not pages:
            break
        if provider is None:
            provider = getattr(build_ocr_chain(cfg), "online", None)
            if provider is None or not provider.available():
                break
        changed = False
        for p in pages:
            tried.add(p)
            pt = scanned[p - 1]
            try:
                for _attempt in range(2):
                    text = _online_page_text(cfg, provider, row["output_path"], _pdf_index(row, int(pt["page_index"])),
                                             int(pt.get("rotation_applied") or 0))
                    if not _reading_broke(text or ""):
                        break
                    log.warning("recheck page %s of %s: unusable reading, %s", p, row["id"], "retry" if _attempt == 0 else "kept Tesseract text")
                else:
                    continue
            except (OcrError, OSError, ValueError, KeyError, TypeError) as e:
                log.warning("recheck page %s of %s failed: %s", p, row["id"], type(e).__name__)
                continue
            new_pt = dict(pt, text=text, text_source=f"ocr:{getattr(provider, 'name', 'online')}:recheck",
                          ocr_quality=round(float(textquality.score(text)), 4), provider=getattr(provider, "name", "online"))
            scanned[p - 1] = new_pt
            scanned_texts[p - 1] = text
            repo.save_page_text(row["job_id"], int(pt["page_index"]), new_pt)
            rechecked.append(p)
            changed = True
        if not changed:
            break
        current = compare(scanned_texts)
    if rechecked:
        log.info("recheck %s: pages %s re-read online", row["id"], rechecked)
        old = {id(d) for d in primary}
        result.diffs = [d for d in result.diffs if id(d) not in old]
        result.diffs.extend(current)
    _local_second_reading(cfg, repo, row, scanned, current, budget)
    _weigh_readers(current, scanned, scanned_texts, compare)


LOCAL_TEXT = "local_text"   # page_texts-ის გასაღები: იმავე გვერდის ადგილობრივი (Tesseract) წაკითხვა — მეორე აზრი Claude-ის ტექსტზე


def _local_page_text(cfg: Config, provider, output_path: str, page_index: int, rotation: int) -> str:
    """დოკუმენტის PDF-ის გვერდი → ადგილობრივი OCR (იგივე რენდერი/გაუმჯობესება, რაც ანალიზისას)."""
    dpi = int(cfg.section("processing").get("dpi", 300))
    with PdfDocument(output_path) as doc:
        img = doc.render(page_index, dpi=dpi)
    if rotation:
        img = apply_rotation(img, int(rotation))
    return provider.recognize(enhance(img), cfg.data["ocr"].get("tesseract_lang") or "kat+eng").text


def _local_second_reading(cfg: Config, repo: Repository, row, scanned: list[dict[str, Any]], diffs: list[TextDiff], budget: int) -> None:
    """გვერდი პირველივე ჯერზე Claude-მა წაიკითხა (`ocr.path = online`) და შედარებამ შეცვლილი სიტყვა ან ამოღებული ტექსტი იპოვა.
    ნამდვილ სკანებზე Claude 14-გვერდიან ხელშეკრულებაში დაახლოებით ერთ სიტყვას მსგავსი მნიშვნელობის სხვა სიტყვით კითხულობს
    („დაეკისრება“ → „დაექვემდებარება“, „ყოველი“ → „ყველა“) — ქაღალდზე კი ორიგინალი წერია. მეორე აზრისთვის იმავე გვერდს
    ადგილობრივი Tesseract კითხულობს (უფასოა, კომპიუტერიდან არაფერი გადის); მისი ტექსტი გვერდთან ინახება (`local_text`), რომ
    „ხელახლა შედარებამ“ თავიდან არ წაიკითხოს. რას ნიშნავს მისი პასუხი — `_weigh_readers`. ≤ `budget` გვერდი ერთ დოკუმენტზე."""
    pages = sorted({d.page for d in diffs if d.significant and d.kind in RECHECK_KINDS and d.reason in RECHECK_DOUBT_REASONS
                    and 0 < d.page <= len(scanned) and readermarks.reader_only(scanned[d.page - 1])
                    and not scanned[d.page - 1].get(LOCAL_TEXT)})[:max(0, budget)]
    if not pages:
        return
    local = getattr(build_ocr_chain(cfg), "local", None)
    if local is None or not local.available():
        return
    for p in pages:
        pt = scanned[p - 1]
        try:
            text = _local_page_text(cfg, local, row["output_path"], _pdf_index(row, int(pt["page_index"])), int(pt.get("rotation_applied") or 0))
        except (OcrError, OSError, ValueError, KeyError, TypeError, RuntimeError) as e:   # RuntimeError: დაზიანებული PDF (pdfium)
            log.warning("second reading of page %s of %s failed: %s", p, row["id"], type(e).__name__)
            continue
        if not text.strip():
            continue
        scanned[p - 1] = dict(pt, **{LOCAL_TEXT: text})
        repo.save_page_text(row["job_id"], int(pt["page_index"]), scanned[p - 1])
        log.info("second reading %s: page %s read locally", row["id"], p)


def _weigh_readers(diffs: list[TextDiff], scanned: list[dict[str, Any]], scanned_texts: list[str], compare) -> None:
    """Claude-ის ტექსტის გვერდზე დარჩენილი სხვაობა: რა ნახა მეორე მკითხველმა (Tesseract)? Tesseract ორიგინალს არ იცნობს, ამიტომ
    ზუსტ დამთხვევას შემთხვევით ვერ მოიგონებს: თუ მან ეს ადგილი ორიგინალივით წაიკითხა, შეცვლილი სიტყვა ან ამოღებული ტექსტი Claude-ის
    წაკითხვის შეცდომაა და ფაილს არ აჩერებს (საეჭვოდ რჩება); რიცხვი და „არ“ მაინც აჩერებს.

    ორი შემთხვევა: (1) გვერდი ჯერ Tesseract-მა წაიკითხა, მერე Claude-მა ხელახლა — Tesseract-ის სიტყვები გვერდთან რჩება, ამიტომ
    „ხელახლა შედარებაც“ იმავე პასუხს იძლევა; (2) გვერდი თავიდანვე Claude-მა წაიკითხა და Tesseract-მა მეორე აზრისთვის
    (`_local_second_reading`). მეორე შემთხვევაში Tesseract-მაც თუ სხვაობა ნახა (ან ეს სიტყვა ვერ წაიკითხა), არაფერი იცვლება."""
    reread = {n for n, pt in enumerate(scanned, start=1) if (pt.get("text_source") or "").endswith(":recheck")}
    local = {n: str(pt[LOCAL_TEXT]) for n, pt in enumerate(scanned, start=1) if n not in reread and pt.get(LOCAL_TEXT)}
    doubted = [d for d in diffs if d.significant and (d.page in reread or d.page in local) and d.kind in RECHECK_KINDS]
    if not doubted:
        return
    first = compare([local[n] if n in local else " ".join(str(w[0]) for w in pt["words"]) if n in reread and pt.get("words")
                     else scanned_texts[n - 1] for n, pt in enumerate(scanned, start=1)])
    for d in doubted:
        alone = _only_claude_differs(d, first)
        if alone and d.reason in RECHECK_DOUBT_REASONS:
            d.significant, d.suspect = False, True
        if d.page in local and not alone:
            continue
        note = RECHECK_DISAGREE if alone else RECHECK_STILL
        d.reason = f"{d.reason} — {note}" if d.reason else note


def _name_tokens(employee: MhrEmployee) -> list[str]:
    """თანამშრომლის სახელი და გვარი სიტყვებად: ხელმოწერის გვერდით ხელით დაწერილი საკუთარი სახელი მიწერა არ არის. მხოლოდ სახელი —
    MHR-ის სხვა მონაცემი (ხელფასი, თარიღი) ხელით მიწერილი რომ იყოს, სწორედ ის უნდა გამოჩნდეს."""
    return [w.strip(_PUNCT) for v in (employee.first_name, employee.last_name) if v for w in normalize(str(v)).split() if w.strip(_PUNCT)]


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
        return Check("company", label, co.name if co else doc_company, shown, "skipped", False,
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

    check = _check("company", doc_company, shown, match)
    check.document_value = co.name if co else doc_company
    return check


def document_pages(repo: Repository, row, meta: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """დოკუმენტის გვერდების PageText-ები (ინდექსები 0-დან): სკანის ნაწილს თავისი გვერდების სია აქვს, სხვას — page_start..page_end.
    დადასტურებულად ცარიელი გვერდები (is_blank) შედარებაში არ მონაწილეობს — ცვლილებას ვერ შეიცავს."""
    if meta is None:
        meta = json.loads(row["metadata"]) if row["metadata"] else {}
    texts = repo.load_page_texts(row["job_id"])
    indexes = (meta.get("part") or {}).get("pages") or range(int(row["page_start"]), int(row["page_end"]) + 1)
    out = []
    for i in indexes:
        pt = texts.get(i) or {}
        if pt.get("is_blank"):
            continue
        out.append(pt)
    return out


def _pdf_index(row, page_index: int) -> int:
    """საწყისი ფაილის გვერდი → დოკუმენტის PDF-ის გვერდი (ნაწილის PDF მხოლოდ თავის გვერდებს შეიცავს)."""
    pages = ((json.loads(row["metadata"]) if row["metadata"] else {}).get("part") or {}).get("pages")
    return pages.index(page_index) if pages else page_index - int(row["page_start"])
