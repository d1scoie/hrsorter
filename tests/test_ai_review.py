"""AI ანალიზი (ai_review.py): პრომპტი, პასუხის დამუშავება, Claude Code-ის გამოძახება, ჩართვის პირობა."""
from __future__ import annotations

import json
import subprocess

import pytest

from hrsorter import ai_review as ar
from hrsorter.config import make_config
from hrsorter.db import Repository

AI_ON = {"ocr": {"online": {"enabled": True, "provider": "claude_code"}}, "mhr": {"enabled": True}}


def _cfg(tmp_path, **over):
    return make_config({"paths": {"root": str(tmp_path)}, **over})


def test_enabled_requires_online_claude_code_and_switch(tmp_path):
    assert ar.enabled(_cfg(tmp_path, **AI_ON))
    assert not ar.enabled(_cfg(tmp_path))                                                  # ონლაინ გამორთულია
    assert not ar.enabled(_cfg(tmp_path, ocr={"online": {"enabled": True, "provider": "anthropic"}}))
    assert not ar.enabled(_cfg(tmp_path, ocr=AI_ON["ocr"], mhr={"ai_review": {"enabled": False}}))


def test_build_prompt_contains_data_and_truncates():
    doc = {"type_ka": "შრომითი ხელშეკრულება", "page_count": 2, "fields": {"salary": "2000"}, "signature_status": "present", "reasons": []}
    val = {"status": "failed", "employee": {"employee_id": "E1", "first_name": "ნინო", "last_name": "ბერიძე", "raw": {"x": 1}},
           "checks": [{"field": "salary", "label_ka": "ხელფასი", "document_value": "2000", "mhr_value": "2200", "status": "mismatch", "blocking": True}],
           "diffs": [{"page": 2, "kind": "changed", "original": "2200", "scanned": "2000", "significant": True, "reason": "ციფრი"},
                     {"page": 1, "kind": "changed", "original": "შპს", "scanned": "შპს.", "significant": False}],
           "summary": ["ხელფასი განსხვავდება"]}
    pages = [{"text": "ხელშეკრულება №5 " + "ა" * 300, "signature_parties": {"დამსაქმებელი": True, "დასაქმებული": False},
              "ocr_quality": 0.9}, {"text": "მეორე გვერდი", "handwriting_suspected": True}]
    p = ar.build_prompt(doc, val, pages)
    for s in ("შრომითი ხელშეკრულება", "ნინო", "ხელფასი", "'2000' / '2200' / mismatch", "«2200» → დასკანერებული «2000»", "«შპს» → «შპს.»",
              "დასაქმებული — აკლია", "ხელნაწერის ეჭვი", "მეორე გვერდი", "იგნორირება", '"verdict"'):
        assert s in p, s
    assert '"raw"' not in p and "შემოკლებულია" not in p
    short = ar.build_prompt(doc, val, pages, max_chars=250)
    assert "ტექსტი შემოკლებულია" in short and "მეორე გვერდი" not in short


@pytest.mark.parametrize("text", [
    '{"verdict": "upload", "confidence": "high", "summary": "კარგია", "items": [], "risks": []}',
    '```json\n{"verdict": "upload", "confidence": "high", "summary": "კარგია"}\n```',
    'აი პასუხი:\n{"verdict": "upload", "confidence": "high", "summary": "კარგია"}\nმადლობა',
])
def test_parse_response_tolerates_fences_and_text(text):
    assert ar.parse_response(text)["verdict"] == "upload"


@pytest.mark.parametrize("text", ["", "არა JSON", '{"verdict": "maybe"}', '{"a": 1}'])
def test_parse_response_rejects_bad(text):
    with pytest.raises(ar.AiReviewError):
        ar.parse_response(text)


def _claude_json(result: str, ok: bool = True) -> bytes:
    return json.dumps({"subtype": "success" if ok else "error", "is_error": not ok, "result": result,
                       "total_cost_usd": 0.1}).encode("utf-8")


class FakeRun:
    def __init__(self, stdout: bytes = b"", returncode: int = 0, exc: BaseException | None = None):
        self.stdout, self.returncode, self.exc, self.calls = stdout, returncode, exc, []

    def __call__(self, args, **kw):
        self.calls.append((args, kw))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, b"boom")


def test_run_claude_passes_prompt_on_stdin_without_tools():
    run = FakeRun(_claude_json('{"verdict":"check"}'))
    out = ar.run_claude("პრომპტი", model="opus", timeout=5, cmd="claude.exe", runner=run)
    assert out == '{"verdict":"check"}'
    args, kw = run.calls[0]
    assert args[:2] == ["claude.exe", "-p"] and "--tools" in args and args[args.index("--tools") + 1] == "" and "--safe-mode" in args
    assert args[args.index("--model") + 1] == "opus" and kw["input"] == "პრომპტი".encode("utf-8") and kw["timeout"] == 5


