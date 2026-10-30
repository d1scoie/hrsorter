"""საზღვრების დეტექტორის ტესტები — PageFeatures პირდაპირ და რეალისტური ტექსტებიდან."""
from __future__ import annotations

import random
import time

import pytest

from hrsorter.boundary import check_coverage, detect_segments
from hrsorter.classifier import Classifier
from hrsorter.config import make_config
from hrsorter.features import page_features
from hrsorter.models import InputMode, PageFeatures, PageText, Segment, SegmentKind


@pytest.fixture
def cfg(tmp_path):
    return make_config({"paths": {"root": str(tmp_path)}})


def feat(i: int, **kw) -> PageFeatures:
    d = dict(page_index=i, is_blank=False, marks_only=False, title_type_id=None, title_text=None, page_type=None,
             personal_numbers=[], employee_names=[], doc_number=None, page_number=None, page_total=None,
             starts_with_continuation=False, ends_mid_sentence=False, has_closing_block=False, text_len=800,
             ocr_quality=0.9)
    d.update(kw)
    return PageFeatures(**d)


def blank(i: int) -> PageFeatures:
    return feat(i, is_blank=True, text_len=0)


def marks(i: int) -> PageFeatures:
    return feat(i, marks_only=True, has_closing_block=True, text_len=0)


def title(i: int, type_id: str, pn: str | None = None, **kw) -> PageFeatures:
    return feat(i, title_type_id=type_id, title_text=f"{type_id} სათაური", personal_numbers=[pn] if pn else [], **kw)


def pt(i: int, text: str, **kw) -> PageText:
    d = dict(page_index=i, text=text, text_source="text_layer", ocr_quality=0.9, is_blank=False,
             has_marks=False, marks_only=False, ink_ratio=0.1)
    d.update(kw)
    return PageText(**d)


def kinds(res) -> list[tuple[str, int, int]]:
    return [(s.kind.value, s.start, s.end) for s in res.segments]


# --- რეალისტური ტექსტები ----------------------------------------------------------------
CONTRACT_P1 = """შპს „ალფა"
ქ. თბილისი, 12 მარტი 2024 წ.
შრომითი ხელშეკრულება № 15/2
დამსაქმებელი: შპს „ალფა", წარმოდგენილი დირექტორის გიორგი ლომიძის სახით, პ/ნ 01001234567
დასაქმებული: ნინო კაპანაძე, პ/ნ 00912345678
1.1. დამსაქმებელი იღებს დასაქმებულს მენეჯერის თანამდებობაზე.
1.2. ხელშეკრულება ძალაში შედის 2024 წლის 15 მარტიდან და მოქმედებს 2025 წლის 14 მარტამდე.
1.3. დასაქმებული ვალდებულია შეასრულოს სამუშაო აღწერილობით გათვალისწინებული ფუნქციები და
"""
CONTRACT_P2 = """დაიცვას შინაგანაწესი, რომელიც დამსაქმებლის მიერ არის დამტკიცებული.
2. შრომის ანაზღაურება
2.1. დასაქმებულის ხელფასი შეადგენს 1500 ლარს თვეში, რომელიც გაიცემა თვის ბოლო სამუშაო დღეს.
3.1. სამუშაო კვირა შეადგენს 40 საათს. დასაქმებულს ეკუთვნის ანაზღაურებადი შვებულება წელიწადში 24 სამუშაო დღით და
"""
CONTRACT_P3 = """ანაზღაურების გარეშე შვებულება კანონმდებლობით დადგენილი წესით.
4. ხელშეკრულების შეწყვეტა
4.1. ხელშეკრულება შეიძლება შეწყდეს საქართველოს შრომის კოდექსით გათვალისწინებულ შემთხვევებში.
მხარეთა ხელმოწერები:
დამსაქმებელი: ________________ გ. ლომიძე
დასაქმებული: ________________ ნ. კაპანაძე
ბ.ა.
"""
VACATION = """შპს „ალფა"-ს დირექტორს ბატონ გიორგი ლომიძეს
განცხადება
გთხოვთ მომცეთ ანაზღაურებადი შვებულება 2024 წლის 1 ივლისიდან 14 ივლისის ჩათვლით, 14 კალენდარული დღის ოდენობით.
განმცხადებელი: ნინო კაპანაძე, პ/ნ 00912345678
თარიღი: 20.06.2024
ხელმოწერა: __________
"""
FINE = """შპს „ალფა"
ბრძანება № 34-კ
ქ. თბილისი 05.08.2024
თანამშრომლის ნინო კაპანაძის დაჯარიმების შესახებ
შრომის შინაგანაწესის დარღვევისთვის ვბრძანებ:
1. თანამშრომელ ნინო კაპანაძეს (პ/ნ 00912345678) დაეკისროს ჯარიმა 150 ლარის ოდენობით.
დირექტორი ________________ გიორგი ლომიძე
"""


