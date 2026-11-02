"""მსუბუქი ადგილობრივი ვებინტერფეისი (მხოლოდ 127.0.0.1, ინტერნეტის გარეშე). მძიმე დამუშავება აქ არ ხდება —
UI კითხულობს SQLite-ს და ცვლის მხოლოდ control-ს, ახალ (reprocess) სამუშაოს და კონფიგურაციის ფაილებს.

დიზაინი: მარტივი ოპერატორისთვის — გვერდითი მენიუ (მობილურზე ქვედა პანელი), სისტემური შრიფტი (Segoe UI,
ქართულისთვის Sylfaen), ღია/მუქი თემა, SVG იკონები, ხილული ფოკუსი, ტექნიკური დეტალები დამალულია.
"""
from __future__ import annotations

import copy
import html
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from ..config import Config, ConfigError, load_config
from ..db import Repository

STATUS_KA = {"auto_saved": "დალაგდა", "proposed": "შემოთავაზებული", "needs_review": "გადასამოწმებელი",
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
h1{font-size:26px;line-height:1.25;margin:0 0 6px;font-weight:650;letter-spacing:-.01em}h2{font-size:18px;margin:32px 0 12px;font-weight:620}
.sub{color:var(--text-2);margin:0 0 24px}
.hero{display:flex;gap:16px;align-items:center;padding:22px 24px;border-radius:16px;background:var(--surface);border:1px solid var(--border);box-shadow:var(--shadow);margin-bottom:20px}
.hero .big{display:grid;place-items:center;width:52px;height:52px;border-radius:14px;flex:none}
.hero h1{margin:0}.hero p{margin:2px 0 0;color:var(--text-2)}
.tone-ok{color:var(--ok);background:var(--ok-soft)}.tone-warn{color:var(--warn);background:var(--warn-soft)}
.tone-bad{color:var(--bad);background:var(--bad-soft)}.tone-info{color:var(--accent);background:var(--accent-soft)}.tone-mute{color:var(--mute);background:var(--mute-soft)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px}
.tile{display:block;padding:18px;border-radius:14px;background:var(--surface);border:1px solid var(--border);box-shadow:var(--shadow);color:var(--text);
transition:border-color .15s,transform .15s}a.tile:hover{border-color:var(--accent);text-decoration:none;transform:translateY(-1px)}
.tile .n{font-size:30px;font-weight:650;line-height:1.1;font-variant-numeric:tabular-nums;margin-top:10px}.tile .l{color:var(--text-2);font-size:14px}
.ib{display:inline-grid;place-items:center;width:34px;height:34px;border-radius:10px;flex:none}
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;box-shadow:var(--shadow)}
.list{display:flex;flex-direction:column}.row{display:flex;gap:14px;align-items:center;padding:14px 18px;border-top:1px solid var(--border)}
.row:first-child{border-top:0}.row .grow{flex:1;min-width:0}.row .t{font-weight:560;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row .m{color:var(--text-2);font-size:13px}
.chip{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;font-size:12.5px;font-weight:560;white-space:nowrap}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:40px;padding:0 16px;border-radius:10px;border:1px solid var(--border);
background:var(--surface);color:var(--text);font:inherit;font-weight:560;cursor:pointer;transition:background .15s,border-color .15s}
.btn:hover{background:var(--surface-2);text-decoration:none}.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.btn.primary:hover{filter:brightness(1.07)}.btn.sm{min-height:34px;padding:0 12px;font-size:13.5px}.btn.ghost{border-color:transparent;background:transparent}
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
        sorted_n = d.get("auto_saved", 0) + d.get("proposed", 0)
        trial = c.section("processing")["trial_mode"]

        if s["paused"]:
            hero = ("pause", "mute", "დამუშავება შეჩერებულია", "ახალი ფაილები რიგში დაგროვდება. გასაგრძელებლად დააჭირე „გაგრძელებას“.")
        elif not s["worker_alive"]:
            hero = ("x", "bad", "ფონური პროცესი არ მუშაობს", "გახსენი „HR გადამრჩევი“ დაწყების მენიუდან ან გადატვირთე კომპიუტერი.")
        elif s["current"] or busy:
            cur = s["current"]
            txt = (f"ახლა: {cur['file']} — {cur['done']}/{cur['total']} გვერდი" if cur else "ფაილები მზადდება დასამუშავებლად")
            hero = ("refresh", "info", f"მუშავდება · რიგში {busy}", txt)
        elif errors:
            hero = ("alert", "bad", f"{errors} ფაილი ვერ დამუშავდა", "ნახე „ფაილები“ → „სად გაიჭედა“.")
        elif review:
            hero = ("inbox", "warn", f"{review} დოკუმენტი გელოდება", "პროგრამა ვერ დარწმუნდა — გადახედე და გადაწყვიტე.")
        else:
            hero = ("check", "ok", "ყველაფერი რიგზეა", "ახალი სკანები ავტომატურად დამუშავდება.")
        ic, tone, title, text = hero
        prog = ""
        if s["current"] and s["current"]["total"]:
            pct = 100 * s["current"]["done"] / max(1, s["current"]["total"])
            prog = f'<div class="pbar" style="margin-top:10px;max-width:420px"><span style="width:{pct:.0f}%"></span></div>'
        out = [f'<div class="hero"><span class="big tone-{tone}">{icon(ic, 26)}</span><div style="flex:1">'
               f'<h1>{e(title)}</h1><p>{e(text)}</p>{prog}</div></div>']
        if trial:
            out.append(f'<div class="banner tone-info">{icon("shield", 18)}<div><b>საცდელი რეჟიმი ჩართულია.</b> '
                       'შედეგები ჯერ შემოთავაზებულია (Proposed საქაღალდე) — საბოლოოდ არაფერი ინაცვლებს, სანამ შენ არ ჩართავ.</div></div>')
        if s["mhr_enabled"]:
            m = s["mhr"]
            if m.get("ready"):
                out.append(f'<div class="banner tone-info">{icon("cloud", 18)}<div style="flex:1"><b>{m["ready"]} ხელშეკრულება შემოწმდა და '
                           f'მზადაა MHR-ზე ასატვირთად.</b> <a href="/mhr">სია →</a></div>'
                           f'<form method="post" action="/mhr-approve-all" class="inline"><button class="btn primary">'
                           f'{icon("cloud", 16)}ყველას ატვირთვა ({m["ready"]})</button></form></div>')
            if m.get("failed"):
                out.append(f'<div class="banner tone-warn">{icon("alert", 18)}<div><b>{m["failed"]} ხელშეკრულება შაბლონს ან MHR-ის მონაცემებს '
                           f'არ ემთხვევა.</b> <a href="/mhr?tab=failed">ნახე სხვაობები →</a></div></div>')
        sorted_lbl = "შემოთავაზებული" if trial else "დალაგდა"
        out.append('<div class="tiles">' + "".join(
            f'<a class="tile" href="{href}"><span class="ib tone-{t}">{icon(i, 18)}</span><div class="n">{n}</div><div class="l">{e(lbl)}</div></a>'
            for href, i, t, n, lbl in [
                ("/documents?status=" + ("proposed" if trial else "auto_saved"), "check", "ok", sorted_n, sorted_lbl),
                ("/review", "inbox", "warn", review, "გადასამოწმებელი"),
                ("/stats", "clock", "info", busy, "რიგში"),
                ("/stats#stuck", "alert", "bad", errors, "შეცდომა")]) + "</div>")

        r = repo()
        try:
            files = r.q("SELECT s.id sid, s.first_path, s.page_count, j.id, j.status, j.updated_at FROM sources s JOIN jobs j ON j.source_id=s.id"
                        " WHERE j.created_at=(SELECT MAX(created_at) FROM jobs WHERE source_id=s.id) ORDER BY j.updated_at DESC LIMIT 8")
            counts = {}
            for x in r.q("SELECT job_id, status, COUNT(*) n FROM documents WHERE active=1 GROUP BY job_id, status"):
                counts.setdefault(x["job_id"], {})[x["status"]] = x["n"]
        finally:
            r.close()
        rows = "".join(
            f'<a class="row" href="/documents?source={e(f["sid"])}" style="color:inherit;text-decoration:none">'
            f'<span class="ib tone-mute">{icon("file", 18)}</span>'
            f'<div class="grow"><div class="t">{e(Path(f["first_path"]).name)}</div>'
            f'<div class="m">{f["page_count"] or 0} გვერდი · {_summary(counts.get(f["id"], {}))} · {ago(f["updated_at"])}</div></div>'
            f'{_outcome_chip(f["status"], counts.get(f["id"], {}))}</a>' for f in files)
        out.append('<h2>ბოლოს შემოსული ფაილები</h2><div class="card list">'
                   + (rows or empty("scan", "info", "ჯერ არაფერი შემოსულა", "დაასკანერე დოკუმენტი Inbox საქაღალდეში.")) + "</div>")
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
        rs = "".join(f'<div class="reason">{icon("alert", 18)}<div><b>{e(REASONS.get(k, (k, ""))[0])}</b><span>{e(REASONS.get(k, ("", ""))[1])}</span></div></div>'
                     for k in reasons)
        flags = "".join(chip(FLAGS.get(fl, fl), "mute") for fl in meta.get("flags", [])
                        if not (mhr_on and fl == "employee_unverified"))
        body = (f'<p><a href="javascript:history.back()">← უკან</a></p>{_flash(msg, err)}<div class="split"><div class="card section">'
                f'<div class="chips" style="margin-bottom:10px">{status_chip(x["status"])}</div>'
                f'<h1 style="font-size:20px">{e(t.name_ka if t else "დოკუმენტი")}{" · " + e(name) if name else ""}</h1>'
                f'<dl class="kv" style="margin:16px 0">{"".join(f"<dt>{e(k)}</dt><dd>{e(v)}</dd>" for k, v in kv)}</dl>'
                + (f'<div style="display:flex;flex-direction:column;gap:8px;margin-bottom:12px">{rs}</div>' if rs else "")
                + (f'<div class="chips" style="margin-bottom:14px">{flags}</div>' if flags else "")
                + f'<div class="btns"><a class="btn primary" href="/file/output/{e(doc_id)}" target="_blank">{icon("open", 16)}PDF-ის გახსნა</a>'
                f'<a class="btn" href="/file/source/{e(doc_id)}#page={x["page_start"] + 1}" target="_blank">{icon("file", 16)}წყარო</a>'
                f'<a class="btn" href="/doc/{e(doc_id)}/text">{icon("search", 16)}ამოკითხული ტექსტი</a>'
                + (f'<a class="btn" href="/mhr/{e(doc_id)}">{icon("cloud", 16)}MHR შედარება</a>' if mhr_item else "")
                + f'<form class="inline" method="post" action="/jobs/{e(x["job_id"])}/reprocess"><button class="btn">{icon("refresh", 16)}თავიდან დამუშავება</button></form></div>'
                + (_manual_block(c, x, mhr_item, f"/doc/{doc_id}") if mhr_on else "")
                + f'<details style="margin-top:18px"><summary>ტექნიკური დეტალები</summary><p class="muted mono">{e(x["output_path"])}</p>'
                f'<pre class="mono" style="white-space:pre-wrap;word-break:break-word">{e(json.dumps(meta, ensure_ascii=False, indent=2))}</pre></details></div>'
                f'<iframe class="pdf" src="/file/output/{e(doc_id)}" title="დოკუმენტის გადახედვა"></iframe></div>')
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
        rows = "".join(_mhr_row(c, x) for x in items)
        bulk = (f'<form method="post" action="/mhr-approve-all" style="margin-left:auto"><button class="btn primary">{icon("cloud", 16)}'
                f'ყველას ატვირთვა ({counts.get("ready", 0)})</button></form>' if tab == "ready" and counts.get("ready") else "")
        off = ("" if c.section("mhr")["enabled"] else
               f'<div class="banner tone-warn">{icon("alert", 18)}<div>MHR ინტეგრაცია გამორთულია — ჩართე „პარამეტრებში“.</div></div>')
        body = (f'<h1>MHR</h1><p class="sub">ყოველი დასკანერებული ხელშეკრულება ავტომატურად მოწმდება: თანამშრომელი MHR-ში, '
                f'პირადი მონაცემები, ხელმოწერები ყველა გვერდზე და ტექსტი — შაბლონთან/ორიგინალთან. „მზადაა“ — ყველაფერი ემთხვევა, '
                f'ერთი ღილაკით აიტვირთება თანამშრომლის ბარათზე. „ვერ გაიარა“ — ნახე სხვაობები და გადაწყვიტე.</p>{off}'
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
        finally:
            r.close()
        if not m:
            raise HTTPException(404)
        return page("MHR — შედარება", _flash(msg, err) + _mhr_detail_body(c, m, d), "/mhr", side_info(status_data()))

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
    def mhr_action(doc_id: str, action: str):
        from .. import mhr_queue
        fn = {"approve": mhr_queue.approve, "reject": mhr_queue.reject, "revalidate": mhr_queue.revalidate,
              "retry": mhr_queue.retry_upload, "ai-review": mhr_queue.rerun_ai_review}.get(action)
        if fn is None:
            raise HTTPException(404)
        r = repo()
        try:
            fn(r, doc_id) if action in ("revalidate", "retry", "ai-review") else fn(r, doc_id, "operator")
        finally:
            r.close()
        return RedirectResponse(f"/mhr/{doc_id}", status_code=303)

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
                           " LEFT JOIN mhr_items m ON m.document_id=d.id WHERE d.active=1 AND d.output_path IS NOT NULL"
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
    def page_image(doc_id: str, index: int):
        from fastapi.responses import Response
        from ..pdfio import PdfDocument
        r = repo()
        try:
            x = r.get_document(doc_id)
            src = r.get_source(x["source_id"]) if x else None
        finally:
            r.close()
        if not x or not src or not (x["page_start"] <= index <= x["page_end"]) or not Path(src["archive_path"]).exists():
            raise HTTPException(404)
        import io
        with PdfDocument(src["archive_path"]) as d:
            img = d.render(index, 100)
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
          "rejected": ("უარყოფილი", "mute")}
