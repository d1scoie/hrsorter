"""პირადობის მოწმობა: ამოკითხვა, MHR-თან „ერთი ერთში“ შედარება, ავტომატური ატვირთვა და დადასტურებული სახელი სხვა
დოკუმენტებში. მონაცემები გამოგონილია."""
from __future__ import annotations

import json

from hrsorter import hrdates
from hrsorter import mhr_queue as q
from hrsorter.classifier import Classifier
from hrsorter.config import make_config
from hrsorter.features import page_features
from hrsorter.idcard import ID_CARD, PASSPORT, document_kind, parse_id_card, same_name, transliterate
from hrsorter.models import PageText
from hrsorter.validation import IDENTITY_TITLE, PASSPORT_NOTE, PASSPORT_TITLE, settle_confirmed_identities
from tests.test_packets import CONTRACT, PN, add_scan, sweep, validation, world  # noqa: F401  (world — fixture)


def id_text(last="ბერიძე", first="ნინო", pn=PN, latin_last="BERIDZE", latin_first="NINO", expiry="26.10.2036", mrz=True) -> str:
    text = (f"საქართველო GEORGIA\nპირადობის მოწმობა IDENTITY CARD\nგვარი / SURNAME\n{last}\n{latin_last}\n"
            f"სახელი / GIVEN NAME\n{first}\n{latin_first}\nსქესი / SEX\nქალი / F\nდაბადების თარიღი / DATE OF BIRTH\n01.01.1990\n"
            f"მოქმედების ვადა / DATE OF EXPIRY\n{expiry}\nპირადი N / PERSONAL NO\n{pn}\n\n"
            "გაცემის თარიღი / DATE OF ISSUE\n26.10.2026\nგამცემი ორგანო / ISSUING AUTHORITY\nიუსტიციის სამინისტრო\n")
    if mrz:
        text += f"IDGE012AB34565{pn}<<<<\n9001011F3610262GE0<<<<<<<<<<<9\n{latin_last}<<{latin_first}<<<<<<<<<<<<<<<<<<<\n"
    return text


def new_card_text(pn=PN) -> str:
    """ახალი ნიმუშის ბარათი, ორივე მხარე ერთ გვერდზე: ქართული წარწერები წვრილია და მკითხველი მათ ნაცვლად „[?]“-ს წერს,
    ამიტომ სათაური „პირადობის მოწმობა“ ტექსტში არ არის."""
    return ("GEORGIA\nIDENTITY CARD\n[?] (საქართველო)\n[?] / SURNAME\nბერიძე\nBERIDZE\n[?] / GIVEN NAME\nნინო\nNINO\n[?] / SEX\nქ / F\n"
            f"[?] / CIT\nGEO\n[?] / DATE OF BIRTH\n01.01.1990\n[?] / PERSONAL NO\n{pn}\n[?] / DATE OF EXPIRY\n26.10.2036\nCAN\n000000\n"
            "[ხელნაწერი: ხელმოწერა [?]]\n\n[?] / PLACE OF BIRTH\nთბილისი / TBILISI\n[?] / DATE OF ISSUE\n26.10.2026\n"
            f"[?] / ISSUING AUTHORITY\nMINISTRY OF JUSTICE\nIDGEO0000000002{pn}<<<<\n9001011F3610262GEO<<<<<<<<<<8\n"
            "BERIDZE<<NINO<<<<<<<<<<<<<<<\n")


def passport_text(last="ბერიძე", first="ნინო", pn=PN, latin_last="BERIDZE", latin_first="NINO", expiry="15 სექ / SEP 2031",
                  mrz=True) -> str:
    """პასპორტის მონაცემების გვერდი, როგორც მკითხველი წერს: მნიშვნელობა ერთ ხაზზე („ბერიძე / BERIDZE“), თარიღი თვის სახელით,
    ბოლოს კი გვერდის ნაბეჭდი ნომერი („03“)."""
    text = ("საქართველო\nGeorgia\nპასპორტი PASSPORT\nტიპი / TYPE P\nსახელმწიფოს კოდი / CODE OF STATE GEO\n"
            f"პასპორტის № / PASSPORT 00AA00000\nგვარი / SURNAME\n{last} / {latin_last}\nსახელი / GIVEN NAME\n{first} / {latin_first}\n"
            "მოქალაქეობა / CITIZENSHIP\nსაქართველო / GEORGIA\nდაბადების თარიღი / DATE OF BIRTH\n01 იან / JAN 1990\nსქესი / SEX\nმდ / F\n"
            f"პირადი ნომერი / PERSONAL NUMBER\n{pn}\nდაბადების ადგილი / PLACE OF BIRTH\nთბილისი / TBILISI\n"
            "გაცემის თარიღი / DATE OF ISSUE\n15 სექ / SEP 2021\nგამცემი ორგანო / ISSUING AUTHORITY\nიუსტიციის სამინისტრო\n"
            f"MINISTRY OF JUSTICE\nმოქმედების ვადა / EXPIRY DATE\n{expiry}\nმფლობელის ხელმოწერა / OWNER'S SIGNATURE\n"
            "[ხელნაწერი: ხელმოწერა]\n")
    if mrz:
        text += (f"P<GEO{latin_last.replace('-', '<')}<<{latin_first}<<<<<<<<<<<<<<<<<<<<<<<<<<<<\n"   # ზოლში ტირე „<“-ად იწერება
                 "00AA000004GEO9001011F3109155<<<<<<<<<<<<<<02\n")
    return text + "შენიშვნები/REMARKS\n03\n"


