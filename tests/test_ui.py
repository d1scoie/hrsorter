"""ადგილობრივი ვებინტერფეისი: გვერდები, pause/resume, პარამეტრების შენახვა, CSRF/DNS-rebinding დაცვა."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hrsorter import tray
from hrsorter.config import EXAMPLE_TYPES_PATH, load_config
from hrsorter.db import Repository
from hrsorter.ui.app import create_app

LOCAL = "http://127.0.0.1:8765"


@pytest.fixture()
def client(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"paths": {"root": str(tmp_path / "root"), "types_file": "document_types.json"}},
                                   ensure_ascii=False), encoding="utf-8")
    shutil.copyfile(EXAMPLE_TYPES_PATH, tmp_path / "document_types.json")
    Repository(load_config(cfg_path).paths.db).close()
    return TestClient(create_app(str(cfg_path)), base_url=LOCAL), cfg_path


def test_pages_render(client):
    c, _ = client
    for url in ("/", "/documents", "/documents?status=needs_review", "/jobs", "/settings", "/api/status"):
        assert c.get(url).status_code == 200, url
    assert "საცდელი რეჟიმი" in c.get("/").text


def test_pause_resume(client):
    c, cfg_path = client
    assert c.post("/control/pause", headers={"origin": LOCAL}, follow_redirects=False).status_code == 303
    assert c.get("/api/status").json()["paused"] is True
    c.post("/control/resume", headers={"origin": LOCAL})
    assert c.get("/api/status").json()["paused"] is False


def test_foreign_origin_and_host_rejected(client):
    c, _ = client
    assert c.post("/control/pause", headers={"origin": "http://evil.example"}).status_code == 403
    assert c.get("/", headers={"host": "evil.example"}).status_code == 403
    assert c.get("/api/status").json()["paused"] is False


def test_paused_home_does_not_say_the_file_is_being_processed(client):
    c, cfg_path = client
    cfg = load_config(cfg_path)
    repo = Repository(cfg.paths.db)
    sid = repo.insert_source(sha256="a" * 64, size=1, page_count=1, archive_path="x.pdf", first_path="in/x.pdf", profile_id="batch")
    repo.create_job(source_id=sid, profile_id="batch", mode="batch_pdf", config_version=cfg.version, config_snapshot=cfg.snapshot())
    assert "ჩარევა არ სჭირდება" in c.get("/").text
    c.post("/control/pause", headers={"origin": LOCAL})
    home = c.get("/").text
    assert "დამუშავება შეჩერებულია" in home and "ჩარევა არ სჭირდება" not in home
    repo.close()


def test_failed_attempt_followed_by_a_good_one_is_not_a_stuck_file(client):
    """ნამდვილი შემთხვევა: 5 სკანი ორ-ორჯერ ჩავარდა, მესამე ცდაზე დამუშავდა — მთავარ გვერდზე კი „10 ფაილი ვერ დამუშავდა“ ეწერა,
    „სად გაიჭედა“ სია კი ცარიელი იყო. რიცხვი და სია ერთსა და იმავეს ითვლის: სკანებს, რომელთა ბოლო ცდა ჩავარდა."""
    c, cfg_path = client
    cfg = load_config(cfg_path)
    repo = Repository(cfg.paths.db)

    def scan(n: int, *attempts: str) -> None:
        sid = repo.insert_source(sha256=f"{n:x}" * 64, size=1, page_count=1, archive_path=f"{n}.pdf", first_path=f"in/scan-{n}.pdf", profile_id="batch")
        for i, status in enumerate(attempts):
            jid = repo.create_job(source_id=sid, profile_id="batch", mode="batch_pdf", config_version=cfg.version, config_snapshot=cfg.snapshot())
            repo.finish_job(jid, status, error_code="max_attempts" if status == "failed" else None)
            repo.conn.execute("UPDATE jobs SET created_at=? WHERE id=?", (f"2026-01-0{n}T10:00:0{i}+00:00", jid))

    scan(1, "failed", "failed", "done")
    scan(2, "failed", "done")
    assert repo.job_counts() == {"failed": 0, "done": 2}
    home = c.get("/").text
    assert "ვერ დამუშავდა" not in home and c.get("/api/status").json()["jobs"]["failed"] == 0
    assert "არაფერი არ გაჭედილა" in c.get("/stats").text

    scan(3, "done", "failed")                                # ბოლო ცდა ჩავარდა → ეს ერთი ფაილი მართლა გაჭედილია
    assert repo.job_counts()["failed"] == 1 and tray.read_status(repo).errors == 1
    assert "1 ფაილი ვერ დამუშავდა" in c.get("/").text
    stats = c.get("/stats").text
    assert "scan-3.pdf" in stats.split('id="stuck"')[1] and "არაფერი არ გაჭედილა" not in stats
    repo.close()


def test_settings_save_creates_new_version(client):
    c, cfg_path = client
    before = load_config(cfg_path).version
    page = c.get("/settings").text
    assert "batch_pdf" in page
    types_json = json.dumps(json.loads(EXAMPLE_TYPES_PATH.read_text(encoding="utf-8")), ensure_ascii=False)
    form = {"paths.root": str(Path(load_config(cfg_path).data["paths"]["root"])),
            "profile.0.inbox": "Inbox/Batch", "profile.0.mode": "batch_pdf",
            "profile.1.inbox": "Inbox/Single", "profile.1.mode": "batch_pdf",          # რეჟიმის შეცვლა UI-დან
            "default_company.id": "default", "default_company.name": "ნაგულისხმევი", "default_company.folder": "ნაგ",
            "watcher.stability_seconds": "15", "processing.concurrency": "1", "processing.dpi": "300",
            "thresholds.type_auto": "0.8", "thresholds.type_min_margin": "0.25", "thresholds.boundary_auto": "0.8",
            "thresholds.boundary_start": "0.7", "thresholds.boundary_continue": "0.3",
            "processing.trial_mode": "1", "companies_json": "[]", "types_json": types_json}
    r = c.post("/settings", data=form, headers={"origin": LOCAL}, follow_redirects=False)
    assert r.status_code == 303 and "err=" not in r.headers["location"]
    cfg = load_config(cfg_path)
    assert cfg.version != before
    assert cfg.profile("single").mode.value == "batch_pdf"
    assert cfg.section("watcher")["stability_seconds"] == 15.0


def test_settings_cleanup_and_tray(client):
    c, cfg_path = client
    page = c.get("/settings").text
    assert 'name="cleanup.processed_days" value="30"' in page and 'name="paths.processed" value="Processed"' in page
    form = {"types_json": EXAMPLE_TYPES_PATH.read_text(encoding="utf-8"), "processing.trial_mode": "1",
            "paths.processed": "Done", "cleanup.processed_days": "14", "cleanup.uploaded_days": "90",
            "tray.notifications": "1"}   # move_processed მოხსნილია
    r = c.post("/settings", data=form, headers={"origin": LOCAL}, follow_redirects=False)
    assert "err=" not in r.headers["location"]
    cfg = load_config(cfg_path)
    assert cfg.section("cleanup") == {"move_processed": False, "processed_days": 14, "uploaded_days": 90}
    assert cfg.section("tray")["notifications"] is True and cfg.paths.processed.name == "Done"
    form["cleanup.processed_days"] = "-3"
    r = c.post("/settings", data=form, headers={"origin": LOCAL}, follow_redirects=False)
    assert "err=" in r.headers["location"] and load_config(cfg_path).section("cleanup")["processed_days"] == 14


def test_settings_rejects_inline_secret(client):
    c, cfg_path = client
    before = load_config(cfg_path).version
    form = {"companies_json": '[{"id": "x", "name": "x", "api_key": "sk-123"}]',
            "types_json": EXAMPLE_TYPES_PATH.read_text(encoding="utf-8"), "processing.trial_mode": "1"}
    r = c.post("/settings", data=form, headers={"origin": LOCAL}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    assert load_config(cfg_path).version == before


@pytest.mark.parametrize("headers", [
    {"origin": "http://127.0.0.1.evil.example"},       # თავსართით შედარების გვერდის ავლა
    {"origin": "http://localhost.evil.example:8765"},
    {"origin": "http://127.0.0.1:9999"},               # სხვა პორტი
    {},                                                # Origin/Referer-ის გარეშე
    {"origin": LOCAL, "sec-fetch-site": "cross-site"},
])
def test_csrf_bypass_attempts_rejected(client, headers):
    c, _ = client
    assert c.post("/control/pause", headers=headers).status_code == 403
    assert c.get("/api/status").json()["paused"] is False


def test_stats_page_with_processed_data(tmp_path):
    from hrsorter.config import EXAMPLE_CONFIG_PATH
    from hrsorter.pipeline import process_job
    from hrsorter.watcher import InboxScanner
    from hrsorter.worker import Worker
    from tools import synthetic
    cfg_path = tmp_path / "config.json"
    example = json.loads(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))
    cfg_path.write_text(json.dumps({"paths": {"root": str(tmp_path / "root"), "types_file": "document_types.json"},
                                    "watcher": {"stability_seconds": 0}, "companies": example["companies"]},
                                   ensure_ascii=False), encoding="utf-8")
    shutil.copyfile(EXAMPLE_TYPES_PATH, tmp_path / "document_types.json")
    cfg = load_config(cfg_path)
    for d in [*cfg.paths.internal_dirs(), *[p.inbox for p in cfg.profiles]]:
        d.mkdir(parents=True, exist_ok=True)
    synthetic.make_batch(cfg.profile("batch").inbox / "batch.pdf", n_docs=8, seed=3)
    synthetic.make_corrupt(cfg.profile("single").inbox / "broken.pdf")
    repo = Repository(cfg.paths.db)
    sc = InboxScanner(cfg, repo)
    sc.scan_once(); sc.scan_once()
    w = Worker(lambda: cfg, process_job)
    while w.run_once():
        pass
    c = TestClient(create_app(str(cfg_path)), base_url=LOCAL)
    html_ = c.get("/stats").text
    assert "batch.pdf" in html_ and "broken.pdf" in html_ and "pdf_corrupt" in html_
    assert "შემოსული ფაილები" in html_ and "სად გაიჭედა" in html_
    src = repo.one("SELECT id FROM sources")["id"]
    assert c.get(f"/documents?source={src}").status_code == 200
    assert c.get("/documents?year=none").status_code == 200


def test_review_and_doc_pages_render(client):
    c, _ = client
    assert c.get("/review").status_code == 200
    assert "გადასამოწმებელი არაფერია" in c.get("/review").text
    assert c.get("/files").status_code == 200


def test_company_rows_form_creates_companies_and_default(client):
    c, cfg_path = client
    types_json = EXAMPLE_TYPES_PATH.read_text(encoding="utf-8")
    form = {"company.0.name": "შპს „ალფა 2009“", "company.0.aliases": "ალფა 2009, ალფა", "company.0.id": "", "company.0.folder": "",
            "company.1.name": "", "company.1.aliases": "", "company.1.id": "", "company.1.folder": "",
            "types_json": types_json, "processing.trial_mode": "1"}
    r = c.post("/settings", data=form, headers={"origin": LOCAL}, follow_redirects=False)
    assert "err=" not in r.headers["location"]
    cfg = load_config(cfg_path)
    assert [x.name for x in cfg.companies if x.id != cfg.default_company.id] == ["შპს „ალფა 2009“"]
    co = cfg.data["companies"][0]
    assert co["aliases"] == ["ალფა 2009", "ალფა"] and co["folder"] and "„" not in co["folder"]
    form.update({"company.0.id": co["id"], "company.0.folder": co["folder"], "default_company_choice": co["id"]})
    c.post("/settings", data=form, headers={"origin": LOCAL})
    assert load_config(cfg_path).default_company.id == co["id"]


def test_doc_text_page_shows_page_images_and_text(tmp_path):
    from hrsorter.pipeline import process_job
    from hrsorter.watcher import InboxScanner
    from hrsorter.worker import Worker
    from tools import synthetic
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"paths": {"root": str(tmp_path / "root"), "types_file": "document_types.json"},
                                    "watcher": {"stability_seconds": 0}}, ensure_ascii=False), encoding="utf-8")
    shutil.copyfile(EXAMPLE_TYPES_PATH, tmp_path / "document_types.json")
    cfg = load_config(cfg_path)
    for d in [*cfg.paths.internal_dirs(), *[p.inbox for p in cfg.profiles]]:
        d.mkdir(parents=True, exist_ok=True)
    synthetic.make_batch(cfg.profile("batch").inbox / "b.pdf", n_docs=3, seed=5)
    repo = Repository(cfg.paths.db)
    sc = InboxScanner(cfg, repo); sc.scan_once(); sc.scan_once()
    w = Worker(lambda: cfg, process_job)
    while w.run_once():
        pass
    doc = repo.one("SELECT id, page_start FROM documents LIMIT 1")
    c = TestClient(create_app(str(cfg_path)), base_url=LOCAL)
    assert "ამოკითხული ტექსტი" in c.get(f"/doc/{doc['id']}").text
    t = c.get(f"/doc/{doc['id']}/text")
    assert t.status_code == 200 and "ხარისხი" in t.text
    img = c.get(f"/page/{doc['id']}/{doc['page_start']}.png")
    assert img.status_code == 200 and img.content[:4] == b"\x89PNG"
    assert c.get(f"/page/{doc['id']}/999.png").status_code == 404     # დოკუმენტის გარეთ გვერდი — არა


def test_mhr_pages_render_validation_details(client):
    c, cfg_path = client
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data["mhr"] = {"enabled": True}
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    repo = Repository(load_config(cfg_path).paths.db)
    v_ok = {"status": "passed", "employee": {"employee_id": "E1", "first_name": "ნინო", "last_name": "ბერიძე"},
            "checks": [{"field": "last_name", "label_ka": "გვარი", "document_value": "ბერიძე", "mhr_value": "ბერიძე",
                        "status": "match", "blocking": True, "note": ""}], "diffs": [], "summary": ["ყველაფერი ემთხვევა"]}
    v_bad = {"status": "failed", "employee": {"employee_id": "E2", "first_name": "გიორგი", "last_name": "კაპანაძე"},
             "checks": [{"field": "salary", "label_ka": "ხელფასი", "document_value": "2000", "mhr_value": "2200",
                         "status": "mismatch", "blocking": True, "note": ""}],
             "diffs": [{"page": 2, "kind": "changed", "original": "2200 ლარი", "scanned": "2000 ლარი", "significant": True, "reason": "ციფრი"}],
             "original": {"doc_id": "o1", "title": "შრომითი ხელშეკრულება"}, "summary": ["ხელფასი განსხვავდება"]}
    for doc, st, v in (("doc_" + "a" * 32, "ready", v_ok), ("doc_" + "b" * 32, "failed", v_bad)):
        repo.mhr_enqueue(doc, "01001000001")
        repo.mhr_transition(doc, ("pending",), st, validation=v, employee_id=v["employee"]["employee_id"])
    home = c.get("/").text
    assert 'href="/mhr"' in home and "მზადაა MHR-ზე ასატვირთად" in home
    assert "ნინო ბერიძე" in c.get("/mhr?tab=ready").text
    failed = c.get("/mhr?tab=failed").text
    assert "გიორგი კაპანაძე" in failed and "ხელფასი განსხვავდება" in failed
    detail = c.get("/mhr/doc_" + "b" * 32).text
    assert "განსხვავდება" in detail and "2200 ლარი" in detail and "ხელახლა შედარება" in detail
    assert c.get("/mhr/doc_nonexistent").status_code == 404

    # „ატვირთვა მაინც“: ჩანს მხოლოდ ვერ გავლილზე; დასტურის ველის გარეშე არაფერი იცვლება
    bad = "doc_" + "b" * 32
    assert "ატვირთვა მაინც" in detail and "ატვირთვა მაინც" not in c.get("/mhr/doc_" + "a" * 32).text
    c.post(f"/mhr/{bad}/override", data={}, headers={"origin": LOCAL})
    assert repo.mhr_get(bad)["status"] == "failed"
    c.post(f"/mhr/{bad}/override", data={"confirm": "1"}, headers={"origin": LOCAL})
    assert repo.mhr_get(bad)["status"] == "approved" and repo.mhr_get(bad)["decided_by"] == "operator (override)"


def test_doc_page_shows_mhr_result_instead_of_employee_api_warning(client):
    c, cfg_path = client
    repo = Repository(load_config(cfg_path).paths.db)
    sid = repo.insert_source(sha256="c" * 64, size=1, page_count=1, archive_path="x.pdf", first_path="in/x.pdf", profile_id="batch")
    doc = "doc_" + "c" * 32
    repo.upsert_document({"id": doc, "job_id": "j", "source_id": sid, "page_start": 0, "page_end": 0, "status": "proposed",
                          "metadata": {"flags": ["employee_unverified"]}})
    assert "თანამშრომელი არ გადამოწმდა" in c.get(f"/doc/{doc}").text             # MHR გამორთულია — როგორც იყო
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data["mhr"] = {"enabled": True}
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    repo.mhr_enqueue(doc, "01001000001")
    repo.mhr_transition(doc, ("pending",), "ready", validation={"status": "passed"}, employee_id="E1")
    page = c.get(f"/doc/{doc}").text
    assert "თანამშრომელი არ გადამოწმდა" not in page
    assert "თანამშრომელი მოიძებნა · მზადაა ატვირთვისთვის" in page and f'href="/mhr/{doc}"' in page


def test_settings_subscription_ocr_hides_api_key_row(client, tmp_path):
    c, cfg_path = client
    page = c.get("/settings").text                     # ნაგულისხმევი provider = claude_code
    assert "Claude-გამოწერით" in page and "hrsorter/online_ocr" not in page
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data["ocr"] = {"online": {"provider": "anthropic"}}
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    c2 = TestClient(create_app(str(cfg_path)), base_url=LOCAL)
    page = c2.get("/settings").text
    assert "API-გასაღები" in page and "hrsorter/online_ocr" in page


def test_settings_claude_login_status_and_button(client, monkeypatch):
    from hrsorter.ocr import claude_code_vision as cc
    c, _ = client
    monkeypatch.setattr(cc, "auth_status", lambda *a, **k: {"installed": True, "logged_in": True, "email": "x@y.ge",
                                                            "subscription": "max", "error": ""})
    page = c.get("/settings").text
    assert "შესულია: x@y.ge (max)" in page and 'formaction="/settings/claude-login"' in page
    # ღილაკი პარამეტრების ფორმის შიგნითაა: ჩადგმულ <form>-ს ბრაუზერი უგულებელყოფს და „შესვლა“ პარამეტრებს ინახავდა
    inner = page.split('action="/settings">', 1)[1].split("</form>", 1)[0]
    assert "<form" not in inner and "შესვლა" in inner
    monkeypatch.setattr(cc, "auth_status", lambda *a, **k: {"installed": False, "logged_in": False, "email": "",
                                                            "subscription": "", "error": "claude not found"})
    assert "არ არის დაყენებული" in c.get("/settings").text

    launched = []
    monkeypatch.setattr(cc, "launch_login", lambda *a, **k: launched.append(1) or True)
    r = c.post("/settings/claude-login", headers={"origin": LOCAL}, follow_redirects=False)
    assert r.status_code == 303 and launched == [1] and "msg=" in r.headers["location"]
    monkeypatch.setattr(cc, "launch_login", lambda *a, **k: False)
    r = c.post("/settings/claude-login", headers={"origin": LOCAL}, follow_redirects=False)
    assert "err=" in r.headers["location"]


def _pdf_doc(repo, tmp_path, doc_id, sid, **fields):
    """documents-ში ჩანაწერი რეალური (ცარიელი) PDF-ით, რომ ატვირთვამ იმუშაოს."""
    import hashlib
    import io
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(200, 200)
    buf = io.BytesIO()
    w.write(buf)
    pdf = tmp_path / "root" / "Sorted" / f"{doc_id}.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(buf.getvalue())
    row = {"id": doc_id, "job_id": "j", "source_id": sid, "page_start": 0, "page_end": 0, "status": "proposed",
           "type_id": "contract", "personal_number": "01001000001", "output_path": str(pdf),
           "output_sha256": hashlib.sha256(buf.getvalue()).hexdigest()}
    row.update(fields)
    repo.upsert_document(row)


def test_manual_upload_page_flow(client, tmp_path):
    c, cfg_path = client
    assert c.get("/manual").status_code == 200                       # MHR გამორთულია — ფორმა მაინც იხსნება
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    mock = tmp_path / "mhr"
    mock.mkdir()
    (mock / "employees.json").write_text(json.dumps([{"id": "E1", "personal_number": "01001000001", "first_name": "ნინო",
                                                      "last_name": "ბერიძე", "position": "ბუღალტერი"}], ensure_ascii=False),
                                         encoding="utf-8")
    data["mhr"] = {"enabled": True, "mode": "mock", "mock_dir": str(mock)}
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    repo = Repository(load_config(cfg_path).paths.db)
    sid = repo.insert_source(sha256="d" * 64, size=1, page_count=3, archive_path="x.pdf", first_path="in/scan.pdf", profile_id="batch")
    a, b, d = "doc_" + "a" * 32, "doc_" + "b" * 32, "doc_" + "d" * 32
    _pdf_doc(repo, tmp_path, a, sid)                                                   # პ/ნ ემთხვევა, შედარება ვერ გაიარა
    repo.mhr_enqueue(a, "01001000001")
    repo.mhr_transition(a, ("pending",), "failed", validation={"status": "failed"}, employee_id="E1")
    _pdf_doc(repo, tmp_path, b, sid, personal_number=None, type_id="unknown", status="needs_review",   # პ/ნ არ იკითხება, სახელი ემთხვევა
             metadata={"fields": {"first_name": {"value": "ნინოს"}, "last_name": {"value": "ბერიძის"}}})
    _pdf_doc(repo, tmp_path, d, sid)                                                   # უკვე ატვირთულია
    repo.mhr_enqueue(d, "01001000001")
    repo.mhr_transition(d, ("pending",), "uploaded", remote_id="R1")

    assert 'href="/manual"' in c.get("/").text                        # მენიუში ჩანს MHR ჩართულისას
    assert "11 ციფრისგან" in c.get("/manual?pn=123&first=ა&last=ბ").text
    assert "ვერ მოიძებნა" in c.get("/manual?pn=01001000002&first=ნინო&last=ბერიძე").text
    bad = c.get("/manual?pn=01001000001&first=გიორგი&last=ბერიძე").text
    assert "არ ემთხვევა" in bad and "MHR-ში: ნინო ბერიძე" in bad and "აუტვირთავი დოკუმენტები" not in bad
    ok = c.get("/manual?pn=01001000001&first=ნინო&last=ბერიძე").text
    assert "სახელი და გვარი ემთხვევა" in ok and "ბუღალტერი" in ok
    assert "აუტვირთავი დოკუმენტები (1)" in ok and f'value="{a}"' in ok
    assert "სავარაუდო დოკუმენტები სახელით (1)" in ok and f'value="{b}"' in ok and '<select name="type_id"' in ok
    assert "უკვე ატვირთული ან პროცესში (1)" in ok and f'href="/mhr/{d}"' in ok

    q = {"pn": "01001000001", "first": "ნინო", "last": "ბერიძე"}
    r = c.post("/manual/upload", data={**q, "last": "სხვა", "document_ids": a}, headers={"origin": LOCAL}, follow_redirects=False)
    assert r.status_code == 303 and "არ ემთხვევა" in c.get(r.headers["location"]).text
    assert repo.mhr_get(a)["status"] == "failed"                      # არასწორი სახელით არაფერი იცვლება
    r = c.post("/manual/upload", data={**q, "document_ids": a}, headers={"origin": LOCAL}, follow_redirects=False)
    after = c.get(r.headers["location"]).text
    assert "ატვირთულია MHR-ზე: 1 დოკუმენტი" in after and "აუტვირთავი დოკუმენტები (0)" in after
    row = repo.mhr_get(a)
    assert row["status"] == "uploaded" and row["remote_id"] and row["decided_by"] == "operator (manual)"
    ups = json.loads((mock / "uploads.json").read_text(encoding="utf-8"))
    assert len(ups) == 1 and ups[0]["employee_id"] == "E1" and ups[0]["external_id"] == a
    r = c.post("/manual/upload", data={**q, "document_ids": b}, headers={"origin": LOCAL}, follow_redirects=False)
    assert "ჯერ აირჩიე დოკუმენტის ტიპი" in c.get(r.headers["location"]).text
    r = c.post("/manual/upload", data={**q, "document_ids": b, "type_id": "contract"}, headers={"origin": LOCAL}, follow_redirects=False)
    assert "ატვირთულია MHR-ზე: 1 დოკუმენტი" in c.get(r.headers["location"]).text
    assert repo.get_document(b)["type_id"] == "contract" and repo.mhr_get(b)["status"] == "uploaded"
    assert len(json.loads((mock / "uploads.json").read_text(encoding="utf-8"))) == 2
    r = c.post("/manual/upload", data={**q, "document_ids": d}, headers={"origin": LOCAL}, follow_redirects=False)
    assert "უკვე ატვირთულია" in c.get(r.headers["location"]).text
    assert "ხელით ატვირთვა — ავტომატური შედარება გამოტოვებულია" in c.get(f"/mhr/{a}").text


def test_manual_upload_when_mhr_unavailable_keeps_approved(client, tmp_path):
    c, cfg_path = client
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    mock = tmp_path / "mhr"
    mock.mkdir()
    (mock / "employees.json").write_text(json.dumps([{"id": "E1", "personal_number": "01001000001", "first_name": "ნინო",
                                                      "last_name": "ბერიძე"}], ensure_ascii=False), encoding="utf-8")
    data["mhr"] = {"enabled": True, "mode": "mock", "mock_dir": str(mock)}
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    repo = Repository(load_config(cfg_path).paths.db)
    sid = repo.insert_source(sha256="e" * 64, size=1, page_count=1, archive_path="x.pdf", first_path="in/x.pdf", profile_id="batch")
    a = "doc_" + "e" * 32
    _pdf_doc(repo, tmp_path, a, sid)
    (mock / "unavailable").write_text("")
    assert "MHR მიუწვდომელია" in c.get("/manual?pn=01001000001&first=ნინო&last=ბერიძე").text


def test_manual_upload_button_on_document_and_mhr_pages(client, tmp_path):
    """ჩავარდნილი დოკუმენტი: ღილაკი დოკუმენტის/MHR-ის გვერდზე — პ/ნ დოკუმენტიდან, აკრეფის გარეშე."""
    c, cfg_path = client
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    mock = tmp_path / "mhr"
    mock.mkdir()
    (mock / "employees.json").write_text(json.dumps([{"id": "E1", "personal_number": "01001000001", "first_name": "ნინო",
                                                      "last_name": "ბერიძე"}], ensure_ascii=False), encoding="utf-8")
    data["mhr"] = {"enabled": True, "mode": "mock", "mock_dir": str(mock)}
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    repo = Repository(load_config(cfg_path).paths.db)
    sid = repo.insert_source(sha256="f" * 64, size=1, page_count=2, archive_path="x.pdf", first_path="in/x.pdf", profile_id="batch")
    a, b, d = "doc_" + "a" * 32, "doc_" + "b" * 32, "doc_" + "d" * 32
    _pdf_doc(repo, tmp_path, a, sid)                                 # failed, თანამშრომელი ვერ მოიძებნა (override არ ჩანს)
    repo.mhr_enqueue(a, "01001000001")
    v = {"status": "failed", "checks": [{"field": "salary", "label_ka": "ხელფასი", "document_value": "1", "mhr_value": "2",
                                        "status": "mismatch", "blocking": True, "note": ""}], "summary": ["ხელფასი განსხვავდება"]}
    repo.mhr_transition(a, ("pending",), "failed", validation=v)
    _pdf_doc(repo, tmp_path, b, sid, status="needs_review", type_id="unknown")   # რიგში არც ყოფილა, ტიპი უცნობია
    _pdf_doc(repo, tmp_path, d, sid, personal_number="01001000009")             # MHR-ში არ არის
    page_a = c.get(f"/mhr/{a}").text
    assert "ხელით ატვირთვა MHR-ზე" in page_a and f'action="/doc/{a}/manual-upload"' in page_a and "ატვირთვა მაინც" not in page_a
    assert "01001000001" in c.get(f"/doc/{a}").text
    assert '<select name="type_id"' in c.get(f"/doc/{b}").text

    r = c.post(f"/doc/{a}/manual-upload", data={"back": f"/mhr/{a}"}, headers={"origin": LOCAL}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/mhr/{a}?msg=")
    after = c.get(r.headers["location"]).text
    assert "ატვირთულია MHR-ზე — ნინო ბერიძე" in after and "ხელით ატვირთვა MHR-ზე" not in after
    row = repo.mhr_get(a)
    vv = json.loads(row["validation"])
    assert row["status"] == "uploaded" and row["employee_id"] == "E1" and row["decided_by"] == "operator (manual)"
    assert vv["status"] == "manual" and vv["checks"][0]["field"] == "salary" and vv["summary"][1] == "ხელფასი განსხვავდება"
    assert "ხელფასი" in after                                         # ვალიდაციის ანგარიში ატვირთვის შემდეგაც ჩანს

    r = c.post(f"/doc/{b}/manual-upload", data={}, headers={"origin": LOCAL}, follow_redirects=False)
    assert "err=" in r.headers["location"] and "ჯერ აირჩიე დოკუმენტის ტიპი" in c.get(r.headers["location"]).text
    r = c.post(f"/doc/{b}/manual-upload", data={"type_id": "contract"}, headers={"origin": LOCAL}, follow_redirects=False)
    assert "ატვირთულია MHR-ზე" in c.get(r.headers["location"]).text and repo.mhr_get(b)["status"] == "uploaded"
    r = c.post(f"/doc/{d}/manual-upload", data={"back": "http://evil/x"}, headers={"origin": LOCAL}, follow_redirects=False)
    assert r.headers["location"].startswith(f"/doc/{d}?err=") and "ვერ მოიძებნა" in c.get(r.headers["location"]).text
    assert repo.mhr_get(d) is None
    assert c.post("/doc/doc_nope/manual-upload", data={}, headers={"origin": LOCAL}).status_code == 404


def test_only_an_older_original_on_the_card_is_said_plainly(client):
    """რეალური შემთხვევა: ბარათზე მხოლოდ სტაჟირების დროს (ერთი თვით ადრე) დაგენერირებული ფაილია, ქაღალდი კი ახალი
    კომპლექტისაა. ადრე ეს მხოლოდ პატარა შენიშვნაში ჩანდა, სიაში კი „გვერდი 1: …“ — ახლა ზემოთ ცალკე და სიაშიც."""
    c, cfg_path = client
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data["mhr"] = {"enabled": True}
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    repo = Repository(load_config(cfg_path).paths.db)
    note = "ორიგინალი MHR-ში 31 დღით ადრე შეიქმნა (2026-10-02), ვიდრე დოკუმენტის თარიღია (2026-11-02) — დარწმუნდი, რომ სწორი ვერსია დაიბეჭდა"
    v = {"status": "failed", "employee": {"employee_id": "E1", "first_name": "ნინო", "last_name": "ბერიძე"},
         "original": {"doc_id": "550", "filename": "საწყობი - დანართი#2 (სტაჟიორი).docx", "created_at": "2026-10-02T08:43:19Z"},
         "checks": [{"field": "original_date", "label_ka": "ორიგინალის თარიღი", "document_value": "2026-11-02",
                     "mhr_value": "2026-10-02", "status": "info", "blocking": False, "note": note}],
         "diffs": [{"page": 1, "kind": "changed", "significant": True, "reason": "ციფრები განსხვავდება",
                    "original": "2026-10-02", "scanned": "2026-11-02"}], "summary": []}
    doc = "doc_" + "6" * 32
    repo.mhr_enqueue(doc, "01001000001")
    repo.mhr_transition(doc, ("pending",), "failed", validation=v, employee_id="E1")
    page = c.get(f"/mhr/{doc}").text
    banner = page.index("MHR-ში ამ ფაილის მხოლოდ ძველი ვერსიაა")
    assert banner < page.index("ნაბიჯი 1 · ნახე") < page.index("id=decide")                   # ზემოთ, ნაბიჯების წინ
    assert "საწყობი - დანართი#2 (სტაჟიორი)" in page and "დააგენერირე ამ თარიღის ფაილი" in page
    assert page.count(note) == 1 and page.index(note) > page.index("<h2>მონაცემების შედარება")   # მხოლოდ ქვედა ცხრილში
    row = c.get("/mhr?tab=failed").text
    assert "MHR-ში ამ ფაილის მხოლოდ ძველი ვერსიაა (2026-10-02), ქაღალდზე — 2026-11-02 (1 სხვაობა)" in row

    repo.mhr_transition(doc, ("failed",), "uploaded", remote_id="9")                       # გადაწყვეტილების შემდეგ — ისტორიაა
    done = c.get(f"/mhr/{doc}").text
    assert "MHR-ში ამ ფაილის მხოლოდ ძველი ვერსიაა" not in done and note in done
    repo.close()


def test_pages_without_original_do_not_claim_the_text_matches(client, tmp_path):
    """ორიგინალი არ არის: „ტექსტი: ემთხვევა“ და „წესრიგშია“ ტყუილია, შედარება საერთოდ არ მომხდარა."""
    c, cfg_path = client
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data["mhr"] = {"enabled": True}
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    repo = Repository(load_config(cfg_path).paths.db)
    emp = {"employee_id": "E1", "first_name": "ნინო", "last_name": "ბერიძე"}
    no_original = {"status": "failed", "employee": emp, "diffs": [], "summary": ["ორიგინალი ვერ მოიძებნა"],
                   "checks": [{"field": "original_contract", "label_ka": "ორიგინალი MHR-ში", "status": "missing_in_mhr", "blocking": True, "note": "ორიგინალი ვერ მოიძებნა"}]}
    compared = {"status": "passed", "employee": emp, "diffs": [], "summary": ["ყველა შემოწმება გავიდა"], "checks": [],
                "original": {"doc_id": "o1", "filename": "ხელშეკრულება.docx"}}

    sid = repo.insert_source(sha256="e" * 64, size=1, page_count=1, archive_path="x.pdf", first_path="in/x.pdf", profile_id="batch")

    def pages(doc, status, v):
        _pdf_doc(repo, tmp_path, doc, sid)
        repo.save_page_text("j", 0, {"text": "გვერდის ტექსტი", "page_index": 0, "text_source": "ocr:tesseract"})
        repo.mhr_enqueue(doc, "01001000001")
        repo.mhr_transition(doc, ("pending",), status, validation=v, employee_id="E1")
        return c.get(f"/mhr/{doc}").text, c.get(f"/mhr/{doc}/page/1").text

    whole, one = pages("doc_" + "7" * 32, "failed", no_original)
    assert "ტექსტი: არ შედარებულა" in whole and "ტექსტი: ემთხვევა" not in whole
    assert "ამ გვერდზე პრობლემა არ არის" not in one and "ტექსტი არაფერს შედარებია" in one
    whole, one = pages("doc_" + "8" * 32, "ready", compared)
    assert "ტექსტი: ემთხვევა" in whole and "ტექსტი: არ შედარებულა" not in whole
    assert "ამ გვერდზე პრობლემა არ არის" in one
    repo.close()


def test_field_problem_shows_where_it_is_on_paper(client, tmp_path):
    """ხელფასი განსხვავდება: ბარათმა უნდა თქვას, რომელ გვერდზეა და მონიშნოს; ის გვერდი „წესრიგშია“ აღარ არის.
    ამოღებისას გვერდი სკანის ნომრითაა: სკანის მე-15 გვერდი ამ ფაილის პირველია."""
    c, cfg_path = client
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data["mhr"] = {"enabled": True}
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    repo = Repository(load_config(cfg_path).paths.db)
    sid = repo.insert_source(sha256="f" * 64, size=1, page_count=2, archive_path="x.pdf", first_path="in/x.pdf", profile_id="batch")
    doc = "doc_" + "9" * 32
    _pdf_doc(repo, tmp_path, doc, sid, page_start=14, page_end=15,
             metadata={"fields": {"salary": {"value": "1300", "raw": "1300 ლარს", "page": 15}, "position": {"value": "მენეჯერი", "page": 99}}})
    words = [["ხელფასი", 100, 200, 180, 220, 0.9], ["1300", 190, 200, 240, 220, 0.9], ["ლარს", 250, 200, 300, 220, 0.9]]
    repo.save_page_text("j", 14, {"text": "ხელფასი 1300 ლარს", "page_index": 14, "text_source": "ocr:tesseract", "words": words})
    repo.save_page_text("j", 15, {"text": "მეორე გვერდი", "page_index": 15, "text_source": "ocr:tesseract"})
    check = {"field": "salary", "label_ka": "ხელფასი", "document_value": "1300", "mhr_value": "1500.00", "status": "mismatch", "blocking": True,
             "note": "MHR-ის დასაქმების პერიოდი: 2026-11-01 – 2026-12-01"}
    unknown = {"field": "position", "label_ka": "თანამდებობა", "document_value": "მენეჯერი", "mhr_value": "მოლარე", "status": "mismatch", "blocking": True, "note": ""}
    v = {"status": "failed", "employee": {"employee_id": "E1", "first_name": "ნინო", "last_name": "ბერიძე"}, "diffs": [], "checks": [check, unknown],
         "summary": ["ხელფასი განსხვავდება"], "original": {"doc_id": "o1", "filename": "ხელშეკრულება.docx"}}
    repo.mhr_enqueue(doc, "01001000001")
    repo.mhr_transition(doc, ("pending",), "failed", validation=v, employee_id="E1")
    whole = c.get(f"/mhr/{doc}").text
    assert "გვერდი 1 (სკანის გვერდი 15)" in whole and f"/page/{doc}/14.png?dpi=170&crop=" in whole and f'href="/mhr/{doc}/page/1"' in whole
    assert "ქაღალდზე და MHR-ის ჩანაწერში სხვადასხვა მნიშვნელობა წერია" in whole and "MHR-ის დასაქმების პერიოდი" in whole
    assert "დოკუმენტის მონაცემები და MHR-ის ბარათი" in whole          # თანამდებობის გვერდი ფაილში არ არის: ადგილს არ იგონებს
    assert "ხელფასი განსხვავდება" in whole and whole.count("პრობლემა</span>") >= 1
    one = c.get(f"/mhr/{doc}/page/1").text
    assert "ამ გვერდზე პრობლემა არ არის" not in one and "1500.00" in one and 'class="hl' in one
    two = c.get(f"/mhr/{doc}/page/2").text
    assert "ამ გვერდზე პრობლემა არ არის" in two and "1500.00" not in two
    repo.close()


def test_unreachable_mhr_page_claims_nothing_was_checked(client, tmp_path):
    """MHR გათიშულია: „თანამშრომელი ვერ მოიძებნა“ და „ხელმოწერები ადგილზეა“ ტყუილია, შემოწმება ჯერ არ მომხდარა;
    ღილაკი, რომელიც შედარებას აგვერდს უვლის, „ჩარევა არ სჭირდება“ ტექსტის გვერდით არ უნდა იყოს."""
    c, cfg_path = client
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data["mhr"] = {"enabled": True}
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    repo = Repository(load_config(cfg_path).paths.db)
    sid = repo.insert_source(sha256="d" * 64, size=1, page_count=1, archive_path="x.pdf", first_path="in/x.pdf", profile_id="batch")
    doc = "doc_" + "e" * 32
    _pdf_doc(repo, tmp_path, doc, sid)
    repo.save_page_text("j", 0, {"text": "ტექსტი", "page_index": 0, "text_source": "ocr:tesseract", "signature_parties": {"employer": True, "employee": False}})
    repo.mhr_enqueue(doc, "01001000001")
    v = {"status": "unavailable", "employee": None, "checks": [], "diffs": [], "original": None, "error": "MhrUnavailable",
         "summary": ["MHR მიუწვდომელია — მოგვიანებით ხელახლა შემოწმდება"]}
    repo.mhr_transition(doc, ("pending",), "unavailable", validation=v, error="MhrUnavailable")
    page = c.get(f"/mhr/{doc}").text
    assert "MHR ახლა მიუწვდომელია" in page and "ახლავე ცდა" in page
    for s in ("MhrUnavailable", "MHR-ში ვერ მოიძებნა", "ხელმოწერები ადგილზეა", "ხელით ატვირთვა MHR-ზე"):
        assert s not in page, s
    assert "ხელით ატვირთვა MHR-ზე" in c.get(f"/doc/{doc}").text        # დოკუმენტის გვერდიდან გზა ღიაა
    repo.close()


def test_new_token_rechecks_files_that_mhr_refused(client, monkeypatch):
    """ტოკენი ვადაგასულია, 10 ფაილი გაჩერდა: ახალი ტოკენის შემდეგ თითოეულზე ხელით დაჭერა ზედმეტია. სხვა მიზეზით გაჩერებული ფაილი ადგილზე რჩება."""
    from hrsorter import secrets
    saved = {}
    monkeypatch.setattr(secrets, "set_secret", lambda name, value: saved.update({name: value}))
    c, cfg_path = client
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data["mhr"] = {"enabled": True, "auth": {"secret_name": "hrsorter/mhr"}}
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    repo = Repository(load_config(cfg_path).paths.db)
    refused, stopped = "doc_" + "1" * 32, "doc_" + "2" * 32
    repo.mhr_enqueue(refused, "01001000001")
    repo.mhr_transition(refused, ("pending",), "failed", validation={"status": "failed", "error": "MhrError"}, error="MhrError")
    repo.mhr_enqueue(stopped, "01001000002")
    repo.mhr_transition(stopped, ("pending",), "failed", validation={"status": "failed"})
    r = c.post("/settings/secret", data={"name": "hrsorter/employee_api", "value": "x"}, headers={"origin": LOCAL}, follow_redirects=False)
    assert repo.mhr_get(refused)["status"] == "failed" and r.status_code == 303      # სხვა სერვისის გასაღები MHR-ს არ ეხება
    page = c.post("/settings/secret", data={"name": "hrsorter/mhr", "value": "x"}, headers={"origin": LOCAL}).text
    assert saved["hrsorter/mhr"] == "x" and "1 ფაილი თავიდან შემოწმდება" in page
    assert repo.mhr_get(refused)["status"] == "pending" and repo.mhr_get(refused)["error"] is None
    assert repo.mhr_get(stopped)["status"] == "failed"
    repo.close()


def test_refused_token_page_claims_nothing_was_checked(client, tmp_path):
    """MHR-მა ტოკენი არ მიიღო: შედარება არ მომხდარა, AI-საც არაფერი აქვს გასაანალიზებელი და „რიგშია“ არასდროს შესრულდება."""
    import time
    c, cfg_path = client
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data["mhr"] = {"enabled": True}
    data["ocr"] = {"online": {"enabled": True, "provider": "claude_code"}}
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    repo = Repository(load_config(cfg_path).paths.db)
    repo.set_control("worker_heartbeat", str(time.time()))
    sid = repo.insert_source(sha256="c" * 64, size=1, page_count=1, archive_path="x.pdf", first_path="in/x.pdf", profile_id="batch")
    doc = "doc_" + "f" * 32
    _pdf_doc(repo, tmp_path, doc, sid)
    repo.save_page_text("j", 0, {"text": "ტექსტი", "page_index": 0, "text_source": "ocr:tesseract", "signature_parties": {"employer": True, "employee": True}})
    repo.mhr_enqueue(doc, "01001000001")
    v = {"status": "failed", "employee": None, "checks": [], "diffs": [], "original": None, "error": "MhrError",
         "summary": ["MHR-მა ტოკენი არ მიიღო: ტოკენი არასწორია ან ვადაგასულია. ჩაწერე ახალი ტოკენი პარამეტრებში"]}
    repo.mhr_transition(doc, ("pending",), "failed", validation=v, error="MhrError")
    page = c.get(f"/mhr/{doc}").text
    assert "ტოკენი არ მიიღო" in page and "ხელახლა შედარება" in page
    for s in ("MhrError", "MHR-ში ვერ მოიძებნა", "ხელმოწერები ადგილზეა", "(Opus) რიგშია", "ხელით ატვირთვა MHR-ზე"):
        assert s not in page, s
    repo.close()


def test_card_says_honestly_what_the_readers_saw():
    """„ეს წაკითხვის შეცდომა არ არის“ ტყუილი იყო: ნამდვილ გვერდზე Claude-მა შეცდა."""
    from hrsorter.ui.app import _clean_reason, _why_diff
    from hrsorter.validation import RECHECK_DISAGREE, RECHECK_STILL
    both = f"სიტყვა შეცვლილია — {RECHECK_STILL}"
    assert "სხვაობა დარჩა" in _why_diff({"kind": "changed", "reason": both, "significant": True}, {})
    assert "წაკითხვის შეცდომა არ არის" not in _why_diff({"kind": "changed", "reason": both, "significant": True}, {})
    one = f"სიტყვა შეცვლილია — {RECHECK_DISAGREE}"
    text = _why_diff({"kind": "changed", "reason": one, "suspect": True}, {})
    assert "ერთ-ერთი ცდება" in text and "არ აჩერებს" in text and "არ აიხსნება" not in text
    assert "მაინც აჩერებს" in _why_diff({"kind": "changed", "reason": f"ციფრები განსხვავდება — {RECHECK_DISAGREE}", "significant": True}, {})
    assert _clean_reason(one) == "სიტყვა შეცვლილია" and _clean_reason(both) == "სიტყვა შეცვლილია"


def test_handwritten_note_card_shows_what_is_written():
    """Claude-ით წაკითხულ გვერდზე ხელით მიწერილი ტექსტი: ბარათზე ჩანს, რა წერია — „MHR-ის ფაილში წერია —“ აქ უაზროა."""
    from hrsorter.ui.app import _problem_card, _why_diff
    note = {"page": 1, "kind": "handwriting", "original": "", "scanned": "ხელფასი 1800 ლარი", "significant": True, "reason": "ხელით მიწერილი ტექსტი"}
    card = _problem_card("doc_x", note, [{}], "ატვირთე მაინც")
    assert "ხელით წერია" in card and "ხელფასი 1800 ლარი" in card and "MHR-ის ფაილში წერია" not in card
    assert "ამიტომ აჩერებს" in _why_diff(note, {}) and "არ აჩერებს" in _why_diff({**note, "significant": False}, {})
    old = {"page": 1, "kind": "handwriting", "original": "", "scanned": "", "significant": True, "reason": "ხელნაწერის ეჭვი"}
    assert "ნიშანია" in _why_diff(old, {}) and "ხელით წერია" not in _problem_card("doc_x", old, [{}], "ატვირთე მაინც")


def test_page_after_approval_stops_asking_to_upload(client):
    """დადასტურებულ ფაილზე „მზადაა“, „რა ვქნა“ და „ხელახალი AI ანალიზი“ (ღილაკი, რომელიც აღარაფერს აკეთებს) ძველი ტექსტია."""
    c, cfg_path = client
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data["mhr"] = {"enabled": True}
    data["ocr"] = {"online": {"enabled": True, "provider": "claude_code"}}
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    repo = Repository(load_config(cfg_path).paths.db)
    emp = {"employee_id": "E1", "first_name": "ნინო", "last_name": "ბერიძე"}
    clean = {"status": "passed", "employee": emp, "diffs": [], "summary": ["ყველა შემოწმება გავიდა"],
             "checks": [{"field": "already_uploaded", "label_ka": "ორჯერ ატვირთვა", "status": "match", "blocking": False,
                         "note": "ბარათზე ამ ფაილის ატვირთული ასლი არ არის"}]}
    stopped = {"status": "failed", "employee": emp, "checks": [], "summary": ["გვერდი 1: ხელმოწერა აკლია: სტაჟიორი"],
               "diffs": [{"page": 1, "kind": "signature", "original": "", "scanned": "", "significant": True, "reason": "ხელმოწერა აკლია: სტაჟიორი"}]}
    ai = {"verdict": "upload", "confidence": "high", "summary": "დოკუმენტი სწორია", "items": [], "risks": [], "model": "opus"}

    def item(doc, status, v, review=ai):
        repo.mhr_enqueue(doc, "01001000001")
        repo.mhr_transition(doc, ("pending",), status, validation=v, employee_id="E1", remote_id="7" if status == "uploaded" else None)
        repo.mhr_set_ai_review(doc, review)
        return c.get(f"/mhr/{doc}").text

    done = item("doc_" + "1" * 32, "uploaded", clean)
    assert "ყველა შემოწმება გავიდა, ფაილი ატვირთულია." in done and "ატვირთვამდე შემოწმდა: ბარათზე ამ ფაილის ატვირთული ასლი არ არის" in done
    for s in ("მზადაა ატვირთვისთვის", "დააჭირე ატვირთვას", "ხელახალი AI ანალიზი"):
        assert s not in done, s

    forced = item("doc_" + "2" * 32, "uploaded", stopped)
    assert "ფაილი ატვირთულია შენი გადაწყვეტილებით." in forced and "ხელმოწერა აკლია: სტაჟიორი" in forced and "რა ნახა შემოწმებამ" in forced
    for s in ("ფაილი გაჩერდა", "რა ვქნა", "ატვირთვა მაინც", "<h2>რატომ გაჩერდა"):
        assert s not in forced, s

    assert "ფაილი ატვირთვის რიგშია შენი გადაწყვეტილებით." in item("doc_" + "3" * 32, "approved", stopped)

    broken = item("doc_" + "4" * 32, "uploaded", clean, {"verdict": "error", "error": "claude exit 1"})
    assert "claude exit 1" in broken and "ხელახალი AI ანალიზი" not in broken and "სცადე თავიდან" not in broken

    waiting = item("doc_" + "5" * 32, "failed", stopped)      # ვერ გაიარა: ყველაფერი ისევ ადგილზეა
    for s in ("ფაილი გაჩერდა", "რა ვქნა", "ატვირთვა მაინც", "ნაბიჯი 1 · ნახე", "ხელახალი AI ანალიზი", "დააჭირე ატვირთვას"):
        assert s in waiting, s
    assert "მზადაა ატვირთვისთვის" in item("doc_" + "6" * 32, "ready", clean)
    assert "გადაწყვიტე, რა უყო ფაილს" not in done and "გადაწყვიტე, რა უყო ფაილს" in waiting     # ატვირთულზე გადასაწყვეტი აღარაფერია
    repo.close()


def test_failed_page_puts_decision_first_and_groups_repeated_problems(client):
    """რეალური ფაილი: 31 პრობლემა, მათგან 27 ბეჭედი/ხელმოწერა ყველა გვერდზე. გადაწყვეტილება ზემოთაა (ადრე ~15 ეკრანის
    ქვემოთ იყო), ერთნაირი პრობლემები ერთ ჯგუფადაა, ციფრი კი პირველია — როგორც გვერდზე, ისე სიაში."""
    c, cfg_path = client
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data["mhr"] = {"enabled": True}
    data["ocr"] = {"online": {"enabled": True, "provider": "claude_code"}}
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    repo = Repository(load_config(cfg_path).paths.db)
    hand = [{"page": p, "kind": "handwriting", "significant": True, "reason": "ხელნაწერის ეჭვი", "original": "", "scanned": ""}
            for p in range(1, 15)]
    salary = {"page": 6, "kind": "changed", "significant": True, "reason": "ციფრები განსხვავდება",
              "original": "ყოველთვიურად 1300 ლარს", "scanned": "ყოველთვიურად 1500 ლარს"}
    v = {"status": "failed", "employee": {"employee_id": "E1", "first_name": "ნინო", "last_name": "ბერიძე"},
         "checks": [], "diffs": [*hand, salary], "summary": ["გვერდი 1: ხელნაწერის ეჭვი"]}
    doc = "doc_" + "7" * 32
    repo.mhr_enqueue(doc, "01001000001")
    repo.mhr_transition(doc, ("pending",), "failed", validation=v, employee_id="E1")
    repo.mhr_set_ai_review(doc, {"verdict": "check", "confidence": "medium", "summary": "ხელფასი თვალით შეამოწმე.",
                                 "items": [], "risks": [], "model": "opus"})
    page = c.get(f"/mhr/{doc}").text
    look, decide = page.index("ნაბიჯი 1 · ნახე"), page.index("id=decide")                  # ჯერ ნახე, მერე გადაწყვიტე
    assert look < decide < page.index("ატვირთვა მაინც</button>") and decide < page.index("ხელახლა შედარება</button>")
    assert 'href="#ai"' in page[look:decide] and "ხელფასი თვალით შეამოწმე." in page[look:decide]
    assert page.index("ნაბიჯი 2 · გადაწყვიტე") < page.index('<details class="more"')        # დეტალები — ბოლოში, დაკეცილი

    groups = page.split('<details class="card pgroup"')[1:]
    assert len(groups) == 2 and "ციფრი სხვანაირად წერია" in groups[0] and "გვერდზე ხელით რაღაც წერია" in groups[1]
    assert groups[0].startswith(" open") and not groups[1].startswith(" open")        # ციფრი ღიაა, 14 ბეჭედი — დაკეცილი
    assert "14 ადგილი · 14 გვერდზე" in groups[1]
    assert groups[1].count("რა ვქნა") == 1 and groups[1].count('class="prob"') == 14   # საერთო ახსნა ერთხელ, ბარათი თითო ადგილზე
    assert "AI: ცრუ განგაშია" not in page                                              # ძველ ანალიზს თითო ადგილის აზრი არ აქვს

    # AI-ის აზრი თითო ადგილზე — პრობლემის ბარათზევე; დაკეცილ ჯგუფს თავში ჯამური ნიშანი აქვს
    def key(x):
        return {k: x.get(k) for k in ("page", "kind", "original", "scanned")}
    repo.mhr_set_ai_review(doc, {"verdict": "check", "confidence": "medium", "summary": "ხელფასი თვალით შეამოწმე.", "items": [],
                                 "risks": [], "model": "opus",
                                 "findings": [{"key": key(salary), "status": "bad", "explanation": "ხელფასი მართლა 1500 წერია."},
                                              *({"key": key(h), "status": "ok", "explanation": "ეს ბეჭედია."} for h in hand)]})
    groups = c.get(f"/mhr/{doc}").text.split('<details class="card pgroup"')[1:]
    assert "AI: ნამდვილი პრობლემაა.</b> ხელფასი მართლა 1500 წერია." in groups[0]
    assert groups[1].count("AI: ცრუ განგაშია.</b> ეს ბეჭედია.") == 14
    assert '<span class="chip tone-ok">AI: ცრუ განგაშია</span>' in groups[1].split("</summary>")[0]

    row = c.get("/mhr?tab=failed").text
    assert "გვერდი 6: ციფრები განსხვავდება" in row and "გვერდი 1: ხელნაწერის ეჭვი (და კიდევ" not in row
    assert row.count("ნინო ბერიძე") == 1                                              # სახელი მხოლოდ სათაურშია
    repo.close()


def test_home_shows_today_where_things_are_and_recent_scans_by_name(client, tmp_path):
    """მთავარი: „დღეს“ (რა დასკანერდა/აიტვირთა), „სად რა არის“ (გზა + გაჩერებული) და ბოლო სკანები თანამშრომლის სახელით,
    ერთი გასაგები მდგომარეობით და ბმულით იქ, სადაც დაჭერაა საჭირო."""
    c, cfg_path = client
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data["mhr"] = {"enabled": True}
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    cfg = load_config(cfg_path)
    repo = Repository(cfg.paths.db)
    names = {"g": ("გიორგი", "კაპანაძე"), "n": ("ნინო", "ბერიძე")}
    for k, (fn, ln) in names.items():
        sid = repo.insert_source(sha256=k * 64, size=1, page_count=14, archive_path=f"{k}.pdf", first_path=f"in/SKM_{k}.pdf", profile_id="batch")
        jid = repo.create_job(source_id=sid, profile_id="batch", mode="batch_pdf", config_version=cfg.version, config_snapshot=cfg.snapshot())
        repo.conn.execute("UPDATE jobs SET status='done' WHERE id=?", (jid,))
        doc = "doc_" + k * 32
        _pdf_doc(repo, tmp_path, doc, sid, job_id=jid, type_id="contract",
                 metadata=json.dumps({"fields": {"first_name": {"value": fn}, "last_name": {"value": ln}}}, ensure_ascii=False))
        repo.mhr_enqueue(doc, "01001000001")
    repo.mhr_transition("doc_" + "g" * 32, ("pending",), "failed", validation={"status": "failed", "diffs": [], "checks": []})
    from hrsorter.db import now_iso
    repo.mhr_transition("doc_" + "n" * 32, ("pending",), "uploaded", remote_id="9", uploaded_at=now_iso())
    home = c.get("/").text

    assert "<h2>დღეს</h2>" in home and "7 დღეში: 2 ფაილი, 28 გვერდი" in home
    flow = home[home.index('class="card flow"'):]
    assert "MHR-ში ვერ გაიარა" in flow and 'href="/mhr?tab=failed"' in flow and "აიტვირთა" in flow
    recent = home[home.index("<h2>ბოლო სკანები</h2>"):]
    assert "გიორგი კაპანაძე" in recent and "ნინო ბერიძე" in recent                 # სახელი და არა SKM_…
    assert f'href="/mhr/doc_{"g" * 32}"' in recent                                  # პრობლემურ ფაილზე — პირდაპირ MHR გვერდზე
    assert "აიტვირთა MHR-ზე" in recent and "<div class=\"day\">დღეს</div>" in recent
    repo.close()


def test_document_page_without_pdf_says_so_in_plain_words(client, tmp_path):
    c, cfg_path = client
    repo = Repository(load_config(cfg_path).paths.db)
    sid = repo.insert_source(sha256="f" * 64, size=1, page_count=1, archive_path="x.pdf", first_path="in/x.pdf", profile_id="batch")
    doc = "doc_" + "f" * 32
    _pdf_doc(repo, tmp_path, doc, sid)
    assert '<iframe class="pdf"' in c.get(f"/doc/{doc}").text
    Path(repo.get_document(doc)["output_path"]).unlink()                               # ვადის გასვლის გამო წაიშალა
    page = c.get(f"/doc/{doc}").text
    assert "PDF ამ კომპიუტერზე აღარ არის" in page and '<iframe class="pdf"' not in page and "PDF ცალკე" not in page
    repo.close()


def test_mhr_pages_show_ai_review(client):
    c, cfg_path = client
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data["mhr"] = {"enabled": True}
    data["ocr"] = {"online": {"enabled": True, "provider": "claude_code"}}
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    repo = Repository(load_config(cfg_path).paths.db)
    v = {"status": "failed", "employee": {"employee_id": "E2", "first_name": "გიორგი", "last_name": "კაპანაძე"},
         "checks": [{"field": "salary", "label_ka": "ხელფასი", "document_value": "2000", "mhr_value": "2200",
                     "status": "mismatch", "blocking": True, "note": ""}], "diffs": [], "summary": ["ხელფასი განსხვავდება"]}
    doc = "doc_" + "d" * 32
    repo.mhr_enqueue(doc, "01001000001")
    repo.mhr_transition(doc, ("pending",), "failed", validation=v, employee_id="E2")
    page = c.get(f"/mhr/{doc}").text          # ფონური პროცესი არ მუშაობს → „რიგშია" შეცდომაში შეიყვანდა
    assert "AI ანალიზი ვერ დაიწყება — ფონური პროცესი არ მუშაობს" in page and "რიგშია" not in page
    import time
    repo.set_control("worker_heartbeat", str(time.time()))
    page = c.get(f"/mhr/{doc}").text
    assert "AI ანალიზი (Opus) რიგშია" in page and "ქაღალდზე ყველაფერი სწორად წერია" in page

    repo.mhr_set_ai_review(doc, {"verdict": "upload", "confidence": "high", "summary": "ხელფასი სწორად იკითხება, დოკუმენტი სწორია.",
                                 "items": [{"topic": "ხელფასი", "status": "ok", "explanation": "2200 სხვაგან ჩანს"}],
                                 "risks": ["ბოლო გვერდის თარიღი"], "model": "opus"})
    page = c.get(f"/mhr/{doc}").text
    for s in ("AI: ატვირთვა შეიძლება", "მაღალი დარწმუნებულობა", "დოკუმენტი სწორია.", "2200 სხვაგან ჩანს", "ბოლო გვერდის თარიღი",
              "AI-მ სხვაობები გადახედა", "ხელახალი AI ანალიზი"):
        assert s in page, s
    assert "(Opus) რიგშია" not in page
    lst = c.get("/mhr?tab=failed").text
    assert "AI: ატვირთვა შეიძლება" in lst and "დოკუმენტი სწორია." in lst
    # სიაში AI-ის ჩიპი ბმულია დეტალური გვერდის AI ბლოკზე, გვერდით კი „ხელახალი AI ანალიზი“ — სიიდანვე, იმავე ჩანართზე დაბრუნებით
    assert f'<a class="chip tone-ok" href="/mhr/{doc}#ai"' in lst and 'id="ai"' in page
    assert f'action="/mhr/{doc}/ai-review"' in lst and 'name="back" value="/mhr?tab=failed"' in lst

    # ხელახალი ანალიზი — სუფთავდება; სიიდან სიაში ბრუნდება, დეტალური გვერდიდან — დეტალურზე; გარე მისამართი არ მიიღება
    r = c.post(f"/mhr/{doc}/ai-review", data={"back": "/mhr?tab=failed"}, headers={"origin": LOCAL}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/mhr?tab=failed"
    assert repo.mhr_get(doc)["ai_review"] is None
    assert "ხელახალი AI ანალიზი" not in c.get("/mhr?tab=failed").text        # დასკვნა აღარ არის — ღილაკიც არა
    r = c.post(f"/mhr/{doc}/ai-review", headers={"origin": LOCAL}, follow_redirects=False)
    assert r.headers["location"] == f"/mhr/{doc}"
    r = c.post(f"/mhr/{doc}/ai-review", data={"back": "//evil.example/x"}, headers={"origin": LOCAL}, follow_redirects=False)
    assert r.headers["location"] == f"/mhr/{doc}"

    repo.mhr_set_ai_review(doc, {"verdict": "error", "error": "claude not found"})
    assert "AI ანალიზი ვერ შესრულდა" in c.get(f"/mhr/{doc}").text

    # AI გამორთული — ბლოკი საერთოდ არ ჩანს; პარამეტრებში ჩამრთველი არის
    assert 'name="mhr.ai_review.enabled"' in c.get("/settings").text
    data["ocr"]["online"]["enabled"] = False
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert "AI ანალიზი" not in c.get(f"/mhr/{doc}").text
    repo.close()


def test_missing_original_is_a_warning_with_working_manual_upload(client, tmp_path):
    """MHR-ში დაგენერირებული ხელშეკრულება არ არის: შედარება არ ხდება, დოკუმენტი „ვერ გაიარა“-ში გაფრთხილებით რჩება,
    მაგრამ „ხელით ატვირთვა“ იქვეა და მუშაობს (მომხმარებლის წესი: გააფრთხილე, დააწვება — აიტვირთოს)."""
    c, cfg_path = client
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    mock = tmp_path / "mhr2"
    mock.mkdir()
    (mock / "employees.json").write_text(json.dumps([{"id": "E7", "personal_number": "01001000001", "first_name": "ნინო",
                                                      "last_name": "ბერიძე"}], ensure_ascii=False), encoding="utf-8")
    data["mhr"] = {"enabled": True, "mode": "mock", "mock_dir": str(mock)}
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    repo = Repository(load_config(cfg_path).paths.db)
    sid = repo.insert_source(sha256="e" * 64, size=1, page_count=1, archive_path="x.pdf", first_path="in/x.pdf", profile_id="batch")
    doc = "doc_" + "e" * 32
    _pdf_doc(repo, tmp_path, doc, sid)
    note = "ხელშეკრულების ორიგინალი MHR-ში ვერ მოიძებნა: ამ თანამშრომელს დაგენერირებული ხელშეკრულება არ აქვს — ჯერ MHR-ში დააგენერირე"
    v = {"status": "failed", "employee": {"employee_id": "E7", "first_name": "ნინო", "last_name": "ბერიძე"},
         "checks": [{"field": "original_contract", "label_ka": "ორიგინალი MHR-ში", "document_value": None, "mhr_value": None,
                     "status": "missing_in_mhr", "blocking": True, "note": note}], "diffs": [], "summary": [note]}
    repo.mhr_enqueue(doc, "01001000001")
    repo.mhr_transition(doc, ("pending",), "failed", validation=v, employee_id="E7")

    page = c.get(f"/mhr/{doc}").text
    assert "ჯერ MHR-ში დააგენერირე" in page                                  # გაფრთხილება ჩანს
    assert "პროგრამამ ტექსტი ვერ შეადარა" in page and "ატვირთვა მაინც" in page   # ქაღალდის ნახვის შემდეგ — ერთი ატვირთვის გზა
    assert f'action="/doc/{doc}/manual-upload"' not in page                   # მეორე, თითქმის იგივე ღილაკი აბნევდა
    assert "MHR-ში ხელშეკრულება ვერ მოიძებნა" in c.get(f"/doc/{doc}").text   # დოკუმენტის გვერდზე სწორი სათაური
    home = c.get("/").text                                                    # „ვერ გაიარა“ კი არა — MHR-ში აკლია, ცალკე სიით
    assert "1 თანამშრომელს MHR-ში რაღაც აკლია" in home and "ნახე სია" in home and "ვერ გაიარა MHR-ის შემოწმება" not in home
    assert doc not in c.get("/mhr?tab=failed").text and doc in c.get("/mhr?tab=missing").text

    r = c.post(f"/doc/{doc}/manual-upload", data={"back": f"/mhr/{doc}"}, headers={"origin": LOCAL}, follow_redirects=False)
    assert r.status_code == 303 and "ატვირთულია MHR-ზე — ნინო ბერიძე" in c.get(r.headers["location"]).text
    assert repo.mhr_get(doc)["status"] == "uploaded" and repo.mhr_get(doc)["decided_by"] == "operator (manual)"
    repo.close()