AI_KA = {"upload": ("AI: ატვირთვა შეიძლება", "ok"), "check": ("AI: გადაამოწმე", "warn"),
         "reject": ("AI: არ ატვირთო", "bad"), "error": ("AI ანალიზი ვერ შესრულდა", "mute")}
_AI_CONF_KA = {"high": "მაღალი დარწმუნებულობა", "medium": "საშუალო დარწმუნებულობა", "low": "დაბალი დარწმუნებულობა"}
_MHR_TABS = {"ready": ("მზადაა", ("ready",)), "failed": ("ვერ გაიარა", ("failed",)),
             "queue": ("მიმდინარე", ("pending", "unavailable", "approved", "uploading")),
             "done": ("ატვირთულია", ("uploaded",)), "other": ("სხვა", ("skipped", "upload_failed", "needs_check", "rejected"))}
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
        return "არ გადაეცა (მხოლოდ სანდოდ დამუშავებული დოკუმენტი მოწმდება)"
    v = json.loads(m["validation"] or "{}")
    found = "თანამშრომელი მოიძებნა" if m["employee_id"] else (
        "თანამშრომელი ვერ მოიძებნა" if v else "ჯერ არ შემოწმებულა")
    return f'{found} · {MHR_KA.get(m["status"], (m["status"], ""))[0]}'


