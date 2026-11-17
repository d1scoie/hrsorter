"""ჯარიმის ბრძანება („№FIN 1017/11-2026“, „დაჯარიმდეს“, „ჯარიმა 12.50 ლარის“): ტიპის ამოცნობა, ნომერი, განახლებისას წესების
შეცვლა და MHR-ში მხოლოდ იმავე ნომრის ორიგინალთან შედარება. მონაცემები გამოგონილია."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hrsorter import mhr_queue as q
from hrsorter.classifier import Classifier
from hrsorter.config import (EXAMPLE_TYPES_PATH, SHIPPED_TYPE_VERSIONS, make_config, type_fingerprint,
                             update_shipped_types)
from hrsorter.db import Repository
from hrsorter.extractor import extract
from hrsorter.features import find_doc_number
from hrsorter.mhr_client import MockMhr
from hrsorter.mhr_models import MhrDocRef
from hrsorter.models import PageText
from hrsorter.validation import same_series, series_numbers
from tests.test_packets import PN, SIGNED, add_scan, make_docx


def fine_order(number: str, amount: str, numbered: bool = True) -> str:
    head = f"შპს „ალფა“\n№FIN {number}\n2026-11-05\n" if numbered else "შპს „ალფა“\n2026-11-05\n"
    return head + (
        "სამუშაო განრიგის დარღვევის გამო, დისციპლინარული სასჯელის დაკისრების მიზნით\n"
        "ვბრძანებ\n"
        "1. ნინო ბერიძე (პ/ნ: 01001000001) – დაჯარიმდეს შინაგანაწესის დარღვევისთვის, კერძოდ, სამუშაოზე დაგვიანების გამო.\n"
        f"2. ნინო ბერიძეს დაექვითოს მიმდინარე თვის ხელფასიდან ჯარიმა {amount} ლარის ოდენობით.\n"
        "3. განმეორებითი დარღვევისას მის მიმართ შეიძლება გამოყენებულ იქნას უფრო მკაცრი სასჯელი, შრომითი ხელშეკრულების "
        "შეწყვეტის ჩათვლით.\n"
        "4. ბრძანება ძალაშია ხელმოწერისთანავე.\n"
        "დირექტორი: დავით ლომიძე    დასაქმებული: ნინო ბერიძე")


# შრომის შინაგანაწესის დანართი: „დაჯარიმებული“ და „დისციპლინარული სასჯელი“ ახსენია, მაგრამ ჯარიმის ბრძანება არ არის
RULES_ANNEX = ("მისამართი: ქ. თბილისი\n"
               "5.3 თანამშრომელი, რომელიც ერთი თვის განმავლობაში ორჯერ დაჯარიმებულია, შეიძლება გადაყვანილ იქნას სხვა პოზიციაზე.\n"
               "5.4 დისციპლინარული სასჯელის სახეებია: გაფრთხილება, საყვედური და ხელფასის ნაწილის დაქვითვა.\n"
               "5.5 თანამშრომელი ვალდებულია გაეცნოს წესებს და დაიცვას ისინი.")


def pt(text: str, i: int = 0) -> PageText:
    return PageText(page_index=i, text=text, text_source="ocr:tesseract", ocr_quality=0.9, is_blank=False, has_marks=True,
                    marks_only=False, ink_ratio=0.05)


@pytest.fixture
def clf(tmp_path):
    return Classifier(make_config({"paths": {"root": str(tmp_path)}}))


# --- ტიპი ---------------------------------------------------------------------------------------------------------
def test_fine_order_with_series_number_is_a_fine(clf):
    c = clf.classify_pages([pt(fine_order("1017/11-2026", "12.50"))])
    assert c.type_id == "fine" and c.confidence >= 0.9


def test_fine_order_is_a_fine_even_when_the_number_line_was_not_read(clf):
    c = clf.classify_pages([pt(fine_order("1017/11-2026", "12,5", numbered=False))])
    assert c.type_id == "fine"


def test_rules_annex_that_mentions_fines_is_not_a_fine(clf):
    assert clf.classify_pages([pt(RULES_ANNEX)]).type_id != "fine"


def test_series_number_line_marks_the_start_of_a_fine_order(clf):
    assert any(h.type_id == "fine" for h in clf.title_hits(fine_order("1017/11-2026", "12.50")))


@pytest.mark.parametrize("type_id", ["fine", None])
def test_fine_amount_is_not_read_as_salary(tmp_path, type_id):
    cfg = make_config({"paths": {"root": str(tmp_path)}})
    f = extract([pt(fine_order("1017/11-2026", "12.50"))], cfg.doc_type(type_id) if type_id else None, cfg).fields
    assert f["salary"].value is None
    assert f["fine_amount"].value == 12.5 and f["document_number"].value == "FIN 1017/11-2026"


def test_salary_line_that_also_mentions_a_fine_keeps_the_salary(tmp_path):
    cfg = make_config({"paths": {"root": str(tmp_path)}})
    text = "შრომითი ხელშეკრულება\n2.1 ხელფასი შეადგენს 1500 ლარს, დაგვიანებისთვის ჯარიმა 10 ლარი"
    assert extract([pt(text)], cfg.doc_type("contract"), cfg).fields["salary"].value == "1500"


# --- ნომერი ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text,number", [
    ("შპს „ალფა“\n№fin 1017/11-2026\n2026-11-05", "FIN 1017/11-2026"),
    ("№ fin1017/11-2026", "FIN 1017/11-2026"),            # ნომერი ნორმალიზებულ (პატარა ასოებიან) ტექსტში იძებნება
    ("ბრძანება № 34-კ", "34-კ"),                       # ასოების გარეშე — როგორც ადრე
])
def test_document_number_keeps_series_letters(text, number):
    assert find_doc_number(text)[0] == number


def test_month_name_is_not_a_document_number():
    assert find_doc_number("valid until 12 nov 2030") is None


def test_series_numbers_read_paper_and_mhr_file_name_alike():
    assert series_numbers("№FIN 1017/11-2026") == {("FIN", 1017)}
    assert series_numbers("№FIN 1017-11-2026 — ნინო ბერიძე-1.docx") == {("FIN", 1017)}
    assert series_numbers("ქ. თბილისი 12/11/2026") == set()        # თარიღი ნომერი არ არის


def _ref(doc_id: str, filename: str) -> MhrDocRef:
    return MhrDocRef(doc_id, "fine", "ჯარიმა", "2026-11-16T10:00:00Z", filename, raw={"kind": "generated"})


def test_other_number_of_the_same_series_is_never_the_original():
    refs = [_ref("A", "№FIN 1052-11-2026 — ნინო ბერიძე.docx"), _ref("B", "№FIN 1017-11-2026 — ნინო ბერიძე.docx"),
            _ref("C", "ლიფტის წესები.docx")]
    assert [r.doc_id for r in same_series(refs, [fine_order("1017/11-2026", "12.50")])] == ["B", "C"]
    assert [r.doc_id for r in same_series(refs, [fine_order("1086/11-2026", "12.50")])] == ["C"]
    assert same_series(refs, ["შრომითი ხელშეკრულება № 15"]) == refs       # ქაღალდზე სერიის ნომერი არ არის


# --- MHR: შედარება მხოლოდ იმავე ნომრის ბრძანებასთან ---------------------------------------------------------------------
@pytest.fixture
def fines_world(tmp_path):
    d = tmp_path / "mhr"
    (d / "files").mkdir(parents=True)
    (d / "employees.json").write_text(json.dumps([{"id": "E1", "personal_number": PN, "first_name": "ნინო", "last_name": "ბერიძე",
                                                   "status": "active"}], ensure_ascii=False), encoding="utf-8")
    d.joinpath("documents.json").write_text("[]", encoding="utf-8")
    cfg = make_config({"paths": {"root": str(tmp_path)}, "mhr": {"enabled": True, "mode": "mock", "mock_dir": str(d), "recheck": {"enabled": False}}})
    repo = Repository(cfg.paths.db)
    w = SimpleNamespace(cfg=cfg, repo=repo, mhr=MockMhr(d), dir=d, tmp=tmp_path)
    yield w
    repo.close()


def _generate(w, doc_id: str, number: str, amount: str) -> None:
    name = f"№FIN {number} — ნინო ბერიძე.docx"
    (w.dir / "files" / name).write_bytes(make_docx([fine_order(number.replace("-", "/", 1), amount)]))
    docs = json.loads((w.dir / "documents.json").read_text(encoding="utf-8"))
    docs.append({"id": doc_id, "employee_id": "E1", "category": "fine", "title": "ჯარიმა", "filename": name,
                 "created_at": "2026-11-16T10:00:00Z", "document_date": "2026-11-16", "kind": "generated", "signed_at": None})
    (w.dir / "documents.json").write_text(json.dumps(docs, ensure_ascii=False), encoding="utf-8")


def _sweep(w):
    for _ in range(2):
        q.sweep(w.repo, w.cfg, w.mhr)


def _validation(w):
    return json.loads(w.repo.mhr_get("doc_scan")["validation"] or "{}")


def test_fine_is_not_compared_with_another_fine_of_the_same_employee(fines_world):
    _generate(fines_world, "F52", "1052-11-2026", "9.40")         # MHR-ში მხოლოდ სხვა ბრძანებაა
    add_scan(fines_world, [(fine_order("1017/11-2026", "12.50"), SIGNED)], type_id="fine")
    _sweep(fines_world)
    v = _validation(fines_world)
    assert not v.get("original") or v["original"].get("doc_id") != "F52"
    status = fines_world.repo.mhr_get("doc_scan")["status"]
    assert (q.approve_override if status == "failed" else q.approve)(fines_world.repo, "doc_scan", "operator")
    _sweep(fines_world)
    up = json.loads((fines_world.dir / "uploads.json").read_text(encoding="utf-8"))
    assert up and up[0].get("signed_copy_of") != "F52"            # სხვა ბრძანების ხელმოწერილ ასლად არ აიტვირთა


def test_fine_finds_its_own_order_among_several(fines_world):
    _generate(fines_world, "F52", "1052-11-2026", "9.40")
    _generate(fines_world, "F17", "1017-11-2026", "12.50")
    add_scan(fines_world, [(fine_order("1017/11-2026", "12.50"), SIGNED)], type_id="fine")
    _sweep(fines_world)
    assert _validation(fines_world)["original"]["doc_id"] == "F17"


def test_unknown_scan_of_a_fine_takes_type_only_from_its_own_order(fines_world):
    _generate(fines_world, "F52", "1052-11-2026", "9.40")
    add_scan(fines_world, [(fine_order("1017/11-2026", "12.50"), SIGNED)], type_id="unknown", status="needs_review",
             reasons=("unknown_type",))
    _sweep(fines_world)
    assert fines_world.repo.mhr_get("doc_scan")["status"] == "skipped"      # სხვა ბრძანებას არ „დაემთხვა“


# --- განახლება: ხელით შეუცვლელი ჩაშენებული ტიპი ახალ წესებს იღებს -----------------------------------------------------------
FINE_0_1_30 = {   # ჯარიმის ტიპი, როგორც 0.1.30-მდე მოჰქონდა პროგრამას
    "id": "fine", "name_ka": "ჯარიმა", "folder": "ჯარიმა", "description": "დაჯარიმების / დისციპლინური პასუხისმგებლობის ბრძანება",
    "positive": [{"pattern": "დაჯარიმებ", "weight": 5, "title_only": True}, {"pattern": "ჯარიმ", "weight": 3},
                 {"pattern": "დისციპლინური\\s+(პასუხისმგებლ|სახდელ)", "weight": 3},
                 {"pattern": "ბრძანება", "weight": 2, "title_only": True}],
    "negative": [{"pattern": "შვებულებ", "weight": 3, "title_only": True}],
    "fields": ["fine_amount", "fine_currency", "fine_reason", "signature_status"],
    "active": True, "auto_save_enabled": False, "attachment_policy": "keep_continuation", "signature_expected": True,
}


def _live_with(tmp_path, fine: dict) -> tuple:
    example = json.loads(EXAMPLE_TYPES_PATH.read_text(encoding="utf-8"))
    live = json.loads(json.dumps(example))
    live["types"] = [fine if t["id"] == "fine" else t for t in live["types"]]
    path = tmp_path / "document_types.json"
    path.write_text(json.dumps(live, ensure_ascii=False, indent=2), encoding="utf-8")
    return path, next(t for t in example["types"] if t["id"] == "fine")


def test_old_shipped_fine_rules_are_recognised():
    assert type_fingerprint(FINE_0_1_30) in SHIPPED_TYPE_VERSIONS["fine"]
    assert type_fingerprint(dict(reversed(list(FINE_0_1_30.items())))) == type_fingerprint(FINE_0_1_30)   # რიგი არ მოქმედებს


def test_untouched_fine_type_gets_the_new_rules_on_update(tmp_path):
    path, new = _live_with(tmp_path, FINE_0_1_30)
    assert update_shipped_types(path) == ["fine"]
    after = json.loads(path.read_text(encoding="utf-8"))
    assert next(t for t in after["types"] if t["id"] == "fine") == new
    assert [t["id"] for t in after["types"]][-1] == "unknown"
    assert update_shipped_types(path) == []                      # მეორედ აღარაფერი იცვლება


def test_fine_type_edited_by_the_user_is_left_alone(tmp_path):
    mine = json.loads(json.dumps(FINE_0_1_30))
    mine["positive"].append({"pattern": "ჩემი წესი", "weight": 1})
    path, _ = _live_with(tmp_path, mine)
    assert update_shipped_types(path) == []
    assert next(t for t in json.loads(path.read_text(encoding="utf-8"))["types"] if t["id"] == "fine") == mine
