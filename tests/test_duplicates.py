from __future__ import annotations

import pytest

from hrsorter.config import make_config
from hrsorter.db import Repository
from hrsorter.duplicates import check, content_fingerprint, jaccard, text_fingerprint
from hrsorter.models import PageText

CFG = make_config()
PN = "01001000001"
BASE_TEXT = ("ბრძანება № 15 დაჯარიმების შესახებ თანამშრომელი ნინო ბერიძე პირადი ნომერი 01001000001 "
             "შრომის შინაგანაწესის დარღვევის გამო დაეკისროს ჯარიმა ასი ლარის ოდენობით ბრძანება შედის ძალაში "
             "ხელმოწერისთანავე დირექტორი გიორგი მაისურაძე")
OTHER_TEXT = ("შვებულების განცხადება გთხოვთ მომცეთ ანაზღაურებადი შვებულება ათი კალენდარული დღით "
              "პირველი აგვისტოდან თანამშრომელი ნინო ბერიძე ბუღალტერია ფინანსური სამსახური თბილისის ფილიალი")


def page(text: str, i: int = 0, blank: bool = False) -> PageText:
    return PageText(i, text, "text_layer", 0.9, blank, False, False, 0.01)


@pytest.fixture
def repo():
    r = Repository(":memory:")
    yield r
    r.close()


def store(repo, doc_id, text, *, job="job_old", type_id="fine", pn=PN, doc_number="15", date="2024-03-15",
          content_fp="cfp_other", active=1):
    tfp, sh = text_fingerprint([page(text)])
    repo.upsert_document({"id": doc_id, "job_id": job, "source_id": "src_a", "page_start": 0, "page_end": 0,
                          "status": "proposed", "active": active, "type_id": type_id, "personal_number": pn,
                          "doc_number": doc_number, "document_date": date, "content_fingerprint": content_fp,
                          "text_fingerprint": tfp, "text_shingles": sh})


def run(repo, text, *, job="job_new", type_id="fine", pn=PN, doc_number="15", date="2024-03-15", content_fp="cfp_new"):
    tfp, sh = text_fingerprint([page(text)])
    return check(repo, content_fp=content_fp, text_fp=tfp, shingles=sh, type_id=type_id, personal_number=pn,
                 doc_number=doc_number, document_date=date, job_id=job, cfg=CFG)


def test_fingerprints_deterministic_and_normalized():
    assert content_fingerprint(["a", "b"]) == content_fingerprint(["a", "b"]) != content_fingerprint(["b", "a"])
    fp1, sh1 = text_fingerprint([page("ბრძანება  №15\nნინო")])
    fp2, sh2 = text_fingerprint([page("ბრძანება №15 ნინო")])
    assert fp1 == fp2 and sh1 == sh2


def test_shingles_capped_and_empty_text():
    long = " ".join(f"სიტყვა{i}" for i in range(2000))
    _, sh = text_fingerprint([page(long)])
    assert len(sh) == 400
    assert text_fingerprint([page("", blank=True)]) == ("", [])


def test_exact_by_content_fingerprint(repo):
    store(repo, "doc_a", OTHER_TEXT, content_fp="same")
    d = run(repo, BASE_TEXT, content_fp="same")
    assert d.status == "exact" and d.matches[0]["document_id"] == "doc_a"
    assert "content_fingerprint" in d.matches[0]["reasons"]


def test_exact_by_text_fingerprint(repo):
    store(repo, "doc_a", BASE_TEXT)
    d = run(repo, BASE_TEXT)
    assert d.status == "exact" and d.matches[0]["reasons"] == ["text_fingerprint"]


def test_same_job_and_inactive_excluded(repo):
    store(repo, "doc_a", BASE_TEXT, job="job_new", content_fp="same")
    store(repo, "doc_b", BASE_TEXT, job="job_old", content_fp="same", active=0)
    assert run(repo, BASE_TEXT, job="job_new", content_fp="same").status == "none"


def test_possible_duplicate_rescan_with_small_ocr_difference(repo):
    store(repo, "doc_a", BASE_TEXT)
    rescan = BASE_TEXT.replace("ხელმოწერისთანავე", "ხელმოწერისთანავე.") + " გვ 1"
    d = run(repo, rescan)
    assert d.status == "possible"
    m = d.matches[0]
    assert m["document_id"] == "doc_a" and m["score"] >= CFG.thresholds["duplicate_possible"]
    assert "same_personal_number" in m["reasons"]


def test_same_employee_and_type_alone_is_not_duplicate(repo):
    store(repo, "doc_a", BASE_TEXT)
    d = run(repo, OTHER_TEXT, doc_number="15", date="2024-03-15")  # ყველა ატრიბუტი ემთხვევა, ტექსტი — არა
    assert d.status == "none"


def test_jaccard():
    assert jaccard([1, 2, 3], [1, 2, 3]) == 1.0
    assert jaccard([], [1]) == 0.0


def test_same_batch_duplicate_detected_but_not_self_or_old_versions(tmp_path):
    from hrsorter.config import make_config
    from hrsorter.db import Repository
    from hrsorter.duplicates import check
    cfg = make_config({"paths": {"root": str(tmp_path)}})
    repo = Repository(cfg.paths.db)
    def doc(i, job, src):
        repo.upsert_document({"id": f"doc_{i:032x}", "job_id": job, "source_id": src, "page_start": i,
                              "page_end": i, "status": "proposed", "active": 1, "content_fingerprint": "fp"})
    doc(1, "job_a", "src_1")                      # იმავე batch-ის პირველი ასლი
    kw = dict(content_fp="fp", text_fp="", shingles=[], type_id=None, personal_number=None, doc_number=None,
              document_date=None, cfg=cfg)
    # იმავე job-ის მეორე ასლი → დუბლიკატი
    assert check(repo, job_id="job_a", document_id=f"doc_{2:032x}", source_id="src_1", **kw).status == "exact"
    # თავად ეს დოკუმენტი (ხელახალი გაშვება) → არა
    assert check(repo, job_id="job_a", document_id=f"doc_{1:032x}", source_id="src_1", **kw).status == "none"
    # იმავე წყაროს ხელახალი დამუშავება (სხვა job) → ძველი ვერსია დუბლიკატად არ ითვლება
    assert check(repo, job_id="job_b", document_id=f"doc_{3:032x}", source_id="src_1", **kw).status == "none"
    # სხვა წყარო (სხვა დიდ PDF-ში მოხვედრილი იგივე დოკუმენტი) → დუბლიკატი
    assert check(repo, job_id="job_c", document_id=f"doc_{4:032x}", source_id="src_2", **kw).status == "exact"
