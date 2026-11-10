"""„MHR-ში აკლია“: სკანი „ვერ გაიარა“-შია, თუმცა სკანში გასასწორებელი არაფერია — MHR-ში ჯერ არ არის ის, რასაც უნდა
შეედაროს (დაგენერირებული ფაილი, თვითონ თანამშრომელი ან ბარათის მონაცემი).

აქ მხოლოდ ამოცნობა და სია დგება: რომელი ჩანაწერია ასეთი და ვის რა უნდა გაუჩნდეს MHR-ში, თანამშრომლების მიხედვით.
თავისით გადამოწმება (MHR-ში გაჩნდა? → შედარება თავიდან) — `mhr_queue._recheck_missing`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

EMPLOYEE, ORIGINAL, DATA = "employee", "original", "data"   # რა აკლია: თანამშრომელი | დაგენერირებული ფაილი | ბარათის მონაცემი
SQL_LIKE = '%"missing_in_mhr"%'                              # სწრაფი წინასწარი ფილტრი ბაზაში; ზუსტი პასუხი — `missing_kind`
NO_EMPLOYEE = "თანამშრომელი ამ პირადი ნომრით MHR-ში არ არის"


def _blocking_missing(v: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in v.get("checks") or [] if c.get("status") == "missing_in_mhr" and c.get("blocking")]


def missing_kind(v: dict[str, Any] | None) -> str | None:
    """რა აკლია MHR-ში ამ შედარების მიხედვით; None — არაფერი (ჩვეულებრივი „ვერ გაიარა“ ან შედარება საერთოდ არ მომხდარა)."""
    if not v or v.get("error"):
        return None
    missing = _blocking_missing(v)
    if not missing:
        return None
    if not v.get("employee"):
        return EMPLOYEE
    return ORIGINAL if any(c.get("field") == "original_contract" for c in missing) else DATA


def what_is_missing(v: dict[str, Any]) -> str:
    """ერთი მოკლე ხაზი სიისთვის: რა უნდა გაჩნდეს MHR-ში. შენიშვნის პირველი ნაწილი — „რა ვქნა“ სიის თავში ერთხელ წერია."""
    kind = missing_kind(v)
    if kind is None:
        return ""
    if kind == EMPLOYEE:
        return NO_EMPLOYEE
    missing = _blocking_missing(v)
    c = next((x for x in missing if x.get("field") == "original_contract"), missing[0])
    note = str(c.get("note") or "").strip()
    for stop in (". ", " — ჯერ "):
        cut = note.find(stop)
        if cut > 0:
            note = note[:cut]
    return note or f'{c.get("label_ka") or c.get("field")}: MHR-ში არ არის'


def groups(rows, cfg) -> list[dict[str, Any]]:
    """`rows`: mhr_items (+ დოკუმენტის type_id, page_start, page_end, სკანის first_path). აბრუნებს თანამშრომლების სიას —
    თითოზე, რა ფაილები დგას და რა აკლია MHR-ში. სხვა ჩანაწერებს (ჩვეულებრივი „ვერ გაიარა“) ტოვებს."""
    out: dict[str, dict[str, Any]] = {}
    for x in rows:
        v = json.loads(x["validation"] or "{}")
        kind = missing_kind(v)
        if kind is None:
            continue
        emp = v.get("employee") or {}
        key = str(emp.get("employee_id") or x["personal_number"] or x["document_id"])
        g = out.setdefault(key, {"name": " ".join(p for p in (emp.get("first_name"), emp.get("last_name")) if p),
                                 "personal_number": x["personal_number"], "employee_id": emp.get("employee_id"), "items": []})
        t = cfg.doc_type(x["type_id"] or "")
        g["items"].append({"document_id": x["document_id"], "kind": kind, "what": what_is_missing(v),
                           "type": t.name_ka if t else "დოკუმენტი",
                           "scan": (f'{Path(x["first_path"]).name} · გვ. {x["page_start"] + 1}–{x["page_end"] + 1}'
                                    if x["first_path"] and x["page_start"] is not None else "")})
    return sorted(out.values(), key=lambda g: (not g["name"], g["name"], str(g["personal_number"] or "")))


def count(rows) -> tuple[int, int]:
    """(რამდენი ფაილი, რამდენი თანამშრომელი) — მთავარი გვერდისა და ჩანართის მთვლელისთვის. `rows`: personal_number, validation."""
    files, people = 0, set()
    for x in rows:
        v = json.loads(x["validation"] or "{}")
        if missing_kind(v):
            files += 1
            people.add(str((v.get("employee") or {}).get("employee_id") or x["personal_number"] or files))
    return files, len(people)
