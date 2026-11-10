"""ერთი სამუშაოს (job) სრული დამუშავება: გვერდები → საზღვრები → ტიპი → ველები → გადაწყვეტილება → შენახვა.

worker იძახებს `process_job(repo, job, cfg)`-ს, სადაც cfg არის სამუშაოს შენახული snapshot.
ხელახლა გაშვება (ავარიის შემდეგ) უსაფრთხოა: გვერდების ტექსტი ბაზიდან იტვირთება, document_id
დეტერმინისტულია (job + გვერდების დიაპაზონი), publish კი იდემპოტენტურია.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import sqlite3
import time
import uuid
from pathlib import Path

from . import PIPELINE_VERSION
from .boundary import detect_segments
from .classifier import Classifier
from .config import Config
from .db import Repository, now_iso
from .decision import decide
from .duplicates import check as check_duplicates
from .duplicates import content_fingerprint, text_fingerprint
from .employee_api import build_directory, lookup_employee
from .errors import HRSorterError, JobError
from .extractor import extract
from .features import page_features
from .models import (DocumentOutcome, ErrorCode, FieldSource, FieldValue, InputMode, PageRole, PageText, Segment, SegmentKind)
from .ocr import build_ocr_chain
from .output import MATCH_RESOLVED_REASONS, publish, retire_previous
from .pageanalysis import analyze_page
from .pdfio import PdfDocument, sha256_file
from .resolve import resolve_company, resolve_year

log = logging.getLogger(__name__)

_DOC_NS = uuid.UUID("6f1c2b9e-4d0a-4c39-9f53-6a3b2a8d7e10")


def stable_document_id(job_id: str, start: int, end: int) -> str:
    """ერთი და იგივე სამუშაოს ერთი და იგივე მონაკვეთი ყოველთვის ერთსა და იმავე ID-ს იღებს."""
    return f"doc_{uuid.uuid5(_DOC_NS, f'{job_id}:{start}-{end}').hex}"


def process_job(repo: Repository, job: sqlite3.Row, cfg: Config) -> None:
    try:
        _process(repo, job, cfg)
    except HRSorterError:
        raise
    except OSError as e:
        if e.errno == errno.ENOSPC or getattr(e, "winerror", None) == 112:
            raise JobError(ErrorCode.DISK_FULL.value, str(e), retryable=True) from e
        raise


def _process(repo: Repository, job: sqlite3.Row, cfg: Config) -> None:
    t0 = time.time()
    job_id = job["id"]
    src = repo.get_source(job["source_id"])
    if src is None:
        raise JobError(ErrorCode.INTERNAL.value, f"source {job['source_id']} missing")
    archive_path = src["archive_path"]
    if not Path(archive_path).exists() or sha256_file(archive_path) != src["sha256"]:
        raise JobError(ErrorCode.ARCHIVE_HASH_MISMATCH.value, "archived source missing or changed")
    mode = InputMode(job["mode"])

    # 1) გვერდების ანალიზი (resume-ით)
    chain = build_ocr_chain(cfg)
    stored = repo.load_page_texts(job_id)
    pages: list[PageText] = []
    page_hashes: list[str] = []
    with PdfDocument(archive_path) as doc:
        n = doc.page_count
        repo.update_job_progress(job_id, stage="pages", done=len(stored), total=n)
        for i in range(n):
            if i in stored:
                pt = PageText.from_dict(stored[i])
            else:
                try:
                    pt = analyze_page(doc, i, cfg, chain)
                except Exception as e:
                    raise JobError(ErrorCode.OCR_FAILED.value, f"page {i + 1}: {type(e).__name__}", retryable=True) from e
                repo.save_page_text(job_id, i, pt.to_dict())
            pages.append(pt)
            page_hashes.append(doc.page_content_hash(i))
            repo.update_job_progress(job_id, done=i + 1)

    # 2) საზღვრები
    repo.update_job_progress(job_id, stage="segmentation")
    classifier = Classifier(cfg)
    features = [page_features(pt, classifier, cfg) for pt in pages]
    seg = detect_segments(features, mode, cfg)

    # 3) გვერდების სრული აღრიცხვა — ინვარიანტი (შენახვამდე)
    covered = sorted(p for s in seg.segments for p in s.pages)
    if covered != list(range(n)):
        raise JobError(ErrorCode.INTERNAL.value, f"page accounting mismatch: {len(covered)} assigned of {n}")

    # 4) დოკუმენტები
    repo.update_job_progress(job_id, stage="documents")
    directory = build_directory(cfg, repo)
    attempts = [dict(r) for r in repo.q(
        "SELECT attempt, started_at, finished_at, status, error_code FROM job_attempts WHERE job_id=? ORDER BY attempt",
        (job_id,))]
    assignments: list[tuple[int, str, str | None]] = []
    outcomes: list[DocumentOutcome] = []
    for s in seg.segments:
        if s.kind == SegmentKind.BLANK:
            assignments += [(p, PageRole.BLANK.value, None) for p in s.pages]
            continue
        outcome = _build_outcome(repo, job, src, cfg, s, pages, page_hashes, classifier, directory,
                                 seg.mode_mismatch, attempts)
        outcome = publish(repo, outcome, cfg)
        outcomes.append(outcome)
        # MHR: სანდოდ დამუშავებული დოკუმენტი → ვალიდაციის რიგი (ვალიდაციასა და ატვირთვას worker-ის sweep აკეთებს)
        if cfg.section("mhr")["enabled"] and _goes_to_mhr(outcome):
            pn = outcome.extraction.fields.get("personal_number")
            repo.mhr_enqueue(outcome.document_id, pn.value if pn else None)
        role = PageRole.DOCUMENT if s.kind == SegmentKind.DOCUMENT else PageRole.UNCERTAIN
        assignments += [(p, role.value, outcome.document_id) for p in s.pages]

    # 5) ხელახალი დამუშავება: წინა შედეგები → _Superseded მხოლოდ ახლების შენახვის შემდეგ
    #    (თუ ახალი ჩავარდა, ძველი აქტიური რჩება; ერთ დოკუმენტზე ორი აქტიური შედეგი არ რჩება)
    if job["reprocess_of"]:
        retire_previous(repo, src["id"], job_id, cfg)
    repo.set_page_assignments(job_id, assignments)

    report = _write_report(cfg, job, src, n, seg, outcomes, assignments, time.time() - t0)
    repo.update_job_progress(job_id, stage="done")
    repo.conn.execute("UPDATE jobs SET report_path=? WHERE id=?", (str(report), job_id))


def _goes_to_mhr(outcome: DocumentOutcome) -> bool:
    """სანდოდ დამუშავებული დოკუმენტი, ან გადასამოწმებელი, რომელსაც მხოლოდ ტიპი/კომპანია აკლია და პირადი ნომერი იკითხება —
    თანამშრომლის დაგენერირებულ ფაილს შეიძლება დაემთხვეს (ცალკე დასკანერებული ინსტრუქცია, წესები, დანართი)."""
    if outcome.decision.outcome in ("proposed", "auto_saved"):
        return True
    pn = outcome.extraction.fields.get("personal_number")
    return bool(pn and pn.value) and set(outcome.decision.review_reasons) <= MATCH_RESOLVED_REASONS


def _typed_document_date(extraction, doc_type, seg_pages: list[PageText]) -> None:
    """პირადობაზე და ანკეტაზე ტექსტში პირველი თარიღი დაბადების თარიღია. დოკუმენტის თარიღი კი პირადობაზე გაცემის, ანკეტაზე
    შევსების თარიღია; ვერ წაიკითხა → თარიღი უცნობია (დაბადების თარიღით წელი და საქაღალდე არ დგება)."""
    if doc_type is None or not (doc_type.identity or doc_type.employee_form):
        return
    text = "\n".join(p.text or "" for p in seg_pages)
    if doc_type.identity:
        from .idcard import parse_id_card
        iso, what = parse_id_card(text).issue_date, "გაცემის თარიღი"
    else:
        from .empform import parse_employee_form
        form = parse_employee_form(text)
        iso, what = form.fill_date or form.start_date, "შევსების თარიღი"
    old = extraction.fields.get("document_date")
    candidates = [c for c in (old.candidates if old else []) if c != iso]
    if old and old.value and old.value != iso:
        candidates.append(old.value)
    extraction.fields["document_date"] = FieldValue(
        value=iso, source=FieldSource.DOCUMENT.value if iso else None, page_index=seg_pages[0].page_index if seg_pages else None,
        confidence=0.9 if iso else 0.0, evidence=what if iso else None, handwritten=bool(doc_type.employee_form),
        candidates=candidates)


def _build_outcome(repo, job, src, cfg: Config, s: Segment, pages: list[PageText], page_hashes: list[str],
                   classifier: Classifier, directory, mode_mismatch: bool, attempts: list[dict]) -> DocumentOutcome:
    seg_pages = [pages[i] for i in s.pages]
    classification = classifier.classify_pages(seg_pages)
    doc_type = cfg.doc_type(classification.type_id)
    extraction = extract(seg_pages, doc_type, cfg)
    _typed_document_date(extraction, doc_type, seg_pages)
    employee = lookup_employee(directory, extraction)
    company = resolve_company(extraction, cfg, job["profile_id"], employee)
    document_date, year = resolve_year(extraction)
    content_fp = content_fingerprint([page_hashes[i] for i in s.pages])
    text_fp, shingles = text_fingerprint(seg_pages)
    pn_field = extraction.fields.get("personal_number")
    duplicate = check_duplicates(
        repo, content_fp=content_fp, text_fp=text_fp, shingles=shingles, type_id=classification.type_id,
        personal_number=pn_field.value if pn_field else None,
        doc_number=extraction.fields["document_number"].value if "document_number" in extraction.fields else None,
        document_date=document_date.value, job_id=job["id"], cfg=cfg,
        document_id=stable_document_id(job["id"], s.start, s.end), source_id=src["id"])
    decision = decide(segment=s, classification=classification, doc_type=doc_type, company=company,
                      extraction=extraction, employee=employee, duplicate=duplicate,
                      mode_mismatch=mode_mismatch, cfg=cfg, pages=seg_pages)
    providers = sorted({p.provider or p.text_source for p in seg_pages})
    models = sorted({p.model_version for p in seg_pages if p.model_version})
    return DocumentOutcome(
        document_id=stable_document_id(job["id"], s.start, s.end), job_id=job["id"], source_id=src["id"],
        source_sha256=src["sha256"], source_path=src["archive_path"], profile_id=job["profile_id"],
        mode=job["mode"], segment=s, pages=s.pages, classification=classification, extraction=extraction,
        employee=employee, company=company, document_date=document_date, year=year, duplicate=duplicate,
        decision=decision, signature_status=extraction.signature_status, content_fingerprint=content_fp,
        text_fingerprint=text_fp, provider="+".join(providers), model_version="+".join(models),
        pipeline_version=PIPELINE_VERSION, config_version=cfg.version, attempt=job["attempt"],
        attempt_history=attempts)


def _write_report(cfg: Config, job, src, n: int, seg, outcomes: list[DocumentOutcome],
                  assignments: list[tuple[int, str, str | None]], seconds: float) -> Path:
    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o.decision.outcome] = counts.get(o.decision.outcome, 0) + 1
    roles: dict[str, int] = {}
    for _, role, _ in assignments:
        roles[role] = roles.get(role, 0) + 1
    report = {
        "job_id": job["id"], "source_id": src["id"], "source_sha256": src["sha256"], "mode": job["mode"],
        "profile_id": job["profile_id"], "config_version": cfg.version, "pipeline_version": PIPELINE_VERSION,
        "pages": n, "page_roles": roles, "outcomes": counts, "mode_mismatch": seg.mode_mismatch,
        "segments": [{"start": s.start + 1, "end": s.end + 1, "kind": s.kind.value,
                      "boundary_confidence": round(s.boundary_confidence, 3)} for s in seg.segments],
        "documents": [{"document_id": o.document_id, "pages": [o.pages[0] + 1, o.pages[-1] + 1],
                       "type": o.classification.type_id, "outcome": o.decision.outcome,
                       "review_reasons": o.decision.review_reasons, "output_path": o.output_path}
                      for o in outcomes],
        "seconds": round(seconds, 2), "finished_at": now_iso(),
    }
    out_dir = cfg.paths.reports
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{job['id']}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path
