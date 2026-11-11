"""დასკანერებული (OCR) ასლის ტექსტის შედარება MHR-ის ორიგინალის ტექსტთან — OCR-ის ხმაურის იგნორირებით.

შემავალი: ორი სია გვერდების ტექსტებით (ორიგინალი — ციფრული PDF-ის ტექსტური ფენა; დასკანერებული — Tesseract).
გამომავალი: `TextDiff`-ების სია. ყველა განსხვავება ინახება; `significant` აღნიშნავს შინაარსობრივს.

ალგორითმი:
  1. გვერდების რაოდენობა განსხვავდება → TextDiff(kind="page_count", significant=True); შედარება მთელ
     დოკუმენტზე (გვერდების გადაბმით), სხვაგვარად — გვერდი გვერდთან.
  2. ტოკენიზაცია: `textnorm.normalize` → გადატანის დეფისის („-\\n") გაერთიანება → სიტყვებად დაყოფა →
     კიდეების სასვენი ნიშნების მოჭრა → მხოლოდ-სასვენი ტოკენები იგდება.
  3. სიტყვების დონის `difflib.SequenceMatcher(autojunk=False)`; მეზობელი არატოლი ოპერაციები, რომლებსაც
     ≤1 ტოლი სიტყვა ყოფს, ერთ ჯგუფად ერთიანდება (OCR-ის შემთხვევითი დამთხვევა ჯგუფს არ უნდა ჰყოფდეს).
  4. ჯგუფის მნიშვნელოვნება (`significant=True`), თუ რომელიმე სრულდება:
       * ციფრები: ორი მხარის ციფრების მიმდევრობა განსხვავდება და (ორიგინალში ციფრები იყო, ან
         დასკანერებულში ≥3 ციფრიანი რიცხვი გაჩნდა) — თანხა, თარიღი, პ/ნ, პუნქტის ნომერი;
       * ≥3 „ნამდვილი" სიტყვის ჩამატება/ამოღება (ან სიგრძეების სხვაობა ≥3 შეცვლილ ჯგუფში);
       * შეცვლილი სიტყვა, რომლის სიმბოლოური მსგავსება < SIMILARITY_MIN — OCR-ის შეცდომა არ არის;
       * „არ", „ვერ"… გაქრა/გაჩნდა ან „სწორი" გახდა „არასწორი" — ერთი სიტყვა აზრს პირიქით ცვლის.
     1-2 ნამდვილი სიტყვის ჩამატება/ამოღება და მსგავსი, მაგრამ სხვა სიტყვა („დასაშვებია"/„დაუშვებელია") Tesseract-ის
     გვერდზე `suspect`-ია: გვერდი Claude-მა ხელახლა უნდა წაიკითხოს. სანდო ტექსტის გვერდზე (`trusted_pages`) იგივე უკვე
     `significant`-ია — ნამდვილ სკანებზე Claude მთელ სიტყვას არ კარგავს და ერთ ასოს ურევს, ამიტომ ერთი ასო ისევ ხმაურია.
     სხვა ყველაფერი — OCR-ის ხმაური (significant=False), მაგრამ სიაში რჩება.
  5. დასკანერებულ მხარეზე ნაგვის ტოკენები (1 სიმბოლო; 2 სიმბოლო, რომელიც ქართული სიტყვა არ არის — „3/6", „_")
     მნიშვნელოვნებას არ ქმნის. ცნობილი OCR-კონფუზიები (3→პ, 6→ნ, შ→ძ …) მხოლოდ მსგავსების გამოთვლისას
     „სწორდება" — არა ციფრების წესისთვის.

დაუნომრავი ორიგინალი (`original_paginated=False`, მაგ. MHR-ის .docx): ორიგინალი ერთი ტექსტია, დასკანერებული —
გვერდები; შედარება მთელ დოკუმენტზე, page_count diff-ის გარეშე, diff-ის `page` = დასკანერებულის გვერდი. მანამდე ორივე
მხრიდან (სიმეტრიულად) იშლება „გამჭოლი" ხაზები: `strip_lines` (docx-ის header/footer) და დასკანერებულის ≥3 გვერდიდან
≥50%-ის კიდეებზე (პირველი/ბოლო RUNNING_EDGE_LINES ხაზი) განმეორებული ხაზი (მსგავსება ≥ RUNNING_SIMILARITY და
იგივე ციფრები — შეცვლილი თარიღი სათაურში არ „იმალება"). დასკანერებულიდან ასევე იშლება გვერდის ნომრის ხაზი,
რომლის ნომერი ამ გვერდის ნომერს ემთხვევა („2", „- 2 -", „გვერდი 2/3").

კონსერვატიულობა: ტყუილად „შეცვლილია" ბევრად უკეთესია, ვიდრე გამოტოვებული რეალური ცვლილება. შეზღუდვები: ერთი ასოს
სხვაობა ხმაურია (მათ შორის Claude-ის წაკითხვაშიც — „ღ"/„ლ", „ბ"/„ზ"); ხელმოწერის ზონაში OCR-ის ≥3 „ნამდვილი" სიტყვა
მნიშვნელოვანი იქნება (ოპერატორი ნახავს). დასკანერებული ტექსტი მონაცემია — აქ არაფერი „ასრულებს" მას.
"""
from __future__ import annotations

import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher

from .mhr_models import TextDiff
from .textnorm import is_georgian_char, normalize

SIMILARITY_MIN = 0.6       # შეცვლილი სიტყვის სიმბოლოური მსგავსება ამაზე დაბალი → შინაარსობრივი ცვლილება
RUN_MIN_WORDS = 3          # ამდენი (ან მეტი) სიტყვის ჩამატება/ამოღება → მნიშვნელოვანი
RUN_MIN_WORD_LEN = 3       # მონაკვეთის წესში სიტყვად ითვლება ≥3 ასოიანი ტოკენი
NEW_NUMBER_MIN_DIGITS = 3  # დასკანერებულში ახლად გაჩენილი რიცხვი ამდენი ციფრით → მნიშვნელოვანი („3/6" — არა)
NEGATIONS = frozenset({"არ", "არა", "ვერ", "ნუ", "არც", "ვერც", "ნურც", "აღარ", "ვეღარ", "ნუღარ"})   # ერთი ასეთი სიტყვა აზრს პირიქით ცვლის
NEGATING_PREFIX = "არა"    # „სწორი" / „არასწორი"
ADDED_WORD_MIN_LETTERS = 4  # Tesseract-ის გვერდზე უფრო მოკლე ჩამატებული სიტყვა ბეჭდის/ლოგოს ნაგავია („თო", „მაყ", „კას")
INFLECTION_PREFIX_SHARE = 0.6   # ამ წილზე მეტი საერთო დასაწყისი → სიტყვები მხოლოდ დაბოლოებით განსხვავდება („მომზადების"/„მომზადება")
KEPT_SIMILARITY = 0.8      # ბოლო გვერდზე სიტყვის ასლების დათვლისას
KEPT_LONG_NUMBER = 6
SNIPPET_MAX = 160
CONTEXT_WORDS = 3
PARTS_MAX = 400            # ვებ-გვერდზე ნაჩვენები შეცვლილი ტექსტის ზღვარი
CLAUSE_LOOKBACK = 400      # პუნქტის ნომერი ამდენ სიტყვაში იძებნება
RUNNING_MIN_PAGES = 3      # გამჭოლი ხაზების ძებნა მხოლოდ ამდენ (ან მეტ) გვერდიან სკანზე
RUNNING_SHARE = 0.5        # ხაზი გვერდების ამ წილის კიდეებზე მაინც უნდა მეორდებოდეს
RUNNING_SIMILARITY = 0.85  # ნორმალიზებული ხაზების მსგავსება (OCR-ის ხმაური)
RUNNING_EDGE_LINES = 3     # გვერდის კიდე = პირველი და ბოლო ამდენი ხაზი
RUNNING_MIN_CHARS = 3
_PAGE_NO = re.compile(r"(?:გვერდი|გვ|page|p)?\s*(\d{1,3})(?:\s*(?:/|of|-|\|)?\s*\d{1,3})?")
_PUNCT = "\"'„“”«»()[]{}.,;:!?-–—/\\№#*_|<>§«»+="
_HYPHEN_BREAK = re.compile(r"(?<=[ა-ჰa-z])-\n(?=[ა-ჰa-z])")
# Tesseract-ის (kat) ტიპური კონფუზიები: დასკანერებული სიმბოლო → ორიგინალის სავარაუდო სიმბოლო.
# გამოიყენება მხოლოდ მსგავსების გამოთვლისას („პ/ნ"→„3/6" ხმაურია, არა ცვლილება).
# თანამდებობის შაბლონის (MHR, PHPWord) ცვლადი `${key-ლეიბლი}` → ეს ტოკენი; ჯგუფში, სადაც ის არის, სკანის შევსებული
# მნიშვნელობა (სახელი, პ/ნ, თარიღი, ხელფასი) სხვაობა არ არის, შაბლონის დანარჩენი სიტყვები კი სკანში უნდა იყოს
PLACEHOLDER = "xxvarxx"
_PLACEHOLDER_RE = re.compile(r"\$\{[^}]*\}")