def page(text: str, i: int = 0) -> PageText:
    return PageText(i, text, "ocr:online", 0.8, False, True, False, 0.05)


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


def test_letter_q_is_written_as_k_on_real_cards():
    """ნამდვილი ბარათი: გვარში „ყ“ ლათინურად „K“-თი წერია, ეროვნული სისტემით კი „Q“ — ორივე სწორი წაკითხვაა."""
    for latin in ("KIPIANI", "QIPIANI"):
        assert parse_id_card(id_text(last="ყიფიანი", latin_last=latin)).problems == []
    assert parse_id_card(id_text(last="ბაყაყაძე", latin_last="BAKAKADZE")).problems == []
    assert any("გვარი" in p for p in parse_id_card(id_text(last="ყიფიანი", latin_last="KIPSHIDZE")).problems)   # სხვა გვარი ისევ ჩანს
    assert any("გვარი" in p for p in parse_id_card(id_text(last="ყიფიანი", latin_last="GIPIANI")).problems)


def test_same_name_is_exact():
    assert same_name("ნინო ", "ნინო") and same_name("ბერიძე-კაპანაძე", "ბერიძე - კაპანაძე")
    assert not same_name("ნინა", "ნინო") and not same_name(None, "ნინო")


def test_id_card_type_is_recognised():
    assert Classifier(make_config()).classify_text(id_text()).type_id == "id_card"


# --- პასპორტი და ახალი ნიმუშის ბარათი ---------------------------------------------------------------------------
def test_passport_fields_are_read_like_the_card():
    card = parse_id_card(passport_text())
    assert card.kind == PASSPORT and (card.last_name, card.first_name, card.personal_number) == ("ბერიძე", "ნინო", PN)
    assert (card.latin_last, card.latin_first) == ("BERIDZE", "NINO")
    assert (card.birth_date, card.issue_date, card.expiry_date) == ("1990-01-01", "2021-09-15", "2031-09-15")
    assert card.problems == []


def test_passport_surname_with_a_label_word_inside_is_still_a_name():
    """„დათეშიძე / DATESHIDZE“ ლეიბლი არ არის, თუმცა შიგნით „date“ წერია; ორმაგი გვარიც ერთ ხაზზე იკითხება."""
    assert parse_id_card(passport_text(last="დათეშიძე", latin_last="DATESHIDZE")).last_name == "დათეშიძე"
    double = parse_id_card(passport_text(last="ბერიძე-კაპანაძე", latin_last="BERIDZE-KAPANADZE"))
    assert double.last_name == "ბერიძე-კაპანაძე" and double.latin_last == "BERIDZE KAPANADZE"


def test_misread_passport_name_is_caught_by_its_machine_line():
    text = passport_text().replace("ბერიძე / BERIDZE", "ბერიმე / BERIDZE")
    assert any("გვარი" in p and "BERIDZE" in p for p in parse_id_card(text).problems)


def test_kind_comes_from_the_machine_readable_line_not_from_the_title():
    assert document_kind(passport_text()) == PASSPORT and document_kind(new_card_text()) == ID_CARD
    assert document_kind(id_text()) == ID_CARD and parse_id_card(id_text()).kind == ID_CARD
    assert document_kind(passport_text(mrz=False)) == PASSPORT          # ზოლი ვერ წაიკითხა — სათაურით და ველებით
    assert document_kind(id_text(mrz=False)) is None and document_kind(CONTRACT[0]) is None and document_kind("") is None


def test_passport_and_new_card_are_identity_documents_without_a_title():
    c = Classifier(make_config())
    for text in (passport_text(), new_card_text()):
        got = c.classify_pages([page(text)])
        assert got.type_id == "id_card" and got.confidence >= 0.8 and got.margin == 1.0
        assert any("identity document" in e for e in got.evidence)
    without = c.classify_pages([page(new_card_text().split("IDGEO")[0])])   # ზოლის გარეშე — წესებით, როგორც ადრე
    assert without.type_id == "id_card" and without.confidence < 0.8