@pytest.mark.parametrize("run, msg", [
    (FakeRun(returncode=1), "exit 1"),
    (FakeRun(exc=subprocess.TimeoutExpired("claude", 5)), "timeout"),
    (FakeRun(b"not json"), "non-JSON"),
    (FakeRun(_claude_json("x", ok=False)), "claude error"),
])
def test_run_claude_errors(run, msg):
    with pytest.raises(ar.AiReviewError, match=msg):
        ar.run_claude("p", model="opus", timeout=5, cmd="claude.exe", runner=run)


def _seed(tmp_path, cfg, doc_id="doc_1"):
    repo = Repository(cfg.paths.db)
    repo.save_page_text("job_1", 0, {"page_index": 0, "text": "შრომითი ხელშეკრულება ნინო ბერიძე 01001000001", "is_blank": False})
    repo.upsert_document({"id": doc_id, "job_id": "job_1", "source_id": "src_1", "page_start": 0, "page_end": 0, "status": "proposed",
                          "type_id": "contract", "personal_number": "01001000001", "output_path": str(tmp_path / "x.pdf"),
                          "metadata": json.dumps({"fields": {"salary": {"value": "2000"}}, "signature_status": "present"})})
    repo.mhr_enqueue(doc_id, "01001000001")
    repo.mhr_transition(doc_id, ("pending",), "failed", validation={"status": "failed", "employee": {"first_name": "ნინო"},
                                                                     "checks": [], "diffs": [], "summary": ["ხელფასი განსხვავდება"]})
    return repo


def test_review_document_builds_prompt_from_db_and_normalizes(tmp_path):
    cfg = _cfg(tmp_path, **AI_ON)
    repo = _seed(tmp_path, cfg)
    run = FakeRun(_claude_json(json.dumps({"verdict": "upload", "confidence": "high", "summary": "დოკუმენტი სწორია.",
                                           "items": [{"topic": "ხელფასი", "status": "ok", "explanation": "ციფრი სწორად იკითხება"},
                                                     {"topic": "x", "status": "weird"}, "garbage"],
                                           "risks": ["არაფერი", ""]}, ensure_ascii=False)))
    res = ar.review_document(repo, cfg, "doc_1", runner=run)
    assert res.verdict == "upload" and res.confidence == "high" and res.model == "opus" and res.reviewed_at and not res.error
    assert res.items == [{"topic": "ხელფასი", "status": "ok", "explanation": "ციფრი სწორად იკითხება"},
                         {"topic": "x", "status": "warn", "explanation": ""}]
    assert res.risks == ["არაფერი"]
    prompt = run.calls[0][1]["input"].decode("utf-8")
    assert "ნინო ბერიძე 01001000001" in prompt and "ხელფასი განსხვავდება" in prompt and '"salary": "2000"' in prompt
    assert "შრომითი ხელშეკრულება" in prompt   # type name_ka from types file
    repo.close()


def test_review_document_errors_are_returned_not_raised(tmp_path):
    cfg = _cfg(tmp_path, **AI_ON)
    repo = _seed(tmp_path, cfg)
    res = ar.review_document(repo, cfg, "doc_1", runner=FakeRun(returncode=2))
    assert res.verdict == "error" and "exit 2" in (res.error or "")
    res = ar.review_document(repo, cfg, "doc_1", runner=FakeRun(_claude_json("ეს არ არის JSON")))
    assert res.verdict == "error" and res.error == "non-JSON response"
    assert ar.review_document(repo, cfg, "doc_missing", runner=FakeRun()).error == "document_not_found"
    repo.close()


def test_run_claude_retries_without_safe_mode_on_old_cli():
    calls = []

    def run(args, **kw):
        calls.append(args)
        if "--safe-mode" in args:
            return subprocess.CompletedProcess(args, 1, b"", b"error: unknown option '--safe-mode'\n")
        return subprocess.CompletedProcess(args, 0, _claude_json('{"verdict":"check"}'), b"")
    assert ar.run_claude("p", model="opus", timeout=5, cmd="claude.exe", runner=run) == '{"verdict":"check"}'
    assert len(calls) == 2 and "--safe-mode" not in calls[1] and "--tools" in calls[1]
    other = FakeRun(returncode=1)   # სხვა შეცდომა — არ მეორდება
    with pytest.raises(ar.AiReviewError):
        ar.run_claude("p", model="opus", timeout=5, cmd="claude.exe", runner=other)
    assert len(other.calls) == 1
