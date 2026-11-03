import json
import subprocess
import sys

import pytest
from PIL import Image

from hrsorter.config import make_config
from hrsorter.ocr import (DailyBudget, OcrError, OcrResult, OnlineVisionProvider, TesseractProvider,
                          build_ocr_chain, gate_open, parse_osd, parse_tsv)
from hrsorter.ocr.chain import OcrChain

TSV = """level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext
1\t1\t0\t0\t0\t0\t0\t0\t2480\t3508\t-1\t
2\t1\t1\t0\t0\t0\t200\t300\t900\t60\t-1\t
3\t1\t1\t1\t0\t0\t200\t300\t900\t60\t-1\t
4\t1\t1\t1\t1\t0\t200\t300\t900\t60\t-1\t
5\t1\t1\t1\t1\t1\t200\t300\t300\t60\t96.5\tბრძანება
5\t1\t1\t1\t1\t2\t520\t300\t100\t60\t88.0\t№
5\t1\t1\t1\t1\t3\t640\t300\t200\t60\t70\t15/2024
4\t1\t1\t1\t2\t0\t200\t400\t900\t60\t-1\t
5\t1\t1\t1\t2\t1\t200\t400\t400\t60\t91\tშვებულების
5\t1\t1\t1\t2\t2\t620\t400\t300\t60\t85\tშესახებ
5\t1\t1\t1\t2\t3\t940\t400\t20\t60\t-1\t
2\t1\t2\t0\t0\t0\t200\t900\t900\t60\t-1\t
5\t1\t2\t1\t1\t1\t200\t900\t300\t60\t60\tდირექტორი
"""


def test_parse_tsv_text_lines_and_confidence():
    res = parse_tsv(TSV)
    assert res.text.splitlines()[0] == "ბრძანება № 15/2024"
    assert "შვებულების შესახებ" in res.text
    assert res.text.rstrip().endswith("დირექტორი")
    assert "\n\n" in res.text                             # ახალი ბლოკი → ცარიელი ხაზი
    assert len(res.words) == 6                            # conf=-1 ცარიელი ტოკენი იგნორირდება
    assert res.words[0]["bbox"] == (200, 300, 500, 360)
    assert res.words[0]["conf"] == pytest.approx(0.965)
    assert res.mean_confidence == pytest.approx((96.5 + 88 + 70 + 91 + 85 + 60) / 6 / 100)


def test_parse_tsv_empty_and_garbage():
    assert parse_tsv("") == OcrResult(text="", mean_confidence=None, words=[])
    assert parse_tsv("not\ta\ttsv\n").text == ""


def test_parse_osd():
    out = "Page number: 0\nOrientation in degrees: 270\nRotate: 90\nOrientation confidence: 12.3\nScript: Georgian\n"
    assert parse_osd(out) == 90
    assert parse_osd("Rotate: 0") == 0
    assert parse_osd("no osd here") is None
    assert parse_osd("Rotate: 45") is None


class FakeRun:
    """ყალბი subprocess.run: list-langs / version / tsv / psm 0 ბრძანებებს პასუხობს."""

    def __init__(self, langs="kat\neng\nosd\n", tsv=TSV, osd="Rotate: 180\n", timeout=False, rc=0):
        self.langs, self.tsv, self.osd, self.timeout, self.rc = langs, tsv, osd, timeout, rc
        self.calls: list[list[str]] = []

    def __call__(self, args, **kw):
        self.calls.append(args)
        if self.timeout:
            raise subprocess.TimeoutExpired(args, kw.get("timeout", 0))
        if "--list-langs" in args:
            return subprocess.CompletedProcess(args, 0, b"List of available languages (3):\n" + self.langs.encode(), b"")
        if "--version" in args:
            return subprocess.CompletedProcess(args, 0, b"tesseract 5.3.3\n leptonica-1.83", b"")
        if "--psm" in args and args[args.index("--psm") + 1] == "0":
            return subprocess.CompletedProcess(args, self.rc, self.osd.encode(), b"")
        return subprocess.CompletedProcess(args, self.rc, self.tsv.encode("utf-8"), b"")


@pytest.fixture
def img():
    return Image.new("L", (200, 100), 255)


def test_tesseract_unavailable_without_binary(monkeypatch):
    monkeypatch.setattr("hrsorter.ocr.tesseract.shutil.which", lambda name: None)
    p = TesseractProvider(cmd="", lang="kat+eng")
    assert p.available() is False
    assert p.model_version == ""
    assert p.osd(Image.new("L", (10, 10), 255)) is None
    assert TesseractProvider(cmd="C:/does/not/exist/tesseract.exe").available() is False


