"""Tesseract CLI პროვაიდერი (subprocess, pytesseract-ის გარეშე).

* ბინარი: `cfg.ocr.tesseract_cmd` ან PATH-ზე `tesseract`.
* ენა: ნაგულისხმევად `kat+eng`; `available()` ამოწმებს ბინარსაც და ყველა მოთხოვნილი traineddata-საც (`--list-langs`).
* გამოსავალი TSV ფორმატში → სიტყვების სანდოობები და bbox-ები.
* `osd()` — ორიენტაცია `--psm 0`-ით, მხოლოდ თუ `osd` traineddata არსებობს.
* ყოველ გვერდზე timeout.

შეზღუდვა: Tesseract ქართულ **ხელნაწერს** არ კითხულობს — ეს პროვაიდერი მხოლოდ ბეჭდურ ტექსტზეა განკუთვნილი.
ამ მანქანაზე Tesseract დაყენებული არ არის; პარსინგი ტესტირდება ყალბი subprocess-ით.
"""
from __future__ import annotations

import io
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from .base import OcrError, OcrResult

Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]

_ROTATE_RE = re.compile(r"^Rotate:\s*(\d+)", re.MULTILINE)
_VERSION_RE = re.compile(r"tesseract\s+v?([\w.\-]+)", re.IGNORECASE)
_TSV_COLUMNS = ("level", "page_num", "block_num", "par_num", "line_num", "word_num",
                "left", "top", "width", "height", "conf", "text")


def parse_tsv(tsv: str) -> OcrResult:
    """Tesseract-ის TSV (level 5 = სიტყვა) → OcrResult. ტექსტი იწყობა ბლოკი/აბზაცი/ხაზის მიხედვით."""
    words: list[dict[str, Any]] = []
    lines_out: list[str] = []
    current_key: tuple[int, int, int] | None = None
    current_words: list[str] = []
    prev_block: tuple[int, int] | None = None
    for raw in tsv.splitlines():
        parts = raw.rstrip("\r").split("\t")
        if len(parts) < 12 or parts[0] == "level":
            continue
        try:
            level = int(parts[0])
        except ValueError:
            continue
        if level != 5:
            continue
        text = parts[11].strip()
        if not text:
            continue
        try:
            block, par, line = int(parts[2]), int(parts[3]), int(parts[4])
            left, top, width, height = (int(float(parts[i])) for i in range(6, 10))
            conf_raw = float(parts[10])
        except ValueError:
            continue
        conf = None if conf_raw < 0 else max(0.0, min(1.0, conf_raw / 100.0))
        words.append({"text": text, "conf": conf, "bbox": (left, top, left + width, top + height)})
        key = (block, par, line)
        if key != current_key:
            if current_words:
                lines_out.append(" ".join(current_words))
            if prev_block is not None and (block, par) != prev_block:
                lines_out.append("")
            current_key, current_words, prev_block = key, [], (block, par)
        current_words.append(text)
    if current_words:
        lines_out.append(" ".join(current_words))
    confs = [w["conf"] for w in words if w["conf"] is not None]
    mean_conf = (sum(confs) / len(confs)) if confs else None
    return OcrResult(text="\n".join(lines_out).strip(), mean_confidence=mean_conf, words=words)


def parse_osd(output: str) -> int | None:
    m = _ROTATE_RE.search(output)
    if not m:
        return None
    r = int(m.group(1)) % 360
    return r if r in (0, 90, 180, 270) else None


