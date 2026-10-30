"""წყაროს უცვლელი ასლის არქივში შენახვა: `_Archive/<sha[:2]>/<sha>.pdf` (read-only).

ნაბიჯები: სამუშაო ასლი `_Work`-ში ჰეშირებით → წყარო კოპირებისას ხომ არ შეიცვალა (stat + ხელახალი ჰეში)
→ ასლის PDF ვალიდაცია → `os.replace` არქივში → არქივის ჰეშის გადამოწმება → read-only ატრიბუტი.
ინბოქსის ფაილი არასდროს იცვლება და არ იშლება.
"""
from __future__ import annotations

import errno
import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import pdfio
from .errors import IngestError
from .models import ErrorCode

_CHUNK = 1024 * 1024
_WIN_DISK_FULL = {39, 112}      # ERROR_HANDLE_DISK_FULL, ERROR_DISK_FULL
_WIN_LOCKED = {32, 33}          # ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION


@dataclass
class ArchiveResult:
    sha256: str
    size: int
    archive_path: Path
    page_count: int
    already_archived: bool


def _copy_hashing(src: Path, dst: Path) -> tuple[str, int]:
    """src → dst კოპირება და იმავდროულად sha256; აბრუნებს (sha256, ბაიტები)."""
    h = hashlib.sha256()
    size = 0
    with src.open("rb") as fin, dst.open("wb") as fout:
        for block in iter(lambda: fin.read(_CHUNK), b""):
            h.update(block)
            fout.write(block)
            size += len(block)
        fout.flush()
        os.fsync(fout.fileno())
    return h.hexdigest(), size


def _os_error(e: OSError) -> IngestError:
    """OSError → მართვადი IngestError (დისკი სავსეა / ფაილი დაბლოკილია / გაქრა)."""
    winerror = getattr(e, "winerror", None)
    if e.errno == errno.ENOSPC or winerror in _WIN_DISK_FULL:
        return IngestError(ErrorCode.DISK_FULL.value, str(e), retryable=True)
    if isinstance(e, FileNotFoundError):
        return IngestError(ErrorCode.FILE_VANISHED.value, str(e), retryable=False)
    if isinstance(e, PermissionError) or winerror in _WIN_LOCKED:
        return IngestError(ErrorCode.FILE_LOCKED.value, str(e), retryable=True)
    return IngestError(ErrorCode.INTERNAL.value, f"{type(e).__name__}: {e}", retryable=True)


def archive_source(src: Path, archive_dir: Path, work_dir: Path) -> ArchiveResult:
    """წყაროს არქივირება. raises IngestError (retryable — ხელახლა ცდა აზრიანია)."""
    src, archive_dir, work_dir = Path(src), Path(archive_dir), Path(work_dir)
    tmp: Path | None = None
    try:
        before = src.stat()
        work_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix="ingest_", suffix=".pdf.tmp", dir=work_dir)
        os.close(fd)
        tmp = Path(tmp_name)
        sha, size = _copy_hashing(src, tmp)

        # წყარო კოპირებისას არ უნდა შეცვლილიყო: ზომა/mtime და შიგთავსის ჰეში
        after = src.stat()
        if ((before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
                or size != after.st_size or pdfio.sha256_file(src) != sha):
            raise IngestError(ErrorCode.SOURCE_CHANGED_DURING_COPY.value, "source changed during copy", retryable=True)

        check = pdfio.validate_pdf(tmp)
        if not check.ok:
            raise IngestError(check.error_code or ErrorCode.PDF_CORRUPT.value, check.detail, retryable=check.retryable)

        final = archive_dir / sha[:2] / f"{sha}.pdf"
        if final.exists():
            if pdfio.sha256_file(final) == sha:
                return ArchiveResult(sha, size, final, check.page_count, already_archived=True)
            # არქივის ასლი დაზიანებულია — აღდგება ახლად გადამოწმებული ბაიტებით
            os.chmod(final, stat.S_IWRITE | stat.S_IREAD)
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, final)
        tmp = None
        if pdfio.sha256_file(final) != sha:
            raise IngestError(ErrorCode.ARCHIVE_HASH_MISMATCH.value, "archived copy hash differs", retryable=True)
        os.chmod(final, stat.S_IREAD)   # Windows: read-only ატრიბუტი
        return ArchiveResult(sha, size, final, check.page_count, already_archived=False)
    except OSError as e:
        raise _os_error(e) from e
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
