"""„MHR-ში აკლია“: სკანი დგას, რადგან MHR-ში ჯერ არ არის ის, რასაც უნდა შეედაროს. პროგრამა ასეთებს ცალკე სიად აჩვენებს
(თანამშრომლების მიხედვით) და თავად ეკითხება MHR-ს, გაჩნდა თუ არა — დაჭერის გარეშე. მონაცემები გამოგონილია."""
from __future__ import annotations

import json
import shutil
import time

from fastapi.testclient import TestClient

from hrsorter import mhr_missing as mm
from hrsorter import mhr_queue as q
from hrsorter.config import EXAMPLE_TYPES_PATH, load_config, make_config
from hrsorter.db import Repository
from hrsorter.ui.app import create_app
from hrsorter.validation import NO_ORIGINAL_NOTE
from tests.test_packets import PN, add_scan, make_docx
from tests.test_stages import INSTR_INTERN, INSTR_PROBATION, RULES, SIGNED, _world

LOCAL = "http://127.0.0.1:8765"
INTERN_FILE = "მენეჯერი (სტაჟიორი) - თანამდებობრივი ინსტრუქცია.docx"
PROBATION_FILE = "მენეჯერი (გამოსაცდელი) - თანამდებობრივი ინსტრუქცია.docx"
LATER = 16 * 60   # თავისით გადამოწმების ინტერვალი (15 წთ) გასულია


def _generate(w, doc_id: str, name: str, pages: list[str]) -> None:
    """HR-მა MHR-ში ფაილი დააგენერირა."""
    (w.dir / "files" / name).write_bytes(make_docx(pages))
    path = w.dir / "documents.json"
    docs = json.loads(path.read_text(encoding="utf-8"))
    docs.append({"id": doc_id, "employee_id": "E1", "category": "contract", "title": "ხელშეკრულება", "filename": name,
                 "created_at": "2026-10-02T10:00:00Z", "document_date": "2026-10-02", "kind": "generated", "signed_at": None})
    path.write_text(json.dumps(docs, ensure_ascii=False), encoding="utf-8")


def _stuck_instruction(tmp_path):
    """გამოსაცდელი ვადის ინსტრუქცია დასკანერდა, MHR-ში კი მხოლოდ სტაჟიორის ვერსიაა."""
    w = _world(tmp_path, {"S1": (INTERN_FILE, INSTR_INTERN)})
    add_scan(w, [(INSTR_PROBATION[0], {}), (INSTR_PROBATION[1], SIGNED)], type_id="job_description")
    q.sweep(w.repo, w.cfg, w.mhr)
    return w


def _item(w, doc_id="doc_scan"):
    m = w.repo.mhr_get(doc_id)
    return m, json.loads(m["validation"] or "{}")


def _missing_check(note: str, field: str = "original_contract") -> dict:
    return {"field": field, "label_ka": "ორიგინალი MHR-ში", "document_value": None, "mhr_value": None,
            "status": "missing_in_mhr", "blocking": True, "note": note}


# --- ამოცნობა და მოკლე ხაზი --------------------------------------------------------------------------------------
def test_kind_tells_what_is_missing_in_mhr():
    emp = {"employee_id": "E1", "first_name": "ნინო", "last_name": "ბერიძე"}
    assert mm.missing_kind({"employee": emp, "checks": [_missing_check("x")]}) == mm.ORIGINAL
    assert mm.missing_kind({"employee": emp, "checks": [_missing_check("x", "start_date")]}) == mm.DATA
    assert mm.missing_kind({"employee": None, "checks": [_missing_check("x", "personal_number")]}) == mm.EMPLOYEE
    # ჩვეულებრივი „ვერ გაიარა“, არაბლოკირებადი შენიშვნა და MHR-ის უარი — აქ არ ხვდება
    assert mm.missing_kind({"employee": emp, "checks": [{"field": "salary", "status": "mismatch", "blocking": True}]}) is None
    assert mm.missing_kind({"employee": emp, "checks": [dict(_missing_check("x", "position"), blocking=False)]}) is None
    assert mm.missing_kind({"error": "MhrError", "checks": [_missing_check("x")]}) is None
    assert mm.missing_kind({}) is None and mm.missing_kind(None) is None