def _png(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


class TesseractProvider:
    name = "tesseract"
    is_online = False

    def __init__(self, cmd: str | None = None, lang: str = "kat+eng", psm: int = 3,
                 timeout_seconds: float = 120.0, runner: Runner | None = None, tessdata_dir: str | None = None):
        self._cmd_cfg = (cmd or "").strip()
        self.tessdata_dir = tessdata_dir or None   # ენების ფაილები ადმინის უფლების გარეშე (Program Files-ის ნაცვლად)
        self.lang = lang or "kat+eng"
        self.psm = int(psm)
        self.timeout = float(timeout_seconds)
        self._run: Runner = runner or subprocess.run
        self._langs: set[str] | None = None
        self._version: str | None = None

    # --- ბინარი და ენები -------------------------------------------------------
    def resolve_cmd(self) -> str | None:
        if self._cmd_cfg:
            return self._cmd_cfg if Path(self._cmd_cfg).exists() else None
        return shutil.which("tesseract")

    def _exec(self, args: list[str], timeout: float | None = None,
              stdin: bytes | None = None) -> "subprocess.CompletedProcess[bytes]":
        cmd = self.resolve_cmd()
        if not cmd:
            raise OcrError("tesseract binary not found", retryable=False)
        kwargs: dict[str, Any] = {"capture_output": True, "timeout": timeout or self.timeout}
        if stdin is not None:
            kwargs["input"] = stdin
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            extra = ["--tessdata-dir", self.tessdata_dir] if self.tessdata_dir else []
            return self._run([cmd, *extra, *args], **kwargs)
        except subprocess.TimeoutExpired as e:
            raise OcrError(f"tesseract timeout after {kwargs['timeout']}s", retryable=True) from e
        except OSError as e:
            raise OcrError(f"tesseract launch failed: {e}", retryable=False) from e

    @staticmethod
    def _decode(b: bytes | None) -> str:
        return (b or b"").decode("utf-8", errors="replace")

    def langs(self) -> set[str]:
        if self._langs is None:
            try:
                cp = self._exec(["--list-langs"], timeout=30)
                out = self._decode(cp.stdout) + "\n" + self._decode(cp.stderr)
                self._langs = {ln.strip() for ln in out.splitlines()
                               if ln.strip() and not ln.lower().startswith("list of") and " " not in ln.strip()}
            except OcrError:
                self._langs = set()
        return self._langs

    @property
    def model_version(self) -> str:
        if self._version is None:
            self._version = ""
            if self.resolve_cmd():
                try:
                    cp = self._exec(["--version"], timeout=30)
                    m = _VERSION_RE.search(self._decode(cp.stdout) + self._decode(cp.stderr))
                    self._version = f"tesseract {m.group(1)}" if m else "tesseract"
                except OcrError:
                    self._version = ""
        return self._version

    def available(self) -> bool:
        if not self.resolve_cmd():
            return False
        have = self.langs()
        return all(l in have for l in self.lang.split("+") if l)

    def has_osd(self) -> bool:
        return bool(self.resolve_cmd()) and "osd" in self.langs()

    # --- ამოცნობა --------------------------------------------------------------
    def recognize(self, image: Image.Image, lang: str | None = None) -> OcrResult:
        lang = lang or self.lang
        # სურათი stdin-ით: Tesseract Windows-ზე ვერ კითხულობს არა-ლათინურ ბილიკებს (მაგ. ქართულ მომხმარებლის სახელს)
        cp = self._exec(["stdin", "stdout", "-l", lang, "--psm", str(self.psm), "-c", "tessedit_create_tsv=1"],
                        stdin=_png(image))  # "-c": configs/ საქაღალდე tessdata_dir-ში შეიძლება არ იყოს
        if cp.returncode != 0:
            raise OcrError(f"tesseract exit {cp.returncode}: {self._decode(cp.stderr)[:200]}", retryable=False)
        return parse_tsv(self._decode(cp.stdout))

    def osd(self, image: Image.Image) -> int | None:
        """ორიენტაცია (0/90/180/270, საათის ისრით გასასწორებელი) ან None, თუ OSD მიუწვდომელია/ვერ დადგინდა."""
        if not self.has_osd():
            return None
        try:
            cp = self._exec(["stdin", "stdout", "--psm", "0", "-l", "osd"], stdin=_png(image))
        except OcrError:
            return None
        if cp.returncode != 0:
            return None
        return parse_osd(self._decode(cp.stdout) + "\n" + self._decode(cp.stderr))
