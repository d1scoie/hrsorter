"""SQLite მუდმივი მდგომარეობა: წყაროები, სამუშაო რიგი, გვერდები, დოკუმენტები, ქეში.

წესები:
  * ერთი კავშირი = ერთი ნაკადი. თითო პროცესი/ნაკადი ხსნის საკუთარ Repository-ს.
  * WAL რეჟიმი — UI-ს კითხვა worker-ის ჩაწერას არ ბლოკავს.
  * ID-ები სტაბილურია (src_/job_/doc_ პრეფიქსით) და ბილიკებისგან დამოუკიდებელია.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- უნიკალური შიგთავსი (sha256) = ერთი წყარო
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    size INTEGER NOT NULL,
    page_count INTEGER,
    archive_path TEXT NOT NULL,
    first_path TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    purged_at TEXT                -- არქივის ფაილი წაიშალა (cleanup.uploaded_days)
);

-- ინბოქსში ნანახი ფაილები (ბილიკი+ზომა+mtime → sha256); სახელით იდენტიფიკაცია არ ხდება
CREATE TABLE IF NOT EXISTS inbox_files (
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    profile_id TEXT NOT NULL,
    state TEXT NOT NULL,          -- pending | ingested | duplicate | error
    sha256 TEXT,
    source_id TEXT,
    first_seen REAL NOT NULL,
    last_change REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    error_detail TEXT,
    updated_at TEXT NOT NULL,
    moved_kind TEXT,              -- NULL = ჯერ ინბოქსშია | done (Processed) | failed (Processed/_Failed) | gone (ფაილი აღარ იყო)
    moved_to TEXT,                -- სად გადავიდა
    moved_at REAL,
    deleted_at REAL,              -- Processed-იდან წაიშალა (cleanup.processed_days)
    PRIMARY KEY (path, size, mtime_ns)
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    profile_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    config_version TEXT NOT NULL,
    config_snapshot TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_until REAL,
    progress_done INTEGER NOT NULL DEFAULT 0,
    progress_total INTEGER NOT NULL DEFAULT 0,
    stage TEXT,
    error_code TEXT,
    error_detail TEXT,
    reprocess_of TEXT,
    report_path TEXT,
    not_before REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- ერთსა და იმავე შიგთავსზე პარალელურად ორი აქტიური სამუშაო დაუშვებელია
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_active_per_source
    ON jobs(source_id) WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, priority, created_at);

CREATE TABLE IF NOT EXISTS job_attempts (
    job_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT,
    error_code TEXT,
    error_detail TEXT,
    worker TEXT,
    PRIMARY KEY (job_id, attempt)
);

-- გვერდების ტექსტი/ანალიზი (resume: უკვე დამუშავებული გვერდი ხელახლა არ იკითხება)
CREATE TABLE IF NOT EXISTS page_texts (
    job_id TEXT NOT NULL,
    page_index INTEGER NOT NULL,
    data TEXT NOT NULL,           -- PageText JSON
    PRIMARY KEY (job_id, page_index)
);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    page_start INTEGER NOT NULL,
    page_end INTEGER NOT NULL,
    status TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    type_id TEXT,
    company_id TEXT,
    employee_id TEXT,
    personal_number TEXT,
    year INTEGER,
    document_date TEXT,
    review_reasons TEXT NOT NULL DEFAULT '[]',
    output_path TEXT,
    output_sha256 TEXT,
    sidecar_path TEXT,
    planned_path TEXT,
    content_fingerprint TEXT,     -- გვერდების შიგთავსის ჰეშების ჰეში (ზუსტი დუბლიკატი)
    text_fingerprint TEXT,        -- ნორმალიზებული ტექსტის ჰეში
    text_shingles TEXT,           -- სავარაუდო დუბლიკატისთვის (JSON სია)
    doc_number TEXT,
    superseded_by TEXT,
    metadata TEXT,                -- სრული sidecar JSON
    purged_at TEXT,               -- შედეგის PDF წაიშალა MHR-ში ატვირთვის შემდეგ (cleanup.uploaded_days)
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_fp ON documents(content_fingerprint);
CREATE INDEX IF NOT EXISTS documents_job ON documents(job_id);
CREATE INDEX IF NOT EXISTS documents_filter ON documents(status, type_id, company_id);

-- ყოველი გვერდის დანიშნულება (100% აღრიცხვა)
CREATE TABLE IF NOT EXISTS page_assignments (
    job_id TEXT NOT NULL,
    page_index INTEGER NOT NULL,
    role TEXT NOT NULL,           -- document | uncertain | blank
    document_id TEXT,
    PRIMARY KEY (job_id, page_index)
);

CREATE TABLE IF NOT EXISTS employee_cache (
    key TEXT PRIMARY KEY,         -- "pn:<n>" | "id:<id>" | "name:<q>" | "companies"
    payload TEXT NOT NULL,
    synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS control (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- MHR ინტეგრაცია: ვალიდაცია და ატვირთვა (ერთი ჩანაწერი დოკუმენტზე; სტატუსები — mhr_models.MhrStatus)
CREATE TABLE IF NOT EXISTS mhr_items (
    document_id TEXT PRIMARY KEY,
    employee_id TEXT,
    personal_number TEXT,
    status TEXT NOT NULL,
    validation TEXT,              -- ValidationResult JSON
    attempts INTEGER NOT NULL DEFAULT 0,
    next_try REAL,
    remote_id TEXT,
    error TEXT,
    decided_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    uploaded_at TEXT
);
CREATE INDEX IF NOT EXISTS mhr_items_status ON mhr_items(status);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    job_id TEXT,
    message TEXT NOT NULL         -- ტექნიკური ტექსტი; დოკუმენტის შიგთავსი აქ არ იწერება
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class Repository:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        if "not_before" not in cols:   # ძველი ბაზის მიგრაცია
            self.conn.execute("ALTER TABLE jobs ADD COLUMN not_before REAL")
        for table, col, typ in (("inbox_files", "moved_kind", "TEXT"), ("inbox_files", "moved_to", "TEXT"), ("inbox_files", "moved_at", "REAL"),
                                ("inbox_files", "deleted_at", "REAL"), ("documents", "purged_at", "TEXT"),
                                ("sources", "purged_at", "TEXT"), ("mhr_items", "ai_review", "TEXT")):
            if col not in {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """ექსკლუზიური ტრანზაქცია (BEGIN IMMEDIATE) — ატომური მრავალსაფეხურიანი ცვლილებისთვის."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def q(self, sql: str, args: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, args).fetchone()

    # --- control (pause/resume) ------------------------------------------------
    def set_control(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO control(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, value))

    def get_control(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM control WHERE key=?", (key,))
        return row["value"] if row else default

    def is_paused(self) -> bool:
        return self.get_control("paused", "0") == "1"

    # --- events ------------------------------------------------------------------
    def log_event(self, kind: str, message: str, level: str = "info", job_id: str | None = None) -> None:
        self.conn.execute("INSERT INTO events(at, level, kind, job_id, message) VALUES (?,?,?,?,?)",
                          (now_iso(), level, kind, job_id, message[:2000]))

    # --- sources -----------------------------------------------------------------
    def source_by_sha(self, sha256: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM sources WHERE sha256=?", (sha256,))

    def get_source(self, source_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM sources WHERE id=?", (source_id,))

    def insert_source(self, *, sha256: str, size: int, page_count: int | None, archive_path: str,
                      first_path: str, profile_id: str) -> str:
        sid = new_id("src")
        self.conn.execute(
            "INSERT INTO sources(id, sha256, size, page_count, archive_path, first_path, profile_id, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (sid, sha256, size, page_count, archive_path, first_path, profile_id, now_iso()))
        return sid

    # --- jobs --------------------------------------------------------------------
    def create_job(self, *, source_id: str, profile_id: str, mode: str, config_version: str,
                   config_snapshot: str, reprocess_of: str | None = None, priority: int = 0) -> str | None:
        """ახალი სამუშაო. აბრუნებს None-ს, თუ ამ წყაროზე უკვე არის აქტიური სამუშაო."""
        jid = new_id("job")
        ts = now_iso()
        try:
            self.conn.execute(
                "INSERT INTO jobs(id, source_id, profile_id, mode, status, priority, config_version, config_snapshot,"
                " reprocess_of, created_at, updated_at) VALUES (?,?,?,?, 'queued', ?,?,?,?,?,?)",
                (jid, source_id, profile_id, mode, priority, config_version, config_snapshot, reprocess_of, ts, ts))
        except sqlite3.IntegrityError:
            return None
        return jid

    def get_job(self, job_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM jobs WHERE id=?", (job_id,))

    def claim_next_job(self, owner: str, lease_seconds: float) -> sqlite3.Row | None:
        """რიგიდან შემდეგი სამუშაოს ატომური აღება lease-ით."""
        with self.tx() as c:
            row = c.execute("SELECT id FROM jobs WHERE status='queued' AND (not_before IS NULL OR not_before <= ?)"
                            " ORDER BY priority DESC, created_at LIMIT 1", (time.time(),)).fetchone()
            if not row:
                return None
            c.execute("UPDATE jobs SET status='running', attempt=attempt+1, lease_owner=?, lease_until=?, updated_at=?"
                      " WHERE id=?", (owner, time.time() + lease_seconds, now_iso(), row["id"]))
            job = c.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            c.execute("INSERT OR REPLACE INTO job_attempts(job_id, attempt, started_at, status, worker) VALUES (?,?,?,?,?)",
                      (job["id"], job["attempt"], now_iso(), "running", owner))
            return job

    def renew_lease(self, job_id: str, owner: str, lease_seconds: float) -> None:
        self.conn.execute("UPDATE jobs SET lease_until=?, updated_at=? WHERE id=? AND lease_owner=?",
                          (time.time() + lease_seconds, now_iso(), job_id, owner))

    def update_job_progress(self, job_id: str, *, stage: str | None = None, done: int | None = None,
                            total: int | None = None) -> None:
        sets, args = ["updated_at=?"], [now_iso()]
        if stage is not None:
            sets.append("stage=?"); args.append(stage)
        if done is not None:
            sets.append("progress_done=?"); args.append(done)
        if total is not None:
            sets.append("progress_total=?"); args.append(total)
        args.append(job_id)
        self.conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", args)

    def finish_job(self, job_id: str, status: str, *, error_code: str | None = None, error_detail: str | None = None,
                   report_path: str | None = None) -> None:
        ts = now_iso()
        with self.tx() as c:
            c.execute("UPDATE jobs SET status=?, error_code=?, error_detail=?, report_path=COALESCE(?, report_path),"
                      " lease_owner=NULL, lease_until=NULL, updated_at=? WHERE id=?",
                      (status, error_code, error_detail, report_path, ts, job_id))
            c.execute("UPDATE job_attempts SET finished_at=?, status=?, error_code=?, error_detail=?"
                      " WHERE job_id=? AND attempt=(SELECT attempt FROM jobs WHERE id=?)",
                      (ts, status, error_code, error_detail, job_id, job_id))

    def requeue_job(self, job_id: str, *, error_code: str | None = None, error_detail: str | None = None,
                    delay_seconds: float = 0) -> None:
        ts = now_iso()
        with self.tx() as c:
            c.execute("UPDATE job_attempts SET finished_at=?, status='retry', error_code=?, error_detail=?"
                      " WHERE job_id=? AND attempt=(SELECT attempt FROM jobs WHERE id=?)",
                      (ts, error_code, error_detail, job_id, job_id))
            c.execute("UPDATE jobs SET status='queued', lease_owner=NULL, lease_until=NULL, not_before=?, updated_at=?"
                      " WHERE id=?", (time.time() + delay_seconds if delay_seconds else None, ts, job_id))

    def recover_stale_jobs(self, max_attempts: int) -> list[str]:
        """ავარიის/გადატვირთვის შემდეგ: ვადაგასული lease-ის მქონე running სამუშაოები ბრუნდება რიგში."""
        now = time.time()
        recovered = []
        for row in self.q("SELECT id, attempt, report_path FROM jobs WHERE status='running'"
                          " AND (lease_until IS NULL OR lease_until < ?)", (now,)):
            if row["report_path"]:   # pipeline დასრულდა, ავარია მხოლოდ finish_job-მდე მოხდა
                self.finish_job(row["id"], "done")
            elif row["attempt"] >= max_attempts:
                self.finish_job(row["id"], "failed", error_code="max_attempts",
                                error_detail="interrupted too many times")
            else:
                self.requeue_job(row["id"], error_code="interrupted", error_detail="lease expired (crash/restart)")
            recovered.append(row["id"])
        return recovered

    # --- page texts ----------------------------------------------------------------
    def save_page_text(self, job_id: str, page_index: int, data: dict[str, Any]) -> None:
        self.conn.execute("INSERT OR REPLACE INTO page_texts(job_id, page_index, data) VALUES (?,?,?)",
                          (job_id, page_index, json.dumps(data, ensure_ascii=False)))

    def load_page_texts(self, job_id: str) -> dict[int, dict[str, Any]]:
        return {r["page_index"]: json.loads(r["data"])
                for r in self.q("SELECT page_index, data FROM page_texts WHERE job_id=?", (job_id,))}

    # --- documents -------------------------------------------------------------------
    def upsert_document(self, doc: dict[str, Any]) -> None:
        """doc: documents ცხრილის სვეტები (id სავალდებულოა). JSON-ველები ავტომატურად სერიალიზდება."""
        d = dict(doc)
        for k in ("review_reasons", "text_shingles", "metadata"):
            if k in d and not isinstance(d[k], str) and d[k] is not None:
                d[k] = json.dumps(d[k], ensure_ascii=False)
        d.setdefault("created_at", now_iso())
        d["updated_at"] = now_iso()
        cols = list(d.keys())
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in ("id", "created_at"))
        self.conn.execute(
            f"INSERT INTO documents({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
            f" ON CONFLICT(id) DO UPDATE SET {updates}", [d[c] for c in cols])

    def get_document(self, doc_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM documents WHERE id=?", (doc_id,))

    def active_documents_for_source(self, source_id: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM documents WHERE source_id=? AND active=1", (source_id,))

    def documents_by_fingerprint(self, fp: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM documents WHERE content_fingerprint=? AND active=1", (fp,))

    def candidate_duplicates(self, *, type_id: str | None, personal_number: str | None,
                             exclude_job: str | None = None, limit: int = 200) -> list[sqlite3.Row]:
        """სავარაუდო დუბლიკატის კანდიდატები: იგივე ტიპი ან იგივე პ/ნ (საბოლოო შეფასება duplicates.py-ში)."""
        return self.q(
            "SELECT * FROM documents WHERE active=1 AND (job_id IS NOT ? ) AND (type_id=? OR (personal_number IS NOT NULL"
            " AND personal_number=?)) ORDER BY created_at DESC LIMIT ?",
            (exclude_job, type_id, personal_number, limit))

    def documents_in_state(self, status: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM documents WHERE status=?", (status,))

    # --- page assignments ------------------------------------------------------------
    def set_page_assignments(self, job_id: str, assignments: list[tuple[int, str, str | None]]) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM page_assignments WHERE job_id=?", (job_id,))
            c.executemany("INSERT INTO page_assignments(job_id, page_index, role, document_id) VALUES (?,?,?,?)",
                          [(job_id, p, role, did) for p, role, did in assignments])

    # --- MHR ----------------------------------------------------------------------------
    def mhr_get(self, document_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM mhr_items WHERE document_id=?", (document_id,))

    def mhr_enqueue(self, document_id: str, personal_number: str | None) -> bool:
        """ახალი ჩანაწერი სტატუსით pending. უკვე არსებულს არ ცვლის (ორმაგი ვალიდაცია/ატვირთვა არ ხდება)."""
        ts = now_iso()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO mhr_items(document_id, personal_number, status, created_at, updated_at)"
            " VALUES (?,?,'pending',?,?)", (document_id, personal_number, ts, ts))
        return cur.rowcount == 1

    def mhr_transition(self, document_id: str, from_status: tuple[str, ...], to_status: str, **fields: Any) -> bool:
        """სტატუსის ატომური ცვლილება მხოლოდ მოსალოდნელი წინა სტატუსიდან (ორი პროცესი ერთსა და იმავეს ვერ გააკეთებს)."""
        sets = ["status=?", "updated_at=?"] + [f"{k}=?" for k in fields]
        vals = [to_status, now_iso()] + [json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                                         for v in fields.values()]
        q = ",".join("?" for _ in from_status)
        cur = self.conn.execute(f"UPDATE mhr_items SET {', '.join(sets)} WHERE document_id=? AND status IN ({q})",
                                [*vals, document_id, *from_status])
        return cur.rowcount == 1

    def mhr_set_ai_review(self, document_id: str, review: dict[str, Any] | None) -> None:
        """AI ანალიზის შედეგი (ai_review.py); None = გასუფთავება (ხელახალი ანალიზი)."""
        self.conn.execute("UPDATE mhr_items SET ai_review=? WHERE document_id=?",
                          (json.dumps(review, ensure_ascii=False) if review is not None else None, document_id))

    def mhr_list(self, statuses: tuple[str, ...] | None = None, limit: int = 500) -> list[sqlite3.Row]:
        if statuses:
            q = ",".join("?" for _ in statuses)
            return self.q(f"SELECT * FROM mhr_items WHERE status IN ({q}) ORDER BY updated_at DESC LIMIT ?", (*statuses, limit))
        return self.q("SELECT * FROM mhr_items ORDER BY updated_at DESC LIMIT ?", (limit,))

    # --- employee cache --------------------------------------------------------------
    def cache_get(self, key: str) -> sqlite3.Row | None:
        return self.one("SELECT payload, synced_at FROM employee_cache WHERE key=?", (key,))

    def cache_put(self, key: str, payload: Any) -> None:
        self.conn.execute(
            "INSERT INTO employee_cache(key, payload, synced_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, synced_at=excluded.synced_at",
            (key, json.dumps(payload, ensure_ascii=False), now_iso()))
