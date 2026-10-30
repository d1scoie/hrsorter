"""Windows-ში შესვლისას ავტომატური გაშვება ადმინისტრატორის უფლებების გარეშე.

მომხმარებლის Startup საქაღალდეში იწერება `.vbs` სკრიპტი, რომელიც ფანჯრის გარეშე უშვებს
`"<venv>\\Scripts\\pythonw.exe" -m hrsorter.cli worker` (და სურვილისამებრ `ui`). კონფიგურაციის ბილიკი
გადაეცემა `HRSORTER_CONFIG` გარემოს ცვლადით. ფაილი UTF-16 (BOM)-ით იწერება — ბილიკებში ქართული ასოებისთვის.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .config import DEFAULT_CONFIG_ENV, PROJECT_DIR

FILES = {"worker": "hrsorter-worker.vbs", "ui": "hrsorter-ui.vbs", "tray": "hrsorter-tray.vbs"}


def default_startup_dir() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _pythonw() -> Path:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return w if w.exists() else exe


def _vbs(command: str, config_path: Path | None) -> str:
    q = lambda s: str(s).replace('"', '""')   # VBScript სტრიქონში ბრჭყალის გაორმაგება
    lines = [
        "' hrsorter autostart (generated; remove with `hrsorter autostart uninstall`)",
        'Set sh = CreateObject("WScript.Shell")',
        f'sh.CurrentDirectory = "{q(PROJECT_DIR)}"',
    ]
    if config_path is not None:
        lines.append(f'sh.Environment("Process")("{DEFAULT_CONFIG_ENV}") = "{q(config_path)}"')
    lines.append(f'sh.Run """{q(_pythonw())}"" -m hrsorter.cli {command}", 0, False')
    return "\r\n".join(lines) + "\r\n"


def install(config_path: str | Path | None, include_ui: bool = True, startup_dir: Path | None = None) -> list[Path]:
    """Startup სკრიპტების შექმნა/განახლება. აბრუნებს ჩაწერილ ფაილებს."""
    d = Path(startup_dir) if startup_dir else default_startup_dir()
    d.mkdir(parents=True, exist_ok=True)
    cfg = Path(config_path).resolve() if config_path else None
    written = []
    for command in (["worker", "ui", "tray"] if include_ui else ["worker", "tray"]):
        p = d / FILES[command]
        p.write_text(_vbs(command, cfg), encoding="utf-16", newline="")
        written.append(p)
    if not include_ui:
        (d / FILES["ui"]).unlink(missing_ok=True)
    return written


def uninstall(startup_dir: Path | None = None) -> list[Path]:
    """Startup სკრიპტების წაშლა. აბრუნებს წაშლილ ფაილებს."""
    d = Path(startup_dir) if startup_dir else default_startup_dir()
    removed = []
    for name in FILES.values():
        p = d / name
        if p.exists():
            p.unlink()
            removed.append(p)
    return removed


def status(startup_dir: Path | None = None) -> dict:
    d = Path(startup_dir) if startup_dir else default_startup_dir()
    return {"startup_dir": str(d), **{k: (d / name).exists() for k, name in FILES.items()}}
