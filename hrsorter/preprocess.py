"""გვერდის სურათის მომზადება OCR-ისთვის — მხოლოდ სამუშაო ასლზე (PIL grayscale), ორიგინალი არ იცვლება.

ყველა ფუნქცია numpy/PIL-ზეა (opencv არ გვაქვს). კონვენცია მობრუნებისთვის: დაბრუნებული რიცხვი
არის **საათის ისრის მიმართულებით** გრადუსი, რომლითაც სურათი უნდა მოაბრუნო, რომ სწორად დადგეს
(იგივე მნიშვნელობა, რასაც Tesseract OSD-ის `Rotate:` ველი იძლევა). გამოყენება: `apply_rotation(img, deg)`.
"""
from __future__ import annotations

from typing import Callable

import numpy as np
from PIL import Image, ImageFilter, ImageOps

OsdFn = Callable[[Image.Image], int | None]

_ANALYSIS_WIDTH = 600   # დახრის/ორიენტაციის ძებნა შემცირებულ ასლზე (სიჩქარე)
_MIN_INK_FOR_GEOMETRY = 0.0005


def to_gray(img: Image.Image) -> Image.Image:
    return img if img.mode == "L" else img.convert("L")


def ink_ratio(img: Image.Image, threshold: int = 160) -> float:
    """მუქი პიქსელების წილი (0..1): პიქსელი < threshold = „მელანი"."""
    a = np.asarray(to_gray(img), dtype=np.uint8)
    if a.size == 0:
        return 0.0
    return float(np.count_nonzero(a < threshold)) / float(a.size)


def _small_binary(img: Image.Image, threshold: int = 160) -> np.ndarray:
    g = to_gray(img)
    if g.width > _ANALYSIS_WIDTH:
        g = g.resize((_ANALYSIS_WIDTH, max(1, round(g.height * _ANALYSIS_WIDTH / g.width))), Image.BILINEAR)
    return (np.asarray(g, dtype=np.uint8) < threshold).astype(np.uint8)


def _profile_score(binary: np.ndarray) -> float:
    """ტექსტის სტრიქონების „სიმკვეთრე": ჰორიზონტალური პროექციის ვარიაცია / საშუალო."""
    rows = binary.sum(axis=1).astype(np.float64)
    m = rows.mean()
    if m <= 0:
        return 0.0
    return float(rows.var() / m)


def _crop_to_ink(binary: np.ndarray) -> np.ndarray:
    """მინდვრების მოჭრა: მხოლოდ ის სტრიქონები/სვეტები, სადაც მელანი არის."""
    rows = np.flatnonzero(binary.sum(axis=1))
    cols = np.flatnonzero(binary.sum(axis=0))
    if rows.size == 0 or cols.size == 0:
        return binary
    return binary[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]


def _line_cv(binary: np.ndarray) -> float:
    """სტრიქონული პროფილის ვარიაციის კოეფიციენტი (std/mean) მოჭრილ ბლოკზე — ტექსტის ხაზებზე მაღალია."""
    rows = binary.sum(axis=1).astype(np.float64)
    m = rows.mean()
    return float(rows.std() / m) if m > 0 else 0.0


def _rotate_binary(binary: np.ndarray, angle: float) -> np.ndarray:
    im = Image.fromarray(binary * 255)
    r = im.rotate(angle, resample=Image.NEAREST, expand=False, fillcolor=0)
    return (np.asarray(r) > 127).astype(np.uint8)


def deskew(img: Image.Image, max_angle: float = 10.0) -> tuple[Image.Image, float]:
    """დახრის გასწორება პროექციული პროფილის ვარიაციის ძებნით (უხეში 1°, შემდეგ წვრილი 0.1°).

    აბრუნებს (გასწორებული სურათი, გამოყენებული კუთხე გრადუსებში; + = საათის ისრის საწინააღმდეგოდ,
    PIL-ის კონვენცია). |კუთხე| < 0.2° → ორიგინალი უცვლელად, 0.0. ცარიელ გვერდზე არაფერი კეთდება.
    შეზღუდვა: მუშაობს ტექსტურ გვერდებზე; მხოლოდ ნახატის/ბეჭდის გვერდზე შედეგი არასანდოა.
    """
    g = to_gray(img)
    binary = _small_binary(g)
    if binary.mean() < _MIN_INK_FOR_GEOMETRY:
        return g, 0.0
    best_angle, best_score = 0.0, _profile_score(binary)
    coarse = np.arange(-max_angle, max_angle + 0.01, 1.0)
    for a in coarse:
        if abs(a) < 1e-9:
            continue
        s = _profile_score(_rotate_binary(binary, float(a)))
        if s > best_score:
            best_angle, best_score = float(a), s
    for a in np.arange(best_angle - 0.9, best_angle + 0.91, 0.1):
        if abs(a) > max_angle:
            continue
        s = _profile_score(_rotate_binary(binary, float(a)))
        if s > best_score:
            best_angle, best_score = float(a), s
    best_angle = round(best_angle, 2)
    if abs(best_angle) < 0.2:
        return g, 0.0
    out = g.rotate(best_angle, resample=Image.BICUBIC, expand=False, fillcolor=255)
    return out, best_angle


