"""ერთ სკანში რამდენიმე MHR-ის ფაილი: გვერდების მიკუთვნება, დაყოფა, ცალ-ცალკე შეფასება და ატვირთვა. მონაცემები გამოგონილია."""
from __future__ import annotations

import io
import json
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter

from hrsorter import mhr_queue as q
from hrsorter.config import EXAMPLE_TYPES_PATH, load_config, make_config
from hrsorter.db import Repository
from hrsorter.housekeeping import purge_uploaded
from hrsorter.mhr_client import MockMhr
from hrsorter.mhr_models import MhrDocRef, clean_file_title
from hrsorter.models import PageText
from hrsorter.output import part_document_id, retire_previous
from hrsorter.pipeline import _goes_to_mhr
from hrsorter.ui.app import create_app
from hrsorter.validation import (PAGE_OWNER_MIN_OVERLAP, PAGE_OWNER_TIE_WORDS, _content_tokens, _Numbering, last_page_signature_diffs,
                                 plan_pages, signature_diffs)

PN = "01001000001"
LOCAL = "http://127.0.0.1:8765"
CONTRACT = ["შრომითი ხელშეკრულება\nდამსაქმებელი შპს ალფა და დასაქმებული ნინო ბერიძე პ/ნ 01001000001 თანხმდებიან შემდეგზე\n"
            "1.1 დასაქმებული მუშაობს მენეჯერის თანამდებობაზე და ასრულებს დაკისრებულ მოვალეობებს\n"
            "1.2 ხელფასი შეადგენს 1500 ლარს თვეში და გაიცემა საბანკო გადარიცხვით",
            "2.1 ხელშეკრულება ძალაშია ერთი წლის ვადით ხელმოწერის დღიდან\n"
            "2.2 მხარეები იცავენ კონფიდენციალურობას და არ ამჟღავნებენ მესამე პირებთან ინფორმაციას\n"
            "2.3 დავა წყდება მოლაპარაკებით, შეუთანხმებლობისას სასამართლოში"]
INSTRUCTION = ["თანამდებობრივი ინსტრუქცია\nმენეჯერი ემსახურება მომხმარებლებს სალაროში ყოველდღიურად\n"
               "პასუხისმგებელია საქონლის სწორად განთავსებაზე თაროებზე და ნაშთების აღრიცხვაზე",
               "ანგარიშს აბარებს ცვლის ხელმძღვანელს სამუშაო დღის ბოლოს წერილობით\n"
               "ახალი თანამშრომლების დატრენინგებაზე ზრუნავს პირველი კვირის განმავლობაში"]
RULES = ["ლიფტით სარგებლობის წესები\nლიფტში აკრძალულია ხალხის გადაყვანა ტვირთის გარეშე\n"
         "მაქსიმალური დატვირთვა ხუთასი კილოგრამია გაუმართაობა დაუყოვნებლივ ეცნობება ადმინისტრაციას"]
STRAY = ("შვებულების განცხადება\nგთხოვთ მომცეთ ანაზღაურებადი შვებულება აგვისტოს თვეში ოჯახური გარემოებების გამო\n"
         "გადაცემული მაქვს საქმეები კოლეგისთვის დროებით შესასრულებლად")
FILES = {"G1": ("მენეჯერი - ხელშეკრულება.docx", CONTRACT), "G2": ("მენეჯერი - თანამდებობრივი ინსტრუქცია.docx", INSTRUCTION),
         "G3": ("ლიფტის წესები.docx.docx", RULES)}


