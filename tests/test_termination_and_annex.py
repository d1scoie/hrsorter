"""ორი ნასწავლი დოკუმენტი: „თანამდებობრივი ინსტრუქციის დანართი #N“ (MHR-ის ფაილს ედარება, საკუთარი ტიპით) და
„შეთანხმება … ხელშეკრულების შეწყვეტის შესახებ“ (MHR-ში ორიგინალი არ აქვს; თარიღები MHR-ის ჩანაწერს ედარება).
მონაცემები გამოგონილია."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hrsorter import mhr_queue as q
from hrsorter.boundary import detect_segments
from hrsorter.classifier import Classifier
from hrsorter.config import (DEFAULTS, EXAMPLE_TYPES_PATH, SHIPPED_TYPE_VERSIONS, make_config, merge_new_types, type_fingerprint,
                             update_shipped_types)
from hrsorter.db import Repository
from hrsorter.features import page_features
from hrsorter.mhr_client import MockMhr, employee_from
from hrsorter.models import InputMode, PageText
from hrsorter.termination import parse_termination
from tests.test_packets import PN, SIGNED, add_scan, make_docx

ANNEX_TITLE = "2030-11-09 წლის თანამდებობრივი ინსტრუქციის დანართი #2"
ANNEX = (f"{ANNEX_TITLE}\n\n2030-11-09\n\n"
         "1. აღნიშნულ დანართი განსაზღვრავს ტრანსპორტში ტვირთამწეობის მაქსიმალური ლიმიტების ოდენობას.\n"
         "2. ტრანსპორტის ტვირთამწეობის მაქსიმალური ლიმიტები განსაზღვრულია ცხრილში მოცემული მონაცემების შესაბამისად:\n"
         "ავტომობილის მარკა | მოდელი | სახელმწიფო ნომერი | ლიმიტი (კგ)\nალფა | ფურგონი | AA-001-AA | 920კგ\nბეტა | სატვირთო | BB-002-BB | 4470კგ\n"
         "3. წინამდებარე დანართი წარმოადგენს მხარეთა შორის არსებული თანამდებობრივი ინსტრუქციის განუყოფელ ნაწილს\n\n"
         "შპს ალფა 2009 (200000001)\nდირექტორი: დავით ლომიძე\n\nნინო ბერიძე\nპ/ნ: 01001000001\n\nდასაქმებული")
ANNEX_P1 = ("2030-11-09 წლის თანამდებობრივი ინსტრუქციის დანართი #1\n\n2030-11-09\n\n"
            "1. აღნიშნულ დანართი განსაზღვრავს პროდუქციით დატვირთული ერთეული ყუთის წონას.\nსახელწოდება | წონა (კგ)\nკრემი | 10 კგ\nხილი | 20 კგ")
ANNEX_P2 = ("2030-11-09 წლის თანამდებობრივი ინსტრუქციის დანართი #1\n\nფქვილი | 25 კგ\nნამცხვარი | 4.5 კგ\n\n"
            "3. წინამდებარე დანართი წარმოადგენს მხარეთა შორის არსებული თანამდებობრივი ინსტრუქციის განუყოფელ ნაწილს.\n"
            "შპს ალფა 2009 (200000001)\nდირექტორი: დავით ლომიძე\nდასაქმებული: ნინო ბერიძე")
INSTRUCTION = ("თანამდებობრივი ინსტრუქცია\nმენეჯერი ემსახურება მომხმარებლებს სალაროში ყოველდღიურად.\n"
               "პოზიცია ექვემდებარება ფილიალის მმართველს. საქმიანობაში უნდა იხელმძღვანელოს შინაგანაწესით.")
CONTRACT_ANNEX = "შრომითი ხელშეკრულების დანართი №1\nქ. თბილისი 2030-11-09\nმხარეები თანხმდებიან სამუშაო გრაფიკზე."


def termination(kind="სტაჟირების", signed="2030-10-12", ends="2030-11-14", party="სტაჟიორი") -> str:
    return ("შპს „ალფა 2009“\nLTD “ALFA 2009”\n\n"
            f"შეთანხმება {kind} ხელშეკრულების შეწყვეტის შესახებ\n\nქ. თბილისი 2030-11-05\n\n"
            "ერთი მხრივ შპს „ალფა 2009“ (ს/კ 200000001), წარმოდგენილი მისი დირექტორის, დავით ლომიძის (პ/ნ 01001000002) მიერ შემდგომში "
            f"წოდებული, როგორც დამსაქმებელი და მეორე მხრივ ნინო ბერიძე (პ/ნ 01001000001), შემდგომში წოდებული, როგორც „{party}“, "
            "ნების თავისუფალი გამოვლენის შედეგად ხელშემკვრელი მხარეები ვთანხმდებით შემდეგზე:\n"
            f"1. ურთიერთშეთანხმების საფუძველზე, შეწყდეს მხარეთა შორის {signed} წელს გაფორმებული {kind} შესახებ ხელშეკრულება.\n"
            f"2. {party} აცხადებს, რომ დამსაქმებლის მიმართ არ გააჩნია არანაირი ფულადი და არაფულადი სახის პრეტენზიები.\n"
            f"3. ხელშეკრულება შეწყვეტილად ჩაითვალოს {ends}-დან.\n"
            "4. შეთანხმება შედგენილია ქართულ ენაზე ორ ეგზემპლარად.\n\n"
            f"დამსაქმებელი:\nშპს ,,ალფა 2009“\nდირექტორი: დავით ლომიძე\n\n{party}:\nნინო ბერიძე")


DISMISSAL_ORDER = ("ბრძანება\nთანამშრომლის სამსახურიდან გათავისუფლების შესახებ\nქ. თბილისი 2030-11-05\n"
                   "ვბრძანებ: ნინო ბერიძე გათავისუფლდეს სამსახურიდან 2030-11-14-დან.")


def pt(text: str, i: int = 0) -> PageText:
    return PageText(page_index=i, text=text, text_source="ocr:tesseract", ocr_quality=0.9, is_blank=False, has_marks=True,
                    marks_only=False, ink_ratio=0.05)


@pytest.fixture
def cfg(tmp_path):
    return make_config({"paths": {"root": str(tmp_path)}})


@pytest.fixture
def clf(cfg):
    return Classifier(cfg)


# --- ტიპი ---------------------------------------------------------------------------------------------------------
def test_instruction_annex_is_its_own_type(clf):
    c = clf.classify_pages([pt(ANNEX)])
    assert c.type_id == "job_description_annex" and c.confidence >= 0.9, c.evidence
    assert clf.classify_pages([pt(ANNEX_P1), pt(ANNEX_P2, 1)]).type_id == "job_description_annex"


def test_instruction_and_contract_annex_keep_their_types(clf):
    assert clf.classify_pages([pt(INSTRUCTION)]).type_id == "job_description"
    assert clf.classify_pages([pt(CONTRACT_ANNEX)]).type_id == "contract_annex"


def test_annex_title_repeated_on_page_two_does_not_split_the_scan(cfg, clf):
    feats = [page_features(p, clf, cfg) for p in (pt(ANNEX_P1), pt(ANNEX_P2, 1))]
    for mode in (InputMode.SINGLE_DOCUMENT_PDF, InputMode.BATCH_PDF):
        seg = detect_segments(feats, mode, cfg)
        assert [s.pages for s in seg.segments] == [[0, 1]] and not seg.mode_mismatch


@pytest.mark.parametrize("kind, party", [("სტაჟირების", "სტაჟიორი"), ("შრომითი", "დასაქმებული")])
def test_termination_agreement_is_its_own_type(clf, kind, party):
    c = clf.classify_pages([pt(termination(kind, party=party))])
    assert c.type_id == "termination_agreement" and c.confidence >= 0.9, c.evidence


def test_dismissal_order_is_still_an_order(clf):
    assert clf.classify_pages([pt(DISMISSAL_ORDER)]).type_id == "dismissal_order"


# --- შეწყვეტის შეთანხმების თარიღები ------------------------------------------------------------------------------------
def test_termination_dates_are_read():
    t = parse_termination(termination())
    assert (t.contract_date, t.end_date) == ("2030-10-12", "2030-11-14")


@pytest.mark.parametrize("text, expect", [
    ("შეწყდეს მხარეთა შორის 2030-09-21-ს გაფორმებული ხელშეკრულება.\nხელშეკრულება შეწყვეტილად ჩაითვალოს 2030-11-14-დან.", ("2030-09-21", "2030-11-14")),
    ("შეწყდეს 12.10.2030 წელს დადებული ხელშეკრულება\nშეწყვეტილად ჩაითვალოს 2030 წლის 14 ნოემბრიდან", ("2030-10-12", "2030-11-14")),
    ("მხარეთა შორის 2030 წლის 12 ოქტომბერს გაფორმებული ხელშეკრულება შეწყდეს 2030 წლის 14 ნოემბრიდან.", ("2030-10-12", "2030-11-14")),
    ("შეთანხმება ხელშეკრულების შეწყვეტის შესახებ\nქ. თბილისი 2030-11-05\nმხარეები ვთანხმდებით.", (None, None)),   # არაფერი იგონება
])
def test_termination_date_wordings(text, expect):
    t = parse_termination(text)
    assert (t.contract_date, t.end_date) == expect


# --- MHR ------------------------------------------------------------------------------------------------------------
@pytest.fixture
def world(tmp_path):
    d = tmp_path / "mhr"
    (d / "files").mkdir(parents=True)
    d.joinpath("documents.json").write_text("[]", encoding="utf-8")
    cfg = make_config({"paths": {"root": str(tmp_path)}, "mhr": {"enabled": True, "mode": "mock", "mock_dir": str(d), "recheck": {"enabled": False}}})
    repo = Repository(cfg.paths.db)
    w = SimpleNamespace(cfg=cfg, repo=repo, mhr=MockMhr(d), dir=d, tmp=tmp_path)
    employee(w)
    yield w
    repo.close()


def employee(w, **fields) -> None:
    row = {"id": "E1", "personal_number": PN, "first_name": "ნინო", "last_name": "ბერიძე", "status": "active",
           "contract_start": "2030-10-12", "contract_end": "2031-01-12", "dismissed_at": None, **fields}
    (w.dir / "employees.json").write_text(json.dumps([row], ensure_ascii=False), encoding="utf-8")


def generate(w, doc_id: str, name: str, pages: list[str]) -> None:
    (w.dir / "files" / name).write_bytes(make_docx(pages))
    docs = json.loads((w.dir / "documents.json").read_text(encoding="utf-8"))
    docs.append({"id": doc_id, "employee_id": "E1", "category": "contract", "title": "ხელშეკრულების ცვლილება", "filename": name,
                 "created_at": "2030-11-09T10:00:00Z", "document_date": "2030-11-09", "kind": "generated", "signed_at": None})
    (w.dir / "documents.json").write_text(json.dumps(docs, ensure_ascii=False), encoding="utf-8")


def sweep(w) -> None:
    for _ in range(2):
        q.sweep(w.repo, w.cfg, w.mhr)


def result(w) -> tuple[str, dict, str]:
    m = w.repo.mhr_get("doc_scan")
    return m["status"], json.loads(m["validation"] or "{}"), w.repo.get_document("doc_scan")["type_id"]


def check(v: dict, field: str) -> dict | None:
    return next((c for c in v.get("checks", []) if c["field"] == field), None)


def test_annex_is_compared_with_its_mhr_file_and_keeps_its_type(world):
    generate(world, "G7", "საწყობი - დანართი #2 (შრომითი).docx", [ANNEX])     # MHR-ში ფაილს მხოლოდ „დანართი #2“ ჰქვია
    add_scan(world, [(ANNEX, SIGNED)], type_id="job_description_annex")
    sweep(world)
    status, v, type_id = result(world)
    assert status == "ready" and v["original"]["doc_id"] == "G7" and type_id == "job_description_annex"
    assert q.approve(world.repo, "doc_scan")
    sweep(world)
    up = json.loads((world.dir / "uploads.json").read_text(encoding="utf-8"))[0]
    assert up["signed_copy_of"] == "G7" and up["category"] == "contract"     # ხელმოწერილი ასლი ორიგინალის კატეგორიაში


def test_annex_without_its_file_in_mhr_waits_instead_of_passing(world):
    """ადრე: ტიპი უცნობია → „გამოტოვებული“ და სამუდამოდ გადასამოწმებელში. ახლა: „MHR-ში აკლია“ — ფაილი რომ გაჩნდება, თავიდან შედარდება."""
    add_scan(world, [(ANNEX, SIGNED)], type_id="job_description_annex")
    sweep(world)
    status, v, _ = result(world)
    assert status == "failed" and check(v, "original_contract")["status"] == "missing_in_mhr"
    generate(world, "G7", "საწყობი - დანართი #2 (შრომითი).docx", [ANNEX])
    world.repo.conn.execute("UPDATE mhr_items SET next_try=NULL")            # „MHR-ში აკლია“-ს თავისით გადამოწმების დრო მოვიდა
    sweep(world)
    assert result(world)[0] == "ready"


def test_unknown_scan_matching_an_annex_file_is_still_a_plain_annex(world):
    """სათაური ვერ წაიკითხა (ტიპი უცნობია), მაგრამ ტექსტი MHR-ის „… დანართი #2“ ფაილს ემთხვევა — როგორც ადრე, დანართია."""
    generate(world, "G7", "საწყობი - დანართი #2 (შრომითი).docx", [ANNEX])
    add_scan(world, [(ANNEX.replace(ANNEX_TITLE, ""), SIGNED)], type_id="unknown", status="needs_review", reasons=("unknown_type",))
    sweep(world)
    status, v, type_id = result(world)
    assert status == "ready" and type_id == "contract_annex"


