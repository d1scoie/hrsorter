"""პირადობის/პასპორტის დაბადების თარიღი და სქესი: სანდო წაკითხვა მანქანური ზოლიდან (საკონტროლო ციფრით), შედარება MHR-ის
ჩანაწერთან, ცარიელი სქესის ჩაწერა და გასწორების შეთავაზება. მონაცემები გამოგონილია."""
from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from hrsorter import mhr_queue as q
from hrsorter.idcard import FEMALE, MALE, mrz_dates, parse_id_card
from hrsorter.mhr_client import MockMhr, employee_from, gender_code, personal_payload
from hrsorter.config import DEFAULTS
from hrsorter.mhr_models import MhrError
from tests.test_identity import id_text, mrz_check
from tests.test_mhr_client import BASE, err, fake_keyring, make_client  # noqa: F401  (fake_keyring — fixture)
from tests.test_packets import PN, add_scan, sweep, validation, world  # noqa: F401  (world — fixture)

TODAY = dt.date(2026, 11, 17)


def zone(birth: str, sex: str, expiry: str) -> str:
    return f"{birth}{mrz_check(birth)}{sex}{expiry}{mrz_check(expiry)}GEO<<<<<<<<<<<4"


# --- წაკითხვა -------------------------------------------------------------------------------------------------------
def test_mrz_dates_need_valid_check_digits():
    assert mrz_dates(zone("900101", "F", "361026"), TODAY) == ("1990-01-01", FEMALE, "2036-10-26")
    bad = zone("900101", "F", "361026").replace("900101", "900102")      # ერთი ციფრი სხვანაირად წაიკითხა
    assert mrz_dates(bad, TODAY) is None


def test_mrz_birth_century_and_unknown_sex():
    assert mrz_dates(zone("070206", "M", "280215"), TODAY) == ("2007-02-06", MALE, "2028-02-15")
    assert mrz_dates(zone("350101", "<", "300101"), TODAY) == ("1935-01-01", None, "2030-01-01")   # 2035 ჯერ არ დამდგარა


def test_passport_zone_is_found_after_number_and_country():
    line = "00AA000004GE0" + zone("900101", "F", "310915")[:15] + "<<<<<<<<<<<<<<02"
    assert mrz_dates(line, TODAY) == ("1990-01-01", FEMALE, "2031-09-15")


def test_birth_date_comes_from_the_zone_when_the_label_was_misread():
    """ნამდვილი შემთხვევა: „დაბადების თარიღის“ ქვეშ მკითხველმა მოქმედების ვადა წაიკითხა."""
    text = id_text().replace("01.01.1990", "26.10.2036")
    card = parse_id_card(text)
    assert card.birth_date == "1990-01-01" and card.birth_sure and card.expiry_date == "2036-10-26"


def test_without_zone_dates_come_from_labels_and_are_not_sure():
    card = parse_id_card(id_text(mrz=False))
    assert card.birth_date == "1990-01-01" and not card.birth_sure


@pytest.mark.parametrize("printed, expect, sure", [
    ("ქალი / F", FEMALE, True), ("მდ / F", FEMALE, True), ("[?] / F", FEMALE, False), ("მამ / M", MALE, True),
    ("ქ / M", None, False),      # ქართული და ლათინური ერთმანეთს ეწინააღმდეგება
])
def test_printed_sex_without_zone(printed, expect, sure):
    card = parse_id_card(id_text(mrz=False).replace("ქალი / F", printed))
    assert card.sex == expect and card.sex_sure is sure


def test_zone_sex_wins_and_printed_conflict_makes_it_unsure():
    assert (c := parse_id_card(id_text())).sex == FEMALE and c.sex_sure
    card = parse_id_card(id_text().replace("ქალი / F", "მამ / M"))
    assert card.sex == FEMALE and not card.sex_sure


# --- MHR-ის კლიენტი ------------------------------------------------------------------------------------------------
def test_employee_fields_and_whether_mhr_provides_them():
    emap = DEFAULTS["mhr"]["employee_map"]
    old = employee_from({"id": 1, "personal_number": PN}, emap)
    assert old.birth_date is None and not old.provides("birth_date") and not old.provides("gender")
    new = employee_from({"id": 1, "personal_number": PN, "birth_date": "1990-01-01T00:00:00Z", "gender": None}, emap)
    assert new.birth_date == "1990-01-01" and new.gender is None and new.provides("birth_date") and new.provides("gender")
    assert gender_code("female") == "F" and gender_code("M") == "M" and gender_code("") is None and gender_code("x") is None


def test_personal_payload_accepts_only_clean_values():
    assert personal_payload({"birth_date": "1990-01-01", "gender": "F"}) == {"birth_date": "1990-01-01", "gender": "female"}
    for bad in ({"birth_date": "01.01.1990"}, {"gender": "female"}, {"salary": "1"}, {}):
        with pytest.raises(MhrError):
            personal_payload(bad)


def test_http_update_is_patch_with_source_and_no_pn_in_url():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"id": "7"})

    make_client(handler).update_employee("7", {"gender": "F"}, source="doc_x")
    req = seen[0]
    assert req.method == "PATCH" and str(req.url) == f"{BASE}/employees/7/personal-data"
    assert json.loads(req.content) == {"gender": "female", "source_external_id": "doc_x"}


def test_http_update_errors_are_codes():
    with pytest.raises(MhrError, match="forbidden"):
        make_client(lambda req: err(403, "forbidden")).update_employee("7", {"gender": "F"})
    with pytest.raises(MhrError, match="not_found"):
        make_client(lambda req: httpx.Response(404)).update_employee("7", {"gender": "F"})


