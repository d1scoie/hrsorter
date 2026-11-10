"""გვერდი „თანამშრომლები“: ვის რა საბუთი აქვს MHR-ის ბარათზე და რა აკლია. მონაცემები გამოგონილია."""
from __future__ import annotations

import json
import shutil
import time

from fastapi.testclient import TestClient

from hrsorter import people
from hrsorter.config import EXAMPLE_TYPES_PATH, load_config, make_config
from hrsorter.db import Repository
from hrsorter.ui.app import create_app
from tests.test_packets import PN, add_scan
from tests.test_stages import INSTR_INTERN, INSTR_PROBATION, SIGNED, _world

LOCAL = "http://127.0.0.1:8765"
CONTRACT_INTERN = "მენეჯერი (სტაჟიორი) - ხელშეკრულება.docx"
CONTRACT_PROBATION = "მენეჯერი (გამოსაცდელი) - ხელშეკრულება.docx"


def _gen(doc_id: str, type_id: str, stage: str | None, created: str = "2026-10-02T10:00:00Z", signed_at: str | None = None) -> dict:
    return {"doc_id": doc_id, "category": "contract", "created_at": created, "generated": True, "signed_at": signed_at,
            "signed_copy_of": "", "type_id": type_id, "stage": stage}


def _upload(doc_id: str, category: str, signed_copy_of: str = "") -> dict:
    return {"doc_id": doc_id, "category": category, "created_at": "2026-11-01T10:00:00Z", "generated": False, "signed_at": None,
            "signed_copy_of": signed_copy_of, "type_id": None, "stage": None}


def _item(type_id: str, status: str, original_id: str = "", missing: bool = False, doc_id: str = "doc_x") -> dict:
    return {"document_id": doc_id, "status": status, "type_id": type_id, "employee_id": "E1", "original_id": original_id,
            "missing": missing, "name": "ნინო ბერიძე"}


def _states(rows: list[dict]) -> dict[str, str]:
    return {r["label"]: r["state"] for r in rows}


# --- სისრულე ----------------------------------------------------------------------------------------------------
def test_everything_uploaded_is_complete():
    cfg = make_config()
    card = {"docs": [_upload("U1", "id_card"), _upload("U2", "employee_form"),
                     _gen("C1", "contract", "probation"), _upload("U3", "contract", signed_copy_of="C1"),
                     _gen("J1", "job_description", "probation", signed_at="2026-11-02T09:00:00Z")]}
    rows = people.checklist(cfg, card, [])
    assert [r["state"] for r in rows] == [people.UPLOADED] * 4
    assert rows[2]["label"] == "გამოსაცდელი ვადის ხელშეკრულება"                 # ეტაპი ფაილის სახელიდან


def test_nothing_on_the_card_says_what_to_do_for_each():
    cfg = make_config()
    rows = people.checklist(cfg, {"docs": []}, [])
    notes = {r["label"]: r["note"] for r in rows}
    assert notes["პირადობის მოწმობა"] == "არ არის, დაასკანერე" and notes["ხელშეკრულება"] == "MHR-ში არ არის შექმნილი"
    assert {r["state"] for r in rows} == {people.TO_SCAN, people.NOT_IN_MHR}


def test_generated_but_unsigned_file_asks_for_the_signed_scan():
    rows = people.checklist(make_config(), {"docs": [_gen("C1", "contract", "intern")]}, [])
    contract = next(r for r in rows if "ხელშეკრულება" in r["label"])
    assert contract["label"] == "სტაჟიორის ხელშეკრულება" and contract["state"] == people.TO_SCAN
    assert "ხელმოწერილი სკანი არ არის" in contract["note"]


def test_only_the_newest_generated_file_counts():
    """სტაჟიორის ხელშეკრულება ხელმოწერილია, მერე გამოსაცდელი დაგენერირდა: ახლა ის უნდა იყოს ატვირთული."""
    card = {"docs": [_gen("C1", "contract", "intern", "2026-08-01T10:00:00Z", signed_at="2026-08-02T10:00:00Z"),
                     _gen("C2", "contract", "probation", "2026-11-01T10:00:00Z")]}
    contract = [r for r in people.checklist(make_config(), card, []) if "ხელშეკრულება" in r["label"]]
    assert [(r["label"], r["state"]) for r in contract] == [("გამოსაცდელი ვადის ხელშეკრულება", people.TO_SCAN)]