def test_termination_agreement_matches_the_dismissal_in_mhr(world):
    employee(world, status="dismissed", dismissed_at="2030-11-14")
    add_scan(world, [(termination(), SIGNED)], type_id="termination_agreement")
    sweep(world)
    status, v, type_id = result(world)
    assert status == "ready" and type_id == "termination_agreement" and not v.get("original")
    assert check(v, "termination_date")["status"] == "match" and check(v, "terminated_contract")["status"] == "match"
    assert q.approve(world.repo, "doc_scan")
    sweep(world)
    up = json.loads((world.dir / "uploads.json").read_text(encoding="utf-8"))[0]
    assert up["category"] == "dismissal_order" and not up["signed_copy_of"]


def test_different_dismissal_date_in_mhr_stops_the_file(world):
    employee(world, status="dismissed", dismissed_at="2030-11-30")
    add_scan(world, [(termination(), SIGNED)], type_id="termination_agreement")
    sweep(world)
    status, v, _ = result(world)
    c = check(v, "termination_date")
    assert status == "failed" and c["status"] == "mismatch" and c["blocking"]
    assert (c["document_value"], c["mhr_value"]) == ("2030-11-14", "2030-11-30")


def test_employee_not_yet_dismissed_in_mhr_is_a_note_not_a_stop(world):
    add_scan(world, [(termination(), SIGNED)], type_id="termination_agreement")
    sweep(world)
    status, v, _ = result(world)
    c = check(v, "termination_date")
    assert status == "ready" and c["status"] == "missing_in_mhr" and not c["blocking"] and "გაატარე გათავისუფლება" in c["note"]


