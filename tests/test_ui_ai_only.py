"""ეკრანი: პარამეტრების ახალი გადამრთველები, MHR-ის მონაცემის გასწორების ბარათი და „მხოლოდ AI“ რეჟიმის შეტყობინებები."""
from __future__ import annotations

import json

from hrsorter.config import EXAMPLE_TYPES_PATH, load_config
from hrsorter.db import Repository
from tests.test_packets import LOCAL
from tests.test_ui import client  # noqa: F401  (fixture)

DOC = "doc_" + "a" * 32
EMP = {"employee_id": "E1", "personal_number": "01001000001", "first_name": "ნინო", "last_name": "ბერიძე"}
SUGGEST = {"field": "birth_date", "label_ka": "დაბადების თარიღი", "value": "1990-01-01", "value_ka": "1990-01-01",
           "mhr_value": "1991-01-01", "mhr_value_ka": "1991-01-01", "auto": False, "state": "pending"}


def _item(cfg_path, status: str, validation: dict, ai: dict | None = None, mhr: dict | None = None) -> Repository:
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data["mhr"] = {"enabled": True, **(mhr or {})}
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    repo = Repository(load_config(cfg_path).paths.db)
    sid = repo.insert_source(sha256="a" * 64, size=1, page_count=1, archive_path="x.pdf", first_path="in/x.pdf", profile_id="batch")
    repo.upsert_document({"id": DOC, "job_id": "j", "source_id": sid, "page_start": 0, "page_end": 0, "status": "proposed",
                          "type_id": "id_card", "personal_number": "01001000001"})
    repo.mhr_enqueue(DOC, "01001000001")
    repo.mhr_transition(DOC, ("pending",), status, validation={"employee": EMP, **validation}, employee_id="E1")
    if ai:
        repo.mhr_set_ai_review(DOC, ai)
    return repo


def test_settings_show_and_save_the_new_switches(client):
    c, cfg_path = client
    page = c.get("/settings").text
    for name in ("mhr.code_check_stops", "mhr.identity_data.birth_date", "mhr.identity_data.gender"):
        assert f'name="{name}"' in page
    assert 'name="mhr.code_check_stops" value="1" >' in page                   # ნაგულისხმევად გამორთულია
    assert 'name="mhr.identity_data.gender" value="1" checked' in page
    cfg = load_config(cfg_path)
    types_json = json.dumps(json.loads(EXAMPLE_TYPES_PATH.read_text(encoding="utf-8")), ensure_ascii=False)
    form = {"paths.root": cfg.data["paths"]["root"], "companies_json": "[]", "types_json": types_json,
            "mhr.code_check_stops": "1", "mhr.identity_data.birth_date": "1"}
    r = c.post("/settings", data=form, headers={"origin": LOCAL}, follow_redirects=False)
    assert r.status_code == 303 and "err=" not in r.headers["location"]
    m = load_config(cfg_path).data["mhr"]
    assert m["code_check_stops"] is True and m["identity_data"] == {"birth_date": True, "gender": False}


def test_open_suggestion_has_a_fix_button_and_applied_one_is_a_note(client):
    c, cfg_path = client
    repo = _item(cfg_path, "ready", {"status": "passed", "suggestions": [SUGGEST]})
    page = c.get(f"/mhr/{DOC}").text
    assert "პირადობაზე წერია 1990-01-01" in page and "MHR-ში ახლა წერია" in page
    assert f'action="/mhr/{DOC}/fix/birth_date"' in page and "MHR-ში გასწორება" in page
    assert "MHR-ის მონაცემის გასწორება გელოდება" in c.get("/mhr?tab=ready").text
    done = {**SUGGEST, "field": "gender", "label_ka": "სქესი", "value_ka": "მდედრობითი", "mhr_value_ka": None, "state": "applied", "by": "auto"}
    repo.conn.execute("UPDATE mhr_items SET validation=? WHERE document_id=?",
                      (json.dumps({"status": "passed", "employee": EMP, "suggestions": [done]}, ensure_ascii=False), DOC))
    repo.conn.commit()
    page = c.get(f"/mhr/{DOC}").text
    assert "სქესი MHR-ში ჩაიწერა: მდედრობითი" in page and "/fix/gender" not in page
    repo.close()