def test_list_line_says_what_to_create_without_the_how_to():
    emp = {"employee_id": "E1"}
    staged = ("MHR-ში ამ თანამშრომლის გამოსაცდელი ვადის თანამდებობრივი ინსტრუქცია ვერ მოიძებნა — ბარათზე მხოლოდ სტაჟიორის ვერსიაა. "
              "ჯერ MHR-ში დააგენერირე, მერე დაბეჭდე")
    line = mm.what_is_missing({"employee": emp, "checks": [_missing_check(staged)]})
    assert line.endswith("მხოლოდ სტაჟიორის ვერსიაა") and "დაბეჭდე" not in line
    line = mm.what_is_missing({"employee": emp, "checks": [_missing_check(NO_ORIGINAL_NOTE)]})
    assert line.endswith("დაგენერირებული ხელშეკრულება არ აქვს") and "დააგენერირე" not in line
    assert mm.what_is_missing({"employee": None, "checks": [_missing_check("x", "personal_number")]}) == mm.NO_EMPLOYEE


# --- თავისით გადამოწმება -----------------------------------------------------------------------------------------
def test_file_generated_in_mhr_is_compared_without_a_click(tmp_path):
    w = _stuck_instruction(tmp_path)
    m, v = _item(w)
    assert m["status"] == "failed" and mm.missing_kind(v) == mm.ORIGINAL and v["mhr_snapshot"]

    assert q.sweep(w.repo, w.cfg, w.mhr)["rechecked"] == 0            # MHR-ში არაფერი შეცვლილა — ფაილი ადგილზეა
    m, _ = _item(w)
    assert m["status"] == "failed" and m["next_try"] > time.time()    # შემდეგი კითხვა — 15 წუთში, არა ყოველ გავლაზე

    _generate(w, "S2", PROBATION_FILE, INSTR_PROBATION)
    assert q.sweep(w.repo, w.cfg, w.mhr)["rechecked"] == 0            # ინტერვალი ჯერ არ გასულა
    counts = q.sweep(w.repo, w.cfg, w.mhr, now=lambda: time.time() + LATER)
    m, v = _item(w)
    assert counts["rechecked"] == 1 and m["status"] == "ready" and v["original"]["doc_id"] == "S2"
    assert w.repo.get_control(q.MISSING_CHECKED_KEY)
    w.repo.close()


def test_check_now_button_does_not_wait_for_the_interval(tmp_path):
    w = _stuck_instruction(tmp_path)
    q.sweep(w.repo, w.cfg, w.mhr)                                     # next_try დაისვა
    _generate(w, "S2", PROBATION_FILE, INSTR_PROBATION)
    assert q.recheck_missing_now(w.repo) == 1
    assert q.sweep(w.repo, w.cfg, w.mhr)["rechecked"] == 1 and _item(w)[0]["status"] == "ready"
    w.repo.close()


def test_unrelated_change_in_mhr_keeps_the_ai_review(tmp_path):
    """MHR-ში სხვა ფაილი გაჩნდა, საჭირო — არა: შედარება თავიდან ეშვება, შედეგი იგივეა, AI ანალიზი ფულს მეორედ არ ხარჯავს."""
    w = _stuck_instruction(tmp_path)
    w.repo.mhr_set_ai_review("doc_scan", {"verdict": "check", "summary": "ორიგინალი MHR-ში არ არის"})
    _generate(w, "R9", "ლიფტით სარგებლობის წესები (სტაჟიორი).docx", RULES)
    before = _item(w)[1]["mhr_snapshot"]
    counts = q.sweep(w.repo, w.cfg, w.mhr)
    m, v = _item(w)
    assert counts["rechecked"] == 1 and m["status"] == "failed" and v["mhr_snapshot"] != before
    assert json.loads(m["ai_review"])["verdict"] == "check"
    assert q.sweep(w.repo, w.cfg, w.mhr, now=lambda: time.time() + LATER)["rechecked"] == 0   # ახალი ანაბეჭდი — აღარ მეორდება
    w.repo.close()


