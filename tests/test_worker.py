from __future__ import annotations

import io
import os
import threading
import time
from pathlib import Path

import pytest
from pypdf import PdfWriter

from hrsorter import autostart
from hrsorter.config import make_config
from hrsorter.db import Repository
from hrsorter.errors import JobError
from hrsorter.watcher import InboxScanner
from hrsorter.worker import Worker


class Recorder:
    """ყალბი process_fn: იწერს გამოძახებებს; სურვილისამებრ აგდებს გამონაკლისს."""

    def __init__(self, exc: BaseException | None = None, delay: float = 0.0):
        self.calls: list[str] = []
        self.cfg_versions: list[str] = []
        self.exc = exc
        self.delay = delay
        self.lock = threading.Lock()

    def __call__(self, repo, job, cfg) -> None:
        with self.lock:
            self.calls.append(job["id"])
            self.cfg_versions.append(cfg.version)
        if self.delay:
            time.sleep(self.delay)
        if self.exc is not None:
            raise self.exc


@pytest.fixture
def cfg(tmp_path):
    return make_config({"paths": {"root": str(tmp_path)},
                        "processing": {"max_attempts": 3, "retry_backoff_seconds": 0}})


@pytest.fixture
def repo(cfg):
    r = Repository(cfg.paths.db)
    yield r
    r.close()


def add_job(repo, cfg, n: int = 0) -> str:
    sid = repo.insert_source(sha256=f"{n:064x}", size=1, page_count=1, archive_path=f"x/{n}.pdf",
                             first_path=f"in/{n}.pdf", profile_id="batch")
    return repo.create_job(source_id=sid, profile_id="batch", mode="batch_pdf",
                           config_version=cfg.version, config_snapshot=cfg.snapshot())


def make_worker(cfg, fn) -> Worker:
    return Worker(lambda: cfg, fn, worker_id="test")


def test_processes_job_with_snapshot_config(cfg, repo):
    jid = add_job(repo, cfg)
    fn = Recorder()
    w = make_worker(cfg, fn)
    assert w.run_once() is True
    assert w.run_once() is False
    assert fn.calls == [jid] and fn.cfg_versions == [cfg.version]
    job = repo.get_job(jid)
    assert job["status"] == "done" and job["lease_owner"] is None and job["attempt"] == 1


def test_crash_recovery_requeues_and_processes_once(cfg, repo):
    jid = add_job(repo, cfg)
    repo.claim_next_job("crashed-worker", lease_seconds=-1)   # ავარია: running, lease ვადაგასული
    fn = Recorder()
    w = make_worker(cfg, fn)
    while w.run_once():
        pass
    assert fn.calls == [jid]
    job = repo.get_job(jid)
    assert job["status"] == "done" and job["attempt"] == 2
    attempts = repo.q("SELECT status, error_code FROM job_attempts WHERE job_id=? ORDER BY attempt", (jid,))
    assert [tuple(a) for a in attempts] == [("retry", "interrupted"), ("done", None)]


def test_live_lease_not_stolen_on_start(cfg, repo):
    jid = add_job(repo, cfg)
    repo.claim_next_job("other-live-worker", lease_seconds=600)
    fn = Recorder()
    assert make_worker(cfg, fn).run_once() is False
    assert fn.calls == [] and repo.get_job(jid)["status"] == "running"


def test_crash_after_max_attempts_fails(cfg, repo):
    jid = add_job(repo, cfg)
    repo.claim_next_job("crashed", lease_seconds=-1)
    repo.conn.execute("UPDATE jobs SET attempt=? WHERE id=?", (cfg.section("processing")["max_attempts"], jid))
    fn = Recorder()
    assert make_worker(cfg, fn).run_once() is False
    job = repo.get_job(jid)
    assert job["status"] == "failed" and job["error_code"] == "max_attempts" and fn.calls == []


def test_pause_blocks_claiming(cfg, repo):
    jid = add_job(repo, cfg)
    repo.set_control("paused", "1")
    fn = Recorder()
    w = make_worker(cfg, fn)
    assert w.run_once() is False
    assert repo.get_job(jid)["status"] == "queued" and fn.calls == []
    repo.set_control("paused", "0")
    assert w.run_once() is True and repo.get_job(jid)["status"] == "done"


def test_retryable_job_error_requeues_until_max_attempts(cfg, repo):
    jid = add_job(repo, cfg)
    w = make_worker(cfg, Recorder(JobError("ocr_failed", "provider down", retryable=True)))
    for expected in ("queued", "queued", "failed"):
        assert w.run_once() is True
        assert repo.get_job(jid)["status"] == expected
    job = repo.get_job(jid)
    assert job["error_code"] == "max_attempts" and job["attempt"] == 3
    assert w.run_once() is False