def test_scan_waiting_in_the_program_is_shown_with_its_state_and_link():
    cfg = make_config()
    card = {"docs": [_gen("C1", "contract", "probation")]}
    ready = people.checklist(cfg, card, [_item("contract", "ready", original_id="C1", doc_id="doc_r")])
    row = next(r for r in ready if "ხელშეკრულება" in r["label"])
    assert row["state"] == people.LOCAL and row["document_id"] == "doc_r" and "მზადაა ასატვირთად" in row["note"]

    form = people.checklist(cfg, {"docs": []}, [_item("employee_form", "failed", doc_id="doc_f")])
    row = next(r for r in form if r["label"] == "თანამშრომლის ანკეტა")
    assert row["state"] == people.LOCAL and "შემოწმება ვერ გაიარა" in row["note"]


def test_scan_whose_file_is_missing_in_mhr_replaces_the_old_stage_row():
    """ბარათზე მხოლოდ სტაჟიორის ხელშეკრულებაა, დასკანერდა გამოსაცდელი: ერთი სტრიქონი — სკანი MHR-ის ფაილს ელოდება."""
    card = {"docs": [_gen("C1", "contract", "intern")]}
    rows = [r for r in people.checklist(make_config(), card, [_item("contract", "failed", missing=True, doc_id="doc_m")])
            if "ხელშეკრულება" in r["label"]]
    assert len(rows) == 1 and rows[0]["state"] == people.LOCAL and rows[0]["document_id"] == "doc_m"
    assert "MHR-ში შესადარებელი ფაილი აკლია" in rows[0]["note"]


def test_upload_by_the_program_counts_before_the_card_is_refreshed():
    cfg = make_config()
    stale_card = {"docs": [_gen("C1", "contract", "probation")]}
    rows = people.checklist(cfg, stale_card, [_item("contract", "uploaded", original_id="C1"), _item("id_card", "uploaded")])
    assert _states(rows)["გამოსაცდელი ვადის ხელშეკრულება"] == people.UPLOADED and _states(rows)["პირადობის მოწმობა"] == people.UPLOADED


def test_card_not_fetched_yet_claims_nothing():
    rows = people.checklist(make_config(), None, [])
    assert {r["state"] for r in rows} == {people.UNKNOWN}


def test_required_list_comes_from_settings_and_skips_unknown_types():
    cfg = make_config({"mhr": {"required_type_ids": ["id_card", "no_such_type"]}})
    assert people.required(cfg) == ["id_card"]
    assert [r["label"] for r in people.checklist(cfg, {"docs": []}, [])] == ["პირადობის მოწმობა"]


# --- MHR-ის ბარათის წამოღება ------------------------------------------------------------------------------------
def _scanned_world(tmp_path):
    from hrsorter import mhr_queue as q
    w = _world(tmp_path, {"S1": ("მენეჯერი (სტაჟიორი) - თანამდებობრივი ინსტრუქცია.docx", INSTR_INTERN),
                          "C1": (CONTRACT_INTERN, ["სტაჟირების ხელშეკრულება\nმხარეები თანხმდებიან"])})
    add_scan(w, [(INSTR_PROBATION[0], {}), (INSTR_PROBATION[1], SIGNED)], type_id="job_description")
    q.sweep(w.repo, w.cfg, w.mhr)       # თანამშრომელი MHR-ში მოიძებნა → პროგრამამ იცის ეს ადამიანი
    return w


def test_refresh_stores_the_card_and_respects_the_interval(tmp_path):
    w = _scanned_world(tmp_path)
    assert people.refresh(w.repo, w.cfg, w.mhr) == 1
    data = people.card(w.repo, PN)
    by_id = {d["doc_id"]: d for d in data["docs"]}
    assert data["employee_id"] == "E1" and data["name"] == "ნინო ბერიძე"
    assert by_id["C1"]["type_id"] == "contract" and by_id["C1"]["stage"] == "intern" and by_id["S1"]["type_id"] == "job_description"

    assert people.refresh(w.repo, w.cfg, w.mhr) == 0                                  # ახალია — MHR-ს აღარ აწუხებს
    assert people.refresh(w.repo, w.cfg, w.mhr, now=time.time() + 61 * 60) == 1       # საათი გავიდა
    w.repo.close()


def test_refresh_keeps_the_old_card_when_mhr_is_down_and_can_be_switched_off(tmp_path):
    w = _scanned_world(tmp_path)
    people.refresh(w.repo, w.cfg, w.mhr)
    (w.dir / "unavailable").write_text("x", encoding="utf-8")
    assert people.refresh(w.repo, w.cfg, w.mhr, now=time.time() + 61 * 60) == 0 and people.card(w.repo, PN)["docs"]
    off = make_config({"paths": {"root": str(tmp_path)}, "mhr": {"enabled": True, "mode": "mock", "mock_dir": str(w.dir),
                                                               "card_refresh_minutes": 0}})
    (w.dir / "unavailable").unlink()
    assert people.refresh(w.repo, off, w.mhr, now=time.time() + 61 * 60) == 0
    w.repo.close()


