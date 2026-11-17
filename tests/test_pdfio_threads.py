"""PDFium ერთ პროცესში რამდენიმე ნაკადიდან: დამმუშავებელი, MHR-ის შემოწმება და ვებგვერდი ერთდროულად ხატავენ გვერდებს.

საკეტის გარეშე PDFium ფუჭდებოდა და შემდეგ ყოველი გახსნა „Data format error“-ით ვარდებოდა:
გვერდის სურათები აღარ ჩანდა, ახალი სკანი კი დაზიანებულად ჩაითვლებოდა.
"""
import threading

import pytest

from hrsorter.pageanalysis import _text_layer_boxes
from hrsorter.pdfio import PdfDocument, validate_pdf
from tools import synthetic as S


@pytest.fixture(scope="module")
def pdfs(tmp_path_factory):
    d = tmp_path_factory.mktemp("threads")
    out = []
    for i in range(2):
        p = d / f"batch{i}.pdf"
        S.make_batch(p, n_docs=3, seed=i)
        out.append(p)
    return out


def test_concurrent_use_does_not_break_pdfium(pdfs):
    errors: list[str] = []
    guard = threading.Lock()

    def note(msg: str) -> None:
        with guard:
            errors.append(msg)

    def renderer(k: int) -> None:   # დამმუშავებელი / MHR-ის შემოწმება / გვერდის სურათი
        for r in range(4):
            try:
                with PdfDocument(pdfs[(k + r) % len(pdfs)]) as doc:
                    for i in range(doc.page_count):
                        doc.render(i, 40 + 10 * k, grayscale=k % 2 == 0)
                        doc.text_layer(i)
                        _text_layer_boxes(doc, i)
            except Exception as e:  # noqa: BLE001
                note(f"{type(e).__name__}: {e}")

    def watcher() -> None:          # საქაღალდის მეთვალყურე ახალ სკანს ამოწმებს
        for r in range(20):
            c = validate_pdf(pdfs[r % len(pdfs)])
            if not c.ok:
                note(f"validate_pdf: {c.error_code} {c.detail}")

    threads = [threading.Thread(target=renderer, args=(k,)) for k in range(5)] + [threading.Thread(target=watcher)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    # ერთდროული მუშაობის შემდეგ ჩვეულებრივი გახსნა ისევ მუშაობს
    with PdfDocument(pdfs[0]) as doc:
        assert doc.render(0, 50, grayscale=False).size[0] > 0
    assert validate_pdf(pdfs[1]).ok
