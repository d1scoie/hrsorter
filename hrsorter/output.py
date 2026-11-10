"""ატომური გამოქვეყნება: tmp (_Work) → ვალიდაცია → sha256 → sidecar → os.replace → DB.

ინვარიანტები:
  * ნაწილობრივ ჩაწერილი ფაილი საბოლოო საქაღალდეში არასდროს ჩანს (tmp იმავე ტომზეა, os.replace ატომურია).
  * output_sha256 და sidecar (metadata) DB-ში იწერება os.replace-მდე — ავარიის შემდეგ `recover_publishing`
    ზუსტად იცის, ჩვენი ფაილია თუ არა საბოლოო ბილიკზე.
  * იგივე document_id (job + გვერდების დიაპაზონი) მეორე საბოლოო ასლს არ ქმნის.
  * ფაილები არასდროს იშლება: ჩანაცვლებული შედეგები გადადის `_Superseded/<doc_id>/`-ში.
"""
from __future__ import annotations

import errno
import json
import os
import shutil
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import naming
from .config import Config
from .db import now_iso
from .duplicates import text_fingerprint
from .errors import JobError
from .extractor import extract
from .models import DocumentOutcome, DocumentStatus, ErrorCode, PageText
from .pdfio import sha256_file, validate_pdf, write_subset
from .sidecar import _field as _sidecar_field, best_personal_number, build_sidecar, validate_sidecar

_DISK_FULL_WINERRORS = {39, 112}  # ERROR_HANDLE_DISK_FULL, ERROR_DISK_FULL


def _is_disk_full(e: OSError) -> bool:
    return e.errno == errno.ENOSPC or getattr(e, "winerror", None) in _DISK_FULL_WINERRORS


def _base_dir(outcome: DocumentOutcome, cfg: Config) -> Path:
    p = cfg.paths
    return {DocumentStatus.AUTO_SAVED.value: p.sorted, DocumentStatus.PROPOSED.value: p.proposed,
            DocumentStatus.NEEDS_REVIEW.value: p.needs_review}[outcome.decision.outcome]


def _tmp_paths(doc_id: str, cfg: Config) -> tuple[Path, Path]:
    w = cfg.paths.work
    return w / f"{doc_id}.pdf.tmp", w / f"{doc_id}.json.tmp"


def _replace(tmp: Path, dest: Path) -> None:
    """`os.replace(tmp, dest)`. ზოგი ქსელური საცავი (NAS) გრძელ ბილიკზე **არსებული** ფაილის ჩანაცვლებას ვერ ასრულებს —
    PermissionError (WinError 31), თუმცა ფაილი არც დაკავებულია და არც დაცული, ახალ სახელზე გადატანა კი მუშაობს. ნამდვილ
    შემთხვევაში (ქართული სახელი ~130 სიმბოლოიან ბილიკზე) ამაზე თავიდან დამუშავება ყოველ ცდაზე ჩერდებოდა. მაშინ შიგთავსი
    პირდაპირ იწერება არსებულ ფაილში: ატომური აღარ არის, მაგრამ sidecar-ის/შედეგის წყარო ბაზაა და `recover_publishing`
    sha-თი ამოწმებს. მართლა დაკავებულ ფაილზე ეს გზაც PermissionError-ს იძლევა — შეცდომა ისევ ზემოთ ადის."""
    try:
        os.replace(tmp, dest)
    except PermissionError:
        if not dest.exists():
            raise
        with open(tmp, "rb") as src, open(dest, "r+b") as out:
            shutil.copyfileobj(src, out)
            out.truncate()
        tmp.unlink(missing_ok=True)


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _replace(tmp, path)


def _shingles(repo, outcome: DocumentOutcome) -> list[int]:
    """სავარაუდო დუბლიკატისთვის: shingles შენახული გვერდების ტექსტიდან (outcome-ში არ ინახება)."""
    stored = repo.load_page_texts(outcome.job_id)
    pages = [PageText.from_dict(stored[i]) for i in outcome.pages if i in stored]
    return text_fingerprint(pages)[1] if pages else []


def _with_paths(outcome: DocumentOutcome, row) -> DocumentOutcome:
    return replace(outcome, output_path=row["output_path"], output_sha256=row["output_sha256"],
                   sidecar_path=row["sidecar_path"])