def mark_placeholders(text: str) -> str:
    return _PLACEHOLDER_RE.sub(f" {PLACEHOLDER} ", text)


_OCR_CONFUSION = str.maketrans({"წ": "ნ", "3": "პ", "6": "ნ", "0": "ო", "8": "ვ", "შ": "ძ", "მ": "ძ", "ღ": "ლ", "ჟ": "კ",
                                "o": "ო", "l": "ლ"})


@dataclass
class _Tok:
    word: str
    page: int   # 1-დან


_GLUED_LABEL = re.compile(r"^(\d+(?:\.\d+)+)\.?([ა-ჰa-z].+)$")   # OCR: „4.2.ჰიგიენის" — ნომერი სიტყვას ეწება


def _tokens(text: str, page: int) -> list[_Tok]:
    t = _HYPHEN_BREAK.sub("", normalize(text))
    out = []
    for w in t.replace("\n", " ").split():
        w = w.strip(_PUNCT)
        if not w:
            continue
        m = _GLUED_LABEL.match(w)
        if m:
            out.append(_Tok(m.group(1), page))
            w = m.group(2)
        out.append(_Tok(w, page))
    return out


_LATIN_WORD = re.compile(r"[a-z~|]{3,}")


def _is_noise(word: str) -> bool:
    """დასკანერებული მხარის ნაგვის ტოკენი: 1 სიმბოლო, ან 2 სიმბოლო, რომელიც არც ქართული სიტყვაა, არც რიცხვი („3/6", „_")."""
    if len(word) <= 1:
        return True
    return len(word) == 2 and not word.isdigit() and not all(is_georgian_char(c) for c in word)


_SHORT_GARBAGE = re.compile(r"[a-z0-9~|]{2,4}")
_ORDINAL = re.compile(r"(მე-)(\d+)|(\d+)(-ე)")


def _ocr_failed_word(orig: str, scanned: str) -> bool:
    """ქართული სიტყვის ადგილას ლათინური ასოები („ზიანის" → „bosbol") — OCR-მა სიტყვა ვერ წაიკითხა; მოკლე სიტყვის
    (≤3 ასო) ადგილას ამავე სიგრძის ლათინური/ციფრები („თუ" → „my", „ან" → „36"). მხოლოდ ერთი-ერთზე ჩანაცვლებისას;
    დამატებული ლათინური ტექსტი ან ლათინური სიტყვის შეცვლა („tbc" → „bog") ისევ სხვაობაა."""
    if not orig or not all(is_georgian_char(c) for c in orig):
        return False
    if _LATIN_WORD.fullmatch(scanned):
        return True
    return len(orig) <= 3 and abs(len(scanned) - len(orig)) <= 1 and bool(_SHORT_GARBAGE.fullmatch(scanned))


def _ordinal_noise(orig: str, scanned: str) -> bool:
    """„მე-3 მუხლი" → „მე-33": რიგითი რიცხვის ციფრი OCR-მა გააორმაგა/დაკარგა (≤1 ციფრი) — მითითებაა, არა თანხა."""
    m = _ORDINAL.fullmatch(orig)
    if not m:
        return False
    prefix, digits = (m.group(1), m.group(2)) if m.group(1) else ("", m.group(3))
    suffix = "" if m.group(1) else m.group(4)
    core = scanned[len(prefix):len(scanned) - len(suffix)] if len(scanned) > len(prefix) + len(suffix) else ""
    return (scanned.startswith(prefix) and scanned.endswith(suffix) and core.isdigit()
            and abs(len(core) - len(digits)) == 1 and _edit_distance(digits, core) == 1)


_GARBAGE = re.compile(r"[a-z~|]")


def _garbage_run(words: list[str]) -> bool:
    """ჩამატებული ტოკენები ლათინური ნაგვით („so ~ 19573498 დ ო 2495 20092" — ბეჭდის ტექსტი OCR-ში): ახალი რიცხვი
    აქ ცვლილება არ არის. სუფთა ჩამატებული რიცხვი („2500") კი რჩება მნიშვნელოვანი."""
    return any(_GARBAGE.search(w) for w in words)


def _digits(words: list[str]) -> str:
    return "".join(c for w in words for c in w if c.isdigit())


_LONE_CONFUSED_DIGIT = re.compile(r"(?<!\d)[0368](?!\d)")
_LETTER_AS_DIGIT = str.maketrans({"ი": "0", "ო": "0", "ზ": "8", "ვ": "3", "ნ": "6"})   # რიცხვში ასოდ წაკითხული ციფრი („40" → „4ი")


def _digits_match(a: list[str], b: list[str]) -> bool:
    """ციფრები ტოლია; ან ტოლია სკანის მარტოხელა 0/3/6/8-ის გარეშე — ქართული ასოს ადგილას წაკითხული ციფრი
    („პ/ნ:4300…" → „პ/6:4300…", „ან" → „36"); ან ტოლია რიცხვში ასოდ წაკითხული ციფრის აღდგენით („40" → „4ი").
    ნამდვილი რიცხვი (≥2 ციფრი ზედიზედ) უცვლელი რჩება. გრძელი ნომრის (პ/ნ, ანგარიში) ერთი ციფრიც ამ წესით სხვაობაა:
    Tesseract-ის შეცდომას ხელახალი წაკითხვა ასწორებს, ანგარიშის ნამდვილი ციფრი კი არ უნდა დაიმალოს."""
    da, db = _digits(a), _digits(b)
    return (da == db or da == _digits([_LONE_CONFUSED_DIGIT.sub("", w) for w in b])
            or (bool(da) and da == _digits([w.translate(_LETTER_AS_DIGIT) for w in b])))


