"""მსუბუქი ადგილობრივი ვებინტერფეისი (მხოლოდ 127.0.0.1, ინტერნეტის გარეშე). მძიმე დამუშავება აქ არ ხდება —
UI კითხულობს SQLite-ს და ცვლის მხოლოდ control-ს, ახალ (reprocess) სამუშაოს და კონფიგურაციის ფაილებს.

დიზაინი: მარტივი ოპერატორისთვის — გვერდითი მენიუ (მობილურზე ქვედა პანელი), სისტემური შრიფტი (Segoe UI,
ქართულისთვის Sylfaen), ღია/მუქი თემა, SVG იკონები, ხილული ფოკუსი, ტექნიკური დეტალები დამალულია.
"""
from __future__ import annotations

import copy
import html
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from ..config import Config, ConfigError, load_config
from ..db import Repository
from ..mhr_models import clean_file_title

STATUS_KA = {"split": "დაიყო ფაილებად", "auto_saved": "დალაგდა", "proposed": "შემოთავაზებული", "needs_review": "გადასამოწმებელი",
             "superseded": "ჩანაცვლებული", "publishing": "იწერება", "queued": "რიგში", "running": "მუშავდება",
             "done": "დასრულდა", "failed": "შეცდომა", "cancelled": "გაუქმდა"}
STATUS_TONE = {"auto_saved": "ok", "proposed": "info", "needs_review": "warn", "superseded": "mute", "publishing": "info",
               "queued": "mute", "running": "info", "done": "ok", "failed": "bad", "cancelled": "mute"}

# მიზეზი მარტივი ენით: (სათაური, რა უნდა გააკეთოს ოპერატორმა)
REASONS = {
    "unknown_type": ("ტიპი ვერ ამოიცნო", "გახსენი და ნახე, რა დოკუმენტია. თუ ახალი ტიპია, დაამატე პარამეტრებში."),
    "low_type_confidence": ("ტიპი სავარაუდოა", "შეამოწმე, სწორად არის თუ არა ამოცნობილი ტიპი."),
    "uncertain_boundary": ("გაურკვეველია, სად იწყება ან მთავრდება", "გახსენი წყარო და შეამოწმე, რომელი გვერდები ეკუთვნის დოკუმენტს."),
    "company_conflict": ("ორი კომპანიაა ნახსენები", "გადაწყვიტე, რომელ კომპანიას ეკუთვნის დოკუმენტი."),
    "company_unrecognized": ("კომპანია სიაში არ არის", "დაამატე კომპანია პარამეტრებში — შემდეგ ავტომატურად ამოიცნობა."),
    "multiple_employees": ("რამდენიმე თანამშრომელია", "დოკუმენტი ერთ თანამშრომელს უნდა ეკუთვნოდეს — შეამოწმე."),
    "possible_duplicate": ("შესაძლო დუბლიკატი", "შეადარე უკვე შენახულ დოკუმენტს. არსებული ფაილი არ შეცვლილა."),
    "mode_mismatch": ("ფაილში რამდენიმე დოკუმენტი ჩანს", "ეს ფაილი Single საქაღალდეში მოვიდა — შეიძლება Batch-ში უნდა ყოფილიყო."),
    "type_not_auto_enabled": ("ტიპი ავტომატურზე ჩართული არ არის", "ჩართე ავტომატური შენახვა ამ ტიპისთვის, როცა მზად იქნები."),
    "ocr_unreadable": ("ტექსტი ვერ წაიკითხა", "სკანი ცუდი ხარისხისაა. ხელახლა დაასკანერე ან ხელით გადაამოწმე."),
    "pages_not_in_mhr": ("ეს გვერდები MHR-ის ფაილებს არ ემთხვევა", "სკანის ეს ნაწილი თანამშრომლის არცერთ დაგენერირებულ ფაილს არ ჰგავს. ნახე, რა არის; თუ საჭიროა, ტიპი აურჩიე და ატვირთე ხელით."),
    "other": ("სხვა მიზეზი", "იხილე ტექნიკური დეტალები."),
}
FLAGS = {"year_unknown": "წელი უცნობია", "date_unknown": "თარიღი უცნობია", "signature_uncertain": "ხელმოწერა გაურკვეველია",
         "signature_not_detected": "ხელმოწერა ვერ აღმოჩნდა", "employee_unverified": "თანამშრომელი არ გადამოწმდა",
         "employee_api_unavailable": "API მიუწვდომელი იყო", "employee_name_candidates": "რამდენიმე შესაძლო სახელი",
         "personal_number_missing": "პირადი ნომერი ვერ ამოიკითხა", "personal_number_handwritten": "პირადი ნომერი ხელნაწერია",
         "company_default": "კომპანია ნაგულისხმევია", "amount_missing": "თანხა ვერ ამოიკითხა",
         "low_ocr_quality": "ტექსტის ხარისხი დაბალია", "api_company_differs": "API-ში სხვა კომპანიაა"}
SIGNATURE_KA = {"present": "არის", "not_detected": "ვერ აღმოჩნდა", "uncertain": "გაურკვეველი", "not_applicable": "—"}

_LOCAL_HOSTS = ("127.0.0.1", "localhost")

# --- იკონები (Lucide-ის სტილის, 24px, stroke) ---------------------------------------------
_ICON_PATHS = {
    "upload": '<path d="M12 16V4"/><path d="m6 10 6-6 6 6"/><path d="M4 20h16"/>',
    "home": '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/><path d="M10 21v-6h4v6"/>',
    "inbox": '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
    "file": '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/><path d="M8 13h8M8 17h5"/>',
    "layers": '<path d="m12 2 9 5-9 5-9-5 9-5z"/><path d="m3 12 9 5 9-5"/><path d="m3 17 9 5 9-5"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
    "check": '<circle cx="12" cy="12" r="10"/><path d="m8 12 3 3 5-6"/>',
    "alert": '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
    "x": '<circle cx="12" cy="12" r="10"/><path d="m15 9-6 6M9 9l6 6"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    "pause": '<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>',
    "play": '<path d="M6 4v16l14-8z"/>',
    "open": '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>',
    "theme": '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9z"/>',
    "refresh": '<path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/>',
    "scan": '<path d="M3 7V5a2 2 0 0 1 2-2h2M17 3h2a2 2 0 0 1 2 2v2M21 17v2a2 2 0 0 1-2 2h-2M7 21H5a2 2 0 0 1-2-2v-2"/><path d="M7 12h10"/>',
    "cloud": '<path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9z"/><path d="m9 15 3-3 3 3M12 12v7"/>',
    "shield": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
}


def icon(name: str, size: int = 20) -> str:
    return (f'<svg class="ic" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{_ICON_PATHS[name]}</svg>')


CSS = """
:root{color-scheme:light;--bg:#F8FAFC;--surface:#FFFFFF;--surface-2:#F1F5F9;--border:#E2E8F0;--text:#0F172A;--text-2:#475569;
--accent:#2563EB;--accent-ink:#FFFFFF;--accent-soft:#EFF6FF;--ok:#15803D;--ok-soft:#F0FDF4;--warn:#B45309;--warn-soft:#FFFBEB;
--bad:#B91C1C;--bad-soft:#FEF2F2;--mute:#64748B;--mute-soft:#F1F5F9;--shadow:0 1px 2px rgba(15,23,42,.05),0 1px 3px rgba(15,23,42,.06)}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#0B1120;--surface:#111827;--surface-2:#1E293B;
--border:#26324A;--text:#E5E7EB;--text-2:#94A3B8;--accent:#60A5FA;--accent-ink:#0B1120;--accent-soft:rgba(96,165,250,.12);--ok:#4ADE80;
--ok-soft:rgba(74,222,128,.10);--warn:#FBBF24;--warn-soft:rgba(251,191,36,.10);--bad:#F87171;--bad-soft:rgba(248,113,113,.10);
--mute:#94A3B8;--mute-soft:rgba(148,163,184,.10);--shadow:none}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#0B1120;--surface:#111827;--surface-2:#1E293B;--border:#26324A;--text:#E5E7EB;
--text-2:#94A3B8;--accent:#60A5FA;--accent-ink:#0B1120;--accent-soft:rgba(96,165,250,.12);--ok:#4ADE80;--ok-soft:rgba(74,222,128,.10);
--warn:#FBBF24;--warn-soft:rgba(251,191,36,.10);--bad:#F87171;--bad-soft:rgba(248,113,113,.10);--mute:#94A3B8;
--mute-soft:rgba(148,163,184,.10);--shadow:none}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;font:15px/1.55 "Segoe UI Variable Text","Segoe UI","Sylfaen",system-ui,sans-serif;background:var(--bg);color:var(--text)}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}
.ic{flex:none;vertical-align:-4px}
.layout{display:grid;grid-template-columns:248px minmax(0,1fr);min-height:100vh}
.side{position:sticky;top:0;height:100vh;display:flex;flex-direction:column;gap:6px;padding:20px 14px;background:var(--surface);
border-right:1px solid var(--border)}
.brand{display:flex;align-items:center;gap:10px;font-weight:650;font-size:16px;padding:4px 10px 18px}
.brand .logo{display:grid;place-items:center;width:34px;height:34px;border-radius:10px;background:var(--accent);color:var(--accent-ink)}
.nav a{display:flex;align-items:center;gap:12px;padding:10px 12px;border-radius:10px;color:var(--text-2);font-weight:500;min-height:42px}
.nav a:hover{background:var(--surface-2);color:var(--text);text-decoration:none}
.nav a[aria-current="page"]{background:var(--accent-soft);color:var(--accent)}
.nav .count{margin-left:auto;font-size:12px;font-weight:600;padding:1px 8px;border-radius:999px;background:var(--warn-soft);color:var(--warn)}
.side-foot{margin-top:auto;display:flex;flex-direction:column;gap:10px;padding:12px;border-radius:12px;background:var(--surface-2);font-size:13px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:var(--mute)}.dot.ok{background:var(--ok)}.dot.bad{background:var(--bad)}
.main{padding:32px clamp(16px,4vw,48px) 96px;max-width:1180px;width:100%}
h1{font-size:26px;line-height:1.25;margin:0 0 6px;font-weight:650;letter-spacing:-.01em}h2{font-size:17px;margin:28px 0 10px;font-weight:620;color:var(--text-2);text-transform:none;letter-spacing:.01em}
.sub{color:var(--text-2);margin:0 0 24px}
.hero{display:flex;gap:16px;align-items:center;padding:22px 24px;border-radius:16px;background:var(--surface);border:1px solid var(--border);box-shadow:var(--shadow);margin-bottom:20px}
.hero .big{display:grid;place-items:center;width:52px;height:52px;border-radius:14px;flex:none}
.hero h1{margin:0}.hero p{margin:2px 0 0;color:var(--text-2)}
.dochead{display:flex;justify-content:space-between;gap:16px;align-items:flex-end;flex-wrap:wrap;margin-bottom:18px}
.dochead h1{font-size:22px;margin:6px 0 2px}.dochead .sub{margin:0}
.next{padding:16px 18px;margin-bottom:14px;border-radius:14px;border:1px solid var(--border);border-left-width:4px;background:var(--surface);box-shadow:var(--shadow)}
.next.tone-ok{border-color:var(--ok);background:var(--ok-soft);color:var(--text)}.next.tone-warn{border-left-color:var(--warn);background:var(--surface);color:var(--text)}
.next.tone-bad{border-left-color:var(--bad);background:var(--surface);color:var(--text)}.next.tone-info{border-left-color:var(--accent);background:var(--surface);color:var(--text)}
.next .lbl{font-size:12.5px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--text-2);margin-bottom:6px}
.next .ttl{font-size:16px;font-weight:620;margin-bottom:4px}.next p{margin:0 0 12px;color:var(--text-2);font-size:14px}
.next .reasons{display:flex;flex-direction:column;gap:8px;margin:4px 0 12px}.next .manual{margin-top:4px}
.facts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px 16px;padding:16px 18px}
.facts div{min-width:0}.facts dt{color:var(--text-2);font-size:12.5px;margin-bottom:2px}.facts dd{margin:0;font-weight:560;overflow-wrap:anywhere}
.facts .wide{grid-column:1/-1}
.strip{display:flex;gap:8px 18px;flex-wrap:wrap;align-items:center;color:var(--text-2);font-size:13.5px;margin:-14px 0 22px}
.strip span{display:inline-flex;align-items:center;gap:6px}
.tasks{display:flex;flex-direction:column;gap:10px;margin-bottom:28px}
.task{display:flex;gap:16px;align-items:center;padding:16px 18px;border-radius:14px;background:var(--surface);border:1px solid var(--border);box-shadow:var(--shadow)}
.task .no{display:grid;place-items:center;width:44px;height:44px;border-radius:12px;flex:none;font-weight:650;font-size:17px}
.task .b{flex:1;min-width:0}.task .b b{display:block;font-size:16px;font-weight:620;line-height:1.3}.task .b span{color:var(--text-2);font-size:14px}
.task .act{flex:none}
.task.ok{border-color:transparent;background:var(--ok-soft)}.task.ok .b span{color:var(--text)}
.task.tone-bad{border-left:4px solid var(--bad);background:var(--surface);color:var(--text)}
.task.tone-warn{border-left:4px solid var(--warn);background:var(--surface);color:var(--text)}
.task.tone-info{border-left:4px solid var(--accent);background:var(--surface);color:var(--text)}
.task.tone-mute{border-left:4px solid var(--mute);background:var(--surface);color:var(--text)}
@media (max-width:640px){.task{flex-wrap:wrap}.task .act{width:100%}.task .act .btn{width:100%}}
.tone-ok{color:var(--ok);background:var(--ok-soft)}.tone-warn{color:var(--warn);background:var(--warn-soft)}
.tone-bad{color:var(--bad);background:var(--bad-soft)}.tone-info{color:var(--accent);background:var(--accent-soft)}.tone-mute{color:var(--mute);background:var(--mute-soft)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px}
.tile{display:block;padding:18px;border-radius:14px;background:var(--surface);border:1px solid var(--border);box-shadow:var(--shadow);color:var(--text);
transition:border-color .15s,transform .15s}a.tile:hover{border-color:var(--accent);text-decoration:none;transform:translateY(-1px)}
.tile .n{font-size:26px;font-weight:650;line-height:1.1;font-variant-numeric:tabular-nums;margin-top:8px}.tile .l{color:var(--text-2);font-size:13.5px}
.tile{padding:14px 16px}
.ib{display:inline-grid;place-items:center;width:34px;height:34px;border-radius:10px;flex:none}
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;box-shadow:var(--shadow)}
.list{display:flex;flex-direction:column}.row{display:flex;gap:14px;align-items:center;padding:14px 18px;border-top:1px solid var(--border)}
.row:first-child{border-top:0}.row .grow{flex:1;min-width:0}.row .t{font-weight:560;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row .m{color:var(--text-2);font-size:13px}
.chip{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;font-size:12.5px;font-weight:560;white-space:nowrap}
a.chip{cursor:pointer;transition:filter .15s}a.chip:hover{text-decoration:underline;filter:brightness(.94)}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:40px;padding:0 16px;border-radius:10px;border:1px solid var(--border);
background:var(--surface);color:var(--text);font:inherit;font-weight:560;cursor:pointer;transition:background .15s,border-color .15s}
.btn:hover{background:var(--surface-2);text-decoration:none}.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.btn.primary:hover{filter:brightness(1.07)}.btn.sm{min-height:36px;padding:0 12px;font-size:13.5px}.btn.ghost{border-color:transparent;background:transparent}
.btns{display:flex;gap:8px;flex-wrap:wrap}form.inline{display:inline}
.rcard{padding:18px 20px;display:flex;flex-direction:column;gap:12px}
.rcard .head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap}
.reason{display:flex;gap:10px;padding:10px 12px;border-radius:10px;background:var(--warn-soft)}.reason b{display:block;color:var(--warn);font-weight:600}
.reason span{color:var(--text-2);font-size:13.5px}
.grid-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:14px}
.toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:16px}
.search{display:flex;align-items:center;gap:8px;padding:0 12px;min-height:40px;border:1px solid var(--border);border-radius:10px;background:var(--surface);flex:1;min-width:220px}
.search input{border:0;outline:0;background:transparent;color:var(--text);font:inherit;flex:1;min-width:0}
.seg{display:inline-flex;padding:3px;border-radius:10px;background:var(--surface-2);gap:2px;flex-wrap:wrap}
.seg a{padding:6px 12px;border-radius:8px;color:var(--text-2);font-weight:560;font-size:13.5px}
.seg a[aria-current="true"]{background:var(--surface);color:var(--text);box-shadow:var(--shadow)}.seg a:hover{text-decoration:none;color:var(--text)}
select,input[type=text],input[type=password],input[type=number],textarea{font:inherit;color:var(--text);background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:8px 12px;min-height:40px}
textarea{width:100%;min-height:240px;font-family:Consolas,monospace;font-size:13px}
.pbar{height:8px;border-radius:999px;background:var(--surface-2);overflow:hidden}.pbar span{display:block;height:100%;background:var(--accent);border-radius:999px;transition:width .3s}
.stack{display:flex;height:8px;border-radius:999px;overflow:hidden;background:var(--surface-2);width:120px;flex:none}.stack span{display:block;height:100%}
.empty{padding:48px 20px;text-align:center;color:var(--text-2)}.empty .big{display:inline-grid;place-items:center;width:56px;height:56px;border-radius:16px;margin-bottom:12px}
.banner{display:flex;gap:12px;align-items:flex-start;padding:12px 16px;border-radius:12px;margin-bottom:16px;font-size:14px}
.kv{display:grid;grid-template-columns:minmax(120px,190px) 1fr;gap:8px 16px;margin:0}.kv dt{color:var(--text-2)}.kv dd{margin:0;font-weight:520}
.split{display:grid;grid-template-columns:minmax(300px,380px) 1fr;gap:20px;align-items:start}
.pdf{width:100%;height:78vh;border:1px solid var(--border);border-radius:14px;background:var(--surface-2)}
.bars .bl{display:grid;grid-template-columns:1fr 44px;gap:4px 10px;align-items:center;padding:6px 0}.bars .bl a{color:var(--text)}
.bars .track{grid-column:1/3;height:6px;border-radius:999px;background:var(--surface-2)}.bars .track span{display:block;height:100%;border-radius:999px;background:var(--accent)}
.section{padding:20px 22px;margin-bottom:16px}.section h2{margin:0 0 4px}.section .hint{color:var(--text-2);font-size:13.5px;margin:0 0 16px}
.field{display:flex;flex-direction:column;gap:6px;margin-bottom:16px}.field label{font-weight:560}.field .help{color:var(--text-2);font-size:13px}
.radio-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}
.radio-cards label{display:flex;gap:10px;padding:12px 14px;border:1px solid var(--border);border-radius:12px;cursor:pointer;font-weight:400}
.radio-cards label:has(input:checked){border-color:var(--accent);background:var(--accent-soft)}.radio-cards b{display:block;font-weight:600}
.switch{display:flex;gap:12px;align-items:flex-start;cursor:pointer}.switch input{width:20px;height:20px;margin-top:3px;accent-color:var(--accent)}
.co-row{display:grid;grid-template-columns:1.2fr 1.5fr;gap:8px;margin-bottom:8px}
details{border-top:1px solid var(--border);padding-top:14px}summary{cursor:pointer;font-weight:600;color:var(--text-2)}
.tbl{width:100%;border-collapse:collapse;font-size:14px}.tbl th,.tbl td{padding:10px 14px;border-top:1px solid var(--border);text-align:left;vertical-align:top}
.tbl th{color:var(--text-2);font-weight:560;border-top:0;font-size:13px}
.muted{color:var(--text-2)}.mono{font-family:Consolas,monospace;font-size:12.5px}
@media (max-width:900px){.layout{grid-template-columns:minmax(0,1fr)}.tiles{grid-template-columns:repeat(2,minmax(0,1fr))}.hero{padding:16px}.hero h1{font-size:20px}.grid-cards{grid-template-columns:minmax(0,1fr)}.side{position:fixed;inset:auto 0 0 0;top:auto;height:auto;flex-direction:row;padding:6px;
border-right:0;border-top:1px solid var(--border);z-index:10}.brand,.side-foot{display:none}.nav{display:flex;width:100%;justify-content:space-around}
.nav a{flex-direction:column;gap:2px;font-size:10.5px;padding:6px 2px;min-height:52px;flex:1 1 0;min-width:0;text-align:center;justify-content:center}.nav a span{max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.nav .count{display:none}
.split{grid-template-columns:1fr}.main{padding-top:20px}.co-row{grid-template-columns:1fr}.stack{display:none}}
.prob{padding:16px 18px;border-top:1px solid var(--border)}.prob:first-child{border-top:0}
.prob-h{display:flex;gap:12px;align-items:flex-start;margin-bottom:12px}.prob-h b{display:block;font-size:15.5px;font-weight:620;line-height:1.35}.prob-h .m{color:var(--text-2);font-size:13px}
.prob-b{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px;align-items:start}
.lines{display:flex;flex-direction:column;gap:10px;font-size:14px;min-width:0}.lines .lbl{display:block;color:var(--text-2);font-size:12.5px;font-weight:560;margin-bottom:1px}
.lines .val{overflow-wrap:anywhere}
mark.o{background:var(--ok-soft);color:var(--ok);font-weight:620;padding:0 3px;border-radius:4px}
mark.s{background:var(--bad-soft);color:var(--bad);font-weight:620;padding:0 3px;border-radius:4px}
.pgfig{position:relative;display:block;line-height:0;background:#fff;border:1px solid var(--border);border-radius:8px;overflow:hidden}.pgfig img{width:100%;display:block}
.hl{position:absolute;border:2px solid #DC2626;background:rgba(220,38,38,.14);border-radius:3px;pointer-events:none}
.hl.ok{border-color:#16A34A;background:rgba(22,163,74,.10)}.hl.warn{border-color:#D97706;background:rgba(217,119,6,.14)}
.pgmap{display:grid;grid-template-columns:repeat(auto-fill,minmax(168px,1fr));gap:12px}
.pgcard{display:flex;flex-direction:column;gap:6px;padding:8px;border:1px solid var(--border);border-radius:12px;background:var(--surface);color:var(--text);box-shadow:var(--shadow);font-size:12.5px;line-height:1.4}
.pgcard:hover{border-color:var(--accent);text-decoration:none}.pgcard.bad{border-color:var(--bad);border-width:2px}.pgcard.warn{border-color:var(--warn);border-width:2px}
.pgcard .cap{display:flex;justify-content:space-between;gap:6px;align-items:center;font-size:13.5px}.pgcard .m{color:var(--text-2)}
.pgview{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,1fr);gap:20px;align-items:start}
.steps{display:flex;flex-direction:column}.steps .st{display:flex;gap:12px;padding:12px 16px;border-top:1px solid var(--border);align-items:flex-start}.steps .st:first-child{border-top:0}
.steps .st b{display:block;font-weight:600}.steps .st span.d{color:var(--text-2);font-size:13.5px;overflow-wrap:anywhere}
@media (max-width:900px){.prob-b,.pgview{grid-template-columns:minmax(0,1fr)}}
.decide{padding:18px 20px;margin:0 0 24px;border-left:4px solid var(--accent)}.decide h2{margin:0 0 12px;color:var(--text);font-size:17px}
.ai-line{display:flex;gap:10px;align-items:flex-start;margin-bottom:14px}.ai-line p{margin:0;color:var(--text-2);font-size:14px}
.ai-line .chip{flex:none;margin-top:1px}
.override{display:flex;gap:16px;align-items:center;justify-content:space-between;flex-wrap:wrap;padding:14px 0;margin:0 0 14px;
border-top:1px solid var(--border);border-bottom:1px solid var(--border)}.override .switch{flex:1;min-width:260px}
details.pgroup{padding:0;margin:0 0 12px;border-top:1px solid var(--border)}
.pgroup>summary{display:flex;gap:12px;align-items:center;padding:14px 18px;list-style:none;color:var(--text);font-weight:400}
.pgroup>summary::-webkit-details-marker{display:none}.pgroup>summary:hover{background:var(--surface-2);border-radius:14px}
.pgroup>summary .grow{flex:1;min-width:0}.pgroup>summary b{display:block;font-size:15.5px;font-weight:620}
.pgroup>summary .m{color:var(--text-2);font-size:13px}
.chev{width:10px;height:10px;border-right:2px solid var(--text-2);border-bottom:2px solid var(--text-2);transform:rotate(45deg);transition:transform .15s;flex:none;margin-right:4px}
.pgroup[open]>summary .chev{transform:rotate(-135deg)}.pgroup>.list{border-top:1px solid var(--border)}
.gintro{padding:2px 18px 14px 64px}.prob-h .place{font-size:14px;font-weight:600;color:var(--text-2)}
@media (max-width:640px){.gintro{padding-left:18px}.ai-line{flex-direction:column;gap:6px}.decide{padding:16px}}
.tile .w{color:var(--text-2);font-size:12.5px;margin-top:6px;padding-top:6px;border-top:1px solid var(--border)}
.flow{padding:16px 18px}.steps-row{display:flex;align-items:stretch;gap:6px;flex-wrap:wrap}
.step{flex:1 1 120px;display:flex;flex-direction:column;gap:4px;padding:10px 12px;border-radius:12px;color:var(--text);min-width:110px}
.step:hover{background:var(--surface-2);text-decoration:none}.step .n{font-size:24px;font-weight:650;line-height:1.1;font-variant-numeric:tabular-nums}
.step .l{color:var(--text-2);font-size:13px}.step.zero .n{color:var(--text-2);font-weight:500}
.arrow{align-self:center;color:var(--text-2);font-size:18px;flex:none}
.jam{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:12px;padding-top:12px;border-top:1px solid var(--border)}
.jam .step{flex:0 1 auto;flex-direction:row;align-items:center;gap:8px;padding:6px 12px}.jam .step .n{font-size:17px}
.jam-l{font-size:12.5px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--text-2);margin-right:6px}
.ok-line{display:inline-flex;gap:6px;align-items:center;color:var(--ok);font-size:14px}
.day{font-size:13px;font-weight:600;color:var(--text-2);margin:14px 2px 6px}.when{color:var(--text-2);font-size:13px;font-variant-numeric:tabular-nums;flex:none}
@media (max-width:640px){.arrow{display:none}.step{flex:1 1 40%}.when{display:none}
.recent .row{flex-wrap:wrap;row-gap:6px}.recent .row .grow{flex:1 1 calc(100% - 60px)}.recent .row .chip{margin-left:48px}}
.nopdf{display:grid;place-items:center;height:auto;min-height:320px}
.clamp2{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

JS = """
(function(){var k='hrs-theme';try{var t=localStorage.getItem(k);if(t)document.documentElement.dataset.theme=t;}catch(e){}
window.toggleTheme=function(){var d=document.documentElement,c=d.dataset.theme,
dark=c?c==='dark':matchMedia('(prefers-color-scheme: dark)').matches,n=dark?'light':'dark';d.dataset.theme=n;try{localStorage.setItem(k,n);}catch(e){}};
var w=document.body.dataset.watch;if(w){var last=null;setInterval(function(){fetch('/api/status').then(function(r){return r.json()}).then(function(s){
var j=JSON.stringify(s);if(last!==null&&j!==last)location.reload();last=j;}).catch(function(){});},5000);}})();
"""


def _is_local_origin(origin: str, port: int) -> bool:
    """Origin/Referer ზუსტად ჩვენი UI-ია (არა `http://127.0.0.1.evil.example`)."""
    try:
        u = urlsplit(origin)
        return u.scheme == "http" and u.hostname in _LOCAL_HOSTS and (u.port or 80) == port
    except ValueError:
        return False


def e(v) -> str:
    return html.escape("" if v is None else str(v))


def chip(text: str, tone: str = "mute") -> str:
    return f'<span class="chip tone-{tone}">{e(text)}</span>'


def status_chip(status: str) -> str:
    return chip(STATUS_KA.get(status, status), STATUS_TONE.get(status, "mute"))


def ago(iso: str | None) -> str:
    """„5 წთ წინ" — ადამიანისთვის გასაგები დრო."""
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    s = (datetime.now(timezone.utc) - dt).total_seconds()
    if s < 60:
        return "ახლახან"
    if s < 3600:
        return f"{int(s // 60)} წთ წინ"
    if s < 86400:
        return f"{int(s // 3600)} სთ წინ"
    return f"{int(s // 86400)} დღის წინ"


