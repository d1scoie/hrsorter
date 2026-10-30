import random

import pytest

from hrsorter.config import make_config
from hrsorter.models import SignatureStatus
from hrsorter.ocr import OcrResult, build_ocr_chain
from hrsorter.ocr.chain import OcrChain
from hrsorter.pageanalysis import analyze_page
from hrsorter.pdfio import PdfDocument
from tools import synthetic as S


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    """0: განცხადება (ტექსტური ფენა + ხელმოწერა); 1: ბრძანება ხელმოწერის გარეშე; 2: ბრძანება; 3: მხოლოდ ნიშნები;
    4: ცარიელი; 5: image-only ჯარიმა; 6: ხელნაწერი პ/ნ-ით მიღების ბრძანება."""
    rng = random.Random(3)
    e1 = S.employee_by_id("E001")
    docs = [
        S.make_document("application", rng, employee=e1, signature=True, stamp=False),
        S.make_document("hire_order", rng, employee=e1, signature=False, stamp=False, director_pn=False),
        S.make_document("dismissal_order", rng, employee=e1, marks_page=True, signature=True, stamp=True),
        S.blank_doc(),
    ]
    img_only = S.make_document("fine", rng, employee=e1)
    img_only.render_opts = {"image_only": True, "dpi": 150}
    docs.append(img_only)
    docs.append(S.make_document("hire_order", rng, employee=e1, handwritten_pn=True, signature=True, stamp=False))
    path = tmp_path_factory.mktemp("pa") / "sample.pdf"
    gt = S.render_pdf(docs, path, seed=3)
    assert gt["pages"] == 7
    return path, gt


@pytest.fixture(scope="module")
def cfg():
    return make_config()


def test_text_layer_page(sample, cfg):
    path, _ = sample
    with PdfDocument(path) as doc:
        pt = analyze_page(doc, 0, cfg, None)
    assert pt.text_source == "text_layer" and pt.ocr_quality >= 0.6
    assert "განცხადება" in pt.text and "01001000001" in pt.text
    assert not pt.is_blank and not pt.marks_only
    assert pt.has_marks and pt.signature_hint == SignatureStatus.PRESENT.value
    assert pt.rotation_applied == 0 and pt.deskew_angle == 0.0 and pt.warnings == []


def test_text_page_without_extra_ink_is_not_detected(sample, cfg):
    path, _ = sample
    with PdfDocument(path) as doc:
        pt = analyze_page(doc, 1, cfg, None)
    assert pt.text_source == "text_layer"
    assert pt.has_marks is False and pt.signature_hint == SignatureStatus.NOT_DETECTED.value
    assert pt.handwriting_suspected is False


def test_marks_only_page_is_not_blank(sample, cfg):
    path, gt = sample
    assert gt["page_tags"]["3"] == ["marks_only"]
    with PdfDocument(path) as doc:
        pt = analyze_page(doc, 3, cfg, None)
    assert pt.is_blank is False and pt.has_marks and pt.marks_only
    assert pt.text_source == "none" and pt.text.strip() == ""
    assert pt.ink_ratio > cfg.thresholds["blank_ink_ratio"]


def test_blank_page(sample, cfg):
    path, gt = sample
    assert gt["blank_pages"] == [4]
    with PdfDocument(path) as doc:
        pt = analyze_page(doc, 4, cfg, None)
    assert pt.is_blank and not pt.has_marks and not pt.marks_only
    assert pt.text == "" and pt.text_source == "none" and pt.ocr_quality == 0
    assert pt.signature_hint == SignatureStatus.NOT_APPLICABLE.value
    assert "ocr_unavailable" not in pt.warnings


def test_image_only_page_without_ocr(sample, cfg):
    path, _ = sample
    chain = build_ocr_chain(make_config({"ocr": {"tesseract_cmd": "C:/nonexistent/tesseract.exe"}}))
    assert chain.available() is False
    with PdfDocument(path) as doc:
        pt_none = analyze_page(doc, 5, cfg, None)
        pt_chain = analyze_page(doc, 5, cfg, chain)
    for pt in (pt_none, pt_chain):
        assert pt.text_source == "none" and pt.ocr_quality == 0 and pt.text == ""
        assert "ocr_unavailable" in pt.warnings
        assert pt.is_blank is False and pt.marks_only is False   # წაუკითხავი ტექსტის გვერდი ≠ ნიშნების გვერდი
        assert "page_unreadable" in pt.warnings
        assert pt.signature_hint == SignatureStatus.UNCERTAIN.value


