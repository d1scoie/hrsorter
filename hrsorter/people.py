"""თანამშრომლის საბუთების სისრულე: ვის რა აქვს MHR-ში ატვირთული და რა აკლია.

შედარება თითო ფაილს ცალკე ამოწმებს; აქ ერთ თანამშრომელზე ერთად ჩანს სავალდებულო საბუთები (`mhr.required_type_ids`):
ატვირთულია · დასკანერებულია, მაგრამ ჯერ არ ატვირთულა · არ არის. სია მოიცავს თანამშრომლებს, ვისი სკანიც პროგრამას
უნახავს და MHR-ში უპოვია (MHR-ის API ყველა თანამშრომლის სიას არ იძლევა).

MHR-ის ბარათი (ფაილების სია) ფონზე მოაქვს worker-ს (`refresh`) და ბაზაში ინახავს — `employee_cache`, გასაღები
`card:<პ/ნ>` — რომ გვერდი MHR-ს არ ელოდოს. MHR-ს მხოლოდ კითხულობს (GET).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from . import hrdates, mhr_missing
from .mhr_models import MhrDocRef, MhrError, MhrStatus as S, MhrUnavailable
from .stages import STAGE_KA, file_stage

log = logging.getLogger(__name__)

CARD_PREFIX = "card:"
REFRESH_LIMIT = 10        # ერთ გავლაზე ამდენი თანამშრომლის ბარათი (თითოზე ორი GET)
UPLOADED, LOCAL, TO_SCAN, NOT_IN_MHR, UNKNOWN = "uploaded", "local", "to_scan", "not_in_mhr", "unknown"
MISSING_STATES = (LOCAL, TO_SCAN, NOT_IN_MHR)   # რაც „აკლია“-ში ითვლება (ჯერ MHR-ის ბარათზე ხელმოწერილი სახით არ არის)

# პროგრამაში მდგარი სკანი: სტატუსი → რა ეწერება სიაში. rejected/skipped/split — სკანი საბუთად არ ითვლება
_LOCAL_KA = {S.READY.value: "დასკანერებულია, მზადაა ასატვირთად", S.FAILED.value: "დასკანერებულია, შემოწმება ვერ გაიარა",
             S.PENDING.value: "დასკანერებულია, მოწმდება", S.UNAVAILABLE.value: "დასკანერებულია, მოწმდება",
             S.APPROVED.value: "იტვირთება", S.UPLOADING.value: "იტვირთება",
             S.UPLOAD_FAILED.value: "დასკანერებულია, ატვირთვა გასარკვევია", S.NEEDS_CHECK.value: "დასკანერებულია, ატვირთვა გასარკვევია"}
_LOCAL_MISSING_KA = "დასკანერებულია, MHR-ში შესადარებელი ფაილი აკლია"


def required(cfg) -> list[str]:
    """სავალდებულო საბუთების ტიპები, კონფიგურაციის რიგით; უცნობი ტიპი იგნორირდება."""
    return [t for t in cfg.section("mhr").get("required_type_ids") or [] if cfg.doc_type(t) is not None]


# --- ვინ და რა აქვს პროგრამაში ---------------------------------------------------------------------------------
def local_items(repo) -> dict[str, list[dict[str, Any]]]:
    """პ/ნ → ამ თანამშრომლის ჩანაწერები MHR-ის რიგში (მოქმედი დოკუმენტები), ახლიდან ძველისკენ."""
    out: dict[str, list[dict[str, Any]]] = {}
    for x in repo.q("SELECT m.document_id, m.status, m.personal_number, m.employee_id, m.validation, d.type_id FROM mhr_items m"
                    " JOIN documents d ON d.id=m.document_id WHERE d.active=1 AND m.personal_number IS NOT NULL"
                    " ORDER BY m.updated_at DESC"):
        v = json.loads(x["validation"] or "{}")
        emp = v.get("employee") or {}
        out.setdefault(x["personal_number"], []).append({
            "document_id": x["document_id"], "status": x["status"], "type_id": x["type_id"] or "unknown",
            "employee_id": x["employee_id"], "original_id": str((v.get("original") or {}).get("doc_id") or ""),
            "missing": bool(mhr_missing.missing_kind(v)) if x["status"] == S.FAILED.value else False,
            "name": " ".join(p for p in (emp.get("first_name"), emp.get("last_name")) if p),
            "facts": v.get("facts") or {},
            "identity_ok": any(c.get("field") == "identity" and c.get("status") == "match" for c in v.get("checks") or [])})
    return out


def identity_facts(repo, cfg, items: list[dict[str, Any]]) -> dict[str, Any]:
    """დადასტურებული პირადობის მონაცემები (`birth_date`, `id_expiry`) გაფრთხილებებისთვის; {} — პირადობა აქ არ დადასტურებულა.
    ძველი ვერსიით შემოწმებულ პირადობას ფაქტები შენახული არ აქვს — მაშინ იკითხება მისივე ტექსტიდან."""
    for i in items:
        t = cfg.doc_type(i["type_id"])
        if t is None or not t.identity or not i["identity_ok"]:
            continue
        if i["facts"]:
            return i["facts"]
        from .idcard import parse_id_card
        from .validation import document_pages
        doc = repo.get_document(i["document_id"])
        if doc is None:
            continue
        parsed = parse_id_card("\n".join(p.get("text") or "" for p in document_pages(repo, doc)))
        return {k: v for k, v in (("birth_date", parsed.birth_date), ("id_expiry", parsed.expiry_date)) if v}
    return {}


def known(items: dict[str, list[dict[str, Any]]]) -> list[str]:
    """თანამშრომლები, ვინც MHR-ში ნაპოვნია (პ/ნ-ები) — მათზე აქვს აზრი სისრულის თვლას."""
    return [pn for pn, rows in items.items() if any(r["employee_id"] for r in rows)]


# --- MHR-ის ბარათი ----------------------------------------------------------------------------------------------
def _slim(cfg, ref: MhrDocRef) -> dict[str, Any]:
    """ბარათის ერთი ფაილი, მხოლოდ ის, რაც სისრულეს სჭირდება. ტიპი და ეტაპი სახელიდან აქვე დგინდება (ერთხელ, ფონზე)."""
    from .validation import _is_generated, type_from_filename
    raw = ref.raw or {}
    name = ref.filename or ref.title
    generated = _is_generated(ref)
    return {"doc_id": ref.doc_id, "category": ref.category, "created_at": ref.created_at, "generated": generated,
            "signed_at": raw.get("signed_at"), "signed_copy_of": str(raw.get("signed_copy_of") or ""),
            "type_id": type_from_filename(cfg, name) if generated else None, "stage": file_stage(name) if generated else None}


def refresh(repo, cfg, client=None, now: float | None = None) -> int:
    """ფონური ნაბიჯი: იმ თანამშრომლების ბარათები, რომლებიც `mhr.card_refresh_minutes`-ზე ძველია (ან ჯერ არ წამოღებულა).
    MHR არ პასუხობს → გავლა ჩერდება, ძველი ბარათი რჩება. აბრუნებს, რამდენი განახლდა."""
    m = cfg.section("mhr")
    minutes = float(m.get("card_refresh_minutes") or 0)
    if not m.get("enabled") or minutes <= 0 or not required(cfg):
        return 0
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    stale = []
    for pn in known(local_items(repo)):
        row = repo.cache_get(CARD_PREFIX + pn)
        age = now - _ts(row["synced_at"]) if row else float("inf")
        if age >= minutes * 60:
            stale.append((age, pn))
    if not stale:
        return 0
    if client is None:
        from .mhr_client import build_mhr_client
        try:
            client = build_mhr_client(cfg)
        except MhrError as e:   # MHR ჯერ არ არის გამართული (მისამართი/ტოკენი) — რიგის გავლა ამას უკვე აჩვენებს
            log.info("employee card refresh skipped: %s", type(e).__name__)
            return 0
    done = 0
    for _, pn in sorted(stale, reverse=True)[:REFRESH_LIMIT]:   # ყველაზე ძველი (და ჯერ არნახული) პირველი
        try:
            emp = client.get_employee(pn)
            refs = client.list_documents(emp.employee_id) if emp else []
        except (MhrUnavailable, MhrError) as e:
            log.info("employee card refresh postponed: %s", type(e).__name__)
            break
        repo.cache_put(CARD_PREFIX + pn, {
            "employee_id": emp.employee_id if emp else None,
            "name": " ".join(p for p in (emp.first_name, emp.last_name) if p) if emp else "",
            "docs": [_slim(cfg, r) for r in refs]})
        done += 1
    return done


def card(repo, pn: str) -> dict[str, Any] | None:
    row = repo.cache_get(CARD_PREFIX + pn)
    if row is None:
        return None
    data = json.loads(row["payload"] or "{}")
    data["synced_at"] = row["synced_at"]
    return data


def _ts(iso: str | None) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return 0.0


# --- სისრულე ----------------------------------------------------------------------------------------------------
def _local_row(label: str, item: dict[str, Any]) -> dict[str, Any]:
    note = _LOCAL_MISSING_KA if item["missing"] else _LOCAL_KA[item["status"]]
    return {"label": label, "state": LOCAL, "note": note, "document_id": item["document_id"]}


def checklist(cfg, card_data: dict[str, Any] | None, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """ერთი თანამშრომლის სავალდებულო საბუთები: [{label, state, note, document_id?}].

    `card_data` — MHR-ის ბარათი (None: ჯერ არ წამოღებულა); `items` — მისი ჩანაწერები პროგრამაში (`local_items`).
    ფაილი, რომელსაც MHR აგენერირებს (ხელშეკრულება, ინსტრუქცია): ბარათის **უახლესი** ასეთი ფაილი უნდა იყოს ხელმოწერილი სახით
    ატვირთული; თუ პროგრამაში დგას სკანი, რომლის ფაილიც MHR-ში ჯერ არ არის, ისიც ცალკე სტრიქონია. სხვა საბუთი (პირადობა,
    ანკეტა): საკმარისია ერთი ატვირთული ფაილი ამ კატეგორიაში."""
    mhr = cfg.section("mhr")
    docs = (card_data or {}).get("docs") or []
    done_local = {S.UPLOADED.value}
    out: list[dict[str, Any]] = []
    for type_id in required(cfg):
        dt = cfg.doc_type(type_id)
        mine = [i for i in items if i["type_id"] == type_id]
        waiting = [i for i in mine if i["status"] in _LOCAL_KA]
        if dt.mhr_original or type_id in (mhr.get("contract_type_ids") or []):
            rows = [_local_row(dt.name_ka, i) for i in waiting if i["missing"]]      # სკანი არის, MHR-ის ფაილი — არა
            gens = sorted((d for d in docs if d.get("generated") and d.get("type_id") == type_id),
                          key=lambda d: str(d.get("created_at") or ""))
            if gens:
                cur = gens[-1]
                label = f'{STAGE_KA[cur["stage"]]} {dt.name_ka}' if cur.get("stage") in STAGE_KA else dt.name_ka
                here = [i for i in mine if i["original_id"] == cur["doc_id"]]
                if (cur.get("signed_at") or any(d.get("signed_copy_of") == cur["doc_id"] for d in docs)
                        or any(i["status"] in done_local for i in here)):
                    rows.append({"label": label, "state": UPLOADED, "note": "ატვირთულია"})
                elif any(i["status"] in _LOCAL_KA for i in here):
                    rows.append(_local_row(label, next(i for i in here if i["status"] in _LOCAL_KA)))
                elif not rows:   # ამ ტიპის სკანი უკვე დგას (სხვა ეტაპის ფაილს ელოდება) → ძველ ეტაპს ცალკე აღარ ვითხოვთ
                    rows.append({"label": label, "state": TO_SCAN, "note": "MHR-ში შექმნილია, ხელმოწერილი სკანი არ არის"})
            elif not rows:
                if card_data is None:
                    rows.append({"label": dt.name_ka, "state": UNKNOWN, "note": "MHR-ის ბარათი ჯერ არ შემოწმებულა"})
                elif waiting:
                    rows.append(_local_row(dt.name_ka, waiting[0]))
                elif any(i["status"] in done_local for i in mine):
                    rows.append({"label": dt.name_ka, "state": UPLOADED, "note": "ატვირთულია"})
                else:
                    rows.append({"label": dt.name_ka, "state": NOT_IN_MHR, "note": "MHR-ში არ არის შექმნილი"})
            out.extend(rows)
            continue
        category = (mhr.get("categories") or {}).get(type_id, type_id)
        if any(i["status"] in done_local for i in mine) or any(d.get("category") == category and not d.get("generated") for d in docs):
            out.append({"label": dt.name_ka, "state": UPLOADED, "note": "ატვირთულია"})
        elif waiting:
            out.append(_local_row(dt.name_ka, waiting[0]))
        elif card_data is None:
            out.append({"label": dt.name_ka, "state": UNKNOWN, "note": "MHR-ის ბარათი ჯერ არ შემოწმებულა"})
        else:
            out.append({"label": dt.name_ka, "state": TO_SCAN, "note": "არ არის, დაასკანერე"})
    return out


def overview(repo, cfg, today: Any = None) -> list[dict[str, Any]]:
    """გვერდისთვის: თანამშრომლები სისრულით — ჯერ ისინი, ვისაც ყველაზე მეტი აკლია.
    [{personal_number, employee_id, name, rows, missing, warnings, synced_at}]; `warnings` — თარიღების გაფრთხილებები (hrdates)."""
    items = local_items(repo)
    out = []
    for pn in known(items):
        data = card(repo, pn)
        rows = checklist(cfg, data, items[pn])
        out.append({"personal_number": pn,
                    "employee_id": (data or {}).get("employee_id") or next((i["employee_id"] for i in items[pn] if i["employee_id"]), None),
                    "name": (data or {}).get("name") or next((i["name"] for i in items[pn] if i["name"]), ""),
                    "rows": rows, "missing": sum(1 for r in rows if r["state"] in MISSING_STATES),
                    "warnings": hrdates.person_warnings(cfg, identity_facts(repo, cfg, items[pn]), today),
                    "synced_at": (data or {}).get("synced_at")})
    return sorted(out, key=lambda p: (-p["missing"], p["name"], p["personal_number"]))