def _mhr_name(v: dict) -> str:
    emp = v.get("employee") or {}
    return " ".join(x for x in (emp.get("first_name"), emp.get("last_name")) if x)


def _mhr_row(c: Config, x) -> str:
    v = json.loads(x["validation"] or "{}")
    t = c.doc_type(x["type_id"] or "")
    name = _mhr_name(v)
    summary = (v.get("summary") or [x["error"] or ""])[0]
    meta = " · ".join(y for y in (name, x["personal_number"], t.name_ka if t else None,
                                  f'{Path(x["first_path"]).name} · გვ. {x["page_start"] + 1}–{x["page_end"] + 1}' if x["first_path"] else None) if y)
    btns = ""
    if x["status"] == "ready":
        btns = (f'<form class="inline" method="post" action="/mhr/{e(x["document_id"])}/approve"><button class="btn sm primary">'
                f'{icon("cloud", 14)}ატვირთვა</button></form>')
    ai = _ai_load(x)
    if ai and ai.get("verdict") != "error" and ai.get("summary"):
        summary = ai["summary"]   # AI-ის ახსნა ოპერატორისთვის უფრო გასაგებია, ვიდრე წესების პირველი სტრიქონი
    ai_chip = chip(*AI_KA[ai["verdict"]]) if ai and ai.get("verdict") in AI_KA else ""
    return (f'<div class="row"><div class="grow"><a class="t" href="/mhr/{e(x["document_id"])}" style="display:block;color:var(--text)">'
            f'{e(name or "თანამშრომელი უცნობია")}</a><div class="m">{e(meta)}</div>'
            f'{f"<div class=m>{e(summary)}</div>" if summary else ""}</div>{ai_chip}{mhr_chip(x["status"])}'
            f'<div class="btns">{btns}<a class="btn sm" href="/mhr/{e(x["document_id"])}">დეტალები</a></div></div>')