_SIG_EMPLOYER = re.compile(r"^დამსაქმებელ")
_SIG_EMPLOYEE = re.compile(r"^(?:დასაქმებულ|სტაჟიორ)")


def _signature_line(b: list[str]) -> bool:
    """ყოველი გვერდის ხელმოწერის სტრიქონი: „დამსაქმებელი … დასაქმებული/სტაჟიორი" და ბეჭდის/ხელმოწერის ნაგავი —
    სხვა ქართული სიტყვის გარეშე."""
    geo = [w for w in b if _GEO_WORD.search(w) and not re.search(r"[a-z]", w)]
    return (any(_SIG_EMPLOYER.match(w) for w in geo) and any(_SIG_EMPLOYEE.match(w) for w in geo)
            and all(_SIG_EMPLOYER.match(w) or _SIG_EMPLOYEE.match(w) for w in geo))


def _has_new_number(words: list[str]) -> bool:
    return any(sum(c.isdigit() for c in w) >= NEW_NUMBER_MIN_DIGITS for w in words)


def _sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return max(SequenceMatcher(None, a, b, autojunk=False).ratio(),
               SequenceMatcher(None, a, b.translate(_OCR_CONFUSION), autojunk=False).ratio())


def _words(toks: list[str]) -> int:
    """„ნამდვილი" სიტყვების რაოდენობა მონაკვეთის წესისთვის: ≥3 ასო (ხელმოწერის ზონის „ღა", „კ.", „3/6" არ ითვლება;
    რიცხვებს ციფრების წესი ფარავს)."""
    return sum(1 for w in toks if sum(c.isalpha() for c in w) >= RUN_MIN_WORD_LEN)


def _significant(a: list[str], b: list[str]) -> str | None:
    """None — ხმაური; სხვაგვარად მიზეზი (ქართულად, ოპერატორისთვის)."""
    b_real = [w for w in b if not _is_noise(w)]
    if a and len(a) == len(b_real) and all(x == y or _ocr_failed_word(x, y) or _ordinal_noise(x, y) for x, y in zip(a, b_real)):
        return None
    if not (_digits_match(a, b_real) or _digits_match(a, b)) and (_digits(a) or (_has_new_number(b_real) and not _garbage_run(b))):
        return "ციფრები განსხვავდება"
    if not a:
        return "ჩამატებული ტექსტი" if _words(b_real) >= RUN_MIN_WORDS else None
    if not b_real:
        return "ამოღებული ტექსტი" if _words(a) >= RUN_MIN_WORDS else None
    if abs(_words(a) - _words(b_real)) >= RUN_MIN_WORDS:
        return "ჩამატებული ტექსტი" if len(b_real) > len(a) else "ამოღებული ტექსტი"
    sim = _sim("".join(a), "".join(b_real))
    if len(a) == len(b_real):   # სიტყვა-სიტყვა შედარება — გრძელ მსგავს სიტყვებს შორის შეცვლილი მოკლე სიტყვა არ დაიკარგოს
        sim = min(sim, min(_sim(x, y) for x, y in zip(a, b_real)))
    return "სიტყვა შეცვლილია" if sim < SIMILARITY_MIN else None


def _placeholder_reason(a: list[str], b: list[str]) -> str | None:
    """ჯგუფი შაბლონის ცვლადით: შაბლონის ყოველი ჩვეულებრივი სიტყვა სკანში იმავე რიგით უნდა იყოს (OCR-ის
    მსგავსებით; ციფრიანი — ზუსტად იგივე ციფრებით). ცვლადის ადგილას რაც წერია, სხვაობა არ არის."""
    b_real = [w for w in b if not _is_noise(w)]
    j = 0
    for w in (x for x in a if x != PLACEHOLDER):
        while j < len(b_real) and not (_sim(w, b_real[j]) >= SIMILARITY_MIN
                                       and (not _digits([w]) or _digits([w]) == _digits([b_real[j]]))):   # „პ/ნ“ ≈ OCR „3/6“
            j += 1
        if j == len(b_real):
            return "ციფრები განსხვავდება" if _digits([w]) else "შაბლონის ტექსტი შეცვლილია"
        j += 1
    return None


_NUMERIC = re.compile(r"\d+(?:\.\d+)*")


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# პუნქტის ნომრის ციფრები, რომლებსაც OCR ქართულ ასოდ კითხულობს (რეალური სკანი: 8.4 → „ზ4", 8.6 → „ზნ", 3.4 → „ვტ")
_DIGIT_LOOKALIKE = str.maketrans({"ზ": "8", "ვ": "3", "ნ": "6"})
_LABEL_LIKE = re.compile(r"[\dზვნბტჭე.]{1,6}")


def _label_noise(a: list[str], b: list[str], labels: set[str]) -> bool:
    """ჯგუფი მხოლოდ ორიგინალის პუნქტის ნომრებისგან („2.2", „8.4") და სკანის ნომრისმაგვარი ტოკენებისგან: OCR-მა წერტილი
    დაკარგა, ციფრი გამოტოვა ან ქართულ ასოდ წაიკითხა („12", „226", „2", „ზ4") — ციფრების სხვაობა ≤ 1 → ხმაური.
    თანხა/პროცენტი პუნქტის ნომერი არ არის (labels — მხოლოდ ორიგინალის ავტო-ნომრები)."""
    if not a or not all(w in labels for w in a):
        return False
    nums = [w for w in b if _LABEL_LIKE.fullmatch(w)]
    if any(not _is_noise(w) for w in b if w not in nums) or not nums:
        return False
    return _edit_distance(_digits(a), _digits([w.translate(_DIGIT_LOOKALIKE) for w in nums])) <= 1


_LABEL_GARBAGE = re.compile(r"[\dა-ჰa-z.,\-]{1,6}")
_CLEAN_NUMBER = re.compile(r"\d+(?:\.\d+)*")


def _without_labels(a: list[str], b: list[str], labels: set[str]) -> tuple[list[str], list[str]]:
    """ორიგინალის პუნქტის ნომერი და სკანში მის ადგილას მყოფი ნაგვის ტოკენი („ვ26", „ზი", „ს", „44") წყვილად იშლება:
    ნუმერაცია შინაარსი არ არის — რჩება მხოლოდ სიტყვების შედარება („ინფორმაცია" ↔ „იწფორმაცია"). სუფთა სხვა რიცხვი
    („8.4" → „5.9") წყვილი არ არის და ჩანს."""
    a2, b2 = list(a), list(b)
    for i, lab in enumerate(a):
        if lab not in labels:
            continue
        for j in (i, i - 1, i + 1):
            if not 0 <= j < len(b) or b[j] in a or not _LABEL_GARBAGE.fullmatch(b[j]):
                continue
            if not _CLEAN_NUMBER.fullmatch(b[j]) or _edit_distance(_digits([lab]), _digits([b[j]])) <= 1:
                a2.remove(lab)
                b2.remove(b[j])
                break
    # წყვილის გარეშე დარჩენილი ნომერი: OCR-მა ნუმერაციის სვეტი გამოტოვა — ნომერი იშლება, თუ სკანში სხვა სუფთა რიცხვი
    # არ დარჩა („8.4" → „5.9" ისევ ჩანს)
    if not any(_CLEAN_NUMBER.fullmatch(w) and w not in a for w in b2):
        a2 = [w for w in a2 if w not in labels]
    return a2, b2