def _update_document(repo, doc_id: str, cols: dict[str, Any]) -> None:
    """არსებული ჩანაწერის ნაწილობრივი განახლება (upsert_document ყველა NOT NULL სვეტს ითხოვს)."""
    d = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v) for k, v in cols.items()}
    d["updated_at"] = now_iso()
    repo.conn.execute(f"UPDATE documents SET {', '.join(f'{k}=?' for k in d)} WHERE id=?", [*d.values(), doc_id])


def _finalize_row(repo, doc_id: str, status: str, output_path: str, sidecar_path: str) -> None:
    _update_document(repo, doc_id, {"status": status, "active": 1, "output_path": output_path,
                                    "sidecar_path": sidecar_path})


def _recover_row(repo, row, cfg: Config) -> bool:
    """PUBLISHING ჩანაწერი: საბოლოო ფაილი არსებობს და sha ემთხვევა → დასრულება (True);
    სხვა შემთხვევაში ნარჩენები იშლება და ჩანაწერი ქრება, რომ ხელახალმა გაშვებამ დეტერმინისტულად გამოაქვეყნოს."""
    for t in _tmp_paths(row["id"], cfg):
        t.unlink(missing_ok=True)
    planned = Path(row["planned_path"]) if row["planned_path"] else None
    sha, meta = row["output_sha256"], row["metadata"]
    if planned and sha and meta and planned.exists() and sha256_file(planned) == sha:
        data = json.loads(meta)
        side = planned.with_suffix(".json")
        if not side.exists():
            _write_json_atomic(side, data)
        _finalize_row(repo, row["id"], data["processing_status"], str(planned), str(side))
        return True
    if planned:
        side = planned.with_suffix(".json")
        if side.exists():
            try:
                ours = json.loads(side.read_text(encoding="utf-8")).get("document_id") == row["id"]
            except (OSError, ValueError):
                ours = False
            if ours:
                side.unlink()
    repo.conn.execute("DELETE FROM documents WHERE id=? AND status=?", (row["id"], DocumentStatus.PUBLISHING.value))
    return False


def recover_publishing(repo, cfg: Config) -> int:
    """ავარიის შემდეგ PUBLISHING ჩანაწერების დასრულება/გასუფთავება. აბრუნებს დამუშავებული ჩანაწერების რაოდენობას."""
    rows = repo.documents_in_state(DocumentStatus.PUBLISHING.value)
    for row in rows:
        _recover_row(repo, row, cfg)
    return len(rows)


def publish(repo, outcome: DocumentOutcome, cfg: Config) -> DocumentOutcome:
    doc_id = outcome.document_id
    row = repo.get_document(doc_id)
    if row is not None and row["status"] == DocumentStatus.PUBLISHING.value:
        if _recover_row(repo, row, cfg):
            return _with_paths(outcome, repo.get_document(doc_id))
        row = None
    if row is not None and row["active"] and row["output_path"] and row["output_sha256"] \
            and Path(row["output_path"]).exists() and sha256_file(row["output_path"]) == row["output_sha256"]:
        return _with_paths(outcome, row)   # იდემპოტენტური: უკვე გამოქვეყნებულია

    final_pdf = naming.build_output_path(_base_dir(outcome, cfg), outcome, cfg)
    side = final_pdf.with_suffix(".json")
    tmp_pdf, tmp_json = _tmp_paths(doc_id, cfg)
    ext = outcome.extraction.fields
    doc_no = ext["document_number"].value if ext.get("document_number") else None
    repo.upsert_document({
        "id": doc_id, "job_id": outcome.job_id, "source_id": outcome.source_id,
        "page_start": min(outcome.pages), "page_end": max(outcome.pages),
        "status": DocumentStatus.PUBLISHING.value, "active": 1, "planned_path": str(final_pdf),
        "output_path": None, "output_sha256": None, "sidecar_path": None, "metadata": None,
    })
    try:
        cfg.paths.work.mkdir(parents=True, exist_ok=True)
        write_subset(outcome.source_path, outcome.pages, tmp_pdf)
        chk = validate_pdf(tmp_pdf)
        if not chk.ok or chk.page_count != len(outcome.pages):
            raise JobError(ErrorCode.OUTPUT_VERIFY_FAILED.value,
                           f"{doc_id}: {chk.error_code or 'page count'} {chk.page_count}/{len(outcome.pages)}")
        sha = sha256_file(tmp_pdf)
        result = replace(outcome, output_path=str(final_pdf), output_sha256=sha, sidecar_path=str(side))
        data = build_sidecar(result, cfg)
        validate_sidecar(data)
        tmp_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        _update_document(repo, doc_id, {
            "output_sha256": sha, "metadata": data,
            "type_id": outcome.classification.type_id, "company_id": outcome.company.company_id,
            "employee_id": data["employee_id"], "personal_number": best_personal_number(outcome),
            "year": outcome.year.value, "document_date": outcome.document_date.value, "doc_number": doc_no,
            "review_reasons": list(outcome.decision.review_reasons),
            "content_fingerprint": outcome.content_fingerprint or None,
            "text_fingerprint": outcome.text_fingerprint or None,
            "text_shingles": _shingles(repo, outcome),
        })
        final_pdf.parent.mkdir(parents=True, exist_ok=True)
        _replace(tmp_pdf, final_pdf)
        _replace(tmp_json, side)
    except OSError as e:
        if _is_disk_full(e):
            raise JobError(ErrorCode.DISK_FULL.value, "no space left while publishing", retryable=True) from e
        raise
    finally:
        tmp_pdf.unlink(missing_ok=True)
        tmp_json.unlink(missing_ok=True)
    _finalize_row(repo, doc_id, outcome.decision.outcome, str(final_pdf), str(side))
    return result