def _ai_load(m) -> dict | None:
    try:
        raw = m["ai_review"]
    except (IndexError, KeyError):
        return None
    return json.loads(raw) if raw else None


def _ai_block(c: Config, m) -> str:
    """AI ანალიზის (Opus) ბლოკი MHR-ის დეტალურ გვერდზე: გადაწყვეტილება, ახსნა პუნქტობრივად, რისკები."""
    from .. import ai_review
    if not ai_review.enabled(c):
        return ""
    ai = _ai_load(m)
    doc_id = e(m["document_id"])
    rerun = (f'<form class="inline" method="post" action="/mhr/{doc_id}/ai-review"><button class="btn sm">'
             f'{icon("refresh", 14)}ხელახალი AI ანალიზი</button></form>')
    if ai is None:
        if m["status"] in ("ready", "failed"):
            return (f'<div class="banner tone-info">{icon("clock", 18)}<div><b>AI ანალიზი (Opus) რიგშია.</b> წესებით შემოწმება '
                    'უკვე დასრულდა (ქვემოთ). რამდენიმე წუთში აქ გამოჩნდება დეტალური ახსნა და რეკომენდაცია — განაახლე გვერდი.</div></div>')
        return ""
    verdict = ai.get("verdict") or "error"
    lbl, tone = AI_KA.get(verdict, AI_KA["error"])
    if verdict == "error":
        return (f'<div class="card section" style="margin-bottom:20px"><div class="chips" style="margin-bottom:8px">{chip(lbl, tone)}</div>'
                f'<p class="muted">Claude Code-მა პასუხი არ დააბრუნა ({e(ai.get("error") or "")}). შეამოწმე, რომ Claude Code '
                f'დაყენებულია და შესულია („პარამეტრები“), მერე სცადე თავიდან.</p>{rerun}</div>')
    items = "".join(
        f'<div class="row" style="align-items:flex-start"><div style="min-width:160px"><b>{e(it.get("topic") or "")}</b></div>'
        f'<div class="grow">{e(it.get("explanation") or "")}</div>{chip(*_CHECK_KA_AI.get(it.get("status"), ("—", "mute")))}</div>'
        for it in ai.get("items") or [])
    risks = "".join(f"<li>{e(r)}</li>" for r in ai.get("risks") or [])
    hint = {"upload": "AI-ის აზრით დოკუმენტი სწორია. თუ ეთანხმები — დააჭირე ატვირთვას.",
            "check": "AI-მ რაღაც ეჭვი დაინახა — ქვემოთ წერია, რა და სად. ნახე PDF-ში და მერე გადაწყვიტე.",
            "reject": "AI-ის აზრით ეს დოკუმენტი ან სხვა თანამშრომლისაა, ან სულ სხვა დოკუმენტია. ატვირთვამდე აუცილებლად გადაამოწმე."}[verdict]
    return (f'<div class="card section" style="margin-bottom:20px"><div class="chips" style="margin-bottom:8px">{chip(lbl, tone)}'
            f'{chip(_AI_CONF_KA.get(ai.get("confidence") or "low", ""), "mute")}</div>'
            f'<h2 style="margin-top:4px">AI ანალიზი · {e(ai.get("model") or "Opus")}</h2>'
            f'<p style="font-size:15px">{e(ai.get("summary") or "")}</p><p class="muted" style="font-size:13.5px">{hint}</p>'
            + (f'<div class="card list" style="margin:12px 0">{items}</div>' if items else "")
            + (f'<p><b>რას გადაამოწმებ:</b></p><ul style="margin-top:4px">{risks}</ul>' if risks else "")
            + f'<div class="btns">{rerun}</div><p class="muted" style="font-size:12.5px;margin-top:10px">AI მხოლოდ რჩევას იძლევა — '
              'არაფერი არ იტვირთება შენი დაჭერის გარეშე. გაანალიზებულია ამოკითხული ტექსტი და შედარების შედეგი, გვერდის სურათები არა.</p></div>')


