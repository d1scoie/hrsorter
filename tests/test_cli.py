"""ინსტალერის `setup` ბრძანება: პირველი დაყენება და განახლება მომხმარებლის პარამეტრების შენარჩუნებით."""
from __future__ import annotations

import json

from hrsorter.cli import main
from hrsorter.config import load_config


def test_setup_first_install_then_upgrade_keeps_user_settings(tmp_path):
    cfg_path = tmp_path / "pd" / "config.json"
    root = tmp_path / "HRScan"
    assert main(["--config", str(cfg_path), "setup", "--root", str(root), "--tesseract-dir", str(tmp_path / "app" / "tesseract")]) == 0
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert data["companies"] == []                                   # გამოგონილი კომპანიები არ გადმოდის
    assert data["ocr"]["tesseract_cmd"].endswith("app/tesseract/tesseract.exe")
    cfg = load_config(cfg_path)
    assert cfg.paths.root == root and cfg.profile("batch").inbox.exists() and cfg.paths.db.exists()

    data["companies"] = [{"id": "x", "name": "შპს X", "folder": "X"}]   # მომხმარებელმა UI-დან შეცვალა
    data["processing"]["trial_mode"] = False
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    main(["--config", str(cfg_path), "setup", "--root", str(tmp_path / "other"), "--tesseract-dir", "D:/new/tesseract"])
    after = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert after["companies"][0]["id"] == "x" and after["processing"]["trial_mode"] is False
    assert after["paths"]["root"] == data["paths"]["root"]           # განახლება root-ს არ ცვლის
    assert after["ocr"]["tessdata_dir"] == "D:/new/tesseract/tessdata"


def _open_with_fake_spawn(tmp_path, monkeypatch):
    """`open --no-browser` ნამდვილი პროცესების გარეშე: რა გაუშვა, ისღა ჩანს."""
    import urllib.request

    import hrsorter.cli as cli
    cfg_path = tmp_path / "pd" / "config.json"
    main(["--config", str(cfg_path), "setup", "--root", str(tmp_path / "HRScan"), "--tesseract-dir", str(tmp_path / "t")])
    spawned = []
    monkeypatch.setattr(cli, "_spawn", lambda *a: spawned.append(a[-1]))

    class _Up:   # UI უკვე პასუხობს — ლოდინის ციკლი მაშინვე მთავრდება
        def close(self):
            pass
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Up())
    return cfg_path, lambda: (spawned.clear(), main(["--config", str(cfg_path), "open", "--no-browser"]), list(spawned))[2]


def test_open_starts_worker_after_update_even_with_fresh_heartbeat(tmp_path, monkeypatch):
    """განახლება: ინსტალერი worker-ს კლავს და წამებში უშვებს `open`-ს. heartbeat ჯერ ახალია, lock კი თავისუფალი —
    worker უნდა ჩაირთოს (ადრე heartbeat-ს ენდობოდა და ახალი სკანები განახლების შემდეგ აღარ მუშავდებოდა)."""
    import time

    from hrsorter.db import Repository
    from hrsorter.worker import InstanceLock
    cfg_path, run_open = _open_with_fake_spawn(tmp_path, monkeypatch)
    cfg = load_config(cfg_path)
    repo = Repository(cfg.paths.db)
    repo.set_control("worker_heartbeat", str(time.time()))      # 2 წამის წინ მოკლული worker-ის კვალი
    repo.close()
    assert "worker" in run_open()

    lock = InstanceLock(cfg.paths.state)                        # worker მართლა მუშაობს — მეორეს არ ვუშვებთ
    try:
        assert "worker" not in run_open()
    finally:
        lock.release()


def test_secret_set_from_clipboard_strips_and_rejects_empty(monkeypatch, capsys):
    import hrsorter.cli as cli
    from hrsorter import secrets
    store = {}
    monkeypatch.setattr(secrets, "set_secret", lambda n, v: store.__setitem__(n, v))
    monkeypatch.setattr(cli, "_read_and_clear_clipboard", lambda: "  tok-abc123\r\n")
    assert main(["secret", "set", "hrsorter/mhr", "--from-clipboard"]) == 0
    assert store == {"hrsorter/mhr": "tok-abc123"}
    assert "tok-abc123" not in capsys.readouterr().out          # მნიშვნელობა ეკრანზე არ იბეჭდება
    monkeypatch.setattr(cli, "_read_and_clear_clipboard", lambda: "   ")
    assert main(["secret", "set", "hrsorter/other", "--from-clipboard"]) == 1
    assert "hrsorter/other" not in store                        # ცარიელი არ ინახება
