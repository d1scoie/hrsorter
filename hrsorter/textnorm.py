"""ქართული ტექსტის ნორმალიზაცია — კლასიფიკაციის, ამოღებისა და შედარებისთვის საერთო."""
from __future__ import annotations

import re
import unicodedata

_QUOTES = str.maketrans({"„": '"', "“": '"', "”": '"', "«": '"', "»": '"', "‘": "'", "’": "'", "`": "'",
                         "–": "-", "—": "-", "‐": "-", "‑": "-", " ": " ", "​": ""})
_WS = re.compile(r"[ \t\f\v]+")
_NL = re.compile(r"\n{2,}")


def is_georgian_char(ch: str) -> bool:
    o = ord(ch)
    return 0x10A0 <= o <= 0x10FF or 0x1C90 <= o <= 0x1CBF or 0x2D00 <= o <= 0x2D2F


def normalize(text: str) -> str:
    """NFC, ერთიანი ბრჭყალები/ტირეები, მთავრული→მხედრული (casefold), ჰარების შეკუმშვა. ხაზები ინახება."""
    t = unicodedata.normalize("NFC", text or "").translate(_QUOTES).casefold()
    lines = [_WS.sub(" ", ln).strip() for ln in t.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return _NL.sub("\n", "\n".join(ln for ln in lines if ln)).strip()


def one_line(text: str) -> str:
    return _WS.sub(" ", normalize(text).replace("\n", " ")).strip()


def title_zone(text: str, max_lines: int = 8, max_chars: int = 400) -> str:
    """გვერდის ზედა ნაწილი (სათაურის ზონა) ნორმალიზებული ტექსტიდან."""
    lines = normalize(text).split("\n")[:max_lines]
    return "\n".join(lines)[:max_chars]


def georgian_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if is_georgian_char(c)) / len(letters)


def digits_only(text: str) -> str:
    return "".join(c for c in text if c.isdigit())