_GEO_WORD = re.compile(r"[ა-ჰ]{3,}")


def _found(w: str, bag: list[str], min_sim: float = SIMILARITY_MIN) -> bool:
    d = _digits([w])
    return any(_sim(w, x) >= min_sim and (not d or _digits_match([w], [x])) for x in bag)


def _table_noise(a: list[str], b: list[str], a_bag: list[str], b_page: list[str], known: list[str]) -> bool:
    """რეკვიზიტების ცხრილი (ბოლო გვერდი / ცვლადებიანი ჯგუფი): OCR ორ სვეტს სხვა რიგით კითხულობს. ხმაურია, თუ შაბლონის
    ყოველი ჩვეულებრივი სიტყვა ამავე გვერდზე სადმე არის (ციფრიანი — იმავე ციფრებით) და სკანის ყოველი ქართული სიტყვა
    შაბლონშია ან MHR-ის თანამშრომლის მონაცემია (სახელი, მისამართი…). შეცვლილი სიტყვა/რიცხვი ვერც აქ იმალება."""
    lits = [w for w in a if w != PLACEHOLDER]
    if len(lits) > 8 or any(not _found(w, b_page) for w in lits):
        return False
    geo = [w for w in b if _GEO_WORD.search(w)]
    if lits:
        unknown = [w for w in geo if not _found(w, a_bag) and not _found(w, known)]
        if not unknown:
            return True
        # 1 უცნობი მოკლე (≤4 ასო) ტოკენი — ხელმოწერის ნაკაწრი ქართულ ასოებად („ალლი"), მაგრამ მხოლოდ თუ დანარჩენი სიტყვები
        # შაბლონში/MHR-ში თითქმის ზუსტად არის: „აღარ არის პასუხისმგებელი" (ბუნდოვანი 0.6 დამთხვევები) ჩამატებაა და უნდა გამოჩნდეს
        rest = [w for w in geo if w not in unknown]
        return (len(unknown) == 1 and len(unknown[0]) <= 4
                and all(_found(w, a_bag, 0.8) or _found(w, known, 0.8) for w in rest))
    # სუფთა ჩამატება — ცვლადების მნიშვნელობები მეორე სვეტიდან: MHR-ის ≥2 მონაცემი უნდა ერიოს (სახელი, პ/ნ…), უცნობი
    # ქართული სიტყვა ≤4 (მისამართი MHR-ში სხვანაირად წერია). ჩამატებული წინადადება ასეთი არ არის — ჩანს.
    hits = sum(1 for w in b if _found(w, known, 0.8))
    unknown = [w for w in geo if not _found(w, a_bag, 0.8) and not _found(w, known, 0.8)]
    nums_known = all(_found(w, known, 0.8) for w in b if _digits([w]))
    short = len(b) <= 5 and not unknown and nums_known       # „ქ თბილისი 2026-10-18" — ადგილი და თარიღი სხვა სვეტიდან
    # ხელმოწერის ბლოკი სხვა რიგით („დირექტორი დავით გელაშვილი დამსაქმებელი" + ≤1 ნაკაწრი „ალლი"): ყველა სიტყვა შაბლონშია
    reorder = (len(b) <= 8 and geo and len(unknown) <= 1 and all(len(w) <= 4 for w in unknown)
               and all(_found(w, a_bag, 0.8) for w in b if _digits([w]) or (w in geo and w not in unknown)))
    return short or reorder or (len(b) <= 15 and hits >= 2 and len(unknown) <= 4)


LAST_PAGE_LINE_MIN = 7     # ბოლო გვერდის სტრიქონი ამდენი (ან მეტი) სიტყვით შაბლონში უნდა მოიძებნოს
LAST_PAGE_UNMATCHED = 5    # სტრიქონში ამდენი (ან მეტი) სიტყვა შაბლონს არ ემთხვევა → ჩამატებული ტექსტი
LAST_PAGE_STRAY_GAP = 4


def _confused(w: str) -> str:
    return w.translate(_OCR_CONFUSION)


def unmatched_long_lines(template: list[str], page_text: str, labels: set[str] | None = None,
                         stamp: list[str] | None = None) -> list[str]:
    """ბოლო გვერდის დაცვა: რეკვიზიტების ცხრილის გამო იქ ჯგუფები რბილად ფასდება, ამიტომ ყოველი გრძელი სტრიქონი
    (≥7 სიტყვა) შაბლონის ერთ კომპაქტურ მონაკვეთს უნდა ემთხვეოდეს (OCR-ის ჩვეული შეცდომებით). ჩამატებული
    პუნქტი/წინადადება ასეთ მონაკვეთს ვერ პოულობს. ცხრილის მოკლე სტრიქონები (ტელ, ფოსტა…) არ მოწმდება. `stamp` — ამ სკანის
    ბეჭდის სიტყვები: სტრიქონის შუაში წაკითხული ბეჭედი სტრიქონს „ჩამატებულად“ არ აქცევს."""
    t = [_confused(w) for w in template]
    in_template = set(template)
    out = []
    for line in page_text.splitlines():
        toks = [_confused(x.word) for x in _tokens(line, 0)
                if (not labels or x.word not in labels)    # ნუმერაცია არ ითვლება
                and not (stamp and x.word not in in_template and _stamp_word(x.word, stamp))]
        if len(toks) < LAST_PAGE_LINE_MIN:
            continue
        matched, span = _best_match(t, toks)
        if len(toks) - matched >= LAST_PAGE_UNMATCHED or matched < 0.5 * len(toks) or span > 2 * len(toks) + 4:
            out.append(line.strip())
    return out


def _best_match(t: list[str], toks: list[str]) -> tuple[int, int]:
    """(დამთხვეული სიტყვები, მონაკვეთის სიგრძე შაბლონში). SequenceMatcher-ის ბლოკები შეიძლება გაიფანტოს (ფრაზა შაბლონში
    ორჯერ გვხვდება — „ქ. თბილისი, ვარდების ქ." პრეამბულაშიც და რეკვიზიტებშიც): მაშინ ყველაზე გრძელი ბლოკის გარშემო
    კომპაქტურ ფანჯარაში ხელახლა ითვლება."""
    blocks = [b for b in SequenceMatcher(None, t, toks, autojunk=False).get_matching_blocks() if b.size]
    if not blocks:
        return 0, 0
    matched = sum(b.size for b in blocks)
    span = blocks[-1].a + blocks[-1].size - blocks[0].a
    if span <= 2 * len(toks) + 4:
        return matched, span
    big = max(blocks, key=lambda b: b.size)
    lo, hi = max(0, big.a - 2 * len(toks)), min(len(t), big.a + big.size + 2 * len(toks))
    inner = [b for b in SequenceMatcher(None, t[lo:hi], toks, autojunk=False).get_matching_blocks() if b.size]
    if not inner:
        return matched, span
    # კიდის ერთი სიტყვა („სტაჟიორი") შორს აღმოჩნდა, მთავარი ნაწილი კი კომპაქტურია — ის ამ სტრიქონს არ ეკუთვნის
    while len(inner) > 1 and inner[0].size == 1 and inner[1].a - inner[0].a > LAST_PAGE_STRAY_GAP:
        inner.pop(0)
    while len(inner) > 1 and inner[-1].size == 1 and inner[-1].a - (inner[-2].a + inner[-2].size) > LAST_PAGE_STRAY_GAP:
        inner.pop()
    m2 = sum(b.size for b in inner)
    s2 = inner[-1].a + inner[-1].size - inner[0].a
    return (m2, s2) if m2 >= matched - 1 else (matched, span)


