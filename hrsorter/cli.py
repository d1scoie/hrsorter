"""ბრძანებების ხაზი: hrsorter <command>.

  init                      config.json / document_types.json შექმნა მაგალითიდან + საქაღალდეები
  worker                    ფონური დამმუშავებელი (მონიტორინგი + რიგი)
  ui                        ადგილობრივი ვებინტერფეისი (http://127.0.0.1:8765)
  run-once                  ინბოქსის ერთჯერადი სკანირება და რიგის დამუშავება (საპილოტე/ტესტი)
  status                    რიგისა და შედეგების მოკლე მდგომარეობა
  pause | resume            დამუშავების შეჩერება / გაგრძელება
  reprocess <job_id>        აშკარა ხელახალი დამუშავება მიმდინარე კონფიგურაციით
  secret set|delete <name>  გასაღების შენახვა Windows Credential Manager-ში
  autostart install|uninstall|status
  update [run|check|install-tasks]  ავტომატური განახლება GitHub Releases-დან
  tray                      ხატულა ეკრანის ქვედა მარჯვენა კუთხეში (სტატუსი, მენიუ, შეტყობინებები)
"""
from __future__ import annotations

import argparse
import getpass
import json
import shutil
import sys
from pathlib import Path

from .config import (DEFAULT_CONFIG_PATH, EXAMPLE_CONFIG_PATH, EXAMPLE_TYPES_PATH, Config, load_config)
from .db import Repository


def _ensure_dirs(cfg: Config) -> None:
    p = cfg.paths
    for d in [p.root, *p.internal_dirs()]:
        d.mkdir(parents=True, exist_ok=True)
    for pr in cfg.profiles:
        pr.inbox.mkdir(parents=True, exist_ok=True)


def cmd_init(args) -> int:
    cfg_path = Path(args.config or DEFAULT_CONFIG_PATH)
    if not cfg_path.exists():
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))
        if args.root:
            data["paths"]["root"] = args.root
        cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"შეიქმნა: {cfg_path}")
    types_path = cfg_path.parent / "document_types.json"
    if not types_path.exists():
        shutil.copyfile(EXAMPLE_TYPES_PATH, types_path)
        print(f"შეიქმნა: {types_path}")
    cfg = load_config(cfg_path)
    _ensure_dirs(cfg)
    Repository(cfg.paths.db).close()
    print(f"საქაღალდეები მზადაა: {cfg.paths.root}")
    for pr in cfg.profiles:
        print(f"  ინბოქსი [{pr.id}] {pr.mode.value}: {pr.inbox}")
    return 0


def cmd_setup(args) -> int:
    """ინსტალერისთვის: კონფიგურაციის შექმნა/განახლება. არსებული მომხმარებლის პარამეტრები არ იცვლება —
    მხოლოდ ჩაშენებული Tesseract-ის ბილიკები (განახლებისას საქაღალდე შეიძლება შეიცვალოს)."""
    cfg_path = Path(args.config or DEFAULT_CONFIG_PATH)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    if cfg_path.exists():
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    else:
        data = json.loads(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))
        data.pop("_comment", None)
        data["companies"] = []   # მაგალითის გამოგონილი კომპანიები რეალურ ინსტალაციაში არ გადადის
        data["paths"]["root"] = (args.root or data["paths"]["root"]).replace("\\", "/")
    if args.tesseract_dir:
        tdir = Path(args.tesseract_dir)
        ocr = data.setdefault("ocr", {})
        ocr["tesseract_cmd"] = str(tdir / "tesseract.exe").replace("\\", "/")
        ocr["tessdata_dir"] = str(tdir / "tessdata").replace("\\", "/")
    cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    types_path = cfg_path.parent / data.get("paths", {}).get("types_file", "document_types.json")
    if not types_path.exists():
        shutil.copyfile(EXAMPLE_TYPES_PATH, types_path)
    cfg = load_config(cfg_path)
    _ensure_dirs(cfg)
    Repository(cfg.paths.db).close()
    print(f"კონფიგურაცია: {cfg_path}; სამუშაო საქაღალდე: {cfg.paths.root}")
    return 0


def _spawn(*cmd_args: str) -> None:
    """ფონური პროცესის გაშვება ფანჯრის გარეშე (შეფუთულ ვერსიაში — იგივე exe)."""
    import subprocess
    if getattr(sys, "frozen", False):
        argv = [sys.executable, *cmd_args]
    else:
        argv = [sys.executable, "-m", "hrsorter.cli", *cmd_args]
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(argv, creationflags=flags, close_fds=True)


def cmd_open(args) -> int:
    """მალსახმობისთვის: თუ worker/UI არ მუშაობს — გაუშვებს, შემდეგ ბრაუზერში ხსნის სტატისტიკას."""
    import time
    import urllib.request
    import webbrowser
    cfg = load_config(args.config)
    _spawn("tray")   # მეორე ასლი თავად გამოდის (lock)
    port = int(cfg.section("ui")["port"])
    url = f"http://127.0.0.1:{port}"
    r = Repository(cfg.paths.db)
    try:
        hb = r.get_control("worker_heartbeat")
    finally:
        r.close()
    if not hb or time.time() - float(hb) > 30:
        _spawn("worker")   # მეორე ინსტანსს InstanceLock არ უშვებს
    for i in range(40):
        try:
            urllib.request.urlopen(f"{url}/api/status", timeout=1).close()
            break
        except OSError:
            if i == 0:
                _spawn("ui")
            time.sleep(0.5)
    if not args.no_browser:
        webbrowser.open(f"{url}/stats")
    return 0