def make_docx(pages: list[str], header: str | None = None, logo: bool = False) -> bytes:
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    paras = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for p in pages for line in p.split("\n"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", f"<w:document {w}><w:body>{paras}</w:body></w:document>")
        if header:   # ყოველ გვერდზე დაბეჭდილი ხაზი; `logo` — მის გვერდით სურათი (ბლანკის ლოგო)
            picture = "<w:p><w:r><w:drawing/></w:r></w:p>" if logo else ""
            zf.writestr("word/header1.xml", f"<w:hdr {w}>{picture}<w:p><w:r><w:t>{header}</w:t></w:r></w:p></w:hdr>")
    return buf.getvalue()


def blank_pdf(path: Path, n: int) -> None:
    w = PdfWriter()
    for _ in range(n):
        w.add_blank_page(200, 200)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        w.write(f)


def page_count(path) -> int:
    return len(PdfReader(str(path)).pages)


@pytest.fixture
def world(tmp_path):
    d = tmp_path / "mhr"
    (d / "files").mkdir(parents=True)
    (d / "employees.json").write_text(json.dumps([{"id": "E1", "personal_number": PN, "first_name": "ნინო", "last_name": "ბერიძე",
                                                   "status": "active"}], ensure_ascii=False), encoding="utf-8")
    docs = []
    for doc_id, (name, pages) in FILES.items():
        (d / "files" / name).write_bytes(make_docx(pages))
        docs.append({"id": doc_id, "employee_id": "E1", "category": "contract", "title": "ხელშეკრულება", "filename": name,
                     "created_at": "2026-10-29T10:00:00Z", "document_date": "2026-10-29", "kind": "generated", "signed_at": None})
    (d / "documents.json").write_text(json.dumps(docs, ensure_ascii=False), encoding="utf-8")
    cfg = make_config({"paths": {"root": str(tmp_path)}, "mhr": {"enabled": True, "mode": "mock", "mock_dir": str(d), "recheck": {"enabled": False}}})
    repo = Repository(cfg.paths.db)
    yield SimpleNamespace(cfg=cfg, repo=repo, mhr=MockMhr(d), dir=d, tmp=tmp_path)
    repo.close()


SIGNED = {"signature_hint": "present", "signature_parties": {"employer": True, "employee": True}}
PACKET = [(CONTRACT[0], SIGNED), (CONTRACT[1], SIGNED),
          (INSTRUCTION[0], {"signature_hint": "not_detected"}), (INSTRUCTION[1], {"signature_hint": "present"}),
          (RULES[0], {"signature_hint": "present"})]


def add_scan(w, pages=PACKET, doc_id="doc_scan", job="job_1", type_id="contract", status="proposed", reasons=()) -> str:
    pdf = w.tmp / "Proposed" / f"{doc_id}.pdf"
    blank_pdf(pdf, len(pages))
    for i, (text, flags) in enumerate(pages):
        fl = {"text_source": "ocr:tesseract", "ocr_quality": 0.9, "is_blank": False, "has_marks": True, "marks_only": False, "ink_ratio": 0.05}
        fl.update(flags)
        w.repo.save_page_text(job, i, PageText(page_index=i, text=text, **fl).to_dict())
    sid = w.repo.insert_source(sha256=(doc_id * 8)[:64], size=1, page_count=len(pages), archive_path=str(pdf), first_path="in/x.pdf", profile_id="batch")
    w.repo.upsert_document({"id": doc_id, "job_id": job, "source_id": sid, "page_start": 0, "page_end": len(pages) - 1, "status": status,
                            "type_id": type_id, "personal_number": PN, "document_date": "2026-10-29", "review_reasons": list(reasons),
                            "output_path": str(pdf), "metadata": {"document_type_id": type_id, "fields": {}}})
    w.repo.mhr_enqueue(doc_id, PN)
    return doc_id


def sweep(w, times=2):
    for _ in range(times):
        q.sweep(w.repo, w.cfg, w.mhr)


def child(w, key, parent="doc_scan"):
    return part_document_id(parent, key)


def validation(w, doc_id):
    return json.loads(w.repo.mhr_get(doc_id)["validation"] or "{}")


# --- გვერდების მიკუთვნება ----------------------------------------------------------------------------------
class Downloads:
    def __init__(self, files: dict[str, list[str]], headers: dict[str, str] | None = None):
        self.files, self.headers, self.calls = files, headers or {}, []

    def download(self, ref):
        self.calls.append(ref.doc_id)
        return make_docx(self.files[ref.doc_id], self.headers.get(ref.doc_id))


def refs(*ids):
    return [MhrDocRef(i, "contract", "x", "2026-10-29T10:00:00Z", f"{i}.docx", raw={"kind": "generated"}) for i in ids]


def test_plan_assigns_each_page_to_its_file(world):
    client = Downloads({"G1": CONTRACT, "G2": INSTRUCTION, "G3": RULES})
    plan = plan_pages(client, world.cfg, refs("G3", "G2", "G1"), [*CONTRACT, *INSTRUCTION, *RULES])
    assert plan.owner == ["G1", "G1", "G2", "G2", "G3"] and plan.owners == ["G1", "G2", "G3"] and plan.needs_split


def test_plan_single_file_is_not_split_and_stray_page_is_left_out(world):
    client = Downloads({"G1": CONTRACT, "G2": INSTRUCTION})
    one = plan_pages(client, world.cfg, refs("G1", "G2"), list(CONTRACT))
    assert one.owner == ["G1", "G1"] and not one.needs_split
    stray = plan_pages(client, world.cfg, refs("G1", "G2"), [*CONTRACT, STRAY])
    assert stray.owner == ["G1", "G1", None] and stray.needs_split


def test_plan_keeps_bad_or_changed_page_with_its_neighbours(world):
    """ცუდად წაკითხული გვერდი იმავე ფაილის გვერდებს შორის იმ ფაილისაა; ასევე ჩამატებული ტექსტის მქონე გვერდი — სხვაობა პირდაპირ ჩანს."""
    client = Downloads({"G1": CONTRACT + INSTRUCTION})
    garbage = "qwerty asdfgh zxcvbn poiuyt lkjhgf mnbvcx wertyu sdfghj xcvbnm"
    middle = plan_pages(client, world.cfg, refs("G1"), [CONTRACT[0], garbage, CONTRACT[1]])
    assert middle.owner == ["G1", "G1", "G1"] and not middle.needs_split
    # ბოლო გვერდი: სიტყვების ნაწილი ახალია, მაგრამ ყველაზე მეტად ისევ ამ ფაილს ჰგავს
    words = CONTRACT[1].split()
    changed = " ".join(words + ["ახალი", "პირობა", "ემატება", "ამ", "ადგილას"])
    tail = plan_pages(client, world.cfg, refs("G1"), [CONTRACT[0], changed])
    assert tail.owner == ["G1", "G1"]
    # დიდი წყვეტა სხვა დოკუმენტია, მეზობლებზე არ მიეწერება
    far = plan_pages(client, world.cfg, refs("G1"), [CONTRACT[0], STRAY, STRAY, STRAY, CONTRACT[1]])
    assert far.owner == ["G1", None, None, None, "G1"]


def test_plan_text_free_pages_join_neighbours(world):
    client = Downloads({"G1": CONTRACT})
    assert plan_pages(client, world.cfg, refs("G1"), ["", CONTRACT[0], "ხელი", CONTRACT[1]]).owner == ["G1", "G1", "G1", "G1"]
    assert plan_pages(client, world.cfg, refs("G1"), ["", "ხელი"]).owner == [None, None]


def test_plan_skips_files_missing_on_server(world):
    client = Downloads({"G1": CONTRACT})
    ref = refs("G1", "G2")
    ref[1].raw["sha256"] = None    # API: ჩანაწერი არის, ფაილი — არა
    plan = plan_pages(client, world.cfg, ref, list(CONTRACT))
    assert client.calls == ["G1"] and plan.owner == ["G1", "G1"]
    assert 0.5 < PAGE_OWNER_MIN_OVERLAP < 0.79   # ნამდვილ სკანებზე სწორი ფაილი ≥0.79, სხვა ≤0.74


# --- ხელმოწერების გვერდი თავის ფაილთან რჩება ----------------------------------------------------------------
# ბოლო გვერდზე მხოლოდ მხარეთა რეკვიზიტები და ხელმოწერებია: ეს სიტყვები თანამშრომლის ყველა ფაილშია, დიდ ხელშეკრულებაში კი
# ერთი-ორი სხვაც მოიძებნება. ასეთი გვერდი ხელშეკრულებას არ უნდა მიეწეროს, თორემ ინსტრუქცია უხელმოწეროდ რჩება.
REQUISITES = ("მხარეთა რეკვიზიტები და ხელმოწერები\n"
              "დამსაქმებელი შპს ალფა ს/კ 400000001 მისამართი ქალაქი თბილისი ვაკის ქუჩა 10 ფოსტა info@alfa.example\n"
              "დასაქმებული ნინო ბერიძე პ/ნ 01001000001 მისამართი ქალაქი ბათუმი ზღვის ქუჩა 5 ფოსტა nino@alfa.example")
APPROVED = "დამტკიცებულია შპს ალფას დირექტორის გიორგი მაისურაძის მიერ"   # ინსტრუქციის header
SIGNER = "დამსაქმებლის სახელით ხელს აწერს გიორგი მაისურაძე ხელმოწერა ბეჭედი"
CONTRACT_END = f"{REQUISITES}\n{SIGNER}"
WITH_SIGN = {"G1": [*CONTRACT, CONTRACT_END], "G2": [*INSTRUCTION, REQUISITES]}


def test_plan_short_signature_page_stays_with_previous_file(world):
    """სკანზე ხელმოწერის ადგილას „ხელმოწერა ბეჭედი" იკითხება — ორივე სიტყვა მხოლოდ ხელშეკრულებაშია. ორი სიტყვის უპირატესობა
    მოკლე გვერდზე 5%-ზე მეტია, მაგრამ ფაილს არ ცვლის."""
    client = Downloads(WITH_SIGN)
    sign = f"{REQUISITES}\nხელმოწერა ბეჭედი"
    plan = plan_pages(client, world.cfg, refs("G1", "G2"), [*INSTRUCTION, sign])
    assert plan.owner == ["G2", "G2", "G2"] and not plan.needs_split
    page = _content_tokens(sign)
    share = {k: len(page & _content_tokens("\n".join(v))) / len(page) for k, v in WITH_SIGN.items()}
    assert share["G1"] - share["G2"] > 0.05 and share["G2"] >= PAGE_OWNER_MIN_OVERLAP   # ძველი წესით გვერდი ხელშეკრულებას მიეწერებოდა
    assert plan.best[2] == ("G2", share["G2"])   # „რატომ" იმ ფაილის წილს აჩვენებს, რომელშიც გვერდი დარჩა
    # შემდეგი ფაილის პირველი გვერდი ისევ თავის ფაილს მიდის, მისი ხელმოწერების გვერდიც
    packet = plan_pages(client, world.cfg, refs("G1", "G2"), [*INSTRUCTION, sign, *CONTRACT, CONTRACT_END])
    assert packet.owner == ["G2", "G2", "G2", "G1", "G1", "G1"]


def test_plan_real_lead_on_short_page_still_moves_it(world):
    """ხელშეკრულების ბოლო გვერდი ინსტრუქციის გვერდების შემდეგ: ინსტრუქციაც ხსნის სიტყვების 70%-ზე მეტს, მაგრამ ხელშეკრულება
    ბევრი სიტყვით იგებს — გვერდი მისია."""
    page, instruction = _content_tokens(CONTRACT_END), _content_tokens("\n".join(WITH_SIGN["G2"]))
    assert len(page & instruction) / len(page) >= PAGE_OWNER_MIN_OVERLAP and len(page - instruction) > PAGE_OWNER_TIE_WORDS
    plan = plan_pages(Downloads(WITH_SIGN), world.cfg, refs("G1", "G2"), [*INSTRUCTION, CONTRACT_END])
    assert plan.owner == ["G2", "G2", "G1"]
    assert plan_pages(Downloads({**WITH_SIGN, "G3": RULES}), world.cfg, refs("G1", "G2", "G3"),
                      [*INSTRUCTION, REQUISITES, *RULES]).owner == ["G2", "G2", "G2", "G3"]


def test_plan_counts_header_words_for_the_file(world):
    """ინსტრუქციის ყოველ გვერდზე მისი header იბეჭდება; ხელშეკრულებაში ამ სიტყვებიდან რამდენიმე სხვა ადგილასაა. header-ის გარეშე
    გვერდი ხელშეკრულებას უფრო ჰგავს, header-ით — თავის ფაილს, თუნდაც სკანი ამ გვერდით იწყებოდეს."""
    files = {"G1": [*CONTRACT, f"{REQUISITES}\nხელს აწერს დირექტორის მოადგილე გიორგი მაისურაძის მინდობილობით"], "G2": WITH_SIGN["G2"]}
    page = f"{APPROVED}\n{REQUISITES}"
    plain = plan_pages(Downloads(files), world.cfg, refs("G1", "G2"), [page])
    assert plain.best[0][0] == "G1"
    plan = plan_pages(Downloads(files, {"G2": APPROVED}), world.cfg, refs("G1", "G2"), [page])
    assert plan.owner == ["G2"] and plan.best[0] == ("G2", 1.0)
    whole = plan_pages(Downloads(files, {"G2": APPROVED}), world.cfg, refs("G1", "G2"), [*INSTRUCTION, page])
    assert whole.owner == ["G2", "G2", "G2"] and not whole.needs_split


def test_instruction_with_signature_page_is_one_file_and_ready(world):
    """ნამდვილი შემთხვევა: ინსტრუქციის სკანის ბოლო გვერდი ცალკე „ხელშეკრულებად" გამოიყო და ინსტრუქციაზე გაჩნდა ცრუ
    „ბოლო გვერდზე ხელმოწერა აკლია". ახლა სკანი ერთი ფაილია და მისი ბოლო გვერდის ხელმოწერები ითვლება."""
    files = world.dir / "files"
    (files / FILES["G1"][0]).write_bytes(make_docx([*CONTRACT, f"{REQUISITES}\nხელს აწერს დირექტორის მოადგილე გიორგი მაისურაძის მინდობილობით"]))
    (files / FILES["G2"][0]).write_bytes(make_docx(WITH_SIGN["G2"], APPROVED))
    unsigned = {"signature_hint": "not_detected"}
    add_scan(world, [(INSTRUCTION[0], unsigned), (INSTRUCTION[1], unsigned), (f"{APPROVED}\n{REQUISITES}", SIGNED)], type_id="job_description")
    sweep(world)
    v = validation(world, "doc_scan")
    assert world.repo.get_document("doc_scan")["status"] != "split" and not v.get("parts")
    assert world.repo.mhr_get("doc_scan")["status"] == "ready", v["summary"]
    assert v["original"]["doc_id"] == "G2" and not any("ხელმოწერა" in x for x in v["summary"])


@pytest.mark.parametrize("logo", [True, False])
def test_logo_read_as_words_does_not_stop_the_file(world, logo):
    """ინსტრუქციის ბლანკზე ლოგოა. ერთ გვერდზე Claude-მა ის სიტყვებად წაიკითხა: თუ MHR-ის ფაილის header-ში სურათია, ეს
    „ჩამატებული ტექსტი" არ არის და ფაილი მზადაა; სურათის გარეშე header-ზე იგივე სიტყვები ისევ აჩერებს."""
    (world.dir / "files" / FILES["G2"][0]).write_bytes(make_docx(WITH_SIGN["G2"], APPROVED, logo=logo))
    read = {"text_source": "ocr:online:claude_code", "signature_hint": "not_detected"}
    add_scan(world, [(f"{APPROVED}\n{INSTRUCTION[0]}", read), (f"საოჯახო\nალფამარტი\n\n{APPROVED}\n{INSTRUCTION[1]}", read),
                     (f"{APPROVED}\n{REQUISITES}", {**read, **SIGNED})], type_id="job_description")
    sweep(world)
    v = validation(world, "doc_scan")
    added = [d for d in v["diffs"] if d["significant"] and "ალფამარტი" in d["scanned"]]
    assert world.repo.mhr_get("doc_scan")["status"] == ("ready" if logo else "failed"), v["summary"]
    assert bool(added) is not logo and v["original"]["doc_id"] == "G2"


# --- დაყოფა და ცალ-ცალკე შეფასება ---------------------------------------------------------------------------
def test_scan_with_several_files_is_split_and_each_is_ready(world):
    add_scan(world)
    sweep(world)
    parent = world.repo.get_document("doc_scan")
    assert parent["status"] == "split" and world.repo.mhr_get("doc_scan")["status"] == "split"
    parts = validation(world, "doc_scan")["parts"]
    assert [(p["title"], p["pages"], p["matched"]) for p in parts] == [
        ("მენეჯერი - ხელშეკრულება", [1, 2], True), ("მენეჯერი - თანამდებობრივი ინსტრუქცია", [3, 4], True), ("ლიფტის წესები", [5], True)]
    for key, pages, type_id in (("G1", 2, "contract"), ("G2", 2, "job_description"), ("G3", 1, "contract_annex")):
        d = world.repo.get_document(child(world, key))
        assert d["part_of"] == "doc_scan" and d["type_id"] == type_id and page_count(d["output_path"]) == pages
        assert world.repo.mhr_get(d["id"])["status"] == "ready", validation(world, d["id"])["summary"]
        assert validation(world, d["id"])["original"]["doc_id"] == key


def test_one_click_uploads_each_part_and_marks_its_original_signed(world):
    add_scan(world)
    sweep(world)
    for row in world.repo.mhr_list(("ready",)):
        q.approve(world.repo, row["document_id"], "operator")
    sweep(world)
    uploads = json.loads((world.dir / "uploads.json").read_text(encoding="utf-8"))
    by_orig = {u["signed_copy_of"]: u for u in uploads}
    assert set(by_orig) == {"G1", "G2", "G3"} and len(uploads) == 3
    assert all(u["category"] == "contract" and u["employee_id"] == "E1" for u in uploads)
    assert by_orig["G2"]["title"] == "მენეჯერი - თანამდებობრივი ინსტრუქცია — ხელმოწერილი სკანი"
    assert by_orig["G3"]["title"] == "ლიფტის წესები — ხელმოწერილი სკანი"
    assert page_count(world.dir / by_orig["G1"]["path"]) == 2 and page_count(world.dir / by_orig["G3"]["path"]) == 1
    originals = json.loads((world.dir / "documents.json").read_text(encoding="utf-8"))
    assert all(o["signed_at"] for o in originals)
    sweep(world)
    assert len(json.loads((world.dir / "uploads.json").read_text(encoding="utf-8"))) == 3   # მეორედ არ იტვირთება


def test_changed_file_stops_only_itself(world):
    changed = INSTRUCTION[1].replace("პირველი კვირის", "მესამე თვის")
    pages = [p for p in PACKET]
    pages[3] = (changed, {"signature_hint": "present"})
    add_scan(world, pages)
    sweep(world)
    assert world.repo.mhr_get(child(world, "G1"))["status"] == "ready"
    assert world.repo.mhr_get(child(world, "G3"))["status"] == "ready"
    g2 = child(world, "G2")
    assert world.repo.mhr_get(g2)["status"] == "failed"
    assert any(d["significant"] and "მესამე" in d["scanned"] for d in validation(world, g2)["diffs"])


def test_signature_rules_differ_by_file(world):
    """ინსტრუქციის შუა გვერდს ხელმოწერა არ სჭირდება, ბოლოს — სჭირდება; ხელშეკრულებას — ყველა გვერდს ორივე მხარისგან."""
    pages = [p for p in PACKET]
    pages[1] = (CONTRACT[1], {"signature_hint": "present", "signature_parties": {"employer": True, "employee": False}})
    pages[3] = (INSTRUCTION[1], {"signature_hint": "not_detected", "ocr_quality": 0.9})
    add_scan(world, pages)
    sweep(world)
    g1, g2 = child(world, "G1"), child(world, "G2")
    assert world.repo.mhr_get(g1)["status"] == "failed" and world.repo.mhr_get(g2)["status"] == "failed"
    assert any("ხელმოწერა აკლია: დასაქმებული" in x for x in validation(world, g1)["summary"])
    assert any("ბოლო გვერდზე ხელმოწერა ვერ მოიძებნა" in x for x in validation(world, g2)["summary"])
    assert world.repo.mhr_get(child(world, "G3"))["status"] == "ready"


def test_last_page_signature_rule():
    assert last_page_signature_diffs([]) == []
    assert last_page_signature_diffs([{"signature_hint": "not_detected"}, {"signature_hint": "present"}]) == []
    assert last_page_signature_diffs([{"signature_hint": "present"}, {"marks_only": True}]) == []
    assert last_page_signature_diffs([{"signature_hint": "uncertain"}])[0].significant
    d = last_page_signature_diffs([{"signature_hint": "present", "signature_parties": {"employer": True, "employee": False}}])
    assert d[0].page == 1 and "დასაქმებული" in d[0].reason


def test_missing_signature_names_the_party_as_the_paper_does():
    """სტაჟირების ფაილში ასე წერია „სტაჟიორი"; „დასაქმებული" ბარათზე „დამსაქმებელს" ეშლებოდა."""
    intern = {"signature_parties": {"employer": True, "employee": False}, "words": [["სტაჟიორი", [0, 0, 9, 9], 0.9]]}
    assert signature_diffs([intern], set())[0].reason == "ხელმოწერა აკლია: სტაჟიორი"
    assert last_page_signature_diffs([intern])[0].reason == "ბოლო გვერდზე ხელმოწერა აკლია: სტაჟიორი"
    plain = {**intern, "words": [["დასაქმებული", [0, 0, 9, 9], 0.9]]}
    assert signature_diffs([plain], set())[0].reason == "ხელმოწერა აკლია: დასაქმებული"
    boss = {"signature_parties": {"employer": False, "employee": True}, "words": intern["words"]}
    assert signature_diffs([boss], set())[0].reason == "ხელმოწერა აკლია: დამსაქმებელი"


def test_pages_matching_nothing_go_to_review_without_mhr_item(world):
    add_scan(world, [*PACKET, (STRAY, {"signature_hint": "present"})])
    sweep(world)
    other = world.repo.get_document(child(world, "other"))
    assert other["status"] == "needs_review" and json.loads(other["review_reasons"]) == ["pages_not_in_mhr"]
    assert other["type_id"] == "unknown" and page_count(other["output_path"]) == 1 and world.repo.mhr_get(other["id"]) is None
    assert world.repo.mhr_get(child(world, "G1"))["status"] == "ready"
    parts = validation(world, "doc_scan")["parts"]
    assert parts[-1]["matched"] is False and parts[-1]["pages"] == [6]


def test_file_scanned_alone_with_unknown_type_gets_its_type_from_mhr(world):
    add_scan(world, [(RULES[0], {"signature_hint": "present"})], type_id="unknown", status="needs_review",
             reasons=("unknown_type", "company_unrecognized"))
    sweep(world)
    d = world.repo.get_document("doc_scan")
    assert world.repo.mhr_get("doc_scan")["status"] == "ready" and d["part_of"] is None
    assert d["type_id"] == "contract_annex" and d["status"] == "proposed" and json.loads(d["review_reasons"]) == []
    q.approve(world.repo, "doc_scan", "operator")
    sweep(world)
    up = json.loads((world.dir / "uploads.json").read_text(encoding="utf-8"))
    assert up[0]["signed_copy_of"] == "G3" and up[0]["category"] == "contract"   # ორიგინალის კატეგორია, არა annex


def test_unknown_type_without_match_stays_in_review(world):
    add_scan(world, [(STRAY, {"signature_hint": "present"})], type_id="unknown", status="needs_review", reasons=("unknown_type",))
    sweep(world)
    d = world.repo.get_document("doc_scan")
    assert world.repo.mhr_get("doc_scan")["status"] == "skipped" and d["status"] == "needs_review" and d["type_id"] == "unknown"
    assert "არ ემთხვევა" in validation(world, "doc_scan")["summary"][0]


def test_contract_type_scanned_alone_stays_one_document(world):
    add_scan(world, PACKET[:2])
    sweep(world)
    assert world.repo.mhr_get("doc_scan")["status"] == "ready" and world.repo.get_document(child(world, "G1")) is None
    assert validation(world, "doc_scan")["original"]["doc_id"] == "G1"


def test_only_the_file_named_contract_gets_every_page_rule(world):
    """კლასიფიკატორმა ინსტრუქციას „ხელშეკრულება" უწოდა: MHR-ის ფაილის სახელი წყვეტს, ხელმოწერა ბოლო გვერდზე მოწმდება."""
    add_scan(world, PACKET[2:4], type_id="contract")
    sweep(world)
    assert world.repo.mhr_get("doc_scan")["status"] == "ready" and world.repo.get_document("doc_scan")["type_id"] == "job_description"


def test_already_uploaded_copy_stops_second_upload(world):
    add_scan(world, PACKET[:2])
    sweep(world)
    q.approve(world.repo, "doc_scan", "operator")
    sweep(world)
    add_scan(world, PACKET[:2], doc_id="doc_second", job="job_2")
    sweep(world)
    assert world.repo.mhr_get("doc_second")["status"] == "failed"
    assert any("უკვე ატვირთულია" in x for x in validation(world, "doc_second")["summary"])
    assert q.approve_override(world.repo, "doc_second")   # გადაწყვეტილება ოპერატორისაა


def test_part_whose_original_vanished_warns_on_revalidation(world):
    add_scan(world)
    sweep(world)
    g2 = child(world, "G2")
    docs = [x for x in json.loads((world.dir / "documents.json").read_text(encoding="utf-8")) if x["id"] != "G2"]
    (world.dir / "documents.json").write_text(json.dumps(docs, ensure_ascii=False), encoding="utf-8")
    assert world.repo.mhr_transition(g2, ("ready",), "failed")
    assert q.revalidate(world.repo, g2)
    sweep(world, 1)
    assert world.repo.mhr_get(g2)["status"] == "failed" and any("ბარათზე აღარ არის" in x for x in validation(world, g2)["summary"])


def test_mhr_outage_during_planning_is_retried_not_split(world):
    add_scan(world)
    (world.dir / "unavailable").write_text("x")
    q.sweep(world.repo, world.cfg, world.mhr)
    assert world.repo.mhr_get("doc_scan")["status"] == "unavailable" and world.repo.get_document("doc_scan")["status"] == "proposed"


def test_split_survives_repeat_and_reprocess(world):
    add_scan(world)
    sweep(world)
    row = world.repo.get_document("doc_scan")
    # ავარიის იმიტაცია: იგივე დაყოფა მეორედ — იგივე ჩანაწერები, დუბლიკატი არ ჩნდება
    assert world.repo.mhr_transition("doc_scan", ("split",), "pending")
    world.repo.conn.execute("UPDATE documents SET status='proposed' WHERE id='doc_scan'")
    sweep(world, 1)
    assert world.repo.one("SELECT COUNT(*) n FROM documents WHERE part_of='doc_scan'")["n"] == 3
    # ხელახალი დამუშავება: მთლიანი სკანიც და ნაწილებიც გადადის _Superseded-ში
    assert retire_previous(world.repo, row["source_id"], "job_new", world.cfg) == 4
    assert world.repo.one("SELECT COUNT(*) n FROM documents WHERE active=1")["n"] == 0
    assert {r["status"] for r in world.repo.mhr_list()} == {"rejected"}


def test_purge_removes_whole_scan_only_after_all_parts(world):
    add_scan(world, [*PACKET, (STRAY, {"signature_hint": "present"})])
    sweep(world)
    for row in world.repo.mhr_list(("ready",)):
        q.approve(world.repo, row["document_id"], "operator")
    sweep(world)
    far = 4102444800.0     # 2100 წელი — ვადა ყველაზე გასულია
    parent_pdf = Path(world.repo.get_document("doc_scan")["output_path"])
    n_docs, n_src = purge_uploaded(world.repo, far, far)
    assert n_docs == 3 and n_src == 0 and parent_pdf.exists()    # გადასამოწმებელი ნაწილი მთლიან სკანს იცავს
    other = child(world, "other")
    world.repo.conn.execute("UPDATE documents SET purged_at='x' WHERE id=?", (other,))
    n_docs, n_src = purge_uploaded(world.repo, far, far)
    assert n_docs == 1 and not parent_pdf.exists() and world.repo.get_document("doc_scan")["purged_at"]


def test_review_documents_with_readable_pn_reach_mhr_queue():
    def outcome(result, reasons, pn):
        return SimpleNamespace(decision=SimpleNamespace(outcome=result, review_reasons=reasons),
                               extraction=SimpleNamespace(fields={"personal_number": SimpleNamespace(value=pn)}))
    assert _goes_to_mhr(outcome("proposed", [], None)) and _goes_to_mhr(outcome("auto_saved", [], PN))
    assert _goes_to_mhr(outcome("needs_review", ["unknown_type", "company_unrecognized"], PN))
    assert not _goes_to_mhr(outcome("needs_review", ["unknown_type"], None))              # ვისია, უცნობია
    assert not _goes_to_mhr(outcome("needs_review", ["possible_duplicate"], PN))          # დუბლიკატი ოპერატორის გადასაწყვეტია
    assert not _goes_to_mhr(outcome("needs_review", ["unknown_type", "multiple_employees"], PN))


def test_clean_file_title():
    assert clean_file_title("ლიფტის წესები.docx.docx") == "ლიფტის წესები" and clean_file_title("x.PDF") == "x"
    assert clean_file_title(None) == "" and clean_file_title("ხელშეკრულება_1") == "ხელშეკრულება_1"


def test_ai_review_of_a_part_sees_only_its_pages(world):
    from hrsorter.ai_review import review_document
    add_scan(world)
    sweep(world)
    seen = {}

    def run(args, **kw):
        import subprocess
        seen["prompt"] = kw["input"].decode("utf-8")
        out = {"subtype": "success", "is_error": False, "result": '{"verdict": "upload", "confidence": "high", "summary": "კარგია"}'}
        return subprocess.CompletedProcess(args, 0, json.dumps(out, ensure_ascii=False).encode("utf-8"), b"")
    res = review_document(world.repo, world.cfg, child(world, "G3"), runner=run)
    assert res.verdict == "upload" and "ლიფტში აკრძალულია" in seen["prompt"] and "მენეჯერი ემსახურება" not in seen["prompt"]
    assert "ლიფტის წესები" in seen["prompt"]


# --- Word-ის ნუმერაცია -----------------------------------------------------------------------------------
def test_numbering_list_starting_with_sublevel_counts_upper_level():
    """ნამდვილი ინსტრუქცია: სია „3.1"-ით იწყება „3"-ის გარეშე. Word ზედა დონეს გამოყენებულად თვლის და შემდეგი მთავარი პუნქტი „4"-ია;
    ადრე „3" გამოდიოდა და მერე ყველა ნომერი ერთით ჩამორჩებოდა („9.1" ქაღალდის „10.1"-ის ნაცვლად)."""
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    numbering = ET.fromstring(
        f'<w:numbering {ns}><w:abstractNum w:abstractNumId="1">'
        '<w:lvl w:ilvl="0"><w:start w:val="3"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>'
        '<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2."/></w:lvl>'
        '</w:abstractNum><w:num w:numId="5"><w:abstractNumId w:val="1"/></w:num></w:numbering>')
    num = _Numbering(numbering, None)

    def para(ilvl):
        return ET.fromstring(f'<w:p {ns}><w:pPr><w:numPr><w:ilvl w:val="{ilvl}"/><w:numId w:val="5"/></w:numPr></w:pPr></w:p>')
    assert [num.label(para(x)) for x in (1, 1, 0, 1, 0)] == ["3.1.", "3.2.", "4.", "4.1.", "5."]


# --- ინტერფეისი -------------------------------------------------------------------------------------------
@pytest.fixture
def ui(world, tmp_path):
    data = {"paths": {"root": str(tmp_path), "types_file": "document_types.json"},
            "mhr": {"enabled": True, "mode": "mock", "mock_dir": str(world.dir), "recheck": {"enabled": False}}}
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    shutil.copyfile(EXAMPLE_TYPES_PATH, tmp_path / "document_types.json")
    world.cfg = load_config(cfg_path)
    return TestClient(create_app(str(cfg_path)), base_url=LOCAL)


def test_pages_show_files_of_a_scan(world, ui):
    add_scan(world, [*PACKET, (STRAY, {"signature_hint": "present"})])
    sweep(world)
    g1, other = child(world, "G1"), child(world, "other")
    detail = ui.get("/mhr/doc_scan").text
    assert "ფაილები ამ სკანში" in detail and "მენეჯერი - თანამდებობრივი ინსტრუქცია" in detail and "ლიფტის წესები" in detail
    assert "სკანის გვერდები 3–4" in detail and "სკანის გვერდები 5<" in detail and 'banner tone-info' in detail
    assert f'href="/mhr/{g1}"' in detail and f'href="/doc/{other}"' in detail and "/doc/doc_scan/manual-upload" not in detail
    assert "სკანი ფაილებად დაიყო" in ui.get("/doc/doc_scan").text and "/doc/doc_scan/manual-upload" not in ui.get("/doc/doc_scan").text
    ready = ui.get("/mhr?tab=ready").text
    assert "მენეჯერი - ხელშეკრულება" in ready and "ყველას ატვირთვა (3)" in ready
    assert "დაიყო ფაილებად" in ui.get("/mhr?tab=other").text
    assert "3 ფაილი მზადაა MHR-ზე ასატვირთად" in ui.get("/").text
    part = ui.get(f"/mhr/{g1}").text
    assert "შედარება MHR-ის ორიგინალთან" in part and "მენეჯერი - ხელშეკრულება.docx" in part
    stray = ui.get(f"/doc/{other}").text
    assert "ეს გვერდები MHR-ის ფაილებს არ ემთხვევა" in stray and f"/doc/{other}/manual-upload" in stray and 'name="type_id"' in stray


def test_manual_page_lists_parts_not_the_whole_scan(world, ui):
    add_scan(world)
    sweep(world)
    page = ui.get("/manual", params={"pn": PN, "first": "ნინო", "last": "ბერიძე"}).text
    assert child(world, "G1") in page and "doc_scan" not in page


def test_manual_upload_of_a_failed_part_marks_its_original(world, ui):
    pages = [p for p in PACKET]
    pages[4] = (RULES[0].replace("ხუთასი", "ექვსასი"), {"signature_hint": "present"})
    add_scan(world, pages)
    sweep(world)
    g3 = child(world, "G3")
    assert world.repo.mhr_get(g3)["status"] == "failed"
    r = ui.post(f"/doc/{g3}/manual-upload", data={"back": f"/mhr/{g3}"}, headers={"origin": LOCAL}, follow_redirects=False)
    assert r.status_code == 303 and "ატვირთულია MHR-ზე" in ui.get(r.headers["location"]).text
    up = json.loads((world.dir / "uploads.json").read_text(encoding="utf-8"))
    assert [u["signed_copy_of"] for u in up] == ["G3"]


# --- ფაილი უკვე დევს ბარათზე ატვირთულ PDF-ში ---------------------------------------------------------------------
def add_uploaded(w, doc_id="U9", external="doc_old", signed_copy_of=None):
    uploads = json.loads((w.dir / "uploads.json").read_text(encoding="utf-8")) if (w.dir / "uploads.json").exists() else []
    blank_pdf(w.dir / "files" / f"{doc_id}.pdf", 1)
    uploads.append({"id": doc_id, "employee_id": "E1", "category": "contract", "title": "სკანი", "filename": f"{doc_id}.pdf",
                    "created_at": "2026-10-30T07:42:11Z", "kind": "uploaded", "signed_at": None, "signed_copy_of": signed_copy_of,
                    "external_id": external, "sha256": doc_id, "path": f"files/{doc_id}.pdf"})
    (w.dir / "uploads.json").write_text(json.dumps(uploads, ensure_ascii=False), encoding="utf-8")


def checks_of(w, doc_id):
    return {c["field"]: c for c in validation(w, doc_id).get("checks", [])}


@pytest.fixture
def no_memory(monkeypatch):
    monkeypatch.setattr("hrsorter.validation._uploaded_texts", {})


def test_whole_packet_uploaded_as_one_pdf_blocks_every_file_inside_it(world, monkeypatch, no_memory):
    """მთელი პაკეტი ადრე ერთ PDF-ად აიტვირთა, signed_copy_of მხოლოდ ხელშეკრულებაზე: დანარჩენები „მზადაა" არ უნდა იყოს."""
    add_uploaded(world, signed_copy_of="G1")
    reads = []

    def pdf_pages(data, cfg, dpi=None, local_only=False):
        reads.append((dpi, local_only))
        return [*CONTRACT, *INSTRUCTION, *RULES], []
    monkeypatch.setattr("hrsorter.validation._pdf_pages", pdf_pages)
    add_scan(world)
    sweep(world)
    assert world.repo.mhr_list(("ready",)) == []
    for key, pages in (("G2", "3–4"), ("G3", "5")):
        c = checks_of(world, child(world, key))["already_uploaded"]
        assert c["blocking"] and c["mhr_value"] == "U9" and f"გვერდი {pages}" in c["note"], c
    assert checks_of(world, child(world, "G1"))["already_uploaded"]["blocking"]
    assert len(reads) == 1 and reads[0] == (150, True)     # ერთი პაკეტი ერთხელ იკითხება


def test_unrelated_upload_on_the_card_does_not_block(world, monkeypatch, no_memory):
    add_uploaded(world)
    monkeypatch.setattr("hrsorter.validation._pdf_pages", lambda data, cfg, dpi=None, local_only=False: ([STRAY], []))
    add_scan(world)
    sweep(world)
    assert len(world.repo.mhr_list(("ready",))) == 3


def test_copy_uploaded_without_a_link_blocks_that_file_only(world, monkeypatch, no_memory):
    add_uploaded(world)
    monkeypatch.setattr("hrsorter.validation._pdf_pages", lambda data, cfg, dpi=None, local_only=False: (list(INSTRUCTION), []))
    add_scan(world)
    sweep(world)
    assert {r["document_id"] for r in world.repo.mhr_list(("ready",))} == {child(world, "G1"), child(world, "G3")}
    assert checks_of(world, child(world, "G2"))["already_uploaded"]["blocking"]


def test_parts_uploaded_by_this_program_are_not_read_again(world, monkeypatch, no_memory):
    add_scan(world)
    sweep(world)
    q.approve(world.repo, child(world, "G1"), "operator")
    sweep(world)
    add_scan(world, doc_id="doc_second", job="job_2")
    monkeypatch.setattr("hrsorter.validation._pdf_pages", lambda *a, **k: pytest.fail("ნაწილი ერთ ფაილს შეიცავს, წაკითხვა არ უნდა"))
    q.sweep(world.repo, world.cfg, world.mhr)


def test_unreadable_upload_is_a_note_not_a_stop(world, monkeypatch, no_memory):
    add_uploaded(world)

    def boom(*a, **k):
        raise OSError("disk")
    monkeypatch.setattr("hrsorter.validation._pdf_pages", boom)
    add_scan(world)
    sweep(world)
    assert len(world.repo.mhr_list(("ready",))) == 3
    assert any("ვერ წაიკითხა" in s for s in validation(world, child(world, "G2"))["summary"])


# --- ვებ-გვერდი: სად, რა, რატომ --------------------------------------------------------------------------------------
LINE_WORDS = [[w, 100 + 80 * i, 300, 170 + 80 * i, 320] for i, w in enumerate("პასუხისმგებელია საქონლის სწორად განთავსებაზე".split())]


def changed_packet():
    changed = INSTRUCTION[1].replace("პირველი კვირის", "მესამე თვის")
    pages = list(PACKET)
    pages[3] = (changed, {"signature_hint": "present", "words": [[w, 100, 300, 170, 320] for w in changed.split()[:40]]})
    return pages


def test_list_shows_the_real_reason_with_the_place(world, ui):
    add_scan(world, changed_packet())
    sweep(world)
    page = ui.get("/mhr?tab=failed").text
    assert "გვერდი 2" in page and "ხელმოწერილად" not in page.split("მზადაა")[-1].split("</main>")[0]


def test_failed_file_page_says_where_what_why_and_what_to_do(world, ui):
    add_scan(world, changed_packet())
    sweep(world)
    page = ui.get(f"/mhr/{child(world, 'G2')}").text
    assert "რატომ გაჩერდა" in page and "ფაილი გაჩერდა: 1 პრობლემა" in page
    assert "გვერდი 2" in page and "სკანის გვერდი 4" in page
    assert '<mark class="s">მესამე თვის</mark>' in page and '<mark class="o">პირველი კვირის</mark>' in page
    assert "რა ვქნა" in page and "ატვირთვა მაინც" in page
    assert "გვერდ-გვერდ" in page and f"/mhr/{child(world, 'G2')}/page/2" in page
    assert "თანამდებობრივი ინსტრუქცია" in page.split('<p class="sub">')[1].split("</p>")[0]


def test_missing_signature_names_the_side_and_marks_the_place(world, ui):
    zones = {"employer": [100, 700, 300, 800], "employee": [550, 700, 900, 880]}
    pages = list(PACKET)
    pages[4] = (RULES[0], {"signature_hint": "present", "signature_parties": {"employer": True, "employee": False}, "signature_boxes": zones})
    add_scan(world, pages)
    sweep(world)
    page = ui.get(f"/mhr/{child(world, 'G3')}").text
    assert "ბოლო გვერდზე ხელმოწერა აკლია: დასაქმებული" in page and "რატომ გაჩერდა" in page
    assert 'class="hl "' in page and "crop=" in page


def test_ready_file_page_lists_what_was_checked(world, ui):
    add_scan(world)
    sweep(world)
    page = ui.get(f"/mhr/{child(world, 'G1')}").text
    assert "ყველა შემოწმება გავიდა" in page and "რა შემოწმდა" in page and "რას შევადარე" in page
    assert "ტექსტი ემთხვევა" in page and "ხელმოწერები ადგილზეა" in page and "ვისი ხელია, პროგრამა ვერ ადგენს" in page
    assert "ორჯერ ატვირთვა" in page and "რატომ გაჩერდა" not in page


def test_single_page_view_and_its_limits(world, ui):
    add_scan(world, changed_packet())
    sweep(world)
    doc = child(world, "G2")
    page = ui.get(f"/mhr/{doc}/page/2").text
    assert "გვერდი 2" in page and "რა არის ამ გვერდზე" in page and "რა წაიკითხა ამ გვერდზე" in page and f"/mhr/{doc}/page/1" in page
    assert "ამ გვერდზე პრობლემა არ არის" in ui.get(f"/mhr/{doc}/page/1").text
    assert ui.get(f"/mhr/{doc}/page/9").status_code == 404 and ui.get(f"/mhr/{doc}/page/0").status_code == 404


def test_page_image_whole_cropped_and_bad_crop(world, ui):
    add_scan(world)
    sweep(world)
    doc = child(world, "G1")
    assert ui.get(f"/page/{doc}/0.png?dpi=60").headers["content-type"] == "image/png"
    assert ui.get(f"/page/{doc}/0.png?crop=100,100,900,400").status_code == 200
    assert ui.get(f"/page/{doc}/0.png?crop=x").status_code == 404 and ui.get(f"/page/{doc}/0.png?crop=900,100,100,400").status_code == 404
    assert ui.get(f"/page/{doc}/99.png").status_code == 404


def test_split_scan_page_explains_each_file_and_each_page(world, ui):
    pages = changed_packet()
    add_scan(world, [*pages, (STRAY, {"signature_hint": "not_detected"})])
    sweep(world)
    page = ui.get("/mhr/doc_scan").text
    assert "სკანის გვერდები 3–4" in page and "რომელი გვერდი რომელ ფაილს მიეკუთვნა" in page
    assert "% სიტყვა ამ ფაილშია" in page and "საჭიროა მინიმუმ 70%" in page
    assert "გვერდი 2" in page       # შეცვლილი ინსტრუქციის მიზეზი იქვე ჩანს


def test_failed_page_without_override_names_manual_upload(world, ui):
    """თანამშრომელი ვერ მოიძებნა: „ატვირთვა მაინც" აქ არ არის, ტექსტიც ხელით ატვირთვას ასახელებს."""
    v = {"status": "failed", "checks": [{"field": "salary", "label_ka": "ხელფასი", "document_value": "1", "mhr_value": "2", "status": "mismatch", "blocking": True, "note": ""}], "summary": []}
    add_scan(world, doc_id="doc_alone", job="job_9")
    world.repo.mhr_transition("doc_alone", ("pending",), "failed", validation=v)
    page = ui.get("/mhr/doc_alone").text
    assert "ხელით ატვირთვა" in page and "ატვირთვა მაინც" not in page and "დოკუმენტში წერია" in page


def test_old_saved_results_without_places_still_open(world, ui):
    v = {"status": "failed", "checks": [], "summary": ["გვერდი 1: ტექსტი შეცვლილია"], "original": {"doc_id": "G1", "filename": "x.docx"},
         "diffs": [{"page": 1, "kind": "changed", "original": "ძველი", "scanned": "ახალი", "significant": True, "reason": "სიტყვა შეცვლილია"}]}
    add_scan(world, doc_id="doc_old", job="job_8")
    world.repo.mhr_transition("doc_old", ("pending",), "failed", validation=v)
    page = ui.get("/mhr/doc_old").text
    assert "ძველი" in page and "ახალი" in page and "სიტყვა სხვანაირად წერია" in page
    assert ui.get("/mhr/doc_old/page/1").status_code == 200


def test_page_fed_twice_is_explained():
    from hrsorter.mhr_models import TextDiff, ValidationResult
    from hrsorter.validation import _scan_hints
    page = " ".join(f"სიტყვა{i}" for i in range(40))
    other = " ".join(f"ტექსტი{i}" for i in range(40))
    r = ValidationResult("failed", diffs=[TextDiff(2, "added", "", "x y z", True, "ჩამატებული ტექსტი")])
    _scan_hints(r, [{"text": page}, {"text": page}, {"text": other}], page + " " + other)
    assert "გვერდი 1 და გვერდი 2" in r.checks[0].note and "ორჯერ" in r.checks[0].note and not r.checks[0].blocking
    clean = ValidationResult("passed")
    _scan_hints(clean, [{"text": page}, {"text": page}], page)
    assert clean.checks == []     # პრობლემა არ არის — ახსნაც არ სჭირდება


def test_order_note_only_when_pages_really_are_out_of_order():
    from hrsorter.mhr_models import TextDiff, ValidationResult
    from hrsorter.validation import _scan_hints
    first = " ".join(f"პირველი{i}" for i in range(40))
    second = " ".join(f"მეორე{i}" for i in range(40))
    diff = [TextDiff(1, "changed", "ა", "ბ", True, "ციფრები განსხვავდება")]
    right = ValidationResult("failed", diffs=list(diff))
    _scan_hints(right, [{"text": first}, {"text": second.replace("მეორე5", "სხვა")}], first + " " + second)
    assert right.checks == []                                  # რიგი სწორია, სხვაობის მიზეზი სხვაა
    wrong = ValidationResult("failed", diffs=list(diff))
    _scan_hints(wrong, [{"text": second}, {"text": first}], first + " " + second)
    assert "არეული რიგით" in wrong.checks[0].note


def test_pages_in_wrong_order_are_explained(world, ui):
    add_scan(world, [PACKET[3], PACKET[2]])
    sweep(world)
    doc = "doc_scan"
    assert "არეული რიგით" in ui.get(f"/mhr/{doc}").text


def test_review_reasons_show_where_they_come_from(world, ui):
    pages = [("პირველი გვერდი პ/ნ 01001000001 ტექსტი აქ წერია", {"signature_hint": "present"}),
             ("მეორე გვერდი პ/ნ 01001000002 ტექსტი აქ წერია", {"signature_hint": "present"})]
    add_scan(world, pages, doc_id="doc_two", job="job_7", status="needs_review", reasons=["multiple_employees"])
    page = ui.get("/doc/doc_two").text
    assert "პირადი ნომერი 01001000001: გვერდი 1" in page and "პირადი ნომერი 01001000002: გვერდი 2" in page


def test_pages_that_match_no_file_say_how_close_they_were(world, ui):
    add_scan(world, [*PACKET, (STRAY, {"signature_hint": "not_detected"})])
    sweep(world)
    page = ui.get(f"/doc/{child(world, 'other')}").text
    assert "ეს გვერდები MHR-ის ფაილებს არ ემთხვევა" in page and "საჭიროა მინიმუმ 70%" in page


def test_technical_error_codes_are_explained(world, ui):
    add_scan(world, doc_id="doc_err", job="job_6")
    world.repo.mhr_transition("doc_err", ("pending",), "upload_failed", error="invalid_signed_copy_of")
    page = ui.get("/mhr/doc_err").text
    assert "ორიგინალი ამ თანამშრომლის დაგენერირებული ფაილი აღარ არის" in page and "invalid_signed_copy_of" in page


def test_removed_files_are_explained_instead_of_broken_pictures(world, ui):
    add_scan(world, changed_packet())
    sweep(world)
    doc = child(world, "G2")
    far = 4102444800
    world.repo.conn.execute("UPDATE documents SET purged_at='2099-01-01T00:00:00+00:00' WHERE id=?", (doc,))
    world.repo.conn.commit()
    page = ui.get(f"/mhr/{doc}").text
    assert "ვადის გასვლის გამო წაიშალა" in page and "რატომ გაჩერდა" in page and f"/page/{doc}/" not in page
    assert "ვადის გასვლის გამო წაიშალა" in ui.get(f"/mhr/{doc}/page/2").text