def test_fix_route_reports_what_mhr_said(client):
    c, cfg_path = client
    repo = _item(cfg_path, "ready", {"status": "passed", "suggestions": [SUGGEST]},
                 mhr={"mode": "mock", "mock_dir": str(cfg_path.parent / "mhr")})   # ცარიელი MHR: თანამშრომელი იქ არ არის
    r = c.post(f"/mhr/{DOC}/fix/birth_date", headers={"origin": LOCAL}, follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]
    assert c.post(f"/mhr/{DOC}/fix/salary", headers={"origin": LOCAL}, follow_redirects=False).status_code == 404
    s = json.loads(repo.mhr_get(DOC)["validation"])["suggestions"][0]
    assert s["state"] == "failed" and s["error"] == "not_found"
    assert "MHR-მა ჩაწერა არ მიიღო" in c.get(f"/mhr/{DOC}").text
    repo.close()


DIFF = {"page": 1, "kind": "changed", "original": "2100 ლარს", "scanned": "2100(ლარს", "significant": True, "reason": "ციფრები განსხვავდება"}


def test_lifted_file_says_ai_checked_and_folds_the_code_remarks(client):
    c, cfg_path = client
    ai = {"verdict": "upload", "confidence": "high", "summary": "ყველაფერი რიგზეა", "items": [], "risks": [], "findings": [],
          "gate": "lifted"}
    repo = _item(cfg_path, "ready", {"status": "failed", "diffs": [DIFF], "checks": []}, ai)
    page = c.get(f"/mhr/{DOC}").text
    assert "AI-მ შეამოწმა: მზადაა ატვირთვისთვის" in page and "ფაილი გაჩერდა" not in page
    assert "პროგრამის შენიშვნები, რომლებიც AI-მ წაკითხვის შეცდომად ჩათვალა (1)" in page
    assert f'action="/mhr/{DOC}/approve"' in page
    assert "პროგრამის შენიშვნები AI-მ წაკითხვის შეცდომად ჩათვალა" in c.get("/mhr?tab=ready").text
    repo.close()


def test_file_stopped_by_ai_shows_what_ai_noticed(client):
    c, cfg_path = client
    ai = {"verdict": "check", "confidence": "medium", "summary": "თარიღი საეჭვოა",
          "items": [{"topic": "სამუშაოს დაწყების თარიღი (გვ. 11)", "status": "warn", "explanation": "ქაღალდზე ნახე თარიღი"},
                    {"topic": "ხელფასი", "status": "ok", "explanation": "ემთხვევა"}],
          "risks": ["გვ. 11: შეადარე თარიღი"], "findings": [], "gate": "stopped"}
    repo = _item(cfg_path, "failed", {"status": "passed", "diffs": [], "checks": []}, ai)
    page = c.get(f"/mhr/{DOC}").text
    assert "AI-მ ფაილი გააჩერა" in page and "ნახე, რა შეამჩნია AI-მ" in page
    assert "სამუშაოს დაწყების თარიღი (გვ. 11)" in page and "გვ. 11: შეადარე თარიღი" in page
    assert f'action="/mhr/{DOC}/override"' in page and "AI-მ რაც შეამჩნია, ქაღალდზე ვნახე" in page
    assert "AI-მ გააჩერა" in c.get("/mhr?tab=failed").text
    repo.close()


def test_approve_all_skips_files_ai_has_not_seen_and_open_suggestions(client):
    c, cfg_path = client
    repo = _item(cfg_path, "ready", {"status": "passed", "suggestions": [SUGGEST]},
                 {"verdict": "upload", "summary": "ok", "items": [], "risks": [], "findings": []})
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data["ocr"] = {"online": {"enabled": True, "provider": "claude_code"}}
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    c.post("/mhr-approve-all", headers={"origin": LOCAL}, follow_redirects=False)
    assert repo.mhr_get(DOC)["status"] == "ready"              # ღია შეთავაზება — ერთად ატვირთვაში არ შედის
    repo.conn.execute("UPDATE mhr_items SET validation=?, ai_review=NULL WHERE document_id=?",
                      (json.dumps({"status": "passed", "employee": EMP}), DOC))
    repo.conn.commit()
    c.post("/mhr-approve-all", headers={"origin": LOCAL}, follow_redirects=False)
    assert repo.mhr_get(DOC)["status"] == "ready"              # AI-ს ჯერ არ უნახავს
    repo.mhr_set_ai_review(DOC, {"verdict": "upload", "summary": "ok"})
    repo.conn.commit()
    c.post("/mhr-approve-all", headers={"origin": LOCAL}, follow_redirects=False)
    assert repo.mhr_get(DOC)["status"] == "approved"
    repo.close()
