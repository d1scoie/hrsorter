from __future__ import annotations

import errno
import hashlib
import io
import os
import stat
from pathlib import Path

import pytest
from pypdf import PdfWriter

from hrsorter import archive
from hrsorter.archive import archive_source
from hrsorter.errors import IngestError


def make_pdf(path: Path, title: str = "t", pages: int = 1, encrypt: bool = False) -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(200, 200)
    w.add_metadata({"/Title": title})
    if encrypt:
        w.encrypt("pw")
    buf = io.BytesIO()
    w.write(buf)
    path.write_bytes(buf.getvalue())
    return buf.getvalue()


@pytest.fixture
def dirs(tmp_path):
    inbox = tmp_path / "Inbox"
    inbox.mkdir()
    return inbox, tmp_path / "_Archive", tmp_path / "_Work"


def test_archive_bytes_identical_readonly_hash_verified(dirs):
    inbox, arch, work = dirs
    src = inbox / "scan.pdf"
    data = make_pdf(src, pages=3)
    src_stat = src.stat()

    res = archive_source(src, arch, work)

    sha = hashlib.sha256(data).hexdigest()
    assert res.sha256 == sha and res.size == len(data) and res.page_count == 3
    assert res.archive_path == arch / sha[:2] / f"{sha}.pdf"
    assert res.archive_path.read_bytes() == data
    assert not res.already_archived
    assert not os.access(res.archive_path, os.W_OK)          # read-only
    if os.name == "nt":
        assert res.archive_path.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
    # ინბოქსის ფაილი ხელუხლებელია, სამუშაო საქაღალდეში ნაგავი არ რჩება
    assert src.read_bytes() == data and src.stat().st_mtime_ns == src_stat.st_mtime_ns
    assert list(work.iterdir()) == []


def test_already_archived_not_rewritten(dirs):
    inbox, arch, work = dirs
    a = inbox / "a.pdf"
    make_pdf(a)
    first = archive_source(a, arch, work)
    mtime = first.archive_path.stat().st_mtime_ns
    b = inbox / "b.pdf"
    b.write_bytes(a.read_bytes())

    second = archive_source(b, arch, work)

    assert second.already_archived and second.sha256 == first.sha256
    assert second.archive_path.stat().st_mtime_ns == mtime
    assert list(work.iterdir()) == []


def test_corrupted_archive_copy_is_restored(dirs):
    inbox, arch, work = dirs
    src = inbox / "a.pdf"
    data = make_pdf(src)
    res = archive_source(src, arch, work)
    os.chmod(res.archive_path, stat.S_IWRITE | stat.S_IREAD)
    res.archive_path.write_bytes(b"broken")

    again = archive_source(src, arch, work)

    assert not again.already_archived
    assert again.archive_path.read_bytes() == data
    assert not os.access(again.archive_path, os.W_OK)


def test_source_changed_during_copy_is_retryable(dirs, monkeypatch):
    inbox, arch, work = dirs
    src = inbox / "a.pdf"
    make_pdf(src)
    real_copy = archive._copy_hashing

    def copy_then_modify(s, d):
        out = real_copy(s, d)
        with open(s, "ab") as f:   # სკანერი ჯერ კიდევ წერს
            f.write(b"\n% more data\n")
        return out

    monkeypatch.setattr(archive, "_copy_hashing", copy_then_modify)
    with pytest.raises(IngestError) as ei:
        archive_source(src, arch, work)
    assert ei.value.code == "source_changed_during_copy" and ei.value.retryable
    assert not arch.exists() or not any(arch.rglob("*.pdf"))
    assert list(work.iterdir()) == []


@pytest.mark.parametrize("exc, code", [
    (OSError(errno.ENOSPC, "No space left on device"), "disk_full"),
    (PermissionError(errno.EACCES, "sharing violation"), "file_locked"),
])
def test_os_errors_mapped_to_retryable_codes(dirs, monkeypatch, exc, code):
    inbox, arch, work = dirs
    src = inbox / "a.pdf"
    make_pdf(src)

    def fail(s, d):
        raise exc

    monkeypatch.setattr(archive, "_copy_hashing", fail)
    with pytest.raises(IngestError) as ei:
        archive_source(src, arch, work)
    assert ei.value.code == code and ei.value.retryable
    assert list(work.iterdir()) == []


def test_windows_disk_full_winerror_mapped():
    e = OSError(None, "disk full")
    e.winerror = 112
    assert archive._os_error(e).code == "disk_full"


@pytest.mark.parametrize("kind, code", [("corrupt", "pdf_corrupt"), ("encrypted", "pdf_encrypted")])
def test_invalid_pdf_not_archived(dirs, kind, code):
    inbox, arch, work = dirs
    src = inbox / "bad.pdf"
    if kind == "corrupt":
        src.write_bytes(b"%PDF-1.4\n" + b"garbage " * 50 + b"\n%%EOF\n")
    else:
        make_pdf(src, encrypt=True)
    with pytest.raises(IngestError) as ei:
        archive_source(src, arch, work)
    assert ei.value.code == code and not ei.value.retryable
    assert not arch.exists() or not any(arch.rglob("*.pdf"))
    assert src.exists()