def test_tesseract_available_requires_all_langs():
    assert TesseractProvider(cmd=sys.executable, lang="kat+eng", runner=FakeRun()).available() is True
    assert TesseractProvider(cmd=sys.executable, lang="kat+eng", runner=FakeRun(langs="eng\n")).available() is False
    p = TesseractProvider(cmd=sys.executable, lang="kat+eng", runner=FakeRun(langs="kat\neng\n"))
    assert p.available() is True and p.has_osd() is False


def test_tesseract_recognize_and_osd_via_fake_subprocess(img):
    run = FakeRun()
    p = TesseractProvider(cmd=sys.executable, lang="kat+eng", psm=6, runner=run)
    res = p.recognize(img, "kat+eng")
    assert res.text.startswith("ბრძანება № 15/2024")
    assert res.mean_confidence is not None and 0 < res.mean_confidence <= 1
    rec_call = [c for c in run.calls if "tessedit_create_tsv=1" in c][0]
    assert rec_call[1] == "stdin"   # სურათი stdin-ით — არა-ლათინური ბილიკების გამო
    assert rec_call[0] == sys.executable and "-l" in rec_call and "kat+eng" in rec_call
    assert rec_call[rec_call.index("--psm") + 1] == "6"
    assert p.osd(img) == 180
    assert p.model_version == "tesseract 5.3.3"


def test_tesseract_timeout_is_retryable_ocr_error(img):
    p = TesseractProvider(cmd=sys.executable, timeout_seconds=1, runner=FakeRun(timeout=True))
    with pytest.raises(OcrError) as ei:
        p.recognize(img, "kat")
    assert ei.value.retryable is True


def test_tesseract_nonzero_exit_raises(img):
    p = TesseractProvider(cmd=sys.executable, runner=FakeRun(rc=1))
    with pytest.raises(OcrError):
        p.recognize(img, "kat")


# --- ონლაინ კარიბჭე -------------------------------------------------------------------
def test_online_gate_closed_by_default():
    cfg = make_config()
    chain = build_ocr_chain(cfg)
    assert chain.online is None
    d = chain.describe()
    assert d["sends_outside_machine"] is False and d["external_send_mode"] == "none"
    assert d["online_gate_open"] is False
    assert gate_open(cfg.data["ocr"]) is False


def test_online_gate_needs_both_enabled_and_path():
    only_enabled = make_config({"ocr": {"online": {"enabled": True, "provider": "x"}}})
    assert build_ocr_chain(only_enabled).online is None
    only_path = make_config({"ocr": {"path": "hybrid"}})
    assert build_ocr_chain(only_path).online is None
    both = make_config({"ocr": {"path": "hybrid", "online": {"enabled": True, "provider": "x"}}})
    assert build_ocr_chain(both).online is not None


def test_online_provider_requires_secret_and_never_sends_without_it(img):
    ocr = make_config({"ocr": {"path": "online", "online": {"enabled": True, "provider": "x", "model": "m1"}}}).data["ocr"]
    p = OnlineVisionProvider(ocr, secret_getter=lambda svc, name: None)
    assert p.available() is False and p.is_online is True
    with pytest.raises(OcrError):
        p.recognize(img, "kat")
    p2 = OnlineVisionProvider(ocr, secret_getter=lambda svc, name: "k")
    assert p2.available() is True and p2.model_version == "m1"
    with pytest.raises(NotImplementedError):
        p2.recognize(img, "kat")


def test_online_provider_disabled_rejects_even_with_secret(img):
    ocr = make_config().data["ocr"]
    p = OnlineVisionProvider(ocr, secret_getter=lambda svc, name: "k")
    assert p.available() is False
    with pytest.raises(OcrError):
        p.recognize(img, "kat")


def test_online_budget_and_retries(img):
    ocr = make_config({"ocr": {"path": "online", "online": {"enabled": True, "provider": "x", "max_retries": 2,
                                                             "max_pages_per_day": 1}}}).data["ocr"]
    calls = {"n": 0}

    class Flaky(OnlineVisionProvider):
        def _call(self, png, lang, timeout, secret):
            calls["n"] += 1
            assert secret == "k"
            if calls["n"] < 3:
                raise OcrError("temporary", retryable=True)
            return OcrResult(text="ბრძანება შვებულების შესახებ", mean_confidence=0.9)

    sleeps: list[float] = []
    p = Flaky(ocr, secret_getter=lambda s, n: "k", sleep=sleeps.append)
    assert p.recognize(img, "kat").text.startswith("ბრძანება")
    assert calls["n"] == 3 and len(sleeps) == 2
    with pytest.raises(OcrError):            # ბიუჯეტი 1 გვერდი/დღე ამოიწურა
        p.recognize(img, "kat")
    assert p.budget.used == 1

    b = DailyBudget(max_pages_per_day=0)
    assert all(b.try_consume() for _ in range(50))


