"""ონლაინ (გარე სერვისის) OCR პროვაიდერის ჩონჩხი — ნაგულისხმევად **გამორთული**.

წესები (სპეცი §16, §18):
  * კარიბჭე: `ocr.online.enabled == true` **და** `ocr.path ∈ {online, hybrid}`. სხვა შემთხვევაში პროვაიდერი
    მიუწვდომელია და არაფერი არ იგზავნება მანქანის გარეთ.
  * საიდუმლო: `keyring.get_password("hrsorter", secret_name)` — არასდროს იწერება ლოგში/შეცდომაში.
  * timeout, შეზღუდული retry (მხოლოდ retryable შეცდომებზე), დღიური გვერდების ბიუჯეტი.
  * ბიუჯეტის მთვლელი **მეხსიერებაშია** — პროცესის გადატვირთვა ნულავს; მუდმივი აღრიცხვა ინტეგრატორმა DB-ში დაამატოს.

კონკრეტული ვენდორი აქ არ არის გამოგონილი: `_call()` აბრუნებს NotImplementedError-ს. ინტეგრატორი
ქვეკლასში ახორციელებს `_call(png_bytes, lang, timeout, secret) -> OcrResult`.
"""
from __future__ import annotations

import io
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from PIL import Image

from .base import OcrError, OcrResult

SERVICE_NAME = "hrsorter"
_ONLINE_PATHS = {"online", "hybrid"}


def gate_open(ocr_cfg: dict[str, Any]) -> bool:
    """ონლაინ გზა ღიაა მხოლოდ აშკარა ორმაგი თანხმობით."""
    online = ocr_cfg.get("online") or {}
    return bool(online.get("enabled")) and ocr_cfg.get("path") in _ONLINE_PATHS


@dataclass
class DailyBudget:
    max_pages_per_day: int = 0          # 0 = ლიმიტი არ არის
    today: Callable[[], date] = field(default=date.today)
    _day: date | None = None
    _used: int = 0

    def _roll(self) -> None:
        d = self.today()
        if d != self._day:
            self._day, self._used = d, 0

    @property
    def used(self) -> int:
        self._roll()
        return self._used

    def try_consume(self, n: int = 1) -> bool:
        self._roll()
        if self.max_pages_per_day > 0 and self._used + n > self.max_pages_per_day:
            return False
        self._used += n
        return True


class OnlineVisionProvider:
    is_online = True

    def __init__(self, ocr_cfg: dict[str, Any], *, secret_getter: Callable[[str, str], str | None] | None = None,
                 budget: DailyBudget | None = None, sleep: Callable[[float], None] = time.sleep):
        online = ocr_cfg.get("online") or {}
        self._gate = gate_open(ocr_cfg)
        self.provider_id = str(online.get("provider") or "")
        self.name = f"online:{self.provider_id}" if self.provider_id else "online"
        self.model_version = str(online.get("model") or "")
        self.secret_name = str(online.get("secret_name") or "hrsorter/online_ocr")
        self.timeout = float(online.get("timeout_seconds") or 60)
        self.max_retries = max(0, int(online.get("max_retries") or 0))
        self.budget = budget or DailyBudget(int(online.get("max_pages_per_day") or 0))
        self._secret_getter = secret_getter
        self._sleep = sleep

    # --- საიდუმლო ----------------------------------------------------------------
    def secret(self) -> str | None:
        getter = self._secret_getter
        if getter is None:
            try:
                import keyring  # ლოკალური იმპორტი: ტესტებში backend შეიძლება არ იყოს
                getter = keyring.get_password
            except Exception:
                return None
        try:
            return getter(SERVICE_NAME, self.secret_name)
        except Exception:
            return None

    def available(self) -> bool:
        return self._gate and bool(self.provider_id) and self.secret() is not None

    # --- ამოცნობა ------------------------------------------------------------------
    def _call(self, png_bytes: bytes, lang: str, timeout: float, secret: str) -> OcrResult:
        raise NotImplementedError("configure a concrete online provider")

    def recognize(self, image: Image.Image, lang: str) -> OcrResult:
        if not self._gate:
            raise OcrError("online OCR is disabled by configuration", retryable=False)
        secret = self.secret()
        if secret is None:
            raise OcrError("online OCR secret not found in credential store", retryable=False)
        if not self.budget.try_consume(1):
            raise OcrError("online OCR daily page budget exhausted", retryable=False)
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        png = buf.getvalue()
        attempt = 0
        while True:
            try:
                return self._call(png, lang, self.timeout, secret)
            except OcrError as e:
                attempt += 1
                if not e.retryable or attempt > self.max_retries:
                    raise
                self._sleep(min(30.0, 2.0 ** attempt))