@dataclass
class PartSpec:
    """სკანის ნაწილი: ერთი MHR-ის ფაილის გვერდები (`pages` — საწყისი ფაილის ინდექსები). original_id=None — არცერთს არ ემთხვევა."""
    key: str
    title: str
    type_id: str
    pages: list[int]
    original_id: str | None
    category: str | None


def part_document_id(parent_id: str, key: str) -> str:
    """დეტერმინისტული — ავარიის შემდეგ ხელახალი დაყოფა იმავე ჩანაწერებს განაახლებს; external_id-ით ატვირთვაც იდემპოტენტური რჩება."""
    return f"doc_{uuid.uuid5(uuid.NAMESPACE_URL, f'hrsorter:{parent_id}:{key}').hex}"


def split_document(repo, cfg: Config, row, meta: dict[str, Any], specs: list[PartSpec], personal_number: str) -> list[dict[str, Any]]:
    """მთლიანი სკანი რჩება აქტიურად, სტატუსით `split`; ყოველი ნაწილი ცალკე ჩანაწერია საკუთარი PDF-ით (მთლიანი სკანის გვერდით), რომ
    შედარება, ატვირთვა, AI ანალიზი და ხელით ატვირთვა ჩვეულებრივ იმუშაოს. არცერთს რომ არ ემთხვევა — გადასამოწმებელში, MHR-ის რიგის გარეშე."""
    parent_pdf = Path(row["output_path"])
    texts = repo.load_page_texts(row["job_id"])
    parts: list[dict[str, Any]] = []
    for spec in specs:
        doc_id = part_document_id(row["id"], spec.key)
        dest = parent_pdf.with_name(f"{parent_pdf.stem}__{spec.key}.pdf")
        tmp = dest.with_name(dest.name + ".tmp")
        write_subset(parent_pdf, [p - int(row["page_start"]) for p in spec.pages], tmp)
        _replace(tmp, dest)
        matched = spec.original_id is not None
        ext = extract([PageText.from_dict(texts[p]) for p in spec.pages], cfg.doc_type(spec.type_id), cfg)
        reasons = [] if matched else ["pages_not_in_mhr"]
        status = DocumentStatus.NEEDS_REVIEW.value
        if matched:
            status = row["status"] if row["status"] == DocumentStatus.AUTO_SAVED.value else DocumentStatus.PROPOSED.value
        child_meta = dict(meta)
        child_meta.update(document_id=doc_id, document_type_id=spec.type_id, output_path=str(dest), page_count=len(spec.pages),
                          fields={k: _sidecar_field(v) for k, v in ext.fields.items()}, signature_status=ext.signature_status,
                          review_reasons=reasons, processing_status=status,
                          part={"of": row["id"], "pages": spec.pages, "original_id": spec.original_id,
                                "category": spec.category, "title": spec.title})
        repo.upsert_document({
            "id": doc_id, "job_id": row["job_id"], "source_id": row["source_id"], "page_start": min(spec.pages),
            "page_end": max(spec.pages), "status": status, "active": 1, "type_id": spec.type_id, "company_id": row["company_id"],
            "employee_id": row["employee_id"], "personal_number": personal_number, "year": row["year"],
            "document_date": row["document_date"], "review_reasons": reasons, "output_path": str(dest),
            "output_sha256": sha256_file(dest), "planned_path": str(dest), "part_of": row["id"], "metadata": child_meta})
        if matched:
            repo.mhr_enqueue(doc_id, personal_number)
        parts.append({"document_id": doc_id, "title": spec.title, "pages": [p + 1 for p in spec.pages], "matched": matched})
    _update_document(repo, row["id"], {"status": DocumentStatus.SPLIT.value, "review_reasons": []})
    return parts