# --- ჯაჭვი ---------------------------------------------------------------------------
class FakeProvider:
    is_online = False
    model_version = "fake-1"

    def __init__(self, name, text, conf=0.9, avail=True):
        self.name, self.text, self.conf, self.avail = name, text, conf, avail
        self.calls = 0

    def available(self):
        return self.avail

    def recognize(self, image, lang):
        self.calls += 1
        return OcrResult(text=self.text, mean_confidence=self.conf, words=[])


GOOD = "ბრძანება სამსახურში მიღების შესახებ ნინო ბერიძე მიღებულ იქნეს მენეჯერის თანამდებობაზე"


def test_chain_hybrid_uses_online_only_for_low_quality(img):
    local_bad = FakeProvider("tess", "\ue000\ue001 \ue002\ue003 \ue004", conf=0.1)
    online = FakeProvider("online", GOOD)
    online.is_online = True
    chain = OcrChain(local_bad, online, "hybrid", min_quality=0.35, lang="kat")
    r = chain.recognize(img)
    assert r.provider is online and r.quality >= 0.35 and online.calls == 1

    local_good = FakeProvider("tess", GOOD)
    online2 = FakeProvider("online", GOOD)
    online2.is_online = True
    chain2 = OcrChain(local_good, online2, "hybrid", min_quality=0.35, lang="kat")
    r2 = chain2.recognize(img)
    assert r2.provider is local_good and online2.calls == 0


def test_chain_local_only_never_touches_online(img):
    online = FakeProvider("online", GOOD)
    online.is_online = True
    chain = OcrChain(FakeProvider("tess", "", conf=None), online, "local", min_quality=0.35, lang="kat")
    r = chain.recognize(img)
    assert online.calls == 0 and r.provider is not online
    assert "low_ocr_quality" in r.warnings


def test_chain_unavailable():
    chain = OcrChain(FakeProvider("tess", GOOD, avail=False), None, "local", 0.35, "kat")
    assert chain.available() is False
    r = chain.recognize(Image.new("L", (10, 10), 255))
    assert r.ok is False and r.attempts[0]["error"] == "unavailable"


def test_build_chain_local_provider_none():
    cfg = make_config({"ocr": {"local_provider": "none"}})
    chain = build_ocr_chain(cfg)
    assert chain.local is None and chain.available() is False
    assert chain.describe()["local"] is None


def test_quality_uses_share_of_confident_words():
    # მკრთალ სკანზე ნაგავიც ქართული ასოებისგან შედგება — ხარისხს სიტყვების სანდოობა განსაზღვრავს
    from hrsorter.ocr.base import OcrResult
    from hrsorter.ocr.chain import result_quality
    words = "ხელშეკრულება გამოსაცდელი ვადით შრომითი ურთიერთობის შესახებ".split()
    good = OcrResult(" ".join(words), 0.95, [{"text": w, "conf": 0.95, "bbox": (0, 0, 1, 1)} for w in words])
    faint = OcrResult(" ".join(words), 0.55, [{"text": w, "conf": c, "bbox": (0, 0, 1, 1)}
                                               for w, c in zip(words, [0.9, 0.4, 0.5, 0.3, 0.6, 0.85])])
    assert result_quality(good) > 0.9
    assert result_quality(faint) < 0.35


# --- Claude Code (გამოწერით) პროვაიდერი --------------------------------------------------
def _cc_cfg(**online):
    base = {"enabled": True, "provider": "claude_code", "timeout_seconds": 5}
    base.update(online)
    return make_config({"ocr": {"path": "online", "online": base}}).data["ocr"]


class FakeClaude:
    def __init__(self, payload=None, rc=0, timeout=False, stdout=None):
        self.payload, self.rc, self.timeout, self.stdout = payload, rc, timeout, stdout
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, args, **kw):
        self.calls.append((args, kw))
        if self.timeout:
            raise subprocess.TimeoutExpired(args, kw.get("timeout", 0))
        out = self.stdout if self.stdout is not None else json.dumps(self.payload).encode()
        return subprocess.CompletedProcess(args, self.rc, out, b"boom")


def test_claude_code_unavailable_without_binary(monkeypatch, img):
    from hrsorter.ocr.claude_code_vision import ClaudeCodeVisionProvider
    monkeypatch.setattr("hrsorter.ocr.claude_code_vision.shutil.which", lambda _n: None)
    p = ClaudeCodeVisionProvider(_cc_cfg())
    assert p.available() is False and p.is_online is True
    with pytest.raises(OcrError):
        p.recognize(img, "kat")


