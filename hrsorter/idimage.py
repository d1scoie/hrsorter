"""პირადობის მოწმობის/პასპორტის სკანი: დოკუმენტის ამოჭრა ფურცლიდან და სწორი მიმართულებით დაყენება.

ბარათი A4 ფურცელზე პატარაა (ფურცლის მეათედი) და ხშირად გვერდულად ან თავდაყირა დევს. მკითხველს მთელი ფურცელი ეგზავნება,
დაპატარავებული — ბარათის წვრილი ქართული წარწერა ასე ვერ იკითხება („[?]“), მსხვილი ლათინური და მანქანური ზოლი კი იკითხება.
ნამდვილ სკანებზე ამის გამო პირადობა ჩერდებოდა: „გვარი პირადობიდან ვერ წაიკითხა“. აქ: ფურცელზე მკვრივი ბლოკები (ბარათის
თითო მხარე, პასპორტის გაშლილი გვერდი) → თითო ცალკე სურათი, სწორი მიმართულებით და გადიდებული — მკითხველი მხოლოდ დოკუმენტს ხედავს.

მიმართულება: ადგილობრივი OCR ბლოკს ოთხივე მხარეს კითხულობს; სწორია ის, სადაც მანქანური ზოლი („…<<<<“), თარიღები და ველების
წარწერები იკითხება (ნამდვილ სკანებზე სწორი მხარე 4–20 ქულას იღებს, დანარჩენი სამი — 0-ს). ერთ ფურცელზე ბარათის ორივე მხარე
ჩვეულებრივ ერთნაირად დევს, ამიტომ სუსტი ბლოკი (წინა მხარე, ზოლის გარეშე) ფურცლის საერთო პასუხს იღებს.
სურათი მონაცემია; აქ არაფერი იწერება.
"""
from __future__ import annotations

import re
from typing import Callable

import numpy as np
from PIL import Image

from .pageanalysis import find_blobs
from .preprocess import apply_rotation, to_gray

INK_THRESHOLD = 160
_ANALYSIS_DPI = 50
_CELL_IN = 0.16            # ბადის უჯრედი: ბარათის სტრიქონებს შორის მანძილზე დიდი, ორ ბარათს შორის მანძილზე პატარა
_MIN_SIDE_IN = 1.5         # ბარათის მოკლე გვერდი 2.1 დიუმია; ამაზე პატარა ბლოკი ლაქაა ან ფურცლის კიდის ჩრდილი
_MIN_FILL = 0.02           # ბლოკის ფართობში მელნის წილი: სკანერის ცარიელი ჩარჩო (მხოლოდ კიდეები) ბლოკი არ არის
_PAD_IN = 0.08
_INSIDE = 0.6              # ბლოკის ამ წილზე მეტი სხვა ბლოკშია — ცალკე დოკუმენტი არ არის (პასპორტის ფოტოიანი გვერდი გაშლილში)
MAX_BLOCKS = 4             # ერთ ფურცელზე: ბარათის ორი მხარე ან პასპორტი (+ მარაგი); მეტი — ეს პირადობის სკანი არ არის
READER_LONG_SIDE = 1800    # ბარათი 300 dpi-ზე ~1000 პიქსელია: მკითხველისთვის დიდდება, წვრილი ასო რომ გაარჩიოს
ROTATIONS = (0, 90, 180, 270)

_MRZ_CHARS = re.compile(r"[A-Z0-9<]{20,}")
_MIN_FILLERS = 3           # ამაზე ნაკლები „<“ შემთხვევითი ნაგავია
_MAX_FILLER_SCORE = 30
_FIELD_WORD = re.compile(r"surname|given|name|birth|expiry|issue|personal|passport|identity|authority|justice|citizen|signature|"
                         r"გვარი|სახელი|თარიღი|სქესი|პირადი|მოქალაქე|საქართველო|ხელმოწერა|ორგანო", re.I)
_DATE = re.compile(r"(?<!\d)\d{2}[.\s]\d{2}[.\s]\d{4}(?!\d)")
_PN = re.compile(r"(?<!\d)\d{11}(?!\d)")

Reader = Callable[[Image.Image], str]
Box = tuple[int, int, int, int]


def _inside(a: Box, b: Box) -> float:
    """`a`-ს ფართობის რა წილია `b`-ში."""
    w, h = min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])
    return (w * h) / max(1, (a[2] - a[0]) * (a[3] - a[1])) if w > 0 and h > 0 else 0.0


