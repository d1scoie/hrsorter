"""პირადობის მოწმობა: ამოკითხვა, MHR-თან „ერთი ერთში“ შედარება, ავტომატური ატვირთვა და დადასტურებული სახელი სხვა
დოკუმენტებში. მონაცემები გამოგონილია."""
from __future__ import annotations

import json

from hrsorter import mhr_queue as q
from hrsorter.classifier import Classifier
from hrsorter.config import make_config
from hrsorter.idcard import parse_id_card, same_name, transliterate
from hrsorter.validation import IDENTITY_TITLE
from tests.test_packets import CONTRACT, PN, add_scan, sweep, validation, world  # noqa: F401  (world — fixture)


def id_text(last="ბერიძე", first="ნინო", pn=PN, latin_last="BERIDZE", latin_first="NINO", expiry="26.10.2036", mrz=True) -> str:
    text = (f"საქართველო GEORGIA\nპირადობის მოწმობა IDENTITY CARD\nგვარი / SURNAME\n{last}\n{latin_last}\n"
            f"სახელი / GIVEN NAME\n{first}\n{latin_first}\nსქესი / SEX\nქალი / F\nდაბადების თარიღი / DATE OF BIRTH\n01.01.1990\n"
            f"მოქმედების ვადა / DATE OF EXPIRY\n{expiry}\nპირადი N / PERSONAL NO\n{pn}\n\n"
            "გაცემის თარიღი / DATE OF ISSUE\n26.10.2026\nგამცემი ორგანო / ISSUING AUTHORITY\nიუსტიციის სამინისტრო\n")
    if mrz:
        text += f"IDGE012AB34565{pn}<<<<\n9001011F3610262GE0<<<<<<<<<<<9\n{latin_last}<<{latin_first}<<<<<<<<<<<<<<<<<<<\n"
    return text


SIGNED = {"signature_hint": "present", "signature_parties": {"employer": True, "employee": True}}


# --- ამოკითხვა ------------------------------------------------------------------------------------------------
def test_card_fields_are_read_and_checked_against_latin_and_mrz():
    card = parse_id_card(id_text())
    assert (card.last_name, card.first_name, card.personal_number) == ("ბერიძე", "ნინო", PN)
    assert (card.latin_last, card.latin_first, card.mrz_has_pn) == ("BERIDZE", "NINO", True)
    assert (card.birth_date, card.expiry_date, card.issue_date) == ("1990-01-01", "2036-10-26", "2026-10-26")
    assert card.problems == []


def test_transliteration_follows_the_card_rules():
    assert transliterate("ჭავჭავაძე") == "CHAVCHAVADZE" and transliterate("ღლონტი") == "GHLONTI"
    assert transliterate("ბერიძე") == "BERIDZE" and transliterate("წულაძე") == "TSULADZE"


def test_misread_georgian_name_is_caught_by_the_latin_line():
    card = parse_id_card(id_text(last="ბერიმე"))
    assert any("გვარი" in p and "BERIDZE" in p for p in card.problems)


def test_same_name_is_exact():
    assert same_name("ნინო ", "ნინო") and same_name("ბერიძე-კაპანაძე", "ბერიძე - კაპანაძე")
    assert not same_name("ნინა", "ნინო") and not same_name(None, "ნინო")


def test_id_card_type_is_recognised():
    assert Classifier(make_config()).classify_text(id_text()).type_id == "id_card"


# --- MHR -------------------------------------------------------------------------------------------------------
def _uploads(w):
    path = w.dir / "uploads.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def test_exact_match_is_uploaded_without_asking(world):
    add_scan(world, [(id_text(), {})], doc_id="doc_id", type_id="id_card")
    sweep(world)
    item = world.repo.mhr_get("doc_id")
    assert item["status"] == "uploaded" and item["decided_by"] == "auto:identity"
    up = _uploads(world)[0]
    assert up["category"] == "id_card" and up["title"] == IDENTITY_TITLE and up["signed_copy_of"] is None
    assert any(c["field"] == "identity" and c["status"] == "match" for c in validation(world, "doc_id")["checks"])