MATCH_RESOLVED_REASONS = {"unknown_type", "low_type_confidence", "company_unrecognized", "uncertain_boundary"}


def adopt_match(repo, row, meta: dict[str, Any], type_id: str) -> None:
    """სკანი MHR-ის დაგენერირებულ ფაილს დაემთხვა: ტიპი ამ ფაილით დგინდება, „ტიპი/კომპანია უცნობია" მიზეზები იხსნება."""
    reasons = [r for r in json.loads(row["review_reasons"] or "[]") if r not in MATCH_RESOLVED_REASONS]
    status = row["status"]
    if status == DocumentStatus.NEEDS_REVIEW.value and not reasons:
        status = DocumentStatus.PROPOSED.value
    meta.update(document_type_id=type_id, review_reasons=reasons, processing_status=status)
    _update_document(repo, row["id"], {"type_id": type_id, "status": status, "review_reasons": reasons, "metadata": meta})
    if row["sidecar_path"] and Path(row["sidecar_path"]).exists():
        _write_json_atomic(Path(row["sidecar_path"]), meta)


def retire_previous(repo, source_id: str, keep_job_id: str, cfg: Config) -> int:
    """ხელახალი დამუშავება: იმავე წყაროს სხვა job-ების აქტიური შედეგები → `_Superseded/<doc_id>/`
    (active=0, status=superseded, superseded_by=keep_job_id). არაფერი იშლება. აბრუნებს რაოდენობას."""
    count = 0
    for row in repo.active_documents_for_source(source_id):
        if row["job_id"] == keep_job_id:
            continue
        dest = cfg.paths.superseded / row["id"]
        dest.mkdir(parents=True, exist_ok=True)
        moved: dict[str, str | None] = {"output_path": row["output_path"], "sidecar_path": row["sidecar_path"]}
        for key in moved:
            p = Path(row[key]) if row[key] else None
            if p is None:
                continue
            target = dest / p.name   # დეტერმინისტული — ავარიის შემდეგ ხელახლა გაშვებაც სწორ ბილიკს ჩაწერს
            if p.exists():
                try:
                    os.replace(p, target)   # იმავე დისკზე — ატომური, ასლის გარეშე
                except PermissionError as e:
                    raise JobError(ErrorCode.FILE_LOCKED.value, f"cannot move {p.name} (open in another program?)",
                                   retryable=True) from e
            if target.exists():
                moved[key] = str(target)
        meta = json.loads(row["metadata"]) if row["metadata"] else None
        if meta is not None:
            meta.update(processing_status=DocumentStatus.SUPERSEDED.value, superseded_by=keep_job_id,
                        output_path=moved["output_path"])
            if moved["sidecar_path"] and Path(moved["sidecar_path"]).exists():
                _write_json_atomic(Path(moved["sidecar_path"]), meta)
        _update_document(repo, row["id"], {"active": 0, "status": DocumentStatus.SUPERSEDED.value,
                                           "superseded_by": keep_job_id, "output_path": moved["output_path"],
                                           "sidecar_path": moved["sidecar_path"], "metadata": meta})
        # MHR-ის რიგში ძველი შედეგი აღარ უნდა ჩანდეს („მზადაა"/„ვერ გაიარა" სიაში ერთი სკანი ორჯერ) — ატვირთვა
        # მიმდინარეობს/დასრულდა → უცვლელი
        if repo.mhr_transition(row["id"], ("pending", "ready", "failed", "unavailable", "needs_check", "upload_failed", "split"),
                               "rejected", error="document superseded"):
            repo.log_event("mhr", f"{row['id']}: rejected (document superseded by {keep_job_id})")
        count += 1
    return count