def _snippet(toks: list[_Tok], i0: int, i1: int) -> str:
    lo, hi = max(0, i0 - CONTEXT_WORDS), min(len(toks), i1 + CONTEXT_WORDS)
    s = " ".join("{…}" if t.word == PLACEHOLDER else t.word for t in toks[lo:hi])
    return s if len(s) <= SNIPPET_MAX else s[:SNIPPET_MAX - 1] + "…"


def _parts(toks: list[_Tok], i0: int, i1: int) -> list[str]:
    """[სიტყვები წინ, შეცვლილი სიტყვები, სიტყვები შემდეგ] — ვებ-გვერდზე შეცვლილი ნაწილი ზუსტად მოინიშნოს."""
    def text(lo: int, hi: int) -> str:
        return " ".join("{…}" if t.word == PLACEHOLDER else t.word for t in toks[max(0, lo):hi])
    changed = text(i0, i1)
    return [text(i0 - CONTEXT_WORDS, i0), changed if len(changed) <= PARTS_MAX else changed[:PARTS_MAX - 1] + "…", text(i1, i1 + CONTEXT_WORDS)]


def _clause_before(a: list[_Tok], i: int, labels: set[str] | None) -> str:
    if labels:
        for k in range(min(i, len(a)) - 1, max(-1, i - CLAUSE_LOOKBACK), -1):
            if a[k].word in labels:
                return a[k].word
    return ""


def locate(words: list[list], scan_parts: list[str]) -> list[list[int]]:
    """დასკანერებულის ფრაგმენტი → სად არის გვერდზე: Tesseract-ის სიტყვების ჩარჩოები, სტრიქონზე თითო (გვერდის მეათასედებში).
    ტექსტი შეიძლება Claude-მა წაიკითხა, ამიტომ ადგილი კონტექსტის სიტყვებითაც იძებნება; ამოღებულ ტექსტზე მეზობელი სიტყვები მოინიშნება."""
    if not words or len(scan_parts) != 3:
        return []
    page = [(t.word, w[1:5]) for w in words for t in _tokens(str(w[0]), 0)]
    before, changed, after = (p.split() for p in scan_parts)
    target = [_confused(x) for x in [*before, *changed, *after]]
    if not page or not target:
        return []
    blocks = [b for b in SequenceMatcher(None, [_confused(p[0]) for p in page], target, autojunk=False).get_matching_blocks() if b.size]
    if not blocks:
        return []
    big = max(blocks, key=lambda b: b.size)
    start = big.a - big.b + len(before)
    lo, hi = (start, start + len(changed)) if changed else (start - 1, start + 1)
    boxes: list[list[int]] = []
    for _word, box in page[max(0, lo):max(0, hi)]:
        last = boxes[-1] if boxes else None
        if last and box[1] < last[3] and box[3] > last[1]:   # იგივე სტრიქონი
            last[0], last[1], last[2], last[3] = min(last[0], box[0]), min(last[1], box[1]), max(last[2], box[2]), max(last[3], box[3])
        else:
            boxes.append(list(box))
    return boxes


def _groups(a: list[_Tok], b: list[_Tok]) -> list[tuple[int, int, int, int]]:
    """არატოლი ოპერაციები (i1,i2,j1,j2), ≤1 ტოლი სიტყვით გაყოფილები გაერთიანებული (დასკანერებულის ერთი გვერდის
    ფარგლებში — ცვლილება სწორ გვერდზე უნდა მიეთითოს)."""
    ops = [(i1, i2, j1, j2) for tag, i1, i2, j1, j2 in
           SequenceMatcher(None, [t.word for t in a], [t.word for t in b], autojunk=False).get_opcodes()
           if tag != "equal"]

    def page_at(j: int) -> int:
        return b[min(j, len(b) - 1)].page if b else 0

    merged: list[list[int]] = []
    for op in ops:
        if merged and op[0] - merged[-1][1] <= 1 and op[2] - merged[-1][3] <= 1 \
                and page_at(op[2]) == page_at(merged[-1][2]):
            merged[-1][1], merged[-1][3] = op[1], op[3]
        else:
            merged.append(list(op))
    return [tuple(m) for m in merged]  # type: ignore[misc]


_SPLIT_MAX_PAIRS = 20000   # გვერდებად დაყოფისას სიტყვების შედარების ზღვარი (სიჩქარე)


def _split_by_page(a: list[_Tok], b: list[_Tok], g: tuple[int, int, int, int]) -> list[tuple[int, int, int, int]]:
    """ჯგუფი, რომლის დასკანერებული სიტყვები რამდენიმე გვერდზეა, იყოფა გვერდებად: ორიგინალის სიტყვა მიეკუთვნება
    პირველ (მონოტონურად) გვერდს, სადაც მსგავსი სიტყვა არის; სხვაგვარად — წინა სიტყვის გვერდს."""
    i1, i2, j1, j2 = g
    starts = [j for j in range(j1, j2) if j == j1 or b[j].page != b[j - 1].page]
    if len(starts) <= 1:
        return [g]
    bounds = [*starts, j2]
    segs = [range(bounds[k], bounds[k + 1]) for k in range(len(starts))]
    owner, k = [], 0
    compare = (i2 - i1) * (j2 - j1) <= _SPLIT_MAX_PAIRS
    for i in range(i1, i2):
        if compare:
            k = next((kk for kk in range(k, len(segs))
                      if any(_sim(a[i].word, b[j].word) >= SIMILARITY_MIN for j in segs[kk])), k)
        owner.append(k)
    out = []
    for kk, seg in enumerate(segs):
        idx = [i1 + n for n, o in enumerate(owner) if o == kk]
        lo = idx[0] if idx else (i1 + sum(1 for o in owner if o < kk))
        out.append((lo, lo + len(idx), seg.start, seg.stop))
    return out


def _negated(neg: str, plain: str) -> bool:
    return (neg.startswith(NEGATING_PREFIX) and not plain.startswith(NEGATING_PREFIX) and len(plain) >= 3
            and _sim(plain, neg[len(NEGATING_PREFIX):]) >= KEPT_SIMILARITY)


def _negation_change(a: list[str], b: list[str]) -> str | None:
    """ერთი სიტყვა, რომელიც აზრს პირიქით ცვლის: „არ", „ვერ"… გაქრა ან გაჩნდა, ან „სწორი" გახდა „არასწორი". მხოლოდ სიტყვებს
    შორის ინტერვალის სხვაობა („არ შესრულების"/„არშესრულების") ცვლილება არ არის."""
    na, nb = Counter(w for w in a if w in NEGATIONS), Counter(w for w in b if w in NEGATIONS)
    if na != nb:
        if _edit_distance(_confused("".join(a)), _confused("".join(b))) <= 1:
            return None
        lost, new = sorted((na - nb).elements()), sorted((nb - na).elements())
        return f"ამოღებული სიტყვა „{lost[0]}“" if lost else f"ჩამატებული სიტყვა „{new[0]}“"
    if any(_negated(x, y) or _negated(y, x) for x in a for y in b):
        return "უარყოფა შეცვლილია"
    return None


