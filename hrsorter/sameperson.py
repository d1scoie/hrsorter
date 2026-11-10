"""პირადი ნომრის გარეშე დასკანერებული დოკუმენტი (დანართი, წესები): ნომერი იმავე თანამშრომლის სხვა სკანიდან.

დანართზე ხშირად მხოლოდ „დასაქმებული: სახელი გვარი“ წერია, ამიტომ MHR-თან შედარება ვერ იწყებოდა და დოკუმენტი
„ტიპი ვერ ამოიცნო“-თი რჩებოდა, თუმცა ორიგინალი ბარათზეა. თუ იმავე სახელ-გვარის თანამშრომელი MHR-ში უკვე დადასტურდა
სხვა სკანით (ხელშეკრულება, ინსტრუქცია), მისი ნომერი აქაც იწერება და დოკუმენტი ჩვეულებრივ ვალიდაციაზე გადის: ტექსტი
ბარათის ფაილს ედარება, ტიპი იქიდან დგება, ატვირთვას კი ისევ ოპერატორი ადასტურებს.

ნომერი მხოლოდ მაშინ აიღება, როცა სახელ-გვარს ზუსტად ერთი პირადი ნომერი შეესაბამება; ორი მოგვარე — ხელით.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .mhr_models import MhrStatus as S
from .output import MATCH_RESOLVED_REASONS, _update_document, _write_json_atomic
from .textnorm import digits_only

log = logging.getLogger(__name__)

EVIDENCE = "პირადი ნომერი დოკუმენტში არ წერია — აღებულია იმავე თანამშრომლის სხვა სკანიდან (MHR-ში დადასტურებული)"
MISSING_FLAG = "personal_number_missing"


def _pn(value: Any) -> str | None:
    d = digits_only(str(value)) if value else ""
    return d if len(d) == 11 else None


def known_people(repo) -> list[SimpleNamespace]:
    """MHR-ში უკვე ნაპოვნი თანამშრომლები: აქტიური დოკუმენტი, რომლის პ/ნ-ით MHR-მა იგივე პ/ნ-ის ჩანაწერი დააბრუნა."""
    seen: dict[str, SimpleNamespace] = {}
    for r in repo.q("SELECT m.personal_number, m.validation FROM mhr_items m JOIN documents d ON d.id=m.document_id"
                    " WHERE d.active=1 AND m.employee_id IS NOT NULL AND m.personal_number IS NOT NULL"
                    " AND m.validation IS NOT NULL"):
        try:
            emp = (json.loads(r["validation"]) or {}).get("employee") or {}
        except ValueError:
            continue
        pn = _pn(emp.get("personal_number"))
        if pn and pn == _pn(r["personal_number"]) and emp.get("first_name") and emp.get("last_name"):
            seen.setdefault(pn, SimpleNamespace(personal_number=pn, first_name=emp["first_name"], last_name=emp["last_name"]))
    return list(seen.values())


def find_personal_number(people: list[SimpleNamespace], first: str | None, last: str | None, threshold: float) -> str | None:
    """სახელ-გვარის ერთადერთი მფლობელის პ/ნ; არავინ ან რამდენიმე — None."""
    if not first or not last:
        return None
    from .validation import employee_name_matches   # მძიმე მოდული — მხოლოდ საჭიროებისას
    pns = {p.personal_number for p in people if employee_name_matches(first, last, p, threshold)}
    return pns.pop() if len(pns) == 1 else None


def _candidates(repo) -> list:
    """პ/ნ-ის გარეშე დარჩენილი აქტიური დოკუმენტები, რომლებიც MHR-ს ჯერ არ შედარებია (რიგში არ არის ან „გამოტოვებულია“)."""
    return repo.q("SELECT d.*, m.status AS mhr_status FROM documents d LEFT JOIN mhr_items m ON m.document_id=d.id"
                  " WHERE d.active=1 AND d.personal_number IS NULL AND d.purged_at IS NULL AND d.output_path IS NOT NULL"
                  " AND d.status IN ('needs_review','proposed')"
                  " AND (m.document_id IS NULL OR (m.status=? AND m.personal_number IS NULL AND m.employee_id IS NULL))",
                  (S.SKIPPED.value,))


def sweep(repo, cfg) -> int:
    """ერთი გავლა (MHR-ის გავლის წინ). აბრუნებს, რამდენ დოკუმენტს ჩაეწერა ნომერი და გადავიდა შედარებაზე."""
    m = cfg.section("mhr")
    if not m.get("enabled"):
        return 0
    rows = _candidates(repo)
    if not rows:
        return 0
    people = known_people(repo)
    if not people:
        return 0
    threshold = float(m.get("name_similarity", 0.85))
    done = 0
    for row in rows:
        meta: dict[str, Any] = json.loads(row["metadata"]) if row["metadata"] else {}
        fields = meta.get("fields") or {}
        if _pn(meta.get("personal_number")) or _pn((fields.get("personal_number") or {}).get("value")):
            continue   # ნომერი იკითხება — ჩვეულებრივი გზა
        reasons = set(json.loads(row["review_reasons"] or "[]"))
        if row["status"] == "needs_review" and not reasons <= MATCH_RESOLVED_REASONS:
            continue   # სხვა მიზეზით გადასამოწმებელი (დუბლიკატი, კომპანიის კონფლიქტი) — ოპერატორს რჩება
        pn = find_personal_number(people, (fields.get("first_name") or {}).get("value"),
                                  (fields.get("last_name") or {}).get("value"), threshold)
        if pn is None:
            continue
        _apply(repo, row, meta, pn)
        done += 1
    return done


def _apply(repo, row, meta: dict[str, Any], pn: str) -> None:
    doc_id = row["id"]
    meta.setdefault("fields", {})["personal_number"] = {
        "value": pn, "raw": None, "source": "derived", "page": None, "confidence": 0.5, "evidence": EVIDENCE,
        "handwritten": False, "candidates": []}
    if "flags" in meta:
        meta["flags"] = [f for f in meta["flags"] if f != MISSING_FLAG]
    _update_document(repo, doc_id, {"personal_number": pn, "metadata": meta})
    if row["sidecar_path"] and Path(row["sidecar_path"]).exists():
        try:
            _write_json_atomic(Path(row["sidecar_path"]), meta)
        except OSError as e:   # sidecar ასლია — ბაზა უკვე სწორია
            log.warning("sidecar of %s not updated: %s", doc_id, type(e).__name__)
    if not repo.mhr_enqueue(doc_id, pn):
        repo.mhr_transition(doc_id, (S.SKIPPED.value,), S.PENDING.value, personal_number=pn, validation=None,
                            attempts=0, next_try=None, error=None, ai_review=None)
    repo.log_event("mhr", f"{doc_id}: personal number taken from the same employee's other scan")
