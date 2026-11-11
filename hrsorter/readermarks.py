"""ონლაინ მკითხველის (Claude) ნიშნები გვერდის ტექსტში: „[ხელნაწერი: …]“, „[ბეჭედი: …]“.

ონლაინ მკითხველი ტექსტს სიტყვების კოორდინატების გარეშე აბრუნებს. გამოსახულების ანალიზს (`pageanalysis`) ამ დროს ტექსტის ნიღაბი
არ აქვს: ნაბეჭდი სტრიქონებიც „ლაქებია“ და ხელნაწერის ევრისტიკა თითქმის ყოველ გვერდზე ირთვებოდა (ნამდვილ სკანებზე 686 გვერდიდან
657) — ხელშეკრულება ყოველ გვერდზე „ხელნაწერის ეჭვით“ ჩერდებოდა. ასეთ გვერდზე ხელნაწერი ის არის, რაც თვითონ მკითხველმა მონიშნა
(„Mark handwritten text as [ხელნაწერი: ...]“), გარდა იმისა, რაც ყოველ ხელმოწერილ გვერდზე მოსალოდნელია: ხელმოწერა, ბეჭდის
აღწერა და ხელმომწერის საკუთარი სახელი ხელმოწერის გვერდით.

ბეჭდის ტექსტსაც მკითხველი ჩვეულებრივ ნიშნით წერს („[ბეჭედი: შპს … 2009 …]“), ზოგ გვერდზე კი წინადადების შუაში, ნიშნის გარეშე —
შედარებაში ეს „ჩამატებული ტექსტია“. `stamp_words` ამ სკანის ბეჭდის სიტყვებს აგროვებს, რომ შედარებამ ისინი იცნოს.

ტექსტი მონაცემია: აქ მხოლოდ იკითხება.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from .textcompare import KEPT_SIMILARITY, NEGATIONS, _found, _is_noise, _tokens

_MARK = re.compile(r"\[([^\]\n]{0,200})\]")
_HAND = re.compile(r"\s*ხელნაწერი\b\s*:?\s*(.*)", re.DOTALL)
_STAMP = re.compile(r"ბეჭედ|ბეჭდ|შტამპ|სტამპ|დამღ|საბეჭდავ")
_SIGNATURE = re.compile(r"ხელმოწერ|ხელმომწერ|პარაფ|signature", re.IGNORECASE)
_FILLER = frozenset({"და", "ან", "თან", "ერთად", "ზედ", "ზემოთ", "ქვემოთ", "გვერდით"})   # „ხელმოწერა და ბეჭედი“, „ხელმოწერა ბეჭედთან ერთად“


def reader_only(pt: Mapping[str, Any]) -> bool:
    """გვერდი თავიდანვე ონლაინ მკითხველმა წაიკითხა და სიტყვების კოორდინატები არ აქვს: გამოსახულებით დადგენილი
    `handwriting_suspected` ასეთ გვერდზე ნიღბის გარეშეა გამოთვლილი და არაფერს ნიშნავს. Tesseract-ის შემდეგ ხელახლა წაკითხული
    გვერდი („…:recheck“) ასეთი არ არის — მისი ნიშნები Tesseract-ის სიტყვების ნიღბით დადგინდა."""
    source = str(pt.get("text_source") or "")
    return source.startswith("ocr:online") and not source.endswith(":recheck") and not pt.get("words")


def handwritten(text: str | None, known: Iterable[str] = ()) -> list[str]:
    """რა მონიშნა მკითხველმა ხელნაწერად — ხელმოწერის, ბეჭდის აღწერისა და ცნობილი სიტყვების (`known`: ხელმომწერის სახელი და
    გვარი MHR-იდან) გარდა. ინიციალი („ნ.“) სიტყვად არ ითვლება. ხელნაწერი, რომელიც მკითხველმა ვერ წაიკითხა („[ხელნაწერი: ?]“),
    ხელმოწერად არ ითვლება და სიაშია. ცარიელი სია — გვერდზე მოულოდნელი ხელნაწერი არ არის."""
    known = list(known)
    out = []
    for m in _MARK.finditer(text or ""):
        hand = _HAND.fullmatch(m.group(1))
        if not hand:
            continue
        body = hand.group(1).strip()
        if _STAMP.search(body):      # „ხელმოწერა, ბეჭედი: შპს … 2009“ — ბეჭდის ტექსტია, არა მიწერა
            continue
        words = [t.word for t in _tokens(body, 0)]
        rest = [w for w in words if not (_is_noise(w) or w in _FILLER or _SIGNATURE.search(w) or _found(w, known, KEPT_SIMILARITY))]
        if words and _SIGNATURE.search(words[0]):
            # მკითხველი ხელმოწერას აღწერს („ხელმოწერა დამსაქმებლის მხარეს“, „ხელმოწერა, ნაწილობრივ ამოუკითხავი“): აღწერის სიტყვები
            # მიწერა არ არის. რიცხვი (თანხა, თარიღი) ან უარყოფა („არ ვეთანხმები“) კი ხელმოწერის აღწერაში არ გვხვდება — ის ჩანს
            rest = [w for w in rest if any(c.isdigit() for c in w) or w in NEGATIONS]
        if rest or not words:
            out.append(body.strip("[] ") or "?")
    return out


def handwriting_suspected(pt: Mapping[str, Any], known: Iterable[str] = ()) -> bool:
    """შენახული გვერდისთვის: კოორდინატების გარეშე წაკითხულზე — მკითხველის ნიშნებით, სხვაზე — გამოსახულების ანალიზის პასუხი."""
    if reader_only(pt):
        return bool(handwritten(pt.get("text"), known))
    return bool(pt.get("handwriting_suspected"))


def stamp_words(texts: Iterable[str | None]) -> list[str]:
    """ამ სკანის ბეჭდის სიტყვები: ყველაფერი, რაც მკითხველმა ბეჭდის ნიშნებში ჩაწერა („[ბეჭედი: …]“, „[შტამპი: …]“,
    „[ხელნაწერი: ხელმოწერა, ბეჭედი: …]“). ერთი და იგივე ბეჭედი ყოველ გვერდზეა, ამიტომ სხვა გვერდის ნიშანი ამ გვერდის ბეჭდის
    სიტყვებსაც ხსნის."""
    seen: dict[str, None] = {}
    for text in texts:
        for m in _MARK.finditer(text or ""):
            if _STAMP.search(m.group(1)):
                for t in _tokens(m.group(1), 0):
                    if not _is_noise(t.word):
                        seen.setdefault(t.word)
    return list(seen)