# --- შედარება MHR-თან და ჩაწერა ---------------------------------------------------------------------------------------
def set_employee(w, **fields) -> None:
    p = w.dir / "employees.json"
    rows = json.loads(p.read_text(encoding="utf-8"))
    rows[0].update(fields)
    p.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def employee(w) -> dict:
    return json.loads((w.dir / "employees.json").read_text(encoding="utf-8"))[0]


def uploads(w) -> list:
    p = w.dir / "uploads.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def scan_id(w, **kw) -> None:
    add_scan(w, [(id_text(**kw), {})], doc_id="doc_id", type_id="id_card")
    sweep(w)


def check(w, field):
    return next((c for c in validation(w, "doc_id")["checks"] if c["field"] == field), None)


def test_old_mhr_without_these_fields_changes_nothing(world):
    scan_id(world)
    v = validation(world, "doc_id")
    assert check(world, "birth_date") is None and check(world, "gender") is None and not v.get("suggestions")
    assert world.repo.mhr_get("doc_id")["status"] == "uploaded"


def test_matching_birth_date_and_gender_upload_as_before(world):
    set_employee(world, birth_date="1990-01-01", gender="female")
    scan_id(world)
    assert check(world, "birth_date")["status"] == "match" and check(world, "gender")["status"] == "match"
    assert world.repo.mhr_get("doc_id")["status"] == "uploaded" and not validation(world, "doc_id")["suggestions"]


def test_empty_gender_is_written_by_itself(world):
    set_employee(world, birth_date="1990-01-01", gender=None)
    scan_id(world)
    s = validation(world, "doc_id")["suggestions"]
    assert [(x["field"], x["state"], x["by"]) for x in s] == [("gender", "applied", "auto")]
    assert employee(world)["gender"] == "female"
    assert world.repo.mhr_get("doc_id")["status"] == "uploaded"       # ღია შეთავაზება აღარ არის — თავისით აიტვირთა


def test_wrong_gender_is_offered_not_written(world):
    set_employee(world, birth_date="1990-01-01", gender="male")
    scan_id(world)
    s = validation(world, "doc_id")["suggestions"]
    assert [(x["field"], x["state"], x["auto"]) for x in s] == [("gender", "pending", False)]
    assert employee(world)["gender"] == "male" and check(world, "gender")["status"] == "mismatch"
    assert world.repo.mhr_get("doc_id")["status"] == "ready" and uploads(world) == []   # ოპერატორმა უნდა ნახოს


@pytest.mark.parametrize("in_mhr", ["1991-01-01", None])
def test_wrong_or_empty_birth_date_is_offered(world, in_mhr):
    set_employee(world, birth_date=in_mhr, gender="female")
    scan_id(world)
    s = validation(world, "doc_id")["suggestions"]
    assert [(x["field"], x["value"], x["state"], x["auto"]) for x in s] == [("birth_date", "1990-01-01", "pending", False)]
    assert employee(world)["birth_date"] == in_mhr and world.repo.mhr_get("doc_id")["status"] == "ready"
    assert not check(world, "birth_date")["blocking"]


def test_operator_fix_writes_to_mhr_and_the_card_is_checked_again(world):
    set_employee(world, birth_date="1991-01-01", gender="female")
    scan_id(world)
    assert q.apply_suggestion(world.repo, world.cfg, "doc_id", "birth_date", world.mhr) is None
    assert employee(world)["birth_date"] == "1990-01-01"
    assert world.repo.mhr_get("doc_id")["status"] == "pending"
    sweep(world)
    assert check(world, "birth_date")["status"] == "match" and world.repo.mhr_get("doc_id")["status"] == "uploaded"
    assert q.apply_suggestion(world.repo, world.cfg, "doc_id", "birth_date", world.mhr) == "no_suggestion"


def test_nothing_is_offered_when_the_identity_is_not_confirmed(world):
    set_employee(world, birth_date="1991-01-01", gender=None, last_name="კაპანაძე")
    scan_id(world)
    assert not validation(world, "doc_id").get("suggestions") and employee(world)["gender"] is None
    assert world.repo.mhr_get("doc_id")["status"] == "failed"


def test_unsure_reading_is_compared_but_not_offered(world):
    set_employee(world, birth_date="1991-01-01", gender="female")
    scan_id(world, mrz=False)        # ზოლი არ იკითხება — თარიღი მხოლოდ წარწერიდანაა
    assert not validation(world, "doc_id").get("suggestions")


def test_settings_switch_off_each_check(world):
    world.cfg.data["mhr"]["identity_data"] = {"birth_date": False, "gender": False}
    set_employee(world, birth_date="1991-01-01", gender=None)
    scan_id(world)
    assert check(world, "birth_date") is None and check(world, "gender") is None and employee(world)["gender"] is None
    assert world.repo.mhr_get("doc_id")["status"] == "uploaded"


class RefusingMhr(MockMhr):
    def update_employee(self, employee_id, fields, source=""):
        raise MhrError("forbidden")


def test_failed_write_stays_as_an_offer_with_the_error(world):
    set_employee(world, birth_date="1990-01-01", gender=None)
    world.mhr = RefusingMhr(world.dir)
    scan_id(world)
    s = validation(world, "doc_id")["suggestions"][0]
    assert (s["state"], s["error"]) == ("failed", "forbidden") and employee(world)["gender"] is None
    assert world.repo.mhr_get("doc_id")["status"] == "ready"
    assert q.apply_suggestion(world.repo, world.cfg, "doc_id", "gender", world.mhr) == "forbidden"
