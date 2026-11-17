"""„მხოლოდ AI“ რეჟიმი (`mhr.code_check_stops=false`, ნაგულისხმევი): ფაილს AI ანალიზი აჩერებს და არა წესების შენიშვნები.
წესების შედარება ისევ სრულდება და AI-ს გადაეცემა; AI თუ არ არის ან ვერ შესრულდა, წესების შედეგი მოქმედებს."""
from __future__ import annotations

import json

import pytest

from hrsorter import mhr_queue as q
from hrsorter.ai_review import AiReview, finding_key
from hrsorter.mhr_models import Check, MhrEmployee, TextDiff, ValidationResult
from tests.test_mhr_queue import AI_ON, add_doc, ai, cfg, mhr, repo, status, uploads  # noqa: F401  (fixtures)

SALARY = Check("salary", "ხელფასი", "1500", "1300.00", "mismatch", True)
MISSING = Check("original_contract", "ორიგინალი MHR-ში", None, None, "missing_in_mhr", True, note="MHR-ში ვერ მოიძებნა")
DIFF = TextDiff(page=1, kind="changed", original="2100 ლარს", scanned="2100(ლარს", significant=True, reason="ციფრები განსხვავდება")


class Validate:
    """წესების შედეგი: `stops` — რა აჩერებს (Check ან TextDiff); ცარიელი → passed."""

    def __init__(self, *stops):
        self.stops = stops

    def __call__(self, repo, cfg, doc_id, client) -> ValidationResult:
        checks = [s for s in self.stops if isinstance(s, Check)]
        diffs = [s for s in self.stops if isinstance(s, TextDiff)]
        return ValidationResult(status="failed" if self.stops else "passed", checks=checks, diffs=diffs,
                                employee=MhrEmployee("E001", personal_number="01001000001"), summary=["x"])


class Review:
    """AI-ის პასუხი: `verdict` და თითო გაჩერების მიზეზზე სტატუსი (`findings`: რიგით ok/warn/bad)."""

    def __init__(self, verdict="upload", findings=(), error=None):
        self.verdict, self.findings, self.error, self.calls = verdict, findings, error, 0

    def __call__(self, repo, cfg, doc_id):
        from hrsorter.ai_review import stop_reasons
        self.calls += 1
        if self.error:
            return AiReview("error", error=self.error)
        stops = stop_reasons(json.loads(repo.mhr_get(doc_id)["validation"]))
        fs = [{"key": finding_key(x), "status": st, "explanation": "ახსნა"} for x, st in zip(stops, self.findings)]
        return AiReview(self.verdict, "high", "შეჯამება", [{"topic": "თარიღი", "status": "warn", "explanation": "ნახე"}], findings=fs)


@pytest.fixture
def ai_cfg(cfg):
    cfg.data["ocr"]["online"].update(AI_ON["ocr"]["online"])
    assert cfg.data["mhr"]["code_check_stops"] is False        # ნაგულისხმევი: მხოლოდ AI
    return cfg


def run(repo, cfg, mhr, tmp_path, validate, review, times=1):
    add_doc(repo, tmp_path)
    for _ in range(times):
        q.sweep(repo, cfg, mhr, validate, review_fn=review)


def test_ai_decides_only_when_enabled_and_code_stop_is_off(cfg):
    assert not q.ai_decides(cfg)                               # AI (ონლაინ სერვისი) გამორთულია → წესები
    cfg.data["ocr"]["online"].update(AI_ON["ocr"]["online"])
    assert q.ai_decides(cfg)
    cfg.data["mhr"]["code_check_stops"] = True
    assert not q.ai_decides(cfg)


def test_code_remarks_that_ai_calls_false_alarms_do_not_stop_the_file(repo, ai_cfg, mhr, tmp_path):
    run(repo, ai_cfg, mhr, tmp_path, Validate(SALARY, DIFF), Review("upload", ("ok", "ok")))
    assert status(repo) == "ready" and ai(repo)["gate"] == "lifted"
    assert json.loads(repo.mhr_get("doc_1")["validation"])["status"] == "failed"   # წესების შედეგი ინახება, როგორც იყო


@pytest.mark.parametrize("verdict, findings", [("upload", ("ok", "warn")), ("upload", ("ok",)), ("check", ("ok", "ok")),
                                               ("reject", ("ok", "ok"))])
def test_file_stays_stopped_unless_ai_clears_every_reason(repo, ai_cfg, mhr, tmp_path, verdict, findings):
    run(repo, ai_cfg, mhr, tmp_path, Validate(SALARY, DIFF), Review(verdict, findings))
    assert status(repo) == "failed" and ai(repo).get("gate") is None