def features_from(cfg, texts: list[str | None]) -> list[PageFeatures]:
    clf = Classifier(cfg)
    pages = [pt(i, "", is_blank=True) if t is None else pt(i, t) for i, t in enumerate(texts)]
    return [page_features(p, clf, cfg) for p in pages]


# --- რეალისტური სცენარები ----------------------------------------------------------------
def test_contract_three_pages_without_page_markers_stays_one_doc(cfg):
    res = detect_segments(features_from(cfg, [CONTRACT_P1, CONTRACT_P2, CONTRACT_P3]), InputMode.BATCH_PDF, cfg)
    assert kinds(res) == [("document", 0, 2)]
    assert res.segments[0].boundary_confidence >= cfg.thresholds["boundary_start"]


def test_contract_three_pages_with_page_markers_stays_one_doc(cfg):
    texts = [CONTRACT_P1 + "\nგვერდი 1/3", "გვ. 2/3\n" + CONTRACT_P2, "გვ. 3/3\n" + CONTRACT_P3]
    res = detect_segments(features_from(cfg, texts), InputMode.BATCH_PDF, cfg)
    assert kinds(res) == [("document", 0, 2)]


def test_contract_then_vacation_then_fine_same_employee_split(cfg):
    res = detect_segments(features_from(cfg, [CONTRACT_P1, CONTRACT_P2, CONTRACT_P3, VACATION, FINE]), InputMode.BATCH_PDF, cfg)
    assert kinds(res) == [("document", 0, 2), ("document", 3, 3), ("document", 4, 4)]
    assert all(s.evidence for s in res.segments)


def test_realistic_features_have_expected_signals(cfg):
    f = features_from(cfg, [CONTRACT_P1, CONTRACT_P2, FINE])
    assert f[0].title_type_id == "contract" and f[0].personal_numbers == ["00912345678"]
    assert f[0].doc_number == "15/2" and f[0].ends_mid_sentence
    assert f[1].title_text is None and f[1].starts_with_continuation
    assert f[2].title_type_id == "fine" and f[2].doc_number == "34-კ" and f[2].has_closing_block


# --- სინთეზური ნიშნები --------------------------------------------------------------------
def test_vacation_then_fine_same_employee_split_by_features(cfg):
    fs = [title(0, "vacation", "00912345678", has_closing_block=True), title(1, "fine", "00912345678")]
    res = detect_segments(fs, InputMode.BATCH_PDF, cfg)
    assert kinds(res) == [("document", 0, 0), ("document", 1, 1)]


def test_same_type_twice_different_employees_split(cfg):
    fs = [title(0, "contract", "00912345678"), feat(1, personal_numbers=["00912345678"]),
          title(2, "contract", "01001234567"), feat(3)]
    res = detect_segments(fs, InputMode.BATCH_PDF, cfg)
    assert kinds(res) == [("document", 0, 1), ("document", 2, 3)]


def test_same_pn_is_not_continuation_evidence_by_itself(cfg):
    fs = [title(0, "contract", "00912345678"), title(1, "fine", "00912345678")]
    res = detect_segments(fs, InputMode.BATCH_PDF, cfg)
    assert kinds(res) == [("document", 0, 0), ("document", 1, 1)]


def test_title_alone_is_not_enough_against_strong_continuation_evidence(cfg):
    # სათაურისებური ხაზი, მაგრამ გვერდი 2/2 და წინადადება გრძელდება → გაგრძელება
    fs = [title(0, "contract", "00912345678", page_number=1, page_total=2, ends_mid_sentence=True),
          feat(1, title_text="ხელშეკრულება", page_number=2, page_total=2, starts_with_continuation=True)]
    res = detect_segments(fs, InputMode.BATCH_PDF, cfg)
    assert kinds(res) == [("document", 0, 1)]


def test_page_number_reset_and_doc_number_change_split_without_title(cfg):
    fs = [feat(0, doc_number="12", page_number=1, page_total=2), feat(1, page_number=2, page_total=2),
          feat(2, doc_number="13", page_number=1, page_total=1, personal_numbers=["00000000001"])]
    res = detect_segments(fs, InputMode.BATCH_PDF, cfg)
    assert kinds(res) == [("document", 0, 1), ("document", 2, 2)]