def test_new_result_drops_the_old_ai_review(tmp_path):
    w = _stuck_instruction(tmp_path)
    w.repo.mhr_set_ai_review("doc_scan", {"verdict": "check", "summary": "ორიგინალი MHR-ში არ არის"})
    _generate(w, "S2", PROBATION_FILE, INSTR_PROBATION)
    q.sweep(w.repo, w.cfg, w.mhr)
    m, _ = _item(w)
    assert m["status"] == "ready" and m["ai_review"] is None
    w.repo.close()


def test_item_from_an_older_version_is_rechecked_once(tmp_path):
    """ძველი ვერსიის ჩანაწერს ანაბეჭდი არ აქვს: ერთხელ მოწმდება თავიდან (შეიძლება ფაილი უკვე შექმნეს), მერე — მხოლოდ ცვლილებაზე."""
    w = _stuck_instruction(tmp_path)

    def forget_snapshot():
        v = _item(w)[1]
        v.pop("mhr_snapshot", None)
        w.repo.conn.execute("UPDATE mhr_items SET validation=?, next_try=NULL WHERE document_id='doc_scan'",
                            (json.dumps(v, ensure_ascii=False),))
    forget_snapshot()
    assert q.sweep(w.repo, w.cfg, w.mhr)["rechecked"] == 1 and _item(w)[1]["mhr_snapshot"]
    forget_snapshot()                                                 # რამე გზა ანაბეჭდს ვერ წერს — უსასრულო წრე არ იქნება
    assert q.sweep(w.repo, w.cfg, w.mhr)["rechecked"] == 0
    w.repo.close()


def test_employee_added_to_mhr_starts_the_comparison(tmp_path):
    w = _world(tmp_path, {"S1": (INTERN_FILE, INSTR_INTERN)})
    people = (w.dir / "employees.json").read_text(encoding="utf-8")
    (w.dir / "employees.json").write_text("[]", encoding="utf-8")
    add_scan(w, [(INSTR_PROBATION[0], {}), (INSTR_PROBATION[1], SIGNED)], type_id="job_description")
    q.sweep(w.repo, w.cfg, w.mhr)
    m, v = _item(w)
    assert m["status"] == "failed" and mm.missing_kind(v) == mm.EMPLOYEE

    (w.dir / "employees.json").write_text(people, encoding="utf-8")   # HR-მა თანამშრომელი MHR-ში დაამატა
    assert q.sweep(w.repo, w.cfg, w.mhr)["rechecked"] == 1
    m, v = _item(w)
    assert v["employee"]["employee_id"] == "E1" and mm.missing_kind(v) == mm.ORIGINAL   # ახლა უკვე ფაილი აკლია
    w.repo.close()


def test_mhr_down_postpones_and_touches_nothing(tmp_path):
    w = _stuck_instruction(tmp_path)
    (w.dir / "unavailable").write_text("x", encoding="utf-8")
    before = _item(w)[0]
    assert q.sweep(w.repo, w.cfg, w.mhr)["rechecked"] == 0
    m, _ = _item(w)
    assert m["status"] == "failed" and m["validation"] == before["validation"] and m["updated_at"] == before["updated_at"]
    assert m["next_try"] > time.time()
    w.repo.close()


def test_recheck_can_be_switched_off(tmp_path):
    w = _stuck_instruction(tmp_path)
    off = make_config({"paths": {"root": str(tmp_path)},
                       "mhr": {"enabled": True, "mode": "mock", "mock_dir": str(w.dir), "recheck": {"enabled": False},
                               "missing_recheck_minutes": 0}})
    _generate(w, "S2", PROBATION_FILE, INSTR_PROBATION)
    assert q.sweep(w.repo, off, w.mhr)["rechecked"] == 0 and _item(w)[0]["status"] == "failed"
    w.repo.close()


# --- სია ---------------------------------------------------------------------------------------------------------
def _failed(repo, doc_id: str, pn: str, emp: dict | None, checks: list[dict], type_id: str = "contract") -> None:
    sid = repo.insert_source(sha256=(doc_id * 8)[:64], size=1, page_count=2, archive_path="x.pdf", first_path=f"in/{doc_id}.pdf",
                             profile_id="batch")
    repo.upsert_document({"id": doc_id, "job_id": "j", "source_id": sid, "page_start": 0, "page_end": 1, "status": "proposed",
                          "type_id": type_id, "personal_number": pn})
    repo.mhr_enqueue(doc_id, pn)
    repo.mhr_transition(doc_id, ("pending",), "failed", employee_id=(emp or {}).get("employee_id"),
                        validation={"status": "failed", "employee": emp, "checks": checks, "diffs": [], "summary": []})