def cmd_update(args) -> int:
    """GitHub-ზე ახალი ვერსიის შემოწმება/დაყენება (Task Scheduler-იდან SYSTEM-ის სახელით) ან დავალებების შექმნა."""
    import time
    from . import updater
    from .config import FROZEN
    if args.action == "install-tasks":
        updater.install_tasks()
        print(f"დავალებები შეიქმნა: {updater.TASK_UPDATE}, {updater.TASK_START}")
        return 0
    if not FROZEN and args.action == "run":
        print("განახლება მხოლოდ დაინსტალირებულ ვერსიაში მუშაობს (შემოწმება: hrsorter update check)")
        return 1
    try:
        check = args.action == "check"   # შემოწმებას ბაზა (რიგი) არ სჭირდება
        msg, code = updater.run(None if check else load_config(args.config).paths.db, check_only=check), 0
    except Exception as e:   # დავალების ჟურნალისთვის — მიზეზი უნდა ჩაიწეროს
        msg, code = f"შეცდომა: {type(e).__name__}: {e}", 1
    print(msg)
    if FROZEN:
        log = updater.APP_DIR / updater.UPDATE_DIR_NAME / "update.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    return code


def cmd_ocr_test(args) -> int:
    """OCR-ის ხარისხის ტესტი: სკანი და ამოკითხული ტექსტი გვერდიგვერდ (report.html), სურვილისამებრ CER/WER."""
    import webbrowser
    from .ocrtest import run
    report = run(Path(args.pdf), args.pages, Path(args.out), load_config(args.config),
                 Path(args.truth) if args.truth else None)
    print(f"ანგარიში: {report}")
    if not args.no_open:
        webbrowser.open(report.as_uri())
    return 0


def cmd_tray(args) -> int:
    from .tray import main as tray_main
    return tray_main(args.config)


def cmd_worker(args) -> int:
    from .worker import main as worker_main
    worker_main(args.config)
    return 0


def cmd_ui(args) -> int:
    import uvicorn

    from .ui.app import create_app
    cfg = load_config(args.config)
    ui = cfg.section("ui")
    uvicorn.run(create_app(args.config), host="127.0.0.1", port=int(ui["port"]), log_level="warning")
    return 0


def cmd_run_once(args) -> int:
    from .pipeline import process_job
    from .watcher import InboxScanner
    from .worker import InstanceLock, Worker
    cfg = load_config(args.config)
    _ensure_dirs(cfg)
    lock = InstanceLock(cfg.paths.state)   # worker უკვე მუშაობს → შეცდომა, არა პარალელური დამუშავება
    repo = Repository(cfg.paths.db)
    scanner = InboxScanner(cfg, repo)
    # სტაბილურობის ინტერვალი მაინც მოქმედებს — ახლად ჩაწერილი ფაილი შეიძლება შემდეგ გაშვებაზე აიღოს
    for ev in scanner.scan_once():
        print(f"{ev.get('action')}: {ev.get('path')}")
    worker = Worker(lambda: load_config(args.config), process_job)
    n = 0
    while worker.run_once():
        n += 1
    print(f"დამუშავდა სამუშაო: {n}")
    lock.release()
    return cmd_status(args)