def test_overview_puts_people_with_most_missing_first(tmp_path):
    w = _scanned_world(tmp_path)
    people.refresh(w.repo, w.cfg, w.mhr)
    rows = people.overview(w.repo, w.cfg)
    assert len(rows) == 1 and rows[0]["name"] == "ნინო ბერიძე" and rows[0]["personal_number"] == PN
    states = _states(rows[0]["rows"])
    # პირადობა და ანკეტა არ არის; სტაჟიორის ხელშეკრულება შექმნილია, სკანი არ არის; ინსტრუქციის სკანი MHR-ის ფაილს ელოდება
    assert states == {"პირადობის მოწმობა": people.TO_SCAN, "თანამშრომლის ანკეტა": people.TO_SCAN,
                      "სტაჟიორის ხელშეკრულება": people.TO_SCAN, "თანამდებობრივი ინსტრუქცია": people.LOCAL}
    assert rows[0]["missing"] == 4
    w.repo.close()


# --- გვერდი -----------------------------------------------------------------------------------------------------
def test_people_page_shows_who_lacks_what(tmp_path):
    cfg_path = tmp_path / "config.json"
    mock = tmp_path / "mhr"
    mock.mkdir()
    cfg_path.write_text(json.dumps({"paths": {"root": str(tmp_path / "root"), "types_file": "document_types.json"},
                                    "mhr": {"enabled": True, "mode": "mock", "mock_dir": str(mock)}}, ensure_ascii=False),
                        encoding="utf-8")
    shutil.copyfile(EXAMPLE_TYPES_PATH, tmp_path / "document_types.json")
    repo = Repository(load_config(cfg_path).paths.db)

    def person(doc_id: str, pn: str, emp: dict, card_docs: list[dict]) -> None:
        sid = repo.insert_source(sha256=(doc_id * 8)[:64], size=1, page_count=1, archive_path="x.pdf", first_path=f"in/{doc_id}.pdf",
                                 profile_id="batch")
        repo.upsert_document({"id": doc_id, "job_id": "j", "source_id": sid, "page_start": 0, "page_end": 0, "status": "proposed",
                              "type_id": "id_card", "personal_number": pn})
        repo.mhr_enqueue(doc_id, pn)
        repo.mhr_transition(doc_id, ("pending",), "uploaded", employee_id=emp["employee_id"],
                            validation={"status": "passed", "employee": emp, "checks": [], "diffs": [], "summary": []})
        repo.cache_put(people.CARD_PREFIX + pn, {"employee_id": emp["employee_id"],
                                                 "name": f'{emp["first_name"]} {emp["last_name"]}', "docs": card_docs})

    person("doc_a", PN, {"employee_id": "E1", "first_name": "ნინო", "last_name": "ბერიძე"},
           [_upload("U2", "employee_form"), _gen("C1", "contract", "probation", signed_at="2026-11-02T09:00:00Z"),
            _gen("J1", "job_description", "probation", signed_at="2026-11-02T09:00:00Z")])
    person("doc_b", "01001000002", {"employee_id": "E2", "first_name": "გიორგი", "last_name": "მაისურაძე"},
           [_gen("C2", "contract", "intern")])
    c = TestClient(create_app(str(cfg_path)), base_url=LOCAL)

    page = c.get("/people").text
    assert 'href="/people"' in c.get("/").text                                        # მენიუში ჩანს
    assert "ყველა (2)" in page and "ვისაც აკლია (1)" in page
    assert page.index("გიორგი მაისურაძე") < page.index("ნინო ბერიძე")                 # ვისაც აკლია — ზემოთ
    assert "3 აკლია" in page and "სრულია" in page
    assert "სტაჟიორის ხელშეკრულება" in page and "MHR-ში შექმნილია, ხელმოწერილი სკანი არ არის" in page
    assert "თანამშრომლის ანკეტა</b> — არ არის, დაასკანერე" in page

    only = c.get("/people?only=missing").text
    assert "გიორგი მაისურაძე" in only and "ნინო ბერიძე" not in only
    repo.close()


def test_people_page_is_hidden_when_switched_off(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"paths": {"root": str(tmp_path / "root"), "types_file": "document_types.json"},
                                    "mhr": {"enabled": True, "mode": "mock", "mock_dir": str(tmp_path / "mhr"),
                                            "card_refresh_minutes": 0}}, ensure_ascii=False), encoding="utf-8")
    shutil.copyfile(EXAMPLE_TYPES_PATH, tmp_path / "document_types.json")
    Repository(load_config(cfg_path).paths.db).close()
    c = TestClient(create_app(str(cfg_path)), base_url=LOCAL)
    assert 'href="/people"' not in c.get("/").text
