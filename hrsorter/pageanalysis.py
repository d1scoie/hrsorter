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


def _label_party(text: str) -> str | None:
    """OCR-ის სიტყვა → რომელი მხარის წარწერაა. მხოლოდ სახელობითი ფორმა („დამსაქმებელი", არა „დამსაქმებელს"/
    „დამსაქმებლის" — ისინი ტექსტშია); ბოლო ასოს OCR-შეცდომა და მოკვეთა (ბეჭედი ფარავს) დასაშვებია."""
    t = text.strip().strip(_LABEL_STRIP)
    if not t or t.endswith("ს"):
        return None
    for party, forms in _PARTY_LABELS.items():
        for f in forms:
            if (len(t) >= len(f) - 3 and f.startswith(t)) or (len(t) == len(f) and t[:-1] == f[:-1]):
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
_STAMP_WORD_MAX_CONF = 0.6
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
    """ბეჭდის შიგნით წაკითხული დაბალი სანდოობის „სიტყვები" (ს/ნ, „არბო 2009", ნაგავი) ტექსტიდან იშლება — ისინი
    ორიგინალთან შედარებისას „ჩამატებულ ტექსტად" ჩანდა. სანდო სიტყვა ბეჭდის ქვეშაც რჩება (ბეჭედი ტექსტს ფარავს)."""
    if not stamps:
        return text
    drop = []
    for t, (l, tp, r, b), conf in words:
        cx, cy = (l + r) / 2, (tp + b) / 2
        if conf < _STAMP_WORD_MAX_CONF and not _KEEP_UNDER_STAMP.search(t)                 and any(x0 <= cx <= x1 and y0 <= cy <= y1 for x0, y0, x1, y1 in stamps):
            drop.append(t)
    if not drop:
        return text
    parts = re.split(r"(\s+)", text)   # მთელი ტოკენები, წაკითხვის რიგით — „2"-ის წაშლამ „2026-10-28" არ უნდა დააზიანოს
    k = 0
    for w in drop:
        for idx in range(k, len(parts), 2):
            if parts[idx] == w:
                parts[idx] = ""
                k = idx + 2
                break
    return "".join(parts)


def party_blobs(arr: np.ndarray, words: list[tuple[str, tuple[int, int, int, int], float]], cell: int, dpi: int) -> list[Blob]:
    """გვერდის ქვედა მესამედის მელანი ხელმოწერის მხარეებისთვის, ბეჭდური ნაწილის გარეშე: ინიშნება ყველა ნამდვილი ქართული
    სიტყვა და ყველა ბეჭდური სიმაღლის სიტყვა (წაუკითხავი წარწერა, გვერდის ნომერი, მკრთალი ტექსტი — სანდოობის
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
    y0 = int(page_h * 0.66)
    part = arr[y0:] & ~_mask_from_px_boxes(arr.shape, printed, pad=2)[y0:]
    if not part.any():
        return []
    part = _erase_rules(part, int(_RULE_MIN_LEN_IN * dpi))
    # min_cell_fill 0.02: კალმის თხელი შტრიხები (ფერადი ნიღაბიდან) ერთ ლაქად შეერთდეს, არა ნატეხებად
    return [Blob(b.x0, b.y0 + y0, b.x1, b.y1 + y0, b.cells, b.ink_px) for b in find_blobs(part, cell, min_cell_fill=0.02)]


_REAL_WORD = re.compile(r"[ა-ჰ]{3,}")


def signature_parties(words: list[tuple[str, tuple[int, int, int, int], float]], blobs: list[Blob], page_w: int,
                      page_h: int, dpi: int) -> dict[str, bool]:
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
        out[party] = any(x0 <= (m.x0 + m.x1) / 2 <= x1 and m.y1 > y0 and m.y0 < y1 for m in marks)
    return out


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
    rgb = np.asarray(doc.render(index, dpi=analysis_dpi, grayscale=False), dtype=np.int16)
    arr_sig = arr | ((rgb.max(axis=2) - rgb.min(axis=2)) > COLOR_INK_CHROMA)
    del rgb
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
    return PageText(
        page_index=index, text=text, text_source=text_source, ocr_quality=round(float(quality), 4),
        is_blank=is_blank, has_marks=has_marks, marks_only=marks_only, ink_ratio=round(ink, 6),
        rotation_applied=int(rotation), deskew_angle=float(angle), provider=provider, model_version=model_version,
        signature_hint=sig.value, handwriting_suspected=handwriting, warnings=warnings,
        signature_parties=(signature_parties(word_boxes, party_blobs(arr_sig, word_boxes, cell, analysis_dpi),
                                             arr.shape[1], arr.shape[0], analysis_dpi) if has_text and word_boxes else {}),
    )
