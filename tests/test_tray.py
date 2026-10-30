"""ხატულის ლოგიკა (სტატუსი, ფერი, tooltip, შეტყობინებები) — თავად ხატულის გაშვების გარეშე."""
from __future__ import annotations

import time

from hrsorter import tray
from hrsorter.config import make_config
from hrsorter.db import Repository


def S(**kw):
    base = dict(alive=True, paused=False, queued=0, running=0, review=0, errors=0)
    return tray.Status(**{**base, **kw})


def test_state_priority():
    assert tray.state(S()) == "ok"
    assert tray.state(S(queued=2)) == "busy"
    assert tray.state(S(queued=2, review=1)) == "attention"
    assert tray.state(S(paused=True)) == "attention"
    assert tray.state(S(alive=False, review=3)) == "down"


def test_tooltip_fits_windows_limit_and_shows_counts():
    t = tray.tooltip(S(queued=1, running=1, review=3, errors=1))
    assert len(t) <= 127 and "რიგში 2" in t and "გადასამოწმებელი 3" in t and "შეცდომა 1" in t


def test_notifications_only_on_change():
    assert tray.notifications(None, S(review=5)) == []                       # გაშვებისას ძველზე — არა
    assert tray.notifications(S(review=5), S(review=5)) == []
    titles = [t for t, _ in tray.notifications(S(review=1, errors=0), S(alive=False, review=3, errors=2))]
    assert titles == ["დამმუშავებელი გაჩერდა", "ახალი გადასამოწმებელი", "შეცდომა დამუშავებისას"]
    assert tray.notifications(S(review=3), S(review=1)) == []                 # შემცირება — არა


def test_read_status_from_db(tmp_path):
    cfg = make_config({"paths": {"root": str(tmp_path)}})
    repo = Repository(cfg.paths.db)
    assert tray.read_status(repo).alive is False
    repo.set_control("worker_heartbeat", str(time.time()))
    repo.set_control("paused", "1")
    s = tray.read_status(repo)
    assert s.alive and s.paused and (s.queued, s.review, s.errors) == (0, 0, 0)
    repo.close()


def test_icon_image_per_state():
    for kind in tray.COLORS:
        img = tray.icon_image(kind)
        assert img.size == (64, 64) and img.getpixel((6, 32))[:3] == tray.COLORS[kind]


def test_check_update_reports_newer_or_latest(monkeypatch):
    from hrsorter import updater
    rel = updater.Release("9.9.9", "HRSorter-Setup-9.9.9.exe", "https://dl/x", "0" * 64)
    monkeypatch.setattr(updater, "latest_release", lambda client: rel)
    version, msg = tray.check_update(client=object())
    assert version == "9.9.9" and "9.9.9" in msg
    monkeypatch.setattr(updater, "latest_release", lambda client: None)
    version, msg = tray.check_update(client=object())
    assert version is None and tray.PIPELINE_VERSION in msg


def test_version_changed_marker(tmp_path):
    marker = tmp_path / "v" / "version.txt"
    assert tray.version_changed(marker) is None                  # პირველი გაშვება
    assert tray.version_changed(marker) is None                  # იგივე ვერსია
    marker.write_text("0.0.1", encoding="utf-8")
    assert tray.version_changed(marker) == "0.0.1"               # განახლდა
    assert marker.read_text(encoding="utf-8") == tray.PIPELINE_VERSION


def test_mhr_results_notify_with_name_and_reason():
    prev = S()
    one_ready = S(mhr_ready=1, mhr_latest="მარინა პეტროვი: ყველა შემოწმება გავიდა — მზადაა ატვირთვისთვის")
    assert tray.notifications(prev, one_ready) == [("მზადაა MHR-ზე ასატვირთად", one_ready.mhr_latest)]
    failed = S(mhr_ready=1, mhr_failed=1, mhr_latest="ლინა მაისურაძე: ხელფასი განსხვავდება: დოკუმენტში 1800, MHR-ში 2000")
    assert tray.notifications(one_ready, failed) == [("MHR-თან შედარება: სხვაობაა", failed.mhr_latest)]
    assert tray.notifications(prev, S(mhr_failed=3))[0][1] == "3 დოკუმენტში სხვაობაა — ნახე MHR გვერდზე"
    assert tray.state(S(mhr_ready=1)) == "attention" and "MHR: მზადაა 1" in tray.mhr_line(S(mhr_ready=1))


def test_read_status_mhr_counts_and_latest(tmp_path):
    cfg = make_config({"paths": {"root": str(tmp_path)}})
    repo = Repository(cfg.paths.db)
    repo.mhr_enqueue("d1", "01001000001")
    repo.mhr_transition("d1", ("pending",), "failed", validation={
        "employee": {"first_name": "ლინა", "last_name": "მაისურაძე"}, "summary": ["ხელფასი განსხვავდება"]})
    s = tray.read_status(repo)
    assert (s.mhr_ready, s.mhr_failed) == (0, 1) and s.mhr_latest == "ლინა მაისურაძე: ხელფასი განსხვავდება"
    repo.close()