def test_other_contract_date_is_a_note(world):
    employee(world, status="dismissed", dismissed_at="2030-11-14")
    add_scan(world, [(termination(signed="2030-10-01"), SIGNED)], type_id="termination_agreement")
    sweep(world)
    status, v, _ = result(world)
    c = check(v, "terminated_contract")
    assert status == "ready" and c["status"] == "info" and not c["blocking"] and "2030-10-01" in c["note"]


def test_termination_agreement_without_a_signature_stops(world):
    employee(world, status="dismissed", dismissed_at="2030-11-14")
    add_scan(world, [(termination(), {"signature_hint": "not_detected", "signature_parties": {"employer": True, "employee": False}})],
             type_id="termination_agreement")
    sweep(world)
    status, v, _ = result(world)
    assert status == "failed" and any(d["kind"] == "signature" and d["significant"] for d in v["diffs"])


def test_dismissal_date_is_read_from_mhr():
    e = employee_from({"id": 1, "personal_number": PN, "dismissed_at": "2030-11-14T00:00:00Z"}, DEFAULTS["mhr"]["employee_map"])
    assert e.dismissed_at == "2030-11-14"
    assert employee_from({"id": 1, "dismissed_at": None}, DEFAULTS["mhr"]["employee_map"]).dismissed_at is None


