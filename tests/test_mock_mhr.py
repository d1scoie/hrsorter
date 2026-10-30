from __future__ import annotations

import json

from pypdf import PdfReader

from hrsorter.mhr_client import MockMhr
from hrsorter.validation import docx_text
from tools import mock_mhr, synthetic


def _text(path) -> str:
    return "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)


def test_mock_mhr_from_synthetic_singles_with_tamper(tmp_path, capsys):
    singles = synthetic.make_singles(tmp_path / "scans", 9, seed=1)   # i=0 და i=8 — ხელშეკრულებები
    gt_path = tmp_path / "ground_truth.json"
    gt_path.write_text(json.dumps({"seed": 1, "batch": None, "singles": singles}, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "mhr"
    assert mock_mhr.main(["--out", str(out), "--from-ground-truth", str(gt_path), "--tamper", "1"]) == 0
    assert "not reproducible" not in capsys.readouterr().out   # ყველა ორიგინალი იგივე SynthDoc-იდანაა

    contracts = [d for s in singles for d in s["documents"] if d["type_id"] == "contract"]
    docs = json.loads((out / "documents.json").read_text(encoding="utf-8"))
    assert len(docs) == len(contracts) == 2
    for d in docs:   # რეალური API-ის ფორმა: ორიგინალი — docx, kind generated, ნომრის გარეშე
        assert d["category"] == "contract" and d["kind"] == "generated" and d["document_number"] is None
        assert d["id"].isdigit() and d["employee_id"].isdigit() and d["filename"].endswith(".docx")
        assert d["content_type"] == mock_mhr.DOCX_CT and d["document_date"]
    tampered = json.loads((out / "tampered.json").read_text(encoding="utf-8"))
    assert [t["doc_id"] for t in tampered] == [docs[0]["id"]]

    client = MockMhr(out)
    emp = client.get_employee(contracts[1]["personal_number"])
    assert emp is not None and emp.employee_id == mock_mhr._mhr_emp_id(contracts[1]["employee_id"]) and emp.salary
    assert emp.company_id in ("1", "2") and emp.raw["company"]["tax_id"] and emp.raw["employments"]
    assert any(e["active"] and e["start"] == emp.contract_start for e in emp.raw["employments"])
    ref = client.list_documents(emp.employee_id, "contract")[-1]
    data = client.download(ref)
    assert data == (out / "files" / ref.filename).read_bytes() and data[:2] == b"PK"
    body, header = docx_text(data)
    assert header == [mock_mhr.RUNNING_TITLE]
    # ხელუხლებელი ორიგინალი = დასკანერებულის ტექსტი; შეცვლილში ხელფასი განსხვავდება
    assert f"{int(float(emp.salary))} ლარს" in body
    scanned0 = _text(singles[0]["file"])
    original0, _ = docx_text((out / "files" / docs[0]["filename"]).read_bytes())
    assert f"{tampered[0]['scanned_value']} ლარს" in scanned0
    assert f"{tampered[0]['original_value']} ლარს" in original0
    assert f"{tampered[0]['scanned_value']} ლარს" not in original0
