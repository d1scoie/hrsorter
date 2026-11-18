"""სკანის ასლი ამ კომპიუტერზე: `<state>/keep/<sha[:2]>/<sha>.pdf`.

რატომ: ნამდვილ დაყენებაზე მთავარი საქაღალდე ქსელური საცავია, რომელიც ღამით თავად შლის დაახლოებით ერთი დღის წინანდელ
ფაილებს. დასკანერებული, მაგრამ ჯერ აუტვირთავი დოკუმენტის PDF-იც და სკანის ორიგინალიც ქრებოდა: ატვირთვა
„file unreadable (FileNotFoundError)“-ით ჩერდებოდა, გვერდები აღარ ჩანდა და ქაღალდი თავიდან იყო დასასკანერებელი.

როგორ:
  * `sweep` (worker-ის მოვლის ნაკადი, 30 წამში ერთხელ) ახალ ორიგინალსა და შედეგის PDF-ს ასლად ინახავს. ასლის სახელი
    შიგთავსის sha256-ია, რომელიც ბაზაში უკვე წერია: ცალკე აღრიცხვა არ სჭირდება და ასლი შენახვამდე მოწმდება.
  * წამკითხველი (ატვირთვა, შედარება, გვერდის სურათი, PDF-ის ჩვენება) `document_file` / `source_file`-ით ჯერ ძირითად
    ბილიკს ნახულობს, მერე ასლს. ძირითად საქაღალდეში არაფერი ბრუნდება — გამონაკლისია თავიდან დამუშავება
    (`restore_source`), რომელიც შედეგებს მაინც იქ წერს.
  * ასლი `cleanup.local_copy_days` დღე ინახება (0 = ასლი არ კეთდება; უკვე შენახული ისევ გამოიყენება).
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .pdfio import sha256_file

log = logging.getLogger(__name__)

DAY = 86400.0
DIR_NAME = "keep"
RECHECK_GONE_SECONDS = 3600.0   # ძირითად საქაღალდეში უკვე გამქრალ ფაილს ყოველ გავლაზე თავიდან არ ვეძებთ
STALE_TMP_SECONDS = 3600.0
_SHA = re.compile(r"[0-9a-f]{64}")
_gone: dict[str, float] = {}     # sha → როდის შევამოწმოთ ისევ (მხოლოდ ამ პროცესის მეხსიერებაში)


def days(cfg: Config) -> int:
    return int(cfg.section("cleanup").get("local_copy_days") or 0)


def root(cfg: Config) -> Path:
    return cfg.paths.state / DIR_NAME


def local_path(cfg: Config, sha: str | None) -> Path | None:
    """ასლის ადგილი; sha ბაზიდან მოდის, მაგრამ ბილიკად მხოლოდ 64 თექვსმეტობითი სიმბოლო იქცევა."""
    if not sha or not _SHA.fullmatch(sha):
        return None
    return root(cfg) / sha[:2] / f"{sha}.pdf"


def _is_file(p: Path) -> bool:
    try:
        return p.is_file()
    except OSError:   # მიუწვდომელი ქსელური ბილიკი
        return False


def find(cfg: Config, path: str | None, sha: str | None) -> Path | None:
    """საიდან წავიკითხოთ: ძირითადი ბილიკი, თუ ფაილი იქ არის; თუ არა — ასლი. None — არცერთი აღარ არსებობს."""
    if path and _is_file(Path(path)):
        return Path(path)
    lp = local_path(cfg, sha)
    return lp if lp is not None and _is_file(lp) else None


def document_file(cfg: Config, doc: Any) -> Path | None:
    """დოკუმენტის შედეგის PDF (`documents` სტრიქონი)."""
    return find(cfg, doc["output_path"], doc["output_sha256"])


def source_file(cfg: Config, src: Any) -> Path | None:
    """სკანის ორიგინალი (`sources` სტრიქონი)."""
    return find(cfg, src["archive_path"], src["sha256"])


def store(cfg: Config, path: str | Path, sha: str) -> bool:
    """ფაილის ასლი sha-ს სახელით. False — ფაილი არ არის, ჯერ იწერება (შიგთავსი sha-ს არ ემთხვევა) ან დისკმა უარი თქვა."""
    dest = local_path(cfg, sha)
    if dest is None:
        return False
    if _is_file(dest):
        return True
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, tmp)
        if sha256_file(tmp) != sha:
            return False
        os.replace(tmp, dest)
        return True
    except OSError as e:
        log.warning("local copy failed: %s", type(e).__name__)
        return False
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def forget(cfg: Config, sha: str | None) -> None:
    """ასლის წაშლა (ვადაგასული ატვირთული დოკუმენტი — `housekeeping.purge_uploaded`)."""
    lp = local_path(cfg, sha)
    if lp is not None:
        try:
            lp.unlink(missing_ok=True)
        except OSError as e:
            log.warning("local copy delete failed: %s", type(e).__name__)


def restore_source(cfg: Config, src: Any) -> Path | None:
    """თავიდან დამუშავებისთვის: არქივის ორიგინალი თუ გაქრა, ასლიდან ბრუნდება იმავე ადგილზე (read-only, როგორც არქივშია).
    აბრუნებს არქივის ბილიკს; None — არც არქივშია და არც ასლი არსებობს (ან არქივში ჩაწერა ვერ მოხერხდა)."""
    target = Path(src["archive_path"])
    if _is_file(target):
        return target
    lp = local_path(cfg, src["sha256"])
    if lp is None or not _is_file(lp):
        return None
    tmp = target.with_name(target.name + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(lp, tmp)
        os.replace(tmp, target)
        os.chmod(target, stat.S_IREAD)
    except OSError as e:
        log.warning("restore of archived source failed: %s", type(e).__name__)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return target


def _wanted(repo, since_iso: str | None = None) -> list[tuple[str, str]]:
    """(sha, ძირითადი ბილიკი): ორიგინალები და აქტიური დოკუმენტების შედეგები, რომლებიც ვადით არ წაშლილა."""
    when = " AND created_at >= ?" if since_iso else ""
    args = (since_iso,) if since_iso else ()
    rows = [(r["sha256"], r["archive_path"])
            for r in repo.q(f"SELECT sha256, archive_path FROM sources WHERE purged_at IS NULL{when}", args)]
    rows += [(r["output_sha256"], r["output_path"])
             for r in repo.q("SELECT output_sha256, output_path FROM documents WHERE active=1 AND purged_at IS NULL"
                             f" AND output_sha256 IS NOT NULL AND output_path IS NOT NULL{when}", args)]
    return rows


def sweep(repo, cfg: Config, now: float | None = None) -> dict[str, int]:
    """ერთი გავლა: ბოლო `local_copy_days` დღის ფაილებს, რომლებსაც ასლი არ აქვს, ასლი უკეთდება; ვადაგასული ასლი იშლება."""
    out = {"kept": 0, "expired": 0}
    n = days(cfg)
    if n <= 0:
        return out
    now = time.time() if now is None else now
    try:
        reachable = cfg.paths.root.is_dir()
    except OSError:
        reachable = False
    if not reachable:
        return out   # ძირითადი საქაღალდე მიუწვდომელია (ქსელი) — თითო ფაილზე ლოდინი მოვლის ნაკადს დააყოვნებდა
    since = datetime.fromtimestamp(now - n * DAY, timezone.utc).isoformat(timespec="seconds")
    for sha, path in _wanted(repo, since):
        lp = local_path(cfg, sha)
        if lp is None or _is_file(lp) or _gone.get(sha, 0.0) > now:
            continue
        if not _is_file(Path(path)):
            _gone[sha] = now + RECHECK_GONE_SECONDS
            continue
        if store(cfg, path, sha):
            out["kept"] += 1
    out["expired"] = _expire(cfg, now, now - (n + 1) * DAY)   # +1 დღე: ვადის ზღვარზე ასლი არ იშლება და თავიდან არ კეთდება
    return out


def _expire(cfg: Config, now: float, cutoff: float) -> int:
    base = root(cfg)
    if not base.is_dir():
        return 0
    n = 0
    for p in base.glob("*/*"):
        try:
            mtime = p.stat().st_mtime
            if p.suffix == ".tmp":
                if mtime < now - STALE_TMP_SECONDS:
                    p.unlink()
            elif mtime < cutoff:
                p.unlink()
                n += 1
        except OSError:
            continue
    return n


def import_folder(repo, cfg: Config, folder: str | Path) -> dict[str, int]:
    """გადარჩენა სათადარიგო ასლიდან: საქაღალდის ყველა PDF, რომლის შიგთავსიც ბაზის ორიგინალს ან აქტიური დოკუმენტის
    შედეგს ემთხვევა და ამ კომპიუტერზე ასლი არ აქვს, ასლად ინახება. ფაილის სახელს მნიშვნელობა არ აქვს."""
    wanted = {sha for sha, _ in _wanted(repo)}
    out = {"seen": 0, "kept": 0}
    for p in sorted(Path(folder).rglob("*.pdf")):
        if not _is_file(p):
            continue
        out["seen"] += 1
        try:
            sha = sha256_file(p)
        except OSError:
            continue
        lp = local_path(cfg, sha)
        if sha in wanted and lp is not None and not _is_file(lp) and store(cfg, p, sha):
            _gone.pop(sha, None)
            out["kept"] += 1
    return out