# --- განახლება და შენახული გვერდები ------------------------------------------------------------------------------------
JOB_DESCRIPTION_0_1_33 = {
    "id": "job_description", "name_ka": "თანამდებობრივი ინსტრუქცია", "folder": "თანამდებობრივი-ინსტრუქცია",
    "description": "თანამშრომლის თანამდებობრივი ინსტრუქცია (MHR-ში დაგენერირებული, ეტაპის მიხედვით)",
    "positive": [{"pattern": "თანამდებობრივი\\s+ინსტრუქცია", "weight": 5, "title_only": True},
                 {"pattern": "თანამდებობრივ\\w*\\s+ინსტრუქცი", "weight": 2}, {"pattern": "პოზიცია\\s+ექვემდებარება", "weight": 1},
                 {"pattern": "საქმიანობაში\\s+(უნდა\\s+)?იხელმძღვანელოს", "weight": 1},
                 {"pattern": "სახელმძღვანელო\\s+დოკუმენტაცია", "weight": 1}],
    "negative": [{"pattern": "დანართი", "weight": 5, "title_only": True}, {"pattern": "ხელშეკრულება", "weight": 3, "title_only": True}],
    "fields": ["signature_status"], "active": True, "auto_save_enabled": False, "attachment_policy": "keep_continuation",
    "signature_expected": True, "mhr_original": True,
}
DISMISSAL_ORDER_0_1_33 = {
    "id": "dismissal_order", "name_ka": "გათავისუფლების ბრძანება", "folder": "გათავისუფლების-ბრძანება",
    "description": "სამსახურიდან გათავისუფლების / ხელშეკრულების შეწყვეტის ბრძანება",
    "positive": [{"pattern": "(სამსახურიდან|თანამდებობიდან)\\s+გათავისუფლებ", "weight": 4},
                 {"pattern": "გათავისუფლების\\s+შესახებ", "weight": 4, "title_only": True},
                 {"pattern": "შრომითი\\s+ხელშეკრულების\\s+შეწყვეტ", "weight": 3}, {"pattern": "ბრძანება", "weight": 2, "title_only": True}],
    "negative": [{"pattern": "შვებულებ", "weight": 3, "title_only": True}],
    "fields": ["signature_status"], "active": True, "auto_save_enabled": False, "attachment_policy": "keep_continuation",
    "signature_expected": True,
}