def cmd_status(args) -> int:
    cfg = load_config(args.config)
    repo = Repository(cfg.paths.db)
    jobs = {r["status"]: r["n"] for r in repo.q("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
    docs = {r["status"]: r["n"] for r in repo.q("SELECT status, COUNT(*) n FROM documents WHERE active=1 GROUP BY status")}
    print(json.dumps({"paused": repo.is_paused(), "jobs": jobs, "documents": docs}, ensure_ascii=False, indent=2))
    return 0


def cmd_pause(args, value: str) -> int:
    cfg = load_config(args.config)
    Repository(cfg.paths.db).set_control("paused", value)
    print("შეჩერებულია" if value == "1" else "გაგრძელდა")
    return 0


def cmd_reprocess(args) -> int:
    cfg = load_config(args.config)
    repo = Repository(cfg.paths.db)
    new_id = reprocess(repo, cfg, args.job_id)
    print(f"ახალი სამუშაო: {new_id}" if new_id else "ამ წყაროზე უკვე არის აქტიური სამუშაო")
    return 0 if new_id else 1


def reprocess(repo: Repository, cfg: Config, job_id: str) -> str | None:
    """აშკარა ხელახალი დამუშავება: ახალი job მიმდინარე კონფიგურაციით; წინა შედეგები ჩანაცვლდება."""
    job = repo.get_job(job_id)
    if job is None:
        raise SystemExit(f"job not found: {job_id}")
    profile = cfg.profile(job["profile_id"])
    mode = profile.mode.resolve(repo.get_source(job["source_id"])["page_count"])
    return repo.create_job(source_id=job["source_id"], profile_id=profile.id, mode=mode.value,
                           config_version=cfg.version, config_snapshot=cfg.snapshot(), reprocess_of=job_id,
                           priority=1)


def _read_and_clear_clipboard() -> str:
    """ბუფერის ტექსტი PowerShell-ით; წაკითხვის შემდეგ ბუფერი სუფთავდება (საიდუმლო იქ აღარ რჩება)."""
    import subprocess
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    out = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"], capture_output=True,
                         creationflags=flags).stdout.decode("utf-8", errors="replace")
    subprocess.run(["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value ' '"], capture_output=True,
                   creationflags=flags)
    return out


def cmd_secret(args) -> int:
    from . import secrets
    if args.action == "set":
        if args.from_clipboard:   # ფარულ შეყვანაში ჩასმა Windows-ის კონსოლში ხშირად არ მუშაობს
            value = _read_and_clear_clipboard()
        else:
            value = getpass.getpass(f"{args.name}: ")
        value = value.strip()
        if not value:
            print("შეცდომა: მნიშვნელობა ცარიელია — არაფერი შენახულა" + ("" if args.from_clipboard else
                  " (სცადე: --from-clipboard, ჯერ დააკოპირე ტოკენი)"))
            return 1
        secrets.set_secret(args.name, value)
        print(f"შენახულია Windows Credential Manager-ში ({len(value)} სიმბოლო)")
    else:
        secrets.delete_secret(args.name)
        print("წაშლილია")
    return 0


def cmd_autostart(args) -> int:
    from . import autostart
    if args.action == "install":
        for p in autostart.install(args.config):
            print(f"შეიქმნა: {p}")
    elif args.action == "uninstall":
        for p in autostart.uninstall():
            print(f"წაიშალა: {p}")
    else:
        print(json.dumps(autostart.status(), ensure_ascii=False, indent=2, default=str))
    return 0


def _redirect_windowless_streams() -> None:
    """ფანჯრის გარეშე exe-ში stdout/stderr არ არსებობს (uvicorn და logging მაშინ ვარდება) — ვწერთ ჟურნალში."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    from .config import MACHINE_DATA_DIR
    log_dir = MACHINE_DATA_DIR / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        stream = open(log_dir / "app.log", "a", encoding="utf-8", buffering=1)
    except OSError:
        import os
        stream = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def main(argv: list[str] | None = None) -> int:
    _redirect_windowless_streams()
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="hrsorter", description="HR PDF გადამრჩევი")
    ap.add_argument("--config", help="config.json ბილიკი (ნაგულისხმევად config/config.json ან HRSORTER_CONFIG)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init"); p.add_argument("--root", help="მთავარი სამუშაო საქაღალდე, მაგ. D:/HRScan")
    sub.add_parser("worker"); sub.add_parser("ui"); sub.add_parser("run-once"); sub.add_parser("status")
    sub.add_parser("tray")
    sub.add_parser("pause"); sub.add_parser("resume")
    p = sub.add_parser("open"); p.add_argument("--no-browser", action="store_true", help="მხოლოდ worker/UI-ის გაშვება")
    p = sub.add_parser("update", help="ავტომატური განახლება GitHub Releases-დან")
    p.add_argument("action", nargs="?", default="run", choices=["run", "check", "install-tasks"])
    p = sub.add_parser("ocr-test", help="OCR-ის ხარისხის ტესტი")
    p.add_argument("pdf"); p.add_argument("--pages", help="მაგ. 1,5,9 ან 1-3 (ნაგულისხმევად ყველა)")
    p.add_argument("--out", required=True, help="სად ჩაიწეროს სურათები, ტექსტები და report.html")
    p.add_argument("--truth", help="საქაღალდე სწორი ტექსტებით (page_001.txt …) — CER/WER-ისთვის")
    p.add_argument("--no-open", action="store_true", help="ანგარიში ბრაუზერში არ გაიხსნას")
    p = sub.add_parser("setup", help="ინსტალერისთვის")
    p.add_argument("--root"); p.add_argument("--tesseract-dir")
    p = sub.add_parser("reprocess"); p.add_argument("job_id")
    p = sub.add_parser("secret"); p.add_argument("action", choices=["set", "delete"]); p.add_argument("name")
    p.add_argument("--from-clipboard", action="store_true", help="მნიშვნელობა ბუფერიდან (შემდეგ ბუფერი სუფთავდება)")
    p = sub.add_parser("autostart"); p.add_argument("action", choices=["install", "uninstall", "status"])
    args = ap.parse_args(argv)
    handlers = {
        "init": cmd_init, "worker": cmd_worker, "ui": cmd_ui, "run-once": cmd_run_once, "status": cmd_status,
        "pause": lambda a: cmd_pause(a, "1"), "resume": lambda a: cmd_pause(a, "0"),
        "reprocess": cmd_reprocess, "secret": cmd_secret, "autostart": cmd_autostart,
        "setup": cmd_setup, "open": cmd_open, "ocr-test": cmd_ocr_test, "update": cmd_update,
        "tray": cmd_tray,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
