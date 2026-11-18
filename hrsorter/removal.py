"""სკანის წაშლა ოპერატორის მიერ, სანამ MHR-ზე აიტვირთება, და შეცდომით წაშლილის დაბრუნება.

წაშლა ნიშნავს, რომ დოკუმენტი ყველა სიიდან ქრება და აღარ აიტვირთება:
  * MHR-ის ჩანაწერი ატომურად ხდება `deleted`, მხოლოდ ჯერ აუტვირთავი მდგომარეობიდან (`DELETABLE_MHR`). ატვირთვის რიგში
    მყოფი, უკვე ატვირთული ან გაურკვეველი ატვირთვის მქონე დოკუმენტი არ იშლება. worker `deleted`-ს არ ეხება და MHR-ის
    სიის არცერთ ჩანართში ის არ ჩანს.
  * დოკუმენტი ხდება active=0, status=deleted. სიები და დუბლიკატის შემოწმება მხოლოდ აქტიურ დოკუმენტებს უყურებს, ამიტომ
    იმავე ქაღალდის ახალი სკანი ჩვეულებრივ მოწმდება.
  * PDF და JSON ძირითადი საქაღალდიდან გადადის `_Superseded/_Deleted/<doc_id>/`-ში. ფიზიკურად არაფერი იშლება, ამ
    კომპიუტერის ასლიც (`keep`) ჩვეულებრივ ვადამდე რჩება.

`restore` ყველაფერს აბრუნებს წინა მდგომარეობაში. წინა სტატუსები და ბილიკები `metadata.deleted`-შია.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import Config
from .db import now_iso
from .mhr_models import MhrStatus as S
from .models import DocumentStatus

DELETED = DocumentStatus.DELETED.value
DELETED_DIR = "_Deleted"
# ჯერ აუტვირთავი MHR-ის მდგომარეობები. არ იშლება: approved/uploading (ატვირთვა მიდის), uploaded (უკვე MHR-შია),
# needs_check (შეიძლება აიტვირთა), split (სკანი ნაწილებადაა დაყოფილი — ნაწილები ცალ-ცალკე იშლება)
DELETABLE_MHR = (S.PENDING.value, S.UNAVAILABLE.value, S.FAILED.value, S.SKIPPED.value, S.READY.value,
                 S.UPLOAD_FAILED.value, S.REJECTED.value)
_FIXED_DOC = (DocumentStatus.PUBLISHING.value, DocumentStatus.SPLIT.value)
_FILES = ("output_path", "sidecar_path")


def refusal(doc: Any, mhr: Any) -> str | None:
    """რატომ არ შეიძლება ამ დოკუმენტის წაშლა (კოდი); None — შეიძლება."""
    if doc is None or not doc["active"]:
        return "not_found"
    if doc["status"] in _FIXED_DOC:
        return f"document_{doc['status']}"
    if mhr is not None and mhr["status"] not in DELETABLE_MHR:
        return f"mhr_{mhr['status']}"
    return None


def deleted_dir(cfg: Config, doc_id: str) -> Path:
    return cfg.paths.superseded / DELETED_DIR / doc_id


def _is_file(p: str | Path | None) -> bool:
    try:
        return bool(p) and Path(p).is_file()
    except OSError:   # მიუწვდომელი ქსელური ბილიკი
        return False


def _exists(p: str | Path) -> bool:
    try:
        return Path(p).exists()
    except OSError:
        return True   # ვერ ვამოწმებთ — თითქოს დაკავებულია (არაფერს გადავაწერთ)


def delete(repo, cfg: Config, doc_id: str, who: str = "operator") -> str | None:
    """წაშლა. აბრუნებს უარის კოდს (`refusal`, `state_changed`, `file_locked`) ან None."""
    doc, mhr = repo.get_document(doc_id), repo.mhr_get(doc_id)
    why = refusal(doc, mhr)
    if why:
        return why
    prev_mhr = mhr["status"] if mhr is not None else None
    # ჯერ MHR-ის ჩანაწერი: ამ წამიდან worker მას ვეღარ ატვირთავს, ოპერატორი — ვეღარ დაადასტურებს
    if mhr is not None and not repo.mhr_transition(doc_id, DELETABLE_MHR, S.DELETED.value):
        return "state_changed"
    dest = deleted_dir(cfg, doc_id)
    moved: dict[str, str] = {}
    try:
        for key in _FILES:
            src = doc[key]
            if not src:
                continue
            target = dest / Path(src).name
            if _is_file(src):
                dest.mkdir(parents=True, exist_ok=True)
                os.replace(src, target)
                moved[key] = str(target)
    except OSError:   # ფაილი სხვა პროგრამაშია გახსნილი ან საქაღალდე მიუწვდომელია — არაფერი იცვლება
        for key, target in moved.items():
            try:
                os.replace(target, doc[key])
            except OSError:
                pass
        if prev_mhr:
            repo.mhr_transition(doc_id, (S.DELETED.value,), prev_mhr)
        return "file_locked"
    meta = json.loads(doc["metadata"] or "{}")
    meta["deleted"] = {"at": now_iso(), "by": who, "status": doc["status"], "mhr_status": prev_mhr,
                       **{key: doc[key] for key in _FILES}}
    # ფაილი უკვე აღარ იყო (საერთო საქაღალდემ წაშალა) — ბილიკი მაინც `_Deleted`-ზე: ძველ ადგილას ახალი დოკუმენტის
    # ფაილი შეიძლება გაჩნდეს და წაშლილი დოკუმენტი მას არ უნდა „ხედავდეს“. წაკითხვა ამ კომპიუტერის ასლიდან ხდება.
    paths = {key: moved.get(key) or (str(dest / Path(doc[key]).name) if doc[key] else None) for key in _FILES}
    cur = repo.conn.execute(
        "UPDATE documents SET active=0, status=?, output_path=?, sidecar_path=?, metadata=?, updated_at=? WHERE id=? AND active=1",
        (DELETED, paths["output_path"], paths["sidecar_path"], json.dumps(meta, ensure_ascii=False), now_iso(), doc_id))
    if cur.rowcount != 1:
        return "state_changed"
    repo.log_event("document", f"{doc_id}: deleted by {who} before upload (was {doc['status']}, mhr {prev_mhr or '—'})")
    return None


def restore(repo, cfg: Config, doc_id: str, who: str = "operator") -> str | None:
    """შეცდომით წაშლილის დაბრუნება წინა მდგომარეობაში. აბრუნებს უარის კოდს (`not_deleted`, `reprocessed`,
    `state_changed`) ან None."""
    doc = repo.get_document(doc_id)
    if doc is None or doc["status"] != DELETED:
        return "not_deleted"
    if any(r["job_id"] != doc["job_id"] for r in repo.active_documents_for_source(doc["source_id"])):
        return "reprocessed"   # სკანი ამასობაში თავიდან დამუშავდა — ეს გვერდები ახალ შედეგებშია
    meta = json.loads(doc["metadata"] or "{}")
    info = meta.pop("deleted", None) or {}
    paths = {key: doc[key] for key in _FILES}
    for key in _FILES:
        orig = info.get(key)
        if orig and doc[key] and doc[key] != orig and not _exists(orig):
            if _is_file(doc[key]):
                try:
                    Path(orig).parent.mkdir(parents=True, exist_ok=True)
                    os.replace(doc[key], orig)
                    paths[key] = orig
                except OSError:
                    pass   # რჩება `_Deleted`-ში — დოკუმენტი იქიდანაც იკითხება და იტვირთება
            else:
                paths[key] = orig   # ფაილი არსად არის — ძველი ბილიკი; წაკითხვა ასლიდან
    status = info.get("status") or DocumentStatus.NEEDS_REVIEW.value
    cur = repo.conn.execute(
        "UPDATE documents SET active=1, status=?, output_path=?, sidecar_path=?, metadata=?, updated_at=? WHERE id=? AND status=?",
        (status, paths["output_path"], paths["sidecar_path"], json.dumps(meta, ensure_ascii=False), now_iso(), doc_id, DELETED))
    if cur.rowcount != 1:
        return "state_changed"
    if info.get("mhr_status"):
        repo.mhr_transition(doc_id, (S.DELETED.value,), info["mhr_status"])
    repo.log_event("document", f"{doc_id}: restored by {who} (back to {status}, mhr {info.get('mhr_status') or '—'})")
    return None
