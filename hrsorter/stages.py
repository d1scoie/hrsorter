"""თანამშრომლის ეტაპი: სტაჟიორი → გამოსაცდელი ვადა → შრომითი.

MHR-ში თითო ეტაპს თავისი დაგენერირებული ფაილი აქვს და ეტაპი ფაილის სახელშია: „… (სტაჟიორი) - ხელშეკრულება“,
„ლიფტით … - სტაჟიორი“, „შინაგანაწესის დანართი - (გამოსაცდელი)“. გამოსაცდელისა და შრომითის ტექსტი ხშირად სიტყვასიტყვით
ერთნაირია, ამიტომ ქაღალდს მისივე ეტაპის ფაილი უნდა შევადაროთ — ტექსტის მსგავსება აქ არაფერს წყვეტს.

ქაღალდის ეტაპი ტექსტიდან: „სტაჟიორი“ — სტაჟიორი; „გამოსაცდელი ვადით“ — გამოსაცდელი. შრომითს ტექსტში ნიშანი არ აქვს
(„შრომითი“ ყველა ეტაპის ხელშეკრულებაში წერია), ამიტომ ის ტექსტიდან არ დგინდება.
"""
from __future__ import annotations

import re

from .textnorm import normalize

INTERN, PROBATION, LABOR = "intern", "probation", "labor"
STAGE_KA = {INTERN: "სტაჟიორის", PROBATION: "გამოსაცდელი ვადის", LABOR: "შრომითი"}

_WORDS = ((INTERN, r"სტაჟიორ\w*"), (PROBATION, r"გამოსაცდელ\w*"), (LABOR, r"შრომით\w*"))
# ფაილის სახელში ეტაპი მხოლოდ ფრჩხილებშია ან ბოლო „- …“ ნაწილად — „შრომითი ხელშეკრულება“ ეტაპი არ არის
_FILE_STAGE = [(stage, re.compile(rf"\(\s*{w}\s*\)|-\s*{w}\s*$")) for stage, w in _WORDS]
_TEXT_INTERN = re.compile(r"სტაჟიორ")                       # „სტაჟირებები“ (ტრენინგები) სხვა რამეა
_TEXT_PROBATION = re.compile(r"გამოსაცდელ\w*\s+ვად")
DOMINANCE = 3   # ორივე ნიშანი თუ წერია, ერთი მეორეზე ამდენჯერ ხშირად უნდა ჩანდეს


def file_stage(title: str | None) -> str | None:
    """MHR-ის ფაილის ეტაპი სახელიდან; None — სახელში ეტაპი არ წერია (ფაილი ყველა ეტაპს ეხება)."""
    name = (title or "").strip()
    while name.lower().endswith((".docx", ".doc", ".pdf")):
        name = name.rsplit(".", 1)[0].strip()
    for stage, rx in _FILE_STAGE:
        if rx.search(name):
            return stage
    return None


def strip_stage(title: str) -> str:
    """ფაილის სახელი ეტაპის გარეშე: „საწყობის თანამშრომელი (სტაჟიორი) - ხელშეკრულება“ → „საწყობის თანამშრომელი - ხელშეკრულება“."""
    out = title
    for _, rx in _FILE_STAGE:
        out = rx.sub("", out)
    return re.sub(r"\s{2,}", " ", out).strip(" -")


def text_stage(text: str | None) -> str | None:
    """ქაღალდის ეტაპი ტექსტიდან. None — ნიშანი არ წერია ან ორაზროვანია."""
    t = normalize(text or "")
    intern, probation = len(_TEXT_INTERN.findall(t)), len(_TEXT_PROBATION.findall(t))
    if intern and probation:
        if intern >= DOMINANCE * probation:
            return INTERN
        if probation >= DOMINANCE * intern:
            return PROBATION
        return None
    return INTERN if intern else PROBATION if probation else None
