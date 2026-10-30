"""OCR-ის ხარისხის ტესტის ინსტრუმენტი: CER/WER გამოთვლა და ანგარიში (ტექსტური ფენით, Tesseract-ის გარეშე)."""
from __future__ import annotations

from hrsorter.config import make_config
from hrsorter.ocrtest import levenshtein, parse_pages, run, score
from tools import synthetic


def test_levenshtein_and_score():
    assert levenshtein("ხელშეკრულება", "ხელშეკრულება") == 0
    assert levenshtein("ხელშეკრულება", "ხელშეკრულეზა") == 1
    s = score("ნინო ბერიძე პ/ნ 01001000001", "ნინო ბერიძე პ/ნ 01001000001")
    assert s.cer == 0 and s.wer == 0 and (s.pn_found, s.pn_total) == (1, 1)
    bad = score("ნინო ბერიშე 3/6 01001000007", "ნინო ბერიძე პ/ნ 01001000001")
    assert bad.cer > 0 and bad.wer == 0.75 and bad.pn_found == 0


def test_parse_pages():
    assert parse_pages("1,3-4", 10) == [0, 2, 3]
    assert parse_pages(None, 3) == [0, 1, 2]
    assert parse_pages("2,99", 5) == [1]


def test_run_writes_report_and_scores_with_truth(tmp_path):
    pdf = tmp_path / "doc.pdf"
    synthetic.render_pdf([synthetic.make_document("contract", __import__("random").Random(1))], pdf)
    cfg = make_config({"paths": {"root": str(tmp_path / "root")}})
    out = tmp_path / "out"
    report = run(pdf, "1", out, cfg)
    text = (out / "page_001.txt").read_text(encoding="utf-8")
    assert report.exists() and (out / "page_001.png").exists() and text
    truth = tmp_path / "truth"
    truth.mkdir()
    (truth / "page_001.txt").write_text(text, encoding="utf-8")   # ტექსტური ფენა = იდეალური „OCR"
    html_ = run(pdf, "1", out, cfg, truth).read_text(encoding="utf-8")
    assert "CER 0.0%" in html_ and "ძალიან კარგი" in html_
