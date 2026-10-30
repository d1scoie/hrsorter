"""ინბოქსისა და დისკის მოვლა (worker-ის ცალკე ნაკადი, ყოველ 30 წამში; პარამეტრები — `cleanup`):

1. დასრულებული წყაროს ინბოქსის ფაილი → `Processed/<ინბოქსის საქაღალდე>`, შეცდომიანი → `Processed/_Failed/<…>`.
   თუ შეცდომიანი მოგვიანებით წარმატებით დამუშავდა („თავიდან დამუშავება“) — `_Failed`-იდან `Processed`-ში გადადის.
2. `Processed`-ის ფაილი იშლება `processed_days` დღის შემდეგ — ორიგინალის ასლი `_Archive`-ში რჩება.
   `_Failed` ავტომატურად არასდროს იშლება (ზოგი ასეთი ფაილი არქივში არც მოხვედრილა).
3. MHR-ში ატვირთვიდან `uploaded_days` დღის შემდეგ იშლება შედეგის PDF (+ sidecar). როცა წყაროს ყველა აქტიური
   დოკუმენტი ასეა წაშლილი — არქივის ორიგინალიც და მისი `Processed` ასლიც. ბაზის ჩანაწერები რჩება, ამიტომ
   იგივე სკანის ხელახლა მოსვლა მაინც დუბლიკატად ამოიცნობა.
"""
from __future__ import annotations

import logging
import os
import shutil
import stat
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .db import Repository

log = logging.getLogger(__name__)

DAY = 86400.0
_LATEST_JOB = ("(SELECT j.status FROM jobs j WHERE j.source_id=f.source_id"
               " ORDER BY j.created_at DESC, j.rowid DESC LIMIT 1)")


def sweep(repo: Repository, cfg: Config, now: float | None = None) -> dict[str, int]:
    """ერთი გავლა; აბრუნებს რაოდენობებს (ტესტებისა და ჟურნალისთვის)."""
    c = cfg.section("cleanup")
    now = time.time() if now is None else now
    out = {"moved": 0, "deleted": 0, "purged_documents": 0, "purged_sources": 0}
    if c["move_processed"]:
        out["moved"] = move_finished(repo, cfg, now)
    if int(c["processed_days"]) > 0:
        out["deleted"] = delete_processed(repo, now - int(c["processed_days"]) * DAY)
    if int(c["uploaded_days"]) > 0:
        out["purged_documents"], out["purged_sources"] = purge_uploaded(repo, now, now - int(c["uploaded_days"]) * DAY)
    if any(out.values()):
        repo.log_event("cleanup", " ".join(f"{k}={v}" for k, v in out.items() if v))
    return out


def move_finished(repo: Repository, cfg: Config, now: float) -> int:
    paths = cfg.paths
    rows = repo.q(f"SELECT f.path, f.size, f.mtime_ns, f.state, f.moved_kind, f.moved_to, {_LATEST_JOB} AS job"
                  " FROM inbox_files f WHERE (f.moved_kind IS NULL AND f.state IN ('ingested', 'duplicate', 'error'))"
                  f" OR (f.moved_kind='failed' AND f.state!='error' AND {_LATEST_JOB}='done')")
    moved = 0
    for r in rows:
        if r["state"] == "error" or r["job"] in ("failed", "cancelled"):
            kind, dest = "failed", paths.failed
        elif r["job"] == "done":
            kind, dest = "done", paths.processed
        else:
            continue   # რიგშია ან მუშავდება
        src = Path(r["moved_to"] if r["moved_kind"] else r["path"])
        key = (r["path"], r["size"], r["mtime_ns"])
        try:
            st = src.stat()
        except FileNotFoundError:
            _mark(repo, key, "gone", None, now)
            continue
        if not r["moved_kind"] and (st.st_size != r["size"] or st.st_mtime_ns != r["mtime_ns"]):
            _mark(repo, key, "gone", None, now)   # იმავე სახელით უკვე სხვა (ახალი) ფაილია — მას არ ვეხებით
            continue
        try:
            target = _move(src, dest / Path(r["path"]).parent.name)
        except OSError as e:   # მაგ. სკანერი/Explorer ჯერ კიდევ კითხულობს — შემდეგ გავლაზე
            log.warning("move to %s failed: %s", kind, type(e).__name__)
            continue
        _mark(repo, key, kind, str(target), now)
        moved += 1
    return moved


