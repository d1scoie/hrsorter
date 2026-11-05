"""ერთი გვერდის ანალიზი: ტექსტი (ფენა ან OCR), ცარიელი/ნიშნების გვერდი, ხელმოწერის მინიშნება.

ნაკადი (`analyze_page`):
  1. ტექსტური ფენა → `textquality.score`; თუ ≥ thresholds.text_layer_min_quality → `text_source="text_layer"`.
  2. სხვაგვარად რენდერი processing.dpi-ზე → auto_rotate / deskew (კონფიგურაციით) → enhance → OCR ჯაჭვი
     (`text_source="ocr:<provider>"`). OCR მიუწვდომელია → `"none"`, `ocr_quality=0`, warning `ocr_unavailable`.
  3. ყოველთვის დაბალი გარჩევადობის ანალიზის სურათი (processing.analysis_dpi) → მელნის წილი და ნიშნები.

ნიშნების ევრისტიკა (კონსერვატიული — სიზუსტე უპირატესია სრულყოფილებასთან):
  * ტექსტის ნიღაბი: ტექსტური ფენის სიმბოლოების bbox-ები (pypdfium2) ან OCR სიტყვების bbox-ები, 2px padding.
  * ნიღბის გარეთ დარჩენილი მელანი იყოფა ~2მმ უჯრებად; მომიჯნავე „შემელნებული" უჯრები → ლაქა (blob).
    ლაქა ითვლება ნიშნად, თუ არც თხელი ხაზია (ჩარჩო/ხაზგასმა), არც ტექსტის სტრიქონის ფორმისაა
    (ძალიან განიერი) და მელნის სიმკვრივე ბრტყელ ჩარჩოს არ ჰგავს.
  * `has_marks` — ≥1 ასეთი ლაქა; `marks_only` — ლაქები არის, ტექსტი კი < 30 ასო/ციფრი.
  * `is_blank` — მელანი < thresholds.blank_ink_ratio **და** ტექსტი არ არის **და** ლაქა არ არის.
  * `signature_hint`: `present` — ხელმოწერისმაგვარი ლაქა („ხელმოწერა"/„დირექტორი"/„____" მინიშნების
    გვერდით ან ტექსტიანი გვერდის ქვედა მესამედში); `not_detected` — ტექსტიანი გვერდი ზედმეტი მელნის გარეშე;
    `not_applicable` — ცარიელი გვერდი; სხვა შემთხვევაში `uncertain`.
  * `handwriting_suspected` — სუსტი ევრისტიკა: ჩანაწერისმაგვარი ლაქა ლეიბლის („პ/ნ", „თარიღი") მარჯვნივ,
    ≥3 ასეთი ლაქა ტექსტის ზონაში, ან OCR-ის სიტყვების ≥40% ძალიან დაბალი სანდოობით ნიშნებიან გვერდზე.
    ხელნაწერის კითხვას არ ცდილობს.

ცნობილი შეზღუდვები: ლოგო/სურათი ლაქად ჩანს (→ has_marks, signature uncertain); ტექსტის გარეშე გვერდზე
(OCR არ არის) ბეჭდური ტექსტი ნიშნებისგან მხოლოდ ფორმით გაირჩევა — თუ მელანი ≥1% არის ან ≥2 განიერი
ტექსტურ-ბლოკისმაგვარი ლაქაა, გვერდი „წაუკითხავად" ითვლება (warning `page_unreadable`), არა ნიშნების გვერდად; PDF-ის /Rotate ≠ 0
გვერდზე ტექსტური ფენის ნიღაბი არ იშენება.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import numpy as np
import pypdfium2 as pdfium
from PIL import Image

from . import textquality
from .config import Config
from .models import PageText, SignatureStatus
from .ocr.chain import OcrChain
from .pdfio import PdfDocument
from .preprocess import apply_rotation, deskew, detect_rotation, enhance, ink_ratio

log = logging.getLogger(__name__)

INK_THRESHOLD = 160
COLOR_INK_CHROMA = 40      # max(R,G,B) − min(R,G,B) ამაზე მეტი = ფერადი მელანი (ლურჯი კალამი, ბეჭედი); ქაღალდი/ტექსტი ≈ 0
_SIG_CUE = re.compile(r"ხელმოწერ|ხელმომწერ|დირექტორ|_{3,}|/\s*$", re.MULTILINE)
_HAND_CUE = re.compile(r"პ/ნ|პირადი\s+ნომერი|თარიღი|სახელი|გვარი")
_MIN_TEXT_ALNUM = 5        # ამდენზე ნაკლები ასო/ციფრი = „ტექსტი არ არის"
_MARKS_ONLY_MAX_ALNUM = 30
_UNREADABLE_INK = 0.01     # ტექსტის გარეშე გვერდზე ამდენი მელანი = სავარაუდოდ წაუკითხავი ბეჭდური ტექსტი


@dataclass
class Blob:
    x0: int
    y0: int
    x1: int
    y1: int
    cells: int
    ink_px: int

    @property
    def w(self) -> int:
        return self.x1 - self.x0

    @property
    def h(self) -> int:
        return self.y1 - self.y0

    @property
    def fill(self) -> float:
        area = max(1, self.w * self.h)
        return self.ink_px / area

    @property
    def aspect(self) -> float:
        return self.w / max(1, self.h)

    def center(self) -> tuple[float, float]:
        return (self.x0 + self.x1) / 2.0, (self.y0 + self.y1) / 2.0


def _alnum_count(text: str) -> int:
    return sum(1 for c in text if c.isalnum())


# --- ტექსტის ნიღაბი -------------------------------------------------------------
def _text_layer_boxes(doc: PdfDocument, index: int) -> tuple[list[tuple[float, float, float, float]], str, float] | None:
    """სიმბოლოების bbox-ები PDF პუნქტებში (l, b, r, t), ტექსტი ინდექსების მიხედვით, გვერდის სიმაღლე. None თუ ვერ."""
    pdf = getattr(doc, "_pdfium", None)
    own = False
    if pdf is None:
        pdf = pdfium.PdfDocument(doc.path)
        own = True
    try:
        page = pdf[index]
        try:
            if page.get_rotation() != 0:
                return None
            _, h = page.get_size()
            tp = page.get_textpage()
            try:
                n = tp.count_chars()
                boxes = [tp.get_charbox(i, loose=False) for i in range(n)]
                text = tp.get_text_range(0, n) if n else ""
            finally:
                tp.close()
            return boxes, text, float(h)
        finally:
            page.close()
    except Exception as e:  # pdfium-ის შიდა შეცდომა — ნიღბის გარეშე ვაგრძელებთ
        log.debug("char boxes unavailable for page %s: %s", index, e)
        return None
    finally:
        if own:
            pdf.close()


def _mask_from_px_boxes(shape: tuple[int, int], boxes: list[tuple[int, int, int, int]], pad: int) -> np.ndarray:
    mask = np.zeros(shape, dtype=bool)
    hh, ww = shape
    for l, t, r, b in boxes:
        x0, y0 = max(0, l - pad), max(0, t - pad)
        x1, y1 = min(ww, r + pad + 1), min(hh, b + pad + 1)
        if x1 > x0 and y1 > y0:
            mask[y0:y1, x0:x1] = True
    return mask


def _pt_boxes_to_px(boxes: list[tuple[float, float, float, float]], page_h: float, scale: float) -> list[tuple[int, int, int, int]]:
    out = []
    for l, b, r, t in boxes:
        if r <= l or t <= b:
            continue
        out.append((int(l * scale), int((page_h - t) * scale), int(np.ceil(r * scale)), int(np.ceil((page_h - b) * scale))))
    return out


# --- ლაქები ------------------------------------------------------------------------
def find_blobs(ink: np.ndarray, cell: int, min_cell_fill: float = 0.05) -> list[Blob]:
    """ბინარული მელნის ნიღაბი → უჯრედოვანი ბადე → 8-მეზობლიანი კომპონენტები (მცირე ბადეზე BFS)."""
    hh, ww = ink.shape
    gh, gw = (hh + cell - 1) // cell, (ww + cell - 1) // cell
    counts = np.zeros((gh, gw), dtype=np.int32)
    ys, xs = np.nonzero(ink)
    if ys.size == 0:
        return []
    np.add.at(counts, (ys // cell, xs // cell), 1)
    active = counts >= max(1, int(min_cell_fill * cell * cell))
    seen = np.zeros_like(active, dtype=bool)
    blobs: list[Blob] = []
    for gy, gx in zip(*np.nonzero(active)):
        if seen[gy, gx]:
            continue
        stack = [(int(gy), int(gx))]
        seen[gy, gx] = True
        cells = []
        while stack:
            cy, cx = stack.pop()
            cells.append((cy, cx))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < gh and 0 <= nx < gw and active[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
        cy_arr = np.array([c[0] for c in cells])
        cx_arr = np.array([c[1] for c in cells])
        y0, y1 = int(cy_arr.min()) * cell, min(hh, (int(cy_arr.max()) + 1) * cell)
        x0, x1 = int(cx_arr.min()) * cell, min(ww, (int(cx_arr.max()) + 1) * cell)
        ink_px = int(sum(counts[c] for c in cells))
        blobs.append(Blob(x0, y0, x1, y1, len(cells), ink_px))
    return blobs


def _mark_blobs(blobs: list[Blob], dpi: int) -> list[Blob]:
    """ნიშნად მისაღები ლაქები: არც თხელი ხაზი, არც ტექსტის სტრიქონი, არც ბრტყელი ჩარჩო."""
    min_w, min_h = 0.2 * dpi, 0.12 * dpi
    out = []
    for b in blobs:
        if b.cells < 4 or b.w < min_w or b.h < min_h:
            continue
        if b.aspect > 8 or b.aspect < 1 / 8:   # ხაზგასმა / ტექსტის სტრიქონი / ვერტიკალური ჩარჩო
            continue
        if b.fill < 0.02:                     # ცხრილის ჩარჩო, თხელი კონტური
            continue
        out.append(b)
    return out


def _signature_like(b: Blob, dpi: int, max_h_in: float = 1.6) -> bool:
    return (0.35 * dpi <= b.w <= 3.5 * dpi and 0.12 * dpi <= b.h <= max_h_in * dpi
            and b.aspect >= 1.2 and 0.02 <= b.fill <= 0.5)


# ხელმოწერები + ბეჭედი გვერდის ქვედა ნაწილში ხშირად ერთ, უფრო მაღალ ლაქად ერწყმის (რეალური სკანი: 1.76–1.84in)
_SIG_BLOCK_MAX_H_IN = 2.2


def _signature_found(marks: list[Blob], cue_boxes: list[tuple[int, int, int, int]], page_h_px: int,
                     alnum: int, dpi: int) -> bool:
    for b in marks:
        bottom = b.center()[1] > 0.65 * page_h_px and alnum >= 100
        if _signature_like(b, dpi) and (bottom or any(_near_cue(b, c, dpi) for c in cue_boxes)):
            return True
        if bottom and _signature_like(b, dpi, _SIG_BLOCK_MAX_H_IN):
            return True
    return False


def _entry_like(b: Blob, dpi: int) -> bool:
    """ხელით შევსებული ველის ფორმა: დაბალი, განიერი ლაქა."""
    return b.aspect >= 2.0 and 0.08 * dpi <= b.h <= 0.6 * dpi and b.w >= 0.4 * dpi


def _near_cue(b: Blob, cue: tuple[int, int, int, int], dpi: int) -> bool:
    cx, cy = b.center()
    l, t, r, bt = cue
    return (l - 0.5 * dpi) <= cx <= (r + 4.0 * dpi) and (t - 1.0 * dpi) <= cy <= (bt + 1.2 * dpi)


def _right_of_cue(b: Blob, cue: tuple[int, int, int, int], dpi: int,
                  text_boxes: list[tuple[int, int, int, int]]) -> bool:
    """ლაქა იწყება ლეიბლის („პ/ნ:", „თარიღი:") მარჯვნივ, იმავე სტრიქონზე და მათ შორის ბეჭდური ტექსტი არ არის
    — ხელით შევსებული ველი. (ბეჭდური მნიშვნელობის შემდეგ მდგომი ხელმოწერა ასე არ ითვლება.)"""
    l, t, r, bt = cue
    cy = b.center()[1]
    if not ((r - 0.1 * dpi) <= b.x0 <= (r + 0.6 * dpi) and (t - 0.15 * dpi) <= cy <= (bt + 0.15 * dpi)):
        return False
    for tl, tt, tr, tb in text_boxes:
        if tl >= r + 2 and tl < b.x0 and t - 0.1 * dpi <= (tt + tb) / 2 <= bt + 0.1 * dpi:
            return False
    return True


# ხელმოწერის ხაზის წარწერები გვერდის ბოლოში (ხელშეკრულება ყოველ გვერდზე: „დამსაქმებელი"   „დასაქმებული"/„სტაჟიორი")
_PARTY_LABELS = {"employer": ("დამსაქმებელი",), "employee": ("დასაქმებული", "სტაჟიორი")}
_LABEL_STRIP = "„“”\"'(),.:;"
_PARTY_MIN_CELLS = 10          # ხელმოწერა/ბეჭედი: ≥10 უჯრედი (გვერდის ნომერი, ლაქა, ხაზის ნატეხი — ნაკლები)
_PARTY_MIN_H_IN = 0.2          # ბეჭდური სიტყვის სიმაღლის ლაქა (წაუკითხავი წარწერა) — არა ხელმოწერა
_TEXT_WORD_MAX_H_IN = 0.22     # OCR-ის სიტყვა ამ სიმაღლემდე ბეჭდური ტექსტია (ხელმოწერის „სიტყვები" უფრო მაღალია)
_RULE_MIN_LEN_IN = 0.3         # ამაზე გრძელი თხელი ჰორიზონტალური ხაზი — ხელმოწერის ადგილის ხაზი, არა მელანი
_SAME_LINE_IN = 0.15
_BLOCK_ABOVE_IN = 1.0          # მარცხენა სვეტის დასაყრდენი მარჯვენაზე ამდენით მაღლა შეიძლება იყოს …
_BLOCK_BELOW_IN = 3.0          # … და ამდენით დაბლა („პ/ნ" ზემოთაა, „დირექტორი" კი ქვემოთ)
_BLOCK_COLUMN_GAP_IN = 0.4     # მარჯვენა სვეტის სიტყვის წინ ამაზე დიდი ცარიელი ადგილი → სვეტებია, არა ტექსტის სტრიქონი
_BLOCK_BOTTOM_SHARE = 0.9      # გვერდის ძირის ბლანკი (კომპანიის რეკვიზიტები) ზონაში არ შედის
_BLOCK_SPLIT_IN = 0.5          # მარჯვენა სვეტი იწყება დასაყრდენი სიტყვიდან ამდენით მარცხნივ
_BLOCK_ZONE_IN = 1.8           # ვებ-გვერდზე მოსანიშნი ადგილის სიმაღლე, როცა ხელმოწერა აკლია
_DIRECTOR_FORM = "დირექტორი"
_BLOCK_LEFT_WORDS = {"შპს", "ს/ნ", "ს/კ"}                  # კომპანიის სვეტი, თუ წარწერა ვერ წაიკითხა (Tesseract მთელ სტრიქონს ხანდახან ტოვებს)
_BLOCK_RIGHT_WORD = re.compile(r"(?:პ/ნ|3/6|3/ნ|პ/6|\d{11})")   # თანამშრომლის სვეტი


def _matches_form(t: str, form: str) -> bool:
    return (len(t) >= len(form) - 3 and form.startswith(t)) or (len(t) == len(form) and t[:-1] == form[:-1])


def _label_party(text: str) -> str | None:
    """OCR-ის სიტყვა → რომელი მხარის წარწერაა. მხოლოდ სახელობითი ფორმა („დამსაქმებელი", არა „დამსაქმებელს"/
    „დამსაქმებლის" — ისინი ტექსტშია); ბოლო ასოს OCR-შეცდომა და მოკვეთა (ბეჭედი ფარავს) დასაშვებია."""
    t = text.strip().strip(_LABEL_STRIP)
    if not t or t.endswith("ს"):
        return None
    for party, forms in _PARTY_LABELS.items():
        if any(_matches_form(t, f) for f in forms):
            return party
    return None


def _erase_rules(ink: np.ndarray, min_len: int) -> np.ndarray:
    """გრძელი ჰორიზონტალური ხაზები (ხელმოწერის ადგილი, ცხრილის საზღვარი) — ბეჭდური ელემენტი, არა მელანი.
    გვერდის ნომერთან შეერთებული ხაზი სხვანაირად „ხელმოწერის" ზომის ლაქა გამოდის."""
    out = ink.copy()
    pad = np.zeros((ink.shape[0], 1), dtype=np.int8)
    d = np.diff(np.concatenate([pad, ink.astype(np.int8), pad], axis=1), axis=1)
    for y in range(ink.shape[0]):
        starts, ends = np.nonzero(d[y] == 1)[0], np.nonzero(d[y] == -1)[0]
        for s0, e0 in zip(starts, ends):
            if e0 - s0 >= min_len:
                out[y, s0:e0] = False
    return out


_STAMP_MIN_IN = (1.0, 0.8)     # ბეჭდის ლაქა: სიგანე × სიმაღლე ინჩებში მინიმუმ
_STAMP_MAX_W_IN = 4.0          # ამაზე განიერი — ცხრილის ჩარჩო, არა ბეჭედი
_STAMP_MIN_CELLS = 60
_STAMP_REAL_MIN_CONF = 0.5   # ბეჭდის ქვეშ რჩება ამაზე სანდო ნამდვილი ქართული სიტყვა …
_STAMP_CLEAN_MIN_CONF = 0.6  # … და ამაზე სანდო სუფთა რიცხვი (≥4 ციფრი), ელ-ფოსტა, „ს/ნ“-ის ტიპის ეტიკეტი (ბეჭედი ცხრილს ფარავს)
_STAMP_CLEAN = re.compile(r"^(?:\d{4,}|[ა-ჰ]{1,2}/[ა-ჰ]{1,2}:?|[a-z0-9._-]+@[a-z0-9.-]+\.[a-z]{2,})$")
_STAMP_STRIP = "„“\"'(),;"
_KEEP_UNDER_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}|\d{10,}")   # თარიღი, პირადი ნომერი — ბეჭდის ქვეშაც რჩება


