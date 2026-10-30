"""OCR პროვაიდერები და ჯაჭვი. ონლაინ პროვაიდერი მხოლოდ აშკარა კონფიგურაციით ჩაირთვება."""
from __future__ import annotations

from .base import OcrError, OcrProvider, OcrResult
from .chain import ChainResult, OcrChain, build_ocr_chain
from .online import DailyBudget, OnlineVisionProvider, gate_open
from .tesseract import TesseractProvider, parse_osd, parse_tsv

__all__ = ["OcrError", "OcrProvider", "OcrResult", "ChainResult", "OcrChain", "build_ocr_chain",
           "DailyBudget", "OnlineVisionProvider", "gate_open", "TesseractProvider", "parse_osd", "parse_tsv"]
