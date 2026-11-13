"""ვინ მოაწერა ხელი: ონლაინ მკითხველი (Claude) ტრანსკრიფციის ბოლოს ერთ სტრიქონს წერს, პროგრამა მას ტექსტიდან ჭრის და
გვერდის `signature_parties`-ად ინახავს — ისე, როგორც ადგილობრივი ანალიზი. მონაცემები გამოგონილია."""
from __future__ import annotations

import sys

from hrsorter.config import make_config
from hrsorter.ocr import OcrResult, signatures
from hrsorter.ocr.chain import OcrChain
from hrsorter.pageanalysis import analyze_page
from hrsorter.pdfio import PdfDocument
from hrsorter.validation import last_page_signature_diffs, signature_diffs
from tests.test_empform import form_text, hired
from tests.test_ocr import FakeClaude, _cc_cfg, img  # noqa: F401  (img — fixture)
from tests.test_packets import add_scan, sweep, validation, world  # noqa: F401  (world — fixture)
from tests.test_pageanalysis import sample  # noqa: F401  (fixture)

PAGE = "შრომითი ხელშეკრულება\n1.1 დასაქმებული მუშაობს მენეჯერად\nდამსაქმებელი ________ დასაქმებული ________"


# --- სტრიქონის ამოღება ------------------------------------------------------------------------------------------
def test_line_is_cut_from_the_text_and_becomes_parties():
    text, parties = signatures.split(PAGE + "\n[SIGNATURES employer=yes employee=no]")
    assert text == PAGE and parties == {"employer": True, "employee": False}


def test_none_means_the_page_has_no_such_place():
    assert signatures.split(PAGE + "\n[SIGNATURES employer=none employee=yes]")[1] == {"employee": True}
    assert signatures.split("პირადობის მოწმობა\n[SIGNATURES employer=none employee=none]") == ("პირადობის მოწმობა", {})


def test_reader_that_wrote_no_line_leaves_parties_unknown():
    assert signatures.split(PAGE) == (PAGE, None)
    assert signatures.split("") == ("", None) and signatures.split(PAGE + "\n[SIGNATURES employer=maybe employee=yes]")[1] is None


def test_format_is_read_leniently_and_the_last_line_wins():
    assert signatures.split(PAGE + "\n[ signatures Employer = YES ; Employee = No ]\n")[1] == {"employer": True, "employee": False}
    printed = "[SIGNATURES employer=yes employee=yes]\n" + PAGE + "\n[SIGNATURES employer=no employee=no]"
    text, parties = signatures.split(printed)       # გვერდზე დაბეჭდილი იგივე ტექსტი მოდელის პასუხს ვერ ცვლის
    assert parties == {"employer": False, "employee": False} and "SIGNATURES" not in text


# --- მკითხველი --------------------------------------------------------------------------------------------------
def test_claude_reader_asks_about_signatures_and_returns_the_answer(img):  # noqa: F811
    from hrsorter.ocr.claude_code_vision import PROMPT, ClaudeCodeVisionProvider
    run = FakeClaude({"is_error": False, "subtype": "success", "result": PAGE + "\n[SIGNATURES employer=yes employee=no]",
                      "total_cost_usd": 0.1})
    p = ClaudeCodeVisionProvider(_cc_cfg(claude_cmd=sys.executable), runner=run, secret_getter=lambda s, n: None)
    res = p.recognize(img, "kat")
    assert res.text == PAGE and res.signature_parties == {"employer": True, "employee": False}
    sent = run.calls[0][0][2]
    assert "[SIGNATURES employer=X employee=Y]" in sent and "Mark handwritten" in sent and "{path}" not in sent
    assert PROMPT.count("{path}") == 1                           # ფორმატირება ისევ მხოლოდ ბილიკს ავსებს


def test_recheck_reading_does_not_ask_and_returns_no_answer(img):  # noqa: F811
    from hrsorter.ocr.claude_code_vision import ClaudeCodeVisionProvider
    run = FakeClaude({"is_error": False, "subtype": "success", "result": "მუხლი 5", "total_cost_usd": 0.1})
    p = ClaudeCodeVisionProvider(_cc_cfg(claude_cmd=sys.executable), runner=run, secret_getter=lambda s, n: None)
    res = p.recognize_printed(img, "kat")
    assert res.signature_parties is None and "SIGNATURES" not in run.calls[0][0][2]


def test_api_reader_asks_the_same_question():
    from hrsorter.ocr.anthropic_vision import SYSTEM
    assert signatures.ASK in SYSTEM