def stamp_boxes(arr: np.ndarray, words: list[tuple[str, tuple[int, int, int, int], float]], cell: int,
                dpi: int) -> list[tuple[int, int, int, int]]:
    """ბეჭდის ზომის ლაქები მთელ გვერდზე (ფერადი მელნის ჩათვლით): ჯერ სანდო ნამდვილი სიტყვები ინიღბება (აბზაცი
    ლაქად არ ჩაითვალოს), დარჩენილი დიდი, არა-ზედმეტად-განიერი ლაქა — ბეჭედია (მრგვალი ბეჭედი, „დამტკიცებულია" ბეჭედი)."""
    real = [box for text, box, conf in words if conf >= 0.5 and _REAL_WORD.search(text)]
    part = arr & ~_mask_from_px_boxes(arr.shape, real, pad=2)
    if not part.any():
        return []
    return [(b.x0, b.y0, b.x1, b.y1) for b in find_blobs(part, cell)
            if _STAMP_MIN_IN[0] * dpi <= b.w <= _STAMP_MAX_W_IN * dpi and b.h >= _STAMP_MIN_IN[1] * dpi
            and b.cells >= _STAMP_MIN_CELLS]


def drop_stamp_words(text: str, words: list[tuple[str, tuple[int, int, int, int], float]],
                     stamps: list[tuple[int, int, int, int]]) -> str:
    """ბეჭდის/ხელმოწერის ლაქის შიგნით წაკითხული „სიტყვები" (ს/ნ, „არბო 2009", „2099/3", „/7", „LI", ნაგავი) ტექსტიდან
    იშლება — ისინი ორიგინალთან შედარებისას „ჩამატებულ ტექსტად"/„ციფრების სხვაობად" ჩანდა, სანდოობის მიუხედავად
    (Tesseract ბეჭდის ციფრებს ხშირად ≥0.6-ით კითხულობს). რჩება მხოლოდ სანდო ნამდვილი ქართული სიტყვა (ბეჭედი ტექსტს
    ფარავს), თარიღი და პირადი ნომერი."""
    if not stamps:
        return text
    drop: set[int] = set()
    for i, (t, (l, tp, r, b), conf) in enumerate(words):
        cx, cy = (l + r) / 2, (tp + b) / 2
        real = ((conf >= _STAMP_REAL_MIN_CONF and _REAL_WORD.search(t))
                or (conf >= _STAMP_CLEAN_MIN_CONF and _STAMP_CLEAN.match(t.strip(_STAMP_STRIP))))
        if not real and not _KEEP_UNDER_STAMP.search(t) and any(x0 <= cx <= x1 and y0 <= cy <= y1 for x0, y0, x1, y1 in stamps):
            drop.add(i)
    if not drop:
        return text
    # მთელი ტოკენები, წაკითხვის რიგით — „2"-ის წაშლამ „2026-10-28" არ უნდა დააზიანოს; ყველა სიტყვა თანმიმდევრობით
    # მოწმდება (ინდექსით, არა მნიშვნელობით), რომ ადრინდელი იგივე ტოკენი (სანდო „2009" ბეჭდის მკრთალი „2009"-ის წინ) არ წაიშალოს
    parts = re.split(r"(\s+)", text)
    k = 0
    for i, (w, _box, _conf) in enumerate(words):
        for idx in range(k, len(parts), 2):
            if parts[idx] == w:
                if i in drop:
                    parts[idx] = ""
                k = idx + 2
                break
    return "".join(parts)


