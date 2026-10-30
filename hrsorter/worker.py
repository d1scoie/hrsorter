"""ფონური დამმუშავებელი: რიგიდან სამუშაოს აღება (lease), დამუშავება, დასრულება/ხელახლა რიგში დაბრუნება.

ავარიის/გადატვირთვის შემდეგ ვადაგასული lease-ის მქონე `running` სამუშაოები რიგში ბრუნდება
(`max_attempts`-ის შემდეგ — failed). ყოველი ნაკადი საკუთარ Repository-ს ხსნის.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import socket
import sqlite3
import sys
import threading
import time
import traceback
import uuid
from typing import Callable

from .config import Config, load_config
from .db import Repository
from .errors import JobError
from .models import ErrorCode, JobStatus
from .watcher import InboxWatcher

log = logging.getLogger(__name__)

ProcessFn = Callable[[Repository, sqlite3.Row, Config], None]   # pipeline.process_job

HEARTBEAT_SECONDS = 5.0
IDLE_SLEEP_SECONDS = 1.0
CLEANUP_SECONDS = 30.0
ROOT_WAIT_SECONDS = 10.0


class Worker:
    def __init__(self, cfg_loader: Callable[[], Config], process_fn: ProcessFn, worker_id: str | None = None):
        self.cfg_loader = cfg_loader
        self.process_fn = process_fn
        self.cfg = cfg_loader()
        self.db_path = self.cfg.paths.db
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self._repo: Repository | None = None   # run_once-ისთვის (გამომძახებლის ნაკადში)
        self.restart = False   # ბაზის ადგილი შეიცვალა (ახალი root) — main ახალ worker-ს გაუშვებს

    # --- აღდგენა ---------------------------------------------------------------
    def _recover(self, repo: Repository) -> None:
        ids = repo.recover_stale_jobs(int(self.cfg.section("processing")["max_attempts"]))
        if ids:
            repo.log_event("recovery", f"recovered {len(ids)} interrupted job(s)", level="warning")
        try:
            from .output import recover_publishing
        except ImportError:
            return
        try:
            n = recover_publishing(repo, self.cfg)
            if n:
                repo.log_event("recovery", f"completed {n} interrupted publish(es)", level="warning")
        except Exception:
            log.exception("recover_publishing failed")

    # --- ერთი სამუშაო ------------------------------------------------------------
    def run_once(self) -> bool:
        """ერთი სამუშაოს დამუშავება; True — თუ რამე დამუშავდა. პირველ გამოძახებაზე აღდგენაც სრულდება."""
        if self._repo is None:
            self._repo = Repository(self.db_path)
            self._recover(self._repo)
        return self._process_one(self._repo, self.worker_id)

    def _process_one(self, repo: Repository, owner: str) -> bool:
        if repo.is_paused():
            return False
        proc = self.cfg.section("processing")
        lease = float(proc["lease_seconds"])
        max_attempts = int(proc["max_attempts"])
        job = repo.claim_next_job(owner, lease)
        if job is None:
            return False
        job_id = job["id"]
        stop_hb = threading.Event()
        hb = threading.Thread(target=self._heartbeat, args=(job_id, owner, lease, stop_hb),
                              name=f"lease-{job_id[-6:]}", daemon=True)
        hb.start()
        try:
            self.process_fn(repo, job, Config.from_snapshot(job["config_snapshot"]))
        except JobError as e:
            if self._lease_lost(repo, job_id, owner):
                return True
            if not e.retryable:
                repo.finish_job(job_id, JobStatus.FAILED.value, error_code=e.code, error_detail=e.detail[:500])
            elif job["attempt"] < max_attempts:
                repo.requeue_job(job_id, error_code=e.code, error_detail=e.detail[:500],
                                 delay_seconds=self._backoff(job["attempt"]))
            else:
                repo.finish_job(job_id, JobStatus.FAILED.value, error_code=ErrorCode.MAX_ATTEMPTS.value,
                                error_detail=f"last error: {e.code}")
            repo.log_event("job_error", f"{e.code} (attempt {job['attempt']})", level="error", job_id=job_id)
        except Exception as e:
            # traceback მხოლოდ ტექნიკურ ჟურნალში; events-ში — მხოლოდ გამონაკლისის ტიპი
            # შეტყობინება შეიძლება დოკუმენტის მონაცემს შეიცავდეს — ვწერთ მხოლოდ ტიპს და stack-ს
            log.error("job %s: unexpected %s\n%s", job_id, type(e).__name__,
                      "".join(traceback.format_tb(e.__traceback__)))
            if self._lease_lost(repo, job_id, owner):
                return True
            detail = type(e).__name__
            if job["attempt"] < max_attempts:
                repo.requeue_job(job_id, error_code=ErrorCode.INTERNAL.value, error_detail=detail,
                                 delay_seconds=self._backoff(job["attempt"]))
            else:
                repo.finish_job(job_id, JobStatus.FAILED.value, error_code=ErrorCode.MAX_ATTEMPTS.value,
                                error_detail=f"last error: internal ({detail})")
            repo.log_event("job_error", f"internal {detail} (attempt {job['attempt']})", level="error", job_id=job_id)
        else:
            if not self._lease_lost(repo, job_id, owner):
                repo.finish_job(job_id, JobStatus.DONE.value)
        finally:
            stop_hb.set()
            hb.join()
        return True

    def _backoff(self, attempt: int) -> float:
        """დროებითი შეცდომის შემდეგ პაუზა: base·2^(attempt-1), მაქსიმუმ 30 წთ."""
        base = float(self.cfg.section("processing").get("retry_backoff_seconds", 60))
        return min(base * 2 ** max(0, attempt - 1), 1800.0)

    @staticmethod
    def _lease_lost(repo: Repository, job_id: str, owner: str) -> bool:
        """სამუშაო დამუშავებისას სხვამ შეცვალა (გაუქმდა / lease სხვას გადაეცა) — მის სტატუსს არ ვეხებით."""
        cur = repo.get_job(job_id)
        lost = cur is None or cur["status"] != JobStatus.RUNNING.value or cur["lease_owner"] != owner
        if lost:
            log.warning("job %s changed while processing; result not recorded", job_id)
        return lost

    def _heartbeat(self, job_id: str, owner: str, lease: float, stop: threading.Event) -> None:
        repo: Repository | None = None   # კავშირი მხოლოდ პირველ განახლებაზე (მოკლე სამუშაოს არ სჭირდება)
        try:
            while not stop.wait(lease / 3):
                try:
                    repo = repo or Repository(self.db_path)
                    repo.renew_lease(job_id, owner, lease)
                except sqlite3.Error:
                    log.warning("lease renew failed for %s", job_id, exc_info=True)
        finally:
            if repo is not None:
                repo.close()

    # --- მუდმივი მუშაობა -----------------------------------------------------------
    def _loop(self, stop_event: threading.Event, owner: str) -> None:
        repo = Repository(self.db_path)
        try:
            while not stop_event.is_set():
                try:
                    worked = self._process_one(repo, owner)
                except Exception:
                    log.exception("worker loop error")
                    worked = False
                if not worked:
                    stop_event.wait(IDLE_SLEEP_SECONDS)
        finally:
            repo.close()

    def _mhr_sweep(self, cfg: Config) -> None:
        """MHR რიგის ერთი გავლა საკუთარი კავშირით; შეცდომა worker-ს არ აჩერებს (ჟურნალში — მხოლოდ ტიპი)."""
        from . import mhr_queue
        repo: Repository | None = None
        try:
            repo = Repository(self.db_path)
            mhr_queue.sweep(repo, cfg)
        except Exception as e:
            log.error("mhr sweep failed: %s\n%s", type(e).__name__, "".join(traceback.format_tb(e.__traceback__)))
        finally:
            if repo is not None:
                repo.close()

    def _cleanup_sweep(self, cfg: Config) -> None:
        """ინბოქსიდან გადატანა და ვადაგასულის წაშლა (housekeeping); შეცდომა worker-ს არ აჩერებს."""
        from . import housekeeping
        repo: Repository | None = None
        try:
            repo = Repository(self.db_path)
            housekeeping.sweep(repo, cfg)
        except Exception as e:
            log.error("cleanup sweep failed: %s\n%s", type(e).__name__, "".join(traceback.format_tb(e.__traceback__)))
        finally:
            if repo is not None:
                repo.close()

    def run_forever(self, stop_event: threading.Event) -> None:
        """`processing.concurrency` ნაკადი + InboxWatcher; stop_event-ზე მიმდინარე სამუშაოს დასრულების შემდეგ ჩერდება."""
        self.cfg = self.cfg_loader()
        repo = Repository(self.db_path)
        self._recover(repo)
        watcher = InboxWatcher(self.cfg, self.db_path, cfg_loader=self.cfg_loader)
        watcher.start()
        n = int(self.cfg.section("processing")["concurrency"])
        threads = [threading.Thread(target=self._loop, args=(stop_event, f"{self.worker_id}#{i}"),
                                    name=f"worker-{i}") for i in range(n)]
        for t in threads:
            t.start()
        log.info("worker %s started (%d thread(s))", self.worker_id, n)
        mhr_thread: threading.Thread | None = None
        next_mhr = 0.0
        cleanup_thread: threading.Thread | None = None
        next_cleanup = 0.0
        try:
            while True:
                try:   # მთავარი საქაღალდე პარამეტრებიდან შეიცვალა → ახალი ბაზა; ძველში აღარ ვწერთ
                    if self.cfg_loader().paths.db != self.db_path:
                        log.info("database location changed; restarting worker")
                        self.restart = True
                        break
                except Exception as e:
                    log.warning("config reload failed: %s", type(e).__name__)
                # MHR რიგი (მიმდინარე კონფიგურაციით) ცალკე ნაკადში — გრძელი ვალიდაცია/ატვირთვა heartbeat-ს არ აჩერებს
                if time.time() >= next_mhr and not (mhr_thread and mhr_thread.is_alive()):
                    next_mhr = time.time() + 60.0
                    try:
                        cur = self.cfg_loader()
                        next_mhr = time.time() + float(cur.section("mhr").get("sweep_seconds", 60))
                        if cur.section("mhr").get("enabled"):
                            mhr_thread = threading.Thread(target=self._mhr_sweep, args=(cur,), name="mhr-sweep",
                                                          daemon=True)
                            mhr_thread.start()
                    except Exception as e:
                        log.warning("mhr sweep not started: %s", type(e).__name__)
                if time.time() >= next_cleanup and not (cleanup_thread and cleanup_thread.is_alive()):
                    next_cleanup = time.time() + CLEANUP_SECONDS
                    try:
                        cleanup_thread = threading.Thread(target=self._cleanup_sweep, args=(self.cfg_loader(),),
                                                          name="cleanup", daemon=True)
                        cleanup_thread.start()
                    except Exception as e:
                        log.warning("cleanup sweep not started: %s", type(e).__name__)
                try:   # UI-სთვის: ფონური პროცესი ცოცხალია
                    repo.set_control("worker_heartbeat", str(time.time()))
                    repo.set_control("worker_pid", str(os.getpid()))
                    # ვადაგასული lease (მაგ. სწრაფი გადატვირთვის შემდეგ) — არა მხოლოდ გაშვებისას
                    if repo.recover_stale_jobs(int(self.cfg.section("processing")["max_attempts"])):
                        repo.log_event("recovery", "recovered interrupted job(s)", level="warning")
                except sqlite3.Error:
                    log.warning("heartbeat write failed", exc_info=True)
                if stop_event.wait(HEARTBEAT_SECONDS):
                    break
        finally:
            stop_event.set()   # შეცდომის შემთხვევაშიც ნაკადები ჩერდება
            for t in threads:
                t.join()
            if mhr_thread is not None:
                mhr_thread.join()
            if cleanup_thread is not None:
                cleanup_thread.join()
            watcher.stop()
            repo.close()
            log.info("worker %s stopped", self.worker_id)


def _setup_logging(cfg: Config) -> None:
    log_dir = cfg.paths.state / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.handlers.RotatingFileHandler(
        log_dir / "worker.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")]
    if sys.stderr is not None:   # pythonw-ში stderr არ არსებობს
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s")
    for name in ("httpx", "httpcore", "anthropic"):   # URL-ში შეიძლება პ/ნ ან სახელი იყოს
        logging.getLogger(name).setLevel(logging.WARNING)


class InstanceLock:
    """ერთ root-ზე მხოლოდ ერთი დამმუშავებელი (worker ან run-once) — ერთმანეთის publish-ს არ აფუჭებენ."""

    def __init__(self, state_dir, name: str = "worker.lock"):
        state_dir.mkdir(parents=True, exist_ok=True)
        self.path = state_dir / name
        self._f = open(self.path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt
                self._f.seek(0)
                msvcrt.locking(self._f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._f.close()
            raise RuntimeError(f"another hrsorter worker is already running ({self.path})") from None

    def release(self) -> None:
        if not self._f.closed:
            self._f.close()


def main(config_path: str | None = None) -> int:
    """`hrsorter worker`: კონფიგურაცია, ჟურნალი, საქაღალდეები, სიგნალები და მუშაობა გაჩერებამდე."""
    cfg = load_config(config_path)
    paths = cfg.paths
    paths.state.mkdir(parents=True, exist_ok=True)
    _setup_logging(cfg)
    _wait_for_root(paths.root)
    for d in [*paths.internal_dirs(), *(p.inbox for p in cfg.profiles)]:
        d.mkdir(parents=True, exist_ok=True)

    stop = threading.Event()

    def on_signal(signum, frame) -> None:
        if stop.is_set():   # მეორე სიგნალი — დაუყოვნებლივ; მიმდინარე სამუშაო აღდგება შემდეგ გაშვებაზე
            os._exit(1)
        log.info("stop requested (signal %s); finishing current job(s)", signum)
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, on_signal)

    from .pipeline import process_job   # ინტეგრატორის მოდული — lazy import
    lock = InstanceLock(paths.state)
    worker = Worker(lambda: load_config(config_path), process_job)
    try:
        worker.run_forever(stop)
    finally:
        lock.release()
    if worker.restart:
        from .cli import _spawn
        _spawn(*(["--config", str(config_path)] if config_path else []), "worker")
    return 0


def _wait_for_root(root) -> None:
    """ქსელური დისკი (მაგ. Z:) Windows-ში შესვლისას შეიძლება გვიან მიუერთდეს — მანამდე ველოდებით, არ ვვარდებით."""
    waited = False
    while True:
        try:
            root.mkdir(parents=True, exist_ok=True)
            if waited:
                log.info("root %s is available", root)
            return
        except OSError as e:
            if not waited:
                log.warning("root %s not available (%s); waiting", root, type(e).__name__)
                waited = True
            time.sleep(ROOT_WAIT_SECONDS)