def test_non_retryable_job_error_fails_immediately(cfg, repo):
    jid = add_job(repo, cfg)
    w = make_worker(cfg, Recorder(JobError("pdf_corrupt", "bad xref", retryable=False)))
    assert w.run_once() is True
    job = repo.get_job(jid)
    assert job["status"] == "failed" and job["error_code"] == "pdf_corrupt" and job["attempt"] == 1


def test_unexpected_exception_requeued_as_internal(cfg, repo):
    jid = add_job(repo, cfg)
    w = make_worker(cfg, Recorder(ValueError("secret document text")))
    assert w.run_once() is True
    assert repo.get_job(jid)["status"] == "queued"
    att = repo.one("SELECT error_code, error_detail FROM job_attempts WHERE job_id=?", (jid,))
    assert att["error_code"] == "internal" and att["error_detail"] == "ValueError"
    # დოკუმენტის ტექსტი (გამონაკლისის შეტყობინება) events-ში არ ხვდება
    assert not repo.q("SELECT 1 FROM events WHERE message LIKE '%secret%'")


def test_job_cancelled_during_processing_is_not_overwritten(cfg, repo):
    jid = add_job(repo, cfg)

    def cancel(r, job, c):
        r.conn.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (job["id"],))

    assert make_worker(cfg, cancel).run_once() is True
    assert repo.get_job(jid)["status"] == "cancelled"


def test_lease_renewed_while_processing(tmp_path):
    cfg = make_config({"paths": {"root": str(tmp_path)}, "processing": {"lease_seconds": 0.3}})
    repo = Repository(cfg.paths.db)
    try:
        jid = add_job(repo, cfg)
        seen = []

        def slow(r, job, c):
            seen.append(job["lease_until"])
            time.sleep(0.5)
            seen.append(r.get_job(job["id"])["lease_until"])

        assert make_worker(cfg, slow).run_once()
        assert seen[1] > seen[0]
        assert repo.get_job(jid)["status"] == "done"
    finally:
        repo.close()


def test_no_two_active_jobs_for_one_source(cfg, repo):
    jid = add_job(repo, cfg)
    sid = repo.get_job(jid)["source_id"]
    kw = dict(source_id=sid, profile_id="batch", mode="batch_pdf", config_version=cfg.version,
              config_snapshot=cfg.snapshot())
    assert repo.create_job(**kw) is None
    make_worker(cfg, Recorder()).run_once()
    assert repo.create_job(**kw, reprocess_of=jid) is not None   # დასრულების შემდეგ — აშკარა ხელახალი დამუშავება


def test_run_forever_concurrent_threads_and_heartbeat(tmp_path):
    cfg = make_config({"paths": {"root": str(tmp_path)}, "processing": {"concurrency": 3}})
    repo = Repository(cfg.paths.db)
    try:
        ids = [add_job(repo, cfg, i) for i in range(12)]
        fn = Recorder(delay=0.05)
        stop = threading.Event()
        t = threading.Thread(target=Worker(lambda: cfg, fn).run_forever, args=(stop,))
        t.start()
        try:
            deadline = time.time() + 20
            while time.time() < deadline and repo.one(
                    "SELECT COUNT(*) AS n FROM jobs WHERE status='done'")["n"] < len(ids):
                time.sleep(0.05)
            hb = repo.get_control("worker_heartbeat")
            assert hb is not None and abs(time.time() - float(hb)) < 30
            assert repo.get_control("worker_pid") == str(os.getpid())
        finally:
            stop.set()
            t.join(timeout=20)
        assert not t.is_alive()
        assert sorted(fn.calls) == sorted(ids)            # თითოეული ზუსტად ერთხელ
        assert repo.one("SELECT COUNT(*) AS n FROM jobs WHERE status='done'")["n"] == len(ids)
    finally:
        repo.close()


def test_autostart_install_status_uninstall(tmp_path, cfg):
    startup = tmp_path / "Startup"
    written = autostart.install(tmp_path / "config.json", include_ui=True, startup_dir=startup)
    assert {p.name for p in written} == {"hrsorter-worker.vbs", "hrsorter-ui.vbs", "hrsorter-tray.vbs"}
    text = (startup / "hrsorter-worker.vbs").read_text(encoding="utf-16")
    assert "pythonw.exe" in text and "-m hrsorter.cli worker" in text and "HRSORTER_CONFIG" in text
    assert autostart.status(startup) | {"startup_dir": ""} == {"startup_dir": "", "worker": True, "ui": True, "tray": True}
    assert len(autostart.uninstall(startup)) == 3
    assert autostart.status(startup)["worker"] is False