def test_id_copy_inside_a_longer_scan_does_not_rename_the_scan():
    c = Classifier(make_config())
    long_scan = [page(passport_text(), 0), page("თავისუფალი ტექსტი პირველ ფურცელზე", 1), page("თავისუფალი ტექსტი მეორეზე", 2)]
    assert c.classify_pages(long_scan).type_id == "unknown"
    assert c.classify_pages([page(CONTRACT[0], 0), page(passport_text(), 1)]).type_id != "id_card"


def test_number_printed_on_a_passport_page_is_not_a_scan_page_number():
    cfg = make_config()
    c = Classifier(cfg)
    assert page_features(page(passport_text()), c, cfg).page_number is None
    assert page_features(page("ეს ტექსტი წინა გვერდიდან გრძელდება და აქ მთავრდება\n03"), c, cfg).page_number == 3


def test_expiry_warning_names_the_passport():
    assert hrdates.id_expiry("2020-01-01", "2026-11-13", passport=True)[1].startswith("პასპორტს ვადა გაუვიდა")
    assert hrdates.id_expiry("2020-01-01", "2026-11-13")[1].startswith("პირადობის მოწმობას ვადა გაუვიდა")
    assert hrdates.person_warnings(make_config(), {"id_expiry": "2020-01-01", "id_kind": "passport"}, "2026-11-13")[0].startswith("პასპორტს")


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


def test_passport_is_checked_and_uploaded_like_the_card(world):
    add_scan(world, [(passport_text(), {})], doc_id="doc_id", type_id="id_card")
    sweep(world)
    item = world.repo.mhr_get("doc_id")
    assert item["status"] == "uploaded" and item["decided_by"] == "auto:identity"
    up = _uploads(world)[0]
    assert up["category"] == "id_card" and up["title"] == PASSPORT_TITLE and IDENTITY_TITLE in up["title"]
    v = validation(world, "doc_id")
    assert v["summary"][0] == PASSPORT_NOTE and v["facts"]["id_kind"] == "passport"


def test_passport_with_another_name_stops_and_says_passport(world):
    add_scan(world, [(passport_text(first="ნინა", latin_first="NINA"), {})], doc_id="doc_id", type_id="id_card")
    sweep(world)
    bad = next(c for c in validation(world, "doc_id")["checks"] if c["field"] == "first_name")
    assert world.repo.mhr_get("doc_id")["status"] == "failed" and _uploads(world) == []
    assert bad["note"].startswith("პასპორტზე „ნინა“") and "„ნინო“" in bad["note"]


# --- „გადასამოწმებელი“ სია --------------------------------------------------------------------------------------
def test_confirmed_identity_leaves_the_review_list(world):
    """ახალი ბარათი: ტიპი „სავარაუდო“ იყო (სათაური არ იკითხება), მაგრამ სახელი, გვარი და პ/ნ MHR-ს ზუსტად დაემთხვა —
    გადასამოწმებელი აღარაფერია."""
    add_scan(world, [(new_card_text(), {})], doc_id="doc_id", type_id="id_card", status="needs_review",
             reasons=("low_type_confidence",))
    sweep(world)
    d = world.repo.get_document("doc_id")
    assert world.repo.mhr_get("doc_id")["status"] == "uploaded"
    assert d["status"] == "proposed" and json.loads(d["review_reasons"]) == []


def test_identity_uploaded_by_an_older_version_is_settled_on_the_next_pass(world):
    add_scan(world, [(new_card_text(), {})], doc_id="doc_id", type_id="id_card")
    sweep(world)
    world.repo.conn.execute("UPDATE documents SET status='needs_review', review_reasons=? WHERE id='doc_id'",
                            (json.dumps(["low_type_confidence"]),))
    world.repo.conn.commit()
    assert settle_confirmed_identities(world.repo, world.cfg) == 1 and settle_confirmed_identities(world.repo, world.cfg) == 0
    d = world.repo.get_document("doc_id")
    assert d["status"] == "proposed" and json.loads(d["review_reasons"]) == []


def test_other_review_reasons_stay_with_the_operator(world):
    add_scan(world, [(new_card_text(), {})], doc_id="doc_id", type_id="id_card", status="needs_review",
             reasons=("low_type_confidence", "possible_duplicate"))
    sweep(world)
    d = world.repo.get_document("doc_id")
    assert d["status"] == "needs_review" and json.loads(d["review_reasons"]) == ["possible_duplicate"]
    assert settle_confirmed_identities(world.repo, world.cfg) == 0


def test_unconfirmed_identity_stays_in_the_review_list(world):
    add_scan(world, [(new_card_text().replace("\nნინო\n", "\nნინა\n").replace("NINO", "NINA"), {})], doc_id="doc_id",
             type_id="id_card", status="needs_review", reasons=("low_type_confidence",))
    sweep(world)
    d = world.repo.get_document("doc_id")
    assert world.repo.mhr_get("doc_id")["status"] == "failed" and d["status"] == "needs_review"
    assert settle_confirmed_identities(world.repo, world.cfg) == 0


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
