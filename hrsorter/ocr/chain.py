"""OCR ჯაჭვი: ადგილობრივი და (მხოლოდ აშკარა ნებართვით) ონლაინ პროვაიდერის შეერთება.

`ocr.path`:
  * local  — მხოლოდ ადგილობრივი;
  * online — მხოლოდ ონლაინ (თუ კარიბჭე ღიაა, სხვაგვარად ჯაჭვი ცარიელია);
  * hybrid — ჯერ ადგილობრივი; ონლაინ მხოლოდ იმ გვერდზე, რომლის ადგილობრივი ხარისხი < thresholds.ocr_min_quality.

ხარისხი (0..1): `textquality.score(text)`; თუ პროვაიდერი სიტყვების სანდოობას იძლევა —
score × (სიტყვების წილი, რომელთა conf ≥ 0.8). რეალურ მკრთალ სკანებზე ნაგავიც ქართული ასოებისგან შედგება
(score ≈ 0.93), ხოლო მაღალი სანდოობის სიტყვების წილი კარგ (≥0.8) და ცუდ (≈0.3) გვერდს მკაფიოდ ჰყოფს.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

from .. import textquality
from ..config import PROJECT_DIR, Config
from .base import OcrError, OcrProvider, OcrResult
from .online import OnlineVisionProvider, gate_open
from .tesseract import TesseractProvider


HIGH_WORD_CONF = 0.8


def result_quality(res: OcrResult) -> float:
    q = textquality.score(res.text)
    confs = [w["conf"] for w in res.words if w.get("conf") is not None and str(w.get("text", "")).strip()]
    if confs:
        return round(q * sum(1 for c in confs if c >= HIGH_WORD_CONF) / len(confs), 4)
    if res.mean_confidence is None:
        return q
    return round(min(1.0, 0.6 * q + 0.4 * max(0.0, min(1.0, res.mean_confidence))), 4)


@dataclass
class ChainResult:
    result: OcrResult | None
    provider: OcrProvider | None
    quality: float = 0.0
    attempts: list[dict[str, Any]] = field(default_factory=list)   # {provider, ok, quality, error}
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.result is not None and self.provider is not None


class OcrChain:
    def __init__(self, local: OcrProvider | None, online: OcrProvider | None, path: str,
                 min_quality: float, lang: str):
        self.local = local
        self.online = online
        self.path = path
        self.min_quality = float(min_quality)
        self.lang = lang

    def providers(self) -> list[OcrProvider]:
        order: list[OcrProvider | None]
        if self.path == "online":
            order = [self.online]
        elif self.path == "hybrid":
            order = [self.local, self.online]
        else:
            order = [self.local]
        return [p for p in order if p is not None]

    def available(self) -> bool:
        return any(p.available() for p in self.providers())

    def sends_outside_machine(self) -> bool:
        return self.online is not None and self.online.available()

    def osd(self, image: Image.Image) -> int | None:
        """ორიენტაცია ადგილობრივი პროვაიდერით (Tesseract OSD), თუ შეუძლია; სხვაგვარად None."""
        fn = getattr(self.local, "osd", None)
        if fn is None:
            return None
        try:
            return fn(image)
        except Exception:
            return None

    def recognize(self, image: Image.Image, lang: str | None = None) -> ChainResult:
        lang = lang or self.lang
        out = ChainResult(result=None, provider=None)
        best: tuple[float, OcrResult, OcrProvider] | None = None
        for p in self.providers():
            if not p.available():
                out.attempts.append({"provider": p.name, "ok": False, "error": "unavailable"})
                continue
            if p.is_online and best is not None and best[0] >= self.min_quality:
                break  # hybrid: ადგილობრივი საკმარისია — გარეთ არაფერი იგზავნება
            try:
                res = p.recognize(image, lang)
            except (OcrError, NotImplementedError) as e:
                out.attempts.append({"provider": p.name, "ok": False, "error": str(e)[:200]})
                out.warnings.append(f"ocr_provider_failed:{p.name}")
                continue
            q = result_quality(res)
            out.attempts.append({"provider": p.name, "ok": True, "quality": q})
            if best is None or q > best[0]:
                best = (q, res, p)
            if q >= self.min_quality:
                break
        if best is not None:
            out.quality, out.result, out.provider = best
            if out.quality < self.min_quality:
                out.warnings.append("low_ocr_quality")
        return out

    def describe(self) -> dict[str, Any]:
        """UI-სთვის: რომელი პროვაიდერი აქტიურია და იგზავნება თუ არა რამე მანქანის გარეთ."""
        def info(p: OcrProvider | None) -> dict[str, Any] | None:
            if p is None:
                return None
            return {"name": p.name, "available": p.available(), "model_version": p.model_version,
                    "is_online": p.is_online}
        return {
            "path": self.path,
            "local": info(self.local),
            "online": info(self.online),
            "online_gate_open": self.online is not None,
            "sends_outside_machine": self.sends_outside_machine(),
            "external_send_mode": "none" if not self.sends_outside_machine()
            else ("all_pages" if self.path == "online" else "low_quality_pages_only"),
            "any_available": self.available(),
        }


def _resolve_dir(d: str | None) -> str | None:
    if not d:
        return None
    p = Path(d)
    return str(p if p.is_absolute() else PROJECT_DIR / p)


def build_orientation_reader(cfg: Config):
    """სურათი → ტექსტი, მხოლოდ იმის გასაგებად, რომელი მხარეა „ზემოთ“ (`idimage.rotations`): Tesseract ინგლისურით, ერთ ბლოკად
    (`--psm 6`). ნაგულისხმევი `--psm 3` გვერდულ ტექსტსაც თვითონ აბრუნებს და ორი მხარე ერთნაირ ქულას იღებდა; ქართულის გარეშე
    კი მანქანური ზოლი და ლათინური წარწერები უფრო სუფთად იკითხება. None — ადგილობრივი OCR არ არის."""
    ocr = cfg.data["ocr"]
    if ocr.get("local_provider", "tesseract") != "tesseract":
        return None
    reader = TesseractProvider(cmd=ocr.get("tesseract_cmd") or None, lang="eng", psm=6,
                               timeout_seconds=float(ocr.get("tesseract_timeout_seconds", 120)),
                               tessdata_dir=_resolve_dir(ocr.get("tessdata_dir")))
    if not reader.available():
        return None
    return lambda image: reader.recognize(image, "eng").text


def build_ocr_chain(cfg: Config) -> OcrChain:
    ocr = cfg.data["ocr"]
    lang = ocr.get("tesseract_lang") or "kat+eng"
    local: OcrProvider | None = None
    if ocr.get("local_provider", "tesseract") == "tesseract":
        local = TesseractProvider(cmd=ocr.get("tesseract_cmd") or None, lang=lang,
                                  psm=int(ocr.get("tesseract_psm", 3)),
                                  timeout_seconds=float(ocr.get("tesseract_timeout_seconds", 120)),
                                  tessdata_dir=_resolve_dir(ocr.get("tessdata_dir")))
    online: OcrProvider | None = None
    if gate_open(ocr):
        if (ocr.get("online") or {}).get("provider") == "anthropic":
            from .anthropic_vision import AnthropicVisionProvider
            online = AnthropicVisionProvider(ocr)
        elif (ocr.get("online") or {}).get("provider") == "claude_code":
            from .claude_code_vision import ClaudeCodeVisionProvider
            online = ClaudeCodeVisionProvider(ocr)
        else:
            online = OnlineVisionProvider(ocr)
    return OcrChain(local=local, online=online, path=str(ocr.get("path") or "local"),
                    min_quality=float(cfg.thresholds.get("ocr_min_quality", 0.35)), lang=lang)
