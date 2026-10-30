"""PDF-ის კითხვა, ვალიდაცია, რენდერი და გვერდების უდანაკარგო ჩაწერა.

ბიბლიოთეკები: pypdf (BSD) — ვალიდაცია/ჩაწერა; pypdfium2 (Apache-2.0/BSD) — ტექსტური ფენა და რენდერი.
დიდი PDF მთლიანად მეხსიერებაში არ იტვირთება: ორივე ბიბლიოთეკა გვერდებს საჭიროებისამებრ კითხულობს,
ხოლო რენდერი ხდება მხოლოდ ერთი გვერდისა ერთ ჯერზე.
"""
from __future__ import annotations

import hashlib
import io
import logging
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError

from .models import ErrorCode

logging.getLogger("pypdf").setLevel(logging.ERROR)


@dataclass
class PdfCheck:
    ok: bool
    page_count: int = 0
    error_code: str | None = None
    retryable: bool = False   # True — შესაძლოა ჯერ კიდევ იწერება (ხელახლა შემოწმდეს)
    detail: str = ""


def validate_pdf(path: str | Path) -> PdfCheck:
    """PDF-ის სტრუქტურული შემოწმება: header, %%EOF, დაშიფვრა, გვერდები, pdfium-ით გახსნა."""
    p = Path(path)
    try:
        size = p.stat().st_size
        with p.open("rb") as f:
            head = f.read(1024)
            f.seek(max(0, size - 2048))
            tail = f.read()
    except FileNotFoundError:
        return PdfCheck(False, error_code=ErrorCode.FILE_VANISHED.value, retryable=False, detail="file not found")
    except PermissionError as e:
        return PdfCheck(False, error_code=ErrorCode.FILE_LOCKED.value, retryable=True, detail=str(e))
    if size == 0:
        return PdfCheck(False, error_code=ErrorCode.PDF_EMPTY.value, retryable=True, detail="zero bytes")
    if b"%PDF-" not in head:
        return PdfCheck(False, error_code=ErrorCode.PDF_CORRUPT.value, retryable=False, detail="missing %PDF header")
    if b"%%EOF" not in tail:
        # სკანერი შეიძლება ჯერ კიდევ წერს — ხელახლა შემოწმება
        return PdfCheck(False, error_code=ErrorCode.PDF_CORRUPT.value, retryable=True, detail="missing %%EOF (incomplete?)")
    try:
        reader = PdfReader(str(p), strict=False)
        if reader.is_encrypted:
            return PdfCheck(False, error_code=ErrorCode.PDF_ENCRYPTED.value, retryable=False, detail="encrypted PDF")
        n = len(reader.pages)
    except PdfReadError as e:
        msg = str(e).lower()
        code = ErrorCode.PDF_ENCRYPTED if ("encrypt" in msg or "password" in msg) else ErrorCode.PDF_CORRUPT
        return PdfCheck(False, error_code=code.value, retryable=False, detail=str(e)[:300])
    except Exception as e:  # pypdf-ის სხვადასხვა შიდა შეცდომა დაზიანებულ ფაილზე
        return PdfCheck(False, error_code=ErrorCode.PDF_CORRUPT.value, retryable=False, detail=f"{type(e).__name__}: {e}"[:300])
    if n == 0:
        return PdfCheck(False, error_code=ErrorCode.PDF_EMPTY.value, retryable=False, detail="no pages")
    try:
        doc = pdfium.PdfDocument(str(p))
        n2 = len(doc)
        doc.close()
    except pdfium.PdfiumError as e:
        msg = str(e).lower()
        code = ErrorCode.PDF_ENCRYPTED if "password" in msg else ErrorCode.PDF_CORRUPT
        return PdfCheck(False, error_code=code.value, retryable=False, detail=str(e)[:300])
    if n2 != n:
        return PdfCheck(False, error_code=ErrorCode.PDF_CORRUPT.value, retryable=False,
                        detail=f"page count mismatch pypdf={n} pdfium={n2}")
    return PdfCheck(True, page_count=n)


class PdfDocument:
    """წყარო PDF-ის გვერდობრივი წვდომა. გამოიყენე `with PdfDocument(path) as doc:`."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._pdfium = pdfium.PdfDocument(self.path)
        self._reader: PdfReader | None = None

    def __enter__(self) -> "PdfDocument":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._pdfium is not None:
            self._pdfium.close()
            self._pdfium = None

    @property
    def page_count(self) -> int:
        return len(self._pdfium)

    def text_layer(self, index: int) -> str:
        """PDF-ში ჩაშენებული ტექსტური ფენა (შეიძლება ცარიელი ან უხარისხო იყოს)."""
        page = self._pdfium[index]
        try:
            tp = page.get_textpage()
            try:
                return tp.get_text_bounded() or ""
            finally:
                tp.close()
        finally:
            page.close()

    def render(self, index: int, dpi: int = 300, grayscale: bool = True) -> Image.Image:
        """ერთი გვერდის რენდერი PIL სურათად (სამუშაო ასლი; ორიგინალი არ იცვლება)."""
        page = self._pdfium[index]
        try:
            bitmap = page.render(scale=dpi / 72.0, grayscale=grayscale)
            img = bitmap.to_pil()
            bitmap.close()
            return img.convert("L") if grayscale else img.convert("RGB")
        finally:
            page.close()

    def page_size_pt(self, index: int) -> tuple[float, float]:
        page = self._pdfium[index]
        try:
            return page.get_size()
        finally:
            page.close()

    def page_content_hash(self, index: int) -> str:
        """გვერდის შიგთავსის ჰეში (content stream + გამოსახულებები) — დოკუმენტის დონის ზუსტი დუბლიკატისთვის."""
        if self._reader is None:
            self._reader = PdfReader(self.path, strict=False)
        page = self._reader.pages[index]
        h = hashlib.sha256()
        contents = page.get_contents()
        if contents is not None:
            h.update(contents.get_data())
        res = page.get("/Resources")
        res = res.get_object() if res is not None else None
        xobjs = res.get("/XObject") if res is not None else None
        if xobjs is not None:
            xobjs = xobjs.get_object()
            for name in sorted(xobjs.keys()):
                obj = xobjs[name].get_object()
                h.update(name.encode("utf-8", "replace"))
                h.update(getattr(obj, "_data", b"") or b"")
        return h.hexdigest()


def write_subset(src_path: str | Path, pages: list[int], dest_path: str | Path) -> int:
    """წყაროს მითითებული გვერდების (0-დან) უდანაკარგო კოპირება ახალ PDF-ში. აბრუნებს გვერდების რაოდენობას."""
    reader = PdfReader(str(src_path), strict=False)
    writer = PdfWriter()
    for i in pages:
        writer.add_page(reader.pages[i])
    buf = io.BytesIO()
    writer.write(buf)
    Path(dest_path).write_bytes(buf.getvalue())
    return len(pages)


def count_pages(path: str | Path) -> int:
    return len(PdfReader(str(path), strict=False).pages)


def sha256_file(path: str | Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()
