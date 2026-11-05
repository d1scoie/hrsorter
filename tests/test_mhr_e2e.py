"""MHR ბოლომდე: სკანი → pipeline → ვალიდაცია (ველები + ორიგინალი) → UI-დან დადასტურება → ატვირთვა (mock MHR)."""
from __future__ import annotations

import json
import shutil

import pytest
from fastapi.testclient import TestClient

from hrsorter import mhr_queue
from hrsorter.config import EXAMPLE_CONFIG_PATH, EXAMPLE_TYPES_PATH, load_config
from hrsorter.db import Repository
from hrsorter.pipeline import process_job
from hrsorter.ui.app import create_app
from hrsorter.watcher import InboxScanner
from hrsorter.worker import Worker
from tools import mock_mhr, synthetic

LOCAL = "http://127.0.0.1:8765"


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    base = tmp_path_factory.mktemp("mhr")
    synthetic.main(["--out", str(base / "synth"), "--batch-docs", "30", "--singles", "6", "--seed", "4"])
    gt = json.loads((base / "synth" / "ground_truth.json").read_text(encoding="utf-8"))
    mock_mhr.build(base / "mhr", gt, tamper=2)
    ex = json.loads(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))
    cfg_path = base / "config.json"
    cfg_path.write_text(json.dumps({"paths": {"root": str(base / "root"), "types_file": "document_types.json"},
                                    "watcher": {"stability_seconds": 0}, "companies": ex["companies"],
                                    # სინთეზურ ხელშეკრულებას ხელმოწერა მხოლოდ ბოლო გვერდზე აქვს; აქ ორიგინალთან შედარება მოწმდება
                                    "mhr": {"enabled": True, "mode": "mock", "mock_dir": str(base / "mhr"),
                                            "signature_every_page": False}},
                                   ensure_ascii=False), encoding="utf-8")
    shutil.copyfile(EXAMPLE_TYPES_PATH, base / "document_types.json")
    cfg = load_config(cfg_path)
    for d in [*cfg.paths.internal_dirs(), *[p.inbox for p in cfg.profiles]]:
        d.mkdir(parents=True, exist_ok=True)
    shutil.copy(base / "synth" / "batch.pdf", cfg.profile("batch").inbox)
    repo = Repository(cfg.paths.db)
    sc = InboxScanner(cfg, repo)
    sc.scan_once(); sc.scan_once()
    w = Worker(lambda: cfg, process_job)
    while w.run_once():
        pass
    while repo.mhr_list(("pending",)):
        mhr_queue.sweep(repo, cfg)
    tampered = {t["employee_id"] for t in json.loads((base / "mhr" / "tampered.json").read_text(encoding="utf-8"))}
    return base, cfg_path, cfg, repo, tampered


def _items(repo):
    out = []
    for r in repo.mhr_list():
        d = repo.get_document(r["document_id"])
        out.append((r, d, json.loads(r["validation"] or "{}")))
    return out


def test_tampered_contracts_fail_and_others_pass(world):
    base, cfg_path, cfg, repo, tampered = world
    contracts = [(r, d, v) for r, d, v in _items(repo) if d["type_id"] == "contract" and r["status"] in ("ready", "failed")]
    assert contracts, "no validated contracts"
    for r, d, v in contracts:
        changed = any(x["significant"] for x in v.get("diffs", []))
        assert (r["status"] == "failed") == changed
        assert v["original"]["raw"]["kind"] == "generated" and v["original"]["filename"].endswith(".docx")
        assert not any(x["kind"] == "page_count" for x in v["diffs"])   # docx — დაუნომრავი შედარება
    failed_emps = {r["employee_id"] for r, d, v in contracts if r["status"] == "failed"}
    assert failed_emps and failed_emps <= tampered          # მხოლოდ გაყალბებული ორიგინალები ვარდება
    assert all(not any(x["significant"] for x in v["diffs"]) for r, d, v in contracts if r["status"] == "ready")


def test_approve_from_ui_uploads_exactly_once(world):
    base, cfg_path, cfg, repo, tampered = world
    ready = [r["document_id"] for r in repo.mhr_list(("ready",))]
    assert ready
    c = TestClient(create_app(str(cfg_path)), base_url=LOCAL)
    assert "ყველას ატვირთვა" in c.get("/mhr?tab=ready").text
    assert c.post("/mhr-approve-all", headers={"origin": LOCAL}, follow_redirects=False).status_code == 303
    mhr_queue.sweep(repo, cfg)
    mhr_queue.sweep(repo, cfg)                              # მეორე გავლა ორმაგად აღარ ტვირთავს
    uploads = json.loads((base / "mhr" / "uploads.json").read_text(encoding="utf-8"))
    assert len(uploads) == len(ready)
    assert {repo.mhr_get(d)["status"] for d in ready} == {"uploaded"}
    assert {u["external_id"] for u in uploads} == set(ready) and all(u["kind"] == "uploaded" for u in uploads)
    signed = {u["signed_copy_of"] for u in uploads if u["category"] == "contract"}
    originals = {d["id"]: d for d in json.loads((base / "mhr" / "documents.json").read_text(encoding="utf-8"))}
    assert signed and all(originals[s]["signed_at"] for s in signed)   # ორიგინალი მოინიშნა ხელმოწერილად
    assert "ატვირთულია" in c.get(f"/mhr/{ready[0]}").text


def test_failed_item_can_be_rejected_from_ui(world):
    base, cfg_path, cfg, repo, tampered = world
    failed = repo.mhr_list(("failed",))
    assert failed
    c = TestClient(create_app(str(cfg_path)), base_url=LOCAL)
    doc = failed[0]["document_id"]
    assert "რატომ გაჩერდა" in c.get(f"/mhr/{doc}").text
    c.post(f"/mhr/{doc}/reject", headers={"origin": LOCAL})
    assert repo.mhr_get(doc)["status"] == "rejected"
