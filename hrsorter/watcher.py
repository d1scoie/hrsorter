"""ინბოქსების მონიტორინგი: სტაბილურობა → ვალიდაცია → არქივი → sources/jobs.

იდენტიფიკაცია შიგთავსით: `inbox_files` ჩანაწერის გასაღებია (path, size, mtime_ns) — ზომის ან mtime-ის
ცვლილება ახალ pending ჩანაწერს ქმნის და სტაბილურობის ათვლა თავიდან იწყება. სტაბილურად ითვლება ფაილი,
რომლის ზომა/mtime `stability_seconds` განმავლობაში არ შეცვლილა, ექსკლუზიურად იხსნება (მწერალი აღარ
უჭირავს) და `validate_pdf` გადის. მხოლოდ დროის გასვლა საკმარისი არ არის.
"""
from __future__ import annotations

import logging
import os
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from . import pdfio
from .archive import archive_source
from .config import Config
from .db import Repository, now_iso
from .errors import IngestError
from .models import ErrorCode

log = logging.getLogger(__name__)


def _ignored_name(name: str) -> bool:
    low = name.lower()
    return (not low.endswith(".pdf") or name.startswith(("~$", "."))
            or low.endswith((".tmp", ".part")))


def _open_exclusive_error(path: str) -> int:
    """0 — ფაილი ექსკლუზიურად იხსნება; სხვა შემთხვევაში OS შეცდომის კოდი (32 = სხვა პროცესს უჭირავს)."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        generic_read, open_existing, attr_normal = 0x80000000, 3, 0x80
        h = k32.CreateFileW(path, generic_read, 0, None, open_existing, attr_normal, None)
        if h is None or h == wintypes.HANDLE(-1).value:
            return ctypes.get_last_error() or 32
        k32.CloseHandle(h)
        return 0
    try:
        with open(path, "rb"):
            return 0
    except OSError as e:
        return e.errno or 1


class InboxScanner:
    def __init__(self, cfg: Config, repo: Repository, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.repo = repo
        self.clock = clock

    # --- სკანირება ------------------------------------------------------------
    def scan_once(self) -> list[dict]:
        """ყველა პროფილის ინბოქსის ერთი გავლა (არარეკურსიული). აბრუნებს მხოლოდ ამ გავლის მოვლენებს."""
        events: list[dict] = []
        internal = [d.resolve() for d in self.cfg.paths.internal_dirs()]
        for profile in self.cfg.profiles:
            inbox = profile.inbox
            if not inbox.is_dir():
                continue
            # სკანირება არარეკურსიულია, ამიტომ საკმარისია თავად ინბოქსის შემოწმება
            inbox_res = inbox.resolve()
            if any(inbox_res.is_relative_to(d) for d in internal):
                log.warning("inbox %s is inside an internal folder; skipped", inbox)
                continue
            seen: set[str] = set()
            for entry in os.scandir(inbox):
                if _ignored_name(entry.name):
                    continue
                try:
                    if not entry.is_file():
                        continue
                    st = entry.stat()
                except OSError:
                    continue
                if getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_HIDDEN:
                    continue
                path = str(Path(entry.path))
                seen.add(path)
                ev = self._check_file(path, st.st_size, st.st_mtime_ns, profile)
                if ev:
                    events.append(ev)
            # ინბოქსიდან გამქრალი ფაილების pending ჩანაწერები აღარ არის საჭირო
            for row in self.repo.q("SELECT path, size, mtime_ns FROM inbox_files WHERE state='pending' AND profile_id=?",
                                   (profile.id,)):
                if row["path"] not in seen:
                    self.repo.conn.execute("DELETE FROM inbox_files WHERE path=? AND size=? AND mtime_ns=?",
                                           (row["path"], row["size"], row["mtime_ns"]))
        return events

    def _check_file(self, path: str, size: int, mtime_ns: int, profile) -> dict | None:
        key = (path, size, mtime_ns)
        row = self.repo.one("SELECT * FROM inbox_files WHERE path=? AND size=? AND mtime_ns=?", key)
        now = self.clock()
        if row is None:
            # ახალი ან შეცვლილი ფაილი: ძველი pending ჩანაწერი (სხვა ზომა/mtime) ჩანაცვლდება
            self.repo.conn.execute("DELETE FROM inbox_files WHERE path=? AND state='pending'", (path,))
            self.repo.conn.execute(
                "INSERT INTO inbox_files(path, size, mtime_ns, profile_id, state, first_seen, last_change, updated_at)"
                " VALUES (?,?,?,?, 'pending', ?, ?, ?)", (*key, profile.id, now, now, now_iso()))
            return {"path": path, "action": "waiting", "profile_id": profile.id, "reason": "new"}
        if row["state"] != "pending":
            return None
        if now - row["last_change"] < float(self.cfg.section("watcher")["stability_seconds"]):
            return {"path": path, "action": "waiting", "profile_id": profile.id, "reason": "stabilizing"}

        err = _open_exclusive_error(path)
        if err:
            if err in (2, 3):   # ERROR_FILE_NOT_FOUND / PATH_NOT_FOUND — გაქრა, შემდეგი სკანი წაშლის
                return None
            return self._retry_or_fail(key, row, now, ErrorCode.FILE_LOCKED.value, f"exclusive open failed ({err})")
        check = pdfio.validate_pdf(path)
        if not check.ok:
            if check.retryable:
                return self._retry_or_fail(key, row, now, check.error_code, check.detail)
            return self._fail(key, check.error_code, check.detail)
        return self._ingest(key, profile, row, now)

    # --- მდგომარეობის ცვლილებები ------------------------------------------------
    def _update(self, key: tuple, **fields: Any) -> None:
        fields["updated_at"] = now_iso()
        sets = ", ".join(f"{k}=?" for k in fields)
        self.repo.conn.execute(f"UPDATE inbox_files SET {sets} WHERE path=? AND size=? AND mtime_ns=?",
                               (*fields.values(), *key))

    def _retry_or_fail(self, key: tuple, row, now: float, code: str, detail: str,
                       reset_stability: bool = False) -> dict:
        max_wait = float(self.cfg.section("watcher")["max_wait_minutes"]) * 60
        if now - row["first_seen"] >= max_wait:
            return self._fail(key, code, f"gave up after {max_wait / 60:g} min: {detail}")
        fields: dict[str, Any] = {"attempts": row["attempts"] + 1, "error_code": code, "error_detail": detail[:500]}
        if reset_stability:
            fields["last_change"] = now
        self._update(key, **fields)
        return {"path": key[0], "action": "waiting", "profile_id": row["profile_id"], "error_code": code}

    def _fail(self, key: tuple, code: str, detail: str) -> dict:
        self._update(key, state="error", error_code=code, error_detail=detail[:500])
        self.repo.log_event("ingest_error", f"{Path(key[0]).name}: {code}", level="error")
        return {"path": key[0], "action": "error", "error_code": code}

    def _ingest(self, key: tuple, profile, row, now: float) -> dict:
        path = key[0]
        try:
            res = archive_source(Path(path), self.cfg.paths.archive, self.cfg.paths.work)
        except IngestError as e:
            if not e.retryable:
                return self._fail(key, e.code, e.detail)
            # წყარო კოპირებისას შეიცვალა → სტაბილურობას თავიდან ველოდებით
            return self._retry_or_fail(key, row, now, e.code, e.detail,
                                       reset_stability=e.code == ErrorCode.SOURCE_CHANGED_DURING_COPY.value)
        with self.repo.tx():
            existing = self.repo.source_by_sha(res.sha256)
            if existing is not None:
                self._update(key, state="duplicate", sha256=res.sha256, source_id=existing["id"],
                             error_code=None, error_detail=None)
                action, source_id, job_id = "duplicate", existing["id"], None
            else:
                source_id = self.repo.insert_source(sha256=res.sha256, size=res.size, page_count=res.page_count,
                                                    archive_path=str(res.archive_path), first_path=path,
                                                    profile_id=profile.id)
                job_id = self.repo.create_job(source_id=source_id, profile_id=profile.id,
                                              mode=profile.mode.resolve(res.page_count).value,
                                              config_version=self.cfg.version, config_snapshot=self.cfg.snapshot())
                self._update(key, state="ingested", sha256=res.sha256, source_id=source_id,
                             error_code=None, error_detail=None)
                action = "ingested"
            self.repo.log_event("ingest", f"{Path(path).name}: {action} source={source_id} job={job_id}",
                                job_id=job_id)
        return {"path": path, "action": action, "profile_id": profile.id, "sha256": res.sha256,
                "source_id": source_id, "job_id": job_id}


class _WakeHandler(FileSystemEventHandler):
    def __init__(self, wake: threading.Event):
        self.wake = wake

    def on_any_event(self, event) -> None:
        self.wake.set()


class InboxWatcher:
    """watchdog მოვლენები (ადრეული სკანი) + პერიოდული scan_once (გამოტოვებული მოვლენებისთვის), ცალკე ნაკადში.

    `cfg_loader` (არასავალდებულო) — ყოველ გავლაზე კონფიგურაციის ხელახლა წაკითხვა, რომ პარამეტრების
    ცვლილება ახალ სამუშაოებზე worker-ის გადატვირთვის გარეშე აისახოს.
    """

    def __init__(self, cfg: Config, db_path: Path, cfg_loader: Callable[[], Config] | None = None):
        self.cfg = cfg
        self.db_path = Path(db_path)
        self.cfg_loader = cfg_loader
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="inbox-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def _run(self) -> None:
        repo = Repository(self.db_path)
        scanner = InboxScanner(self.cfg, repo)
        observer = Observer()
        handler = _WakeHandler(self._wake)
        for p in self.cfg.profiles:
            try:
                p.inbox.mkdir(parents=True, exist_ok=True)
                observer.schedule(handler, str(p.inbox), recursive=False)
            except OSError:
                log.warning("cannot watch inbox %s; periodic scan only", p.inbox, exc_info=True)
        observer.start()
        try:
            while not self._stop.is_set():
                if self.cfg_loader is not None:
                    try:
                        scanner.cfg = self.cfg_loader()
                    except Exception:
                        log.warning("config reload failed; keeping previous config", exc_info=True)
                try:
                    events = scanner.scan_once()
                except Exception:
                    log.exception("inbox scan failed")
                    events = []
                w = scanner.cfg.section("watcher")
                pending = any(e["action"] == "waiting" for e in events)
                self._wake.wait(float(w["recheck_interval_seconds"] if pending else w["poll_interval_seconds"]))
                self._wake.clear()
                self._stop.wait(0.5)   # მოვლენების ნაკადის (ჩაწერის დროს) დაგროვება — ზედმეტი სკანის გარეშე
        finally:
            observer.stop()
            observer.join()
            repo.close()