def delete_processed(repo: Repository, cutoff: float) -> int:
    rows = repo.q("SELECT path, size, mtime_ns, moved_to FROM inbox_files"
                  " WHERE moved_kind='done' AND deleted_at IS NULL AND moved_at < ?", (cutoff,))
    return sum(_delete_inbox_copy(repo, r) for r in rows)


def purge_uploaded(repo: Repository, now: float, cutoff: float) -> tuple[int, int]:
    cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat(timespec="seconds")
    docs = repo.q("SELECT d.id, d.output_path, d.sidecar_path FROM documents d JOIN mhr_items m ON m.document_id=d.id"
                  " WHERE d.active=1 AND d.purged_at IS NULL AND m.status='uploaded' AND m.uploaded_at < ?", (cutoff_iso,))
    n_docs = 0
    for d in docs:
        try:
            for p in (d["output_path"], d["sidecar_path"]):
                if p:
                    _force_unlink(Path(p))
        except OSError as e:
            log.warning("purge document failed: %s", type(e).__name__)
            continue
        repo.conn.execute("UPDATE documents SET purged_at=? WHERE id=?", (_iso(now), d["id"]))
        n_docs += 1
    # წყარო — მხოლოდ თუ ყველა აქტიური დოკუმენტი უკვე წაშლილია და ახალი დამუშავება არ მიმდინარეობს
    sources = repo.q("SELECT s.id, s.archive_path FROM sources s WHERE s.purged_at IS NULL"
                     " AND EXISTS (SELECT 1 FROM documents d WHERE d.source_id=s.id AND d.active=1)"
                     " AND NOT EXISTS (SELECT 1 FROM documents d WHERE d.source_id=s.id AND d.active=1 AND d.purged_at IS NULL)"
                     " AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.source_id=s.id AND j.status IN ('queued', 'running'))")
    n_src = 0
    for s in sources:
        try:
            _force_unlink(Path(s["archive_path"]))
        except OSError as e:
            log.warning("purge source failed: %s", type(e).__name__)
            continue
        repo.conn.execute("UPDATE sources SET purged_at=? WHERE id=?", (_iso(now), s["id"]))
        for r in repo.q("SELECT path, size, mtime_ns, moved_to FROM inbox_files"
                        " WHERE source_id=? AND moved_kind='done' AND deleted_at IS NULL", (s["id"],)):
            _delete_inbox_copy(repo, r)
        n_src += 1
    return n_docs, n_src


def _delete_inbox_copy(repo: Repository, r) -> bool:
    try:
        _force_unlink(Path(r["moved_to"]))
    except OSError as e:
        log.warning("delete processed file failed: %s", type(e).__name__)
        return False
    repo.conn.execute("UPDATE inbox_files SET deleted_at=? WHERE path=? AND size=? AND mtime_ns=?",
                      (time.time(), r["path"], r["size"], r["mtime_ns"]))
    return True


def _move(src: Path, dest_dir: Path) -> Path:
    """გადატანა სახელის შენარჩუნებით; თუ უკვე არსებობს — „name (2).pdf“."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    target, n = dest_dir / src.name, 2
    while target.exists():
        target, n = dest_dir / f"{src.stem} ({n}){src.suffix}", n + 1
    shutil.move(str(src), str(target))
    return target


def _force_unlink(p: Path) -> None:
    """წაშლა read-only ატრიბუტის მოხსნით (არქივის ფაილები read-only-ა); არარსებული — უკვე წაშლილია."""
    try:
        os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
    except FileNotFoundError:
        return
    p.unlink(missing_ok=True)


def _mark(repo: Repository, key: tuple, kind: str, moved_to: str | None, now: float) -> None:
    repo.conn.execute("UPDATE inbox_files SET moved_kind=?, moved_to=?, moved_at=? WHERE path=? AND size=? AND mtime_ns=?",
                      (kind, moved_to, now, *key))


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")