def party_blobs(arr: np.ndarray, words: list[tuple[str, tuple[int, int, int, int], float]], cell: int, dpi: int,
                top: int | None = None) -> list[Blob]:
    """გვერდის ქვედა მესამედის (ან `top`-იდან ქვემოთ) მელანი ხელმოწერის მხარეებისთვის, ბეჭდური ნაწილის გარეშე: ინიშნება ყველა
    ნამდვილი ქართული სიტყვა და ყველა ბეჭდური სიმაღლის სიტყვა (წაუკითხავი წარწერა, გვერდის ნომერი, მკრთალი ტექსტი — სანდოობის
    მიუხედავად); ხელმოწერა/ბეჭედი Tesseract-ს უფრო მაღალ ნაგვის „სიტყვებად" უჩანს და რჩება. ხაზებიც იშლება."""
    max_h = _TEXT_WORD_MAX_H_IN * dpi
    page_h = arr.shape[0]
    label_bottoms = [b[3] for t, b, _c in words if _label_party(t) and (b[1] + b[3]) / 2 > 0.75 * page_h]
    line_y = max(label_bottoms) + 0.08 * dpi if label_bottoms else None   # წარწერის ხაზის ქვედა ზღვარი
    printed = []
    for text, box, conf in words:
        real = conf >= 0.5 and _REAL_WORD.search(text)
        number = len(text) <= 3 and text.strip(_LABEL_STRIP).isdigit()           # გვერდის ნომერი
        text_height = box[3] - box[1] <= max_h and (line_y is None or box[3] <= line_y)   # ტექსტი ხაზამდე/ხაზზე
        if real or number or text_height:
            printed.append(box)
    y0 = int(page_h * 0.66) if top is None else max(0, top)
    part = arr[y0:] & ~_mask_from_px_boxes(arr.shape, printed, pad=2)[y0:]
    if not part.any():
        return []
    part = _erase_rules(part, int(_RULE_MIN_LEN_IN * dpi))
    # min_cell_fill 0.02: კალმის თხელი შტრიხები (ფერადი ნიღაბიდან) ერთ ლაქად შეერთდეს, არა ნატეხებად
    return [Blob(b.x0, b.y0 + y0, b.x1, b.y1 + y0, b.cells, b.ink_px) for b in find_blobs(part, cell, min_cell_fill=0.02)]


