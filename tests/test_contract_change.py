"""შეთანხმება ხელშეკრულებაში ცვლილების შესახებ (ანაზღაურება, პოზიცია): ხელშეკრულების დანართად იცნობა, სათაური ორ ხაზზე
გადატანილიც. ხელშეკრულებად აღარ ითვლება, ამიტომ შეთანხმებაში ნახსენები ძველი ხელშეკრულების თარიღი ვადად აღარ იკითხება.
ფრჩხილი რიცხვს მიწებებული („2100(ორი ათას ასი)“) ციფრის ცვლილება არ არის. მონაცემები გამოგონილია."""
from __future__ import annotations

import json

import pytest

from hrsorter.classifier import Classifier
from hrsorter.config import EXAMPLE_TYPES_PATH, SHIPPED_TYPE_VERSIONS, make_config, type_fingerprint, update_shipped_types
from hrsorter.models import PageText
from hrsorter.textcompare import compare_texts


def agreement(title: str, change: str = "ანაზღაურების", salary: str = "2100 (ორი ათას ასი)") -> str:
    return (f"{title}\n\nქ. თბილისი                                                  2026-10-28წ.\n\n"
            "ერთი მხრივ შპს „ალფა 2009“ (ს/ნ 200000001), წარმოდგენილი დირექტორის დავით ლომიძის (პ/ნ 01001000002) სახით, "
            "შემდგომში წოდებული, როგორც „დამსაქმებელი“ და მეორე მხრივ ნინო ბერიძე (01001000001), შემდგომში წოდებული, როგორც "
            "„დასაქმებული“, ხელშემკვრელი მხარეები ვთანხმდებით შემდეგზე:\n"
            f"1. შევიდეს ცვლილება მხარეთა შორის 2026-09-15-ს გაფორმებულ შრომით ხელშეკრულებაში, დასაქმებულის {change} "
            "განსაზღვრის დებულებებში და მე-4 მუხლის 4.1. პუნქტი ჩამოყალიბდეს შემდეგი რედაქციით:\n"
            f"4.1. დასაქმებულის საზღაური ყოველთვიურად შეადგენს {salary} ლარს.\n"
            "2. ხელშეკრულების სხვა პირობები, რომელსაც ეს ცვლილება არ შეხებია, დარჩეს უცვლელი.\n"
            "3. ეს შეთანხმება ძალაში შედის 2026-11-01-დან.\n\n"
            "დამსაქმებელი:\nშპს ,,ალფა 2009“,\nდირექტორი: დავით ლომიძე [ხელნაწერი: ხელმოწერა]\n\n"
            "დასაქმებული:\nნინო ბერიძე [ხელნაწერი: ხელმოწერა]")


PROBATION_WRAPPED = "შეთანხმება გამოსაცდელი ვადით შრომით ხელშეკრულებაში ანაზღაურების ცვლილების\nშეტანის შესახებ"
ONE_LINE = "შეთანხმება შრომით ხელშეკრულებაში ანაზღაურების ცვლილების შეტანის შესახებ"
POSITION = "შეთანხმება შრომით ხელშეკრულებაში პოზიციის ცვლილების შესახებ"
CONTRACT = ("შრომითი ხელშეკრულება № 15/2\nქ. თბილისი 2026-09-15\n"
            "დამსაქმებელი: შპს „ალფა 2009“, დასაქმებული: ნინო ბერიძე, პ/ნ 01001000001\n"
            "1.1. ხელშეკრულება ძალაში შედის 2026 წლის 15 სექტემბრიდან და მოქმედებს 2027 წლის 15 სექტემბრამდე.\n"
            "4.1. დასაქმებულის ხელფასი შეადგენს 1500 ლარს თვეში.")
TERMINATION = ("შეთანხმება შრომითი ხელშეკრულების შეწყვეტის შესახებ\nქ. თბილისი 2026-11-05\n"
               "ერთი მხრივ შპს „ალფა 2009“, შემდგომში დამსაქმებელი და მეორე მხრივ ნინო ბერიძე (01001000001), შემდგომში "
               "დასაქმებული, ვთანხმდებით შემდეგზე:\n1. შეწყდეს მხარეთა შორის 2026-09-15-ს გაფორმებული შრომითი ხელშეკრულება.")


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
@pytest.mark.parametrize("title, change", [(PROBATION_WRAPPED, "ანაზღაურების"), (ONE_LINE, "ანაზღაურების"),
                                           (POSITION, "პოზიციის")])