_ONLY_GEORGIAN = re.compile(r"[ა-ჰ]{2,}(?:[-/][ა-ჰ]+)*")   # ციფრიან და ლათინურ ტოკენებს სხვა წესები ფარავს


def _is_subsequence(short: str, long: str) -> bool:
    letters = iter(long)
    return all(c in letters for c in short)


def _far_word(x: str, y: str) -> bool:
    """მსგავსი, მაგრამ სხვა სიტყვა („დასაშვებია"/„დაუშვებელია", „სამასი"/„რვაასი"): ერთი ასოს შეცდომა არ არის, OCR-ს
    ზედმეტი ან დაკარგული ასო არ ახსნის („უფლება"/„როუფლება") და სხვაობა მხოლოდ დაბოლოებაში არ არის."""
    if not (_ONLY_GEORGIAN.fullmatch(x) and _ONLY_GEORGIAN.fullmatch(y)):
        return False
    cx, cy = _confused(x), _confused(y)
    if _edit_distance(cx, cy) < 2 or _is_subsequence(cx, cy) or _is_subsequence(cy, cx):
        return False
    return len(os.path.commonprefix([cx, cy])) < INFLECTION_PREFIX_SHARE * max(len(cx), len(cy))


@dataclass
class _WordChange:
    removed: list[str]
    added: list[str]
    far: bool

    @property
    def reason(self) -> str | None:
        return ("ამოღებული ტექსტი" if self.removed else "ჩამატებული ტექსტი" if self.added
                else "სიტყვა შეცვლილია" if self.far else None)


def _word_change(a: list[str], b: list[str], known: list[str], trusted: bool) -> _WordChange:
    """წესების ქვემოთ დარჩენილი პატარა სხვაობა ჯგუფში: რომელი ნამდვილი სიტყვა გაქრა ან გაჩნდა და არის თუ არა მსგავსი, მაგრამ
    სხვა სიტყვა. ნაგვად წაკითხული სიტყვა („ზიანის" → „bosbob"), ხელმოწერის სტრიქონის წარწერა და MHR-ის მონაცემი არ ითვლება;
    Tesseract-ის გვერდზე ასევე მოკლე ჩამატებული ტოკენი (ბეჭდის ნაგავი)."""
    rest, removed, far = list(b), [], False
    for x in a:
        best = max(rest, key=lambda y: _sim(x, y), default=None)
        if best is not None and _sim(x, best) >= SIMILARITY_MIN:
            far = far or _far_word(x, best)
            rest.remove(best)
        else:
            removed.append(x)
    removed = [x for x in removed if _ONLY_GEORGIAN.fullmatch(x) and not any(_ocr_failed_word(x, y) for y in rest)]
    added = [y for y in rest if _ONLY_GEORGIAN.fullmatch(y) and not _SIG_EMPLOYER.match(y) and not _SIG_EMPLOYEE.match(y)
             and (trusted or len(y) >= ADDED_WORD_MIN_LETTERS) and not _found(y, known, KEPT_SIMILARITY)]
    return _WordChange(removed, added, far)


def _kept(w: str, mine: list[str], other: list[str]) -> bool:
    """სიტყვა/რიცხვი გვერდიდან არ დაკარგულა: მეორე მხარეს მაინც იმდენჯერ წერია. ცხრილის სვეტების სხვა რიგით წაკითხვა
    რაოდენობას არ ცვლის, შეცვლილი რიცხვი („01" → „11") კი ცვლის, თუნდაც ძველი იმავე გვერდზე სხვაგანაც ეწეროს."""
    digits = _digits([w])
    if len(digits) >= KEPT_LONG_NUMBER:   # გრძელი ნომერი სახელს შეიძლება მიეწებოს („მარიამპ/ნ:0100…")
        def same(x: str) -> bool:
            return digits in _digits([x])
    elif digits:
        def same(x: str) -> bool:
            return bool(_digits([x])) and _sim(w, x) >= SIMILARITY_MIN and (_digits_match([w], [x]) or _digits_match([x], [w]))
    else:
        def same(x: str) -> bool:
            return _sim(w, x) >= KEPT_SIMILARITY
    return sum(map(same, other)) >= sum(map(same, mine))


def _table_counts_kept(a: list[str], b: list[str], a_last: list[str], b_last: list[str], known: list[str]) -> bool:
    """მოკლე ტოკენები არ ითვლება („ს/ნ"/„ს/კ", ნაკაწრი, „პ/ნ" → „3/6"); წარწერა „დამსაქმებელი"/„სტაჟიორი" ბეჭდურ სტრიქონშიც წერია."""
    def long_word(w: str) -> bool:
        return len(w) > ADDED_WORD_MIN_LETTERS and bool(_GEO_WORD.search(w))
    return (all(_kept(w, a_last, b_last) for w in a if w != PLACEHOLDER and (_digits([w]) or long_word(w)))
            and all(_kept(w, b_last, [*a_last, *known]) for w in b
                    if (_digits([_LONE_CONFUSED_DIGIT.sub("", w)]) or long_word(w))
                    and not _SIG_EMPLOYER.match(w) and not _SIG_EMPLOYEE.match(w)))


def _a_index_at(groups: list[tuple[int, int, int, int]], j: int) -> int:
    """სკანის j-ური სიტყვის შესაბამისი ადგილი ორიგინალში (ჯგუფებს შორის სიტყვები ტოლია)."""
    i_end, j_end = 0, 0
    for i1, i2, j1, j2 in groups:
        if j < j1:
            break
        if j < j2:
            return i1
        i_end, j_end = i2, j2
    return i_end + (j - j_end)


STAMP_NUMBER_MIN_DIGITS = 8   # ბეჭდის გრძელი ნომერი (საიდენტიფიკაციო კოდი)


def _stamp_word(w: str, stamp: list[str]) -> bool:
    """სიტყვა ამ სკანის ბეჭდის ტექსტიდანაა (`stamp` — რაც მკითხველმა სხვა გვერდებზე ბეჭდის ნიშანში ჩაწერა). ბეჭდის გრძელ ნომერში
    ერთი ციფრის სხვაობა დასაშვებია: მკრთალ ბეჭედზე მკითხველი კოდის ბოლო ციფრს გვერდიდან გვერდზე სხვადასხვანაირად კითხულობს
    (ნამდვილ სკანზე ერთი და იგივე ბეჭდის კოდი 14 გვერდზე სამნაირად წაიკითხა). მოკლე რიცხვი (წელი, თანხა) ზუსტად უნდა ემთხვეოდეს."""
    if _found(w, stamp, KEPT_SIMILARITY):
        return True
    return (w.isdigit() and len(w) >= STAMP_NUMBER_MIN_DIGITS
            and any(x.isdigit() and len(x) == len(w) and sum(c != d for c, d in zip(w, x)) <= 1 for x in stamp))