def test_claude_code_needs_no_secret_and_parses_result(img):
    from hrsorter.ocr.claude_code_vision import ClaudeCodeVisionProvider, usage_totals, reset_usage
    reset_usage()
    run = FakeClaude({"is_error": False, "subtype": "success", "result": "ბრძანება № 15/2024", "total_cost_usd": 0.12})
    p = ClaudeCodeVisionProvider(_cc_cfg(claude_cmd=sys.executable, model="opus"), runner=run,
                                 secret_getter=lambda s, n: None)
    assert p.available() is True
    res = p.recognize(img, "kat")
    assert res.text == "ბრძანება № 15/2024" and res.mean_confidence is None
    args, kw = run.calls[0]
    assert args[0] == sys.executable and "-p" in args and args[args.index("--model") + 1] == "opus"
    assert args[args.index("--allowedTools") + 1] == "Read" and "--output-format" in args and "--safe-mode" in args
    assert kw["cwd"] and kw["timeout"] == 5.0
    assert usage_totals == {"pages": 1, "cost_usd": 0.12}


def test_claude_code_errors(img):
    from hrsorter.ocr.claude_code_vision import ClaudeCodeVisionProvider
    cfg = _cc_cfg(claude_cmd=sys.executable, max_retries=0)
    with pytest.raises(OcrError) as ei:
        ClaudeCodeVisionProvider(cfg, runner=FakeClaude(timeout=True)).recognize(img, "kat")
    assert ei.value.retryable is True
    with pytest.raises(OcrError, match="exit 1"):
        ClaudeCodeVisionProvider(cfg, runner=FakeClaude({}, rc=1)).recognize(img, "kat")
    with pytest.raises(OcrError, match="claude error"):
        ClaudeCodeVisionProvider(cfg, runner=FakeClaude({"is_error": True, "result": "rate limit"})).recognize(img, "kat")
    with pytest.raises(OcrError, match="non-JSON"):
        ClaudeCodeVisionProvider(cfg, runner=FakeClaude(stdout=b"nope")).recognize(img, "kat")


def test_claude_code_respects_gate_and_chain():
    from hrsorter.ocr.claude_code_vision import ClaudeCodeVisionProvider
    off = make_config({"ocr": {"online": {"enabled": True, "provider": "claude_code"}}})
    assert build_ocr_chain(off).online is None
    on = make_config({"ocr": {"path": "hybrid", "online": {"enabled": True, "provider": "claude_code"}}})
    assert isinstance(build_ocr_chain(on).online, ClaudeCodeVisionProvider)


def test_claude_code_auth_status_and_login_launch(monkeypatch):
    from hrsorter.ocr import claude_code_vision as cc
    monkeypatch.setattr(cc.shutil, "which", lambda _n: None)
    assert cc.auth_status()["installed"] is False and cc.launch_login() is False
    ok = FakeClaude({"loggedIn": True, "email": "x@y.ge", "subscriptionType": "max"})
    st = cc.auth_status(sys.executable, runner=ok)
    assert st == {"installed": True, "logged_in": True, "email": "x@y.ge", "subscription": "max", "error": ""}
    assert ok.calls[0][0][1:] == ["auth", "status", "--json"]
    st = cc.auth_status(sys.executable, runner=FakeClaude(stdout=b"garbage"))
    assert st["installed"] is True and st["logged_in"] is False and st["error"]
    st = cc.auth_status(sys.executable, runner=FakeClaude(timeout=True))
    assert st["logged_in"] is False and st["error"] == "TimeoutExpired"
    spawned = []
    assert cc.launch_login(sys.executable, popen=lambda args, **kw: spawned.append(args)) is True
    assert spawned[0][1:] == ["auth", "login", "--claudeai"]


def test_claude_code_recognize_printed_uses_recheck_prompt(img):
    """ხელახალი წაკითხვა (validation._recheck_diffs): ინსტრუქცია ბეჭდებს/ხელმოწერებს საერთოდ გამოტოვებს — რეალურ სკანზე
    ჩვეულებრივი OCR-ინსტრუქციით მოდელი „[ბეჭედი: შპს არბო 2009 …]"-ს წერდა და ის ორიგინალთან შედარებაში ჩანდა."""
    from hrsorter.ocr.claude_code_vision import PROMPT, RECHECK_PROMPT, ClaudeCodeVisionProvider
    run = FakeClaude({"is_error": False, "subtype": "success", "result": "მუხლი 5", "total_cost_usd": 0.1})
    p = ClaudeCodeVisionProvider(_cc_cfg(claude_cmd=sys.executable), runner=run, secret_getter=lambda s, n: None)
    assert p.recognize_printed(img, "kat").text == "მუხლი 5"
    sent = run.calls[0][0][2]
    assert "omit" in sent and "stamps" in sent and sent != PROMPT.format(path="x") and RECHECK_PROMPT.split("{path}")[0] in sent
    p.recognize(img, "kat")
    assert "Mark handwritten" in run.calls[1][0][2]   # ჩვეულებრივი OCR უცვლელია
