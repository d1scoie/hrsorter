"""ორი რამ, რაც ნამდვილ კომპიუტერზე თავიდან დამუშავებას აჩერებდა: (1) ქსელური საცავი გრძელ ბილიკზე არსებულ ფაილს ვერ
ანაცვლებს; (2) Task Scheduler-იდან გაშვებული პროგრამა დაბალი პრიორიტეტით მუშაობს და დატვირთულ კომპიუტერზე პროცესორს ვერ იღებს."""
from __future__ import annotations

import json
import os
import sys

import pytest

from hrsorter import cli, output


def _nas(monkeypatch):
    """ქსელური საცავის ქცევა: ახალ სახელზე გადატანა მუშაობს, არსებულის ჩანაცვლება — PermissionError (WinError 31)."""
    real = os.replace
    calls = []

    def replace(src, dst):
        calls.append((str(src), str(dst)))
        if os.path.exists(dst):
            raise PermissionError(13, "A device attached to the system is not functioning")
        return real(src, dst)
    monkeypatch.setattr(output.os, "replace", replace)
    return calls


def test_sidecar_is_rewritten_when_the_share_cannot_replace_an_existing_file(tmp_path, monkeypatch):
    side = tmp_path / "ნინო-ბერიძე_თანამდებობრივი-ინსტრუქცია.json"
    side.write_text(json.dumps({"processing_status": "proposed", "note": "ძველი და უფრო გრძელი შიგთავსი" * 20}, ensure_ascii=False),
                    encoding="utf-8")
    calls = _nas(monkeypatch)
    output._write_json_atomic(side, {"processing_status": "superseded"})
    assert json.loads(side.read_text(encoding="utf-8")) == {"processing_status": "superseded"}   # ძველის კუდი არ დარჩა
    assert calls and not side.with_name(side.name + ".tmp").exists()


def test_new_file_still_goes_through_plain_replace(tmp_path, monkeypatch):
    side = tmp_path / "new.json"
    _nas(monkeypatch)
    output._write_json_atomic(side, {"a": 1})
    assert json.loads(side.read_text(encoding="utf-8")) == {"a": 1} and not (tmp_path / "new.json.tmp").exists()


def test_error_without_an_existing_target_is_not_swallowed(tmp_path, monkeypatch):
    def denied(src, dst):
        raise PermissionError(13, "Access is denied")
    monkeypatch.setattr(output.os, "replace", denied)
    tmp = tmp_path / "x.tmp"
    tmp.write_text("x", encoding="utf-8")
    with pytest.raises(PermissionError):
        output._replace(tmp, tmp_path / "missing.json")


def test_pdf_bytes_survive_the_fallback(tmp_path, monkeypatch):
    dest, tmp = tmp_path / "doc.pdf", tmp_path / "doc.pdf.tmp"
    dest.write_bytes(b"%PDF-old" + b"0" * 5000)
    tmp.write_bytes(b"%PDF-new")
    _nas(monkeypatch)
    output._replace(tmp, dest)
    assert dest.read_bytes() == b"%PDF-new" and not tmp.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-ის პროცესის პრიორიტეტი")
def test_low_priority_process_returns_itself_to_normal():
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.GetPriorityClass.argtypes, k32.GetPriorityClass.restype = [wintypes.HANDLE], wintypes.DWORD
    k32.SetPriorityClass.argtypes, k32.SetPriorityClass.restype = [wintypes.HANDLE, wintypes.DWORD], wintypes.BOOL
    me = k32.GetCurrentProcess()
    before = k32.GetPriorityClass(me)
    try:
        assert k32.SetPriorityClass(me, 0x4000)                    # BELOW_NORMAL — ასე იწყებს Task Scheduler
        assert cli.normal_priority() is True and k32.GetPriorityClass(me) == 0x20
        assert cli.normal_priority() is False                      # უკვე ჩვეულებრივია — არაფერს ცვლის
        assert k32.SetPriorityClass(me, 0x8000)                    # ABOVE_NORMAL: ვინმემ განზრახ ასწია — არ ვეხებით
        assert cli.normal_priority() is False and k32.GetPriorityClass(me) == 0x8000
    finally:
        k32.SetPriorityClass(me, before)