def test_signature_only_page_attaches_to_previous_doc(cfg):
    fs = [title(0, "contract", "00912345678"), marks(1), title(2, "fine", "00912345678")]
    res = detect_segments(fs, InputMode.BATCH_PDF, cfg)
    assert kinds(res) == [("document", 0, 1), ("document", 2, 2)]


def test_blank_between_docs_becomes_blank_segment(cfg):
    fs = [title(0, "contract", "00912345678"), blank(1), title(2, "fine", "01001234567")]
    res = detect_segments(fs, InputMode.BATCH_PDF, cfg)
    assert kinds(res) == [("document", 0, 0), ("blank", 1, 1), ("document", 2, 2)]


def test_blank_inside_doc_goes_to_blank_pages(cfg):
    fs = [title(0, "contract", "00912345678", ends_mid_sentence=True), blank(1),
          feat(2, starts_with_continuation=True, page_number=2)]
    res = detect_segments(fs, InputMode.BATCH_PDF, cfg)
    assert kinds(res) == [("document", 0, 2)]
    assert res.segments[0].blank_pages == [1]


def test_leading_and_trailing_blanks(cfg):
    fs = [blank(0), title(1, "contract", "00912345678"), blank(2), blank(3)]
    res = detect_segments(fs, InputMode.BATCH_PDF, cfg)
    assert kinds(res) == [("blank", 0, 0), ("document", 1, 1), ("blank", 2, 3)]


def test_ambiguous_page_makes_uncertain_segment_neighbors_stay_document(cfg):
    # p1: მხოლოდ სათაურისებური ხაზი ტიპის გარეშე, სხვა მტკიცებულება არ არის → ქულა შუა ზონაშია
    fs = [title(0, "contract", "00912345678", doc_number="1", employee_names=["ნინო კაპანაძე"]),
          feat(1, title_text="ბრძანება"),
          feat(2),
          title(3, "fine", "01001234567", page_number=1),
          feat(4, page_number=2)]
    res = detect_segments(fs, InputMode.BATCH_PDF, cfg)
    assert kinds(res) == [("uncertain", 0, 2), ("document", 3, 4)]
    unc = res.segments[0]
    assert unc.boundary_confidence < cfg.thresholds["boundary_start"]
    assert any("uncertain" in e for e in unc.evidence)
    assert res.segments[1].kind == SegmentKind.DOCUMENT


def test_annex_after_contract_review_policy_is_uncertain(cfg):
    fs = [title(0, "contract", "00912345678", has_closing_block=True), title(1, "contract_annex", "00912345678"),
          title(2, "fine", "00912345678")]
    res = detect_segments(fs, InputMode.BATCH_PDF, cfg)
    assert kinds(res) == [("uncertain", 0, 1), ("document", 2, 2)]
    assert any("attachment policy review" in e for e in res.segments[0].evidence)


def test_annex_for_different_employee_splits_despite_review_policy(cfg):
    fs = [title(0, "contract", "00912345678"), title(1, "contract_annex", "01001234567")]
    res = detect_segments(fs, InputMode.BATCH_PDF, cfg)
    assert kinds(res) == [("document", 0, 0), ("document", 1, 1)]


def test_split_independent_policy_adds_weight(tmp_path):
    import json
    from hrsorter.config import EXAMPLE_TYPES_PATH
    types = json.loads(EXAMPLE_TYPES_PATH.read_text(encoding="utf-8"))
    for t in types["types"]:
        if t["id"] == "explanation":
            t["attachment_policy"] = "split_independent"
    cfg = make_config({"paths": {"root": str(tmp_path)}}, types_data=types)
    fs = [title(0, "contract", "00912345678", page_number=1, page_total=2),
          feat(1, title_type_id="explanation", title_text="ახსნა-განმარტება", page_number=2, page_total=2)]
    res = detect_segments(fs, InputMode.BATCH_PDF, cfg)
    assert kinds(res) == [("document", 0, 0), ("document", 1, 1)]


def test_no_evidence_page_is_continuation(cfg):
    fs = [title(0, "contract", "00912345678"), feat(1), feat(2), feat(3)]
    res = detect_segments(fs, InputMode.BATCH_PDF, cfg)
    assert kinds(res) == [("document", 0, 3)]


# --- წაუკითხავი გვერდები და თავფურცელი ------------------------------------------------------
def unreadable(i: int) -> PageFeatures:
    return feat(i, text_len=0, ocr_quality=0.0)