_REAL_WORD = re.compile(r"[ა-ჰ]{3,}")
MARKS_KEPT = 12                # ამდენი ყველაზე დიდი ნიშანი ინახება ვებ-გვერდზე მოსანიშნად


def signature_parties(words: list[tuple[str, tuple[int, int, int, int], float]], blobs: list[Blob], page_w: int,
                      page_h: int, dpi: int, zones: dict[str, tuple[float, float, float, float]] | None = None) -> dict[str, bool]:
    """თითოეული მხარის ხელმოწერა ცალკე. გვერდის ქვედა მეოთხედში ყველაზე დაბალი წარწერა („დამსაქმებელი",
    „დასაქმებული"/„სტაჟიორი"); ორივე სხვადასხვა სტრიქონზე → ზედა ტექსტის ნახსენებია, რჩება ქვედა. ერთი წარწერა ვერ
    წაიკითხა (ბეჭედი ფარავს, მკრთალია) → მისი ადგილი მეორის სარკეა. ზონა: წარწერის თავიდან 1in ქვემოთ, მარცხნივ 0.5in,
    მარჯვნივ 1.2in, მეორე წარწერამდე. ნიშანი = `blobs`-ის ლაქა, რომელიც ხელმოწერის ზომისაა (≥0.2in, ≥10 უჯრედი) და
    რომლის ცენტრიც ზონაშია (მარცხენა ბეჭედი მარჯვენა მხარეს არ ეთვლება). ვერც ერთი წარწერა → {} (ზოგადი შემოწმება)."""
    labels: dict[str, tuple[int, int, int, int]] = {}
    for text, box, _conf in words:
        party = _label_party(text)
        if party and (box[1] + box[3]) / 2 > 0.75 * page_h and (party not in labels or box[3] > labels[party][3]):
            labels[party] = box
    if len(labels) == 2 and abs(labels["employer"][1] - labels["employee"][1]) > _SAME_LINE_IN * dpi:
        drop = min(labels, key=lambda k: labels[k][3])
        del labels[drop]
    if len(labels) == 1:
        (found, (l, t, r, b)), = labels.items()
        other = "employer" if found == "employee" else "employee"
        cx = (l + r) / 2
        if cx > 0.55 * page_w or cx < 0.45 * page_w:
            labels[other] = (page_w - r, t, page_w - l, b)
    marks = [m for m in blobs if m.h >= _PARTY_MIN_H_IN * dpi and m.cells >= _PARTY_MIN_CELLS]
    out: dict[str, bool] = {}
    for party, (l, t, r, b) in labels.items():
        x0, x1 = max(0, l - 0.5 * dpi), min(page_w, r + 1.2 * dpi)
        for other, (ol, _ot, orr, _ob) in labels.items():
            if other != party and ol > r:
                x1 = min(x1, ol - 0.1 * dpi)
            elif other != party and orr < l:
                x0 = max(x0, orr + 0.1 * dpi)
        y0, y1 = t - 0.1 * dpi, b + 1.0 * dpi
        mine = [m for m in marks if x0 <= (m.x0 + m.x1) / 2 <= x1 and m.y1 > y0 and m.y0 < y1]
        out[party] = bool(mine)
        if zones is not None:
            zones[party] = _biggest_box(mine) or (x0, y0, x1, y1)
    return out