def blocks(img: Image.Image, dpi: int) -> list[Box]:
    """ფურცელზე დოკუმენტის ბლოკები (l, t, r, b — `img`-ის პიქსელებში), ზემოდან ქვემოთ და მარცხნიდან მარჯვნივ. მთლიანად
    ტექსტით სავსე ფურცელი ერთი ბლოკია (ტექსტი მინდვრების გარეშე). ცარიელი სია — ფურცელი ცარიელია ან ნაწილები ზედმეტად ბევრია."""
    g = to_gray(img)
    scale = _ANALYSIS_DPI / float(dpi)
    small = g.resize((max(1, round(g.width * scale)), max(1, round(g.height * scale))), Image.BILINEAR)
    ink = np.asarray(small, dtype=np.uint8) < INK_THRESHOLD
    min_side = _MIN_SIDE_IN * _ANALYSIS_DPI
    found = [(b.x0, b.y0, b.x1, b.y1) for b in find_blobs(ink, max(2, round(_CELL_IN * _ANALYSIS_DPI)))
             if min(b.w, b.h) >= min_side and b.fill >= _MIN_FILL]
    found.sort(key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)
    kept: list[Box] = []
    for b in found:
        if not any(_inside(b, k) >= _INSIDE for k in kept):
            kept.append(b)
    if len(kept) > MAX_BLOCKS:
        return []
    pad = _PAD_IN * _ANALYSIS_DPI
    kept.sort(key=lambda b: (round(b[1] / (2 * min_side)), b[0]))
    return [(max(0, int((l - pad) / scale)), max(0, int((t - pad) / scale)),
             min(img.width, int((r + pad) / scale)), min(img.height, int((b + pad) / scale))) for l, t, r, b in kept]


def upright_score(text: str) -> int:
    """რამდენად ჰგავს წაკითხული ტექსტი სწორად დაყენებულ პირადობას: მანქანური ზოლის ხაზები, თარიღები, პირადი ნომერი, წარწერები."""
    text = text or ""
    score = 0
    for ln in text.splitlines():
        flat = re.sub(r"\s", "", ln).upper()
        if flat.count("<") >= 3 and _MRZ_CHARS.search(flat):
            score += 6
    # ზოლის შემავსებელი „<“ ცალკეც ითვლება: ჭრელფონიან ბარათზე ადგილობრივი OCR ზოლს ნაწყვეტებად კითხულობს („…KLI.<<<<£<0<<“),
    # თავდაყირა ზოლში კი იგივე ნიშანი „>“-ად იკითხება — ეს ორი მხარე ამით ირჩევა
    fill, upside_down = text.count("<"), text.count(">")
    if fill >= _MIN_FILLERS and fill > 2 * upside_down:
        score += min(fill, _MAX_FILLER_SCORE)
    return score + 2 * len(_DATE.findall(text)) + 3 * len(_PN.findall(text)) + len(_FIELD_WORD.findall(text))


def _scores(img: Image.Image, read: Reader) -> dict[int, int]:
    out: dict[int, int] = {}
    for rot in ROTATIONS:
        try:
            out[rot] = upright_score(read(apply_rotation(img, rot)))
        except Exception:   # ადგილობრივი OCR-ის შეცდომა მიმართულების გამოცნობაზე უარია, არა დამუშავების გაჩერება
            out[rot] = 0
    return out


def _clear_best(scores: dict[int, int]) -> int | None:
    """მობრუნება, რომელმაც სხვებს აშკარად აჯობა; None — არცერთ მხარეს არაფერი იკითხება, ან ორი მხარე ტოლია."""
    top = max(scores.values(), default=0)
    best = [r for r, s in scores.items() if s == top]
    return best[0] if top > 0 and len(best) == 1 else None


def rotations(images: list[Image.Image], read: Reader | None) -> list[int]:
    """ყოველი ბლოკის მობრუნება (საათის ისრით), რომ სწორად დადგეს. ბლოკი, რომელზეც არაფერი იკითხება, ფურცლის სხვა ბლოკების
    საერთო პასუხს იღებს; `read`-ის გარეშე, ან როცა ფურცელზე არაფერი იკითხება — 0 (სურათი უცვლელია)."""
    if read is None:
        return [0] * len(images)
    per_block = [_scores(im, read) for im in images]
    page = _clear_best({r: sum(s[r] for s in per_block) for r in ROTATIONS})
    return [own if (own := _clear_best(s)) is not None else (page or 0) for s in per_block]


def enlarge(img: Image.Image, long_side: int = READER_LONG_SIDE) -> Image.Image:
    """პატარა ბლოკი მკითხველისთვის დიდდება (დიდი უცვლელია — დაპატარავება მკითხველის საქმეა)."""
    m = max(img.size)
    if m >= long_side or m == 0:
        return img
    k = long_side / m
    return img.resize((round(img.width * k), round(img.height * k)), Image.LANCZOS)


def document_images(img: Image.Image, dpi: int, read: Reader | None = None) -> list[Image.Image]:
    """ფურცლის სურათი → დოკუმენტის ბლოკები ცალ-ცალკე, სწორი მიმართულებით, გადიდებული. ბლოკი ვერ გამოიყო → მთელი ფურცელი,
    სწორი მიმართულებით."""
    crops = [img.crop(box) for box in blocks(img, dpi)] or [img]
    return [enlarge(apply_rotation(c, rot)) for c, rot in zip(crops, rotations(crops, read))]
