"""ტექსტური ფენის / OCR გამოსავლის წაკითხვადობის შეფასება ქართული დოკუმენტებისთვის (0..1).

ეს არის ევრისტიკა და არა ენის მოდელი. მიზანი: კარგი ქართული ტექსტი (≥0.8) აშკარად
გაცალკევდეს „ნაგვისგან" — დაზიანებული ფონტის რუკიდან მიღებული Latin-1 აქცენტები,
Private-Use გლიფები, U+FFFD, შემთხვევითი სიმბოლოები (≤0.3) — და ცარიელი ტექსტისგან (0).

კომპონენტები:
  * script   — ასოებს შორის ქართულის წილი (ASCII ლათინური ნახევარი წონით; სხვა — 0).
  * tokens   — „სიტყვისმაგვარი" ტოკენების წილი: მხოლოდ ქართული ასოები, ნორმალური
               ლათინური სიტყვა (Word / word / WORD), რიცხვი/თარიღი (12.03.2024, 01001000001).
               `xelSekruleba`-ს ტიპის შერეული რეგისტრი (ძველი ASCII-ქართული ფონტები) სიტყვად არ ითვლება.
  * bad      — U+FFFD, PUA, კონტროლ-სიმბოლოები, კომბინირებადი ნიშნები, ჩარჩოს სიმბოლოები.
  * სიგრძე   — 30 ასო/ციფრამდე ქულა პროპორციულად მცირდება (ძალიან მოკლე ტექსტი არასანდოა).

ცნობილი შეზღუდვები: სუფთა ინგლისური ტექსტი ~0.75-ს იღებს (დომენი ქართულია, ასეც მისაღებია);
სემანტიკურად უაზრო, მაგრამ „სწორი" ქართული ასოებისგან შემდგარი ტექსტი მაღალ ქულას მიიღებს.
"""
from __future__ import annotations

import re
import unicodedata

from .textnorm import is_georgian_char

_GEO_WORD = re.compile(r"^[Ⴀ-ჿᲐ-Ჿⴀ-⴯]{2,}$")
_LAT_WORD = re.compile(r"^(?:[A-Z]?[a-z]{1,}|[A-Z]{2,})$")
_NUMBER = re.compile(r"^[+№#]?\d+(?:[.,/:\-]\d+)*%?$")
_STRIP_PUNCT = "\"'„“”«»()[]{}.,;:!?-–—/\\№#*_|<>"
_SHORT_FULL_CREDIT = 30


def _is_bad_char(ch: str) -> bool:
    o = ord(ch)
    if ch == "�":
        return True
    if 0xE000 <= o <= 0xF8FF or 0xF0000 <= o <= 0x10FFFF:  # private use
        return True
    if 0x2500 <= o <= 0x25FF:  # box drawing / block elements / geometric shapes
        return True
    cat = unicodedata.category(ch)
    if cat in ("Mn", "Me", "Mc"):  # კომბინირებადი ნიშნები — ქართულ ტექსტში არ გვხვდება
        return True
    if cat == "Cc" and ch not in "\n\r\t\f\v":
        return True
    return cat in ("Co", "Cn", "Cs")


def _token_kind(tok: str) -> str:
    """'good' | 'bad' | 'skip' (ერთსიმბოლოიანი/ცარიელი ტოკენები ნეიტრალურია)."""
    t = tok.strip(_STRIP_PUNCT)
    if len(t) <= 1:
        return "skip"
    if _GEO_WORD.match(t) or _LAT_WORD.match(t) or _NUMBER.match(t):
        return "good"
    return "bad"


def score(text: str) -> float:
    """0..1 — რამდენად წაკითხვადია ტექსტი (არა რამდენად სწორია შინაარსი)."""
    if not text or not text.strip():
        return 0.0
    t = unicodedata.normalize("NFC", text)
    nonspace = [c for c in t if not c.isspace()]
    if not nonspace:
        return 0.0
    letters = [c for c in nonspace if c.isalpha()]
    digits = sum(1 for c in nonspace if c.isdigit())
    if not letters and digits == 0:
        return 0.0

    geo = sum(1 for c in letters if is_georgian_char(c))
    lat = sum(1 for c in letters if "a" <= c.lower() <= "z")
    script = (geo + 0.5 * lat) / len(letters) if letters else 0.0

    bad = sum(1 for c in nonspace if _is_bad_char(c))
    bad_ratio = bad / len(nonspace)

    toks = t.split()
    unknown = sum(1 for tok in toks if "[?]" in tok)        # ონლაინ OCR-ის „ამოუცნობი“ მარკერი
    kinds = [_token_kind(tok) for tok in toks if "[?]" not in tok]
    considered = [k for k in kinds if k != "skip"]
    token_score = (sum(1 for k in considered if k == "good") / len(considered)) if considered else 0.0
    coverage = 1.0 - unknown / (unknown + len(considered)) if unknown else 1.0

    raw = 0.5 * script + 0.35 * token_score + 0.15 * (1.0 - bad_ratio) - 1.5 * bad_ratio
    length_factor = min(1.0, (len(letters) + digits) / _SHORT_FULL_CREDIT)
    return round(max(0.0, min(1.0, raw * length_factor * coverage)), 4)
