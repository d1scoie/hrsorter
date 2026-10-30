import json
import random

import pytest

from hrsorter.pdfio import PdfDocument, count_pages, validate_pdf
from hrsorter.preprocess import detect_rotation
from tools import synthetic as S

TYPES = ["contract", "contract_annex", "fine", "vacation", "application", "hire_order", "dismissal_order",
         "explanation", "unknown"]
TITLE_HINT = {"contract": "შრომითი ხელშეკრულება", "contract_annex": "დანართი", "fine": "დაჯარიმების",
              "vacation": "შვებულებ", "application": "განცხადება", "hire_order": "მიღების შესახებ",
              "dismissal_order": "გათავისუფლების", "explanation": "ახსნა-განმარტება", "unknown": ""}


@pytest.fixture(scope="module")
def batch(tmp_path_factory):
    path = tmp_path_factory.mktemp("synth") / "batch.pdf"
    gt = S.make_batch(path, n_docs=20, seed=1)
    return path, gt


@pytest.mark.parametrize("type_id", TYPES)
def test_make_document_each_type(type_id):
    rng = random.Random(5)
    d = S.make_document(type_id, rng)
    assert d.type_id == type_id and len(d.pages) >= 1
    assert TITLE_HINT[type_id] in d.text()
    if d.employee is not None:
        assert d.employee["personal_number"] in d.text() or "handwritten" in d.tags


def test_make_document_overrides():
    rng = random.Random(9)
    e = S.employee_by_id("E004")
    d = S.make_document("contract", rng, employee=e, n_pages=3, page_markers=True, date_style="words", director_pn=True)
    assert len(d.pages) == 3 and d.pages[1].footer == "გვერდი 2/3"
    assert "multi_page" in d.tags and "director_pn" in d.tags
    assert d.document_date is not None and d.year == int(d.document_date[:4])
    assert "00001000004" in d.pages[0].text()
    assert d.employee["last_name"] not in d.pages[1].text()      # მე-2 გვერდზე სახელი არ მეორდება
    assert d.fields["contract_start"] and "contract_end" in d.fields
    assert S.make_document("fine", rng, date_style="missing").document_date is None
    yo = S.make_document("fine", rng, date_style="year_only")
    assert yo.document_date is None and yo.year is not None and "year_only" in yo.tags
    v = S.make_document("vacation", rng, variant="order", extra_employees=[S.employee_by_id("E002")])
    assert "multiple_employees" in v.tags and len(v.fields["employees"]) == 2 and v.fields["vacation_days"] > 0
    f = S.make_document("fine", rng)
    assert f.fields["fine_currency"] == "GEL" and f.fields["fine_amount"] > 0 and f.fields["fine_reason"]
    hc = S.make_document("hire_order", rng, company_conflict=True, company_id="acme")
    assert "company_conflict" in hc.tags and "ბეტა" in hc.text()


def test_batch_page_accounting(batch):
    path, gt = batch
    assert gt["pages"] == count_pages(path) == validate_pdf(path).page_count
    covered = set(gt["blank_pages"])
    prev_end = -1
    for d in gt["documents"]:
        assert d["start"] <= d["end"] and d["start"] > prev_end
        prev_end = d["end"]
        pages = set(range(d["start"], d["end"] + 1))
        assert not (pages & covered)
        covered |= pages
    assert covered == set(range(gt["pages"]))
    assert len(gt["documents"]) >= 20


def test_batch_covers_spec_cases(batch):
    _, gt = batch
    tags = {t for d in gt["documents"] for t in d["tags"]}
    for t in ["multi_page", "page_markers", "director_pn", "date_missing", "year_only", "multiple_employees",
              "marks_only_page", "low_quality", "image_only", "handwritten", "rescan_duplicate", "company_conflict"]:
        assert t in tags, t
    assert any(d["type_id"] == "unknown" for d in gt["documents"])
    assert len(gt["blank_pages"]) >= 1 and len(gt["duplicates"]) >= 1
    i, j = gt["duplicates"][0]
    assert gt["documents"][i]["personal_number"] == gt["documents"][j]["personal_number"]
    assert any(t.startswith("rotated_") for tags_ in gt["page_tags"].values() for t in tags_)
    # ერთნაირი სახელი — სხვადასხვა პირადი ნომერი
    same = {d["personal_number"] for d in gt["documents"] if d["last_name"] == "კაპანაძე"}
    assert len(same) >= 2
    # ზედიზედ ორი სხვადასხვა დოკუმენტი ერთ თანამშრომელზე
    docs = gt["documents"]
    assert any(a["personal_number"] == b["personal_number"] and a["type_id"] != b["type_id"]
               for a, b in zip(docs, docs[1:]))
    assert any(d["personal_number"] and d["personal_number"].startswith("0000") for d in docs)


def test_rotated_and_image_pages(batch):
    path, gt = batch
    rotated = [(int(p), [t for t in tags if t.startswith("rotated_")][0]) for p, tags in gt["page_tags"].items()
               if any(t.startswith("rotated_") for t in tags)]
    assert rotated
    with PdfDocument(path) as doc:
        for p, tag in rotated:
            deg = int(tag.split("_")[1])
            assert doc.text_layer(p).strip() == ""              # რასტერიზებული გვერდი — ტექსტური ფენა არ არის
            img = doc.render(p, dpi=100)
            found = detect_rotation(img)
            assert found == (90 if deg in (90, 270) else 0)    # 180 ევრისტიკით არ ჩანს (დოკუმენტირებულია)
        for p in gt["blank_pages"]:
            assert doc.text_layer(p).strip() == ""
        for p, tags in gt["page_tags"].items():
            if "image_only" in tags:
                assert doc.text_layer(int(p)).strip() == ""
        first = gt["documents"][0]
        assert first["personal_number"] in doc.text_layer(first["start"])


def test_corrupt_and_encrypted(tmp_path):
    c = S.make_corrupt(tmp_path / "corrupt.pdf")
    chk = validate_pdf(c)
    assert chk.ok is False and chk.error_code == "pdf_corrupt"
    e = S.make_encrypted(tmp_path / "enc.pdf")
    chk2 = validate_pdf(e)
    assert chk2.ok is False and chk2.error_code == "pdf_encrypted"


def test_singles_and_cli(tmp_path):
    singles = S.make_singles(tmp_path / "singles", 4, seed=2)
    assert len(singles) == 4
    for gt in singles:
        assert validate_pdf(gt["file"]).ok and len(gt["documents"]) == 1
        assert gt["documents"][0]["start"] == 0 and gt["documents"][0]["end"] == gt["pages"] - 1
    out = tmp_path / "cli"
    assert S.main(["--out", str(out), "--batch-docs", "0", "--singles", "2", "--seed", "3"]) == 0
    data = json.loads((out / "ground_truth.json").read_text(encoding="utf-8"))
    assert len(data["singles"]) == 2 and (out / "corrupt.pdf").exists() and (out / "encrypted.pdf").exists()