def test_different_name_stops_and_says_both(world):
    add_scan(world, [(id_text(first="ნინა", latin_first="NINA"), {})], doc_id="doc_id", type_id="id_card")
    sweep(world)
    v = validation(world, "doc_id")
    assert world.repo.mhr_get("doc_id")["status"] == "failed" and _uploads(world) == []
    bad = next(c for c in v["checks"] if c["field"] == "first_name")
    assert bad["status"] == "mismatch" and "„ნინა“" in bad["note"] and "„ნინო“" in bad["note"]
    assert not any(c["field"] == "identity" for c in v["checks"])


def test_doubtful_reading_is_not_uploaded_automatically(world):
    add_scan(world, [(id_text(latin_last="BERIDZA"), {})], doc_id="doc_id", type_id="id_card")
    sweep(world)
    assert world.repo.mhr_get("doc_id")["status"] == "failed" and _uploads(world) == []


def test_expired_card_waits_for_the_operator(world):
    add_scan(world, [(id_text(expiry="01.01.2020"), {})], doc_id="doc_id", type_id="id_card")
    sweep(world)
    v = validation(world, "doc_id")
    assert world.repo.mhr_get("doc_id")["status"] == "failed" and _uploads(world) == []
    assert any(c["field"] == "id_expiry" for c in v["checks"])
    assert any(c["field"] == "identity" for c in v["checks"])   # სახელი მაინც დადასტურებულია


def test_second_card_is_not_uploaded_twice(world):
    add_scan(world, [(id_text(), {})], doc_id="doc_id", type_id="id_card")
    sweep(world)
    add_scan(world, [(id_text(), {})], doc_id="doc_id2", job="job_2", type_id="id_card")
    sweep(world)
    assert world.repo.mhr_get("doc_id2")["status"] == "failed" and len(_uploads(world)) == 1


# --- დადასტურებული სახელი სხვა დოკუმენტებში ------------------------------------------------------------------------
def _contract_with_names(world, first, last, doc_id="doc_c", job="job_c"):
    add_scan(world, [(CONTRACT[0], SIGNED), (CONTRACT[1], SIGNED)], doc_id=doc_id, job=job)
    row = world.repo.get_document(doc_id)
    meta = json.loads(row["metadata"])
    meta["fields"] = {"first_name": {"value": first}, "last_name": {"value": last}}
    world.repo.conn.execute("UPDATE documents SET metadata=? WHERE id=?", (json.dumps(meta, ensure_ascii=False), doc_id))
    world.repo.conn.commit()


def test_after_verified_id_a_misread_name_does_not_stop(world):
    add_scan(world, [(id_text(), {})], doc_id="doc_id", type_id="id_card")
    sweep(world)
    _contract_with_names(world, "ნინო", "ბერიმე")
    sweep(world)
    checks = {c["field"]: c for c in validation(world, "doc_c")["checks"]}
    assert checks["last_name"]["status"] == "match" and not checks["last_name"]["blocking"]
    assert "პირადობით დადასტურებულია „ბერიძე“" in checks["last_name"]["note"]
    assert checks["identity"]["status"] == "match"


def test_after_verified_id_another_name_still_stops(world):
    add_scan(world, [(id_text(), {})], doc_id="doc_id", type_id="id_card")
    sweep(world)
    _contract_with_names(world, "ნინო", "კაპანაძე")
    sweep(world)
    last = next(c for c in validation(world, "doc_c")["checks"] if c["field"] == "last_name")
    assert last["status"] == "mismatch" and last["blocking"] and "პირადობით დადასტურებულია „ბერიძე“" in last["note"]


def test_without_verified_id_names_are_checked_as_before(world):
    _contract_with_names(world, "ნინო", "ბერიმე")
    sweep(world)
    checks = {c["field"]: c for c in validation(world, "doc_c")["checks"]}
    # პირადობის გარეშე — როგორც ადრე: „ბერიმე“ ≠ „ბერიძე“ (მსგავსება 0.83 < 0.85) და აჩერებს, დადასტურების შენიშვნის გარეშე
    assert "identity" not in checks and checks["last_name"]["status"] == "mismatch" and checks["last_name"]["blocking"]
    assert "პირადობით" not in (checks["last_name"]["note"] or "")