def test_handwritten_field_suspected(sample, cfg):
    path, gt = sample
    assert "handwritten" in gt["documents"][-1]["tags"]
    with PdfDocument(path) as doc:
        pt = analyze_page(doc, 6, cfg, None)
    assert pt.text_source == "text_layer"
    assert "01001000001" not in pt.text          # პ/ნ ტექსტურ ფენაში არ არის
    assert pt.handwriting_suspected is True


class FakeOcr:
    name = "fake"
    model_version = "fake-1"
    is_online = False

    def __init__(self):
        self.sizes = []

    def available(self):
        return True

    def recognize(self, image, lang):
        self.sizes.append(image.size)
        return OcrResult(text="ბრძანება დაჯარიმების შესახებ გიორგი კაპანაძე ჯარიმა 100 ლარი დირექტორი",
                         mean_confidence=0.8, words=[])


def test_image_only_page_with_fake_ocr(sample):
    path, _ = sample
    fake = FakeOcr()
    chain = OcrChain(fake, None, "local", 0.35, "kat+eng")
    cfg = make_config({"processing": {"dpi": 150, "deskew": False}})
    with PdfDocument(path) as doc:
        pt = analyze_page(doc, 5, cfg, chain)
    assert pt.text_source == "ocr:fake" and pt.provider == "fake" and pt.model_version == "fake-1"
    assert pt.ocr_quality > 0.5 and "ჯარიმა" in pt.text
    assert fake.sizes and fake.sizes[0][0] > 1000            # 150 dpi-ზე რენდერი
    assert "ocr_unavailable" not in pt.warnings