def _diff_tokens(a: list[_Tok], b: list[_Tok], default_page: int, labels: set[str] | None = None,
                 known: list[str] | None = None, trusted_pages: set[int] | None = None,
                 stamp: list[str] | None = None) -> list[TextDiff]:
    out: list[TextDiff] = []
    if [t.word for t in a] == [t.word for t in b]:
        return out
    stamp = stamp or []
    familiar = [*(known or []), *stamp]   # MHR-ის მონაცემი ან ბეჭდის სიტყვა: ჩამატებულ სიტყვად არ ითვლება
    a_bag = [t.word for t in a]
    last_page = b[-1].page if b else 0
    groups = _groups(a, b)
    last_start = next((j for j, t in enumerate(b) if t.page == last_page), 0)
    a_last, b_last = a_bag[_a_index_at(groups, last_start):], [t.word for t in b[last_start:]]
    for i1, i2, j1, j2 in (s for g in groups for s in _split_by_page(a, b, g)):
        aw, bw = [t.word for t in a[i1:i2]], [t.word for t in b[j1:j2]]
        if labels and not any(w in labels for w in aw):
            bw = [w for w in bw if w not in labels]   # სკანში სხვაგან წაკითხული ნუმერაცია („7.8 7.9 8.1" ტექსტის შუაში)
        b_real = [w for w in bw if not _is_noise(w)]   # ნაგვის ტოკენები სახეობას არ ცვლის („ამოღებული"+„1" ≠ „შეცვლილი")
        kind = "added" if not aw else "removed" if not b_real else "changed"
        if b:
            page = b[min(j1, len(b) - 1)].page
        else:
            page = default_page
        if PLACEHOLDER in aw:
            reason = _placeholder_reason(aw, bw)
        elif not aw and _signature_line(bw):
            reason = None
        elif labels and _label_noise(aw, bw, labels):
            reason = None
        elif labels and (aw or b_real) and all(w in labels for w in aw) and all(w in labels for w in b_real):
            reason = None      # ნუმერაცია სხვა ადგილას წაიკითხა (OCR სვეტს ცალკე ბლოკად იღებს) — შინაარსი არ არის
        elif labels and any(w in labels for w in aw):
            aw2, bw2 = _without_labels(aw, bw, labels)
            reason = _significant(aw2, bw2) if aw2 or [w for w in bw2 if not _is_noise(w)] else None
        else:
            reason = _significant(aw, bw)
        if reason and labels is not None and (PLACEHOLDER in aw or page == last_page) \
                and _table_noise(aw, bw, a_bag, [t.word for t in b if t.page == page], known or []) \
                and (PLACEHOLDER in a_bag or _table_counts_kept(aw, bw, a_last, b_last, known or [])):   # შაბლონში მნიშვნელობები არ წერია
            reason = None
        if reason and not aw and b_real and all(_found(w, known or [], 0.8) or _stamp_word(w, stamp) for w in b_real):
            reason = None      # სუფთა ჩამატება მხოლოდ ცნობილი ტოკენებით („2009" — ბეჭდის/კომპანიის სახელის ციფრი, ს/კ) — ბეჭედია
        suspect = False
        if reason is None and PLACEHOLDER not in aw:
            a_text = [w for w in aw if not (labels and w in labels)]
            b_text = [w for w in b_real if not (labels and w in labels)]
            reason = _negation_change(a_text, b_text)
            if reason is None and PLACEHOLDER not in a_bag:
                trusted = page in (trusted_pages or ())
                change = _word_change(a_text, b_text, familiar, trusted)
                moved = (labels is not None and page == last_page and not change.far   # ცხრილის სვეტები სხვა რიგით
                         and all(_kept(w, a_last, b_last) for w in change.removed)
                         and all(_kept(w, b_last, [*a_last, *(known or [])]) for w in change.added))
                if change.reason and not moved:
                    if trusted:
                        reason = change.reason
                    else:
                        suspect = True
        out.append(TextDiff(page=page, kind=kind, original=_snippet(a, i1, i2), scanned=_snippet(b, j1, j2),
                            significant=reason is not None, suspect=suspect,
                            reason=reason or ("საეჭვო პატარა სხვაობა" if suspect else "OCR-ის ხმაური"),
                            orig_parts=_parts(a, i1, i2), scan_parts=_parts(b, j1, j2), clause=_clause_before(a, i2, labels)))
    return out


def _norm_line(line: str) -> str:
    words = (w.strip(_PUNCT) for w in normalize(line).split())
    return " ".join(w for w in words if w)


def _same_line(a: str, b: str) -> bool:
    """ნორმალიზებული ხაზები ერთი და იგივეა OCR-ის ხმაურის გათვალისწინებით; ციფრები ზუსტად უნდა ემთხვეოდეს."""
    if a == b:
        return True
    if _digits([a]) != _digits([b]) or min(len(a), len(b)) < 0.7 * max(len(a), len(b)):
        return False
    return SequenceMatcher(None, a, b, autojunk=False).ratio() >= RUNNING_SIMILARITY


def _edges(lines: list[str]) -> list[str]:
    k = RUNNING_EDGE_LINES
    return lines if len(lines) <= 2 * k else lines[:k] + lines[-k:]


def running_lines(scanned_pages: list[str]) -> list[str]:
    """დასკანერებულის გამჭოლი (header/footer) ხაზები: ≥ RUNNING_MIN_PAGES გვერდიდან ≥ RUNNING_SHARE წილის კიდეებზე."""
    n = len(scanned_pages)
    if n < RUNNING_MIN_PAGES:
        return []
    need = max(2, math.ceil(n * RUNNING_SHARE))
    edges = [[x for x in (_norm_line(ln) for ln in _edges([ln for ln in p.split("\n") if ln.strip()]))
              if len(x) >= RUNNING_MIN_CHARS] for p in scanned_pages]
    found: list[str] = []
    for page in edges:
        for cand in page:
            if any(_same_line(cand, f) for f in found):
                continue
            if sum(1 for other in edges if any(_same_line(cand, x) for x in other)) >= need:
                found.append(cand)
    return found


def _is_page_number(norm: str, page: int) -> bool:
    m = _PAGE_NO.fullmatch(norm)
    return bool(m) and int(m.group(1)) == page


HEADER_JUNK_MAX_TOKENS = 3    # გამჭოლი სათაურის წინ ამდენამდე წვრილი ტოკენი — ლოგოს ასოებია („კამარგ ი დავით … მიერ")
HEADER_JUNK_MAX_CHARS = 10
HEADER_MIN_CHARS = 12         # ამაზე მოკლე ხაზი სათაურად არ ითვლება
HEADER_LOGO_MAX_LINES = 2     # სათაურის ზემოთ ამდენამდე ხაზი (სულ HEADER_JUNK_MAX_TOKENS სიტყვამდე) — სიტყვებად წაკითხული ლოგო


def _header_after_junk(norm: str, lines: list[str]) -> bool:
    """ბლანკის თავი ყოველ გვერდზე: ლოგო სურათია, OCR კი მის ასოებს სათაურის ხაზს აწებებს — ხაზი ისევ სათაურია."""
    toks = norm.split()
    for k in range(1, HEADER_JUNK_MAX_TOKENS + 1):
        rest = " ".join(toks[k:])
        if len("".join(toks[:k])) <= HEADER_JUNK_MAX_CHARS and len(rest) >= HEADER_MIN_CHARS \
                and any(_same_line(rest, x) for x in lines):
            return True
    return False


