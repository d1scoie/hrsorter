import random

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from hrsorter import preprocess as pp

FONT = "C:/Windows/Fonts/sylfaen.ttf"
WORDS = "შრომითი ხელშეკრულება დამსაქმებელი შპს მაგალითი დასაქმებული ნინო ბერიძე ბრძანება შვებულება დირექტორი".split()


@pytest.fixture(scope="module")
def text_page() -> Image.Image:
    rng = random.Random(1)
    font = ImageFont.truetype(FONT, 28)
    img = Image.new("L", (1240, 1754), 255)
    d = ImageDraw.Draw(img)
    for i in range(32):
        d.text((120, 150 + i * 45), " ".join(rng.choice(WORDS) for _ in range(rng.randint(3, 9))), font=font, fill=0)
    return img


def test_ink_ratio_extremes():
    assert pp.ink_ratio(Image.new("L", (100, 100), 255)) == 0.0
    assert pp.ink_ratio(Image.new("L", (100, 100), 0)) == 1.0
    half = Image.new("L", (100, 100), 255)
    ImageDraw.Draw(half).rectangle([0, 0, 49, 99], fill=0)
    assert abs(pp.ink_ratio(half) - 0.5) < 0.01


def test_ink_ratio_accepts_rgb():
    assert pp.ink_ratio(Image.new("RGB", (10, 10), (0, 0, 0))) == 1.0


@pytest.mark.parametrize("angle", [3.0, -4.5, 7.0])
def test_deskew_recovers_angle(text_page, angle):
    skewed = text_page.rotate(angle, resample=Image.BICUBIC, fillcolor=255)
    out, found = pp.deskew(skewed)
    assert abs(found + angle) <= 0.5          # found არის გამოსწორების კუთხე (= -დახრა)
    assert out.size == skewed.size and out.mode == "L"


def test_deskew_straight_page_unchanged(text_page):
    out, found = pp.deskew(text_page)
    assert found == 0.0 and out is not None


def test_deskew_blank_noop():
    blank = Image.new("L", (400, 600), 255)
    _, found = pp.deskew(blank)
    assert found == 0.0


def test_detect_rotation_heuristic(text_page):
    assert pp.detect_rotation(text_page) == 0
    assert pp.detect_rotation(text_page.rotate(90, expand=True, fillcolor=255)) == 90
    # 270 არ გაირჩევა 90-ისგან OSD-ის გარეშე — დოკუმენტირებული შეზღუდვა: 90 ბრუნდება
    assert pp.detect_rotation(text_page.rotate(-90, expand=True, fillcolor=255)) == 90
    # 180 OSD-ის გარეშე ვერ აღმოჩნდება
    assert pp.detect_rotation(text_page.rotate(180)) == 0
    assert pp.detect_rotation(Image.new("L", (300, 400), 255)) == 0


def test_detect_rotation_prefers_osd(text_page):
    assert pp.detect_rotation(text_page, osd_fn=lambda im: 180) == 180
    assert pp.detect_rotation(text_page, osd_fn=lambda im: None) == 0
    assert pp.detect_rotation(text_page, osd_fn=lambda im: 45) == 0        # უმართებულო მნიშვნელობა იგნორირდება

    def boom(im):
        raise RuntimeError("osd crashed")
    assert pp.detect_rotation(text_page, osd_fn=boom) == 0


def test_apply_rotation_swaps_dimensions():
    img = Image.new("L", (100, 200), 255)
    assert pp.apply_rotation(img, 90).size == (200, 100)
    assert pp.apply_rotation(img, 180).size == (100, 200)
    assert pp.apply_rotation(img, 0) is img


def test_enhance_and_binarize(text_page):
    e = pp.enhance(text_page)
    assert e.mode == "L" and e.size == text_page.size
    b = pp.enhance(text_page, do_binarize=True)
    assert set(np.unique(np.asarray(b)).tolist()) <= {0, 255}
    assert 0 <= pp.otsu_threshold(text_page) <= 255