def _biggest_box(blobs: list[Blob]) -> tuple[float, float, float, float] | None:
    b = max(blobs, key=lambda m: m.cells, default=None)
    return (b.x0, b.y0, b.x1, b.y1) if b else None


def signature_block(words: list[tuple[str, tuple[int, int, int, int], float]], page_w: int,
                    dpi: int) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]] | None:
    """ხელმოწერის ორსვეტიანი ბლოკი გვერდის შუაში (ინსტრუქცია, წესები, დანართი). მარცხენა სვეტის დასაყრდენი: „დამსაქმებელი",
    „დირექტორი", „შპს", „ს/ნ"; მარჯვენასი: „სტაჟიორი"/„დასაქმებული", „პ/ნ", პირადი ნომერი — და მის წინ ცარიელი ადგილი (ტექსტში
    ნახსენები სიტყვა სტრიქონის შუაშია). რამდენიმე წყვილიდან — ქვედა."""
    def strip(t: str) -> str:
        return t.strip().strip(_LABEL_STRIP)
    lefts = [b for t, b, _c in words if (b[0] + b[2]) / 2 < 0.45 * page_w and (
        _label_party(t) == "employer" or _matches_form(strip(t), _DIRECTOR_FORM) or strip(t) in _BLOCK_LEFT_WORDS)]
    rights = [b for t, b, _c in words if (b[0] + b[2]) / 2 > 0.45 * page_w and (
        _label_party(t) == "employee" or _BLOCK_RIGHT_WORD.fullmatch(strip(t)))]
    best = None
    for right in rights:
        before = [b[2] for _t, b, _c in words if b[2] <= right[0] and b[1] < right[3] and b[3] > right[1]]
        if before and right[0] - max(before) < _BLOCK_COLUMN_GAP_IN * dpi:
            continue
        for left in lefts:
            if not -_BLOCK_ABOVE_IN * dpi <= left[1] - right[1] <= _BLOCK_BELOW_IN * dpi:
                continue
            if best is None or max(left[1], right[1]) > max(best[0][1], best[1][1]):
                best = (left, right)
    return best


