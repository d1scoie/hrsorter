"""შეფასების ინსტრუმენტი: მონიშნული ნაკრები → სრული დამუშავება ცალკე დროებით root-ში → მეტრიკები.

ერთი და იგივე ნაკრები ეშვება სხვადასხვა ამოცნობის გზით (--ocr-path local|online|hybrid), რათა
ადგილობრივი და ონლაინ მიდგომები შედარდეს (სპეც. §16, §20).

ground truth ფორმატი (სინთეზური გენერატორიც ამას წერს; რეალური ნაკრებისთვის ხელით შეადგინე):
  {"files": [{"file": "<pdf>", "mode": "batch_pdf"|"single_document_pdf",
              "documents": [{"start": 0, "end": 2, "type_id": "contract", "personal_number": "01001000001",
                             "company_id": "acme", "year": 2020, "document_date": "2020-10-18",
                             "first_name": "...", "last_name": "...", "tags": ["handwritten", ...]}],
              "blank_pages": [5]}]}
  გვერდები 0-დან, start/end ჩათვლით. არასავალდებულო ველი გამოტოვე, თუ ის არ არის მონიშნული.
  {"batch": {...}, "singles": [...]} (tools.synthetic-ის ფორმატი) ავტომატურად გარდაიქმნება.

გამოყენება:
  python -m tools.evaluate --gt ground_truth.json --work <ცარიელი საქაღალდე> [--config config/config.json]
         [--ocr-path local] [--label "ადგილობრივი"] --out report.json
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

from hrsorter.config import EXAMPLE_CONFIG_PATH, Config, deep_merge, load_config, make_config
from hrsorter.db import Repository
from hrsorter.pdfio import sha256_file

FIELDS = ["personal_number", "company_id", "year", "document_date", "first_name", "last_name"]


def normalize_gt(gt: dict) -> list[dict]:
    if "files" in gt:
        return gt["files"]
    files = []
    if gt.get("batch"):
        files.append({**gt["batch"], "mode": gt["batch"].get("mode") or "batch_pdf"})
    for s in gt.get("singles") or []:
        files.append({**s, "mode": "single_document_pdf"})
    return files


def machine_info() -> dict:
    info = {"platform": platform.platform(), "processor": platform.processor(), "cpu_count": os.cpu_count(),
            "python": sys.version.split()[0]}
    if sys.platform == "win32":
        class MEMSTAT(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MEMSTAT(); m.dwLength = ctypes.sizeof(m)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            info["ram_gb"] = round(m.ullTotalPhys / 2**30, 1)
    return info


def peak_rss_mb() -> float | None:
    if sys.platform != "win32":
        try:
            import resource
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        except Exception:
            return None

    class PMC(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong), ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]
    c = PMC(); c.cb = ctypes.sizeof(c)
    k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(PMC), ctypes.c_ulong]
    if psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
        return round(c.PeakWorkingSetSize / 2**20, 1)
    return None


def run(files: list[dict], cfg: Config, work: Path) -> dict:
    """ფაილები ინბოქსებში → სკანირება → რიგის დამუშავება. აბრუნებს {sha256: file_gt} და დროს."""
    from hrsorter.pipeline import process_job
    from hrsorter.watcher import InboxScanner
    from hrsorter.worker import Worker

    for d in [*cfg.paths.internal_dirs(), *[p.inbox for p in cfg.profiles]]:
        d.mkdir(parents=True, exist_ok=True)
    inbox = {p.mode.value: p.inbox for p in cfg.profiles}
    by_sha = {}
    for i, f in enumerate(files):
        dest = inbox[f["mode"]] / f"{i:05d}_{Path(f['file']).name}"
        shutil.copyfile(f["file"], dest)
        by_sha[sha256_file(dest)] = f
    repo = Repository(cfg.paths.db)
    scanner = InboxScanner(cfg, repo)
    t0 = time.perf_counter()
    for _ in range(3):  # stability_seconds=0 — ორი გავლა საკმარისია
        scanner.scan_once()
    worker = Worker(lambda: cfg, process_job)
    while worker.run_once():
        pass
    seconds = time.perf_counter() - t0
    return {"by_sha": by_sha, "seconds": seconds, "repo": repo}


def score(by_sha: dict, repo: Repository) -> dict:
    gt_docs = pred_docs = matched = type_ok_matched = fully_ok = 0
    auto = auto_type_ok = auto_pages_ok = 0
    pages_total = pages_accounted = 0
    field_hits: Counter = Counter(); field_total: Counter = Counter(); field_wrong: Counter = Counter()
    reasons: Counter = Counter(); outcomes: Counter = Counter(); confusion: Counter = Counter()
    failed_jobs = []
    tag_stats: dict[str, Counter] = {}
    online_pages = 0
    for sha, f in by_sha.items():
        src = repo.source_by_sha(sha)
        if not src:
            failed_jobs.append({"file": f["file"], "error": "not ingested"})
            continue
        job = repo.one("SELECT * FROM jobs WHERE source_id=? ORDER BY created_at DESC LIMIT 1", (src["id"],))
        if job["status"] != "done":
            failed_jobs.append({"file": f["file"], "error": job["error_code"]})
        pages_total += src["page_count"] or 0
        pages_accounted += repo.one("SELECT COUNT(DISTINCT page_index) n FROM page_assignments WHERE job_id=?",
                                    (job["id"],))["n"]
        for r in repo.q("SELECT data FROM page_texts WHERE job_id=?", (job["id"],)):
            if '"text_source": "ocr:online' in r["data"] or '"provider": "online' in r["data"]:
                online_pages += 1
        docs = repo.q("SELECT * FROM documents WHERE job_id=? AND active=1", (job["id"],))
        gt_by_range = {(d["start"], d["end"]): d for d in f["documents"]}
        gt_docs += len(f["documents"])
        for d in docs:
            pred_docs += 1
            outcomes[d["status"]] += 1
            rr = json.loads(d["review_reasons"] or "[]")
            reasons.update(rr)
            g = gt_by_range.get((d["page_start"], d["page_end"]))
            is_auto = d["status"] in ("auto_saved", "proposed")
            if is_auto:
                auto += 1
                auto_pages_ok += g is not None
                auto_type_ok += g is not None and g["type_id"] == d["type_id"]
            if g is None:
                continue
            matched += 1
            confusion[(g["type_id"], d["type_id"])] += 1
            type_ok = g["type_id"] == d["type_id"]
            type_ok_matched += type_ok
            meta = json.loads(d["metadata"] or "{}")
            pred = {"personal_number": d["personal_number"], "company_id": d["company_id"], "year": d["year"],
                    "document_date": d["document_date"],
                    "first_name": (meta.get("fields", {}).get("first_name") or {}).get("value"),
                    "last_name": (meta.get("fields", {}).get("last_name") or {}).get("value")}
            all_ok = type_ok
            for k in FIELDS:
                if k not in g:
                    continue
                gv = g[k]
                field_total[k] += 1
                if pred[k] == gv:
                    field_hits[k] += 1
                else:
                    if k in ("personal_number", "company_id", "year") and gv is not None:
                        all_ok = False
                    if pred[k] is not None:
                        field_wrong[k] += 1  # ამოღებული, მაგრამ არასწორი (გამოგონილის/შეცდომის ნიშანი)
            fully_ok += all_ok
            for tag in g.get("tags", []):
                c = tag_stats.setdefault(tag, Counter())
                c["n"] += 1; c["type_ok"] += type_ok; c["auto"] += is_auto
    ratio = lambda a, b: round(a / b, 4) if b else None  # noqa: E731
    return {
        "gt_documents": gt_docs, "predicted_documents": pred_docs, "boundary_exact_matches": matched,
        "boundary_precision": ratio(matched, pred_docs), "boundary_recall": ratio(matched, gt_docs),
        "type_accuracy_all_gt_docs": ratio(type_ok_matched, gt_docs),
        "type_accuracy_on_matched": ratio(type_ok_matched, matched),
        "fully_correct_share": ratio(fully_ok, gt_docs),
        "automated_share": ratio(auto, pred_docs),
        "automated_type_accuracy": ratio(auto_type_ok, auto),
        "automated_exact_pages_accuracy": ratio(auto_pages_ok, auto),
        "field_accuracy": {k: ratio(field_hits[k], field_total[k]) for k in FIELDS if field_total[k]},
        "field_wrong_value_rate": {k: ratio(field_wrong[k], field_total[k]) for k in FIELDS if field_total[k]},
        "page_accounting": ratio(pages_accounted, pages_total), "pages": pages_total,
        "outcomes": dict(outcomes), "review_reasons": dict(reasons.most_common()),
        "type_confusion": {f"{a}->{b}": n for (a, b), n in sorted(confusion.items()) if a != b},
        "by_tag": {t: dict(c) for t, c in sorted(tag_stats.items())},
        "online_pages": online_pages, "failed_jobs": failed_jobs,
    }


def online_usage(cfg: Config) -> dict:
    """ონლაინ OCR-ის ტოკენები და სავარაუდო ხარჯი (მხოლოდ Anthropic ადაპტერისთვის, ამ პროცესში)."""
    try:
        from hrsorter.ocr import anthropic_vision as av
    except ImportError:
        return {}
    u = dict(av.usage_totals)
    if u["pages"]:
        u["estimated_cost_usd"] = round(av.estimated_cost_usd(cfg.section("ocr")["online"]), 4)
        u["cost_per_page_usd"] = round(u["estimated_cost_usd"] / u["pages"], 5)
    return u


def to_markdown(rep: dict) -> str:
    m = rep["metrics"]
    pct = lambda v: "—" if v is None else f"{v * 100:.1f}%"  # noqa: E731
    lines = [f"# შეფასება: {rep['label']}", "",
             f"- ნაკრები: {rep['gt_path']} — ფაილები {rep['files']}, გვერდები {m['pages']}, დოკუმენტები {m['gt_documents']}",
             f"- ამოცნობის გზა: `{rep['ocr_path']}`, კონფიგურაცია {rep['config_version']}",
             f"- კომპიუტერი: {rep['machine']}",
             f"- დრო: {rep['seconds']} წმ ({rep['pages_per_second']} გვ/წმ), პიკური მეხსიერება {rep['peak_rss_mb']} MB",
             f"- ონლაინ დამუშავებული გვერდები: {m['online_pages']}; ხარჯი: {rep.get('online_usage') or '—'}", "",
             "| მეტრიკა | მნიშვნელობა |", "|---|---|"]
    for k, lbl in [("type_accuracy_all_gt_docs", "ტიპის სიზუსტე (ყველა დოკ.)"),
                   ("boundary_precision", "საზღვრების precision"), ("boundary_recall", "საზღვრების recall"),
                   ("fully_correct_share", "სრულად სწორი დოკუმენტები"), ("automated_share", "ავტომატიზაციის წილი"),
                   ("automated_type_accuracy", "ავტომატურებში სწორი ტიპი (სამიზნე ≥99%)"),
                   ("automated_exact_pages_accuracy", "ავტომატურებში ზუსტი გვერდები (სამიზნე ≥99%)"),
                   ("page_accounting", "გვერდების აღრიცხვა (უნდა იყოს 100%)")]:
        lines.append(f"| {lbl} | {pct(m[k])} |")
    lines += ["", "ველების სიზუსტე / არასწორი მნიშვნელობის წილი:", ""]
    for k, v in m["field_accuracy"].items():
        lines.append(f"- {k}: {pct(v)} / არასწორი {pct(m['field_wrong_value_rate'][k])}")
    lines += ["", f"შედეგები: {m['outcomes']}", f"გადამოწმების მიზეზები: {m['review_reasons']}",
              f"ტიპების აღრევა: {m['type_confusion'] or 'არა'}", f"ჩავარდნილი სამუშაოები: {m['failed_jobs'] or 'არა'}", "",
              "ტეგების მიხედვით (n / სწორი ტიპი / ავტომატური):", ""]
    for t, c in m["by_tag"].items():
        lines.append(f"- {t}: {c.get('n', 0)} / {c.get('type_ok', 0)} / {c.get('auto', 0)}")
    lines += ["", f"> {rep['caveat']}"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="hrsorter შეფასება მონიშნულ ნაკრებზე")
    ap.add_argument("--gt", required=True)
    ap.add_argument("--work", required=True, help="ცარიელი/დროებითი საქაღალდე (შიგთავსი წაიშლება)")
    ap.add_argument("--config", help="საბაზისო config.json (paths.root გადაიფარება)")
    ap.add_argument("--ocr-path", choices=["local", "online", "hybrid"])
    ap.add_argument("--label", default="")
    ap.add_argument("--out", required=True, help="report.json; გვერდით იწერება report.md")
    a = ap.parse_args(argv)
    gt_path = Path(a.gt)
    files = normalize_gt(json.loads(gt_path.read_text(encoding="utf-8")))
    for f in files:  # ფარდობითი ბილიკები gt ფაილის მიმართ
        if not Path(f["file"]).is_absolute():
            f["file"] = str(gt_path.parent / f["file"])
    work = Path(a.work)
    shutil.rmtree(work, ignore_errors=True)
    over = {"paths": {"root": str(work)}, "watcher": {"stability_seconds": 0}}
    if a.ocr_path:
        over["ocr"] = {"path": a.ocr_path}
    if a.config:
        base = load_config(a.config)
        cfg = Config(deep_merge(base.data, over), base.types_data)
    else:
        example = json.loads(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))
        cfg = make_config(deep_merge({"companies": example["companies"],
                                      "employee_api": {"enabled": True, "mode": "mock"}}, over))
    res = run(files, cfg, work)
    metrics = score(res["by_sha"], res["repo"])
    rep = {"label": a.label or cfg.section("ocr")["path"], "gt_path": str(gt_path), "files": len(files),
           "ocr_path": cfg.section("ocr")["path"], "config_version": cfg.version, "machine": machine_info(),
           "seconds": round(res["seconds"], 1),
           "pages_per_second": round(metrics["pages"] / res["seconds"], 2) if res["seconds"] else None,
           "peak_rss_mb": peak_rss_mb(), "metrics": metrics, "online_usage": online_usage(cfg),
           "caveat": "შედეგი ეხება მხოლოდ ამ ნაკრებს. სინთეზური მონაცემები რეალური სკანების სიზუსტეს არ ადასტურებს; "
                     "ზღვრების კალიბრაციისა და საბოლოო შეფასების ნაკრებები ცალ-ცალკე უნდა იყოს."}
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    out.with_suffix(".md").write_text(to_markdown(rep), encoding="utf-8")
    print(to_markdown(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