def test_unreadable_page_is_never_a_confident_continuation(cfg):
    fs = [title(0, "explanation", "00912345678", has_closing_block=True), unreadable(1), unreadable(2),
          title(3, "fine", "01001234567")]
    res = detect_segments(fs, InputMode.BATCH_PDF, cfg)
    assert kinds(res) == [("uncertain", 0, 2), ("document", 3, 3)]
    assert res.segments[0].boundary_confidence < cfg.thresholds["boundary_auto"]
    assert any("unreadable" in e for e in res.segments[0].evidence)


def test_low_ocr_quality_page_is_uncertain(cfg):
    fs = [title(0, "contract", "00912345678"), feat(1, ocr_quality=cfg.thresholds["ocr_min_quality"] - 0.01)]
    res = detect_segments(fs, InputMode.BATCH_PDF, cfg)
    assert kinds(res) == [("uncertain", 0, 1)]


def test_unreadable_first_page_opens_uncertain(cfg):
    res = detect_segments([unreadable(0), feat(1)], InputMode.BATCH_PDF, cfg)
    assert kinds(res) == [("uncertain", 0, 1)]


def test_single_mode_unreadable_pages_lower_confidence_not_split(cfg):
    fs = [title(0, "contract", "00912345678"), unreadable(1), feat(2)]
    res = detect_segments(fs, InputMode.SINGLE_DOCUMENT_PDF, cfg)
    assert kinds(res) == [("document", 0, 2)]
    assert res.segments[0].boundary_confidence < cfg.thresholds["boundary_auto"]
    assert any("unreadable" in e for e in res.segments[0].evidence)


def test_header_block_after_closing_starts_new_doc_without_known_title(cfg):
    # განცხადება ხელმოწერის ხაზით, შემდეგ უცნობი ტიპის დოკუმენტი საკუთარი თავფურცლით
    fs = [title(0, "application", "00912345678", has_closing_block=True),
          feat(1, has_header_block=True, header_company="beta", doc_number="0592")]
    res = detect_segments(fs, InputMode.BATCH_PDF, cfg)
    assert kinds(res) == [("document", 0, 0), ("document", 1, 1)]


def test_header_block_without_title_is_at_least_uncertain(cfg):
    fs = [title(0, "contract", "00912345678", ends_mid_sentence=True), feat(1, has_header_block=True)]
    res = detect_segments(fs, InputMode.BATCH_PDF, cfg)
    assert res.segments[0].kind == SegmentKind.UNCERTAIN


def test_header_company_change_is_strong_start_evidence(cfg):
    fs = [feat(0, has_header_block=True, header_company="alpha", personal_numbers=["00912345678"]),
          feat(1, has_header_block=True, header_company="beta")]
    res = detect_segments(fs, InputMode.BATCH_PDF, cfg)
    assert kinds(res) == [("document", 0, 0), ("document", 1, 1)]
    assert detect_segments(fs, InputMode.SINGLE_DOCUMENT_PDF, cfg).mode_mismatch is True


def test_annex_with_own_header_after_order_is_independent(cfg):
    fs = [title(0, "hire_order", "00912345678", has_closing_block=True),
          title(1, "contract_annex", "00912345678", has_header_block=True, header_company="alpha", doc_number="1")]
    res = detect_segments(fs, InputMode.BATCH_PDF, cfg)
    assert kinds(res) == [("document", 0, 0), ("document", 1, 1)]


def test_realistic_header_block_and_wrapped_body_line_is_not_title(cfg):
    invoice = "შპს „ბეტა\"\nს/კ 000000001\n№ 0592\nქუთაისი, 26.06.2023\nანგარიშ-ფაქტურა\nმიმწოდებელი: შპს „ბეტა\". მყიდველი: შპს „გამა\".\nსულ გადასახდელი: 825 ლარი.\nმიმწოდებლის წარმომადგენელი: ______"
    page2 = ("მხარეს წერილობით აფრთხილებს კანონით დადგენილ ვადაში.\n9. დასკვნითი დებულებები\n"
             "ხელშეკრულება შედგენილია ქართულ ენაზე, თანაბარი იურიდიული ძალის მქონე ორ\nეგზემპლარად, თითო თითოეული მხარისთვის.\n"
             "დირექტორი: ლევან მაისურაძე ხელმოწერა: ______\nდასაქმებული: სალომე მჭედლიშვილი ხელმოწერა: ______")
    f = features_from(cfg, [invoice, page2])
    assert f[0].has_header_block and f[0].title_text is None
    assert f[1].title_text is None and f[1].starts_with_continuation and not f[1].has_header_block
    res = detect_segments(features_from(cfg, [CONTRACT_P1, page2, VACATION, invoice]), InputMode.BATCH_PDF, cfg)
    assert kinds(res) == [("document", 0, 1), ("document", 2, 2), ("document", 3, 3)]