def _seconds(start: str | None, end: str | None) -> float | None:
    try:
        return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    except (TypeError, ValueError):
        return None


_NAV = [("/", "home", "მთავარი"), ("/review", "inbox", "გადასამოწმებელი"), ("/documents", "file", "დოკუმენტები"),
        ("/stats", "layers", "ფაილები"), ("/settings", "settings", "პარამეტრები")]


def page(title: str, body: str, active: str = "", side: dict | None = None, watch: bool = False) -> HTMLResponse:
    side = side or {}
    items = list(_NAV)
    if side.get("mhr"):
        items.insert(4, ("/mhr", "cloud", "MHR"))
        items.insert(5, ("/manual", "upload", "ხელით ატვირთვა"))
    badge = {"/review": side.get("review"), "/mhr": side.get("mhr_ready")}
    nav = "".join(
        f'<a href="{href}"{" aria-current=page" if href == active else ""}>{icon(ic)}<span>{e(lbl)}</span>'
        + (f'<span class="count">{badge[href]}</span>' if badge.get(href) else "") + "</a>"
        for href, ic, lbl in items)
    alive, paused = side.get("alive"), side.get("paused")
    state = ("<span class='dot'></span>შეჩერებულია" if paused else
             "<span class='dot ok'></span>მუშაობს" if alive else "<span class='dot bad'></span>ფონური პროცესი გაჩერებულია")
    ctl = (f'<form class="inline" method="post" action="/control/resume"><button class="btn sm primary">{icon("play", 16)}გაგრძელება</button></form>'
           if paused else
           f'<form class="inline" method="post" action="/control/pause"><button class="btn sm">{icon("pause", 16)}შეჩერება</button></form>')
    foot = (f'<div class="side-foot"><div>{state}</div>{ctl}'
            f'<button class="btn sm ghost" type="button" onclick="toggleTheme()" aria-label="თემის შეცვლა">{icon("theme", 16)}თემა</button></div>')
    return HTMLResponse(
        f'<!doctype html><html lang="ka"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{e(title)} · HR გადამრჩევი</title><style>{CSS}</style></head>'
        f'<body{" data-watch=1" if watch else ""}><div class="layout"><aside class="side">'
        f'<div class="brand"><span class="logo">{icon("scan", 20)}</span>HR გადამრჩევი</div>'
        f'<nav class="nav" aria-label="მთავარი მენიუ">{nav}</nav>{foot}</aside>'
        f'<main class="main">{body}</main></div><script>{JS}</script></body></html>')


def empty(ic: str, tone: str, title: str, text: str = "") -> str:
    return (f'<div class="empty"><span class="big tone-{tone}">{icon(ic, 26)}</span><div style="font-weight:600;color:var(--text)">'
            f'{e(title)}</div><div>{e(text)}</div></div>')