def _logo_rows(rows: list[tuple[str, str]], filled: list[int], headers: set[int]) -> set[int]:
    """გვერდის თავში, სათაურის პირველი ხაზის ზემოთ მდგომი ერთი-ორი მოკლე ხაზი: ბლანკის ლოგო სურათია, Claude კი მას ხანდახან
    სიტყვებად კითხულობს — ყოველ გვერდზე სხვანაირად („საოჯახო / ალფამარტი", „სახლი ალფამარჯი"), ამიტომ გამჭოლ ხაზად ვერ იპოვება.
    ციფრიანი ან უფრო გრძელი ტექსტი ლოგო არ არის და რჩება."""
    first = next((i for i in filled if i in headers), None)
    above = [i for i in filled if first is not None and i < first]
    words = [w for i in above for w in rows[i][1].split()]
    if not above or len(above) > HEADER_LOGO_MAX_LINES or len(words) > HEADER_JUNK_MAX_TOKENS \
            or any(ch.isdigit() for w in words for ch in w):
        return set()
    return set(above)


def _strip(text: str, lines: list[str], page: int | None = None, logo: bool = False) -> str:
    """ტექსტიდან ამოიღებს `lines`-ის მსგავს ხაზებს (და page-ის მითითებისას — გვერდის კიდეზე ამ გვერდის ნომერს). `logo` —
    ორიგინალის სათაურში სურათია: დასკანერებული გვერდის თავში სათაურის ზემოთ წაკითხული ლოგოს სიტყვებიც იშლება."""
    rows = [(ln, _norm_line(ln)) for ln in text.split("\n")]
    filled = [i for i, (_, norm) in enumerate(rows) if norm]
    edge = set(_edges(filled)) if page is not None else set()
    split_header = {k for i, j in zip(filled, filled[1:]) if i in edge and j in edge   # Claude სათაურს ორ ხაზად წერს („ქ. თბილისი" / „2026-10-29")
                    and any(_same_line(f"{rows[i][1]} {rows[j][1]}", x) for x in lines)
                    and not any(_same_line(rows[i][1], x) or _same_line(rows[j][1], x) for x in lines) for k in (i, j)}
    keep: dict[int, str] = {}
    headers = set(split_header)   # სათაურად ამოღებული ხაზები (გვერდის ნომერი და პუნქტს მიწებებული სათაური აქ არ ითვლება)
    for i, (ln, norm) in enumerate(rows):
        if i in split_header:
            continue
        if norm and any(_same_line(norm, x) for x in lines):
            headers.add(i)
            continue
        if norm and i in edge and _is_page_number(norm, page):
            continue
        if i in edge and len(norm) >= HEADER_MIN_CHARS and any(f" {norm} " in f" {x} " for x in lines):
            headers.add(i)
            continue   # კიდეზე სათაურის ნაწილი, მთელი სიტყვებით: Claude სათაურის დასაწყისს ხანდახან ტოვებს („01 იანვრის … დანართი")
        m = _GLUED_HEADER.match(ln)   # „10.6 ქ. თბილისი 2026-11-01": პუნქტის ნომერი + გამჭოლი სათაური ერთ ხაზად — სათაური იშლება
        if m and any(_same_line(_norm_line(m.group(2)), x) for x in lines):
            keep[i] = m.group(1)
            continue
        if norm and _header_after_junk(norm, lines):
            headers.add(i)
            continue
        keep[i] = ln
    if logo and page is not None:
        for i in _logo_rows(rows, filled, headers):
            keep.pop(i, None)
    return "\n".join(keep.values())


_GLUED_HEADER = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2})+\.?)\s+(\S.*)$")


def compare_texts(original_pages: list[str], scanned_pages: list[str], cfg, original_paginated: bool = True,
                  strip_lines: list[str] | None = None, labels: set[str] | None = None,
                  known: list[str] | None = None, trusted_pages: set[int] | None = None,
                  header_picture: bool = False, stamp: list[str] | None = None) -> list[TextDiff]:
    """ორიგინალის და დასკანერებულის ტექსტები → განსხვავებები (significant = შინაარსობრივი).

    `original_paginated=False` — ორიგინალს გვერდები არ აქვს (docx): მთელი დოკუმენტის შედარება page_count-ის
    გარეშე, გამჭოლი ხაზების და გვერდის ნომრების მოცილებით. `strip_lines` — ორივე მხრიდან ამოსაღები ხაზები
    (docx-ის header/footer). `labels` — შაბლონის პუნქტების ავტო-ნომრები (ნუმერაცია შინაარსად არ ითვლება); `known` —
    MHR-ის თანამშრომლის მონაცემების სიტყვები (რეკვიზიტების ცხრილში ცვლადების მნიშვნელობები). `trusted_pages` — დასკანერებულის
    გვერდები (1-დან), რომელთა ტექსტი Tesseract-ის არ არის (Claude, ტექსტური ფენა). `header_picture` — docx-ის header-ში სურათია
    (ბლანკის ლოგო): გვერდის თავში სათაურის ზემოთ წაკითხული ერთი-ორი სიტყვა ლოგოა, არა ჩამატებული ტექსტი. `stamp` — ამ სკანის
    ბეჭდის სიტყვები (`readermarks.stamp_words`): მხოლოდ მათგან შემდგარი ჩამატება წინადადების შუაში წაკითხული ბეჭედია. `cfg`
    ინტერფეისისთვისაა (ზღურბლები აქ მუდმივებია)."""
    strip = [x for x in (_norm_line(s) for s in strip_lines or []) if len(x) >= RUNNING_MIN_CHARS]
    if not original_paginated:
        strip += running_lines(scanned_pages)
        a = _tokens(_strip("\n".join(original_pages), strip), 0)
        b = [t for i, p in enumerate(scanned_pages) for t in _tokens(_strip(p, strip, page=i + 1, logo=header_picture), i + 1)]
        diffs = _diff_tokens(a, b, 1, labels, known, trusted_pages, stamp)
        if labels is not None and b:
            last = b[-1].page
            seen = " ".join(d.scanned for d in diffs if d.significant and d.page == last)
            for line in unmatched_long_lines([t.word for t in a], _strip(scanned_pages[last - 1], strip, page=last, logo=header_picture),
                                             labels, stamp):
                if sum(1 for w in line.split() if w in seen) < 3:
                    diffs.append(TextDiff(page=last, kind="added", original="", scanned=line, significant=True,
                                          reason="ჩამატებული ტექსტი (ბოლო გვერდი)"))
        return diffs
    if strip:
        original_pages = [_strip(p, strip) for p in original_pages]
        scanned_pages = [_strip(p, strip) for p in scanned_pages]
    diffs: list[TextDiff] = []
    if len(original_pages) != len(scanned_pages):
        diffs.append(TextDiff(page=0, kind="page_count", original=str(len(original_pages)),
                              scanned=str(len(scanned_pages)), significant=True,
                              reason=f"გვერდების რაოდენობა განსხვავდება: ორიგინალში {len(original_pages)}, "
                                     f"დასკანერებულში {len(scanned_pages)}"))
        a = [t for i, p in enumerate(original_pages) for t in _tokens(p, i + 1)]
        b = [t for i, p in enumerate(scanned_pages) for t in _tokens(p, i + 1)]
        diffs.extend(_diff_tokens(a, b, max(1, len(scanned_pages)), trusted_pages=trusted_pages))
        return diffs
    for i, (o, s) in enumerate(zip(original_pages, scanned_pages)):
        diffs.extend(_diff_tokens(_tokens(o, i + 1), _tokens(s, i + 1), i + 1, trusted_pages=trusted_pages))
    return diffs
