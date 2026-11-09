"""ავტოგანახლება — მხოლოდ ოფლაინ: GitHub API და ჩამოტვირთვა `httpx.MockTransport`-ით."""
from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET

import httpx
import pytest

from hrsorter import updater
from hrsorter.db import Repository

PAYLOAD = b"fake setup exe"
SHA = hashlib.sha256(PAYLOAD).hexdigest()
API = f"/repos/{updater.GITHUB_REPO}/releases/latest"


def _client(version="0.2.0", digest=f"sha256:{SHA}", status=200, hits=None):
    def handler(req: httpx.Request) -> httpx.Response:
        if hits is not None:
            hits.append(req.url.path)
        if req.url.path == API:
            if status != 200:
                return httpx.Response(status)
            return httpx.Response(200, json={"tag_name": f"v{version}", "assets": [
                {"name": "notes.txt", "digest": "sha256:00", "browser_download_url": "https://dl/notes.txt"},
                {"name": f"HRSorter-Setup-{version}.exe", "digest": digest,
                 "browser_download_url": f"https://dl/HRSorter-Setup-{version}.exe"},
            ]})
        return httpx.Response(200, content=PAYLOAD)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_parse_version_compares_numerically():
    assert updater.parse_version("v0.10.0") > updater.parse_version("0.9.9")


def test_latest_release_picks_setup_asset_and_handles_no_release():
    rel = updater.latest_release(_client())
    assert (rel.version, rel.name, rel.sha256) == ("0.2.0", "HRSorter-Setup-0.2.0.exe", SHA)
    assert updater.latest_release(_client(status=404)) is None
    assert updater.latest_release(_client(digest=None)) is None          # hash-ის გარეშე — არ ვაყენებთ


def test_newer_version_downloads_verifies_and_launches_silently(tmp_path):
    launched = []
    msg = updater.run(tmp_path / "none.db", client=_client(), current="0.1.0", app_dir=tmp_path,
                      launch=lambda s, log: launched.append((s, log)))
    setup = tmp_path / "_update" / "HRSorter-Setup-0.2.0.exe"
    assert "0.2.0" in msg and setup.read_bytes() == PAYLOAD
    assert launched == [(setup, tmp_path / "_update" / "setup.log")]


@pytest.mark.parametrize("current", ["0.2.0", "0.3.0"])
def test_same_or_older_release_does_nothing(tmp_path, current):
    hits = []
    updater.run(tmp_path / "none.db", client=_client(hits=hits), current=current, app_dir=tmp_path,
                launch=lambda *a: pytest.fail("must not launch"))
    assert hits == [API] and not (tmp_path / "_update").exists()


def test_failed_install_is_retried_even_though_exe_is_already_new(tmp_path):
    # ჩავარდნილი ინსტალაციის შემდეგ hrsorter.exe უკვე ახალია, installed-version.txt კი ძველ ვერსიას წერს
    (tmp_path / updater.INSTALLED_FILE).write_text("0.1.0", encoding="utf-8")
    launched = []
    msg = updater.run(tmp_path / "none.db", client=_client(), current="0.2.0", app_dir=tmp_path,
                      launch=lambda s, log: launched.append(s))
    assert launched == [tmp_path / "_update" / "HRSorter-Setup-0.2.0.exe"]
    assert "0.1.0 → 0.2.0" in msg and "თავიდან" in msg
    check = updater.run(None, check_only=True, client=_client(), current="0.2.0", app_dir=tmp_path)
    assert "ახლა 0.1.0" in check


@pytest.mark.parametrize("content, expected", [
    (None, "0.2.0"),            # ძველი ინსტალერი ფაილს არ წერდა — კოდის ვერსია
    ("0.2.0\r\n", "0.2.0"),     # წარმატებით დაყენდა
    ("0.1.9", "0.1.9"),         # ჩავარდა
    ("0.3.0", "0.2.0"),         # ფაილი კოდზე ახალს ვერ იტყვის
    ("garbage", "0.2.0"),
])
def test_installed_version_reads_marker_safely(tmp_path, content, expected):
    if content is not None:
        (tmp_path / updater.INSTALLED_FILE).write_text(content, encoding="utf-8")
    assert updater.installed_version(tmp_path, "0.2.0") == expected


