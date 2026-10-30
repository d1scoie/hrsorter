"""დემო/სატესტო mock MHR საქაღალდის შექმნა სინთეზური მონაცემებიდან (`hrsorter.mhr_client.MockMhr`-ისთვის).

    python -m tools.mock_mhr --out <dir> --from-ground-truth <ground_truth.json> [--tamper N]

ფორმატი — რეალური MHR HR Sorter API-ის ასლი (docs/api/hr-sorter-openapi.yaml):
* `employees.json` — Employee: რიცხვითი id (E001 → "4001"), ჩადგმული `company` {id, name, tax_id} და
  `salary` {amount, currency}, მიმდინარე ველები უახლესი ხელშეკრულებიდან, `employments` — ყოველი ხელშეკრულების პერიოდი.
* ყოველი სინთეზური ხელშეკრულებისთვის — „ორიგინალი" `.docx` `files/`-ში (zipfile-ით აწყობილი მინიმალური docx:
  [Content_Types].xml, _rels/.rels, word/document.xml, word/header1.xml გამჭოლი სათაურით) + `documents.json`
  (Document: category "contract", kind "generated", document_number null). ორიგინალი იგივე SynthDoc-იდანაა:
  ground truth-ის seed-ით დოკუმენტები ხელახლა გენერირდება (render-ის გარეშე) და `text_hash`-ით ემთხვევა.
  ვერ დაემთხვა → დეტერმინისტულად ახალი ხელშეკრულება (გაფრთხილებით).
* `--tamper N` — პირველ N ორიგინალში ხელფასი იცვლება (დასკანერებული ვერსია განსხვავდება); სია → `tampered.json`.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import random
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from unittest import mock
from xml.sax.saxutils import escape

from tools import synthetic as syn

_SALARY = re.compile(r"(შეადგენს )(\d+)( ლარს)")
DOCX_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
RUNNING_TITLE = "შრომითი ხელშეკრულება"
_COMPANY_IDS = {"acme": "1", "beta": "2"}   # MHR-ის კომპანიის id (რიცხვითი სტრიქონი)
_NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
       'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')
_XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
_RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
_WML_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml."


def _mhr_emp_id(eid: str) -> str:
    """სინთეზური E001 → MHR-ის სტილის რიცხვითი "4001"."""
    return "4" + (re.sub(r"\D", "", eid) or "0").zfill(3)


def _paras(lines: list[str]) -> str:
    return "".join(f'<w:p><w:r><w:t xml:space="preserve">{escape(t)}</w:t></w:r></w:p>' for t in lines)


def make_docx(lines: list[str], header: str | None = RUNNING_TITLE) -> bytes:
    """მინიმალური ვალიდური docx: სხეულის აბზაცები + (არჩევით) word/header1.xml გამჭოლი სათაურით."""
    overrides = f'<Override PartName="/word/document.xml" ContentType="{_WML_CT}document.main+xml"/>'
    if header:
        overrides += f'<Override PartName="/word/header1.xml" ContentType="{_WML_CT}header+xml"/>'
    sect = '<w:sectPr><w:headerReference w:type="default" r:id="rId1"/></w:sectPr>' if header else "<w:sectPr/>"
    files = {
        "[Content_Types].xml": f'{_XML}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                               '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                               f'<Default Extension="xml" ContentType="application/xml"/>{overrides}</Types>',
        "_rels/.rels": f'{_XML}<Relationships xmlns="{_RELS_NS}"><Relationship Id="rId1" '
                       f'Type="{_REL_TYPE}officeDocument" Target="word/document.xml"/></Relationships>',
        "word/document.xml": f"{_XML}<w:document {_NS}><w:body>{_paras(lines)}{sect}</w:body></w:document>",
    }
    if header:
        files["word/_rels/document.xml.rels"] = (f'{_XML}<Relationships xmlns="{_RELS_NS}"><Relationship Id="rId1" '
                                                 f'Type="{_REL_TYPE}header" Target="header1.xml"/></Relationships>')
        files["word/header1.xml"] = f"{_XML}<w:hdr {_NS}>{_paras([header])}</w:hdr>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, xml in files.items():
            zf.writestr(name, xml)
    return buf.getvalue()


def _company(cid: str | None) -> dict[str, Any] | None:
    c = syn.COMPANIES.get(cid or "")
    return {"id": _COMPANY_IDS.get(cid or "", cid), "name": c["name"], "tax_id": c["tax_id"]} if c else None


def _money(amount: str | None) -> dict[str, str] | None:
    return {"amount": f"{int(amount)}.00", "currency": "GEL"} if amount else None


def _regenerate(gt: dict[str, Any]) -> dict[str, syn.SynthDoc]:
    """make_batch/make_singles-ის ხელახალი გაშვება იგივე seed-ით, render_pdf-ის გარეშე → {text_hash: SynthDoc}."""
    captured: list[syn.SynthDoc] = []

    def capture(docs, path, **kw):
        captured.extend(docs)
        return {}

    seed = int(gt.get("seed", 1))
    with mock.patch.object(syn, "render_pdf", capture):
        if gt.get("batch"):
            syn.make_batch("unused.pdf", len(gt["batch"]["documents"]), seed)
        if gt.get("singles"):
            with tempfile.TemporaryDirectory() as tmp:
                syn.make_singles(tmp, len(gt["singles"]), seed)
    return {syn._text_hash(d): d for d in captured if d.type_id != "blank"}


def _gt_documents(gt: dict[str, Any]) -> list[dict[str, Any]]:
    out = list((gt.get("batch") or {}).get("documents", []))
    for s in gt.get("singles") or []:
        out += s.get("documents", [])
    return out


def _salary(doc: syn.SynthDoc) -> str | None:
    m = _SALARY.search(doc.text())
    return m.group(2) if m else None


def _tamper_salary(doc: syn.SynthDoc) -> tuple[str, str] | None:
    """ორიგინალში ხელფასის შეცვლა (+300). აბრუნებს (ძველი, ახალი)."""
    for page in doc.pages:
        for i, (style, text) in enumerate(page.lines):
            m = _SALARY.search(text)
            if m:
                new = str(int(m.group(2)) + 300)
                page.lines[i] = (style, _SALARY.sub(lambda x: x.group(1) + new + x.group(3), text, count=1))
                return m.group(2), new
    return None


def build(out: Path, gt: dict[str, Any], tamper: int = 0) -> dict[str, int]:
    out.mkdir(parents=True, exist_ok=True)
    (out / "files").mkdir(exist_ok=True)
    synth = _regenerate(gt)
    contracts: list[tuple[dict[str, Any], syn.SynthDoc]] = []
    seen: set[str] = set()
    regenerated = 0
    for g in _gt_documents(gt):
        if g.get("type_id") != "contract" or not g.get("employee_id") or g.get("text_hash") in seen:
            continue
        seen.add(g.get("text_hash"))
        doc = synth.get(g.get("text_hash"))
        if doc is None:   # ground truth სხვა ვერსიით შეიქმნა — დეტერმინისტული ჩანაცვლება (ტექსტი არ დაემთხვევა)
            regenerated += 1
            doc = syn.make_document("contract", random.Random(f"{g['employee_id']}:{g.get('text_hash')}"),
                                    employee=syn.employee_by_id(g["employee_id"]))
        contracts.append((g, doc))

    employees = {e["id"]: e for e in syn.employees()}
    latest: dict[str, syn.SynthDoc] = {}
    periods: dict[str, list[syn.SynthDoc]] = {}
    documents, tampered = [], []
    for n, (g, doc) in enumerate(contracts, 1):
        eid = g["employee_id"]
        periods.setdefault(eid, []).append(doc)
        if eid not in latest or (doc.fields.get("contract_start") or "") >= (latest[eid].fields.get("contract_start") or ""):
            latest[eid] = doc
        doc_id = str(9000 + n)
        original = copy.deepcopy(doc)
        if n <= tamper:
            change = _tamper_salary(original)
            if change:
                tampered.append({"doc_id": doc_id, "employee_id": _mhr_emp_id(eid), "field": "salary",
                                 "scanned_value": change[0], "original_value": change[1]})
        filename = f"{doc_id}.docx"
        data = make_docx([t for p in original.pages for style, t in p.lines if style != "gap" and t])
        (out / "files" / filename).write_bytes(data)
        date = doc.document_date or doc.fields.get("contract_start")
        documents.append({"id": doc_id, "employee_id": _mhr_emp_id(eid), "category": "contract",
                          "title": "ხელშეკრულება", "document_number": None, "document_date": date,
                          "created_at": f"{date}T09:00:00Z" if date else None, "filename": filename,
                          "content_type": DOCX_CT, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                          "kind": "generated", "signed_at": None, "signed_copy_of": None, "external_id": None})

    emp_rows = []
    for eid, e in employees.items():
        c = latest.get(eid)
        company = _company(e.get("company_id"))
        history = sorted(periods.get(eid, []), key=lambda d: d.fields.get("contract_start") or "", reverse=True)
        employments = [{"company": company, "branch": e.get("branch"), "position": d.fields.get("position"),
                        "salary": _money(_salary(d)), "start": d.fields.get("contract_start"),
                        "end": d.fields.get("contract_end"), "active": d is c} for d in history]
        emp_rows.append({"id": _mhr_emp_id(eid), "personal_number": e.get("personal_number"),
                         "first_name": e.get("first_name"), "last_name": e.get("last_name"), "company": company,
                         "branch": e.get("branch"), "position": c.fields.get("position") if c else None,
                         "salary": _money(_salary(c)) if c else None,
                         "contract_start": c.fields.get("contract_start") if c else None,
                         "contract_end": c.fields.get("contract_end") if c else None,
                         "status": e.get("status") or "active", "dismissed_at": None, "employments": employments})

    def dump(name: str, data: Any) -> None:
        (out / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    dump("employees.json", emp_rows)
    dump("documents.json", documents)
    dump("tampered.json", tampered)
    return {"employees": len(emp_rows), "contracts": len(documents), "tampered": len(tampered),
            "regenerated": regenerated}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="mock MHR საქაღალდე სინთეზური მონაცემებიდან")
    ap.add_argument("--out", required=True)
    ap.add_argument("--from-ground-truth", required=True, help="tools.synthetic-ის ground_truth.json")
    ap.add_argument("--tamper", type=int, default=0, help="რამდენ ორიგინალში შეიცვალოს ხელფასი")
    a = ap.parse_args(argv)
    gt = json.loads(Path(a.from_ground_truth).read_text(encoding="utf-8"))
    r = build(Path(a.out), gt, a.tamper)
    print(f"wrote {a.out} — employees: {r['employees']}, contracts: {r['contracts']}, tampered: {r['tampered']}")
    if r["regenerated"]:
        print(f"warning: {r['regenerated']} contract(s) not reproducible from ground truth — regenerated (text differs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