def test_signature_slightly_off_the_line_still_counts_everywhere():
    """მომხმარებლის წესი: ხელმოწერა ან ბეჭედი ხაზს ოდნავ ზემოთ ან ქვემოთ (ან დაბეჭდილ სახელზე) მაინც ადგილზეა — ეს
    მკითხველსაც ეუბნება, AI ანალიზსაც და ოპერატორსაც. დირექტორის ხელმოწერა/ბეჭედი კი ისევ სავალდებულოა."""
    from hrsorter import ai_review
    from hrsorter.ocr.claude_code_vision import PROMPT
    from hrsorter.ui.app import _problem_todo
    assert "slightly above or below it, or over the printed name, still counts as yes" in signatures.ASK
    assert "still counts as yes" in PROMPT and PROMPT.count("{path}") == 1
    assert "ხაზს ოდნავ ზემოთ ან" in ai_review._INSTRUCTIONS and "ნამდვილი პრობლემაა" in ai_review._INSTRUCTIONS
    todo = _problem_todo({"kind": "signature"}, "„ატვირთვა მაინც“")
    assert "თუნდაც ხაზს ოდნავ ზემოთ ან ქვემოთ" in todo and "მოაწერინე" in todo


# --- გვერდი -----------------------------------------------------------------------------------------------------
class _Reader:
    """ონლაინ მკითხველის მსგავსი: ტექსტი კოორდინატების გარეშე + პასუხი ხელმოწერებზე."""
    name, model_version, is_online = "fake", "fake-1", False

    def __init__(self, parties):
        self.parties = parties

    def available(self):
        return True

    def recognize(self, image, lang):
        return OcrResult(text="ბრძანება დაჯარიმების შესახებ ნინო ბერიძე ჯარიმა 100 ლარი დირექტორი", mean_confidence=0.8,
                         words=[], signature_parties=self.parties)


def _page(sample, parties):  # noqa: F811
    path, _ = sample
    cfg = make_config({"processing": {"dpi": 150, "deskew": False}})
    with PdfDocument(path) as doc:
        return analyze_page(doc, 5, cfg, OcrChain(_Reader(parties), None, "local", 0.35, "kat+eng"))


def test_page_keeps_what_the_reader_said_about_signatures(sample):  # noqa: F811
    assert _page(sample, {"employer": True, "employee": False}).signature_parties == {"employer": True, "employee": False}
    assert _page(sample, None).signature_parties == {}           # მკითხველმა არ უპასუხა → მხარეები უცნობია, როგორც ადრე
    assert _page(sample, {}).signature_parties == {}


# --- შედარება ----------------------------------------------------------------------------------------------------
def test_missing_party_stops_the_file_and_names_who():
    signed = {"text": PAGE, "signature_hint": "present", "signature_parties": {"employer": True, "employee": True}}
    one = {"text": PAGE, "signature_hint": "present", "signature_parties": {"employer": True, "employee": False}}
    assert signature_diffs([signed, signed], set()) == []
    diffs = signature_diffs([signed, one], set())
    assert [(d.page, d.significant, d.reason) for d in diffs] == [(2, True, "ხელმოწერა აკლია: დასაქმებული")]
    last = last_page_signature_diffs([signed, dict(one, signature_parties={"employer": False, "employee": True})])
    assert [(d.page, d.reason) for d in last] == [(2, "ბოლო გვერდზე ხელმოწერა აკლია: დამსაქმებელი")]


def test_form_follows_the_reader_when_it_says_the_employee_did_not_sign(world):  # noqa: F811
    """ანკეტაზე ხელნაწერის ნიშანი არ არის; გვერდზე რაღაც ნიშანი ჩანს (ძველი წესით — „ხელმოწერილია“), მაგრამ მკითხველი
    პირდაპირ ამბობს, რომ თანამშრომლის ადგილი ცარიელია."""
    hired(world)
    marks = {"signature_hint": "present"}
    add_scan(world, [(form_text(signed=False), dict(marks, signature_parties={"employee": False}))], doc_id="doc_no", job="job_no",
             type_id="employee_form")
    add_scan(world, [(form_text(signed=False), dict(marks, signature_parties={"employee": True}))], doc_id="doc_yes", job="job_yes",
             type_id="employee_form")
    add_scan(world, [(form_text(signed=False), marks)], doc_id="doc_old", job="job_old", type_id="employee_form")
    sweep(world)

    def unsigned(doc_id: str) -> bool:
        return any(c["field"] == "signature" and c["blocking"] for c in validation(world, doc_id)["checks"])
    assert unsigned("doc_no") and not unsigned("doc_yes") and not unsigned("doc_old")