def test_successful_install_marker_means_up_to_date(tmp_path):
    (tmp_path / updater.INSTALLED_FILE).write_text("0.2.0", encoding="utf-8")
    msg = updater.run(tmp_path / "none.db", client=_client(), current="0.2.0", app_dir=tmp_path,
                      launch=lambda *a: pytest.fail("must not launch"))
    assert msg == "უახლესი ვერსიაა (0.2.0)"


def test_installer_restarts_app_after_failure_and_records_success():
    from pathlib import Path
    iss = (Path(__file__).parents[1] / "packaging" / "installer.iss").read_text(encoding="utf-8-sig")
    assert "installed-version.txt" in iss and updater.INSTALLED_FILE == "installed-version.txt"
    assert "procedure DeinitializeSetup" in iss and "AppStopped and not Installed" in iss
    # Microsoft runtime — იგივე ვერსია არ გადაიწერება (სხვა პროგრამას შეიძლება ეკავოს)
    runtime = next(line for line in iss.splitlines() if line.startswith('Source: "{#DistDir}\\_internal\\VCRUNTIME'))
    assert "ignoreversion" not in runtime and "restartreplace" in runtime


def test_busy_queue_postpones_update(tmp_path):
    db = tmp_path / "hrsorter.db"
    repo = Repository(db)
    sid = repo.insert_source(sha256="a" * 64, size=1, page_count=1, archive_path="x.pdf", first_path="x.pdf",
                             profile_id="batch")
    repo.create_job(source_id=sid, profile_id="batch", mode="batch_pdf", config_version="v", config_snapshot="{}")
    repo.close()
    msg = updater.run(db, client=_client(), current="0.1.0", app_dir=tmp_path,
                      launch=lambda *a: pytest.fail("must not launch"))
    assert "რიგი" in msg and not (tmp_path / "_update").exists()


def test_hash_mismatch_rejects_file(tmp_path):
    with pytest.raises(updater.UpdateError):
        updater.run(tmp_path / "none.db", client=_client(digest="sha256:" + "0" * 64), current="0.1.0",
                    app_dir=tmp_path, launch=lambda *a: pytest.fail("must not launch"))
    assert list((tmp_path / "_update").iterdir()) == []


def test_append_log_writes_and_tolerates_no_write_access(tmp_path):
    assert updater.append_log("ხელმისაწვდომია 0.2.0", app_dir=tmp_path)
    assert (tmp_path / "_update" / "update.log").read_text(encoding="utf-8").rstrip().endswith("ხელმისაწვდომია 0.2.0")
    # ჩვეულებრივი მომხმარებელი Program Files-ში ვერ წერს: ჟურნალი გამოიტოვება, `update check` არ ვარდება
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("x", encoding="utf-8")
    assert updater.append_log("ignored", app_dir=blocked) is False


def test_cli_update_check_survives_unwritable_log(tmp_path, monkeypatch, capsys):
    from hrsorter import cli, config
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("x", encoding="utf-8")
    monkeypatch.setattr(config, "FROZEN", True)
    monkeypatch.setattr(updater, "APP_DIR", blocked)
    monkeypatch.setattr(updater, "run", lambda db, check_only: "ხელმისაწვდომია 0.2.0 (ახლა 0.1.0)")
    assert cli.main(["update", "check"]) == 0
    assert "ხელმისაწვდომია 0.2.0" in capsys.readouterr().out


def test_task_xml_principals_and_commands(tmp_path):
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    app = tmp_path / "Program Files" / "HR&Sorter"
    upd = ET.fromstring(updater.task_xml("update", app).split("\n", 1)[1])
    assert upd.find(".//t:UserId", ns).text == "S-1-5-18"                       # SYSTEM
    assert upd.find(".//t:Command", ns).text == str(app / "hrsorter.exe")
    assert upd.find(".//t:Arguments", ns).text == "update"
    assert "(A;;GRGX;;;AU)" in upd.find(".//t:SecurityDescriptor", ns).text   # ხატულიდან გაშვება
    start = ET.fromstring(updater.task_xml("start", app).split("\n", 1)[1])
    assert start.find(".//t:GroupId", ns).text == "S-1-5-32-545"                # შესული მომხმარებლები
    assert start.find(".//t:Arguments", ns).text == "open --no-browser"