def test_ai_cannot_lift_a_fact(repo, ai_cfg, mhr, tmp_path):
    """MHR-ში ორიგინალი არ არის — ეს წაკითხვის საკითხი არ არის, AI-ის „ატვირთე“ მას ვერ ხსნის."""
    run(repo, ai_cfg, mhr, tmp_path, Validate(MISSING), Review("upload", ("ok",)))
    assert status(repo) == "failed"


def test_ai_doubt_stops_a_file_that_passed_the_code_check(repo, ai_cfg, mhr, tmp_path):
    run(repo, ai_cfg, mhr, tmp_path, Validate(), Review("check"))
    assert status(repo) == "failed" and ai(repo)["gate"] == "stopped"
    assert q.approve_override(repo, "doc_1")                   # ოპერატორმა ქაღალდი ნახა — „ატვირთვა მაინც“
    assert status(repo) == "approved"


def test_ai_ok_keeps_a_passed_file_ready(repo, ai_cfg, mhr, tmp_path):
    run(repo, ai_cfg, mhr, tmp_path, Validate(), Review("upload"))
    assert status(repo) == "ready" and ai(repo).get("gate") is None


@pytest.mark.parametrize("validate, expect", [(Validate(), "ready"), (Validate(SALARY), "failed")])
def test_failed_ai_leaves_the_code_result(repo, ai_cfg, mhr, tmp_path, validate, expect):
    run(repo, ai_cfg, mhr, tmp_path, validate, Review(error="timeout"))
    assert status(repo) == expect and ai(repo)["verdict"] == "error"


def test_old_mode_ai_only_advises(repo, ai_cfg, mhr, tmp_path):
    ai_cfg.data["mhr"]["code_check_stops"] = True
    run(repo, ai_cfg, mhr, tmp_path, Validate(SALARY), Review("upload", ("ok",)))
    assert status(repo) == "failed" and ai(repo).get("gate") is None


def test_new_ai_answer_can_stop_a_lifted_file_again(repo, ai_cfg, mhr, tmp_path):
    run(repo, ai_cfg, mhr, tmp_path, Validate(SALARY), Review("upload", ("ok",)))
    assert status(repo) == "ready"
    assert q.rerun_ai_review(repo, "doc_1")
    q.sweep(repo, ai_cfg, mhr, Validate(SALARY), review_fn=Review("check", ("warn",)))
    assert status(repo) == "failed" and ai(repo).get("gate") is None


def test_same_result_on_recheck_keeps_the_ai_decision(repo, ai_cfg, mhr, tmp_path):
    rv = Review("upload", ("ok",))
    run(repo, ai_cfg, mhr, tmp_path, Validate(SALARY), rv)
    assert status(repo) == "ready"
    repo.mhr_transition("doc_1", ("ready",), "pending")        # თავისით გადამოწმება (მაგ. MHR-ში რაღაც შეიცვალა)
    q.sweep(repo, ai_cfg, mhr, Validate(SALARY), review_fn=rv)
    assert status(repo) == "ready" and rv.calls == 1           # AI მეორედ არ გაშვებულა, გადაწყვეტილება ძალაშია


def test_switching_the_setting_moves_already_checked_files(repo, ai_cfg, mhr, tmp_path):
    ai_cfg.data["mhr"]["code_check_stops"] = True               # ძველი რეჟიმი: წესებმა გააჩერა, AI-მ თქვა „ცრუ განგაშია“
    rv = Review("upload", ("ok",))
    run(repo, ai_cfg, mhr, tmp_path, Validate(SALARY), rv)
    assert status(repo) == "failed"
    ai_cfg.data["mhr"]["code_check_stops"] = False              # „მხოლოდ AI“ ჩაირთო — AI-ის ძველი პასუხი საკმარისია
    q.sweep(repo, ai_cfg, mhr, Validate(SALARY), review_fn=rv)
    assert status(repo) == "ready" and ai(repo)["gate"] == "lifted" and rv.calls == 1
    ai_cfg.data["mhr"]["code_check_stops"] = True               # უკან: ისევ წესების შედეგი
    q.sweep(repo, ai_cfg, mhr, Validate(SALARY), review_fn=rv)
    assert status(repo) == "failed" and ai(repo)["gate"] is None


def test_without_approval_upload_waits_for_ai(repo, ai_cfg, mhr, tmp_path):
    ai_cfg.data["mhr"]["require_approval"] = False
    add_doc(repo, tmp_path)
    q.sweep(repo, ai_cfg, mhr, Validate(), review_fn=Review("upload"))
    assert status(repo) == "uploaded" and repo.mhr_get("doc_1")["decided_by"] == "auto:ai" and len(uploads(mhr)) == 1


def test_without_approval_ai_doubt_blocks_the_upload(repo, ai_cfg, mhr, tmp_path):
    ai_cfg.data["mhr"]["require_approval"] = False
    run(repo, ai_cfg, mhr, tmp_path, Validate(), Review("check"), times=2)
    assert status(repo) == "failed" and uploads(mhr) == []