# --- single_document_pdf ------------------------------------------------------------------
def test_single_mode_never_splits_and_flags_mismatch(cfg):
    fs = [title(0, "contract", "00912345678"), feat(1), title(2, "fine", "01001234567"), blank(3), feat(4)]
    res = detect_segments(fs, InputMode.SINGLE_DOCUMENT_PDF, cfg)
    assert kinds(res) == [("document", 0, 4)]
    assert res.segments[0].blank_pages == [3]
    assert res.mode_mismatch is True
    assert res.evidence


def test_single_mode_continuation_pages_no_mismatch(cfg):
    fs = [title(0, "contract", "00912345678"), feat(1, page_number=2), feat(2, page_number=3), marks(3)]
    res = detect_segments(fs, InputMode.SINGLE_DOCUMENT_PDF, cfg)
    assert kinds(res) == [("document", 0, 3)]
    assert res.mode_mismatch is False


def test_single_mode_same_type_repeated_same_employee_is_not_mismatch(cfg):
    # იგივე სათაური/იგივე თანამშრომელი (მაგ. დანართი) — არ არის „ცალსახად დამოუკიდებელი"
    fs = [title(0, "contract", "00912345678"), title(1, "contract", "00912345678")]
    res = detect_segments(fs, InputMode.SINGLE_DOCUMENT_PDF, cfg)
    assert res.mode_mismatch is False


def test_single_mode_all_blank(cfg):
    res = detect_segments([blank(0), blank(1)], InputMode.SINGLE_DOCUMENT_PDF, cfg)
    assert kinds(res) == [("blank", 0, 1)]


def test_mode_accepts_string_value(cfg):
    res = detect_segments([title(0, "contract")], "single_document_pdf", cfg)
    assert kinds(res) == [("document", 0, 0)]


def test_empty_input(cfg):
    res = detect_segments([], InputMode.BATCH_PDF, cfg)
    assert res.segments == [] and res.mode_mismatch is False


# --- ინვარიანტი და წარმადობა ------------------------------------------------------------------
def random_feature(rng: random.Random, i: int) -> PageFeatures:
    r = rng.random()
    if r < 0.08:
        return blank(i)
    if r < 0.14:
        return marks(i)
    types = [None, "contract", "fine", "vacation", "contract_annex", "application"]
    t = rng.choice(types)
    pn = rng.choice([None, "00912345678", "01001234567", "12345678901"])
    return feat(i, title_type_id=t, title_text=(f"{t} სათაური" if t else rng.choice([None, "ბრძანება"])),
                personal_numbers=[pn] if pn else [], doc_number=rng.choice([None, "1", "2", "3"]),
                page_number=rng.choice([None, None, 1, 2, 3]), page_total=rng.choice([None, 2, 3]),
                starts_with_continuation=rng.random() < 0.3, ends_mid_sentence=rng.random() < 0.3,
                has_closing_block=rng.random() < 0.3, employee_names=rng.choice([[], ["ა ბ"], ["გ დ"]]))


def test_page_accounting_invariant_on_random_sequences(cfg):
    rng = random.Random(1234)
    for trial in range(300):
        n = rng.randint(1, 25)
        fs = [random_feature(rng, i) for i in range(n)]
        for mode in (InputMode.BATCH_PDF, InputMode.SINGLE_DOCUMENT_PDF):
            res = detect_segments(fs, mode, cfg)
            covered = [p for s in res.segments for p in s.pages]
            assert covered == list(range(n)), (trial, mode, kinds(res))
            for s in res.segments:
                assert 0.0 <= s.boundary_confidence <= 1.0
                assert all(s.start <= b <= s.end for b in s.blank_pages)
                assert s.kind != SegmentKind.BLANK or not s.blank_pages
                if s.kind == SegmentKind.UNCERTAIN:
                    assert s.boundary_confidence < cfg.thresholds["boundary_start"]


