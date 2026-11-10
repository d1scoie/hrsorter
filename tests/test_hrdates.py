"""თარიღების გაფრთხილებები: არასრულწლოვანი, ხელმოწერა მუშაობის დაწყების შემდეგ, პირადობის ვადა. გაფრთხილება არაფერს
აჩერებს — შემოწმების შედეგში და გვერდზე „თანამშრომლები“ ჩანს. მონაცემები გამოგონილია."""
from __future__ import annotations

import datetime as dt
import json
import shutil

from fastapi.testclient import TestClient

from hrsorter import hrdates, people
from hrsorter.config import EXAMPLE_TYPES_PATH, load_config, make_config
from hrsorter.db import Repository
from hrsorter.mhr_models import ValidationResult
from hrsorter.ui.app import create_app
from hrsorter.validation import _contract_date_warnings
from tests.test_empform import form_text, hired
from tests.test_identity import id_text
from tests.test_packets import PN, add_scan, sweep, validation, world  # noqa: F401  (world — fixture)

LOCAL = "http://127.0.0.1:8765"
TODAY = dt.date.today()


def _dmy(d: dt.date) -> str:
    return d.strftime("%d.%m.%Y")


def _scan_id(w, birth: str = "01.01.1990", expiry: str = "26.10.2036") -> None:
    """პირადობა დასკანერდა და MHR-ს დაემთხვა (MRZ-ის გარეშე — თარიღები მხოლოდ ტექსტშია)."""
    add_scan(w, [(id_text(expiry=expiry, mrz=False).replace("01.01.1990", birth), {})], doc_id="doc_id", job="job_id", type_id="id_card")
    sweep(w)


# --- სუფთა ფუნქციები --------------------------------------------------------------------------------------------
def test_age_counts_full_years():
    assert hrdates.age_on("2008-05-20", "2026-11-05") == 18
    assert hrdates.age_on("2008-11-06", "2026-11-05") == 17 and hrdates.age_on("2008-11-05", "2026-11-05") == 18
    assert hrdates.age_on(None, "2026-11-05") is None and hrdates.age_on("არ იკითხება", "2026-11-05") is None
    assert hrdates.comes_of_age("2008-02-29") == "2026-02-28" and hrdates.comes_of_age("2010-03-01") == "2028-03-01"


def test_minor_note_only_under_the_adult_age():
    note = hrdates.minor_note("2010-03-01", "2026-11-05")
    assert "16 წლისაა" in note and "არასრულწლოვანია" in note and "2026-11-05" in note
    assert hrdates.minor_note("2008-05-20", "2026-11-05") is None
    assert hrdates.minor_note(None, "2026-11-05") is None and hrdates.minor_note("2010-03-01", None) is None


def test_signed_late_note_only_when_the_date_is_after_the_start():
    note = hrdates.signed_late_note("2026-11-10", "2026-11-05")
    assert "2026-11-10" in note and "2026-11-05" in note and "5 დღის შემდეგ" in note
    assert hrdates.signed_late_note("2026-11-05", "2026-11-05") is None       # იმავე დღეს — წესრიგშია
    assert hrdates.signed_late_note("2026-11-01", "2026-11-05") is None       # წინასწარ მოწერილი
    assert hrdates.signed_late_note("2026-11-10", None) is None


def test_id_expiry_levels():
    assert hrdates.id_expiry("2026-10-01", "2026-11-10")[0] == hrdates.EXPIRED
    level, text = hrdates.id_expiry("2026-11-20", "2026-11-10")
    assert level == hrdates.SOON and "10 დღეში" in text and "2026-11-20" in text
    assert "დღეს" in hrdates.id_expiry("2026-11-10", "2026-11-10")[1]
    assert hrdates.id_expiry("2026-12-11", "2026-11-10") is None               # 31 დღე — ჯერ შორია
    assert hrdates.id_expiry("2026-12-11", "2026-11-10", days=60)[0] == hrdates.SOON
    assert hrdates.id_expiry(None, "2026-11-10") is None


