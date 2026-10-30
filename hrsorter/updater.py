"""ავტომატური განახლება GitHub Releases-დან.

`hrsorter update` (Task Scheduler-ის დავალება `HRSorter\\Update`, SYSTEM-ის სახელით — ჩატვირთვისას და ყოველ 4 საათში):
ბოლო release → თუ ვერსია ახალია და რიგი ცარიელია → Setup.exe ჩამოტვირთვა `{app}\\_update`-ში (Program Files —
ჩაწერა მხოლოდ ადმინს შეუძლია) → sha256 შემოწმება GitHub-ის `digest`-ით → ჩუმი ინსტალაცია. ინსტალერი თავად აჩერებს
პროგრამას, ცვლის ფაილებს და `HRSorter\\Start` დავალებით თავიდან უშვებს შესული მომხმარებლების სესიებში.

რეპოზიტორია კოდშია და არა config.json-ში: კონფიგურაციას ჩვეულებრივი მომხმარებელიც ცვლის, ეს კოდი კი
გადმოწერილ ფაილს SYSTEM-ის უფლებით უშვებს.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

import httpx

from . import PIPELINE_VERSION

GITHUB_REPO = "d1scoie/hrsorter-releases"
ASSET_RE = re.compile(r"^HRSorter-Setup-(\d+\.\d+\.\d+)\.exe$")
APP_DIR = Path(sys.executable).parent        # შეფუთულ ვერსიაში — {app}
UPDATE_DIR_NAME = "_update"
TASK_UPDATE = r"HRSorter\Update"
TASK_START = r"HRSorter\Start"


class UpdateError(Exception):
    pass


@dataclass
class Release:
    version: str
    name: str
    url: str
    sha256: str


def parse_version(s: str) -> tuple[int, ...]:
    return tuple(int(x) for x in s.lstrip("v").split("."))


def latest_release(client: httpx.Client) -> Release | None:
    """ბოლო გამოქვეყნებული release (draft/pre-release არ ითვლება). None — თუ release ან Setup ფაილი არ არის."""
    r = client.get(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                   headers={"Accept": "application/vnd.github+json"})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    for a in r.json().get("assets", []):
        m = ASSET_RE.match(a.get("name", ""))
        digest = a.get("digest") or ""
        if m and digest.startswith("sha256:"):
            return Release(m.group(1), a["name"], a["browser_download_url"], digest.split(":", 1)[1].lower())
    return None


def queue_busy(db_path: Path) -> bool:
    """რიგში ან დამუშავებაში არის სამუშაო? ბაზა მხოლოდ წასაკითხად იხსნება (SYSTEM-მა ფაილები არ შექმნას)."""
    if not db_path.exists():
        return False
    con = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        return con.execute("SELECT 1 FROM jobs WHERE status IN ('queued', 'running') LIMIT 1").fetchone() is not None
    finally:
        con.close()


def download(client: httpx.Client, rel: Release, dest_dir: Path) -> Path:
    """Setup.exe-ს ჩამოტვირთვა; sha256 თუ არ ემთხვევა — ფაილი იშლება და UpdateError."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    for old in dest_dir.glob("HRSorter-Setup-*"):
        old.unlink(missing_ok=True)
    target = dest_dir / rel.name
    part = target.with_name(target.name + ".part")
    h = hashlib.sha256()
    with client.stream("GET", rel.url) as r:
        r.raise_for_status()
        with open(part, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                h.update(chunk)
                f.write(chunk)
    if h.hexdigest() != rel.sha256:
        part.unlink()
        raise UpdateError(f"sha256 mismatch for {rel.name}")
    os.replace(part, target)
    return target


def _launch(setup: Path, log: Path) -> None:
    """ინსტალერის ჩუმი გაშვება და მაშინვე გამოსვლა — hrsorter.exe დაკავებული არ უნდა დარჩეს, რომ Setup-მა შეცვალოს."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={log}"],
                     creationflags=flags, close_fds=True)


def run(db_path: Path | None, *, check_only: bool = False, client: httpx.Client | None = None,
        current: str = PIPELINE_VERSION, app_dir: Path = APP_DIR, launch=_launch) -> str:
    """ერთი შემოწმება/განახლება. აბრუნებს შედეგის მოკლე აღწერას (ჟურნალისთვის)."""
    client = client or httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": "hrsorter-updater"})
    rel = latest_release(client)
    if rel is None:
        return "release არ მოიძებნა"
    if parse_version(rel.version) <= parse_version(current):
        return f"უახლესი ვერსიაა ({current})"
    if check_only:
        return f"ხელმისაწვდომია {rel.version} (ახლა {current})"
    if queue_busy(db_path):
        return f"ხელმისაწვდომია {rel.version}, მაგრამ რიგი ცარიელი არ არის — შემდეგ შემოწმებაზე"
    upd = app_dir / UPDATE_DIR_NAME
    setup = download(client, rel, upd)
    launch(setup, upd / "setup.log")
    return f"{current} → {rel.version}: ინსტალაცია დაიწყო"


def trigger_update() -> str:
    """ხატულიდან (ჩვეულებრივი მომხმარებელი): SYSTEM-ის დავალების `HRSorter\\Update` დაუყოვნებლივ გაშვება.
    თუ დავალების გაშვების უფლება არ აქვს — `hrsorter.exe update` ადმინისტრატორის ნებართვით (UAC)."""
    r = subprocess.run(["schtasks.exe", "/Run", "/TN", TASK_UPDATE], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode == 0:
        return "task"
    import ctypes
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", str(APP_DIR / "hrsorter.exe"), "update", str(APP_DIR), 0)
    if rc <= 32:   # ShellExecute: ≤32 = შეცდომა (მაგ. ნებართვა არ მოიცა)
        raise UpdateError("განახლება ვერ დაიწყო: ადმინისტრატორის ნებართვა არ მოიცა")
    return "uac"


# --- Task Scheduler ----------------------------------------------------------------
# Update დავალების გაშვება (არა შეცვლა) ნებისმიერ შესულ მომხმარებელს შეუძლია — ხატულის „განახლებისთვის“.
# ის მხოლოდ ზემოთ აღწერილ შემოწმებას უშვებს (ფიქსირებული რეპო + sha256), ამიტომ უფლებებს არ ზრდის.
_UPDATE_SDDL = "D:(A;;FA;;;SY)(A;;FA;;;BA)(A;;GRGX;;;AU)"
_SETTINGS = """  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>{network}</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>{limit}</ExecutionTimeLimit>
    <AllowStartOnDemand>true</AllowStartOnDemand>
  </Settings>"""


def task_xml(kind: str, app_dir: Path) -> str:
    """update — SYSTEM, ჩატვირთვისას + ყოველ 4 საათში; start — შესული მომხმარებლები (Users), მხოლოდ მოთხოვნით."""
    registration = ""
    if kind == "update":
        registration = f"<RegistrationInfo><SecurityDescriptor>{_UPDATE_SDDL}</SecurityDescriptor></RegistrationInfo>"
        principal = "<UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel>"
        triggers = ("<BootTrigger><Delay>PT10M</Delay></BootTrigger>"
                    "<CalendarTrigger><StartBoundary>2026-01-01T00:30:00</StartBoundary>"
                    "<Repetition><Interval>PT4H</Interval><Duration>P1D</Duration></Repetition>"
                    "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>")
        exe, arguments, settings = app_dir / "hrsorter.exe", "update", _SETTINGS.format(network="true", limit="PT1H")
    else:
        principal = "<GroupId>S-1-5-32-545</GroupId><RunLevel>LeastPrivilege</RunLevel>"
        triggers = ""
        exe, arguments, settings = app_dir / "hrsorter-bg.exe", "open --no-browser", _SETTINGS.format(network="false", limit="PT0S")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  {registration}
  <Triggers>{triggers}</Triggers>
  <Principals><Principal id="Author">{principal}</Principal></Principals>
{settings}
  <Actions Context="Author"><Exec><Command>{escape(str(exe))}</Command><Arguments>{escape(arguments)}</Arguments></Exec></Actions>
</Task>
"""


def install_tasks(app_dir: Path = APP_DIR) -> None:
    """ინსტალერიდან (ადმინის უფლებით): ორივე დავალების შექმნა/განახლება."""
    upd = app_dir / UPDATE_DIR_NAME
    upd.mkdir(parents=True, exist_ok=True)
    for kind, name in (("update", TASK_UPDATE), ("start", TASK_START)):
        xml = upd / f"task-{kind}.xml"
        xml.write_text(task_xml(kind, app_dir), encoding="utf-16")
        subprocess.run(["schtasks.exe", "/Create", "/F", "/TN", name, "/XML", str(xml)], check=True,
                       stdout=subprocess.DEVNULL)
