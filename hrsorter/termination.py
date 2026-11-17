"""ხელშეკრულების შეწყვეტის შეთანხმება: რომელი ხელშეკრულება წყდება და რა თარიღიდან.

ტიპური ტექსტი: „შეწყდეს მხარეთა შორის 2030-10-12 წელს გაფორმებული სტაჟირების შესახებ ხელშეკრულება … ხელშეკრულება შეწყვეტილად
ჩაითვალოს 2030-11-14-დან“. ორივე თარიღი MHR-ის ჩანაწერს ედარება (`validation._termination_checks`). ვერ წაკითხული თარიღი — None;
არაფერი იგონება. სკანირებული ტექსტი მონაცემია; ის მხოლოდ იკითხება.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .extractor import date_tokens
from .textnorm import normalize

_ENDS = re.compile(r"შეწყვეტილად\s+ჩაითვალოს|შეწყვეტილად\s+ითვლება|შეწყვეტის\s+თარიღ\w*")
_SIGNED = re.compile(r"\s*(?:-?\s*ს)?\s*(?:წელს|წ\.?)?\s*(?:გაფორმებულ|დადებულ|დათარიღებულ)")
_STOP = re.compile(r"შეწყდეს|შეწყვეტ")
END_WINDOW = 80          # „შეწყვეტილად ჩაითვალოს“-ის შემდეგ ამდენ სიმბოლოში ვეძებთ თარიღს
SIGNED_WINDOW = 16       # თარიღსა და „გაფორმებული“-ს შორის („2030-10-12 წელს გაფორმებული“, „…-ს გაფორმებული“)


@dataclass
class Termination:
    contract_date: str | None = None   # რომელი თარიღის ხელშეკრულება წყდება („… გაფორმებული ხელშეკრულება“)
    end_date: str | None = None        # რა თარიღიდან ითვლება შეწყვეტილად


def parse_termination(text: str) -> Termination:
    t = normalize(text or "")
    toks = [d for d in date_tokens(t) if d.kind == "full" and d.iso()]
    out = Termination()
    for d in toks:   # „2030-10-12 წელს გაფორმებული“ — თარიღს მაშინვე მოსდევს „გაფორმებული“
        m = _SIGNED.match(t, d.end)
        if m and m.end() - d.end <= SIGNED_WINDOW + len("გაფორმებულ"):
            out.contract_date = d.iso()
            break
    for m in _ENDS.finditer(t):   # „შეწყვეტილად ჩაითვალოს 2030-11-14-დან“
        d = next((x for x in toks if m.end() <= x.start <= m.end() + END_WINDOW), None)
        if d is not None:
            out.end_date = d.iso()
            break
    if out.end_date is None:   # „… შეწყდეს 2030 წლის 14 ნოემბრიდან“: „-დან“ თარიღი იმავე ხაზზე, სადაც შეწყვეტაა ნახსენები
        for d in toks:
            line = t[t.rfind("\n", 0, d.start) + 1: (t.find("\n", d.end) if t.find("\n", d.end) != -1 else len(t))]
            if d.suffix.endswith("დან") and _STOP.search(line) and d.iso() != out.contract_date:
                out.end_date = d.iso()
                break
    return out