_CHECK_KA_AI = {"ok": ("წესრიგშია", "ok"), "warn": ("ყურადღება", "warn"), "bad": ("პრობლემა", "bad")}


def _mhr_detail_body(c: Config, m, d) -> str:
    v = json.loads(m["validation"] or "{}")
    emp = v.get("employee") or {}
    out = [f'<p><a href="/mhr">← MHR</a></p><div class="chips" style="margin-bottom:8px">{mhr_chip(m["status"])}</div>'
           f'<h1>{e(_mhr_name(v) or "თანამშრომელი")}</h1><p class="sub">{e(m["personal_number"] or "")}'
           + (f' · MHR ID {e(emp.get("employee_id"))}' if emp.get("employee_id") else "")
           + (f' · ატვირთვის ID {e(m["remote_id"])}' if m["remote_id"] else "") + "</p>"]
    if m["error"]:
        out.append(f'<div class="banner tone-bad">{icon("x", 18)}<div>{e(m["error"])}</div></div>')
    out.append(_ai_block(c, m))
    if v.get("summary"):
        tone = "ok" if v.get("status") == "passed" else "warn"
        out.append(f'<div class="banner tone-{tone}">{icon("check" if tone == "ok" else "alert", 18)}<div>'
                   + "<br>".join(e(x) for x in v["summary"]) + "</div></div>")
    btn = {"ready": [("approve", "ატვირთვა MHR-ზე", "primary"), ("reject", "უარყოფა", "")],
           "failed": [("revalidate", "ხელახლა შედარება", ""), ("reject", "უარყოფა", "")],
           "upload_failed": [("retry", "ხელახლა ატვირთვა", "primary"), ("revalidate", "ხელახლა შედარება", "")],
           "needs_check": [("revalidate", "შემოწმდა — ხელახლა შედარება", "")],
           "unavailable": [("revalidate", "ახლავე ცდა", "")], "rejected": [("revalidate", "ხელახლა შედარება", "")]}.get(m["status"], [])
    links = ""
    if d is not None:
        links = (f'<a class="btn" href="/doc/{e(d["id"])}">{icon("file", 16)}დოკუმენტი</a>'
                 f'<a class="btn" href="/file/output/{e(d["id"])}" target="_blank">{icon("open", 16)}PDF</a>')
    out.append('<div class="btns" style="margin-bottom:20px">' + "".join(
        f'<form class="inline" method="post" action="/mhr/{e(m["document_id"])}/{a}"><button class="btn {cls}">{e(lbl)}</button></form>'
        for a, lbl, cls in btn) + links + "</div>")
    if d is not None and not (m["status"] == "failed" and m["employee_id"]):
        out.append(_manual_block(c, d, m, f"/mhr/{m['document_id']}").replace('margin-top:16px', 'margin-bottom:20px'))
    if m["status"] == "failed" and m["employee_id"]:
        ai = _ai_load(m)
        ai_ok = bool(ai and ai.get("verdict") == "upload")
        lead = ("AI-მ სხვაობები გადახედა და თვლის, რომ ეს წაკითხვის შეცდომებია — დოკუმენტი სწორია" if ai_ok
                else "ქვემოთ მოცემული სხვაობები ვნახე — დოკუმენტი სწორია")
        out.append(f'<form method="post" action="/mhr/{e(m["document_id"])}/override" class="card" style="padding:14px 16px;margin-bottom:20px">'
                   f'<label class="switch" style="margin-bottom:10px"><input type="checkbox" name="confirm" value="1" required>'
                   f'<span><b>{lead}</b><br><span class="muted" style="font-size:13.5px">'
                   'მაგ. ბეჭედი ან ხელმოწერა შეცდომით ჩაითვალა ცვლილებად. ატვირთვა ჟურნალში ჩაიწერება, როგორც შენი გადაწყვეტილება.'
                   f'</span></span></label><button class="btn{" primary" if ai_ok else ""}">ატვირთვა მაინც</button></form>')
    checks = v.get("checks") or []
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
    noise = [x for x in diffs if not x.get("significant")]

    def drow(x):
        return (f'<div class="row" style="align-items:flex-start"><div style="min-width:110px"><b>{"გვ. " + str(x["page"]) if x.get("page") else "დოკუმენტი"}</b>'
                f'<div class="m">{e(_DIFF_KA.get(x.get("kind"), x.get("kind")))}</div></div><div class="grow">'
                + (f'<div><span class="muted">ორიგინალი:</span> <del style="background:var(--bad-soft);color:var(--bad)">{e(x.get("original"))}</del></div>' if x.get("original") else "")
                + (f'<div><span class="muted">დასკანერებული:</span> <ins style="background:var(--ok-soft);color:var(--ok);text-decoration:none">{e(x.get("scanned"))}</ins></div>' if x.get("scanned") else "")
                + (f'<div class="m">{e(x.get("reason"))}</div>' if x.get("reason") else "") + "</div></div>")
    template = next((ch for ch in checks if ch.get("field") == "contract_template"), None)
    if v.get("original"):
        o = v["original"]
        out.append(f'<h2>ხელშეკრულების შედარება ორიგინალთან</h2><p class="muted" style="margin-top:-6px;font-size:13px">ორიგინალი MHR-ში: '
                   f'{e(o.get("title") or o.get("filename") or o.get("doc_id"))}{" · " + e(o.get("created_at")) if o.get("created_at") else ""}</p>')
    elif template:
        out.append(f'<h2>ხელშეკრულების შედარება შაბლონთან</h2><p class="muted" style="margin-top:-6px;font-size:13px">'
                   f'{e(template.get("note") or "")}</p>')
    if v.get("original") or template:
        out.append(f'<div class="card list">{"".join(drow(x) for x in sig) or empty("check", "ok", "შინაარსობრივი ცვლილება არ მოიძებნა")}</div>')
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
    who = (f"MHR-ში ამ პირადი ნომრით: <b>{e(known)}</b>." if known else
           f"თანამშრომელი MHR-ში მოიძებნება პირადი ნომრით <b>{e(pn)}</b>." if pn else
           "პირადი ნომერი დოკუმენტიდან არ იკითხება — გამოიყენე ქვემოთ ბმული და ჩაწერე ხელით.")
    sel = ""
    if unknown:
        sel = ('<select name="type_id" required><option value="">ტიპი — აირჩიე</option>'
               + "".join(f'<option value="{e(dt.id)}">{e(dt.name_ka)}</option>' for dt in c.active_types) + "</select>")
    btn = (f'<button class="btn primary" {"disabled" if not pn else ""}>{icon("upload", 16)}ხელით ატვირთვა MHR-ზე</button>')
    link = "/manual?" + urlencode({"pn": pn, "first": first, "last": last})
    return (f'<form method="post" action="/doc/{e(doc["id"])}/manual-upload" class="card" style="padding:14px 16px;margin-top:16px">'
            f'<input type="hidden" name="back" value="{e(back)}"><div style="margin-bottom:10px"><b>ხელით ატვირთვა</b> — ავტომატური შედარების '
            f'გარეშე, მაშინვე. {who}<br><span class="muted" style="font-size:13px">ატვირთვა ჟურნალში შენს გადაწყვეტილებად იწერება. '
            f'სხვა თანამშრომელზე ან პ/ნ-ის გარეშე — <a href="{e(link)}">ხელით ატვირთვის გვერდი</a>.</span></div>'
            f'<div class="btns" style="align-items:center">{sel}{btn}</div></form>')


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