def test_person_warnings_are_about_today_and_follow_the_settings():
    cfg = make_config()
    facts = {"birth_date": "2010-03-01", "id_expiry": "2026-11-20"}
    out = hrdates.person_warnings(cfg, facts, "2026-11-10")
    assert len(out) == 2 and "ვადა 10 დღეში გასდის" in out[0] and "არასრულწლოვანია: 16 წლის (18 შეუსრულდება 2028-03-01)" in out[1]
    assert hrdates.person_warnings(cfg, facts, "2028-03-01") == [hrdates.id_expiry("2026-11-20", "2028-03-01")[1]]   # უკვე სრულწლოვანია
    assert hrdates.person_warnings(cfg, {}, "2026-11-10") == [] and hrdates.person_warnings(cfg, None) == []
    strict = make_config({"mhr": {"warnings": {"adult_age": 16, "id_expiry_days": 5}}})
    assert hrdates.person_warnings(strict, facts, "2026-11-10") == []


# --- შემოწმება ----------------------------------------------------------------------------------------------------
def test_id_about_to_expire_is_still_uploaded_with_a_warning(world):
    soon = TODAY + dt.timedelta(days=10)
    _scan_id(world, expiry=_dmy(soon))
    v = validation(world, "doc_id")
    check = next(c for c in v["checks"] if c["field"] == "id_expiry")
    assert world.repo.mhr_get("doc_id")["status"] == "uploaded"                 # არ აჩერებს
    assert check["status"] == "info" and check["blocking"] is False and "10 დღეში გასდის" in check["note"]
    assert v["facts"] == {"birth_date": "1990-01-01", "id_expiry": soon.isoformat()}


def test_id_far_from_expiry_has_no_warning_but_keeps_the_facts(world):
    _scan_id(world)
    v = validation(world, "doc_id")
    assert not any(c["field"] == "id_expiry" for c in v["checks"]) and v["facts"]["id_expiry"] == "2036-10-26"


def test_form_of_a_minor_is_uploaded_and_says_so(world):
    hired(world)
    _scan_id(world, birth="01.03.2010")
    add_scan(world, [(form_text(birth="01.03.2010"), {})], doc_id="doc_form", job="job_form", type_id="employee_form")
    sweep(world)
    v = validation(world, "doc_form")
    age = next(c for c in v["checks"] if c["field"] == "age")
    assert world.repo.mhr_get("doc_form")["status"] == "uploaded"
    assert age["status"] == "info" and not age["blocking"] and "16 წლისაა" in age["note"]
    assert any("არასრულწლოვანია" in line for line in v["summary"])               # სიაშიც ჩანს


def test_form_of_an_adult_has_no_age_line(world):
    hired(world)
    _scan_id(world)
    add_scan(world, [(form_text(), {})], doc_id="doc_form", job="job_form", type_id="employee_form")
    sweep(world)
    assert not any(c["field"] == "age" for c in validation(world, "doc_form")["checks"])


def test_contract_date_warnings(world):
    cfg = world.cfg
    late = ValidationResult("failed")
    _contract_date_warnings(late, cfg, world.repo, {"fields": {"contract_start": {"value": "2026-11-05"}}}, "2026-11-10", None)
    assert [(c.field, c.status, c.blocking) for c in late.checks] == [("signed_late", "info", False)]
    assert "5 დღის შემდეგ" in late.checks[0].note

    fine = ValidationResult("failed")
    _contract_date_warnings(fine, cfg, world.repo, {"fields": {"contract_start": {"value": "2026-11-05"}}}, "2026-11-05", None)
    _contract_date_warnings(fine, cfg, world.repo, {"fields": {}}, "2026-11-10", None)       # დაწყება ქაღალდზე არ წერია
    assert fine.checks == []

    _scan_id(world, birth="01.03.2010")                                          # დადასტურებული პირადობა: 16 წლის
    minor = ValidationResult("failed")
    _contract_date_warnings(minor, cfg, world.repo, {"fields": {"contract_start": {"value": "2026-11-05"}}}, "2026-11-05",
                            {"document_id": "doc_id"})
    assert [c.field for c in minor.checks] == ["age"] and "16 წლისაა" in minor.checks[0].note


