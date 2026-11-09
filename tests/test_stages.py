"""თანამდებობრივი ინსტრუქცია და ეტაპები (სტაჟიორი / გამოსაცდელი / შრომითი): ტიპის ამოცნობა, MHR-ის ფაილის არჩევა ქაღალდის
ეტაპით და ბოლო ცვლილებით, ახალი ტიპის დამატება განახლებისას. მონაცემები გამოგონილია."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hrsorter import mhr_queue as q
from hrsorter.classifier import Classifier
from hrsorter.config import EXAMPLE_TYPES_PATH, OFFERED_TYPES_KEY, make_config, merge_new_types
from hrsorter.db import Repository
from hrsorter.mhr_client import MockMhr
from hrsorter.mhr_models import MhrDocRef
from hrsorter.output import part_document_id
from hrsorter.stages import INTERN, LABOR, PROBATION, file_stage, strip_stage, text_stage
from hrsorter.validation import latest_versions, missing_original_note, same_stage, type_from_filename
from tests.test_packets import PN, add_scan, make_docx

INSTR_PROBATION = ["მენეჯერის\nთანამდებობრივი ინსტრუქცია\nპოზიცია მენეჯერი\nპოზიცია ექვემდებარება ფილიალის ხელმძღვანელს\n"
                   "თავის საქმიანობაში უნდა იხელმძღვანელოს კომპანიის შინაგანაწესით\n"
                   "1.3 მისი სახელფასო სარგო განისაზღვრება გამოსაცდელი ვადით შრომითი ხელშეკრულებით",
                   "2.1 ემსახურება მომხმარებლებს სალაროში ყოველდღიურად და აღრიცხავს ნაშთებს\n"
                   "5. სახელმძღვანელო დოკუმენტაცია: გამოსაცდელი ვადით შრომითი ხელშეკრულება; შინაგანაწესი"]
INSTR_INTERN = ["მენეჯერი (სტაჟიორი)\nთანამდებობრივი ინსტრუქცია\nპოზიცია მენეჯერი (სტაჟიორი)\nპოზიცია ექვემდებარება ფილიალის ხელმძღვანელს\n"
                "თავის საქმიანობაში უნდა იხელმძღვანელოს კომპანიის შინაგანაწესით\n"
                "1.3 სტაჟიორს სარგო განისაზღვრება სტაჟირების ხელშეკრულებით",
                "2.1 ემსახურება მომხმარებლებს სალაროში ყოველდღიურად და აღრიცხავს ნაშთებს\n"
                "5. სახელმძღვანელო დოკუმენტაცია: სტაჟირების ხელშეკრულება; შინაგანაწესი"]
RULES = ["ლიფტით სარგებლობის წესები\nლიფტში აკრძალულია ხალხის გადაყვანა ტვირთის გარეშე\n"
         "მაქსიმალური დატვირთვა ხუთასი კილოგრამია გაუმართაობა დაუყოვნებლივ ეცნობება ადმინისტრაციას"]
ANNEX_OF_INSTRUCTION = "2026-11-02 წლის თანამდებობრივი ინსტრუქციის დანართი #2\nცხრილი #1\nავტომობილის მარკა მოდელი ტვირთამწეობა"


def _world(tmp_path, files: dict[str, tuple[str, list[str]]]):
    d = tmp_path / "mhr"
    (d / "files").mkdir(parents=True)
    (d / "employees.json").write_text(json.dumps([{"id": "E1", "personal_number": PN, "first_name": "ნინო", "last_name": "ბერიძე",
                                                   "status": "active"}], ensure_ascii=False), encoding="utf-8")
    docs = []
    for doc_id, (name, pages) in files.items():
        (d / "files" / name).write_bytes(make_docx(pages))
        docs.append({"id": doc_id, "employee_id": "E1", "category": "contract", "title": "ხელშეკრულება", "filename": name,
                     "created_at": "2026-10-02T10:00:00Z", "document_date": "2026-10-02", "kind": "generated", "signed_at": None})
    (d / "documents.json").write_text(json.dumps(docs, ensure_ascii=False), encoding="utf-8")
    cfg = make_config({"paths": {"root": str(tmp_path)},
                       "mhr": {"enabled": True, "mode": "mock", "mock_dir": str(d), "recheck": {"enabled": False}}})
    return SimpleNamespace(cfg=cfg, repo=Repository(cfg.paths.db), mhr=MockMhr(d), dir=d, tmp=tmp_path)


def _validation(w, doc_id="doc_scan"):
    return json.loads(w.repo.mhr_get(doc_id)["validation"] or "{}")


SIGNED = {"signature_hint": "present", "signature_parties": {"employer": True, "employee": True}}


# --- ტიპის ამოცნობა -----------------------------------------------------------------------------------------
def test_job_description_is_recognised_by_its_title():
    cfg = make_config()
    c = Classifier(cfg).classify_text(INSTR_PROBATION[0] + "\nდამსაქმებელი და დასაქმებული; შრომის ანაზღაურება — ხელფასი")
    assert c.type_id == "job_description" and c.confidence > 0.8
    assert cfg.doc_type("job_description").mhr_original is True


def test_annex_of_a_job_description_is_not_a_job_description():
    assert Classifier(make_config()).classify_text(ANNEX_OF_INSTRUCTION).type_id != "job_description"


def test_mhr_file_name_tells_its_type():
    cfg = make_config()
    assert type_from_filename(cfg, "საწყობის თანამშრომელი (სტაჟიორი) - თანამდებობრივი ინსტრუქცია.docx") == "job_description"
    assert type_from_filename(cfg, "საწყობის თანამშრომელი (სტაჟიორი) - ხელშეკრულება.docx") == "contract"
    assert type_from_filename(cfg, "საწყობი - დანართი#2  (სტაჟიორი).docx") == "contract_annex"
    assert type_from_filename(cfg, "ლიფტით სარგებლობის წესები და პირობები (გამოსაცდელი).docx") is None


# --- ეტაპი ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name, stage", [
    ("საწყობის თანამშრომელი (სტაჟიორი) - ხელშეკრულება.docx", INTERN),
    ("ლიფტით სარგებლობის წესები და პირობები - სტაჟიორი.docx", INTERN),
    ("შინაგანაწესის დანართი - სტაჟიორი.docx.docx", INTERN),
    ("შინაგანაწესის დანართი - (გამოსაცდელი).docx", PROBATION),
    ("ლიფტით სარგებლობის წესები და პირობები (შრომითი).docx", LABOR),
    ("შრომითი ხელშეკრულება.docx", None),          # „შრომითი“ აქ ეტაპი არ არის
    ("მენეჯერი - თანამდებობრივი ინსტრუქცია.docx", None),
])
def test_file_stage_from_name(name, stage):
    assert file_stage(name) == stage


def test_strip_stage_keeps_the_rest_of_the_name():
    assert strip_stage("საწყობის თანამშრომელი (სტაჟიორი) - თანამდებობრივი ინსტრუქცია") == "საწყობის თანამშრომელი - თანამდებობრივი ინსტრუქცია"


def test_paper_stage_from_text():
    assert text_stage("\n".join(INSTR_PROBATION)) == PROBATION
    assert text_stage("\n".join(INSTR_INTERN)) == INTERN
    assert text_stage("ტრენინგები, სტაჟირებები, სემინარები") is None     # „სტაჟირებები“ ეტაპი არ არის
    assert text_stage("შრომითი ხელშეკრულება") is None                   # შრომითს ტექსტში ნიშანი არ აქვს


def test_latest_version_of_the_same_file_wins_and_stage_filter_keeps_unnamed():
    old = MhrDocRef("1", "contract", None, "2026-10-01T09:00:00Z", "მენეჯერი - ხელშეკრულება.docx", raw={"kind": "generated"})
    new = MhrDocRef("2", "contract", None, "2026-10-20T09:00:00Z", "მენეჯერი - ხელშეკრულება.docx", raw={"kind": "generated"})
    intern = MhrDocRef("3", "contract", None, "2026-10-02T09:00:00Z", "წესები (სტაჟიორი).docx", raw={"kind": "generated"})
    assert [r.doc_id for r in latest_versions([old, new, intern])] == ["2", "3"]
    assert [r.doc_id for r in same_stage([new, intern], PROBATION)] == ["2"]
    assert [r.doc_id for r in same_stage([new, intern], None)] == ["2", "3"]


def test_missing_note_names_the_stage_and_what_the_card_has():
    cfg = make_config()
    card = [MhrDocRef("1", "contract", None, "2026-10-02", "მენეჯერი (სტაჟიორი) - თანამდებობრივი ინსტრუქცია.docx", raw={"kind": "generated"})]
    note = missing_original_note(cfg, "job_description", PROBATION, card)
    assert "გამოსაცდელი ვადის თანამდებობრივი ინსტრუქცია ვერ მოიძებნა" in note and "მხოლოდ სტაჟიორის ვერსიაა" in note
    assert "ხელშეკრულების ორიგინალი MHR-ში ვერ მოიძებნა" in missing_original_note(cfg, "contract", None, card)


# --- MHR: საკუთარი ეტაპის ფაილი ---------------------------------------------------------------------------------
def test_job_description_is_compared_with_the_file_of_its_own_stage(tmp_path):
    w = _world(tmp_path, {"S1": ("მენეჯერი (სტაჟიორი) - თანამდებობრივი ინსტრუქცია.docx", INSTR_INTERN),
                          "S2": ("მენეჯერი (გამოსაცდელი) - თანამდებობრივი ინსტრუქცია.docx", INSTR_PROBATION)})
    add_scan(w, [(INSTR_PROBATION[0], {}), (INSTR_PROBATION[1], SIGNED)], type_id="job_description")
    q.sweep(w.repo, w.cfg, w.mhr)
    v = _validation(w)
    assert v["original"]["doc_id"] == "S2" and w.repo.get_document("doc_scan")["type_id"] == "job_description"
    assert w.repo.mhr_get("doc_scan")["status"] == "ready"
    w.repo.close()


def test_only_another_stage_on_the_card_is_said_plainly(tmp_path):
    w = _world(tmp_path, {"S1": ("მენეჯერი (სტაჟიორი) - თანამდებობრივი ინსტრუქცია.docx", INSTR_INTERN),
                          "R1": ("ლიფტით სარგებლობის წესები (გამოსაცდელი).docx", RULES)})
    add_scan(w, [(INSTR_PROBATION[0], {}), (INSTR_PROBATION[1], SIGNED)], type_id="job_description")
    q.sweep(w.repo, w.cfg, w.mhr)
    v = _validation(w)
    assert w.repo.mhr_get("doc_scan")["status"] == "failed" and v.get("original") is None
    miss = next(c for c in v["checks"] if c["field"] == "original_contract")
    assert miss["blocking"] and "გამოსაცდელი ვადის თანამდებობრივი ინსტრუქცია" in miss["note"] and "სტაჟიორის ვერსიაა" in miss["note"]
    w.repo.close()


def test_rules_in_a_probation_package_go_to_the_probation_file(tmp_path):
    """გამოსაცდელისა და შრომითის წესები სიტყვასიტყვით ერთნაირია — ფაილს პაკეტის ეტაპი წყვეტს, არა ტექსტი."""
    w = _world(tmp_path, {"S2": ("მენეჯერი (გამოსაცდელი) - თანამდებობრივი ინსტრუქცია.docx", INSTR_PROBATION),
                          "R3": ("ლიფტით სარგებლობის წესები (შრომითი).docx", RULES),
                          "R2": ("ლიფტით სარგებლობის წესები (გამოსაცდელი).docx", RULES)})
    add_scan(w, [(INSTR_PROBATION[0], {}), (INSTR_PROBATION[1], SIGNED), (RULES[0], SIGNED)], type_id="job_description")
    q.sweep(w.repo, w.cfg, w.mhr)
    q.sweep(w.repo, w.cfg, w.mhr)
    assert _validation(w)["status"] == "split"
    assert w.repo.get_document(part_document_id("doc_scan", "R2")) is not None
    assert w.repo.get_document(part_document_id("doc_scan", "R3")) is None
    assert w.repo.get_document(part_document_id("doc_scan", "S2"))["type_id"] == "job_description"
    w.repo.close()


# --- განახლება: ახალი ტიპი არსებულ კონფიგურაციაში --------------------------------------------------------------
def test_new_types_are_added_once_and_user_types_stay(tmp_path):
    example = json.loads(EXAMPLE_TYPES_PATH.read_text(encoding="utf-8"))
    live = json.loads(json.dumps(example))
    live["types"] = [t for t in live["types"] if t["id"] != "job_description"]
    live["types"][0]["positive"].append({"pattern": "ჩემი წესი", "weight": 1})   # მომხმარებლის ცვლილება
    path = tmp_path / "document_types.json"
    path.write_text(json.dumps(live, ensure_ascii=False), encoding="utf-8")

    assert merge_new_types(path) == ["job_description"]
    after = json.loads(path.read_text(encoding="utf-8"))
    ids = [t["id"] for t in after["types"]]
    assert "job_description" in ids and ids[-1] == "unknown"
    assert {"pattern": "ჩემი წესი", "weight": 1} in after["types"][0]["positive"]
    assert "job_description" in after[OFFERED_TYPES_KEY]

    after["types"] = [t for t in after["types"] if t["id"] != "job_description"]   # მომხმარებელმა წაშალა
    path.write_text(json.dumps(after, ensure_ascii=False), encoding="utf-8")
    assert merge_new_types(path) == []
    assert "job_description" not in [t["id"] for t in json.loads(path.read_text(encoding="utf-8"))["types"]]