def test_tab_lists_people_and_what_each_needs_in_mhr(tmp_path):
    cfg_path = tmp_path / "config.json"
    mock = tmp_path / "mhr"
    mock.mkdir()
    cfg_path.write_text(json.dumps({"paths": {"root": str(tmp_path / "root"), "types_file": "document_types.json"},
                                    "mhr": {"enabled": True, "mode": "mock", "mock_dir": str(mock)}}, ensure_ascii=False),
                        encoding="utf-8")
    shutil.copyfile(EXAMPLE_TYPES_PATH, tmp_path / "document_types.json")
    repo = Repository(load_config(cfg_path).paths.db)
    nino = {"employee_id": "E1", "first_name": "ნინო", "last_name": "ბერიძე"}
    giorgi = {"employee_id": "E2", "first_name": "გიორგი", "last_name": "მაისურაძე"}
    staged = ("MHR-ში ამ თანამშრომლის გამოსაცდელი ვადის თანამდებობრივი ინსტრუქცია ვერ მოიძებნა — ბარათზე მხოლოდ სტაჟიორის ვერსიაა. "
              "ჯერ MHR-ში დააგენერირე")
    _failed(repo, "doc_a", PN, nino, [_missing_check(NO_ORIGINAL_NOTE)])
    _failed(repo, "doc_b", PN, nino, [_missing_check(staged)], type_id="job_description")
    _failed(repo, "doc_c", "01001000002", giorgi, [_missing_check(NO_ORIGINAL_NOTE)])
    _failed(repo, "doc_d", "01001000003", None, [_missing_check("ვერ მოიძებნა", "personal_number")])
    _failed(repo, "doc_e", PN, nino, [{"field": "salary", "label_ka": "ხელფასი", "document_value": "1500", "mhr_value": "1800",
                                       "status": "mismatch", "blocking": True, "note": ""}])
    c = TestClient(create_app(str(cfg_path)), base_url=LOCAL)

    page = c.get("/mhr?tab=missing").text
    assert "3 თანამშრომელი · 4 ფაილი" in page and "ვერ გაიარა (1)" in page and "MHR-ში აკლია (4)" in page
    assert page.count("ნინო ბერიძე") == 1 and "2 ფაილი" in page          # ერთი თანამშრომელი — ერთი სტრიქონი, ორი ფაილით
    assert "გამოსაცდელი ვადის თანამდებობრივი ინსტრუქცია ვერ მოიძებნა — ბარათზე მხოლოდ სტაჟიორის ვერსიაა" in page
    assert mm.NO_EMPLOYEE in page and "01001000003" in page
    assert "ყოველ 15 წუთში თავად ეკითხება MHR-ს" in page and "ახლავე შეამოწმე" in page
    assert all(f'href="/mhr/{d}"' in page for d in ("doc_a", "doc_b", "doc_c", "doc_d")) and "/mhr/doc_e" not in page

    failed = c.get("/mhr?tab=failed").text                               # აქ მხოლოდ ის რჩება, სადაც მართლა რაღაც არ ემთხვევა
    assert "/mhr/doc_e" in failed and "/mhr/doc_a" not in failed

    home = c.get("/").text
    assert "3 თანამშრომელს MHR-ში რაღაც აკლია" in home and "4 სკანი დგას" in home and 'href="/mhr?tab=missing"' in home
    assert "1 ფაილმა ვერ გაიარა MHR-ის შემოწმება" in home

    repo.conn.execute("UPDATE mhr_items SET next_try=?", (time.time() + 900,))
    r = c.post("/mhr-recheck-missing", headers={"origin": LOCAL}, follow_redirects=False)
    assert r.status_code == 303 and "ერთ წუთში შეეკითხება MHR-ს" in c.get(r.headers["location"]).text
    waiting = {x["document_id"]: x["next_try"] for x in repo.q("SELECT document_id, next_try FROM mhr_items")}
    assert waiting["doc_a"] is None and waiting["doc_d"] is None and waiting["doc_e"] is not None   # ჩვეულებრივს არ ეხება
    repo.close()