# --- გვერდი „თანამშრომლები“ და მთავარი ---------------------------------------------------------------------------
def test_people_overview_carries_the_warnings(world):
    _scan_id(world, birth="01.03.2010", expiry=_dmy(TODAY + dt.timedelta(days=10)))
    person = people.overview(world.repo, world.cfg)[0]
    assert len(person["warnings"]) == 2 and "10 დღეში გასდის" in person["warnings"][0] and "არასრულწლოვანია" in person["warnings"][1]


def test_id_checked_by_an_older_version_is_read_from_its_text(world):
    """ძველი ვერსიით დადასტურებულ პირადობას ფაქტები შენახული არ აქვს — გაფრთხილება მაინც ჩანს."""
    _scan_id(world, expiry=_dmy(TODAY + dt.timedelta(days=3)))
    v = validation(world, "doc_id")
    v.pop("facts")
    world.repo.conn.execute("UPDATE mhr_items SET validation=? WHERE document_id='doc_id'", (json.dumps(v, ensure_ascii=False),))
    assert "3 დღეში გასდის" in people.overview(world.repo, world.cfg)[0]["warnings"][0]


def test_page_and_home_show_the_warnings(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"paths": {"root": str(tmp_path / "root"), "types_file": "document_types.json"},
                                    "mhr": {"enabled": True, "mode": "mock", "mock_dir": str(tmp_path / "mhr")}}, ensure_ascii=False),
                        encoding="utf-8")
    shutil.copyfile(EXAMPLE_TYPES_PATH, tmp_path / "document_types.json")
    repo = Repository(load_config(cfg_path).paths.db)

    def person(doc_id: str, pn: str, first: str, last: str, facts: dict) -> None:
        emp = {"employee_id": "E" + pn[-1], "first_name": first, "last_name": last}
        sid = repo.insert_source(sha256=(doc_id * 8)[:64], size=1, page_count=1, archive_path="x.pdf", first_path=f"in/{doc_id}.pdf",
                                 profile_id="batch")
        repo.upsert_document({"id": doc_id, "job_id": "j", "source_id": sid, "page_start": 0, "page_end": 0, "status": "proposed",
                              "type_id": "id_card", "personal_number": pn})
        repo.mhr_enqueue(doc_id, pn)
        check = {"field": "identity", "label_ka": "პირადობა", "document_value": None, "mhr_value": None, "status": "match",
                 "blocking": False, "note": ""}
        repo.mhr_transition(doc_id, ("pending",), "uploaded", employee_id=emp["employee_id"],
                            validation={"status": "passed", "employee": emp, "checks": [check], "diffs": [], "summary": [], "facts": facts})

    person("doc_a", PN, "ნინო", "ბერიძე", {"birth_date": "1990-01-01", "id_expiry": (TODAY + dt.timedelta(days=7)).isoformat()})
    person("doc_b", "01001000002", "გიორგი", "მაისურაძე", {"birth_date": "1985-05-05", "id_expiry": "2036-10-26"})
    c = TestClient(create_app(str(cfg_path)), base_url=LOCAL)

    page = c.get("/people").text
    assert "გაფრთხილებით (1)" in page and "პირადობის მოწმობას ვადა 7 დღეში გასდის" in page
    only = c.get("/people?only=warn").text
    assert "ნინო ბერიძე" in only and "გიორგი მაისურაძე" not in only
    home = c.get("/").text
    assert "1 თანამშრომელზე გაფრთხილებაა" in home and 'href="/people?only=warn"' in home
    repo.close()