def block_parties(block: tuple[tuple[int, int, int, int], tuple[int, int, int, int]], blobs: list[Blob], page_h: int,
                  dpi: int, zones: dict[str, tuple[float, float, float, float]] | None = None,
                  words: list[tuple[str, tuple[int, int, int, int], float]] | None = None, page_w: int = 0) -> dict[str, bool]:
    """ბლოკის თითო სვეტში ცალკე: დასაყრდენი სიტყვიდან ქვემოთ ხელმოწერის ზომის ლაქა (ბეჭედი დამსაქმებლის მხარეს ითვლება)."""
    left, right = block
    y0, y1 = min(left[1], right[1]) - 0.1 * dpi, _BLOCK_BOTTOM_SHARE * page_h
    split = right[0] - _BLOCK_SPLIT_IN * dpi
    marks = [m for m in blobs if m.h >= _PARTY_MIN_H_IN * dpi and m.cells >= _PARTY_MIN_CELLS and y0 <= (m.y0 + m.y1) / 2 <= y1]
    left_marks = [m for m in marks if (m.x0 + m.x1) / 2 < split]
    right_marks = [m for m in marks if (m.x0 + m.x1) / 2 >= split]
    if zones is not None:   # აკლია → სვეტის ადგილი ბოლო ბეჭდური სტრიქონის ქვემოთ, სადაც ხელმოწერა უნდა ყოფილიყო
        for party, mine, (x0, x1) in (("employer", left_marks, (0.0, split)), ("employee", right_marks, (split, float(page_w or split * 2)))):
            printed = [b[3] for _t, b, _c in words or [] if x0 <= (b[0] + b[2]) / 2 < x1 and y0 <= b[1] and b[3] <= y1]
            top = max(printed, default=y0)
            other = _biggest_box(left_marks if party == "employee" else right_marks)
            if other:   # ხელმოწერები ერთ დონეზეა: მეორე მხარის ხელმოწერის გასწვრივ
                top = max(top, other[1])
            zones[party] = _biggest_box(mine) or (x0, top, x1, min(top + _BLOCK_ZONE_IN * dpi, y1))
    return {"employer": bool(left_marks), "employee": bool(right_marks)}