def test_check_coverage_detects_gaps_and_overlaps():
    ok = [Segment(0, 1, SegmentKind.DOCUMENT, 1.0), Segment(2, 2, SegmentKind.BLANK, 1.0)]
    check_coverage(ok, 0, 2)
    with pytest.raises(RuntimeError):
        check_coverage([Segment(0, 1, SegmentKind.DOCUMENT, 1.0), Segment(3, 3, SegmentKind.BLANK, 1.0)], 0, 3)
    with pytest.raises(RuntimeError):
        check_coverage([Segment(0, 1, SegmentKind.DOCUMENT, 1.0), Segment(1, 2, SegmentKind.DOCUMENT, 1.0)], 0, 2)
    with pytest.raises(RuntimeError):
        check_coverage([Segment(0, 1, SegmentKind.DOCUMENT, 1.0)], 0, 2)


def test_600_pages_runs_fast(cfg):
    rng = random.Random(7)
    fs = [random_feature(rng, i) for i in range(600)]
    t0 = time.perf_counter()
    res = detect_segments(fs, InputMode.BATCH_PDF, cfg)
    assert time.perf_counter() - t0 < 2.0
    assert [p for s in res.segments for p in s.pages] == list(range(600))


# --- რეალური სკანებიდან ნასწავლი წესები (2026-10-28 პილოტი) --------------------------------
def _contract_page(date: str, body: str, opening: bool = False, footer: str = "") -> str:
    head = "ხელშეკრულება გამოსაცდელი ვადით შრომითი ურთიერთობის\nშესახებ\nქ. თბილისი " + date + "\n"
    if opening:
        body = ("პრეამბულა\nწინამდებარე ხელშეკრულებაზე ხელის მოწერით ერთი მხრივ, შპს „მაგალითი“ და მეორე მხრივ, "
                "ნინო ბერიძე (პ/ნ 01001000001) ვთანხმდებით შემდეგზე:\n") + body
    return head + body + ("\n" + footer if footer else "")


def test_running_page_header_does_not_split_document(cfg):
    pages = [_contract_page("2026-10-28", "1.1. ხელშეკრულების საგანია შრომითი ურთიერთობა და მხარეთა", True, "1"),
             _contract_page("2026-10-28", "ვალდებულებები, რომლებიც განსაზღვრულია ამ ხელშეკრულებით და", footer="2"),
             _contract_page("2026-10-28", "კანონმდებლობით. 3.1. ანაზღაურება გაიცემა ყოველთვიურად.", footer="3")]
    res = detect_segments(features_from(cfg, pages), InputMode.BATCH_PDF, cfg)
    assert kinds(res) == [("document", 0, 2)]


def test_same_template_new_date_with_preamble_starts_new_document(cfg):
    pages = [_contract_page("2026-09-16", "1.1. ხელშეკრულების საგანია სტაჟირება.", True),
             _contract_page("2026-09-16", "2.1. მხარეები ვალდებულნი არიან კეთილსინდისიერად შეასრულონ პირობები."),
             _contract_page("2026-09-04", "1.1. ხელშეკრულების საგანია სტაჟირება.", True)]
    res = detect_segments(features_from(cfg, pages), InputMode.BATCH_PDF, cfg)
    assert kinds(res) == [("document", 0, 1), ("document", 2, 2)]


def test_wrapped_title_ending_with_shesaxeb_is_a_title(cfg):
    from hrsorter.classifier import title_lines
    text = "ხელშეკრულება გამოსაცდელი ვადით შრომითი ურთიერთობის\n_ შესახებ\nქ. თბილისი 2026-10-28\nტექსტი."
    assert "ხელშეკრულება გამოსაცდელი ვადით შრომითი ურთიერთობის შესახებ" in title_lines(text)


def test_single_file_starting_mid_document_is_not_trusted(cfg):
    page = _contract_page("2026-10-28", "ვალდებულებები, რომლებიც განსაზღვრულია ამ ხელშეკრულებით და", footer="5")
    res = detect_segments(features_from(cfg, [page]), InputMode.SINGLE_DOCUMENT_PDF, cfg)
    assert res.segments[0].boundary_confidence < cfg.thresholds["boundary_auto"]
    assert any("mid-document" in e for e in res.segments[0].evidence)


def test_single_complete_document_still_trusted(cfg):
    page = _contract_page("2026-10-28", "1.1. ხელშეკრულების საგანია შრომითი ურთიერთობა.", True, "1")
    res = detect_segments(features_from(cfg, [page]), InputMode.SINGLE_DOCUMENT_PDF, cfg)
    assert res.segments[0].boundary_confidence >= cfg.thresholds["boundary_auto"]