def create_app(config_path: str | None = None) -> FastAPI:
    app = FastAPI(title="HR Sorter", docs_url=None, redoc_url=None, openapi_url=None)

    def cfg() -> Config:
        return load_config(config_path)

    def repo() -> Repository:
        return Repository(cfg().paths.db)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # DNS-rebinding/CSRF დაცვა: მხოლოდ ლოკალური Host, POST-ზე — ლოკალური Origin
        host = (request.headers.get("host") or "").split(":")[0]
        if host not in _LOCAL_HOSTS:
            return HTMLResponse("forbidden host", status_code=403)
        if request.method != "GET":
            if request.headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
                return HTMLResponse("forbidden origin", status_code=403)
            origin = request.headers.get("origin") or request.headers.get("referer") or ""
            if not _is_local_origin(origin, int(cfg().section("ui")["port"])):
                return HTMLResponse("forbidden origin", status_code=403)
        return await call_next(request)

    async def form(request: Request) -> dict[str, str]:
        raw = (await request.body()).decode("utf-8")
        return {k: v[-1] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    def status_data() -> dict:
        r = repo()
        try:
            hb = r.get_control("worker_heartbeat")
            alive = bool(hb) and time.time() - float(hb) < 30
            cur = r.one("SELECT j.stage, j.progress_done, j.progress_total, s.first_path FROM jobs j JOIN sources s ON s.id=j.source_id"
                        " WHERE j.status='running' ORDER BY j.updated_at DESC LIMIT 1")
            return {
                "paused": r.is_paused(), "worker_alive": alive,
                "jobs": {x["status"]: x["n"] for x in r.q("SELECT status, COUNT(*) n FROM jobs GROUP BY status")},
                "documents": {x["status"]: x["n"] for x in
                              r.q("SELECT status, COUNT(*) n FROM documents WHERE active=1 GROUP BY status")},
                "inbox_waiting": r.one("SELECT COUNT(*) n FROM inbox_files WHERE state='pending'")["n"],
                "inbox_errors": r.one("SELECT COUNT(*) n FROM inbox_files WHERE state='error'")["n"],
                "current": ({"file": Path(cur["first_path"]).name, "stage": cur["stage"], "done": cur["progress_done"],
                             "total": cur["progress_total"]} if cur else None),
                "mhr_enabled": bool(cfg().section("mhr")["enabled"]),
                "mhr": {x["status"]: x["n"] for x in r.q("SELECT status, COUNT(*) n FROM mhr_items GROUP BY status")},
            }
        finally:
            r.close()

    def side_info(s: dict) -> dict:
        return {"alive": s["worker_alive"], "paused": s["paused"], "review": s["documents"].get("needs_review", 0),
                "mhr": s["mhr_enabled"], "mhr_ready": s["mhr"].get("ready", 0)}

    @app.get("/api/status")
    def api_status():
        return JSONResponse(status_data())

    # --- მთავარი ------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def home():
        c = cfg()
        s = status_data()
        d, j = s["documents"], s["jobs"]
        review = d.get("needs_review", 0)
        errors = j.get("failed", 0) + s["inbox_errors"]
        busy = j.get("queued", 0) + j.get("running", 0) + s["inbox_waiting"]
        trial = c.section("processing")["trial_mode"]

        m = s["mhr"] if s["mhr_enabled"] else {}
        mhr_stuck = m.get("upload_failed", 0) + m.get("needs_check", 0)
        cur = s["current"]

        # ამოცანები პრიორიტეტით: (ტონი, სათაური, ახსნა, მოქმედება) — მოქმედება: (ბმული|form-action, წარწერა, primary?)
        tasks: list[tuple[str, str, str, tuple | None]] = []
        if s["paused"]:
            tasks.append(("mute", "დამუშავება შეჩერებულია", "ახალი სკანები რიგში გროვდება და არ მუშავდება.",
                          ("post:/control/resume", "გაგრძელება", True)))
        elif not s["worker_alive"]:
            tasks.append(("bad", "ფონური პროცესი არ მუშაობს", "გახსენი „HR გადამრჩევი“ დაწყების მენიუდან ან გადატვირთე კომპიუტერი.", None))
        if errors:
            tasks.append(("bad", f"{errors} ფაილი ვერ დამუშავდა", "ნახე, სად გაიჭედა, და გაუშვი თავიდან.", ("/stats#stuck", "სად გაიჭედა", True)))
        if review:
            tasks.append(("warn", f"{review} დოკუმენტი გადასამოწმებელია", "პროგრამა ვერ დარწმუნდა — თითოეულზე წერია, რატომ და რა გააკეთო.",
                          ("/review", "გადახედვა", True)))
        if m.get("failed"):
            tasks.append(("warn", f"{m['failed']} ფაილმა ვერ გაიარა MHR-ის შემოწმება",
                          "რაღაც არ ემთხვევა, ან MHR-ში ფაილი ვერ მოიძებნა. ნახე რატომ; თუ დოკუმენტი სწორია, ატვირთე მაინც.",
                          ("/mhr?tab=failed", "ნახე რატომ", True)))
        if mhr_stuck:
            tasks.append(("warn", f"{mhr_stuck} ატვირთვა გასარკვევია", "ატვირთვა ვერ მოხერხდა ან შედეგი გაურკვეველია — შეამოწმე MHR-ში.",
                          ("/mhr?tab=other", "ნახე", False)))
        if m.get("ready"):
            tasks.append(("info", f"{m['ready']} ფაილი მზადაა MHR-ზე ასატვირთად", "ყველაფერი ემთხვევა — ერთი ღილაკით აიტვირთება თანამშრომლის ბარათზე.",
                          ("post:/mhr-approve-all", f"ყველას ატვირთვა ({m['ready']})", True)))
        if cur or (busy and not s["paused"]):
            txt = (f"{cur['file']} — {cur['done']}/{cur['total']} გვერდი" if cur else f"რიგში {busy} ფაილი") + ". ჩარევა არ სჭირდება."
            tasks.append(("info", "მუშავდება", txt, None))

        def task_html(n: int, tone: str, title: str, text: str, act: tuple | None) -> str:
            btn = ""
            if act:
                href, lbl, primary = act
                cls = "btn primary" if primary else "btn"
                btn = (f'<form method="post" action="{e(href[5:])}" class="act"><button class="{cls}">{e(lbl)}</button></form>'
                       if href.startswith("post:") else f'<a class="{cls} act" href="{e(href)}">{e(lbl)}</a>')
            return (f'<div class="task tone-{tone}"><span class="no tone-{tone}">{n}</span><div class="b"><b>{e(title)}</b>'
                    f'<span>{e(text)}</span></div>{btn}</div>')

        if tasks:
            out = [f'<h1>რა გავაკეთო ახლა</h1><p class="sub">{len(tasks)} რამ ელოდება — თანმიმდევრობით.</p>'
                   + '<div class="tasks" id="todo">' + "".join(task_html(n, *t) for n, t in enumerate(tasks, 1)) + "</div>"]
        else:
            out = ['<h1>რა გავაკეთო ახლა</h1><p class="sub">არაფერი — ყველაფერი რიგზეა.</p>'
                   f'<div class="tasks"><div class="task ok"><span class="no tone-ok">{icon("check", 22)}</span><div class="b">'
                   '<b>ყველაფერი რიგზეა</b><span>ახალი სკანი Inbox საქაღალდეში ავტომატურად დამუშავდება. აქ გამოჩნდება, თუ რამე შენს გადაწყვეტილებას დასჭირდება.</span></div></div></div>']
        prog = ""
        if cur and cur["total"]:
            pct = 100 * cur["done"] / max(1, cur["total"])
            prog = f'<div class="pbar" style="margin:-16px 0 22px;max-width:420px"><span style="width:{pct:.0f}%"></span></div>'
        out.append(prog)
        state = ("<span class='dot'></span>შეჩერებულია" if s["paused"] else
                 "<span class='dot ok'></span>მუშაობს" if s["worker_alive"] else "<span class='dot bad'></span>გაჩერებულია")
        strip = [f"<span>{state}</span>", f"<span>{icon('clock', 14)}რიგში {busy}</span>"]
        if trial:
            strip.append(f"<span title='შედეგები Proposed საქაღალდეშია — საბოლოოდ არაფერი ინაცვლებს, სანამ არ ჩართავ'>{icon('shield', 14)}საცდელი რეჟიმი</span>")
        strip.append(f"<span>{icon('cloud', 14)}MHR {'ჩართულია' if s['mhr_enabled'] else 'გამორთულია'}</span>")
        out.append('<div class="strip">' + "".join(strip) + "</div>")

        r = repo()
        try:
            today = _home_today(r)
            files = _recent_files(r, c, limit=12)
        finally:
            r.close()
        waiting_you = review + errors + m.get("failed", 0) + mhr_stuck
        out.append('<h2>დღეს</h2><div class="tiles today">' + "".join(
            f'<a class="tile" href="{href}"><span class="ib tone-{t}">{icon(i, 18)}</span><div class="n">{n}</div><div class="l">{e(lbl)}</div>'
            f'<div class="w">{e(week)}</div></a>'
            for href, i, t, n, lbl, week in [
                ("/stats", "scan", "info", today["files"], f'ფაილი დასკანერდა · {today["pages"]} გვერდი',
                 f'7 დღეში: {today["files_week"]} ფაილი, {today["pages_week"]} გვერდი'),
                ("/documents", "file", "mute", today["docs"], "დოკუმენტი გამოიყო", f'7 დღეში: {today["docs_week"]}'),
                *([("/mhr?tab=done", "cloud", "ok", today["uploaded"], "აიტვირთა MHR-ზე", f'7 დღეში: {today["uploaded_week"]}')]
                  if s["mhr_enabled"] else []),
                ("#todo" if waiting_you else "/", "alert", "warn" if waiting_you else "ok", waiting_you, "შენ გელოდება",
                 "გადასამოწმებელი, ვერ გაიარა, შეცდომა" if waiting_you else "არაფერი")]) + "</div>")
        out.append('<h2>სად რა არის</h2>' + _home_flow(s, trial))
        out.append('<h2>ბოლო სკანები</h2>' + (_recent_html(files) or
                   f'<div class="card list">{empty("scan", "info", "ჯერ არაფერი შემოსულა", "დაასკანერე დოკუმენტი Inbox საქაღალდეში.")}</div>')
                   + '<p style="margin:10px 2px 0"><a href="/stats">ყველა ფაილი →</a></p>')
        out = [x for x in out if x]
        return page("მთავარი", "".join(out), "/", side_info(s), watch=True)

    @app.post("/control/{action}")
    def control(action: str, request: Request):
        if action not in ("pause", "resume"):
            raise HTTPException(404)
        r = repo()
        try:
            r.set_control("paused", "1" if action == "pause" else "0")
            r.log_event("control", f"processing {action}d from UI")
        finally:
            r.close()
        back = urlsplit(request.headers.get("referer") or "/").path or "/"
        return RedirectResponse(back if back.startswith("/") else "/", status_code=303)

    # --- გადასამოწმებელი ------------------------------------------------------------
    @app.get("/review", response_class=HTMLResponse)
    def review():
        c = cfg()
        r = repo()
        try:
            rows = r.q("SELECT d.*, s.first_path FROM documents d JOIN sources s ON s.id=d.source_id"
                       " WHERE d.active=1 AND d.status='needs_review' ORDER BY d.created_at DESC LIMIT 300")
        finally:
            r.close()
        cards = "".join(_review_card(c, x) for x in rows)
        body = (f'<h1>გადასამოწმებელი</h1><p class="sub">ამ დოკუმენტებში პროგრამა ვერ დარწმუნდა. თითოეულზე წერია, რატომ და რა უნდა გააკეთო.</p>'
                + (f'<div style="display:flex;flex-direction:column;gap:10px">{cards}</div>' if cards else
                   '<div class="card">' + empty("check", "ok", "გადასამოწმებელი არაფერია", "ყველა დოკუმენტი სანდოდ დამუშავდა.") + "</div>"))
        return page("გადასამოწმებელი", body, "/review", side_info(status_data()), watch=True)

    # --- დოკუმენტები ----------------------------------------------------------------
    @app.get("/documents", response_class=HTMLResponse)
    def documents(status: str = "", type: str = "", company: str = "", employee: str = "", reason: str = "",
                  source: str = "", year: str = ""):
        c = cfg()
        where, args = ["d.active=1"], []
        if source:
            where.append("d.source_id=?"); args.append(source)
        if year:
            where.append("d.year IS NULL" if year == "none" else "d.year=?")
            if year != "none":
                args.append(year)
        if status:
            where.append("d.status=?"); args.append(status)
        if type:
            where.append("d.type_id=?"); args.append(type)
        if company:
            where.append("d.company_id=?"); args.append(company)
        if employee:
            where.append("(d.personal_number LIKE ? OR d.employee_id LIKE ? OR d.metadata LIKE ?)")
            args += [f"%{employee}%"] * 3
        if reason:
            where.append("d.review_reasons LIKE ?"); args.append(f'%"{reason}"%')
        r = repo()
        try:
            rows = r.q(f"SELECT d.*, s.first_path FROM documents d JOIN sources s ON s.id=d.source_id WHERE {' AND '.join(where)}"
                       " ORDER BY d.created_at DESC LIMIT 500", tuple(args))
        finally:
            r.close()
        keep = {k: v for k, v in dict(type=type, company=company, employee=employee, reason=reason, source=source, year=year).items() if v}
        seg = "".join(f'<a href="/documents?{urlencode({**keep, **({"status": k} if k else {})})}"'
                      f'{" aria-current=true" if status == k else ""}>{e(lbl)}</a>'
                      for k, lbl in [("", "ყველა"), ("auto_saved", "დალაგდა"), ("proposed", "შემოთავაზებული"), ("needs_review", "გადასამოწმებელი")])

        def opt(name, value, options, all_lbl):
            o = "".join(f'<option value="{e(k)}" {"selected" if str(k) == value else ""}>{e(v)}</option>' for k, v in options)
            return f'<select name="{name}" aria-label="{e(all_lbl)}" onchange="this.form.submit()"><option value="">{e(all_lbl)}</option>{o}</select>'
        hidden = "".join(f'<input type="hidden" name="{k}" value="{e(v)}">' for k, v in dict(status=status, source=source, reason=reason).items() if v)
        toolbar = (f'<form class="toolbar" method="get">{hidden}<label class="search">{icon("search", 18)}'
                   f'<input name="employee" placeholder="ძიება: პირადი ნომერი ან სახელი" value="{e(employee)}" aria-label="ძიება"></label>'
                   + opt("type", type, [(t.id, t.name_ka) for t in c.types if t.active], "ყველა ტიპი")
                   + opt("company", company, [(x.id, x.name) for x in c.companies], "ყველა კომპანია")
                   + opt("year", year, [("none", "წელი უცნობია")] + [(y, y) for y in range(datetime.now().year, 1999, -1)], "ყველა წელი")
                   + '<button class="btn">ძიება</button></form>')
        filt = ""
        if source and rows:
            filt = f'<div class="banner tone-mute">{icon("file", 18)}<div>ნაჩვენებია ფაილიდან: <b>{e(Path(rows[0]["first_path"]).name)}</b> · <a href="/documents">ყველას ჩვენება</a></div></div>'
        if reason:
            filt += f'<div class="banner tone-warn">{icon("alert", 18)}<div>მიზეზი: <b>{e(REASONS.get(reason, (reason,))[0])}</b> · <a href="/documents">გასუფთავება</a></div></div>'
        items = "".join(_doc_row(c, x) for x in rows)
        body = (f'<h1>დოკუმენტები</h1><p class="sub">ყველა დამუშავებული დოკუმენტი ({len(rows)}).</p>'
                f'<div class="toolbar"><div class="seg" role="tablist">{seg}</div></div>{toolbar}{filt}'
                f'<div class="card list">{items or empty("search", "mute", "არაფერი მოიძებნა", "შეცვალე ფილტრი.")}</div>')
        return page("დოკუმენტები", body, "/documents" if status != "needs_review" else "/review", side_info(status_data()))

    @app.get("/doc/{doc_id}", response_class=HTMLResponse)
    def doc_detail(doc_id: str, msg: str = "", err: str = ""):
        c = cfg()
        r = repo()
        try:
            x = r.one("SELECT d.*, s.first_path FROM documents d JOIN sources s ON s.id=d.source_id WHERE d.id=?", (doc_id,))
            mhr_item = r.mhr_get(doc_id)
            page_rows = _file_pages(r, x) if x else []
            parent = r.mhr_get(x["part_of"]) if x and x["part_of"] else None
        finally:
            r.close()
        if not x:
            raise HTTPException(404)
        meta = json.loads(x["metadata"] or "{}")
        mhr_on = bool(c.section("mhr")["enabled"])
        t = c.doc_type(x["type_id"] or "")
        comp = c.company(x["company_id"] or "") if x["company_id"] else None
        f = meta.get("fields", {})
        name = " ".join(v for v in ((f.get("first_name") or {}).get("value"), (f.get("last_name") or {}).get("value")) if v)
        reasons = json.loads(x["review_reasons"] or "[]")
        kv = [("ტიპი", t.name_ka if t else x["type_id"]), ("კომპანია", comp.name if comp else (x["company_id"] or "—")),
              ("თანამშრომელი", name or "—"), ("პირადი ნომერი", x["personal_number"] or "—"),
              ("თარიღი", x["document_date"] or "უცნობია"), ("წელი", x["year"] or "უცნობია"),
              ("ხელმოწერა", SIGNATURE_KA.get(meta.get("signature_status", ""), "—")),
              ("წყარო", f'{Path(x["first_path"]).name}, გვ. {x["page_start"] + 1}–{x["page_end"] + 1}')]
        if mhr_on:   # თანამშრომელს MHR ამოწმებს — ცალკე „თანამშრომლების API“-ს გაფრთხილება აქ დამაბნეველია
            kv.insert(4, ("MHR", _mhr_doc_status(mhr_item)))
            ai = _ai_load(mhr_item) if mhr_item else None
            if ai and ai.get("verdict") in AI_KA and ai.get("verdict") != "error":
                kv.insert(5, ("AI ანალიზი", f'{AI_KA[ai["verdict"]][0]} — {ai.get("summary") or ""}'))
        rs = "".join(f'<div class="reason">{icon("alert", 18)}<div><b>{e(REASONS.get(k, (k, ""))[0])}</b><span>{e(REASONS.get(k, ("", ""))[1])}</span>'
                     + "".join(f'<span style="display:block;margin-top:4px;color:var(--text)">{d_line}</span>' for d_line in _reason_where(k, meta, page_rows, parent)) + "</div></div>"
                     for k in reasons)
        flags = "".join(chip(FLAGS.get(fl, fl), "mute") for fl in meta.get("flags", [])
                        if not (mhr_on and fl == "employee_unverified"))
        nxt = _doc_next(c, x, mhr_item, rs, mhr_on)
        has_pdf = bool(x["output_path"]) and Path(x["output_path"]).exists()
        viewer = (f'<iframe class="pdf" src="/file/output/{e(doc_id)}" title="დოკუმენტის გადახედვა"></iframe>' if has_pdf else
                  f'<div class="card pdf nopdf">{empty("file", "mute", "PDF ამ კომპიუტერზე აღარ არის", "ფაილი გადატანილია ან ვადის გასვლის გამო წაიშალა. ორიგინალი სკანი შეიძლება ისევ იყოს „წყაროში“.")}</div>')
        skip = {"—", "უცნობია", "", None}
        facts = "".join(f'<div{" class=wide" if k in ("MHR", "AI ანალიზი") else ""}><dt>{e(k)}</dt><dd>{e(v)}</dd></div>'
                        for k, v in kv if k != "წყარო" and v not in skip and not (k == "წელი" and x["document_date"]))
        head = (f'<p style="margin:0 0 10px"><a href="javascript:history.back()">← უკან</a></p>{_flash(msg, err)}'
                f'<div class="dochead"><div><div class="chips">{status_chip(x["status"])}{mhr_chip(mhr_item["status"]) if mhr_item else ""}</div>'
                f'<h1>{e((meta.get("part") or {}).get("title") or (t.name_ka if t else "დოკუმენტი"))}{" · " + e(name) if name else ""}</h1>'
                f'<p class="sub">{e(Path(x["first_path"]).name)} · გვ. {x["page_start"] + 1}–{x["page_end"] + 1} · {e(x["document_date"] or "თარიღი უცნობია")}</p></div>'
                f'<div class="btns">'
                + (f'<a class="btn" href="/file/output/{e(doc_id)}" target="_blank">{icon("open", 16)}PDF ცალკე</a>' if has_pdf else "")
                + f''
                f'<a class="btn" href="/file/source/{e(doc_id)}#page={x["page_start"] + 1}" target="_blank">{icon("file", 16)}წყარო</a>'
                f'<a class="btn" href="/doc/{e(doc_id)}/text">{icon("search", 16)}ამოკითხული ტექსტი</a></div></div>')
        body = (head + f'<div class="split"><div>{nxt}<dl class="card facts" style="margin:0 0 14px">{facts}</dl>'
                + (f'<div class="chips" style="margin-bottom:14px">{flags}</div>' if flags else "")
                + f'<details style="border:0;padding:0"><summary style="font-size:13.5px;font-weight:500">სხვა მოქმედებები და ტექნიკური დეტალები</summary>'
                f'<div class="btns" style="margin:12px 0">'
                + (f'<a class="btn sm" href="/mhr/{e(doc_id)}">{icon("cloud", 16)}MHR შედარება</a>' if mhr_item else "")
                + f'<form class="inline" method="post" action="/jobs/{e(x["job_id"])}/reprocess"><button class="btn sm">{icon("refresh", 16)}თავიდან დამუშავება</button></form></div>'
                f'<p class="muted mono">{e(x["output_path"])}</p>'
                f'<pre class="mono" style="white-space:pre-wrap;word-break:break-word">{e(json.dumps(meta, ensure_ascii=False, indent=2))}</pre></details></div>'
                f'{viewer}</div>')
        return page("დოკუმენტი", body, "/review" if x["status"] == "needs_review" else "/documents", side_info(status_data()))

    @app.get("/mhr", response_class=HTMLResponse)
    def mhr_list(tab: str = "ready"):
        c = cfg()
        tab = tab if tab in _MHR_TABS else "ready"
        r = repo()
        try:
            counts = {x["status"]: x["n"] for x in r.q("SELECT status, COUNT(*) n FROM mhr_items GROUP BY status")}
            items = r.q(f"SELECT m.*, d.type_id, d.page_start, d.page_end, s.first_path FROM mhr_items m"
                        f" LEFT JOIN documents d ON d.id=m.document_id LEFT JOIN sources s ON s.id=d.source_id"
                        f" WHERE m.status IN ({','.join('?' for _ in _MHR_TABS[tab][1])}) ORDER BY m.updated_at DESC LIMIT 300",
                        _MHR_TABS[tab][1])
        finally:
            r.close()
        seg = "".join(f'<a href="/mhr?tab={k}"{" aria-current=true" if k == tab else ""}>{e(lbl)} ({sum(counts.get(x, 0) for x in st)})</a>'
                      for k, (lbl, st) in _MHR_TABS.items())
        rows = "".join(_mhr_row(c, x, back=f"/mhr?tab={tab}") for x in items)
        bulk = (f'<form method="post" action="/mhr-approve-all" style="margin-left:auto"><button class="btn primary">{icon("cloud", 16)}'
                f'ყველას ატვირთვა ({counts.get("ready", 0)})</button></form>' if tab == "ready" and counts.get("ready") else "")
        off = ("" if c.section("mhr")["enabled"] else
               f'<div class="banner tone-warn">{icon("alert", 18)}<div>MHR ინტეგრაცია გამორთულია — ჩართე „პარამეტრებში“.</div></div>')
        tip = {"ready": "ყველაფერი ემთხვევა — „ატვირთვა“ ან „ყველას ატვირთვა“, და დოკუმენტი თანამშრომლის ბარათზე აიტვირთება.",
               "failed": "რაღაც არ ემთხვევა ან შედარება ვერ მოხერხდა — „დეტალები“ აჩვენებს, ზუსტად რა. "
                         "დარწმუნებული რომ ხარ — „ატვირთვა მაინც“ ან „ხელით ატვირთვა“.",
               "queue": "ელოდება შემოწმებას ან ატვირთვას — ჩარევა არ სჭირდება.",
               "done": "უკვე MHR-შია.", "other": "გამოტოვებული, ვერ ატვირთული ან უარყოფილი — თითოეულზე წერია, რატომ."}[tab]
        body = (f'<h1>MHR</h1><p class="sub">{e(tip)}</p>{off}'
                '<details style="border:0;padding:0;margin:-12px 0 18px"><summary style="font-size:13.5px;font-weight:500">როგორ მოწმდება?</summary>'
                '<p class="muted" style="font-size:13.5px;margin:6px 0 0">ყოველი დასკანერებული ხელშეკრულება ავტომატურად მოწმდება: თანამშრომელი MHR-ში '
                'პირადი ნომრით, პირადი მონაცემები, ხელმოწერები ყველა გვერდზე და ტექსტი — MHR-ში დაგენერირებულ ხელშეკრულებასთან. '
                'თუ ბარათზე დაგენერირებული ხელშეკრულება არ არის, შედარება არ ხდება და დოკუმენტი აქ გაფრთხილებით რჩება — '
                'ქაღალდზე ნახვის შემდეგ „ხელით ატვირთვა“ მაინც მუშაობს. ატვირთვა მხოლოდ შენი დადასტურებით ხდება.</p></details>'
                f'<div class="toolbar"><div class="seg">{seg}</div><a class="btn" href="/manual">{icon("upload", 16)}ხელით ატვირთვა</a>{bulk}</div>'
                f'<div class="card list">{rows or empty("cloud", "mute", "აქ ჯერ არაფერია")}</div>')
        return page("MHR", body, "/mhr", side_info(status_data()), watch=True)

    @app.get("/mhr/{doc_id}", response_class=HTMLResponse)
    def mhr_detail(doc_id: str, msg: str = "", err: str = ""):
        c = cfg()
        r = repo()
        try:
            m = r.mhr_get(doc_id)
            d = r.one("SELECT d.*, s.first_path FROM documents d JOIN sources s ON s.id=d.source_id WHERE d.id=?", (doc_id,))
            pts = _file_pages(r, d) if d else []
        finally:
            r.close()
        if not m:
            raise HTTPException(404)
        s = status_data()
        return page("MHR — შედარება", _flash(msg, err) + _mhr_detail_body(c, m, d, s["worker_alive"], pts, _part_states(doc_id, repo)), "/mhr", side_info(s))

    @app.get("/mhr/{doc_id}/page/{n}", response_class=HTMLResponse)
    def mhr_page(doc_id: str, n: int):
        """ფაილის ერთი გვერდი დიდად: მონიშნული ადგილები და ყველაფერი, რაც ამ გვერდზე ნახა პროგრამამ."""
        c = cfg()
        r = repo()
        try:
            m = r.mhr_get(doc_id)
            d = r.get_document(doc_id)
            pts = _file_pages(r, d) if d else []
        finally:
            r.close()
        if not m or not d or not 1 <= n <= len(pts):
            raise HTTPException(404)
        return page(f"გვერდი {n}", _page_view(c, m, d, pts, n), "/mhr", side_info(status_data()))

    @app.post("/mhr/{doc_id}/override")
    async def mhr_override(doc_id: str, request: Request):
        """შედარება ვერ გაიარა — ოპერატორმა სხვაობები ნახა და მაინც ატვირთვა დაადასტურა (ჟურნალში იწერება)."""
        from .. import mhr_queue
        if (await form(request)).get("confirm") == "1":
            r = repo()
            try:
                mhr_queue.approve_override(r, doc_id, "operator")
            finally:
                r.close()
        return RedirectResponse(f"/mhr/{doc_id}", status_code=303)

    @app.post("/mhr/{doc_id}/{action}")
    async def mhr_action(doc_id: str, action: str, request: Request):
        """ფორმის „back“ ველი ამბობს, სად დავბრუნდეთ (მაგ. სიის იმავე ჩანართზე); სხვა შემთხვევაში — დეტალურ გვერდზე."""
        from .. import mhr_queue
        fn = {"approve": mhr_queue.approve, "reject": mhr_queue.reject, "revalidate": mhr_queue.revalidate,
              "retry": mhr_queue.retry_upload, "ai-review": mhr_queue.rerun_ai_review}.get(action)
        if fn is None:
            raise HTTPException(404)
        back = (await form(request)).get("back") or ""
        back = back if back.startswith("/") and not back.startswith("//") else f"/mhr/{doc_id}"
        r = repo()
        try:
            fn(r, doc_id) if action in ("revalidate", "retry", "ai-review") else fn(r, doc_id, "operator")
        finally:
            r.close()
        return RedirectResponse(back, status_code=303)

    @app.post("/mhr-approve-all")
    def mhr_approve_all():
        from .. import mhr_queue
        r = repo()
        try:
            for x in r.mhr_list(("ready",)):
                mhr_queue.approve(r, x["document_id"], "operator")
        finally:
            r.close()
        return RedirectResponse("/mhr?tab=queue", status_code=303)

    # --- ხელით ატვირთვა: თანამშრომელი პ/ნ + სახელ-გვარით დასტურდება MHR-ში, დოკუმენტები შედარების გარეშე იტვირთება
    @app.get("/manual", response_class=HTMLResponse)
    def manual(pn: str = "", first: str = "", last: str = "", msg: str = "", err: str = ""):
        c = cfg()
        q = {"pn": pn.strip(), "first": first.strip(), "last": last.strip()}
        res = _manual_lookup(c, q) if any(q.values()) else None
        rows: list = []
        if res and res["employee"] and not res["error"]:
            r = repo()
            try:
                rows = r.q("SELECT d.*, s.first_path, m.status AS mhr_status FROM documents d JOIN sources s ON s.id=d.source_id"
                           " LEFT JOIN mhr_items m ON m.document_id=d.id WHERE d.active=1 AND d.status<>'split' AND d.output_path IS NOT NULL"
                           " AND d.purged_at IS NULL ORDER BY d.created_at DESC LIMIT 3000")
            finally:
                r.close()
        return page("ხელით ატვირთვა", _manual_body(c, q, res, rows, msg, err), "/manual", side_info(status_data()))

    @app.post("/doc/{doc_id}/manual-upload")
    async def doc_manual_upload(doc_id: str, request: Request):
        """ჩავარდნილი/გამოტოვებული დოკუმენტი: დოკუმენტიდან ამოკითხული პ/ნ-ით MHR-ში თანამშრომელი მოიძებნება და
        დოკუმენტი შედარების გარეშე მაშინვე იტვირთება. სახელ-გვარის აკრეფა არ სჭირდება."""
        from .. import mhr_queue
        from ..textnorm import digits_only
        f = await form(request)
        back = f.get("back") or f"/doc/{doc_id}"
        back = back if back.startswith("/") else f"/doc/{doc_id}"
        c = cfg()
        r = repo()
        try:
            doc = r.get_document(doc_id)
            if doc is None:
                raise HTTPException(404)
            meta = json.loads(doc["metadata"] or "{}")
            pn = digits_only(str(doc["personal_number"] or ((meta.get("fields") or {}).get("personal_number") or {}).get("value") or ""))
            emp, msg, err = None, "", ""
            if len(pn) != 11:
                err = "დოკუმენტიდან პირადი ნომერი არ იკითხება — გამოიყენე გვერდი „ხელით ატვირთვა“ და ჩაწერე ხელით."
            else:
                look = _manual_lookup_pn(c, pn)
                emp, err = look["employee"], look["error"]
            if emp is not None:
                code = mhr_queue.approve_manual(r, doc_id, emp, pn, type_id=(f.get("type_id") or "").strip() or None)
                if code:
                    err = _MANUAL_ERR.get(code, code)
                else:
                    st = mhr_queue.upload_now(r, c, doc_id)
                    if st == "uploaded":
                        msg = f"ატვირთულია MHR-ზე — {_mhr_emp_name(emp)} (პ/ნ {pn})."
                    else:
                        item = r.mhr_get(doc_id)
                        detail = (item["error"] or "") if item else ""
                        err = f"{_MANUAL_UPLOAD_KA.get(st, st)}{f' ({detail})' if detail and st == 'upload_failed' else ''}"
        finally:
            r.close()
        sep = "&" if "?" in back else "?"
        return RedirectResponse(back + sep + urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v}), status_code=303)

    @app.post("/manual/upload")
    async def manual_upload(request: Request):
        """ლუქაპი თავიდან კეთდება (ფორმის ველებს არ ვენდობით) → approve_manual → ატვირთვა ახლავე, შედეგი შეტყობინებაში."""
        from .. import mhr_queue
        f = await form(request)
        q = {k: (f.get(k) or "").strip() for k in ("pn", "first", "last")}
        c = cfg()
        res = _manual_lookup(c, q)
        back = "/manual?" + urlencode(q)
        if res["error"]:
            return RedirectResponse(back + "&" + urlencode({"err": res["error"]}), status_code=303)
        ids = [x for x in (f.get("document_ids") or "").split(",") if x]
        type_id = (f.get("type_id") or "").strip() or None
        done, bad = 0, []
        r = repo()
        try:
            for doc_id in ids:
                row = r.get_document(doc_id)
                label = " · ".join(x for x in _doc_title(c, row) if x) if row else doc_id
                code = mhr_queue.approve_manual(r, doc_id, res["employee"], res["pn"],
                                                type_id=type_id if len(ids) == 1 else None)
                if code:
                    bad.append(f"{label}: {_MANUAL_ERR.get(code, code)}")
                    continue
                st = mhr_queue.upload_now(r, c, doc_id)
                if st == "uploaded":
                    done += 1
                else:
                    item = r.mhr_get(doc_id)
                    detail = (item["error"] or "") if item else ""
                    bad.append(f"{label}: {_MANUAL_UPLOAD_KA.get(st, st)}{f' ({detail})' if detail and st == 'upload_failed' else ''}")
        finally:
            r.close()
        extra = {}
        if done:
            extra["msg"] = f"ატვირთულია MHR-ზე: {done} დოკუმენტი ({_mhr_emp_name(res['employee'])})."
        if bad:
            extra["err"] = "; ".join(bad)
        return RedirectResponse(back + ("&" + urlencode(extra) if extra else ""), status_code=303)

    @app.get("/doc/{doc_id}/text", response_class=HTMLResponse)
    def doc_text(doc_id: str):
        """ყოველი გვერდის სკანი და პროგრამის მიერ ამოკითხული ტექსტი გვერდიგვერდ — OCR-ის თვალით შესამოწმებლად."""
        r = repo()
        try:
            x = r.get_document(doc_id)
            if not x:
                raise HTTPException(404)
            texts = r.load_page_texts(x["job_id"])
        finally:
            r.close()
        secs = []
        for p in range(x["page_start"], x["page_end"] + 1):
            pt = texts.get(p, {})
            q = float(pt.get("ocr_quality", 0))
            tone = "ok" if q >= 0.8 else "warn" if q >= 0.5 else "bad"
            secs.append(f'<div class="card section"><div class="chips" style="margin-bottom:10px">'
                        f'<b style="margin-right:8px">გვერდი {p + 1}</b>{chip(f"ხარისხი {q:.2f}", tone)}{chip(pt.get("text_source", "—"), "mute")}</div>'
                        f'<div class="split" style="grid-template-columns:minmax(0,1fr) minmax(0,1fr)"><img src="/page/{e(doc_id)}/{p}.png" '
                        f'alt="გვერდი {p + 1}" loading="lazy" style="width:100%;border:1px solid var(--border);border-radius:10px">'
                        f'<pre style="white-space:pre-wrap;word-break:break-word;margin:0;font:14px/1.6 inherit">{e(pt.get("text", ""))}</pre></div></div>')
        body = (f'<p><a href="/doc/{e(doc_id)}">← დოკუმენტი</a></p><h1>ამოკითხული ტექსტი</h1>'
                '<p class="sub">მარცხნივ სკანი, მარჯვნივ ის, რაც პროგრამამ წაიკითხა. ხარისხი: მწვანე ≥ 0.8 კარგი, ყვითელი საშუალო, წითელი ცუდი. '
                'ზუსტი გაზომვისთვის გამოიყენე ბრძანება <span class="mono">ocr-test</span> (იხ. ინსტრუქცია).</p>' + "".join(secs))
        return page("ამოკითხული ტექსტი", body, "/documents", side_info(status_data()))

    @app.get("/page/{doc_id}/{index}.png")
    def page_image(doc_id: str, index: int, dpi: int = 100, crop: str = ""):
        """გვერდის სურათი იმავე მიმართულებით, რა მიმართულებითაც წაიკითხა (მონიშვნები ზუსტად დაჯდეს); `crop` — x0,y0,x1,y1, მეათასედებში."""
        from fastapi.responses import Response
        from PIL import Image
        from ..pdfio import PdfDocument
        from ..preprocess import apply_rotation
        r = repo()
        try:
            x = r.get_document(doc_id)
            src = r.get_source(x["source_id"]) if x else None
            pt = r.load_page_texts(x["job_id"]).get(index, {}) if x else {}
        finally:
            r.close()
        if not x or not src or not (x["page_start"] <= index <= x["page_end"]) or not Path(src["archive_path"]).exists():
            raise HTTPException(404)
        import io
        with PdfDocument(src["archive_path"]) as d:
            img = d.render(index, max(40, min(dpi, 200)), grayscale=False)
        if pt.get("rotation_applied"):
            img = apply_rotation(img, int(pt["rotation_applied"]))
        if pt.get("deskew_angle"):
            img = img.rotate(float(pt["deskew_angle"]), resample=Image.BICUBIC, expand=False, fillcolor=(255, 255, 255))
        if crop:
            try:
                x0, y0, x1, y1 = (max(0, min(int(v), 1000)) for v in crop.split(","))
            except ValueError:
                raise HTTPException(404) from None
            if x1 <= x0 or y1 <= y0:
                raise HTTPException(404)
            img = img.crop((img.width * x0 // 1000, img.height * y0 // 1000, img.width * x1 // 1000, img.height * y1 // 1000))
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return Response(buf.getvalue(), media_type="image/png")

    @app.get("/file/{kind}/{doc_id}")
    def file(kind: str, doc_id: str):
        # ბილიკი მხოლოდ ბაზიდან მოდის (არა მომხმარებლის შეყვანიდან)
        r = repo()
        try:
            x = r.get_document(doc_id)
            if not x:
                raise HTTPException(404)
            if kind == "output":
                path = x["output_path"]
            elif kind == "source":
                src = r.get_source(x["source_id"])
                path = src["archive_path"] if src else None
            else:
                raise HTTPException(404)
        finally:
            r.close()
        if not path or not Path(path).exists():
            raise HTTPException(404, "file not found")
        return FileResponse(path, media_type="application/pdf", content_disposition_type="inline")

    # --- ფაილები (სტატისტიკა) ---------------------------------------------------------
    @app.get("/stats", response_class=HTMLResponse)
    @app.get("/files", response_class=HTMLResponse)
    def stats():
        c = cfg()
        r = repo()
        try:
            body = _stats_body(c, r)
        finally:
            r.close()
        return page("ფაილები", body, "/stats", side_info(status_data()), watch=True)

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs(status: str = ""):
        r = repo()
        try:
            sql = ("SELECT j.*, s.first_path FROM jobs j JOIN sources s ON s.id=j.source_id"
                   + (" WHERE j.status=?" if status else "") + " ORDER BY j.created_at DESC LIMIT 300")
            rows = r.q(sql, (status,) if status else ())
            events = r.q("SELECT at, level, kind, message FROM events ORDER BY id DESC LIMIT 50")
        finally:
            r.close()
        trs = "".join(
            f'<tr><td>{e(Path(x["first_path"]).name)}</td><td>{status_chip(x["status"])}</td><td>{x["progress_done"]}/{x["progress_total"]}</td>'
            f'<td>{x["attempt"]}</td><td class="mono">{e(x["error_code"] or "")}</td><td class="mono">{e(x["config_version"])}</td>'
            f'<td>{ago(x["updated_at"])}</td><td><form class="inline" method="post" action="/jobs/{e(x["id"])}/reprocess">'
            f'<button class="btn sm">{icon("refresh", 14)}თავიდან</button></form></td></tr>' for x in rows)
        ev = "".join(f'<tr><td>{ago(x["at"])}</td><td>{chip(x["kind"], "bad" if x["level"] == "error" else "mute")}</td><td>{e(x["message"])}</td></tr>'
                     for x in events)
        body = (f'<p><a href="/stats">← ფაილები</a></p><h1>ტექნიკური ჟურნალი</h1><p class="sub">სამუშაოები, მცდელობები და მოვლენები — მხარდაჭერისთვის.</p>'
                f'<div class="card" style="overflow-x:auto"><table class="tbl"><tr><th>ფაილი</th><th>სტატუსი</th><th>გვერდები</th><th>მცდელობა</th>'
                f'<th>შეცდომა</th><th>კონფ. ვერსია</th><th>განახლდა</th><th></th></tr>{trs}</table></div>'
                f'<h2>მოვლენები</h2><div class="card" style="overflow-x:auto"><table class="tbl">{ev or "<tr><td class=muted>—</td></tr>"}</table></div>')
        return page("ჟურნალი", body, "/stats", side_info(status_data()))

    @app.post("/jobs/{job_id}/reprocess")
    def reprocess_job(job_id: str):
        from ..cli import reprocess
        c = cfg()
        r = repo()
        try:
            new = reprocess(r, c, job_id)
            r.log_event("reprocess", f"reprocess of {job_id} -> {new or 'already active'}", job_id=job_id)
        finally:
            r.close()
        return RedirectResponse("/stats", status_code=303)

    # --- პარამეტრები ------------------------------------------------------------------
    @app.get("/settings", response_class=HTMLResponse)
    def settings(msg: str = "", err: str = ""):
        return page("პარამეტრები", _settings_body(cfg(), msg, err), "/settings", side_info(status_data()))

    @app.post("/settings")
    async def save_settings(request: Request):
        c = cfg()
        f = await form(request)
        try:
            data, types = _apply_settings_form(c, f)
            new = Config(data, types, path=c.path, types_path=c.types_path)
            moved = new.paths.db != c.paths.db
            if moved:   # ახალი root = ახალი ბაზა; ძველის რიგი იქ დარჩებოდა და აღარავინ დაამუშავებდა
                r = repo()
                try:
                    busy = (r.one("SELECT COUNT(*) n FROM jobs WHERE status IN ('queued', 'running')")["n"]
                            + r.one("SELECT COUNT(*) n FROM inbox_files WHERE state='pending'")["n"])
                finally:
                    r.close()
                if busy:
                    raise ValueError(f"მთავარი საქაღალდე ახლა ვერ შეიცვლება: {busy} ფაილი ჯერ მუშავდება. "
                                     "სცადე დამუშავების დასრულების შემდეგ.")
            new.save()
        except (ConfigError, ValueError, KeyError) as ex:
            return RedirectResponse("/settings?" + urlencode({"err": str(ex)}), status_code=303)
        msg = ("შენახულია. დამმუშავებელი ახალ მთავარ საქაღალდეზე რამდენიმე წამში გადაერთვება. დოკუმენტების სია აქ თავიდან "
               "იწყება — ძველი ფაილები ძველ საქაღალდეში რჩება." if moved else "შენახულია. ცვლილება ახალ ფაილებზე იმოქმედებს.")
        return RedirectResponse("/settings?" + urlencode({"msg": msg}), status_code=303)

    @app.post("/settings/secret")
    async def save_secret(request: Request):
        from .. import secrets
        f = await form(request)
        name, value = f.get("name", "").strip(), f.get("value", "")
        if not name or not value:
            return RedirectResponse("/settings?" + urlencode({"err": "სახელი და მნიშვნელობა სავალდებულოა"}), status_code=303)
        secrets.set_secret(name, value)
        rechecked = 0
        if name == cfg().section("mhr")["auth"].get("secret_name"):
            from .. import mhr_queue
            r = repo()
            try:
                rechecked = mhr_queue.revalidate_refused(r)
            finally:
                r.close()
        if rechecked:
            return RedirectResponse("/settings?" + urlencode({"msg": f"გასაღები „{name}“ შენახულია. {rechecked} ფაილი თავიდან შემოწმდება."}), status_code=303)
        return RedirectResponse("/settings?" + urlencode({"msg": f"გასაღები „{name}“ შენახულია Windows-ის დაცულ საცავში"}), status_code=303)

    @app.post("/settings/claude-login")
    def claude_login():
        from ..ocr import claude_code_vision as cc
        if not cc.launch_login():
            return RedirectResponse("/settings?" + urlencode({"err": "Claude Code ვერ მოიძებნა — ჯერ დააყენე და PATH-ში ჩასვი"}),
                                    status_code=303)
        return RedirectResponse("/settings?" + urlencode(
            {"msg": "გაიხსნა Claude-ის ფანჯარა — ბრაუზერში გაიარე ავტორიზაცია, მერე ეს გვერდი განაახლე"}), status_code=303)

    return app


# --- გვერდების ნაწილები ------------------------------------------------------------------
def _summary(counts: dict[str, int]) -> str:
    if not counts:
        return "მუშავდება"
    parts = [f"{v} {STATUS_KA.get(k, k).lower()}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]
    return f"{sum(counts.values())} დოკუმენტი ({', '.join(parts)})"


def _local_midnight_utc(days_ago: int = 0) -> str:
    """ადგილობრივი შუაღამე (დღეს / N დღის წინ) UTC ISO-ში — ბაზაში დრო UTC-შია, „დღეს“ კი ოპერატორის დღეა."""
    local = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return (local - timedelta(days=days_ago)).astimezone(timezone.utc).isoformat(timespec="seconds")


def _home_today(r: Repository) -> dict[str, int]:
    """დღევანდელი რიცხვები და იგივე ბოლო 7 დღეში (`*_week`) — დღე ცარიელი რომ იყოს, კვირა მაინც ჩანს."""
    out: dict[str, int] = {}
    for suffix, since in (("", _local_midnight_utc()), ("_week", _local_midnight_utc(6))):
        src = r.one("SELECT COUNT(*) n, COALESCE(SUM(page_count), 0) p FROM sources WHERE created_at>=?", (since,))
        docs = r.one("SELECT COUNT(*) n FROM documents WHERE active=1 AND part_of IS NULL AND created_at>=?", (since,))
        up = r.one("SELECT COUNT(*) n FROM mhr_items WHERE status='uploaded' AND uploaded_at>=?", (since,))
        out |= {f"files{suffix}": src["n"], f"pages{suffix}": src["p"], f"docs{suffix}": docs["n"], f"uploaded{suffix}": up["n"]}
    return out


def _home_flow(s: dict, trial: bool) -> str:
    """ფაილის გზა ერთ ხაზზე (რამდენი სად დგას ახლა) და ქვემოთ — რაც გაჩერდა და შენ გელოდება."""
    d, j, m = s["documents"], s["jobs"], s["mhr"] if s["mhr_enabled"] else {}
    busy = j.get("queued", 0) + j.get("running", 0) + s["inbox_waiting"]
    if s["mhr_enabled"]:
        path = [("/stats", "scan", "მუშავდება", busy, "info"),
                ("/mhr?tab=queue", "clock", "MHR-ში მოწმდება", m.get("pending", 0) + m.get("unavailable", 0), "info"),
                ("/mhr?tab=ready", "check", "მზადაა ასატვირთად", m.get("ready", 0), "info"),
                ("/mhr?tab=queue", "cloud", "იტვირთება", m.get("approved", 0) + m.get("uploading", 0), "info"),
                ("/mhr?tab=done", "cloud", "აიტვირთა", m.get("uploaded", 0), "ok")]
    else:
        path = [("/stats", "scan", "მუშავდება", busy, "info"),
                ("/documents?status=" + ("proposed" if trial else "auto_saved"), "check",
                 "შემოთავაზებული" if trial else "დალაგდა", d.get("proposed" if trial else "auto_saved", 0), "ok")]
    stuck = [("/review", "inbox", "გადასამოწმებელი", d.get("needs_review", 0)),
             *([("/mhr?tab=failed", "alert", "MHR-ში ვერ გაიარა", m.get("failed", 0)),
                ("/mhr?tab=other", "alert", "ატვირთვა გასარკვევია", m.get("upload_failed", 0) + m.get("needs_check", 0))]
               if s["mhr_enabled"] else []),
             ("/stats#stuck", "x", "ვერ დამუშავდა", j.get("failed", 0) + s["inbox_errors"])]

    def step(href, ic, lbl, n, tone):
        return (f'<a class="step{" zero" if not n else ""}" href="{href}"><span class="ib tone-{tone if n else "mute"}">{icon(ic, 16)}</span>'
                f'<span class="n">{n}</span><span class="l">{e(lbl)}</span></a>')
    arrow = '<span class="arrow" aria-hidden="true">→</span>'
    flow = arrow.join(step(*x) for x in path)
    jam = [x for x in stuck if x[3]]
    jam_html = ("".join(step(href, ic, lbl, n, "warn" if ic != "x" else "bad") for href, ic, lbl, n in jam) if jam else
                f'<span class="ok-line">{icon("check", 16)}არაფერი გაჭედილა</span>')
    return (f'<div class="card flow"><div class="steps-row">{flow}</div>'
            f'<div class="jam"><span class="jam-l">გაჩერებული</span>{jam_html}</div></div>')


_FILE_STATE = {   # (წარწერა, ტონი) — ფაილზე ყველაზე „მძიმე“ მდგომარეობა იმარჯვებს (თანმიმდევრობა = პრიორიტეტი)
    "job_failed": ("ვერ დამუშავდა", "bad"), "mhr_failed": ("MHR-ში ვერ გაიარა", "warn"),
    "mhr_stuck": ("ატვირთვა გასარკვევია", "warn"), "needs_review": ("გადასამოწმებელი", "warn"),
    "working": ("მუშავდება", "info"), "mhr_ready": ("მზადაა ასატვირთად", "info"),
    "mhr_checking": ("MHR-ში მოწმდება", "info"), "mhr_uploading": ("იტვირთება", "info"),
    "uploaded": ("აიტვირთა MHR-ზე", "ok"), "sorted": ("დალაგდა", "ok"), "proposed": ("შემოთავაზებული", "ok"),
    "empty": ("ცარიელი", "mute")}


def _recent_files(r: Repository, c: Config, limit: int = 12) -> list[dict]:
    """ბოლო სკანები ოპერატორის ენაზე: ვისია, რა დოკუმენტია და ერთი გასაგები მდგომარეობა (+ სად დააჭიროს)."""
    files = r.q("SELECT s.id sid, s.first_path, s.page_count, s.created_at, j.id jid, j.status, j.progress_done, j.progress_total"
                " FROM sources s JOIN jobs j ON j.source_id=s.id"
                " WHERE j.created_at=(SELECT MAX(created_at) FROM jobs WHERE source_id=s.id) ORDER BY s.created_at DESC LIMIT ?", (limit,))
    out = []
    for f in files:
        docs = r.q("SELECT d.id, d.status, d.type_id, d.metadata, d.part_of, m.status mhr FROM documents d"
                   " LEFT JOIN mhr_items m ON m.document_id=d.id WHERE d.job_id=? AND d.active=1", (f["jid"],))
        names, types = [], []
        for x in docs:
            if x["part_of"]:
                continue
            fl = json.loads(x["metadata"] or "{}").get("fields") or {}
            nm = " ".join(v for v in ((fl.get("first_name") or {}).get("value"), (fl.get("last_name") or {}).get("value")) if v)
            if nm and nm not in names:
                names.append(nm)
            t = c.doc_type(x["type_id"] or "")
            if t and t.name_ka not in types:
                types.append(t.name_ka)
        st = {x["status"] for x in docs}
        mhr = {x["mhr"] for x in docs if x["mhr"]}
        pick = lambda pred: next((x["id"] for x in docs if pred(x)), None)   # noqa: E731
        if f["status"] == "failed":
            key, href = "job_failed", "/stats#stuck"
        elif f["status"] in ("queued", "running"):
            key, href = "working", "/stats"
        elif "failed" in mhr:
            key, href = "mhr_failed", f'/mhr/{pick(lambda x: x["mhr"] == "failed")}'
        elif mhr & {"upload_failed", "needs_check"}:
            key, href = "mhr_stuck", f'/mhr/{pick(lambda x: x["mhr"] in ("upload_failed", "needs_check"))}'
        elif "needs_review" in st:
            key, href = "needs_review", f'/doc/{pick(lambda x: x["status"] == "needs_review")}'
        elif "ready" in mhr:
            key, href = "mhr_ready", f'/mhr/{pick(lambda x: x["mhr"] == "ready")}'
        elif mhr & {"pending", "unavailable"}:
            key, href = "mhr_checking", "/mhr?tab=queue"
        elif mhr & {"approved", "uploading"}:
            key, href = "mhr_uploading", "/mhr?tab=queue"
        elif mhr and mhr <= {"uploaded", "split", "skipped", "rejected"} and "uploaded" in mhr:
            key, href = "uploaded", f'/documents?source={f["sid"]}'
        elif docs:
            key, href = ("proposed" if "proposed" in st else "sorted"), f'/documents?source={f["sid"]}'
        else:
            key, href = "empty", f'/documents?source={f["sid"]}'
        progress = f' · {f["progress_done"]}/{f["progress_total"]} გვერდი' if key == "working" and f["progress_total"] else ""
        out.append({"title": ", ".join(names[:2]) + (f" და კიდევ {len(names) - 2}" if len(names) > 2 else "") or Path(f["first_path"]).name,
                    "meta": " · ".join(x for x in (", ".join(types[:2]), Path(f["first_path"]).name if names else "",
                                                     f'{f["page_count"] or 0} გვერდი') if x) + progress,
                    "created_at": f["created_at"], "state": key, "href": href})
    return out


def _recent_html(files: list[dict]) -> str:
    """ბოლო სკანები დღეების მიხედვით: „დღეს“, „გუშინ“, თარიღი."""
    if not files:
        return ""
    today, yesterday = _local_midnight_utc(), _local_midnight_utc(1)
    groups: dict[str, list[dict]] = {}
    for f in files:
        ca = f["created_at"] or ""
        if ca >= today:
            day = "დღეს"
        elif ca >= yesterday:
            day = "გუშინ"
        else:
            try:
                day = datetime.fromisoformat(ca).astimezone().strftime("%d.%m.%Y")
            except ValueError:
                day = "ადრე"
        groups.setdefault(day, []).append(f)
    out = []
    for day, xs in groups.items():
        rows = "".join(
            f'<a class="row" href="{e(f["href"])}" style="color:inherit;text-decoration:none">'
            f'<span class="ib tone-{_FILE_STATE[f["state"]][1]}">{icon("file", 18)}</span>'
            f'<div class="grow"><div class="t">{e(f["title"])}</div><div class="m">{e(f["meta"])}</div></div>'
            f'<span class="when">{_local_time(f["created_at"])}</span>{chip(*_FILE_STATE[f["state"]])}</a>' for f in xs)
        out.append(f'<div class="day">{e(day)}</div><div class="card list recent" style="margin-bottom:12px">{rows}</div>')
    return "".join(out)


def _local_time(iso: str | None) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%H:%M")
    except (TypeError, ValueError):
        return ""


def _outcome_chip(job_status: str, counts: dict[str, int]) -> str:
    """ფაილის შედეგი ოპერატორისთვის: რა უნდა გააკეთოს (და არა სამუშაოს ტექნიკური სტატუსი)."""
    if job_status in ("failed", "queued", "running"):
        return status_chip(job_status)
    for k in ("needs_review", "proposed", "auto_saved"):
        if counts.get(k):
            return status_chip(k)
    return chip("ცარიელი", "mute")


def _doc_title(c: Config, x) -> tuple[str, str]:
    t = c.doc_type(x["type_id"] or "")
    meta = json.loads(x["metadata"] or "{}")
    f = meta.get("fields", {})
    name = " ".join(v for v in ((f.get("first_name") or {}).get("value"), (f.get("last_name") or {}).get("value")) if v)
    if not t or t.id == "unknown":
        return "დოკუმენტი (ტიპი უცნობია)", name
    return t.name_ka, name


def _doc_row(c: Config, x) -> str:
    tname, name = _doc_title(c, x)
    meta = [v for v in (name, x["personal_number"], x["document_date"] or (str(x["year"]) if x["year"] else "თარიღი უცნობია"),
                        f'{Path(x["first_path"]).name} · გვ. {x["page_start"] + 1}–{x["page_end"] + 1}') if v]
    return (f'<a class="row" href="/doc/{e(x["id"])}" style="color:inherit;text-decoration:none">'
            f'<div class="grow"><div class="t">{e(tname)}</div><div class="m">{e(" · ".join(meta))}</div></div>'
            f'{status_chip(x["status"])}</a>')


def _review_card(c: Config, x) -> str:
    tname, name = _doc_title(c, x)
    reasons = json.loads(x["review_reasons"] or "[]")
    chips = "".join(chip(REASONS.get(k, (k, ""))[0], "warn") for k in reasons)
    tip = REASONS.get(reasons[0], ("", ""))[1] if reasons else ""
    return (f'<div class="card rcard"><div class="head"><div style="min-width:0;flex:1">'
            f'<div style="font-weight:620">{e(tname)}{" · " + e(name) if name else ""}</div>'
            f'<div class="muted" style="font-size:13px">{e(Path(x["first_path"]).name)} · გვ. {x["page_start"] + 1}–{x["page_end"] + 1} · {ago(x["created_at"])}</div></div>'
            f'<div class="btns"><a class="btn sm primary" href="/doc/{e(x["id"])}">{icon("file", 16)}გადახედვა</a>'
            f'<a class="btn sm" href="/file/source/{e(x["id"])}#page={x["page_start"] + 1}" target="_blank">{icon("open", 16)}წყარო</a></div></div>'
            f'<div class="chips">{chips}</div>{f"<div class=muted style=font-size:13.5px>{e(tip)}</div>" if tip else ""}</div>')


MHR_KA = {"pending": ("ვალიდაციას ელოდება", "mute"), "unavailable": ("MHR მიუწვდომელია", "warn"),
          "failed": ("ვალიდაცია ვერ გაიარა", "bad"), "skipped": ("გამოტოვებული", "mute"), "ready": ("მზადაა ატვირთვისთვის", "info"),
          "approved": ("ატვირთვის რიგში", "info"), "uploading": ("იტვირთება", "info"), "uploaded": ("ატვირთულია", "ok"),
          "upload_failed": ("ატვირთვა ვერ მოხერხდა", "bad"), "needs_check": ("საჭიროებს შემოწმებას", "warn"),
          "rejected": ("უარყოფილი", "mute"), "split": ("დაიყო ფაილებად", "mute")}
AI_KA = {"upload": ("AI: ატვირთვა შეიძლება", "ok"), "check": ("AI: გადაამოწმე", "warn"),
         "reject": ("AI: არ ატვირთო", "bad"), "error": ("AI ანალიზი ვერ შესრულდა", "mute")}
_AI_CONF_KA = {"high": "მაღალი დარწმუნებულობა", "medium": "საშუალო დარწმუნებულობა", "low": "დაბალი დარწმუნებულობა"}
_MHR_TABS = {"ready": ("მზადაა", ("ready",)), "failed": ("ვერ გაიარა", ("failed",)),
             "queue": ("მიმდინარე", ("pending", "unavailable", "approved", "uploading")),
             "done": ("ატვირთულია", ("uploaded",)), "other": ("სხვა", ("skipped", "upload_failed", "needs_check", "rejected", "split"))}
_CHECK_KA = {"match": ("ემთხვევა", "ok"), "mismatch": ("განსხვავდება", "bad"), "missing_in_document": ("დოკუმენტში არ არის", "mute"),
             "missing_in_mhr": ("MHR-ში არ არის", "warn"), "skipped": ("—", "mute"), "info": ("გაფრთხილება", "warn")}
_DIFF_KA = {"changed": "შეცვლილია", "added": "დამატებულია", "removed": "ამოღებულია", "handwriting": "ხელნაწერი / მიწერა",
            "signature": "ხელმოწერა",
            "page_count": "გვერდების რაოდენობა"}


def mhr_chip(status: str) -> str:
    lbl, tone = MHR_KA.get(status, (status, "mute"))
    return chip(lbl, tone)


def _mhr_doc_status(m) -> str:
    """დოკუმენტის გვერდისთვის: მოიძებნა თუ არა თანამშრომელი MHR-ში და სად არის პროცესში."""
    if m is None:
        return "არ შემოწმებულა"
    v = json.loads(m["validation"] or "{}")
    found = "თანამშრომელი მოიძებნა" if m["employee_id"] else (
        "თანამშრომელი ვერ მოიძებნა" if v else "ჯერ არ შემოწმებულა")
    return f'{found} · {MHR_KA.get(m["status"], (m["status"], ""))[0]}'


def _mhr_name(v: dict) -> str:
    emp = v.get("employee") or {}
    return " ".join(x for x in (emp.get("first_name"), emp.get("last_name")) if x)


def _mhr_row(c: Config, x, back: str = "") -> str:
    """სიის ერთი სტრიქონი. AI-ის დასკვნის ჩიპი დეტალური გვერდის AI ბლოკზე გადადის; „ხელახალი AI ანალიზი“ აქედანვე იწყება
    და `back`-ზე (სიის იმავე ჩანართზე) აბრუნებს."""
    from .. import ai_review
    v = json.loads(x["validation"] or "{}")
    t = c.doc_type(x["type_id"] or "")
    name = _mhr_name(v)
    summary = _main_reason(v) or (_error_text(x["error"]) if x["error"] else "")
    title = clean_file_title((v.get("original") or {}).get("filename")) or (t.name_ka if t else None)
    meta = " · ".join(y for y in (x["personal_number"], title,   # სახელი უკვე სათაურშია
                                  f'{Path(x["first_path"]).name} · გვ. {x["page_start"] + 1}–{x["page_end"] + 1}' if x["first_path"] else None) if y)
    btns = ""
    if x["status"] == "ready":
        btns = (f'<form class="inline" method="post" action="/mhr/{e(x["document_id"])}/approve"><button class="btn sm primary">'
                f'{icon("cloud", 14)}ატვირთვა</button></form>')
    ai = _ai_load(x)
    doc_id = e(x["document_id"])
    ai_chip = rerun = ""
    if ai and ai.get("verdict") in AI_KA:
        lbl, tone = AI_KA[ai["verdict"]]
        ai_chip = f'<a class="chip tone-{tone}" href="/mhr/{doc_id}#ai" title="AI ანალიზის ნახვა">{e(lbl)}</a>'
        if x["status"] in ("ready", "failed") and ai_review.enabled(c):
            # AI-ის შეჯამების ქვეშ, ტექსტის სვეტში — მარჯვენა ღილაკების სვეტი არ ფართოვდება და ვიწრო ეკრანზე ტექსტი არ იჭყლიტება
            rerun = (f'<form class="inline" method="post" action="/mhr/{doc_id}/ai-review">'
                     f'<input type="hidden" name="back" value="{e(back)}"><button class="btn sm ghost" '
                     f'style="min-height:28px;padding:0 6px;margin:2px 0 0 -6px;font-size:13px;color:var(--accent)" '
                     f'title="AI დასკვნას თავიდან გააკეთებს">{icon("refresh", 14)}ხელახალი AI ანალიზი</button></form>')
    ai_text = ai.get("summary") if ai and ai.get("verdict") != "error" else ""
    # AI-ის შეჯამება ორ სტრიქონამდე; სრული ტექსტი — მაუსის მიტანისას და დეტალურ გვერდზე
    ai_div = f'<div class="m clamp2" title="{e(ai_text)}">AI: {e(ai_text)}</div>' if ai_text else ""
    return (f'<div class="row"><div class="grow"><a class="t" href="/mhr/{doc_id}" style="display:block;color:var(--text)">'
            f'{e(name or "თანამშრომელი უცნობია")}</a><div class="m">{e(meta)}</div>'
            f'{f"<div class=m><b>{e(summary)}</b></div>" if summary else ""}'
            f'{ai_div}{rerun}</div>{ai_chip}{mhr_chip(x["status"])}'
            f'<div class="btns">{btns}<a class="btn sm{"" if btns else " primary"}" href="/mhr/{doc_id}">'
            f'{"ნახე სხვაობები" if x["status"] == "failed" and not _no_compare(v) else "დეტალები"}</a></div></div>')


def _ai_load(m) -> dict | None:
    try:
        raw = m["ai_review"]
    except (IndexError, KeyError):
        return None
    return json.loads(raw) if raw else None


def _ai_block(c: Config, m, worker_alive: bool = True) -> str:
    """AI ანალიზის (Opus) ბლოკი MHR-ის დეტალურ გვერდზე: გადაწყვეტილება, ახსნა პუნქტობრივად, რისკები."""
    from .. import ai_review
    if not ai_review.enabled(c):
        return ""
    ai = _ai_load(m)
    doc_id = e(m["document_id"])
    undecided = m["status"] in ("ready", "failed")
    rerun = (f'<form class="inline" method="post" action="/mhr/{doc_id}/ai-review"><button class="btn sm">'
             f'{icon("refresh", 14)}ხელახალი AI ანალიზი</button></form>') if undecided else ""
    if ai is None:
        if not undecided or m["error"]:   # შეცდომაზე შედარება არ შესრულებულა, AI-ს რიგში არ დგება (mhr_queue._ai_review_pending)
            return ""
        if not worker_alive:   # ანალიზს ფონური პროცესი აკეთებს — თუ არ მუშაობს, „რიგშია" შეცდომაში შეიყვანს
            return (f'<div class="banner tone-warn" id="ai">{icon("alert", 18)}<div><b>AI ანალიზი ვერ დაიწყება — ფონური პროცესი არ მუშაობს.</b> '
                    'გახსენი „HR გადამრჩევი“ დაწყების მენიუდან; წესებით შემოწმება ქვემოთ უკვე ჩანს.</div></div>')
        return (f'<div class="banner tone-info" id="ai">{icon("clock", 18)}<div><b>AI ანალიზი (Opus) რიგშია.</b> წესებით შემოწმება '
                'უკვე დასრულდა. რამდენიმე წუთში აქ გამოჩნდება დეტალური ახსნა და რეკომენდაცია — განაახლე გვერდი.</div></div>')
    verdict = ai.get("verdict") or "error"
    lbl, tone = AI_KA.get(verdict, AI_KA["error"])
    if verdict == "error":
        return (f'<div class="card section" id="ai" style="margin-bottom:20px"><div class="chips" style="margin-bottom:8px">{chip(lbl, tone)}</div>'
                f'<p class="muted">Claude Code-მა პასუხი არ დააბრუნა ({e(ai.get("error") or "")}).'
                + (' შეამოწმე, რომ Claude Code დაყენებულია და შესულია („პარამეტრები“), მერე სცადე თავიდან.' if undecided else "")
                + f'</p>{rerun}</div>')
    items = "".join(
        f'<div class="row" style="align-items:flex-start"><div style="min-width:160px"><b>{e(it.get("topic") or "")}</b></div>'
        f'<div class="grow">{e(it.get("explanation") or "")}</div>{chip(*_CHECK_KA_AI.get(it.get("status"), ("—", "mute")))}</div>'
        for it in ai.get("items") or [])
    risks = "".join(f"<li>{e(r)}</li>" for r in ai.get("risks") or [])
    hint = {"upload": "AI-ის აზრით დოკუმენტი სწორია. თუ ეთანხმები — დააჭირე ატვირთვას.",
            "check": "AI-მ რაღაც ეჭვი დაინახა — ქვემოთ წერია, რა და სად. ნახე PDF-ში და მერე გადაწყვიტე.",
            "reject": "AI-ის აზრით ეს დოკუმენტი ან სხვა თანამშრომლისაა, ან სულ სხვა დოკუმენტია. ატვირთვამდე აუცილებლად გადაამოწმე."}[verdict]
    return (f'<div class="card section" id="ai" style="margin-bottom:20px"><div class="chips" style="margin-bottom:8px">{chip(lbl, tone)}'
            f'{chip(_AI_CONF_KA.get(ai.get("confidence") or "low", ""), "mute")}</div>'
            f'<h2 style="margin-top:4px">AI ანალიზი · {e(ai.get("model") or "Opus")}</h2>'
            f'<p style="font-size:15px">{e(ai.get("summary") or "")}</p>'
            + (f'<p class="muted" style="font-size:13.5px">{hint}</p>' if undecided else "")
            + (f'<div class="card list" style="margin:12px 0">{items}</div>' if items else "")
            + (f'<p><b>რას გადაამოწმებ:</b></p><ul style="margin-top:4px">{risks}</ul>' if risks else "")
            + f'<div class="btns">{rerun}</div><p class="muted" style="font-size:12.5px;margin-top:10px">AI მხოლოდ რჩევას იძლევა — '
              'არაფერი არ იტვირთება შენი დაჭერის გარეშე. გაანალიზებულია ამოკითხული ტექსტი და შედარების შედეგი, გვერდის სურათები არა.</p></div>')


_CHECK_KA_AI = {"ok": ("წესრიგშია", "ok"), "warn": ("ყურადღება", "warn"), "bad": ("პრობლემა", "bad")}


def _part_states(doc_id: str, repo_fn) -> dict[str, tuple[str | None, str | None, dict]]:
    """დაყოფილი სკანის ნაწილები: document_id → (MHR-ის სტატუსი, დოკუმენტის სტატუსი, ვალიდაცია)."""
    r = repo_fn()
    try:
        return {x["id"]: (x["mhr_status"], x["status"], json.loads(x["validation"] or "{}")) for x in r.q(
            "SELECT d.id, d.status, m.status AS mhr_status, m.validation FROM documents d LEFT JOIN mhr_items m ON m.document_id=d.id"
            " WHERE d.part_of=?", (doc_id,))}
    finally:
        r.close()


def _page_list(pages: list[int]) -> str:
    """[15, 16, 17] → „15–17"; მიმდევრობის გარეშე — ჩამონათვალი."""
    if len(pages) > 1 and pages == list(range(pages[0], pages[-1] + 1)):
        return f"{pages[0]}–{pages[-1]}"
    return ", ".join(map(str, pages))


def _mhr_parts_block(states: dict, m) -> str:
    """დაყოფილი სკანი: რა ფაილები აღმოჩნდა და რა მდგომარეობაშია თითოეული."""
    parts = json.loads(m["validation"] or "{}").get("parts") or []
    if not parts:
        return ""
    rows, table = [], []
    for p in parts:
        mhr_st, doc_st, pv = states.get(p["document_id"], (None, None, {}))
        href = f"/mhr/{e(p['document_id'])}" if mhr_st else f"/doc/{e(p['document_id'])}"
        why = _main_reason(pv) or ("" if p.get("matched") else "ეს გვერდები თანამშრომლის არცერთ დაგენერირებულ ფაილს არ ემთხვევა")
        rows.append(f'<div class="row"><div class="grow"><a class="t" href="{href}" style="display:block;color:var(--text)">{e(p["title"])}</a>'
                    f'<div class="m">სკანის გვერდები {e(_page_list(p["pages"]))}</div>{f"<div class=m>{e(why)}</div>" if why else ""}</div>'
                    f'{mhr_chip(mhr_st) if mhr_st else status_chip(doc_st or "")}'
                    f'<div class="btns"><a class="btn sm" href="{href}">დეტალები</a></div></div>')
        for pg in p["pages"]:
            best = (p.get("page_match") or {}).get(str(pg))
            match = (f'{best[1]}% სიტყვა ამ ფაილშია' if p.get("matched") else f'საუკეთესოა „{best[0]}“, {best[1]}% — საჭიროა მინიმუმ {OWNER_MIN_PCT}%') if best else "ტექსტი თითქმის არ არის — მეზობელ გვერდს მიჰყვა"
            table.append((pg, f'<tr><td>{pg}</td><td>{e(p["title"])}</td><td>{e(match)}</td></tr>'))
    how = (f'<details style="margin-top:12px"><summary>რომელი გვერდი რომელ ფაილს მიეკუთვნა და რატომ</summary><p class="muted" style="font-size:13.5px;margin:8px 0">'
           f'გვერდი იმ ფაილს ეკუთვნის, რომლის სიტყვებსაც ყველაზე მეტად შეიცავს (მინიმუმ {OWNER_MIN_PCT}%).</p>'
           f'<div class="card" style="overflow-x:auto"><table class="tbl"><tr><th>გვერდი</th><th>ფაილი</th><th>რატომ</th></tr>'
           + "".join(t for _pg, t in sorted(table, key=lambda z: z[0])) + '</table></div></details>') if table else ""
    return f'<h2>ფაილები ამ სკანში</h2><div class="card list">{"".join(rows)}</div>{how}'


def _mhr_did_not_answer(v: dict) -> bool:
    """MHR გათიშული იყო ან უარი თქვა (მაგ. ტოკენზე): ჯერ არაფერი შემოწმებულა, ახსნა `summary`-შია."""
    return v.get("status") == "unavailable" or v.get("error") == "MhrError"


def _text_compared(v: dict) -> bool:
    """ტექსტი რამეს შედარდა: ორიგინალს ან შაბლონს. სხვაგვარად „ემთხვევა“ ტყუილი იქნებოდა."""
    return bool(v.get("original")) or any(ch.get("field") == "contract_template" for ch in v.get("checks") or [])


def _no_compare(v: dict) -> bool:
    """ვალიდაცია შედარებამდე გაჩერდა: MHR-ში დაგენერირებული ხელშეკრულება (ან მისი ფაილი) არ არის. ეს ცვლილება არ არის —
    გაფრთხილებაა, ამიტომ ოპერატორს იქვე სთავაზობს ხელით ატვირთვას."""
    return any(ch.get("field") == "original_contract" and ch.get("status") == "missing_in_mhr"
               for ch in (v.get("checks") or []))


OWNER_MIN_PCT = 70   # validation.PAGE_OWNER_MIN_OVERLAP პროცენტებში (ტექსტისთვის)
CROP_PAD_X, CROP_PAD_Y, CROP_MIN_W = 80, 35, 620   # პრობლემის მოჭრილი სურათი: მინდვრები და მინიმალური სიგანე, მეათასედებში
PURGED_NOTE = "სკანის ფაილი ვადის გასვლის გამო წაიშალა, ამიტომ გვერდის სურათი აღარ ჩანს. ტექსტი და შედეგი ინახება."
READER_KA = {"text_layer": "PDF-ის ტექსტი", "none": "ტექსტი არ არის"}
def _file_pages(r: Repository, d) -> list[dict]:
    from ..validation import document_pages
    return document_pages(r, d)


def _reader(pt: dict) -> str:
    src = pt.get("text_source") or "none"
    if src in READER_KA:
        return READER_KA[src]
    return "Tesseract" if src.startswith("ocr:tesseract") else "Claude"


def _clean_reason(reason: str | None) -> str:
    from ..validation import RECHECK_DISAGREE, RECHECK_STILL
    text = reason or ""
    for note in (RECHECK_STILL, RECHECK_DISAGREE):
        text = text.replace(f" — {note}", "").replace(note, "")
    return text.strip()


def _diff_place(x: dict, pts: list[dict]) -> str:
    """სად: ფაილის გვერდი, პუნქტი და — თუ სხვაა — სკანის გვერდი."""
    if not x.get("page"):
        return "მთელი დოკუმენტი"
    n = int(x["page"])
    place = f"გვერდი {n}"
    if x.get("clause"):
        place += f", პუნქტი {x['clause']}"
    if 0 < n <= len(pts) and int(pts[n - 1].get("page_index", n - 1)) + 1 != n:
        place += f" (სკანის გვერდი {int(pts[n - 1]['page_index']) + 1})"
    return place


def _diff_line(x: dict) -> str:
    """ერთი ხაზი სიისთვის: სად და რა."""
    what = _clean_reason(x.get("reason"))
    parts_o, parts_s = x.get("orig_parts") or [], x.get("scan_parts") or []
    if "„" not in what and x.get("kind") in ("changed", "added", "removed") and len(parts_o) == 3 and len(parts_s) == 3 and len(parts_o[1]) <= 60 and len(parts_s[1]) <= 60:
        if parts_o[1] and parts_s[1]:
            what += f": ქაღალდზე „{parts_s[1]}“, MHR-ში „{parts_o[1]}“"
        elif parts_o[1]:
            what += f": „{parts_o[1]}“"
        elif parts_s[1]:
            what += f": „{parts_s[1]}“"
    page = f"გვერდი {x['page']}" + (f", პუნქტი {x['clause']}" if x.get("clause") else "") if x.get("page") else ""
    return f"{page}: {what}" if page else what


def _blocking_checks(v: dict) -> list[dict]:
    return [c for c in v.get("checks") or [] if c.get("blocking") and c.get("status") != "match"]


def _check_line(c: dict) -> str:
    if c.get("status") == "mismatch" and c.get("mhr_value") is not None:
        return f'{c.get("label_ka")} განსხვავდება: დოკუმენტში „{c.get("document_value")}“, MHR-ში „{c.get("mhr_value")}“'
    return c.get("note") or f'{c.get("label_ka")}: MHR-ში ვერ მოიძებნა'


def _reason_lines(v: dict) -> list[str]:
    """ყველა მიზეზი, რის გამოც ფაილი ჩერდება: ჯერ მონაცემები, მერე მნიშვნელობით (ციფრი ხელნაწერის ეჭვზე წინ), მერე გვერდით.
    სიაში პირველი სტრიქონი ჩანს — ის ყველაზე მნიშვნელოვანი უნდა იყოს და არა პირველი გვერდის ბეჭედი."""
    sig = sorted((x for x in v.get("diffs") or [] if x.get("significant")),
                 key=lambda x: (_DIFF_PRIORITY.get(x.get("kind"), 2), x.get("page") or 0))
    return [*(_check_line(c) for c in _blocking_checks(v)), *(_diff_line(x) for x in sig)]


def _old_original(v: dict) -> dict | None:
    """MHR-ის ორიგინალი სკანის თარიღს არ ემთხვევა. ორიგინალის არჩევისას (validation.original_candidates) ±7 დღის ვერსიას
    უპირატესობა აქვს — თუ შენიშვნა `original_date` მაინც გაჩნდა, ბარათზე ამ თარიღის ვერსია არ არის და შედარდა სხვა,
    მაგ. სტაჟირების დროს დაგენერირებული ფაილი. მაშინ ტექსტის ბევრი „სხვაობა“ ამით აიხსნება და არა ქაღალდის შეცდომით."""
    ch = next((c for c in v.get("checks") or [] if c.get("field") == "original_date"), None)
    if not ch or not ch.get("document_value") or not ch.get("mhr_value"):
        return None
    doc_date, mhr_date = str(ch["document_value"])[:10], str(ch["mhr_value"])[:10]
    o = v.get("original") or {}
    return {"doc_date": doc_date, "mhr_date": mhr_date, "older": mhr_date < doc_date,
            "file": clean_file_title(o.get("filename")) or o.get("title") or "ფაილი"}


def _old_original_line(v: dict) -> str:
    oo = _old_original(v)
    if not oo:
        return ""
    return (f'MHR-ში ამ ფაილის მხოლოდ ძველი ვერსიაა ({oo["mhr_date"]}), ქაღალდზე — {oo["doc_date"]}' if oo["older"] else
            f'MHR-ში ფაილი ქაღალდის შემდეგ ხელახლა დაგენერირდა ({oo["mhr_date"]}), ქაღალდზე — {oo["doc_date"]}')


def _old_original_banner(v: dict, m) -> str:
    """გვერდის თავში, მკაფიოდ: რას შეადარა, რატომ შეიძლება იყოს ბევრი სხვაობა და რა გააკეთოს."""
    oo = _old_original(v)
    if not oo or m["status"] in _PAST_APPROVAL:
        return ""
    if oo["older"]:
        head = "MHR-ში ამ ფაილის მხოლოდ ძველი ვერსიაა"
        body = (f'შედარდა „{oo["file"]}“ — შექმნილია {oo["mhr_date"]}. ქაღალდზე კი {oo["doc_date"]} წერია, და ამ თარიღის ვერსია '
                "თანამშრომლის ბარათზე არ არის. სავარაუდოდ, ახალი ვერსია (მაგ. სტაჟირების შემდეგ — გამოსაცდელი ან შრომითი) MHR-ში "
                "არ დაგენერირებულა, ამიტომ ქვემოთ ჩამოთვლილი სხვაობების დიდი ნაწილი შეიძლება ამით იყოს გამოწვეული.")
        todo = ("MHR-ში დააგენერირე ამ თარიღის ფაილი და მერე დააჭირე „ხელახლა შედარება“. თუ ქაღალდი ძველი ვერსიის ზუსტი "
                "ასლია და მხოლოდ თარიღი შეიცვალა, შეგიძლია „ატვირთვა მაინც“.")
    else:
        head = "MHR-ში ფაილი ქაღალდის შემდეგ შეიცვალა"
        body = (f'შედარდა „{oo["file"]}“ — დაგენერირდა {oo["mhr_date"]}, ქაღალდზე კი {oo["doc_date"]} წერია. შესაძლოა ფაილი '
                "ხელმოწერის შემდეგ ხელახლა დააგენერირეს და ტექსტიც შეიცვალა.")
        todo = "შეადარე ქაღალდი MHR-ის ახლანდელ ფაილს. თუ ქაღალდი სწორია, „ატვირთვა მაინც“; თუ არა — დაბეჭდე ახალი ვერსია და მოაწერინე."
    return (f'<div class="banner tone-warn" role="note">{icon("alert", 18)}<div><b>{head}</b><br>{e(body)}'
            f'<div style="margin-top:6px"><b>რა ვქნა:</b> {e(todo)}</div></div></div>')


def _main_reason(v: dict) -> str:
    lines = _reason_lines(v)
    old = _old_original_line(v)
    if old and lines and (v.get("diffs") or []):   # ბევრი სხვაობის ძირითადი მიზეზი — სიაში ეს ჩანდეს პირველი
        return f"{old} ({len(lines)} სხვაობა)"
    if lines:
        return lines[0] + (f" (და კიდევ {len(lines) - 1})" if len(lines) > 1 else "")
    return (v.get("summary") or [""])[0]


def _why_diff(x: dict, pt: dict) -> str:
    """რატომ: რა წესი გააჩერა და რატომ არ ჩაითვალა წაკითხვის შეცდომად."""
    from ..validation import RECHECK_DISAGREE, RECHECK_STILL
    reason = x.get("reason") or ""
    kind = x.get("kind")
    if RECHECK_DISAGREE in reason:
        return ("Tesseract-მა აქ ზუსტად ორიგინალი წაიკითხა, Claude-მა სხვა, ანუ ერთ-ერთი ცდება. "
                + ("რიცხვი და „არ“ სიტყვა მაინც აჩერებს, ამიტომ შეადარე ქაღალდს." if x.get("significant") else "ფაილს ეს არ აჩერებს, მაგრამ შეადარე ქაღალდს."))
    if kind == "signature":
        return ("ამ ადგილას ხელმოწერის ზომის ნიშანი ვერ მოიძებნა. პროგრამა ადგენს, არის თუ არა ადგილზე ხელმოწერა, მაგრამ ვისი ხელია, ვერ ადგენს.")
    if kind == "handwriting":
        return "გვერდზე ტექსტის გარდა ნიშანია, რომელიც ხელმოწერის ადგილას არ არის: შეიძლება ხელით მიწერილი იყოს."
    if kind == "page_count":
        return "ფაილსა და სკანში გვერდების რაოდენობა სხვადასხვაა."
    if "ციფრები" in reason:
        base = "ამ ადგილას MHR-ის ფაილში და ქაღალდზე სხვა რიცხვი წერია. რიცხვი შეიძლება თანხა, თარიღი, ანგარიში ან პირადი ნომერი იყოს, ამიტომ ერთი ციფრის სხვაობაც აჩერებს."
    elif "სიტყვა „" in reason or "უარყოფა" in reason:
        base = "ასეთი ერთი სიტყვა წინადადების აზრს პირიქით ცვლის, ამიტომ ყოველთვის აჩერებს."
    elif "ამოღებული" in reason:
        base = "ეს ტექსტი MHR-ის ფაილშია, ქაღალდზე კი არ ჩანს."
    elif "ჩამატებული" in reason:
        base = "ეს ტექსტი ქაღალდზეა, MHR-ის ფაილში კი არ არის."
    else:
        base = "ამ ადგილას სხვა სიტყვა წერია და სხვაობა წაკითხვის შეცდომით არ აიხსნება."
    if RECHECK_STILL in reason:
        return base + " გვერდი Claude-მაც ხელახლა წაიკითხა და სხვაობა დარჩა, ამიტომ დიდი ალბათობით ქაღალდზე მართლა ასე წერია."
    if (pt.get("text_source") or "").startswith("ocr:tesseract"):
        return base + " გვერდი მხოლოდ Tesseract-მა წაიკითხა, ამიტომ წაკითხვის შეცდომაც შეიძლება იყოს: შეადარე ქაღალდს."
    return base


_TODO_TEXT = ("შეადარე ქაღალდს. თუ ქაღალდზე მართლა ასე წერია, დოკუმენტი MHR-ის ფაილს არ ემთხვევა: ხელახლა დაბეჭდე და მოაწერინე. "
              "თუ ქაღალდზე სწორად წერია, {action}.")
_TODO_KIND = {"signature": "გახსენი ქაღალდი. თუ ხელმოწერა მართლა აკლია, მოაწერინე და ხელახლა დაასკანერე. თუ არის, {action}.",
              "handwriting": "გახსენი გვერდი და ნახე, რა წერია. თუ ეს ბეჭედი ან ხელმოწერაა, {action}.",
              "page_count": "შეამოწმე, ყველა გვერდი დასკანერდა თუ არა."}
_TODO_CHECK = {"already_uploaded": "ნახე თანამშრომლის ბარათი MHR-ში. თუ ეს ახალი ვერსიაა, {action}; თუ იგივეა, უარყავი.",
               "original_contract": "ჯერ MHR-ში დააგენერირე ფაილი და მერე „ხელახლა შედარება“, ან დოკუმენტი ქაღალდზე ნახე და ატვირთე ხელით.",
               "personal_number": "შეამოწმე პირადი ნომერი ქაღალდზე და MHR-ში."}
_TODO_FIELD = "შეამოწმე, რომელია სწორი: დოკუმენტი თუ MHR-ის ჩანაწერი. თუ დოკუმენტი სწორია, {action}."


def _error_text(error: str) -> str:
    """MHR-ის მანქანური კოდი → ადამიანის ენა; უცნობი ისევ ჩანს, ტექნიკური დეტალისთვის."""
    from ..validation import MHR_ERROR_KA
    if error in MHR_ERROR_KA:
        return f"{MHR_ERROR_KA[error]} (კოდი: {error})"
    if error.startswith(("MHR unavailable", "MhrUnavailable")):
        return "MHR ახლა მიუწვდომელია. პროგრამა მოგვიანებით თავად სცდის, ჩარევა არ სჭირდება"
    return error


_PAST_APPROVAL = {"approved": "ატვირთვის რიგშია", "uploading": "იტვირთება", "uploaded": "ატვირთულია"}


def _action_name(m) -> str:
    """რომელი ღილაკი არსებობს ამ მდგომარეობაში (რა ვქნა-ს ტექსტისთვის); დადასტურებულზე საქმე აღარ არის."""
    if m["status"] in _PAST_APPROVAL:
        return ""
    if m["status"] == "ready":
        return "„ატვირთვა“"
    return "„ატვირთვა მაინც“" if m["status"] == "failed" and m["employee_id"] else "„ხელით ატვირთვა“"


def _highlight(parts: list[str], cls: str) -> str:
    if len(parts) != 3:
        return ""
    middle = f'<mark class="{cls}">{e(parts[1])}</mark>' if parts[1] else '<span class="muted">(აქ არაფერი წერია)</span>'
    return f'{e(parts[0])} {middle} {e(parts[2])}'


def _overlay(boxes: list[list[int]], tone: str = "", view: tuple[int, int, int, int] = (0, 0, 1000, 1000)) -> str:
    """მონიშვნა სურათზე: კოორდინატები მეათასედებია, ამიტომ სურათის ზომაზე არ არის დამოკიდებული. `view` — სურათის მონაკვეთი."""
    vx0, vy0, vx1, vy1 = view
    w, h = max(vx1 - vx0, 1), max(vy1 - vy0, 1)
    out = []
    for x0, y0, x1, y1 in boxes:
        out.append(f'<span class="hl {tone}" style="left:{(x0 - vx0) * 100 / w:.1f}%;width:{max(x1 - x0, 6) * 100 / w:.1f}%;'
                   f'top:{(y0 - vy0) * 100 / h:.1f}%;height:{max(y1 - y0, 6) * 100 / h:.1f}%"></span>')
    return "".join(out)


def _crop(doc_id: str, n: int, pt: dict, boxes: list[list[int]], tone: str = "", purged: bool = False) -> str:
    """გვერდის მონაკვეთი პრობლემის გარშემო, მონიშნული ადგილით."""
    if not boxes or "page_index" not in pt or purged:
        return ""
    y0, y1 = max(0, min(b[1] for b in boxes) - CROP_PAD_Y), min(1000, max(b[3] for b in boxes) + CROP_PAD_Y)
    x0, x1 = max(0, min(b[0] for b in boxes) - CROP_PAD_X), min(1000, max(b[2] for b in boxes) + CROP_PAD_X)
    if x1 - x0 < CROP_MIN_W:
        grow = (CROP_MIN_W - (x1 - x0)) // 2
        x0, x1 = max(0, x0 - grow), min(1000, x1 + grow)
    return (f'<a class="pgfig" href="/mhr/{e(doc_id)}/page/{n}" title="მთელი გვერდი"><img src="/page/{e(doc_id)}/{int(pt["page_index"])}.png?dpi=170&crop={x0},{y0},{x1},{y1}" '
            f'alt="გვერდი {n}, პრობლემის ადგილი">{_overlay(boxes, tone, (x0, y0, x1, y1))}</a>')


def _problem_why(x: dict, pts: list[dict]) -> str:
    n = int(x.get("page") or 0)
    return _why_diff(x, pts[n - 1] if 0 < n <= len(pts) else {})


def _problem_todo(x: dict, action: str) -> str:
    return _TODO_KIND.get(x.get("kind"), _TODO_TEXT).format(action=action) if action else ""


def _problem_card(doc_id: str, x: dict, pts: list[dict], action: str, purged: bool = False, in_group: bool = False,
                  shared: bool = False) -> str:
    """ერთი პრობლემა. `in_group` — ჯგუფში სათაური უკვე ზემოთაა, ბარათზე მხოლოდ ადგილი ჩანს;
    `shared` — „რატომ“ და „რა ვქნა“ ჯგუფის ყველა ბარათზე ერთნაირია და ჯგუფის თავში ერთხელ წერია."""
    n = int(x.get("page") or 0)
    pt = pts[n - 1] if 0 < n <= len(pts) else {}
    lines = []
    if x.get("orig_parts"):
        lines.append(f'<div><span class="lbl">MHR-ის ფაილში წერია</span><div class="val">{_highlight(x["orig_parts"], "o")}</div></div>')
        lines.append(f'<div><span class="lbl">ქაღალდზე წერია</span><div class="val">{_highlight(x["scan_parts"], "s")}</div></div>')
    elif x.get("original") or x.get("scanned"):
        lines.append(f'<div><span class="lbl">MHR-ის ფაილში წერია</span><div class="val">{e(x.get("original") or "—")}</div></div>')
        lines.append(f'<div><span class="lbl">ქაღალდზე წერია</span><div class="val">{e(x.get("scanned") or "—")}</div></div>')
    if not shared:
        lines.append(f'<div><span class="lbl">{"რატომ გაჩერდა" if x.get("significant") else "რატომ საეჭვოა"}</span><div class="val">{e(_why_diff(x, pt))}</div></div>')
        if action:
            lines.append(f'<div><span class="lbl">რა ვქნა</span><div class="val">{e(_problem_todo(x, action))}</div></div>')
    if 0 < n <= len(pts):
        lines.append(f'<div><a href="/mhr/{e(doc_id)}/page/{n}">მთელი გვერდი {n}</a></div>')
    crop = _crop(doc_id, n, pt, x.get("boxes") or [], purged=purged)
    title = "" if in_group else f'<b>{e(_clean_reason(x.get("reason")) or _DIFF_KA.get(x.get("kind"), ""))}</b>'
    head = (f'<div class="prob-h"><b class="place">{e(_diff_place(x, pts))}</b></div>' if in_group else
            f'<div class="prob-h"><span class="ib tone-bad">{icon("alert", 18)}</span><div>{title}<div class="m">{e(_diff_place(x, pts))}</div></div></div>')
    return (f'<div class="prob">{head}'
            + (f'<div class="prob-b"><div class="lines">{"".join(lines)}</div><div>{crop}</div></div>' if crop else f'<div class="lines">{"".join(lines)}</div>') + "</div>")


def _field_place(c: dict, meta: dict, pts: list[dict]) -> tuple[int, list[list[int]]]:
    """სად წერია ველი ქაღალდზე: (ფაილის გვერდი, მონიშვნა). ამოღებისას გვერდი სკანის ნომრითაა, ფაილში კი შეიძლება სხვა იყოს; 0 — უცნობია."""
    from ..textcompare import locate
    field = (meta.get("fields") or {}).get(c.get("field")) or {}
    for n, pt in enumerate(pts, start=1):
        if int(pt.get("page_index", -1)) + 1 == field.get("page"):
            return n, locate(pt.get("words") or [], ["", str(field.get("raw") or ""), ""])
    return 0, []


def _field_problems(v: dict, meta: dict, pts: list[dict]) -> dict[int, list[tuple[dict, list[list[int]]]]]:
    """გვერდი → გაჩერებული ველები, რომლებიც ამ გვერდზე წერია."""
    out: dict[int, list[tuple[dict, list[list[int]]]]] = {}
    for ch in _blocking_checks(v):
        n, boxes = _field_place(ch, meta, pts)
        if n:
            out.setdefault(n, []).append((ch, boxes))
    return out


def _check_card(c: dict, action: str, doc_id: str, meta: dict, pts: list[dict], purged: bool = False) -> str:
    n, boxes = _field_place(c, meta, pts)
    differs = c.get("status") == "mismatch" and c.get("mhr_value") is not None
    lines = []
    if differs:
        lines.append(f'<div><span class="lbl">დოკუმენტში წერია</span><div class="val"><mark class="s">{e(c.get("document_value"))}</mark></div></div>')
        lines.append(f'<div><span class="lbl">MHR-ის ჩანაწერში წერია</span><div class="val"><mark class="o">{e(c.get("mhr_value"))}</mark></div></div>')
    why = " ".join(x for x in ("ქაღალდზე და MHR-ის ჩანაწერში სხვადასხვა მნიშვნელობა წერია." if differs else "", c.get("note")) if x)
    if why:
        lines.append(f'<div><span class="lbl">რატომ გაჩერდა</span><div class="val">{e(why)}</div></div>')
    if action:
        todo = _TODO_CHECK.get(c.get("field"), _TODO_FIELD).format(action=action)
        lines.append(f'<div><span class="lbl">რა ვქნა</span><div class="val">{e(todo)}</div></div>')
    if n:
        lines.append(f'<div><a href="/mhr/{e(doc_id)}/page/{n}">მთელი გვერდი {n}</a></div>')
    crop = _crop(doc_id, n, pts[n - 1], boxes, purged=purged) if n else ""
    return (f'<div class="prob"><div class="prob-h"><span class="ib tone-bad">{icon("alert", 18)}</span><div><b>{e(c.get("label_ka") or c.get("field"))}</b>'
            f'<div class="m">{e(_diff_place({"page": n}, pts)) if n else "დოკუმენტის მონაცემები და MHR-ის ბარათი"}</div></div></div>'
            + (f'<div class="prob-b"><div class="lines">{"".join(lines)}</div><div>{crop}</div></div>' if crop else f'<div class="lines">{"".join(lines)}</div>') + "</div>")


def _verdict(v: dict, m) -> str:
    """ერთი წინადადება ზემოთ: რა მოხდა და რამდენი პრობლემაა."""
    lines = _reason_lines(v)
    past = _PAST_APPROVAL.get(m["status"])
    if v.get("status") == "split":
        return f'<div class="banner tone-info">{icon("file", 18)}<div>{"<br>".join(e(x) for x in v.get("summary") or [])}</div></div>'
    if lines and past:
        return (f'<div class="banner tone-info">{icon("file", 18)}<div><b>ფაილი {past} შენი გადაწყვეტილებით.</b> '
                f'შემოწმებამ {len(lines)} პრობლემა ნახა, თითოეული ქვემოთაა.</div></div>')
    if lines:
        return (f'<div class="banner tone-warn">{icon("alert", 18)}<div><b>ფაილი გაჩერდა: {len(lines)} '
                f'{"პრობლემა" if len(lines) == 1 else "პრობლემა"}.</b> თითოეული ქვემოთაა: სად არის, რა წერია და რატომ გაჩერდა.</div></div>')
    if v.get("status") == "passed":
        return (f'<div class="banner tone-ok">{icon("check", 18)}<div><b>ყველა შემოწმება გავიდა, {f"ფაილი {past}" if past else "მზადაა ატვირთვისთვის"}.</b> '
                'ქვემოთ ჩანს, რა შემოწმდა და რა ნახა პროგრამამ თითო გვერდზე.</div></div>')
    if v.get("summary"):
        return f'<div class="banner tone-warn">{icon("alert", 18)}<div>{"<br>".join(e(x) for x in v["summary"])}</div></div>'
    return ""


def _notes_block(v: dict, skip: frozenset[str] = frozenset()) -> str:
    """შენიშვნები, რომლებიც არ აჩერებს. `skip` — ველები, რომლებზეც ზემოთ ცალკე გაფრთხილება უკვე ჩანს."""
    notes = [c["note"] for c in v.get("checks") or [] if c.get("status") == "info" and not c.get("blocking") and c.get("note")
             and c.get("field") not in skip]
    notes += [c.get("note") or f'{c.get("label_ka")}: დოკუმენტში „{c.get("document_value")}“, MHR-ში „{c.get("mhr_value")}“'
              for c in v.get("checks") or [] if c.get("status") == "mismatch" and not c.get("blocking")]
    notes += [s for s in v.get("summary") or [] if s.startswith("ყურადღება")]
    if not notes:
        return ""
    return (f'<div class="banner tone-info">{icon("file", 18)}<div><b>შენიშვნა, არ აჩერებს:</b><br>' + "<br>".join(e(x) for x in notes) + "</div></div>")


def _sig_checked(n: int, total: int, is_contract: bool) -> bool:
    return is_contract or n == total


def _page_signature(pt: dict, checked: bool) -> tuple[str, str]:
    """(ტექსტი, ტონი): ამ გვერდის ხელმოწერის შედეგი."""
    from ..validation import party_name
    if not checked:
        return "ამ ფაილზე მხოლოდ ბოლო გვერდზე მოწმდება", "mute"
    if pt.get("marks_only"):
        return "მხოლოდ ხელმოწერის გვერდია", "ok"
    parties = pt.get("signature_parties") or {}
    if parties:
        text = " · ".join(f'{party_name(p, pt)}: {"არის" if parties[p] else "აკლია"}' for p in ("employer", "employee") if p in parties)
        return text, "ok" if all(parties.values()) else "bad"
    hint = pt.get("signature_hint")
    if hint == "present":
        return "ხელმოწერა ჩანს, მხარეები ვერ გაირჩა", "warn"
    if hint == "not_detected":
        return "ხელმოწერის ადგილი ამ გვერდზე არ ჩანს", "warn"
    return "ხელმოწერა ვერ დადგინდა", "warn"


_PN_ON_PAGE = re.compile(r"(?<!\d)\d{11}(?!\d)")


def _reason_where(reason: str, meta: dict, pts: list[dict], parent) -> list[str]:
    """გადასამოწმებლის მიზეზი: სად ჩანს ეს დოკუმენტში (უკვე HTML-ისთვის მომზადებული ხაზები)."""
    def scan_page(pt: dict) -> int:
        return int(pt.get("page_index", 0)) + 1
    if reason == "multiple_employees":
        lines: dict[str, list[int]] = {}
        for pt in pts:
            for pn in sorted(set(_PN_ON_PAGE.findall(pt.get("text") or ""))):
                lines.setdefault(pn, []).append(scan_page(pt))
        return [f"პირადი ნომერი {e(pn)}: გვერდი {e(_page_list(pages))}" for pn, pages in lines.items()]
    if reason == "ocr_unreadable":
        return [f"გვერდი {scan_page(pt)}: წაკითხვის ხარისხი {float(pt.get('ocr_quality') or 0):.2f}" for pt in pts]
    if reason == "pages_not_in_mhr" and parent is not None:
        rows = []
        for part in json.loads(parent["validation"] or "{}").get("parts") or []:
            if not part.get("matched"):
                rows += [f"გვერდი {e(pg)}: ყველაზე მეტად ჰგავს „{e(m[0])}“-ს, {m[1]}% (საჭიროა მინიმუმ {OWNER_MIN_PCT}%)" for pg, m in (part.get("page_match") or {}).items()]
        return rows
    if reason == "possible_duplicate":
        return [f'მსგავსი დოკუმენტი: <a href="/doc/{e(m["document_id"])}">გახსენი</a>' for m in ((meta.get("duplicate") or {}).get("matches") or [])[:3] if m.get("document_id")]
    if reason == "uncertain_boundary" and pts:
        return [f"გვერდები {e(_page_list([scan_page(pt) for pt in pts]))}"]
    return []


def _is_contract(c: Config, d) -> bool:
    return bool(d is not None and d["type_id"] in (c.section("mhr").get("contract_type_ids") or []))


def _page_overlays(pt: dict, diffs: list[dict], checked: bool) -> str:
    out = []
    for x in diffs:
        if x.get("significant"):
            out.append(_overlay(x.get("boxes") or []))
        elif x.get("suspect"):
            out.append(_overlay(x.get("boxes") or [], "warn"))
    if checked:   # ნაპოვნი ხელმოწერა მწვანედ
        zones = pt.get("signature_boxes") or {}
        parties = pt.get("signature_parties") or {}
        out.append(_overlay([zones[p] for p, found in parties.items() if found and p in zones], "ok"))
    return "".join(out)


def _page_map(c: Config, m, d, v: dict, pts: list[dict]) -> str:
    if not pts or d is None or v.get("status") == "split" or not v:
        return ""
    doc_id = m["document_id"]
    is_contract = _is_contract(c, d)
    compared = _text_compared(v)
    field_bad = _field_problems(v, json.loads(d["metadata"] or "{}"), pts)
    if d["purged_at"]:
        return f'<h2>გვერდ-გვერდ</h2><div class="banner tone-mute">{icon("file", 18)}<div>{PURGED_NOTE}</div></div>'
    cards = []
    for n, pt in enumerate(pts, start=1):
        here = [x for x in v.get("diffs") or [] if x.get("page") == n]
        fields = field_bad.get(n, [])
        here += [{"significant": True, "boxes": boxes} for _ch, boxes in fields]
        sig = [x for x in here if x.get("significant")]
        text_bad = [x for x in sig if x.get("kind") in ("changed", "added", "removed")]
        suspect = [x for x in here if x.get("suspect")]
        noise = sum(1 for x in here if not x.get("significant") and not x.get("suspect") and x.get("kind") in ("changed", "added", "removed"))
        checked = _sig_checked(n, len(pts), is_contract)
        sig_text, sig_tone = _page_signature(pt, checked)
        text_line = (f'ტექსტი: {len(text_bad)} სხვაობა' if text_bad else f'ტექსტი: {len(suspect)} საეჭვო' if suspect
                     else "ტექსტი: არ შედარებულა" if not compared else "ტექსტი: ემთხვევა" + (f" ({noise} წვრილი)" if noise else ""))
        tone = "bad" if sig else "warn" if suspect else ""
        st = (chip("პრობლემა", "bad") if sig else chip("საეჭვო", "warn") if suspect
              else chip("წესრიგშია", "ok") if compared else chip("არ შედარებულა", "mute"))
        cards.append(f'<a class="pgcard {tone}" href="/mhr/{e(doc_id)}/page/{n}"><span class="cap"><b>გვ. {n}</b>{st}</span>'
                     f'<span class="pgfig"><img src="/page/{e(doc_id)}/{int(pt.get("page_index", 0))}.png?dpi=60" loading="lazy" alt="გვერდი {n}">{_page_overlays(pt, here, checked)}</span>'
                     f'<span class="m">{e(text_line)}</span>' + "".join(f'<span class="m">{e(ch.get("label_ka"))} განსხვავდება</span>' for ch, _b in fields)
                     + f'<span class="m">{e(sig_text)}</span><span class="m">წაიკითხა: {e(_reader(pt))}</span></a>')
    return ('<h2>გვერდ-გვერდ</h2><p class="muted" style="margin:-6px 0 12px;font-size:13.5px">წითელი ჩარჩო პრობლემაა, ყვითელი საეჭვო, მწვანე ნაპოვნი ხელმოწერა. '
            f'გვერდზე დაჭერით დიდად გაიხსნება.</p><div class="pgmap">{"".join(cards)}</div>')


def _checked_block(c: Config, m, d, v: dict, pts: list[dict]) -> str:
    """რა შემოწმდა: ყოველი ნაბიჯი, შედეგით და ახსნით, მათ შორის რაც გავიდა. MHR მიუწვდომელი იყო: ჯერ არაფერი შემოწმებულა."""
    if not v or v.get("status") == "split" or _mhr_did_not_answer(v):
        return ""
    steps = []

    def step(tone: str, title: str, text: str) -> None:
        ic = {"ok": "check", "bad": "x", "warn": "alert", "mute": "file"}[tone]
        steps.append(f'<div class="st"><span class="ib tone-{tone}">{icon(ic, 18)}</span><div><b>{e(title)}</b><span class="d">{e(text)}</span></div></div>')

    emp = v.get("employee") or {}
    if emp:
        step("ok", "თანამშრომელი MHR-ში ნაპოვნია", f'{_mhr_name(v)} · პირადი ნომერი {m["personal_number"] or "—"} · MHR ID {emp.get("employee_id")}')
    else:
        step("bad", "თანამშრომელი MHR-ში ვერ მოიძებნა", f'პირადი ნომერი {m["personal_number"] or "—"} — შეიძლება ნომერი არასწორად წერია ან წაიკითხა.')
    o = v.get("original") or {}
    if o:
        step("ok", "რას შევადარე", f'{o.get("filename") or o.get("title")} · MHR ID {o.get("doc_id")}' + (f' · შექმნილია {str(o.get("created_at"))[:10]}' if o.get("created_at") else ""))
    elif _no_compare(v):
        step("bad", "შესადარებელი ფაილი ვერ მოიძებნა", "თანამშრომლის ბარათზე დაგენერირებული ფაილი არ არის, ამიტომ ტექსტი არაფერს შედარებია.")
    diffs = v.get("diffs") or []
    if o:
        text_diffs = [x for x in diffs if x.get("kind") in ("changed", "added", "removed", "page_count")]
        bad = [x for x in text_diffs if x.get("significant")]
        suspect = [x for x in text_diffs if x.get("suspect")]
        reread = [n for n, pt in enumerate(pts, start=1) if "recheck" in (pt.get("text_source") or "")]
        noise = len(text_diffs) - len(bad) - len(suspect)
        detail = (f'{len(pts)} გვერდი შევადარე სიტყვა-სიტყვით. ' + (f"წვრილი წაკითხვის სხვაობა: {noise} (ცვლილებად არ ითვლება). " if noise else "")
                  + (f'Claude-მა ხელახლა წაიკითხა გვერდი {_page_list(reread)}.' if reread else "ხელახლა წაკითხვა არ დასჭირვებია."))
        if bad:
            step("bad", f"ტექსტი: {len(bad)} ნამდვილი სხვაობა", detail)
        elif suspect:
            step("warn", f"ტექსტი: {len(suspect)} პატარა სხვაობა ვერ გადამოწმდა", detail)
        else:
            step("ok", "ტექსტი ემთხვევა", detail)
    if pts:
        is_contract = _is_contract(c, d)
        sig_bad = [x for x in diffs if x.get("kind") == "signature" and x.get("significant")]
        rule = "ხელშეკრულებაზე ხელმოწერა ყველა გვერდზე მოწმდება, ორივე მხარისთვის." if is_contract else "ამ ფაილზე ხელმოწერა ბოლო გვერდზე მოწმდება, ორივე მხარისთვის."
        if sig_bad:
            step("bad", "ხელმოწერა აკლია", f'{rule} პრობლემა: {"; ".join(_diff_line(x) for x in sig_bad)}.')
        else:
            last_text, _tone = _page_signature(pts[-1], True)
            step("ok", "ხელმოწერები ადგილზეა", f"{rule} ბოლო გვერდი: {last_text}. ვისი ხელია, პროგრამა ვერ ადგენს.")
    dup = [ch for ch in v.get("checks") or [] if ch.get("field") == "already_uploaded"]
    if any(ch.get("blocking") for ch in dup):
        step("bad", "ეს ფაილი ბარათზე უკვე ატვირთულია", next(ch.get("note") or "" for ch in dup if ch.get("blocking")))
    elif dup:
        note = " ".join(ch.get("note") or "" for ch in dup)
        step("ok", "ორჯერ ატვირთვა", f"ატვირთვამდე შემოწმდა: {note}" if m["status"] in _PAST_APPROVAL else note)
    return f'<h2>რა შემოწმდა</h2><div class="card steps">{"".join(steps)}</div>' if steps else ""


def _page_view(c: Config, m, d, pts: list[dict], n: int) -> str:
    v = json.loads(m["validation"] or "{}")
    doc_id = m["document_id"]
    pt = pts[n - 1]
    here = [x for x in v.get("diffs") or [] if x.get("page") == n]
    checked = _sig_checked(n, len(pts), _is_contract(c, d))
    sig_text, sig_tone = _page_signature(pt, checked)
    nav = (f'<div class="btns" style="margin-bottom:14px"><a class="btn sm" href="/mhr/{e(doc_id)}">← ფაილის შედეგი</a>'
           + (f'<a class="btn sm" href="/mhr/{e(doc_id)}/page/{n - 1}">გვერდი {n - 1}</a>' if n > 1 else "")
           + (f'<a class="btn sm" href="/mhr/{e(doc_id)}/page/{n + 1}">გვერდი {n + 1}</a>' if n < len(pts) else "") + "</div>")
    action = _action_name(m)
    purged = bool(d["purged_at"])
    problems = "".join(_problem_card(doc_id, x, pts, action, purged).replace('<div class="prob-b">', '<div>') for x in here if x.get("significant") or x.get("suspect"))
    meta = json.loads(d["metadata"] or "{}")
    fields = _field_problems(v, meta, pts).get(n, [])
    problems += "".join(_check_card(ch, action, doc_id, meta, pts, purged).replace('<div class="prob-b">', '<div>') for ch, _b in fields)
    noise = [x for x in here if not x.get("significant") and not x.get("suspect")]
    noise_html = "".join(f'<div class="row" style="align-items:flex-start"><div class="grow lines"><div><span class="lbl">MHR-ის ფაილში</span><div class="val">{_highlight(x.get("orig_parts") or [], "o") or e(x.get("original"))}</div></div>'
                         f'<div><span class="lbl">წაკითხული</span><div class="val">{_highlight(x.get("scan_parts") or [], "s") or e(x.get("scanned"))}</div></div></div></div>' for x in noise[:80])
    side = (f'<div class="card steps"><div class="st"><div><b>ხელმოწერა</b><span class="d">{e(sig_text)}</span></div></div>'
            f'<div class="st"><div><b>ვინ წაიკითხა</b><span class="d">{e(_reader(pt))}, ხარისხი {float(pt.get("ocr_quality") or 0):.2f}</span></div></div>'
            f'<div class="st"><div><b>სკანის გვერდი</b><span class="d">{int(pt.get("page_index", 0)) + 1}</span></div></div></div>')
    return (f'{nav}<h1>გვერდი {n}</h1><p class="sub">{e(clean_file_title((v.get("original") or {}).get("filename")) or _mhr_name(v) or "")}</p>'
            + (f'<div class="pgview"><div class="banner tone-mute">{icon("file", 18)}<div>{PURGED_NOTE}</div></div><div>' if purged else
               f'<div class="pgview"><div class="pgfig"><img src="/page/{e(doc_id)}/{int(pt.get("page_index", 0))}.png?dpi=130" alt="გვერდი {n}">{_page_overlays(pt, [*here, *({"significant": True, "boxes": b} for _ch, b in fields)], checked)}</div><div>')
            + (f'<h2 style="margin-top:0">რა არის ამ გვერდზე</h2><div class="card list">{problems}</div>' if problems
               else f'<div class="banner tone-ok">{icon("check", 18)}<div>ამ გვერდზე პრობლემა არ არის.</div></div>' if _text_compared(v)
               else f'<div class="banner tone-mute">{icon("file", 18)}<div>ტექსტი არაფერს შედარებია, ამიტომ ეს გვერდი ქაღალდზე ნახე.</div></div>')
            + f'<h2>დეტალები</h2>{side}'
            + (f'<details style="margin-top:12px"><summary>წვრილი წაკითხვის სხვაობები ({len(noise)}) — ცვლილებად არ ითვლება</summary><div class="card list" style="margin-top:10px">{noise_html}</div></details>' if noise else "")
            + f'<details style="margin-top:12px"><summary>რა წაიკითხა ამ გვერდზე</summary><pre style="white-space:pre-wrap;word-break:break-word;font:13.5px/1.6 inherit">{e(pt.get("text") or "")}</pre></details>'
            + "</div></div>")


def _mhr_detail_body(c: Config, m, d, worker_alive: bool = True, pts: list[dict] | None = None, parts: dict | None = None) -> str:
    v = json.loads(m["validation"] or "{}")
    emp = v.get("employee") or {}
    pts = pts or []
    doc_id = m["document_id"]
    meta = json.loads(d["metadata"] or "{}") if d is not None else {}
    file_title = (meta.get("part") or {}).get("title") or clean_file_title((v.get("original") or {}).get("filename"))
    scan_pages = [int(p["page_index"]) + 1 for p in pts if "page_index" in p]
    out = [f'<p><a href="/mhr">← MHR</a></p><div class="chips" style="margin-bottom:8px">{mhr_chip(m["status"])}</div>'
           f'<h1>{e(_mhr_name(v) or "თანამშრომელი")}{" · " + e(file_title) if file_title else ""}</h1><p class="sub">{e(m["personal_number"] or "")}'
           + (f' · MHR ID {e(emp.get("employee_id"))}' if emp.get("employee_id") else "")
           + (f' · ატვირთვის ID {e(m["remote_id"])}' if m["remote_id"] else "")
           + (f' · {e(Path(d["first_path"]).name)}, სკანის გვერდები {e(_page_list(scan_pages))}' if d is not None and scan_pages and d["first_path"] else "") + "</p>"]
    if m["error"] and m["error"] != "MhrError":
        out.append(f'<div class="banner tone-bad">{icon("x", 18)}<div>{e(_error_text(m["error"]))}</div></div>')
    out.append(_verdict(v, m))
    old_banner = _old_original_banner(v, m)
    out.append(old_banner)
    out.append(_notes_block(v, frozenset({"original_date"}) if old_banner else frozenset()))
    # გადაწყვეტილება ზემოთაა, პრობლემების სიის წინ: ადრე ღილაკები ~15 ეკრანის ქვემოთ იყო, AI-ის დასკვნა კი ~12-ზე
    out.append(_decision_block(c, m, d, v))
    sig_diffs = sorted((x for x in v.get("diffs") or [] if x.get("significant")), key=lambda x: x.get("page") or 0)
    action = _action_name(m)
    purged = bool(d is not None and d["purged_at"])
    if sig_diffs or _blocking_checks(v):
        out.append(f'<h2>{"რა ნახა შემოწმებამ" if m["status"] in _PAST_APPROVAL else "რატომ გაჩერდა"}</h2>'
                   + "".join(f'<div class="card list" style="margin-bottom:12px">{_check_card(ch, action, doc_id, meta, pts, purged)}</div>'
                             for ch in _blocking_checks(v))
                   + _grouped_problems(doc_id, sig_diffs, pts, action, purged))
    suspects = [x for x in v.get("diffs") or [] if x.get("suspect") and not x.get("significant")]
    if suspects:
        out.append('<h2>საეჭვო პატარა სხვაობები, არ აჩერებს</h2>' + _grouped_problems(doc_id, suspects, pts, action, purged))
    if parts is not None:
        out.append(_mhr_parts_block(parts, m))
    out.append(_ai_block(c, m, worker_alive))
    out.append(_checked_block(c, m, d, v, pts))
    out.append(_page_map(c, m, d, v, pts))
    out.append(_data_and_text_blocks(v))
    return "".join(out)


# რაც უფრო მაღლაა, მით უფრო მნიშვნელოვანია: ციფრი/ამოღებული ტექსტი — ნამდვილი ცვლილება; ხელნაწერი/ბეჭედი — ხშირად ხმაური
_DIFF_PRIORITY = {"changed": 0, "removed": 1, "page_count": 1, "added": 2, "signature": 3, "handwriting": 4}


def _grouped_problems(doc_id: str, diffs: list[dict], pts: list[dict], action: str, purged: bool) -> str:
    """ერთნაირი პრობლემები ერთ ჯგუფად: „ხელნაწერის ეჭვი · 13 გვერდი“ ერთი სტრიქონია და არა 13 ბარათი.
    მნიშვნელოვანი (ციფრი, ამოღებული ტექსტი) და პატარა ჯგუფები ღიაა, დიდი და ხშირად ხმაურიანი — დაკეცილი."""
    groups: dict[str, list[dict]] = {}
    for x in diffs:
        groups.setdefault(_clean_reason(x.get("reason")) or _DIFF_KA.get(x.get("kind"), "") or "სხვაობა", []).append(x)
    out = []
    for title, xs in sorted(groups.items(), key=lambda kv: (min(_DIFF_PRIORITY.get(x.get("kind"), 2) for x in kv[1]), kv[0])):
        pages = sorted({int(x["page"]) for x in xs if x.get("page")})
        where = (f'{len(pages)} გვერდზე' if len(pages) > 6 else
                 f'გვერდ{"ი" if len(pages) == 1 else "ები"} {_page_list(pages)}' if pages else "")
        important = min(_DIFF_PRIORITY.get(x.get("kind"), 2) for x in xs) <= 1
        tone = "bad" if important else "warn"
        whys = {_problem_why(x, pts) for x in xs}
        todos = {_problem_todo(x, action) for x in xs}
        shared = len(xs) > 1 and len(whys) == 1 and len(todos) == 1
        intro = ""
        if shared:
            label = "რატომ გაჩერდა" if any(x.get("significant") for x in xs) else "რატომ საეჭვოა"
            intro = (f'<div class="lines gintro"><div><span class="lbl">{label}</span><div class="val">{e(next(iter(whys)))}</div></div>'
                     + (f'<div><span class="lbl">რა ვქნა</span><div class="val">{e(next(iter(todos)))}</div></div>' if action else "")
                     + "</div>")
        cards = "".join(_problem_card(doc_id, x, pts, action, purged, in_group=len(xs) > 1, shared=shared) for x in xs)
        out.append(f'<details class="card pgroup"{" open" if important or len(xs) <= 2 else ""}><summary>'
                   f'<span class="ib tone-{tone}">{icon("alert", 18)}</span><span class="grow"><b>{e(title)}</b>'
                   f'<span class="m">{len(xs)} ადგილი{" · " + e(where) if where else ""}</span></span>'
                   f'<span class="chev" aria-hidden="true"></span></summary>{intro}<div class="list">{cards}</div></details>')
    return "".join(out)


def _decision_block(c: Config, m, d, v: dict) -> str:
    """„რა გავაკეთო“: AI-ის მოკლე დასკვნა და ყველა გადაწყვეტილების ღილაკი ერთ ადგილას, გვერდის თავში."""
    ai = _ai_load(m)
    ai_line = ""
    if ai and ai.get("verdict") in AI_KA and ai.get("verdict") != "error" and ai.get("summary"):
        lbl, tone = AI_KA[ai["verdict"]]
        ai_line = (f'<div class="ai-line"><a class="chip tone-{tone}" href="#ai" title="AI-ის სრული ანალიზი">{e(lbl)}</a>'
                   f'<p>{e(ai["summary"])} <a href="#ai">სრულად</a></p></div>')
    btn = {"ready": [("approve", "ატვირთვა MHR-ზე", "primary"), ("reject", "უარყოფა", "")],
           "failed": [("revalidate", "ხელახლა შედარება", ""), ("reject", "უარყოფა", "")],
           "upload_failed": [("retry", "ხელახლა ატვირთვა", "primary"), ("revalidate", "ხელახლა შედარება", "")],
           "needs_check": [("revalidate", "შემოწმდა — ხელახლა შედარება", "")],
           "unavailable": [("revalidate", "ახლავე ცდა", "")], "rejected": [("revalidate", "ხელახლა შედარება", "")]}.get(m["status"], [])
    links = ""
    if d is not None:
        links = (f'<a class="btn ghost" href="/doc/{e(d["id"])}">{icon("file", 16)}დოკუმენტი</a>'
                 f'<a class="btn ghost" href="/file/output/{e(d["id"])}" target="_blank">{icon("open", 16)}PDF</a>')
    no_compare = _no_compare(v)
    override = ""
    if m["status"] == "failed" and m["employee_id"]:
        ai_ok = bool(ai and ai.get("verdict") == "upload")
        if no_compare:
            lead = "შედარება არ მომხდარა — დოკუმენტი თვითონ ვნახე და სწორია"
            hint = ("MHR-ში დაგენერირებული ხელშეკრულება ვერ მოიძებნა, ამიტომ ტექსტი არაფერს შედარებია. "
                    "ატვირთვა ჟურნალში ჩაიწერება, როგორც შენი გადაწყვეტილება.")
        else:
            lead = ("AI-მ სხვაობები გადახედა და თვლის, რომ ეს წაკითხვის შეცდომებია — დოკუმენტი სწორია" if ai_ok
                    else "ქვემოთ ჩამოთვლილი პრობლემები ქაღალდზე ვნახე — დოკუმენტი სწორია")
            hint = "მაგ. ბეჭედი ან ხელმოწერა შეცდომით ჩაითვალა ცვლილებად. ატვირთვა ჟურნალში ჩაიწერება, როგორც შენი გადაწყვეტილება."
        override = (f'<form method="post" action="/mhr/{e(m["document_id"])}/override" class="override">'
                    f'<label class="switch"><input type="checkbox" name="confirm" value="1" required>'
                    f'<span><b>{lead}</b><br><span class="muted" style="font-size:13.5px">{hint}'
                    f'</span></span></label><button class="btn{" primary" if ai_ok else ""}">{icon("cloud", 16)}ატვირთვა მაინც</button></form>')
    actions = "".join(
        f'<form class="inline" method="post" action="/mhr/{e(m["document_id"])}/{a}"><button class="btn {cls}">{e(lbl)}</button></form>'
        for a, lbl, cls in btn) + links
    manual = ""
    if d is not None and m["status"] != "pending" and not _mhr_did_not_answer(v) and (no_compare or not (m["status"] == "failed" and m["employee_id"])):
        blk = _manual_block(c, d, m, f"/mhr/{m['document_id']}")
        if blk:
            title = "ხელით ატვირთვა — შედარების გარეშე" if no_compare else "ხელით ატვირთვა"
            manual = (f'<div class="card" style="padding:14px 16px;margin-bottom:20px"><b>{title}</b>'
                      + ('<div class="muted" style="font-size:13.5px;margin-top:4px">დოკუმენტი ქაღალდზე ნახე; თუ სწორია, '
                         'ერთი დაჭერით აიტვირთება თანამშრომლის ბარათზე.</div>' if no_compare else "")
                      + blk.replace("margin-top:16px", "margin-top:6px") + "</div>")
    if not (override or btn):   # გადასაწყვეტი აღარაფერია (მაგ. უკვე ატვირთულია) — „რა გავაკეთო“ დამაბნეველი იქნება
        return f'<div class="btns" style="margin-bottom:20px">{links}</div>{manual}' if links or manual else ""
    return (f'<section class="card decide" aria-labelledby="decide-h"><h2 id="decide-h">რა გავაკეთო</h2>{ai_line}{override}'
            f'<div class="btns">{actions}</div></section>{manual}')


def _data_and_text_blocks(v: dict) -> str:
    """გვერდის ბოლო: მონაცემების ცხრილი და ტექსტის შედარება MHR-ის ორიგინალთან (დეტალები, ვისაც სჭირდება)."""
    out = []
    checks = [ch for ch in v.get("checks") or [] if ch.get("field") != "already_uploaded"]
    if checks:
        rows = "".join(
            f'<tr><td>{e(ch.get("label_ka") or ch.get("field"))}</td><td>{e(ch.get("document_value") if ch.get("document_value") is not None else "—")}</td>'
            f'<td>{e(ch.get("mhr_value") if ch.get("mhr_value") is not None else "—")}</td>'
            f'<td>{chip(*_CHECK_KA.get(ch.get("status"), (ch.get("status"), "mute")))}{" " + e(ch.get("note")) if ch.get("note") else ""}</td></tr>'
            for ch in checks)
        out.append(f'<h2>მონაცემების შედარება</h2><div class="card" style="overflow-x:auto"><table class="tbl"><tr><th>ველი</th>'
                   f'<th>დოკუმენტში</th><th>MHR-ში</th><th>შედეგი</th></tr>{rows}</table></div>')
    diffs = v.get("diffs") or []
    sig = [x for x in diffs if x.get("significant")]
    noise = [x for x in diffs if not x.get("significant") and not x.get("suspect")]

    def drow(x):
        return (f'<div class="row" style="align-items:flex-start"><div style="min-width:110px"><b>{"გვ. " + str(x["page"]) if x.get("page") else "დოკუმენტი"}</b>'
                f'<div class="m">{e(_DIFF_KA.get(x.get("kind"), x.get("kind")))}</div></div><div class="grow">'
                + (f'<div><span class="muted">ორიგინალი:</span> <del style="background:var(--bad-soft);color:var(--bad)">{e(x.get("original"))}</del></div>' if x.get("original") else "")
                + (f'<div><span class="muted">დასკანერებული:</span> <ins style="background:var(--ok-soft);color:var(--ok);text-decoration:none">{e(x.get("scanned"))}</ins></div>' if x.get("scanned") else "")
                + (f'<div class="m">{e(x.get("reason"))}</div>' if x.get("reason") else "") + "</div></div>")
    template = next((ch for ch in checks if ch.get("field") == "contract_template"), None)
    if v.get("original"):
        o = v["original"]
        out.append(f'<h2>შედარება MHR-ის ორიგინალთან</h2><p class="muted" style="margin-top:-6px;font-size:13px">ორიგინალი MHR-ში: '
                   f'{e(o.get("filename") or o.get("title") or o.get("doc_id"))}{" · " + e(str(o.get("created_at"))[:10]) if o.get("created_at") else ""}</p>')
    elif template:
        out.append(f'<h2>ხელშეკრულების შედარება შაბლონთან</h2><p class="muted" style="margin-top:-6px;font-size:13px">'
                   f'{e(template.get("note") or "")}</p>')
    if v.get("original") or template:
        if not sig:
            out.append(f'<div class="card list">{empty("check", "ok", "შინაარსობრივი ცვლილება არ მოიძებნა")}</div>')
        if noise:
            out.append(f'<details style="margin-top:12px"><summary>OCR-ის წვრილი განსხვავებები ({len(noise)}) — ცვლილებად არ ითვლება</summary>'
                       f'<div class="card list" style="margin-top:10px">{"".join(drow(x) for x in noise[:200])}</div></details>')
    return "".join(out)


_MANUAL_ERR = {"document_not_found": "დოკუმენტი ვერ მოიძებნა ან ჩანაცვლებულია", "no_output_file": "შედეგის PDF აღარ არსებობს",
               "type_unknown": "ჯერ აირჩიე დოკუმენტის ტიპი", "already_approved": "უკვე დადასტურებულია — ატვირთვა მიდის",
               "already_uploading": "ატვირთვა მიმდინარეობს", "already_uploaded": "უკვე ატვირთულია",
               "already_needs_check": "საჭიროებს შემოწმებას — ნახე MHR გვერდზე",
               "state_changed": "სტატუსი ამასობაში შეიცვალა — განაახლე გვერდი"}
_MANUAL_UPLOAD_KA = {"approved": "MHR მიუწვდომელია — ფონური პროცესი მოგვიანებით თავად სცდის",
                     "upload_failed": "ატვირთვა ვერ მოხერხდა", "needs_check": "შედეგი გაურკვეველია — ნახე MHR გვერდზე",
                     "rejected": "დოკუმენტი ჩანაცვლებულია"}


def _doc_next(c: Config, x, item, reasons_html: str, mhr_on: bool) -> str:
    """დოკუმენტის გვერდის თავში ერთი ბარათი: რა მდგომარეობაშია დოკუმენტი და რა არის შემდეგი ნაბიჯი (ერთი მთავარი ღილაკი)."""
    doc_id = x["id"]
    st = item["status"] if item else None
    v = json.loads(item["validation"] or "{}") if item else {}
    why = _main_reason(v)
    manual = _manual_block(c, x, item, f"/doc/{doc_id}") if mhr_on else ""

    def card(tone: str, title: str, text: str, inner: str = "") -> str:
        return (f'<div class="next tone-{tone}"><div class="lbl">შემდეგი ნაბიჯი</div><div class="ttl">{e(title)}</div>'
                + (f"<p>{e(text)}</p>" if text else "") + inner + "</div>")

    emp = v.get("employee") or {}
    emp_name = " ".join(x for x in (emp.get("first_name"), emp.get("last_name")) if x)
    alt = (f'<div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--border)">'
           f'<div class="muted" style="font-size:13px;margin-bottom:6px">ან, თუ დოკუმენტი სწორია — ატვირთე ხელით</div>{manual}</div>') if manual else ""
    if st == "split":
        return card("info", "სკანი ფაილებად დაიყო", "თითოეული ფაილი ცალკე მოწმდება და ცალკე აიტვირთება.",
                    f'<a class="btn sm" href="/mhr/{e(doc_id)}">ფაილების სია</a>')
    if st == "uploaded":
        return card("ok", "ატვირთულია MHR-ზე", f"თანამშრომლის ბარათზეა{' · ' + emp_name if emp_name else ''}.",
                    f'<a class="btn sm" href="/mhr/{e(doc_id)}">დეტალები</a>')
    if st in ("approved", "uploading"):
        return card("info", "იტვირთება MHR-ზე", "ჩარევა არ სჭირდება.")
    if st == "ready":
        return card("info", "მზადაა MHR-ზე ასატვირთად", "",
                    f'<div class="btns"><form class="inline" method="post" action="/mhr/{e(doc_id)}/approve"><button class="btn primary">'
                    f'{icon("cloud", 16)}ატვირთვა MHR-ზე</button></form><a class="btn" href="/mhr/{e(doc_id)}">რა შემოწმდა</a></div>')
    if st == "failed" and _no_compare(v):   # შედარება ვერ მოხდა (ორიგინალი MHR-ში არ არის) — გაფრთხილება, არა ცვლილება
        return card("warn", "MHR-ში ხელშეკრულება ვერ მოიძებნა", why,
                    (manual or f'<div class="btns"><a class="btn" href="/mhr/{e(doc_id)}">დეტალები</a></div>'))
    if st == "failed":
        lines = _reason_lines(v)
        listed = "".join(f"<li>{e(t)}</li>" for t in lines[:6]) + (f"<li>და კიდევ {len(lines) - 6}</li>" if len(lines) > 6 else "")
        return card("warn", "რატომ გაჩერდა", "" if lines else why,
                    (f'<ul style="margin:0 0 12px;padding-left:20px;font-size:14px">{listed}</ul>' if listed else "")
                    + f'<div class="btns"><a class="btn primary" href="/mhr/{e(doc_id)}">{icon("search", 16)}ნახე ადგილი და დეტალები</a></div>{alt}')
    if st in ("upload_failed", "needs_check"):
        return card("bad" if st == "upload_failed" else "warn", MHR_KA[st][0], item["error"] or "",
                    f'<div class="btns"><a class="btn primary" href="/mhr/{e(doc_id)}">რა მოხდა</a></div>')
    if x["status"] == "needs_review":
        return card("warn", "გადახედე და გადაწყვიტე", "",
                    (f'<div class="reasons">{reasons_html}</div>' if reasons_html else "") + (manual or ""))
    if manual:   # დამუშავებულია, MHR-ში ჯერ არ არის (გამოტოვებული, რიგში, უარყოფილი…)
        return card("info", "MHR-ზე ჯერ არ ატვირთულა", (MHR_KA.get(st, ("", ""))[0] + (f" — {why}" if why else "")) if st else "", manual)
    return card("ok", "დამუშავებულია", "გასაკეთებელი არაფერია.")


def _mhr_emp_name(emp) -> str:
    return " ".join(x for x in (emp.first_name, emp.last_name) if x) or emp.employee_id


def _flash(msg: str, err: str) -> str:
    return ((f'<div class="banner tone-ok">{icon("check", 18)}<div>{e(msg)}</div></div>' if msg else "")
            + (f'<div class="banner tone-bad">{icon("x", 18)}<div>{e(err)}</div></div>' if err else ""))


def _manual_lookup_pn(c: Config, pn: str) -> dict:
    """თანამშრომელი MHR-ში მხოლოდ პ/ნ-ით (სახელის შედარების გარეშე). {"employee", "error"}."""
    from ..mhr_client import build_mhr_client
    from ..mhr_models import MhrError, MhrUnavailable
    out: dict = {"employee": None, "error": None}
    if not c.section("mhr")["enabled"]:
        out["error"] = "MHR ინტეგრაცია გამორთულია — ჩართე „პარამეტრებში“."
        return out
    try:
        emp = build_mhr_client(c).get_employee(pn)
    except MhrUnavailable:
        out["error"] = "MHR მიუწვდომელია — შეამოწმე ინტერნეტი/MHR და სცადე თავიდან."
    except MhrError as ex:
        out["error"] = ("MHR-ში ამ პირადი ნომრით რამდენიმე აქტიური ჩანაწერია — ჯერ MHR-ში გაასწორე."
                        if str(ex) == "multiple_matches" else
                        f"MHR-ის შეცდომა ({str(ex)[:80]}) — შეამოწმე პარამეტრები (მისამართი, გასაღები).")
    else:
        if emp is None:
            out["error"] = f"პირადი ნომრით {pn} თანამშრომელი MHR-ში ვერ მოიძებნა — შეამოწმე ნომერი ან ჯერ MHR-ში დაამატე."
        else:
            out["employee"] = emp
    return out


def _manual_block(c: Config, doc, item, back: str) -> str:
    """„ხელით ატვირთვა“ ბარათი დოკუმენტის/MHR-ის გვერდზე: ჩანს, სანამ დოკუმენტი არ აიტვირთა ან ატვირთვა არ მიდის.
    თანამშრომელი — დოკუმენტიდან ამოკითხული პ/ნ-ით; სახელი ვალიდაციის შედეგიდან ჩანს, თუ უკვე ვიცით."""
    from ..mhr_queue import MANUAL_BLOCKED
    if not doc["active"] or not doc["output_path"] or doc["purged_at"] or (item and item["status"] in MANUAL_BLOCKED):
        return ""
    meta = json.loads(doc["metadata"] or "{}")
    f = meta.get("fields") or {}
    pn = doc["personal_number"] or (f.get("personal_number") or {}).get("value") or ""
    first, last = (f.get("first_name") or {}).get("value") or "", (f.get("last_name") or {}).get("value") or ""
    v = json.loads(item["validation"] or "{}") if item else {}
    known = _mhr_name(v)
    t = c.doc_type(doc["type_id"] or "")
    unknown = not t or t.id == "unknown"
    who = (f"თანამშრომელი MHR-ში: <b>{e(known)}</b>" if known else
           f"თანამშრომელი MHR-ში — პირადი ნომრით <b>{e(pn)}</b>" if pn else
           "პირადი ნომერი არ იკითხება — ჩაწერე ხელით:")
    sel = ""
    if unknown:
        sel = ('<select name="type_id" required><option value="">ტიპი — აირჩიე</option>'
               + "".join(f'<option value="{e(dt.id)}">{e(dt.name_ka)}</option>' for dt in c.active_types) + "</select>")
    btn = (f'<button class="btn primary" {"disabled" if not pn else ""}>{icon("upload", 16)}ხელით ატვირთვა MHR-ზე</button>')
    link = "/manual?" + urlencode({"pn": pn, "first": first, "last": last})
    other = f'<a href="{e(link)}" style="font-size:13px;white-space:nowrap">{"ჩაწერე ხელით →" if not pn else "სხვა თანამშრომელი →"}</a>'
    return (f'<form method="post" action="/doc/{e(doc["id"])}/manual-upload" class="manual">'
            f'<input type="hidden" name="back" value="{e(back)}"><div style="font-size:14px;margin-bottom:8px">{who}</div>'
            f'<div class="btns" style="align-items:center">{sel}{btn}{other}</div></form>')


def _manual_lookup(c: Config, q: dict[str, str]) -> dict:
    """თანამშრომელი MHR-ში პ/ნ-ით + სახელ-გვარის შედარება. {"pn", "employee", "name_ok", "error"} — error ქართულად."""
    from ..mhr_client import build_mhr_client
    from ..mhr_models import MhrError, MhrUnavailable
    from ..textnorm import digits_only
    from ..validation import employee_name_matches
    pn = digits_only(q["pn"])
    out: dict = {"pn": pn, "employee": None, "name_ok": False, "error": None}
    if not c.section("mhr")["enabled"]:
        out["error"] = "MHR ინტეგრაცია გამორთულია — ჩართე „პარამეტრებში“."
    elif len(pn) != 11:
        out["error"] = "პირადი ნომერი 11 ციფრისგან უნდა შედგებოდეს."
    elif not q["first"] or not q["last"]:
        out["error"] = "სახელიც და გვარიც სავალდებულოა — ისე, როგორც MHR-შია ჩაწერილი."
    else:
        try:
            emp = build_mhr_client(c).get_employee(pn)
        except MhrUnavailable:
            out["error"] = "MHR მიუწვდომელია — შეამოწმე ინტერნეტი/MHR და სცადე თავიდან."
        except MhrError as ex:
            out["error"] = ("MHR-ში ამ პირადი ნომრით რამდენიმე აქტიური ჩანაწერია — ჯერ MHR-ში გაასწორე."
                            if str(ex) == "multiple_matches" else
                            f"MHR-ის შეცდომა ({str(ex)[:80]}) — შეამოწმე პარამეტრები (მისამართი, გასაღები).")
        else:
            if emp is None:
                out["error"] = "ამ პირადი ნომრით თანამშრომელი MHR-ში ვერ მოიძებნა — შეამოწმე ნომერი ან ჯერ MHR-ში დაამატე."
            else:
                out["employee"] = emp
                out["name_ok"] = employee_name_matches(q["first"], q["last"], emp,
                                                       float(c.section("mhr").get("name_similarity", 0.85)))
                if not out["name_ok"]:
                    out["error"] = (f"სახელი/გვარი არ ემთხვევა MHR-ის ჩანაწერს (MHR-ში: {_mhr_emp_name(emp)}). "
                                    "დარწმუნდი, რომ სწორი თანამშრომელია, და ჩაწერე ისე, როგორც MHR-შია.")
    return out


def _doc_names(x) -> tuple[str | None, str | None]:
    f = json.loads(x["metadata"] or "{}").get("fields", {})
    return (f.get("first_name") or {}).get("value"), (f.get("last_name") or {}).get("value")


def _manual_doc_row(c: Config, x, q: dict[str, str], done: bool) -> str:
    tname, name = _doc_title(c, x)
    t = c.doc_type(x["type_id"] or "")
    meta = " · ".join(v for v in (name, x["personal_number"] or "პ/ნ არ იკითხება",
                                  x["document_date"] or (str(x["year"]) if x["year"] else None),
                                  f'{Path(x["first_path"]).name} · გვ. {x["page_start"] + 1}–{x["page_end"] + 1}') if v)
    if done:
        btns = f'{mhr_chip(x["mhr_status"])}<a class="btn sm" href="/mhr/{e(x["id"])}">დეტალები</a>'
    else:
        hidden = "".join(f'<input type="hidden" name="{k}" value="{e(v)}">'
                         for k, v in (("pn", q["pn"]), ("first", q["first"]), ("last", q["last"]), ("document_ids", x["id"])))
        sel = ""
        if not t or t.id == "unknown":
            sel = ('<select name="type_id" required style="min-height:32px;padding:4px 8px"><option value="">ტიპი — აირჩიე</option>'
                   + "".join(f'<option value="{e(dt.id)}">{e(dt.name_ka)}</option>' for dt in c.active_types) + "</select>")
        btns = ((mhr_chip(x["mhr_status"]) if x["mhr_status"] else "")
                + f'<form method="post" action="/manual/upload" style="display:flex;gap:8px;align-items:center">{hidden}{sel}'
                f'<button class="btn sm primary">{icon("upload", 14)}ატვირთვა</button></form>')
    return (f'<div class="row"><div class="grow"><a class="t" href="/doc/{e(x["id"])}" style="display:block;color:var(--text)">'
            f'{e(tname)}</a><div class="m">{e(meta)}</div></div>{status_chip(x["status"])}'
            f'<div class="btns" style="align-items:center">{btns}<a class="btn sm" href="/file/output/{e(x["id"])}" target="_blank">PDF</a></div></div>')


def _manual_body(c: Config, q: dict[str, str], res: dict | None, rows: list, msg: str, err: str) -> str:
    from ..mhr_queue import MANUAL_BLOCKED
    from ..textnorm import digits_only
    from ..validation import employee_name_matches
    out = ['<h1>ხელით ატვირთვა</h1><p class="sub">როცა ავტომატური შედარება ვერ გადის ან დოკუმენტი „გადასამოწმებელში“ დარჩა: '
           'ჩაწერე თანამშრომლის პირადი ნომერი, სახელი და გვარი ზუსტად ისე, როგორც MHR-შია. პროგრამა MHR-ში იპოვის თანამშრომელს, '
           'შეადარებს სახელ-გვარს და აჩვენებს მის ჯერ აუტვირთავ დოკუმენტებს — ატვირთვა ერთი ღილაკით, შედარების გარეშე.</p>']
    if msg:
        out.append(f'<div class="banner tone-ok">{icon("check", 18)}<div>{e(msg)}</div></div>')
    if err:
        out.append(f'<div class="banner tone-bad">{icon("x", 18)}<div>{e(err)}</div></div>')
    out.append('<form method="get" action="/manual" class="card section"><div class="co-row" style="grid-template-columns:1fr 1fr 1fr">'
               + _field("პირადი ნომერი", f'<input type="text" name="pn" value="{e(q["pn"])}" inputmode="numeric" maxlength="11" '
                        f'placeholder="11 ციფრი" autofocus required>')
               + _field("სახელი", f'<input type="text" name="first" value="{e(q["first"])}" required>')
               + _field("გვარი", f'<input type="text" name="last" value="{e(q["last"])}" required>')
               + f'</div><button class="btn primary">{icon("search", 16)}მოძებნა MHR-ში</button></form>')
    if res is None:
        return "".join(out)
    if res["error"]:
        tone = "warn" if res["employee"] else "bad"
        out.append(f'<div class="banner tone-{tone}">{icon("alert" if tone == "warn" else "x", 18)}<div>{e(res["error"])}</div></div>')
        return "".join(out)
    emp = res["employee"]
    kv = [("თანამშრომელი", _mhr_emp_name(emp)), ("პირადი ნომერი", emp.personal_number or res["pn"]), ("MHR ID", emp.employee_id),
          ("თანამდებობა", emp.position or "—"), ("სტატუსი", emp.status or "—")]
    out.append(f'<div class="banner tone-ok">{icon("check", 18)}<div><b>თანამშრომელი მოიძებნა MHR-ში, სახელი და გვარი ემთხვევა.</b></div></div>'
               f'<div class="card section"><dl class="kv">{"".join(f"<dt>{e(k)}</dt><dd>{e(str(v))}</dd>" for k, v in kv)}</dl></div>')
    thr = float(c.section("mhr").get("name_similarity", 0.85))
    exact = [x for x in rows if digits_only(x["personal_number"] or "") == res["pn"]]
    by_name = [x for x in rows if digits_only(x["personal_number"] or "") != res["pn"]
               and employee_name_matches(*_doc_names(x), emp, thr)]
    todo = [x for x in exact if x["mhr_status"] not in MANUAL_BLOCKED]
    done = [x for x in exact if x["mhr_status"] in MANUAL_BLOCKED]
    out.append(f'<h2>აუტვირთავი დოკუმენტები ({len(todo)})</h2>')
    bulk_ids = [x["id"] for x in todo if (x["type_id"] or "unknown") != "unknown"]
    if len(bulk_ids) > 1:
        hidden = "".join(f'<input type="hidden" name="{k}" value="{e(v)}">'
                         for k, v in (("pn", q["pn"]), ("first", q["first"]), ("last", q["last"]), ("document_ids", ",".join(bulk_ids))))
        out.append(f'<form method="post" action="/manual/upload" class="toolbar">{hidden}<button class="btn primary">'
                   f'{icon("upload", 16)}ყველას ატვირთვა ({len(bulk_ids)})</button>'
                   f'<span class="muted" style="font-size:13px">უცნობი ტიპის დოკუმენტი ცალ-ცალკე იტვირთება — ჯერ ტიპი აირჩიე.</span></form>')
    out.append('<div class="card list">' + ("".join(_manual_doc_row(c, x, q, False) for x in todo)
               or empty("check", "ok", "ამ პირადი ნომრით აუტვირთავი დოკუმენტი არ არის",
                        "დასკანერებული დოკუმენტი აქ ჩნდება, როცა პროგრამა მას დაამუშავებს (იხ. „დოკუმენტები“).")) + '</div>')
    if by_name:
        out.append(f'<h2 style="margin-top:24px">სავარაუდო დოკუმენტები სახელით ({len(by_name)})</h2>'
                   f'<div class="banner tone-warn">{icon("alert", 18)}<div>ამ დოკუმენტებში პირადი ნომერი ვერ წაიკითხა ან სხვაა, მაგრამ სახელი და '
                   'გვარი ემთხვევა. გახსენი PDF და დარწმუნდი, რომ ამ თანამშრომლისაა — ატვირთვა მის ბარათზე მოხდება.</div></div>'
                   '<div class="card list">' + "".join(_manual_doc_row(c, x, q, x["mhr_status"] in MANUAL_BLOCKED) for x in by_name) + '</div>')
    if done:
        out.append(f'<details style="margin-top:20px"><summary>უკვე ატვირთული ან პროცესში ({len(done)})</summary>'
                   f'<div class="card list" style="margin-top:10px">{"".join(_manual_doc_row(c, x, q, True) for x in done)}</div></details>')
    return "".join(out)


_ROLE_COLOR = {"document": "var(--ok)", "uncertain": "var(--warn)", "blank": "var(--border)"}


def _bars(title: str, items: list[tuple[str, int, str]]) -> str:
    top = max((n for _, n, _ in items), default=0) or 1
    rows = "".join(f'<div class="bl"><a href="{e(link)}">{e(label)}</a><b style="text-align:right">{n}</b>'
                   f'<div class="track"><span style="width:{100 * n / top:.0f}%"></span></div></div>' for label, n, link in items)
    return f'<div class="card section bars"><h2 style="font-size:15px;margin-bottom:8px">{e(title)}</h2>{rows or "<p class=muted>—</p>"}</div>'


def _stats_body(c: Config, r: Repository) -> str:
    docs = r.q("SELECT * FROM documents WHERE active=1")
    by: dict[str, dict[str, int]] = {"status": {}, "type": {}, "company": {}, "year": {}, "reason": {}}
    for d in docs:
        for k, v in (("status", d["status"]), ("type", d["type_id"] or "unknown"), ("company", d["company_id"] or "—"),
                     ("year", str(d["year"]) if d["year"] else "none")):
            by[k][v] = by[k].get(v, 0) + 1
        for rr in json.loads(d["review_reasons"] or "[]"):
            by["reason"][rr] = by["reason"].get(rr, 0) + 1

    files = r.q("SELECT s.id sid, s.first_path, s.page_count, s.created_at, j.* FROM sources s JOIN jobs j ON j.source_id=s.id"
                " WHERE j.created_at=(SELECT MAX(created_at) FROM jobs WHERE source_id=s.id) ORDER BY s.created_at DESC")
    counts: dict[str, dict[str, int]] = {}
    for x in r.q("SELECT job_id, status, COUNT(*) n FROM documents WHERE active=1 GROUP BY job_id, status"):
        counts.setdefault(x["job_id"], {})[x["status"]] = x["n"]
    roles: dict[str, dict[str, int]] = {}
    for x in r.q("SELECT job_id, role, COUNT(*) n FROM page_assignments GROUP BY job_id, role"):
        roles.setdefault(x["job_id"], {})[x["role"]] = x["n"]
    last = {x["job_id"]: x for x in r.q(
        "SELECT a.* FROM job_attempts a WHERE a.attempt=(SELECT MAX(attempt) FROM job_attempts WHERE job_id=a.job_id)")}
    inbox_err = r.q("SELECT path, error_code, error_detail, updated_at FROM inbox_files WHERE state='error' ORDER BY updated_at DESC")
    waiting = r.one("SELECT COUNT(*) n FROM inbox_files WHERE state='pending'")["n"]

    secs: list[tuple[float, int]] = []
    for f in files:
        la = last.get(f["id"])
        s = _seconds(la["started_at"], la["finished_at"]) if la and f["status"] == "done" else None
        if s:
            secs.append((s, f["page_count"] or 0))
    per_page = (sum(s for s, _ in secs) / max(1, sum(p for _, p in secs))) if secs else None
    total_pages = sum(f["page_count"] or 0 for f in files)
    failed = sum(1 for f in files if f["status"] == "failed") + len(inbox_err)

    out = ['<h1>ფაილები</h1><p class="sub">ყოველი დასკანერებული ფაილი: რა მოვიდა, რამდენ დოკუმენტად დაიყო და სად წავიდა.</p>',
           '<div class="tiles" style="margin-bottom:8px">' + "".join(
               f'<div class="tile"><span class="ib tone-{t}">{icon(i, 18)}</span><div class="n">{e(n)}</div><div class="l">{e(l)}</div></div>'
               for i, t, n, l in [("scan", "info", len(files) + len(inbox_err), "შემოსული ფაილი"), ("file", "mute", total_pages, "გვერდი"),
                                  ("layers", "ok", len(docs), "ნაპოვნი დოკუმენტი"), ("alert", "bad", failed, "შეცდომიანი ფაილი"),
                                  ("clock", "mute", f"{per_page:.1f} წმ" if per_page else "—", "საშუალოდ გვერდზე")]) + "</div>"]

    rows = []
    for f in files:
        rl = roles.get(f["id"], {})
        n = f["page_count"] or 0
        bar = "".join(f'<span title="{e(k)}: {v}" style="width:{100 * v / max(1, n):.1f}%;background:{_ROLE_COLOR[k]}"></span>'
                      for k, v in rl.items() if k in _ROLE_COLOR)
        la = last.get(f["id"])
        dur = _seconds(la["started_at"], la["finished_at"]) if la else None
        mode = "ერთობლივი" if f["mode"] == "batch_pdf" else "ცალკე"
        err = f' · <span style="color:var(--bad)">{e(f["error_code"])}</span>' if f["error_code"] and f["status"] == "failed" else ""
        rows.append(
            f'<a class="row" href="/documents?source={e(f["sid"])}" style="color:inherit;text-decoration:none"><div class="grow">'
            f'<div class="t">{e(Path(f["first_path"]).name)}</div><div class="m">{mode} · {n} გვერდი · {_summary(counts.get(f["id"], {}))}'
            f'{" · " + f"{dur:.0f} წმ" if dur else ""}{err}</div></div><div class="stack" aria-hidden="true">{bar}</div>{_outcome_chip(f["status"], counts.get(f["id"], {}))}</a>')
    out.append('<h2>შემოსული ფაილები</h2><p class="muted" style="margin-top:-6px;font-size:13px">ზოლი აჩვენებს გვერდებს: '
               '<span style="color:var(--ok)">■</span> დოკუმენტი · <span style="color:var(--warn)">■</span> გაურკვეველი · ■ ცარიელი</p>'
               f'<div class="card list">{"".join(rows) or empty("scan", "info", "ჯერ არაფერი შემოსულა")}</div>')

    type_name = {t.id: t.name_ka for t in c.types}
    comp_name = {x.id: x.name for x in c.companies}
    srt = lambda dct: sorted(dct.items(), key=lambda kv: -kv[1])  # noqa: E731
    out.append('<h2>შეჯამება</h2><div class="grid-cards">'
               + _bars("შედეგი", [(STATUS_KA.get(k, k), v, f"/documents?status={k}") for k, v in srt(by["status"])])
               + _bars("ტიპი", [(type_name.get(k, k), v, f"/documents?type={k}") for k, v in srt(by["type"])])
               + _bars("კომპანია", [(comp_name.get(k, k), v, f"/documents?company={k}") for k, v in srt(by["company"])])
               + _bars("წელი", [("წელი უცნობია" if k == "none" else k, v, f"/documents?year={k}") for k, v in sorted(by["year"].items(), reverse=True)])
               + _bars("რატომ გადავიდა გადასამოწმებლად", [(REASONS.get(k, (k,))[0], v, f"/documents?reason={k}") for k, v in srt(by["reason"])])
               + "</div>")

    stuck = [f for f in files if f["status"] in ("failed", "queued", "running")]
    srows = "".join(f'<div class="row"><div class="grow"><div class="t">{e(Path(f["first_path"]).name)}</div>'
                    f'<div class="m">{e(f["error_code"] or "")} {e(f["error_detail"] or "")}</div></div>{status_chip(f["status"])}</div>' for f in stuck)
    srows += "".join(f'<div class="row"><div class="grow"><div class="t">{e(Path(x["path"]).name)}</div>'
                     f'<div class="m mono">{e(x["error_code"])} — {e(x["error_detail"])}</div></div>{chip("ვერ მიიღო", "bad")}</div>' for x in inbox_err)
    out.append(f'<h2 id="stuck">სად გაიჭედა</h2><p class="muted" style="margin-top:-6px;font-size:13px">ჩაწერის დასრულებას ელოდება: {waiting} ფაილი · '
               f'<a href="/jobs">ტექნიკური ჟურნალი</a></p>'
               f'<div class="card list">{srows or empty("check", "ok", "არაფერი არ გაჭედილა")}</div>')
    return "".join(out)


# --- პარამეტრები ------------------------------------------------------------------------
_NUM_FIELDS = [
    ("watcher.stability_seconds", "ფაილის სტაბილურობის დრო (წმ)", float),
    ("processing.concurrency", "პარალელური სამუშაოები", int),
    ("processing.dpi", "ანალიზის გარჩევადობა (DPI)", int),
    ("thresholds.type_auto", "ტიპის ზღვარი ავტომატურისთვის", float),
    ("thresholds.type_min_margin", "ტიპის მინ. სხვაობა მეორესთან", float),
    ("thresholds.boundary_auto", "საზღვრის ზღვარი ავტომატურისთვის", float),
    ("thresholds.boundary_start", "ახალი დოკუმენტის დაწყების ზღვარი", float),
    ("thresholds.boundary_continue", "გაგრძელების ზღვარი", float),
]
_BOOL_FIELDS = [("processing.trial_mode", "საცდელი რეჟიმი"), ("ocr.online.enabled", "ონლაინ სერვისის დაშვება"),
                ("employee_api.enabled", "თანამშრომლების API ჩართულია"), ("autostart.enabled", "ავტოგაშვება"),
                ("mhr.enabled", "MHR ინტეგრაცია"), ("mhr.require_approval", "ატვირთვამდე დადასტურება"),
                ("cleanup.move_processed", "დამუშავებულის გადატანა"), ("tray.notifications", "Windows-ის შეტყობინებები"),
                ("mhr.signature_every_page", "ხელმოწერა ყველა გვერდზე"), ("mhr.ai_review.enabled", "AI ანალიზი")]
_CLEANUP_NUM_FIELDS = [
    ("cleanup.processed_days", "დამუშავებული ფაილის წაშლა (დღე)", int,
     "Processed-იდან. ორიგინალის ასლი არქივში რჩება, ამიტომ ეს უსაფრთხოა. 0 = არასდროს. _Failed არასდროს იშლება."),
    ("cleanup.uploaded_days", "ორიგინალისა და შედეგის წაშლა MHR-ში ატვირთვიდან (დღე)", int,
     "არქივის ორიგინალი და Sorted/Proposed-ის PDF — მხოლოდ მას შემდეგ, რაც ფაილის ყველა დოკუმენტი MHR-ში აიტვირთა. 0 = არასდროს."),
]
_TEXT_FIELDS = [("default_company.id", "ნაგულისხმევი კომპანია — ID"), ("default_company.name", "ნაგულისხმევი კომპანია — სახელი"),
                ("default_company.folder", "ნაგულისხმევი კომპანია — საქაღალდე"), ("ocr.tesseract_cmd", "Tesseract-ის ბილიკი"),
                ("employee_api.base_url", "API მისამართი"), ("employee_api.auth.secret_name", "API გასაღების სახელი"),
                ("mhr.base_url", "MHR — მისამართი"), ("mhr.auth.secret_name", "MHR — გასაღების სახელი"),
                ("mhr.endpoints.employee_by_pn", "MHR — თანამშრომელი პ/ნ-ით (შაბლონი, {pn})"),
                ("mhr.endpoints.list_documents", "MHR — თანამშრომლის დოკუმენტები ({employee_id}, {category})"),
                ("mhr.endpoints.download_document", "MHR — დოკუმენტის ჩამოტვირთვა ({doc_id})"),
                ("mhr.endpoints.upload_document", "MHR — ატვირთვა ({employee_id})")]
_SELECT_FIELDS = [("ocr.path", "ამოცნობის გზა", ["local", "online", "hybrid"]),
                  ("employee_api.mode", "API რეჟიმი", ["mock", "http"]),
                  ("employee_api.auth.method", "API ავტორიზაცია", ["none", "bearer", "basic", "api_key_header"]),
                  ("mhr.mode", "MHR რეჟიმი", ["mock", "http"]),
                  ("mhr.auth.method", "MHR ავტორიზაცია", ["none", "bearer", "basic", "api_key_header"])]
_COMPANY_ROWS_EXTRA = 3


def _get(d: dict, dotted: str):
    for k in dotted.split("."):
        d = d[k]
    return d


def _set(d: dict, dotted: str, value) -> None:
    keys = dotted.split(".")
    for k in keys[:-1]:
        d = d[k]
    d[keys[-1]] = value


def _field(label: str, inner: str, help_: str = "") -> str:
    return f'<div class="field"><label>{e(label)}</label>{inner}{f"<div class=help>{e(help_)}</div>" if help_ else ""}</div>'


def _settings_body(c: Config, msg: str, err: str) -> str:
    d = c.data
    out = ['<h1>პარამეტრები</h1><p class="sub">ცვლილება მოქმედებს მხოლოდ ახალ ფაილებზე — უკვე დამუშავებული არ იცვლება.</p>']
    if msg:
        out.append(f'<div class="banner tone-ok">{icon("check", 18)}<div>{e(msg)}</div></div>')
    if err:
        out.append(f'<div class="banner tone-bad">{icon("x", 18)}<div>შეცდომა: {e(err)}</div></div>')
    out.append('<form method="post" action="/settings">')

    # საქაღალდეები და რეჟიმები
    prof = []
    for i, p in enumerate(d["profiles"]):
        radios = "".join(
            f'<label><input type="radio" name="profile.{i}.mode" value="{m}" {"checked" if p["mode"] == m else ""}>'
            f'<span><b>{t}</b><span class="muted" style="font-size:13px">{h}</span></span></label>'
            for m, t, h in [("auto", "პროგრამა თავად წყვეტს", "1 გვერდი = ერთი დოკუმენტი, მეტი = ნაკრები (დაიყოფა)"),
                            ("batch_pdf", "ერთ ფაილში ბევრი დოკუმენტი", "პროგრამა თავად დაყოფს"),
                            ("single_document_pdf", "ერთი ფაილი = ერთი დოკუმენტი", "არ დაიყოფა")])
        prof.append(_field(f"სკანერის საქაღალდე „{p['id']}“", f'<input type="text" name="profile.{i}.inbox" value="{e(p["inbox"])}">'
                           f'<div class="radio-cards" style="margin-top:8px">{radios}</div>'))
    out.append('<div class="card section"><h2>სკანერი და საქაღალდეები</h2><p class="hint">სად ინახავს სკანერი PDF-ს და როგორ დამუშავდეს.</p>'
               + _field("მთავარი საქაღალდე", f'<input type="text" name="paths.root" value="{e(d["paths"]["root"])}">',
                        "აქ არის Inbox, Sorted, NeedsReview და არქივი. მხოლოდ ლათინური ასოები. შეცვლისას დოკუმენტების სია "
                        "თავიდან იწყება (ძველი ფაილები ადგილზე რჩება). ქსელური დისკის (მაგ. Z:) შემთხვევაში პროგრამის ჟურნალი "
                        f"ამ კომპიუტერზე ინახება. ახლა ჟურნალი: {c.paths.state}")
               + "".join(prof) + "</div>")

    # კომპანიები
    rows = "".join(
        f'<div class="co-row"><input type="hidden" name="company.{i}.id" value="{e(co.get("id", ""))}">'
        f'<input type="hidden" name="company.{i}.folder" value="{e(co.get("folder", ""))}">'
        f'<input type="text" name="company.{i}.name" value="{e(co.get("name", ""))}" placeholder="დასახელება, მაგ. შპს „მაგალითი“" aria-label="კომპანიის დასახელება">'
        f'<input type="text" name="company.{i}.aliases" value="{e(", ".join(co.get("aliases", [])))}" placeholder="სხვა ფორმები, მძიმით" aria-label="სხვა ფორმები"></div>'
        for i, co in enumerate(d["companies"] + [{}] * _COMPANY_ROWS_EXTRA))
    choices = "".join(f'<option value="{e(co["id"])}" {"selected" if co["id"] == d["default_company"]["id"] else ""}>{e(co["name"])}</option>'
                      for co in d["companies"])
    out.append('<div class="card section"><h2>კომპანიები</h2><p class="hint">რომელი კომპანიების დოკუმენტებს ელოდები. '
               'სხვა ფორმებში ჩაწერე, როგორ შეიძლება ეწეროს დოკუმენტში (მაგ. „არბო 2009, არბო“). წასაშლელად წაშალე დასახელება.</p>'
               f'{rows}' + _field("თუ კომპანია დოკუმენტში ვერ მოიძებნა", f'<select name="default_company_choice"><option value="">'
                                  f'— {e(d["default_company"]["name"])} (ამჟამინდელი) —</option>{choices}</select>') + "</div>")

    # ავტომატიზაცია
    trial = d["processing"]["trial_mode"]
    out.append('<div class="card section"><h2>ავტომატიზაცია</h2><p class="hint">როგორ შეინახოს დარწმუნებით ამოცნობილი დოკუმენტები.</p>'
               f'<label class="switch"><input type="checkbox" name="processing.trial_mode" value="1" {"checked" if trial else ""}>'
               '<span><b>საცდელი რეჟიმი</b><br><span class="muted" style="font-size:13.5px">შედეგები მხოლოდ შემოთავაზებულია (Proposed). '
               'გამორთე, როცა შედეგებით კმაყოფილი იქნები — მაშინ დალაგდება Sorted საქაღალდეში იმ ტიპებისთვის, რომლებზეც ავტომატური შენახვა ჩართულია.</span></span></label></div>')

    # დამუშავებული ფაილები და ადგილი
    cl = d["cleanup"]
    out.append('<div class="card section"><h2>დამუშავებული ფაილები და ადგილი</h2><p class="hint">დამუშავების შემდეგ სკანი ინბოქსიდან '
               'გადადის, რომ ინბოქსში მხოლოდ დაუმუშავებელი დარჩეს. შეცდომიანი — ცალკე, _Failed საქაღალდეში.</p>'
               f'<label class="switch" style="margin-bottom:12px"><input type="checkbox" name="cleanup.move_processed" value="1" {"checked" if cl["move_processed"] else ""}>'
               '<span><b>დამუშავებული ფაილის გადატანა ინბოქსიდან</b></span></label>'
               + _field("დამუშავებულების საქაღალდე", f'<input type="text" name="paths.processed" value="{e(d["paths"]["processed"])}">',
                        "მთავარ საქაღალდესთან მიმართებით (მაგ. Processed) ან სრული ბილიკი.")
               + "".join(_field(label, f'<input type="text" inputmode="numeric" name="{key}" value="{e(_get(d, key))}">', hint)
                         for key, label, _, hint in _CLEANUP_NUM_FIELDS)
               + f'<label class="switch"><input type="checkbox" name="tray.notifications" value="1" {"checked" if d["tray"]["notifications"] else ""}>'
               '<span><b>Windows-ის შეტყობინებები</b><br><span class="muted" style="font-size:13.5px">ეკრანის კუთხის ხატულა აჩვენებს: ახალი '
               'გადასამოწმებელი, შეცდომა, დამმუშავებელი გაჩერდა.</span></span></label></div>')

    # ამოცნობა და კონფიდენციალურობა
    online = d["ocr"]["online"]["enabled"]
    by_subscription = d["ocr"]["online"]["provider"] == "claude_code"
    how = ('ამოცნობას შენი Claude-გამოწერით შესული Claude Code აკეთებს — გასაღები არ სჭირდება.' if by_subscription
           else 'გარე სერვისს API-გასაღები სჭირდება (გასაღებები ქვემოთ).')
    out.append('<div class="card section"><h2>ამოცნობა და კონფიდენციალურობა</h2><p class="hint">ნაგულისხმევად დოკუმენტები კომპიუტერს არ ტოვებს.</p>'
               f'<label class="switch"><input type="checkbox" name="ocr.online.enabled" value="1" {"checked" if online else ""}>'
               '<span><b>ონლაინ სერვისის დაშვება</b><br><span class="muted" style="font-size:13.5px">თუ ჩართავ და ამოცნობის გზას „hybrid/online“-ზე '
               f'დააყენებ (დამატებითში), დოკუმენტის გვერდები გარე სერვისში გაიგზავნება. {how}</span></span></label>'
               + (_claude_login_block() if by_subscription else "")
               + (f'<label class="switch" style="margin-top:12px"><input type="checkbox" name="mhr.ai_review.enabled" value="1" '
                  f'{"checked" if d["mhr"]["ai_review"]["enabled"] else ""}><span><b>AI ანალიზი MHR-შედარების შემდეგ (Opus)</b><br>'
                  '<span class="muted" style="font-size:13.5px">ყოველი შემოწმებული დოკუმენტის ამოკითხულ ტექსტს და შედარების შედეგს '
                  'Claude Opus გადახედავს, ყველაფერს მარტივად ახსნის და იტყვის, შეიძლება თუ არა ატვირთვა. თავად არ ტვირთავს — '
                  'გადაწყვეტილება შენია. მუშაობს მხოლოდ ონლაინ სერვისის დაშვებისას.</span></span></label>' if by_subscription else "")
               + '</div>')

    # MHR
    mh = d["mhr"]
    out.append('<div class="card section"><h2>MHR ინტეგრაცია</h2><p class="hint">დოკუმენტის შედარება MHR-ის მონაცემებთან და ორიგინალ '
               'ხელშეკრულებასთან, შემდეგ ატვირთვა თანამშრომლის ბარათზე. მისამართები და ავტორიზაცია — „დამატებითში“.</p>'
               f'<label class="switch" style="margin-bottom:12px"><input type="checkbox" name="mhr.enabled" value="1" {"checked" if mh["enabled"] else ""}>'
               '<span><b>MHR ინტეგრაცია ჩართულია</b></span></label>'
               f'<label class="switch"><input type="checkbox" name="mhr.require_approval" value="1" {"checked" if mh["require_approval"] else ""}>'
               '<span><b>ატვირთვამდე ჩემი დადასტურება</b><br><span class="muted" style="font-size:13.5px">გამორთვის შემთხვევაში ვალიდაციაგავლილი '
               'დოკუმენტი ავტომატურად აიტვირთება.</span></span></label>'
               f'<label class="switch" style="margin-top:12px"><input type="checkbox" name="mhr.signature_every_page" value="1" {"checked" if mh["signature_every_page"] else ""}>'
               '<span><b>ხელშეკრულებაზე ხელმოწერა ყველა გვერდზე</b><br><span class="muted" style="font-size:13.5px">თუ რომელიმე გვერდზე '
               'ხელმოწერა ვერ მოიძებნა, ატვირთვას არ შემოგთავაზებს (ცარიელი გვერდი გამონაკლისია).</span></span></label></div>')

    # დამატებითი (ექსპერტისთვის)
    adv = []
    for key, label in _TEXT_FIELDS:
        adv.append(_field(label, f'<input type="text" name="{key}" value="{e(_get(d, key))}">'))
    for key, label, opts in _SELECT_FIELDS:
        cur = _get(d, key)
        adv.append(_field(label, f'<select name="{key}">' + "".join(f'<option {"selected" if o == cur else ""}>{o}</option>' for o in opts) + "</select>"))
    for key, label, _ in _NUM_FIELDS:
        adv.append(_field(label, f'<input type="text" inputmode="decimal" name="{key}" value="{e(_get(d, key))}">'))
    for key, label in _BOOL_FIELDS[2:4]:
        adv.append(f'<label class="switch" style="margin-bottom:12px"><input type="checkbox" name="{key}" value="1" {"checked" if _get(d, key) else ""}>'
                   f'<span>{e(label)}</span></label>')
    adv.append(_field("დოკუმენტების ტიპები (JSON)", f'<textarea name="types_json" style="min-height:320px">'
                      f'{e(json.dumps(c.types_data, ensure_ascii=False, indent=2))}</textarea>',
                      "ახალი ტიპის დამატება კოდის გარეშე. auto_save_enabled ჩართე მხოლოდ გამოცდის შემდეგ."))
    out.append(f'<div class="card section"><details><summary>დამატებითი პარამეტრები (ექსპერტისთვის)</summary><div style="margin-top:16px">{"".join(adv)}</div></details></div>')
    out.append(f'<div style="position:sticky;bottom:12px;display:flex;justify-content:flex-end;margin:8px 0 24px">'
               f'<button class="btn primary" style="box-shadow:0 4px 14px rgba(37,99,235,.25)">{icon("check", 16)}შენახვა</button></div></form>')

    # გასაღებები
    try:
        from .. import secrets
        names = {d["employee_api"]["auth"]["secret_name"], d["mhr"]["auth"]["secret_name"]}
        if d["ocr"]["online"]["provider"] != "claude_code":   # გამოწერის რეჟიმში OCR-გასაღები არ არსებობს
            names.add(d["ocr"]["online"]["secret_name"])
        names = sorted(names)
        state = "".join(f'<div class="row"><div class="grow mono">{e(n)}</div>'
                        f'{chip("შენახულია", "ok") if secrets.get_secret(n) else chip("არ არის", "mute")}</div>' for n in names)
    except Exception:
        state = '<div class="row muted">Windows-ის დაცული საცავი მიუწვდომელია</div>'
    out.append('<div class="card section"><h2>სერვისების გასაღებები</h2><p class="hint">ინახება Windows-ის დაცულ საცავში და არა ფაილში.</p>'
               f'<div class="card list" style="margin-bottom:14px">{state}</div><form method="post" action="/settings/secret" class="toolbar">'
               '<input type="text" name="name" placeholder="სახელი, მაგ. hrsorter/employee_api" aria-label="გასაღების სახელი" style="flex:1;min-width:200px">'
               '<input type="password" name="value" placeholder="მნიშვნელობა" aria-label="გასაღების მნიშვნელობა" style="flex:1;min-width:200px">'
               '<button class="btn">შენახვა</button></form></div>')
    return "".join(out)


def _claude_login_block() -> str:
    """Claude Code-ის ავტორიზაციის სტატუსი + „შესვლა“ ღილაკი (გამოწერის რეჟიმში)."""
    from ..ocr import claude_code_vision as cc
    st = cc.auth_status()
    if not st["installed"]:
        state = chip("Claude Code არ არის დაყენებული", "bad")
        hint = 'დააყენე Claude Code (claude.com/code), მერე აქ „შესვლა“ დააჭირე.'
    elif st["logged_in"]:
        who = st["email"] + (f' ({st["subscription"]})' if st["subscription"] else "")
        state = chip(f"შესულია: {who}", "ok")
        hint = "ამოცნობა ამ ანგარიშის გამოწერით მუშაობს. სხვა ანგარიშზე გადასასვლელად თავიდან შედი."
    else:
        state = chip("არ არის შესული", "warn")
        hint = "დააჭირე „შესვლა“ — გაიხსნება Claude-ის ფანჯარა და ბრაუზერში ავტორიზაციას გაივლი."
    return (f'<div class="row" style="margin-top:12px"><div class="grow"><b>Claude-ის ანგარიში</b><br>'
            f'<span class="muted" style="font-size:13.5px">{e(hint)}</span></div>{state}'
            '<button class="btn" formaction="/settings/claude-login" style="margin-left:10px">შესვლა</button></div>')


def _apply_settings_form(c: Config, f: dict[str, str]) -> tuple[dict, dict]:
    from ..naming import sanitize_component
    data = copy.deepcopy(c.data)
    data["paths"]["root"] = f.get("paths.root", data["paths"]["root"]).strip()
    data["paths"]["processed"] = f.get("paths.processed", data["paths"]["processed"]).strip() or "Processed"
    for i, p in enumerate(data["profiles"]):
        p["inbox"] = f.get(f"profile.{i}.inbox", p["inbox"]).strip()
        p["mode"] = f.get(f"profile.{i}.mode", p["mode"])
    for key, _ in _TEXT_FIELDS:
        if key in f:
            _set(data, key, f[key].strip())
    for key, _, opts in _SELECT_FIELDS:
        if f.get(key) in opts:
            _set(data, key, f[key])
    for key, label, conv, *_ in _NUM_FIELDS + _CLEANUP_NUM_FIELDS:
        if key in f:
            try:
                _set(data, key, conv(f[key]))
            except ValueError:
                raise ValueError(f"არასწორი რიცხვი: {label}")
    for key, _ in _BOOL_FIELDS:
        _set(data, key, f.get(key) == "1")
    try:
        if "companies_json" in f:   # ძველი/ავტომატური ფორმატი (ტესტები, სკრიპტები)
            data["companies"] = json.loads(f.get("companies_json") or "[]")
        elif any(k.startswith("company.") for k in f):
            data["companies"] = _companies_from_rows(f, sanitize_component)
        types = json.loads(f.get("types_json") or json.dumps(c.types_data))
    except json.JSONDecodeError as ex:
        raise ValueError(f"JSON შეცდომა: {ex}")
    if not isinstance(data["companies"], list) or not isinstance(types.get("types"), list):
        raise ValueError("companies უნდა იყოს სია, types — ობიექტი 'types' სიით")
    choice = f.get("default_company_choice", "")
    if choice:
        co = next((x for x in data["companies"] if x["id"] == choice), None)
        if co:
            data["default_company"] = {"id": co["id"], "name": co["name"], "folder": co.get("folder") or co["id"]}
    return data, types


def _companies_from_rows(f: dict[str, str], sanitize) -> list[dict]:
    out, used = [], set()
    i = 0
    while f"company.{i}.name" in f:
        name = f[f"company.{i}.name"].strip()
        if name:
            cid = f.get(f"company.{i}.id", "").strip()
            if not cid or cid in used:
                n = len(out) + 1
                while f"c{n}" in used:
                    n += 1
                cid = f"c{n}"
            used.add(cid)
            folder = f.get(f"company.{i}.folder", "").strip() or sanitize(name.replace("„", "").replace("“", "").replace('"', ""), 40, cid)
            aliases = [a.strip() for a in f.get(f"company.{i}.aliases", "").split(",") if a.strip()]
            out.append({"id": cid, "name": name, "folder": folder, "aliases": aliases})
        i += 1
    return out
