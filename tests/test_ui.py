"""ადგილობრივი ვებინტერფეისი: გვერდები, pause/resume, პარამეტრების შენახვა, CSRF/DNS-rebinding დაცვა."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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
    form = {"company.0.name": "შპს „არბო 2009“", "company.0.aliases": "არბო 2009, არბო", "company.0.id": "", "company.0.folder": "",
            "company.1.name": "", "company.1.aliases": "", "company.1.id": "", "company.1.folder": "",
            "types_json": types_json, "processing.trial_mode": "1"}
    r = c.post("/settings", data=form, headers={"origin": LOCAL}, follow_redirects=False)
    assert "err=" not in r.headers["location"]
    cfg = load_config(cfg_path)
    assert [x.name for x in cfg.companies if x.id != cfg.default_company.id] == ["შპს „არბო 2009“"]
    co = cfg.data["companies"][0]
    assert co["aliases"] == ["არბო 2009", "არბო"] and co["folder"] and "„" not in co["folder"]
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