def test_change_agreement_is_a_contract_annex(clf, title, change):
    c = clf.classify_pages([pt(agreement(title, change))])
    assert c.type_id == "contract_annex" and c.confidence >= 0.9, c.evidence


def test_change_agreement_title_marks_the_start_of_a_document(clf):
    assert any(h.type_id == "contract_annex" for h in clf.title_hits(agreement(PROBATION_WRAPPED)))


def test_ordinary_contract_is_still_a_contract(clf):
    assert clf.classify_pages([pt(CONTRACT)]).type_id == "contract"


def test_termination_agreement_is_not_a_change_annex(clf):
    assert clf.classify_pages([pt(TERMINATION)]).type_id != "contract_annex"


# --- ტექსტის შედარება ------------------------------------------------------------------------------------------------
def _compare_docx(cfg, original: str, scanned: str):
    """როგორც MHR-ის docx ორიგინალთან: გვერდების გარეშე, სკანის გვერდი ონლაინ მკითხველმა წაიკითხა (სანდო გვერდი)."""
    return [d for d in compare_texts([original], [scanned], cfg, original_paginated=False, trusted_pages={1}) if d.significant]


def test_bracket_glued_to_the_salary_is_not_a_change(cfg):
    assert _compare_docx(cfg, agreement(ONE_LINE), agreement(ONE_LINE, salary="2100(ორი ათას ასი)")) == []


def test_changed_salary_next_to_a_bracket_is_still_a_change(cfg):
    s = _compare_docx(cfg, agreement(ONE_LINE), agreement(ONE_LINE, salary="2200(ორი ათას ასი)"))
    assert len(s) == 1 and "2100" in s[0].original and "2200" in s[0].scanned


# --- განახლება ------------------------------------------------------------------------------------------------------
CONTRACT_0_1_31 = {
    "id": "contract", "name_ka": "ხელშეკრულება", "folder": "ხელშეკრულება", "description": "შრომითი ხელშეკრულება",
    "positive": [{"pattern": "შრომითი\\s+ხელშეკრულება", "weight": 5, "title_only": True},
                 {"pattern": "ხელშეკრულება", "weight": 3, "title_only": True},
                 {"pattern": "დამსაქმებელი", "weight": 1}, {"pattern": "დასაქმებული", "weight": 1},
                 {"pattern": "შრომის\\s+ანაზღაურება|ხელფას", "weight": 1},
                 {"pattern": "ხელშეკრულების\\s+(ვადა|მოქმედების)", "weight": 1}],
    "negative": [{"pattern": "დანართი", "weight": 4, "title_only": True},
                 {"pattern": "ბრძანება", "weight": 3, "title_only": True},
                 {"pattern": "შეწყვეტის\\s+შესახებ", "weight": 3, "title_only": True}],
    "fields": ["contract_start", "contract_end", "signature_status"],
    "active": True, "auto_save_enabled": False, "attachment_policy": "keep_continuation", "signature_expected": True,
}
ANNEX_0_1_31 = {
    "id": "contract_annex", "name_ka": "ხელშეკრულების დანართი", "folder": "ხელშეკრულების-დანართი",
    "description": "შრომითი ხელშეკრულების დანართი / ცვლილება",
    "positive": [{"pattern": "ხელშეკრულების\\s+დანართი", "weight": 5, "title_only": True},
                 {"pattern": "დანართი\\s*(№|n)?\\s*\\d*", "weight": 4, "title_only": True},
                 {"pattern": "ცვლილებ(ის|ები)\\s+შეტანის", "weight": 2}],
    "negative": [], "fields": ["signature_status"],
    "active": True, "auto_save_enabled": False, "attachment_policy": "review", "signature_expected": True,
}


def test_old_shipped_contract_rules_are_recognised():
    assert type_fingerprint(CONTRACT_0_1_31) in SHIPPED_TYPE_VERSIONS["contract"]
    assert type_fingerprint(ANNEX_0_1_31) in SHIPPED_TYPE_VERSIONS["contract_annex"]


def test_untouched_contract_types_get_the_new_rules_on_update(tmp_path):
    example = json.loads(EXAMPLE_TYPES_PATH.read_text(encoding="utf-8"))
    live = json.loads(json.dumps(example))
    old = {"contract": CONTRACT_0_1_31, "contract_annex": ANNEX_0_1_31}
    live["types"] = [old.get(t["id"], t) for t in live["types"]]
    path = tmp_path / "document_types.json"
    path.write_text(json.dumps(live, ensure_ascii=False, indent=2), encoding="utf-8")
    assert sorted(update_shipped_types(path)) == ["contract", "contract_annex"]
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after == example
