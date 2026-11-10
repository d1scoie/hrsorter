"""OCR პროვაიდერის საერთო ინტერფეისი (კონტრაქტი ARCHITECTURE.md-ში)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from PIL import Image

from ..errors import HRSorterError
from ..models import ErrorCode


class OcrError(HRSorterError):
    """პროვაიდერის შეცდომა (timeout, პროცესის ავარია, ქსელი). retryable=True → ხელახალი ცდა აზრიანია."""

    def __init__(self, detail: str = "", retryable: bool = False):
        super().__init__(ErrorCode.OCR_FAILED.value, detail, retryable)


@dataclass
class OcrResult:
    text: str
    mean_confidence: float | None = None           # 0..1; None თუ პროვაიდერი არ იძლევა
    words: list[dict[str, Any]] = field(default_factory=list)  # {text, conf (0..1|None), bbox (l,t,r,b) px}
    signature_parties: dict[str, bool] | None = None  # ონლაინ მკითხველის პასუხი „ვინ მოაწერა ხელი“ (ocr/signatures.py); None — არ უთქვამს


@runtime_checkable
class OcrProvider(Protocol):
    name: str
    model_version: str
    is_online: bool

    def available(self) -> bool: ...
    def recognize(self, image: Image.Image, lang: str) -> OcrResult: ...