def test_update_adds_the_new_types_and_refreshes_untouched_ones(tmp_path):
    assert type_fingerprint(JOB_DESCRIPTION_0_1_33) in SHIPPED_TYPE_VERSIONS["job_description"]
    assert type_fingerprint(DISMISSAL_ORDER_0_1_33) in SHIPPED_TYPE_VERSIONS["dismissal_order"]
    example = json.loads(EXAMPLE_TYPES_PATH.read_text(encoding="utf-8"))
    old = {"job_description": JOB_DESCRIPTION_0_1_33, "dismissal_order": DISMISSAL_ORDER_0_1_33}
    live = {"types": [old.get(t["id"], t) for t in example["types"] if t["id"] not in ("job_description_annex", "termination_agreement")]}
    path = tmp_path / "document_types.json"
    path.write_text(json.dumps(live, ensure_ascii=False, indent=2), encoding="utf-8")
    assert sorted(merge_new_types(path)) == ["job_description_annex", "termination_agreement"]
    assert sorted(update_shipped_types(path)) == ["dismissal_order", "job_description"]
    after = {t["id"]: t for t in json.loads(path.read_text(encoding="utf-8"))["types"]}
    assert after == {t["id"]: t for t in example["types"]}
    assert list(after)[-1] == "unknown"


def test_stored_page_with_extra_marks_still_loads():
    """ვალიდაცია შენახულ გვერდს საკუთარ ნიშანს უმატებს (`identity_reread`) — ასეთი გვერდის ჩატვირთვა შეცდომას იწვევდა."""
    d = pt("ტექსტი").to_dict()
    d["identity_reread"] = True
    assert PageText.from_dict(d).text == "ტექსტი"
