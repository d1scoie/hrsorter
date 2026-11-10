"""ვინ მოაწერა ხელი — კითხვა ონლაინ მკითხველს (Claude).

ადგილობრივი ანალიზი მხარეებს სიტყვების კოორდინატებით არჩევს (`pageanalysis.page_parties`), ონლაინ მკითხველი კი მხოლოდ
ტექსტს აბრუნებს — კოორდინატების გარეშე. ამიტომ იმავე მოთხოვნაში ვთხოვთ, ტრანსკრიფციის ბოლოს ერთი სტრიქონი დაწეროს:
`[SIGNATURES employer=yes employee=no]`. სტრიქონი ტექსტიდან იჭრება (შედარებაში, ტიპის ამოცნობაში და ველებში არ ხვდება) და
`OcrResult.signature_parties`-ად ბრუნდება: {"employer": bool, "employee": bool}; `none` — ამ გვერდზე ასეთი ადგილი არ არის
(გასაღები აკლია). სტრიქონი არ დაწერა → None (მხარეები უცნობია, მუშაობს ზოგადი შეფასება).
"""
from __future__ import annotations

import re

ASK = ("After the transcription, add one final line in exactly this form: [SIGNATURES employer=X employee=Y] where X and Y "
       "are each yes, no or none. 'employer' is the place where the employer or company side signs (labels such as "
       "„დამსაქმებელი“ or „დირექტორი“, the company name, the company stamp). 'employee' is the place where the employee "
       "or intern signs (labels such as „დასაქმებული“ or „სტაჟიორი“, the person's name). yes = a handwritten signature is "
       "visibly present at that place (for the employer a stamp also counts); no = that place is on this page but nothing "
       "is signed there; none = this page has no such place. Judge only what you see in the image.")

_LINE = re.compile(r"[ \t]*\[\s*SIGNATURES\s+employer\s*=\s*(yes|no|none)\s*[,;]?\s*employee\s*=\s*(yes|no|none)\s*\][ \t]*", re.IGNORECASE)


def split(text: str) -> tuple[str, dict[str, bool] | None]:
    """(ტექსტი სტრიქონის გარეშე, მხარეები | None). რამდენიმე ასეთი სტრიქონიდან ბოლო ითვლება — ის მოდელმა დაწერა
    ტრანსკრიფციის შემდეგ; გვერდზე დაბეჭდილი იგივე ტექსტი (თუ ოდესმე შეხვდა) მასზე ადრეა."""
    found = list(_LINE.finditer(text or ""))
    if not found:
        return text, None
    answers = {"employer": found[-1].group(1).lower(), "employee": found[-1].group(2).lower()}
    return _LINE.sub("", text).rstrip(), {k: v == "yes" for k, v in answers.items() if v != "none"}