def page_parties(arr: np.ndarray, words: list[tuple[str, tuple[int, int, int, int], float]], cell: int, dpi: int,
                 zones: dict[str, tuple[float, float, float, float]] | None = None) -> dict[str, bool]:
    """გვერდის ქვედა სტრიქონის წარწერები (ხელშეკრულება), ან — თუ იქ არ არის — ორსვეტიანი ბლოკი გვერდის შუაში."""
    page_h, page_w = arr.shape[:2]
    parties = signature_parties(words, party_blobs(arr, words, cell, dpi), page_w, page_h, dpi, zones)
    if parties:
        return parties
    block = signature_block(words, page_w, dpi)
    if block is None:
        return {}
    return block_parties(block, party_blobs(arr, words, cell, dpi, top=min(block[0][1], block[1][1])), page_h, dpi, zones, words, page_w)


def _permille(box, page_w: int, page_h: int) -> list[int]:
    x0, y0, x1, y1 = box
    return [round(1000 * x0 / page_w), round(1000 * y0 / page_h), round(1000 * x1 / page_w), round(1000 * y1 / page_h)]


def _text_block_like(b: Blob, dpi: int) -> bool:
    """ბეჭდური ტექსტის სტრიქონის/აბზაცის ფორმა: განიერი ბლოკი (ხელმოწერა/ბეჭედი ასეთი არ არის)."""
    return b.aspect >= 4 and b.w >= 2.0 * dpi


def _boxes_for_cues(regex: re.Pattern[str], idx_text: str, boxes_pt: list, page_h: float, scale: float) -> list[tuple[int, int, int, int]]:
    out = []
    for m in regex.finditer(idx_text):
        span = [boxes_pt[i] for i in range(m.start(), min(m.end(), len(boxes_pt)))]
        px = _pt_boxes_to_px(span, page_h, scale)
        if px:
            out.append((min(p[0] for p in px), min(p[1] for p in px), max(p[2] for p in px), max(p[3] for p in px)))
    return out


