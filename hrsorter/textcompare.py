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
       * შეცვლილი სიტყვა, რომლის სიმბოლოური მსგავსება < SIMILARITY_MIN — OCR-ის შეცდომა არ არის.
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

კონსერვატიულობა: ტყუილად „შეცვლილია" ბევრად უკეთესია, ვიდრე გამოტოვებული რეალური ცვლილება. შეზღუდვები:
1-2 სიტყვის ჩამატება („არ") ხმაურად ითვლება; ხელმოწერის ზონაში OCR-ის ≥3 „ნამდვილი" სიტყვა მნიშვნელოვანი
იქნება (ოპერატორი ნახავს). დასკანერებული ტექსტი მონაცემია — აქ არაფერი „ასრულებს" მას.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from .mhr_models import TextDiff
from .textnorm import is_georgian_char, normalize

SIMILARITY_MIN = 0.6       # შეცვლილი სიტყვის სიმბოლოური მსგავსება ამაზე დაბალი → შინაარსობრივი ცვლილება
RUN_MIN_WORDS = 3          # ამდენი (ან მეტი) სიტყვის ჩამატება/ამოღება → მნიშვნელოვანი
RUN_MIN_WORD_LEN = 3       # მონაკვეთის წესში სიტყვად ითვლება ≥3 ასოიანი ტოკენი
NEW_NUMBER_MIN_DIGITS = 3  # დასკანერებულში ახლად გაჩენილი რიცხვი ამდენი ციფრით → მნიშვნელოვანი („3/6" — არა)
SNIPPET_MAX = 160
CONTEXT_WORDS = 3
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
    ნამდვილი რიცხვი (≥2 ციფრი ზედიზედ) უცვლელი რჩება."""
    da, db = _digits(a), _digits(b)
    if len(da) >= 10 and len(db) >= 10 and len(da) == len(db) and _edit_distance(da, db) <= 1:
        return True   # 11-ნიშნა პ/ნ-ის ერთი ციფრის OCR-შეცდომა — პ/ნ ცალკე, MHR-თან მოწმდება
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
        return all(_found(w, a_bag) or _found(w, known) for w in geo)
    # სუფთა ჩამატება — ცვლადების მნიშვნელობები მეორე სვეტიდან: MHR-ის ≥2 მონაცემი უნდა ერიოს (სახელი, პ/ნ…), უცნობი
    # ქართული სიტყვა ≤4 (მისამართი MHR-ში სხვანაირად წერია). ჩამატებული წინადადება ასეთი არ არის — ჩანს.
    hits = sum(1 for w in b if _found(w, known, 0.8))
    unknown = [w for w in geo if not _found(w, a_bag, 0.8) and not _found(w, known, 0.8)]
    nums_known = all(_found(w, known, 0.8) for w in b if _digits([w]))
    short = len(b) <= 5 and not unknown and nums_known       # „ქ თბილისი 2026-10-18" — ადგილი და თარიღი სხვა სვეტიდან
    return short or (len(b) <= 15 and hits >= 2 and len(unknown) <= 4)


LAST_PAGE_LINE_MIN = 7     # ბოლო გვერდის სტრიქონი ამდენი (ან მეტი) სიტყვით შაბლონში უნდა მოიძებნოს
LAST_PAGE_UNMATCHED = 5    # სტრიქონში ამდენი (ან მეტი) სიტყვა შაბლონს არ ემთხვევა → ჩამატებული ტექსტი


def _confused(w: str) -> str:
    return w.translate(_OCR_CONFUSION)


def unmatched_long_lines(template: list[str], page_text: str, labels: set[str] | None = None) -> list[str]:
    """ბოლო გვერდის დაცვა: რეკვიზიტების ცხრილის გამო იქ ჯგუფები რბილად ფასდება, ამიტომ ყოველი გრძელი სტრიქონი
    (≥7 სიტყვა) შაბლონის ერთ კომპაქტურ მონაკვეთს უნდა ემთხვეოდეს (OCR-ის ჩვეული შეცდომებით). ჩამატებული
    პუნქტი/წინადადება ასეთ მონაკვეთს ვერ პოულობს. ცხრილის მოკლე სტრიქონები (ტელ, ფოსტა…) არ მოწმდება."""
    t = [_confused(w) for w in template]
    out = []
    for line in page_text.splitlines():
        toks = [_confused(x.word) for x in _tokens(line, 0) if not labels or x.word not in labels]   # ნუმერაცია არ ითვლება
        if len(toks) < LAST_PAGE_LINE_MIN:
            continue
        blocks = [b for b in SequenceMatcher(None, t, toks, autojunk=False).get_matching_blocks() if b.size]
        matched = sum(b.size for b in blocks)
        span = (blocks[-1].a + blocks[-1].size - blocks[0].a) if blocks else 0
        if len(toks) - matched >= LAST_PAGE_UNMATCHED or matched < 0.5 * len(toks) or span > 2 * len(toks) + 4:
            out.append(line.strip())
    return out


def _snippet(toks: list[_Tok], i0: int, i1: int) -> str:
    lo, hi = max(0, i0 - CONTEXT_WORDS), min(len(toks), i1 + CONTEXT_WORDS)
    s = " ".join("{…}" if t.word == PLACEHOLDER else t.word for t in toks[lo:hi])
    return s if len(s) <= SNIPPET_MAX else s[:SNIPPET_MAX - 1] + "…"


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


def _diff_tokens(a: list[_Tok], b: list[_Tok], default_page: int, labels: set[str] | None = None,
                 known: list[str] | None = None) -> list[TextDiff]:
    out: list[TextDiff] = []
    if [t.word for t in a] == [t.word for t in b]:
        return out
    a_bag = [t.word for t in a]
    last_page = b[-1].page if b else 0
    for i1, i2, j1, j2 in (s for g in _groups(a, b) for s in _split_by_page(a, b, g)):
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
                and _table_noise(aw, bw, a_bag, [t.word for t in b if t.page == page], known or []):
            reason = None
        out.append(TextDiff(page=page, kind=kind, original=_snippet(a, i1, i2), scanned=_snippet(b, j1, j2),
                            significant=reason is not None, reason=reason or "OCR-ის ხმაური"))
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


def _strip(text: str, lines: list[str], page: int | None = None) -> str:
    """ტექსტიდან ამოიღებს `lines`-ის მსგავს ხაზებს (და page-ის მითითებისას — გვერდის კიდეზე ამ გვერდის ნომერს)."""
    rows = [(ln, _norm_line(ln)) for ln in text.split("\n")]
    filled = [i for i, (_, norm) in enumerate(rows) if norm]
    edge = set(_edges(filled)) if page is not None else set()
    keep = []
    for i, (ln, norm) in enumerate(rows):
        if norm and (any(_same_line(norm, x) for x in lines) or (i in edge and _is_page_number(norm, page))):
            continue
        keep.append(ln)
    return "\n".join(keep)


def compare_texts(original_pages: list[str], scanned_pages: list[str], cfg, original_paginated: bool = True,
                  strip_lines: list[str] | None = None, labels: set[str] | None = None,
                  known: list[str] | None = None) -> list[TextDiff]:
    """ორიგინალის და დასკანერებულის ტექსტები → განსხვავებები (significant = შინაარსობრივი).

    `original_paginated=False` — ორიგინალს გვერდები არ აქვს (docx): მთელი დოკუმენტის შედარება page_count-ის
    გარეშე, გამჭოლი ხაზების და გვერდის ნომრების მოცილებით. `strip_lines` — ორივე მხრიდან ამოსაღები ხაზები
    (docx-ის header/footer). `labels` — შაბლონის პუნქტების ავტო-ნომრები (ნუმერაცია შინაარსად არ ითვლება); `known` —
    MHR-ის თანამშრომლის მონაცემების სიტყვები (რეკვიზიტების ცხრილში ცვლადების მნიშვნელობები). `cfg` ინტერფეისისთვისაა
    (ზღურბლები აქ მუდმივებია)."""
    strip = [x for x in (_norm_line(s) for s in strip_lines or []) if len(x) >= RUNNING_MIN_CHARS]
    if not original_paginated:
        strip += running_lines(scanned_pages)
        a = _tokens(_strip("\n".join(original_pages), strip), 0)
        b = [t for i, p in enumerate(scanned_pages) for t in _tokens(_strip(p, strip, page=i + 1), i + 1)]
        diffs = _diff_tokens(a, b, 1, labels, known)
        if labels is not None and b:
            last = b[-1].page
            seen = " ".join(d.scanned for d in diffs if d.significant and d.page == last)
            for line in unmatched_long_lines([t.word for t in a], _strip(scanned_pages[last - 1], strip, page=last), labels):
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
        diffs.extend(_diff_tokens(a, b, max(1, len(scanned_pages))))
        return diffs
    for i, (o, s) in enumerate(zip(original_pages, scanned_pages)):
        diffs.extend(_diff_tokens(_tokens(o, i + 1), _tokens(s, i + 1), i + 1))
    return diffs