def test_signature_merged_with_stamp_at_page_bottom_is_present():
    """რეალური სკანი (არბო 2009, 20 გვ.): ხელმოწერები + ბეჭედი ერთ ლაქად — 2.64×1.84in, გვერდის ქვედა ნაწილში.
    1.6in-ზე მაღალი ლაქა ადრე „გაურკვეველი“ იყო → MHR-ის შედარებაში ცრუ „ნიშნები ხელმოწერის ზონის გარეთ“."""
    from hrsorter.pageanalysis import Blob, _signature_found
    dpi, page_h = 100, 1170

    def blob(w_in, h_in, y_center_in, fill=0.08):
        w, h, cy = int(w_in * dpi), int(h_in * dpi), int(y_center_in * dpi)
        return Blob(100, cy - h // 2, 100 + w, cy + h // 2, cells=50, ink_px=int(w * h * fill))

    stamp_and_sig = blob(2.64, 1.84, 10.2)
    assert _signature_found([stamp_and_sig], [], page_h, alnum=1500, dpi=dpi)
    assert _signature_found([blob(2.80, 1.44, 10.2)], [], page_h, alnum=1500, dpi=dpi)      # ჩვეულებრივი გვერდი
    # იგივე მაღალი ლაქა ტექსტის შუაში — არა ხელმოწერა (ხელით ჩანაწერი უნდა დარჩეს საეჭვოდ)
    assert not _signature_found([blob(2.64, 1.84, 5.0)], [], page_h, alnum=1500, dpi=dpi)
    # ძალიან მაღალი (2.5in) ქვემოთაც — არა
    assert not _signature_found([blob(3.2, 2.5, 10.0)], [], page_h, alnum=1500, dpi=dpi)
    # მრგვალი ბეჭედი ხელმოწერის გარეშე — ხელმოწერად არ ითვლება
    assert not _signature_found([blob(1.5, 1.5, 10.2)], [], page_h, alnum=1500, dpi=dpi)


def test_signature_parties_each_side_checked_separately():
    """რეალური სკანი (მე-6 გვერდი): მარცხნივ „დამსაქმებელი“ + ბეჭედი, მარჯვნივ „დასაქმებული“ — ხაზი ცარიელი.
    ადრე „ხელმოწერა არის“ ეწერა (ბეჭედი საკმარისი იყო) — თითოეული მხარე ცალკე უნდა შემოწმდეს."""
    from hrsorter.pageanalysis import Blob, signature_parties
    dpi, w, h = 100, 827, 1170
    words = [("დამსაქმებელი", (110, 1075, 230, 1092), 0.9), ("დასაქმებული", (560, 1075, 670, 1092), 0.9),
             ("დამსაქმებელი", (100, 300, 220, 315), 0.9)]          # ტექსტში ნახსენები — ზედა ნაწილში, არ ითვლება
    stamp = Blob(160, 990, 310, 1140, cells=200, ink_px=5000)    # ბეჭედი მარცხნივ, ოდნავ მარჯვნივ გადაწეული
    assert signature_parties(words, [stamp], w, h, dpi) == {"employer": True, "employee": False}
    employee_sig = Blob(590, 1080, 700, 1120, cells=30, ink_px=900)
    assert signature_parties(words, [stamp, employee_sig], w, h, dpi) == {"employer": True, "employee": True}
    # მხოლოდ ერთი წარწერა წაიკითხა → მეორე მხარე სარკისებურ ზონაში მოწმდება (აქ ცარიელია)
    assert signature_parties([("სტაჟიორი", (560, 1075, 650, 1092), 0.9)], [employee_sig], w, h, dpi) ==         {"employee": True, "employer": False}
    assert signature_parties([], [stamp], w, h, dpi) == {}       # წარწერები ვერ მოიძებნა — ძველი შემოწმება
    # წვრილი ლაქა (გვერდის ნომერი + ხაზის ნატეხი) და ბეჭდური სიმაღლის ზოლი (წაუკითხავი წარწერა) — არა ხელმოწერა
    page_no = Blob(700, 1100, 740, 1124, cells=6, ink_px=120)
    label_ink = Blob(560, 1075, 670, 1092, cells=24, ink_px=600)
    assert signature_parties(words, [page_no, label_ink], w, h, dpi) == {"employer": False, "employee": False}
    # მარცხენა ბეჭედი მარჯვენა ზონაში შემოდის, მაგრამ ცენტრი მარცხნივაა → მარჯვენა მხარეს არ ეთვლება
    wide_stamp = Blob(160, 990, 500, 1140, cells=400, ink_px=9000)
    assert signature_parties(words, [wide_stamp], w, h, dpi) == {"employer": True, "employee": False}


def test_signature_parties_signature_crossing_line_and_unreadable_label():
    """რეალური სკანი: ხელმოწერა ხაზს კვეთს (ერთი განიერი ლაქა — ადრე „ხაზგასმად“ იყრებოდა); ბეჭედი „დამსაქმებელს“
    ფარავს (OCR ვერ კითხულობს) — მისი ზონა მეორე წარწერის სარკით განისაზღვრება."""
    from hrsorter.pageanalysis import Blob, signature_parties
    dpi, w, h = 100, 827, 1170
    employee_label = ("დასაქმებული", (560, 1075, 670, 1092), 0.9)
    sig_on_line = Blob(540, 1090, 760, 1140, cells=40, ink_px=1500)   # 2.2in × 0.5in — ხაზი + ხელმოწერა
    bare_line = Blob(540, 1118, 760, 1121, cells=20, ink_px=600)       # მხოლოდ ხაზი — ხელმოწერა არ არის
    stamp = Blob(160, 990, 310, 1140, cells=200, ink_px=5000)
    assert signature_parties([employee_label], [stamp, sig_on_line], w, h, dpi) == {"employer": True, "employee": True}
    assert signature_parties([employee_label], [bare_line], w, h, dpi) == {"employer": False, "employee": False}


def test_signature_parties_label_mentioned_in_text_above_is_ignored():
    """რეალური სკანი (52-ე გვ.): „დამსაქმებელი“ წარწერა წაუკითხავია, ტექსტში კი „დამსაქმებელს“/„დამსაქმებელი“ ქვედა
    მეოთხედში ხვდება — სხვა სტრიქონზე მყოფი ნახსენები წარწერად არ უნდა ჩაითვალოს (ზონა ტექსტზე გადადიოდა)."""
    from hrsorter.pageanalysis import Blob, _label_party, signature_parties
    assert _label_party("დამსაქმებელი") == "employer" and _label_party("„დამსაქმებელი“:") == "employer"
    assert _label_party("დამსაქმებე") == "employer" and _label_party("დამსაქმებელღ") == "employer"   # მოკვეთა, OCR-შეცდომა
    assert _label_party("დასაქმებული") == "employee" and _label_party(",,სტაჟიორი”") == "employee"
    for word in ("დამსაქმებელს", "დამსაქმებლის", "დასაქმებულის", "სტაჟიორს", "დასაქმება", "ხელშეკრულება", ""):
        assert _label_party(word) is None, word
    dpi, w, h = 100, 827, 1170
    words = [("დამსაქმებელი", (460, 1000, 560, 1016), 0.9),      # ტექსტის სტრიქონი წარწერებზე ზემოთ
             ("დასაქმებული", (560, 1075, 670, 1092), 0.9)]
    text_ink = Blob(400, 1000, 740, 1040, cells=60, ink_px=2000)   # მკრთალი, უნიღბავი ტექსტი იმავე სტრიქონზე
    assert signature_parties(words, [text_ink], w, h, dpi) == {"employee": False, "employer": False}


def test_party_blobs_keeps_signature_drops_printed_label_rule_and_page_number():
    """ქვედა მესამედის მელანი: ბეჭდური წარწერა (სიტყვის სიმაღლის OCR-ყუთი, სანდოობის მიუხედავად), ხელმოწერის ხაზი და
    გვერდის ნომერი ქრება; ხელმოწერა (OCR-მა ნაგვის „სიტყვად“ წაიკითხა, მაღალი ყუთი) რჩება."""
    import numpy as np
    from hrsorter.pageanalysis import party_blobs
    dpi, w, h, cell = 100, 827, 1170, 8
    arr = np.zeros((h, w), dtype=bool)
    arr[1075:1092, 110:230] = True                      # „დამსაქმებელი“ წარწერა (ბეჭდური)
    arr[1075:1092, 560:670] = True                      # „დასაქმებული“ წარწერა — OCR-მა ვერ წაიკითხა (ნაგავი, დაბალი სანდოობა)
    arr[1118:1120, 100:300] = True                      # ხელმოწერის ხაზი მარცხნივ (2in)
    arr[1118:1120, 550:750] = True                      # ხელმოწერის ხაზი მარჯვნივ, გვერდის ნომერთან ერთად
    arr[1108:1120, 742:752] = True                      # გვერდის ნომერი (ხაზს ეკვრის)
    rng = np.random.default_rng(1)
    ys = (1095 + 30 * np.abs(np.sin(np.linspace(0, 12, 400)))).astype(int)       # ხელმოწერის ტალღა 1.4in × 0.3in
    for i, y in enumerate(ys):
        arr[y:y + 3, 570 + i // 3:573 + i // 3] = True
    words = [("დამსაქმებელი", (110, 1075, 230, 1092), 0.95), ("ება“,", (560, 1075, 670, 1092), 0.09),
             ("თკირქსელიძი>", (568, 1094, 706, 1128), 0.0)]
    blobs = party_blobs(arr, words, cell, dpi)
    big = [b for b in blobs if b.cells >= 10 and b.h >= 0.2 * dpi]
    assert len(big) == 1 and 560 <= big[0].x0 and big[0].x1 <= 720 and big[0].y0 >= 1088, [(b.x0, b.y0, b.x1, b.y1, b.cells) for b in blobs]
    assert all(b.x1 <= 300 or b.x0 >= 560 for b in blobs)                                   # ხაზი და წარწერები ამოიშალა
    assert not [b for b in blobs if b.x0 >= 730 and b.cells >= 10]                           # გვერდის ნომერი წვრილი დარჩა


def test_drop_stamp_words_keeps_dates_and_personal_numbers():
    """ბეჭდის შიგნით წაკითხული ნაგავი იშლება, თარიღი/პ.ნ. და სანდო სიტყვა — არა (რეალური სკანი: ბეჭედი რეკვიზიტებს ფარავდა
    და პირადი ნომერი იკარგებოდა → „რამდენიმე თანამშრომელი")."""
    from hrsorter.pageanalysis import drop_stamp_words
    stamp = [(100, 100, 300, 300)]
    words = [("სტაჟიორი", (10, 10, 80, 26), 0.95), ("2989", (150, 150, 190, 170), 0.3), ("9527ვგამიას", (150, 180, 250, 200), 0.1),
             ("2026-10-28", (150, 210, 230, 226), 0.5), ("პ/6:01001000205", (150, 240, 290, 256), 0.55), ("არბო", (150, 270, 190, 286), 0.9)]
    text = "სტაჟიორი 2989 9527ვგამიას 2026-10-28 პ/6:01001000205 არბო"
    assert drop_stamp_words(text, words, stamp).split() == ["სტაჟიორი", "2026-10-28", "პ/6:01001000205", "არბო"]
    assert drop_stamp_words(text, words, []) == text
    # ნაწილობრივი დამთხვევა არ ითვლება: „2"-ის წაშლა „2026-10-28"-ს არ ეხება, „კა" — „დაკავებული"-ს
    words2 = [("2", (150, 150, 160, 166), 0.3), ("კა", (150, 170, 170, 186), 0.5)]
    assert drop_stamp_words("2026-10-28 დაკავებული 2 კა ბოლო", words2, stamp) == "2026-10-28 დაკავებული   ბოლო"