# --- მთავარი -----------------------------------------------------------------------
def analyze_page(doc: PdfDocument, index: int, cfg: Config, chain: OcrChain | None) -> PageText:
    th = cfg.thresholds
    proc = cfg.data["processing"]
    dpi = int(proc.get("dpi", 300))
    analysis_dpi = int(proc.get("analysis_dpi", 100))
    warnings: list[str] = []

    layer = doc.text_layer(index)
    layer_q = textquality.score(layer)

    small = doc.render(index, dpi=analysis_dpi)
    ink = ink_ratio(small, INK_THRESHOLD)

    text, text_source, quality = "", "none", 0.0
    rotation, angle, provider, model_version = 0, 0.0, "", ""
    text_boxes_px: list[tuple[int, int, int, int]] | None = None   # ანალიზის სურათის პიქსელებში
    cue_boxes: list[tuple[int, int, int, int]] = []
    hand_cues: list[tuple[int, int, int, int]] = []
    ocr_words: list[dict[str, Any]] = []
    word_boxes: list[tuple[str, tuple[int, int, int, int], float]] = []   # OCR სიტყვები ანალიზის პიქსელებში + სანდოობა
    mask_available = False

    if layer_q >= float(th.get("text_layer_min_quality", 0.6)):
        text, text_source, quality = layer, "text_layer", layer_q
        tl = _text_layer_boxes(doc, index)
        if tl is not None:
            boxes_pt, idx_text, page_h = tl
            scale = analysis_dpi / 72.0
            text_boxes_px = _pt_boxes_to_px(boxes_pt, page_h, scale)
            mask_available = True
            cue_boxes = _boxes_for_cues(_SIG_CUE, idx_text, boxes_pt, page_h, scale)
            hand_cues = _boxes_for_cues(_HAND_CUE, idx_text, boxes_pt, page_h, scale)
        else:
            warnings.append("mark_detection_unavailable")
    else:
        if layer.strip():
            warnings.append("text_layer_rejected")
        blank_candidate = ink < float(th.get("blank_ink_ratio", 0.002))
        if blank_candidate:
            pass  # ცარიელ გვერდზე OCR აზრს მოკლებულია
        elif chain is None or not chain.available():
            warnings.append("ocr_unavailable")
        else:
            img = doc.render(index, dpi=dpi)
            try:
                if proc.get("auto_rotate", True):
                    osd_fn = chain.osd if getattr(chain.local, "has_osd", lambda: False)() else None
                    rotation = detect_rotation(img, osd_fn=osd_fn)
                    if rotation:
                        img = apply_rotation(img, rotation)
                        small = apply_rotation(small, rotation)
                if proc.get("deskew", True):
                    img, angle = deskew(img)
                    if angle:
                        small = small.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=255)
                res = chain.recognize(enhance(img), chain.lang)
            finally:
                img.close()
            warnings.extend(res.warnings)
            if res.ok and res.result is not None and res.provider is not None:
                text, quality = res.result.text, res.quality
                text_source, provider = f"ocr:{res.provider.name}", res.provider.name
                model_version = res.provider.model_version
                ocr_words = res.result.words
                s = analysis_dpi / dpi
                text_boxes_px = [(int(l * s), int(t * s), int(np.ceil(r * s)), int(np.ceil(b * s)))
                                 for (l, t, r, b) in (w["bbox"] for w in ocr_words)]
                mask_available = True
                for w in ocr_words:
                    l, t, r, b = w["bbox"]
                    box = (int(l * s), int(t * s), int(np.ceil(r * s)), int(np.ceil(b * s)))
                    word_boxes.append((w["text"], box, float(w.get("conf") or 0.0)))
                    if _SIG_CUE.search(w["text"]):
                        cue_boxes.append(box)
                    if _HAND_CUE.search(w["text"]):
                        hand_cues.append(box)
            else:
                warnings.append("ocr_failed")

    # --- ნიშნები ანალიზის სურათზე --------------------------------------------------
    arr = np.asarray(small, dtype=np.uint8) < INK_THRESHOLD
    small.close()
    # ხელმოწერის მხარეებისთვის ფერადი მელანიც (ლურჯი კალამი): მკრთალი ლურჯი ნაცრისფრად ზღურბლს ვერ გადის, ქრომა კი
    # ქაღალდსა და შავ ტექსტზე 0-ია — რეალურ სკანზე სტაჟიორის ხელმოწერა 45 პიქსელიდან ~300-მდე გაიზარდა
    color = doc.render(index, dpi=analysis_dpi, grayscale=False)
    if rotation:   # იგივე მიმართულება, რაც `small`-ს: გვერდით შებრუნებულ ფურცელზე სხვაგვარად ზომები არ ემთხვევა
        color = apply_rotation(color, rotation)
    if angle:
        color = color.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=(255, 255, 255))
    rgb = np.asarray(color, dtype=np.int16)
    arr_sig = arr | ((rgb.max(axis=2) - rgb.min(axis=2)) > COLOR_INK_CHROMA)
    del rgb
    color.close()
    alnum = _alnum_count(text)
    cell = max(4, round(analysis_dpi * 0.08))
    if mask_available:
        leftover = arr & ~_mask_from_px_boxes(arr.shape, text_boxes_px or [], pad=2)
    else:
        leftover = arr
    all_blobs = find_blobs(leftover, cell) if leftover.any() else []
    marks = _mark_blobs(all_blobs, analysis_dpi)

    has_text = alnum >= _MIN_TEXT_ALNUM
    text_blocks = sum(1 for b in all_blobs if _text_block_like(b, analysis_dpi))
    unreadable = (not has_text) and (not mask_available) and (ink >= _UNREADABLE_INK or text_blocks >= 2)
    if unreadable:
        warnings.append("page_unreadable")
        marks = []
    has_marks = bool(marks)
    is_blank = (ink < float(th.get("blank_ink_ratio", 0.002))) and not has_text and not has_marks
    marks_only = has_marks and alnum < _MARKS_ONLY_MAX_ALNUM

    page_h_px = arr.shape[0]
    if is_blank:
        sig = SignatureStatus.NOT_APPLICABLE
    elif not has_text:
        sig = SignatureStatus.UNCERTAIN
    elif not mask_available:
        sig = SignatureStatus.UNCERTAIN
    elif not has_marks:
        sig = SignatureStatus.NOT_DETECTED
    else:
        sig = (SignatureStatus.PRESENT if _signature_found(marks, cue_boxes, page_h_px, alnum, analysis_dpi)
               else SignatureStatus.UNCERTAIN)

    handwriting = False
    if has_marks and has_text and mask_available:
        entries = [b for b in marks if _entry_like(b, analysis_dpi)]
        if any(_right_of_cue(b, c, analysis_dpi, text_boxes_px or []) for b in entries for c in hand_cues):
            handwriting = True   # ლეიბლის („პ/ნ:", „თარიღი:") გვერდით ხელით ჩაწერილი ველი
        elif len([b for b in entries if not any(_near_cue(b, c, analysis_dpi) for c in cue_boxes)]) >= 3:
            handwriting = True
        confs = [w["conf"] for w in ocr_words if w.get("conf") is not None]
        if not handwriting and len(confs) >= 8 and sum(1 for c in confs if c < 0.3) / len(confs) >= 0.4:
            handwriting = True

    if word_boxes and text:
        text = drop_stamp_words(text, word_boxes, stamp_boxes(arr_sig, word_boxes, cell, analysis_dpi))
    zones: dict[str, tuple[float, float, float, float]] = {}
    parties = page_parties(arr_sig, word_boxes, cell, analysis_dpi, zones) if has_text and word_boxes else {}
    page_w_px = arr.shape[1]
    return PageText(
        page_index=index, text=text, text_source=text_source, ocr_quality=round(float(quality), 4),
        is_blank=is_blank, has_marks=has_marks, marks_only=marks_only, ink_ratio=round(ink, 6),
        rotation_applied=int(rotation), deskew_angle=float(angle), provider=provider, model_version=model_version,
        signature_hint=sig.value, handwriting_suspected=handwriting, warnings=warnings,
        signature_parties=parties,
        words=[[t, *_permille(b, page_w_px, page_h_px)] for t, b, _c in word_boxes],
        marks=[_permille((b.x0, b.y0, b.x1, b.y1), page_w_px, page_h_px) for b in sorted(marks, key=lambda m: -m.cells)[:MARKS_KEPT]],
        signature_boxes={p: _permille(z, page_w_px, page_h_px) for p, z in zones.items()},
    )