def _pdf_bytes(i: int) -> bytes:
    w = PdfWriter()
    w.add_blank_page(200, 200)
    w.add_metadata({"/Title": f"doc-{i}"})
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


@pytest.mark.slow
def test_500_single_pdfs_ingested_and_processed(tmp_path):
    cfg = make_config({"paths": {"root": str(tmp_path)}})
    inbox = cfg.profile("single").inbox
    inbox.mkdir(parents=True)
    for i in range(500):
        (inbox / f"scan_{i:03d}.pdf").write_bytes(_pdf_bytes(i))
    repo = Repository(cfg.paths.db)
    try:
        t = [0.0]
        sc = InboxScanner(cfg, repo, clock=lambda: t[0])
        assert len(sc.scan_once()) == 500
        t[0] += 10
        ev = sc.scan_once()
        assert [e["action"] for e in ev].count("ingested") == 500
        assert sc.scan_once() == []
        fn = Recorder()
        w = make_worker(cfg, fn)
        while w.run_once():
            pass
        assert len(fn.calls) == len(set(fn.calls)) == 500
        n = lambda sql: repo.one(sql)["n"]
        assert n("SELECT COUNT(*) AS n FROM sources") == 500
        assert n("SELECT COUNT(DISTINCT sha256) AS n FROM sources") == 500
        assert n("SELECT COUNT(*) AS n FROM jobs WHERE status='done'") == 500
        assert n("SELECT COUNT(*) AS n FROM jobs") == 500
        assert len(list(Path(cfg.paths.archive).rglob("*.pdf"))) == 500
    finally:
        repo.close()


def test_retryable_error_waits_backoff_before_retry(tmp_path):
    cfg = make_config({"paths": {"root": str(tmp_path)}, "processing": {"retry_backoff_seconds": 60}})
    repo = Repository(cfg.paths.db)
    jid = add_job(repo, cfg)
    w = make_worker(cfg, Recorder(JobError("disk_full", "no space", retryable=True)))
    assert w.run_once() is True
    job = repo.get_job(jid)
    assert job["status"] == "queued" and job["not_before"] > time.time() + 50
    assert w.run_once() is False                      # პაუზამდე ხელახლა არ აიღება
    repo.conn.execute("UPDATE jobs SET not_before=? WHERE id=?", (time.time() - 1, jid))
    assert w.run_once() is True


def test_stale_lease_recovered_periodically_not_only_at_start(cfg, repo):
    jid = add_job(repo, cfg)
    w = make_worker(cfg, Recorder())
    assert w.run_once() is True                        # აღდგენა გაშვებისას უკვე შესრულდა
    jid2 = add_job(repo, cfg, n=1)
    repo.claim_next_job("crashed-later", lease_seconds=-1)   # სხვა ინსტანსი დაეცა გაშვების შემდეგ
    assert repo.recover_stale_jobs(3) == [jid2]
    assert repo.get_job(jid2)["status"] == "queued"


def test_restart_takes_back_job_of_crashed_worker_at_once(cfg, repo):
    """ფონური პროცესი ერთადერთია (InstanceLock), ამიტომ ჩართვისას ყველა running სამუშაო დაობლებულია.
    ნამდვილ გათიშვაზე სამუშაო 15 წუთი „4/14“-ზე იდგა და ლოდინი ტყუილი იყო."""
    jid = add_job(repo, cfg)
    repo.claim_next_job("crashed", lease_seconds=900)
    assert repo.recover_stale_jobs(3) == []             # მუშაობისას: სხვისი ცოცხალი lease ხელუხლებელია
    stop = threading.Event()
    stop.set()
    make_worker(cfg, Recorder()).run_forever(stop)
    assert repo.get_job(jid)["status"] == "queued"


def test_crash_after_pipeline_finished_marks_done(cfg, repo):
    jid = add_job(repo, cfg)
    repo.claim_next_job("crashed", lease_seconds=-1)
    repo.conn.execute("UPDATE jobs SET attempt=3, report_path='r.json' WHERE id=?", (jid,))
    repo.recover_stale_jobs(3)
    assert repo.get_job(jid)["status"] == "done"


def test_instance_lock_blocks_second_worker(tmp_path):
    from hrsorter.worker import InstanceLock
    lock = InstanceLock(tmp_path)
    with pytest.raises(RuntimeError):
        InstanceLock(tmp_path)
    lock.release()
    InstanceLock(tmp_path).release()
