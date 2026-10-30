"""მთავარი საქაღალდე ქსელურ დისკზე (მაგ. Z:): ჟურნალი ადგილობრივად, root-ის შეცვლა, დისკის მოლოდინი."""
from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hrsorter import config as config_mod
from hrsorter import worker as worker_mod
from hrsorter.config import EXAMPLE_TYPES_PATH, MACHINE_DATA_DIR, load_config, make_config
from hrsorter.db import Repository
from hrsorter.ui.app import create_app
from hrsorter.worker import Worker

LOCAL = "http://127.0.0.1:8765"


def test_state_is_local_when_root_is_not_a_fixed_disk(tmp_path, monkeypatch):
    assert make_config({"paths": {"root": str(tmp_path)}}).paths.state == tmp_path / "_State"
    monkeypatch.setattr(config_mod, "on_local_disk", lambda p: False)
    p = make_config({"paths": {"root": "Z:/"}}).paths
    assert p.state == MACHINE_DATA_DIR / "state" / "Z" and p.archive == Path("Z:/") / "_Archive"
    assert make_config({"paths": {"root": "Z:/", "state": str(tmp_path / "st")}}).paths.state == tmp_path / "st"


def test_real_drive_detection_for_temp_dir(tmp_path):
    assert config_mod.on_local_disk(tmp_path) is True
    assert config_mod.on_local_disk(Path("relative/dir")) is True


def test_worker_restarts_when_database_location_changes(tmp_path):
    a = make_config({"paths": {"root": str(tmp_path / "a")}})
    b = make_config({"paths": {"root": str(tmp_path / "b")}})
    current = {"cfg": a}
    w = Worker(lambda: current["cfg"], lambda *args: None)
    current["cfg"] = b                       # პარამეტრებიდან შეიცვალა
    t = threading.Thread(target=w.run_forever, args=(threading.Event(),))
    t.start()
    t.join(timeout=30)
    assert not t.is_alive() and w.restart is True


def test_wait_for_root_retries_until_available(tmp_path, monkeypatch):
    calls = {"n": 0}
    real_mkdir = Path.mkdir

    def flaky_mkdir(self, *a, **kw):
        if self == tmp_path / "net":
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("not mapped yet")
        return real_mkdir(self, *a, **kw)

    monkeypatch.setattr(Path, "mkdir", flaky_mkdir)
    monkeypatch.setattr(worker_mod.time, "sleep", lambda s: None)
    worker_mod._wait_for_root(tmp_path / "net")
    assert calls["n"] == 3 and (tmp_path / "net").is_dir()


@pytest.fixture()
def client(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"paths": {"root": str(tmp_path / "root")}}), encoding="utf-8")
    shutil.copyfile(EXAMPLE_TYPES_PATH, tmp_path / "document_types.json")
    return TestClient(create_app(str(cfg_path)), base_url=LOCAL), cfg_path


def _change_root(c, new_root):
    form = {"types_json": EXAMPLE_TYPES_PATH.read_text(encoding="utf-8"), "processing.trial_mode": "1",
            "paths.root": str(new_root)}
    return c.post("/settings", data=form, headers={"origin": LOCAL}, follow_redirects=False).headers["location"]


def test_root_change_blocked_while_queue_busy_then_allowed(client, tmp_path):
    c, cfg_path = client
    repo = Repository(load_config(cfg_path).paths.db)
    sid = repo.insert_source(sha256="a" * 64, size=1, page_count=1, archive_path="x", first_path="x", profile_id="batch")
    jid = repo.create_job(source_id=sid, profile_id="batch", mode="batch_pdf", config_version="v", config_snapshot="{}")
    loc = _change_root(c, tmp_path / "other")
    assert "err=" in loc and load_config(cfg_path).paths.root == tmp_path / "root"
    repo.claim_next_job("t", 60)
    repo.finish_job(jid, "done")
    repo.close()
    loc = _change_root(c, tmp_path / "other")
    assert "err=" not in loc and load_config(cfg_path).paths.root == tmp_path / "other"