def detect_rotation(img: Image.Image, osd_fn: OsdFn | None = None) -> int:
    """გვერდის ორიენტაცია: 0/90/180/270 (საათის ისრით, რამდენით მოვაბრუნოთ გასწორებისთვის).

    თუ `osd_fn` მოცემულია (Tesseract OSD პროვაიდერიდან) და მართებულ მნიშვნელობას აბრუნებს — ის გამოიყენება.
    სხვა შემთხვევაში პროექციული ევრისტიკა, რომელიც მხოლოდ 0-ს და 90/270-ს არჩევს ერთმანეთისგან:
    ტექსტის სტრიქონები ჰორიზონტალურ პროფილს უფრო „ზოლიანს" ხდის, ვიდრე ვერტიკალურს.
    **180° OSD-ის გარეშე ვერ აღმოჩნდება** (პროფილი სიმეტრიულია) და 90 vs 270 ვერ გაირჩევა — ასეთ
    შემთხვევაში 90 ბრუნდება. ეჭვის შემთხვევაში 0.
    """
    if osd_fn is not None:
        try:
            r = osd_fn(img)
        except Exception:
            r = None
        if r in (0, 90, 180, 270):
            return int(r)
    binary = _small_binary(img)
    if binary.mean() < _MIN_INK_FOR_GEOMETRY:
        return 0
    # მცირე დახრა ხაზებს „აბუნდოვანებს" — ორივე ორიენტაციისთვის ±3°-ში საუკეთესო პროფილი ავიღოთ
    def best_cv(b: np.ndarray) -> float:
        return max(_line_cv(_crop_to_ink(_rotate_binary(b, a) if a else b)) for a in (-3, -2, -1, 0, 1, 2, 3))

    upright = best_cv(binary)
    sideways = best_cv(np.ascontiguousarray(binary.T))
    if sideways > upright * 1.5 and sideways > 0:
        return 90
    return 0


def apply_rotation(img: Image.Image, degrees_cw: int) -> Image.Image:
    """`detect_rotation`-ის შედეგის გამოყენება (PIL აბრუნებს საათის ისრის საწინააღმდეგოდ, ამიტომ -deg)."""
    d = int(degrees_cw) % 360
    if d == 0:
        return img
    return img.rotate(-d, expand=True, fillcolor=255)


def otsu_threshold(img: Image.Image) -> int:
    """Otsu-ს ზღვარი histogram-ზე (numpy)."""
    hist = np.bincount(np.asarray(to_gray(img), dtype=np.uint8).ravel(), minlength=256).astype(np.float64)
    total = hist.sum()
    if total == 0:
        return 128
    w = np.cumsum(hist)
    mu = np.cumsum(hist * np.arange(256))
    mu_t = mu[-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        between = (mu_t * w - mu) ** 2 / (w * (total - w))
    between[~np.isfinite(between)] = -1
    best = np.flatnonzero(between >= between.max() - 1e-9)
    return int(round(float(best.mean())))  # პლატოზე (მაგ. სუფთა შავ-თეთრი) — შუა წერტილი


def binarize(img: Image.Image, threshold: int | None = None) -> Image.Image:
    t = otsu_threshold(img) if threshold is None else threshold
    return to_gray(img).point(lambda v: 255 if v > t else 0)


def enhance(img: Image.Image, *, do_binarize: bool = False) -> Image.Image:
    """OCR-ისთვის მსუბუქი გაუმჯობესება: autocontrast + მედიანური ფილტრი 3×3 (ხმაური); სურვილისამებრ Otsu."""
    g = ImageOps.autocontrast(to_gray(img), cutoff=1)
    g = g.filter(ImageFilter.MedianFilter(3))
    return binarize(g) if do_binarize else g
