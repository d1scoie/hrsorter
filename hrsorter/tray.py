"""ხატულა ეკრანის ქვედა მარჯვენა კუთხეში (Windows notification area): ფერით სტატუსი, მენიუ, შეტყობინებები.

`hrsorter-bg.exe tray` — ეშვება Windows-ში შესვლისას და `open`-იდან. ერთ მომხმარებელზე ერთი ასლი
(lock-ფაილი `%LOCALAPPDATA%\\HRSorter`-ში). მდგომარეობას კითხულობს პირდაპირ ბაზიდან ყოველ 5 წამში,
ამიტომ ინტერფეისის (UI) გარეშეც მუშაობს.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from . import PIPELINE_VERSION
from .config import load_config
from .db import Repository

log = logging.getLogger(__name__)

POLL_SECONDS = 5.0
COLORS = {"ok": (22, 163, 74), "busy": (37, 99, 235), "attention": (234, 179, 8), "down": (220, 38, 38)}
LABELS = {"ok": "მუშაობს", "busy": "მუშავდება", "attention": "ყურადღება სჭირდება", "down": "დამმუშავებელი გაჩერებულია"}


@dataclass
class Status:
    alive: bool
    paused: bool
    queued: int
    running: int
    review: int
    errors: int
    current: str | None = None   # „ფაილი.pdf 3/20“
    mhr_ready: int = 0           # MHR-თან შედარება გავიდა — ატვირთვის დასტურს ელოდება
    mhr_failed: int = 0          # MHR-თან სხვაობაა
    mhr_latest: str | None = None   # ბოლოს შემოწმებული: „სახელი გვარი: მთავარი მიზეზი“


def read_status(repo: Repository, now: float | None = None) -> Status:
    hb = repo.get_control("worker_heartbeat")
    jobs = repo.job_counts()
    cur = repo.one("SELECT j.progress_done, j.progress_total, s.first_path FROM jobs j JOIN sources s ON s.id=j.source_id"
                   " WHERE j.status='running' ORDER BY j.updated_at DESC LIMIT 1")
    return Status(
        alive=bool(hb) and (now or time.time()) - float(hb) < 30,
        paused=repo.is_paused(),
        queued=jobs.get("queued", 0) + repo.one("SELECT COUNT(*) n FROM inbox_files WHERE state='pending'")["n"],
        running=jobs.get("running", 0),
        review=repo.one("SELECT COUNT(*) n FROM documents WHERE active=1 AND status='needs_review'")["n"],
        errors=jobs.get("failed", 0) + repo.one("SELECT COUNT(*) n FROM inbox_files WHERE state='error'")["n"],
        current=f'{Path(cur["first_path"]).name} {cur["progress_done"]}/{cur["progress_total"]}' if cur else None,
        **_mhr_status(repo),
    )


def _mhr_status(repo: Repository) -> dict:
    n = {x["status"]: x["n"] for x in repo.q("SELECT status, COUNT(*) n FROM mhr_items WHERE status IN ('ready', 'failed')"
                                             " GROUP BY status")}
    last = repo.one("SELECT validation FROM mhr_items WHERE status IN ('ready', 'failed') ORDER BY updated_at DESC LIMIT 1")
    latest = None
    if last and last["validation"]:
        v = json.loads(last["validation"])
        emp = v.get("employee") or {}
        name = " ".join(x for x in (emp.get("first_name"), emp.get("last_name")) if x) or "თანამშრომელი"
        reason = next(iter(v.get("summary") or []), "")
        latest = f"{name}: {reason}" if reason else name
    return {"mhr_ready": n.get("ready", 0), "mhr_failed": n.get("failed", 0), "mhr_latest": latest}


def state(s: Status) -> str:
    if not s.alive:
        return "down"
    if s.paused or s.review or s.errors or s.mhr_ready or s.mhr_failed:
        return "attention"
    if s.running or s.queued:
        return "busy"
    return "ok"


def headline(s: Status) -> str:
    return f"HR გადამრჩევი {PIPELINE_VERSION} — {LABELS[state(s)]}" + (" (შეჩერებულია)" if s.paused and s.alive else "")


def counts(s: Status) -> str:
    return f"რიგში {s.queued + s.running} · გადასამოწმებელი {s.review} · შეცდომა {s.errors}"


def mhr_line(s: Status) -> str:
    return f"MHR: მზადაა {s.mhr_ready} · სხვაობა {s.mhr_failed}"


def tooltip(s: Status) -> str:
    return f"{headline(s)}\n{counts(s)}"[:127]   # Windows-ის ზღვარი


def notifications(prev: Status | None, cur: Status) -> list[tuple[str, str]]:
    """რა შეიცვალა წინა შემოწმების შემდეგ (პირველ შემოწმებაზე — არაფერი, რომ ძველზე არ შეგვაწუხოს)."""
    if prev is None:
        return []
    out = []
    if prev.alive and not cur.alive:
        out.append(("დამმუშავებელი გაჩერდა", "ახალი სკანები არ მუშავდება. დააჭირე ხატულას → „გახსნა“."))
    if cur.review > prev.review:
        out.append(("ახალი გადასამოწმებელი", f"{cur.review - prev.review} დოკუმენტს შენი ნახვა სჭირდება."))
    if cur.errors > prev.errors:
        out.append(("შეცდომა დამუშავებისას", f"{cur.errors - prev.errors} ფაილი ვერ დამუშავდა (Processed\\_Failed)."))
    if cur.mhr_failed > prev.mhr_failed:
        new = cur.mhr_failed - prev.mhr_failed
        out.append(("MHR-თან შედარება: სხვაობაა",
                    cur.mhr_latest if new == 1 and cur.mhr_latest else f"{new} დოკუმენტში სხვაობაა — ნახე MHR გვერდზე"))
    elif cur.mhr_ready > prev.mhr_ready:
        new = cur.mhr_ready - prev.mhr_ready
        out.append(("მზადაა MHR-ზე ასატვირთად",
                    cur.mhr_latest if new == 1 and cur.mhr_latest else f"{new} დოკუმენტი მზადაა — დაადასტურე MHR გვერდზე"))
    return out


def check_update(client=None) -> tuple[str | None, str]:
    """ხატულის „განახლების შემოწმება“: (ახალი ვერსია ან None, შეტყობინების ტექსტი)."""
    from . import updater
    import httpx
    client = client or httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "hrsorter-tray"})
    rel = updater.latest_release(client)
    if rel is None or updater.parse_version(rel.version) <= updater.parse_version(PIPELINE_VERSION):
        return None, f"დაინსტალირებულია უახლესი ვერსია ({PIPELINE_VERSION})."
    return rel.version, (f"{PIPELINE_VERSION} → {rel.version}. დაიწყება, როცა მიმდინარე დამუშავება დასრულდება; "
                         "პროგრამა რამდენიმე წამით გაითიშება და თავიდან ჩაირთვება.")


def version_changed(marker: Path) -> str | None:
    """წინა გაშვების ვერსია, თუ მას შემდეგ განახლდა (შეტყობინებისთვის); ინახავს მიმდინარეს."""
    try:
        old = marker.read_text(encoding="utf-8").strip()
    except OSError:
        old = None
    if old != PIPELINE_VERSION:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(PIPELINE_VERSION, encoding="utf-8")
    return old if old and old != PIPELINE_VERSION else None


def icon_image(kind: str):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill=COLORS[kind] + (255,))
    d.text((32, 33), "HR", anchor="mm", fill="white", font=ImageFont.load_default(size=28))
    return img


def main(config_path: str | None = None) -> int:
    import pystray
    from .cli import _spawn
    from .worker import InstanceLock
    from .config import FROZEN
    user_dir = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "HRSorter"
    try:
        lock = InstanceLock(user_dir, "tray.lock")
    except RuntimeError:
        return 0   # უკვე ჩართულია
    cur = {"cfg": load_config(config_path)}   # ყოველ შემოწმებაზე ახლდება (მთავარი საქაღალდე შეიძლება შეიცვალოს)
    url = f"http://127.0.0.1:{int(cur['cfg'].section('ui')['port'])}"
    last: dict[str, Status | None] = {"s": None}
    upd = {"available": None, "wanted": False, "checking": False}
    stop = threading.Event()

    def ask_update(icon, item) -> None:
        if not FROZEN:
            icon.notify("განახლება მუშაობს მხოლოდ დაინსტალირებულ ვერსიაში.", "განახლება")
            return
        if upd["checking"]:
            return

        def run() -> None:   # ქსელი — ცალკე ნაკადში, მენიუ არ უნდა გაიჭედოს
            upd["checking"] = True
            try:
                version, msg = check_update()
                upd["available"], upd["wanted"] = version, bool(version)
            except Exception as e:
                msg = f"ვერ შემოწმდა ({type(e).__name__}). შეამოწმე ინტერნეტი."
            finally:
                upd["checking"] = False
            icon.notify(msg, "განახლება")
            icon.update_menu()
        threading.Thread(target=run, daemon=True).start()

    def update_label(item) -> str:
        if upd["checking"]:
            return "განახლების შემოწმება…"
        if upd["wanted"]:
            return f"განახლება {upd['available']}-ზე — ელოდება დამუშავების დასრულებას"
        return "განახლების შემოწმება"

    def toggle_pause(icon, item) -> None:
        r = Repository(cur["cfg"].paths.db)
        try:
            r.set_control("paused", "0" if r.is_paused() else "1")
        finally:
            r.close()

    def quit_(icon, item) -> None:
        stop.set()
        icon.stop()

    def text(fn):
        return lambda item: fn(last["s"]) if last["s"] else "იტვირთება…"

    menu = pystray.Menu(
        pystray.MenuItem(text(headline), None, enabled=False),
        pystray.MenuItem(text(counts), None, enabled=False),
        pystray.MenuItem(text(lambda s: f"ახლა: {s.current}"), None, enabled=False,
                         visible=lambda item: bool(last["s"] and last["s"].current)),
        pystray.MenuItem(text(mhr_line), lambda icon, item: webbrowser.open(f"{url}/mhr"),
                         visible=lambda item: bool(last["s"] and (last["s"].mhr_ready or last["s"].mhr_failed))),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("გახსნა", lambda icon, item: _spawn("open"), default=True),
        pystray.MenuItem("გადასამოწმებელი", lambda icon, item: webbrowser.open(f"{url}/review")),
        pystray.MenuItem("საქაღალდის გახსნა", lambda icon, item: os.startfile(cur["cfg"].paths.root)),
        pystray.MenuItem(lambda item: "გაგრძელება" if last["s"] and last["s"].paused else "შეჩერება", toggle_pause),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(f"ვერსია {PIPELINE_VERSION}", None, enabled=False),
        pystray.MenuItem(update_label, ask_update),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("ხატულის დახურვა", quit_),
    )
    icon = pystray.Icon("hrsorter", icon_image("down"), "HR გადამრჩევი", menu)

    def poll(icon) -> None:
        icon.visible = True
        old = version_changed(user_dir / "version.txt")
        if old:
            icon.notify(f"{old} → {PIPELINE_VERSION}", "HR გადამრჩევი განახლდა")
        repo = Repository(cur["cfg"].paths.db)
        kind = None
        try:
            while not stop.is_set():
                try:
                    new_cfg = load_config(config_path)
                    if new_cfg.paths.db != cur["cfg"].paths.db:
                        repo.close()
                        repo = Repository(new_cfg.paths.db)
                    cur["cfg"] = new_cfg
                    s = read_status(repo)
                    if state(s) != kind:
                        kind = state(s)
                        icon.icon = icon_image(kind)
                    icon.title = tooltip(s)
                    notes = notifications(last["s"], s)
                    last["s"] = s
                    icon.update_menu()
                    if notes and new_cfg.section("tray")["notifications"]:
                        icon.notify("\n".join(m for _, m in notes), notes[0][0])
                    if upd["wanted"] and s.queued + s.running == 0:   # დამუშავებას შუაში არ ვწყვეტთ
                        upd["wanted"] = False
                        icon.update_menu()
                        try:
                            from . import updater
                            updater.trigger_update()
                        except Exception as e:
                            icon.notify(str(e) if str(e) else type(e).__name__, "განახლება ვერ დაიწყო")
                except Exception as e:   # დროებით დაკავებული ბაზა და ა.შ. — შემდეგ ციკლზე
                    log.warning("tray poll failed: %s", type(e).__name__)
                stop.wait(POLL_SECONDS)
        finally:
            repo.close()

    try:
        icon.run(setup=poll)
    finally:
        stop.set()
        lock.release()
    return 0
