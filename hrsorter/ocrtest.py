"""OCR-ის ხარისხის ტესტი ოპერატორისთვის: `hrsorter ocr-test <pdf> --pages 1,5,9 --out <საქაღალდე> [--truth <საქაღალდე>]`.

ყოველ გვერდზე იწერება page_NNN.png (სკანის სურათი) და page_NNN.txt (ამოკითხული ტექსტი) და იქმნება report.html,
სადაც სურათი და ტექსტი გვერდიგვერდ ჩანს. თუ --truth საქაღალდეში არის იმავე სახელის სწორი ტექსტი (page_NNN.txt),
ითვლება CER (ასოების შეცდომა) და WER (სიტყვების შეცდომა) და შეცდომები ფერით ინიშნება.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from .config import Config
from .ocr import build_ocr_chain
from .pageanalysis import analyze_page
from .pdfio import PdfDocument

_WS = re.compile(r"\s+")
_PN = re.compile(r"(?<!\d)\d{11}(?!\d)")


def _norm(text: str) -> str:
    return _WS.sub(" ", text).strip()


def levenshtein(a: list | str, b: list | str) -> int:
    """რედაქტირების მანძილი (ჩასმა/წაშლა/ჩანაცვლება) — ორი მწკრივით, მეხსიერება O(min(n, m))."""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


@dataclass
class PageScore:
    cer: float          # ასოების შეცდომის წილი (0 = უნაკლო)
    wer: float          # სიტყვების შეცდომის წილი
    pn_found: int       # სწორ ტექსტში არსებული პირადი ნომრებიდან რამდენი ამოიკითხა ზუსტად
    pn_total: int


def score(ocr: str, truth: str) -> PageScore:
    o, t = _norm(ocr), _norm(truth)
    cer = levenshtein(o, t) / max(1, len(t))
    ow, tw = o.split(), t.split()
    wer = levenshtein(ow, tw) / max(1, len(tw))
    tpn = set(_PN.findall(t))
    return PageScore(round(cer, 4), round(wer, 4), len(tpn & set(_PN.findall(o))), len(tpn))


def _diff_html(ocr: str, truth: str) -> str:
    """სიტყვების დონის განსხვავება: წითელი — OCR-მა არასწორად/ზედმეტად, მწვანე — გამოტოვა (სწორი ვარიანტი)."""
    ow, tw = _norm(ocr).split(), _norm(truth).split()
    out = []
    for op, i1, i2, j1, j2 in SequenceMatcher(None, ow, tw, autojunk=False).get_opcodes():
        if op == "equal":
            out.append(html.escape(" ".join(ow[i1:i2])))
            continue
        if i2 > i1:
            out.append(f'<del>{html.escape(" ".join(ow[i1:i2]))}</del>')
        if j2 > j1:
            out.append(f'<ins>{html.escape(" ".join(tw[j1:j2]))}</ins>')
    return " ".join(out)


def parse_pages(spec: str | None, n: int) -> list[int]:
    """„1,3,5-7" → [0, 2, 4, 5, 6] (1-დან დანომრილი); ცარიელი → ყველა გვერდი."""
    if not spec:
        return list(range(n))
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
            out += list(range(a - 1, b))
        elif part:
            out.append(int(part) - 1)
    return [p for p in out if 0 <= p < n]


def run(pdf: Path, pages_spec: str | None, out_dir: Path, cfg: Config, truth_dir: Path | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    chain = build_ocr_chain(cfg)
    rows, scores = [], []
    with PdfDocument(pdf) as doc:
        pages = parse_pages(pages_spec, doc.page_count)
        for i in pages:
            name = f"page_{i + 1:03d}"
            doc.render(i, 110).save(out_dir / f"{name}.png")
            pt = analyze_page(doc, i, cfg, chain)
            (out_dir / f"{name}.txt").write_text(pt.text, encoding="utf-8")
            truth_file = truth_dir / f"{name}.txt" if truth_dir else None
            sc = None
            if truth_file and truth_file.exists():
                truth = truth_file.read_text(encoding="utf-8")
                sc = score(pt.text, truth)
                scores.append(sc)
                body = f'<div class="diff">{_diff_html(pt.text, truth)}</div>'
                metric = (f'<b>CER {sc.cer * 100:.1f}%</b> · WER {sc.wer * 100:.1f}%'
                          + (f' · პირადი ნომერი {sc.pn_found}/{sc.pn_total}' if sc.pn_total else ""))
            else:
                body = f"<pre>{html.escape(pt.text)}</pre>"
                metric = "სწორი ტექსტი არ არის მითითებული — მხოლოდ თვალით შესადარებლად"
            print(f"{name}: ხარისხი {pt.ocr_quality:.2f}" + (f", CER {sc.cer * 100:.1f}%, WER {sc.wer * 100:.1f}%" if sc else ""))
            rows.append(f'<section><h2>გვერდი {i + 1} <small>ხარისხის ქულა {pt.ocr_quality:.2f} · {html.escape(pt.text_source)}</small></h2>'
                        f'<p>{metric}</p><div class="pair"><img src="{name}.png" alt="გვერდი {i + 1}">{body}</div></section>')
    total = ""
    if scores:
        cer = sum(s.cer for s in scores) / len(scores)
        wer = sum(s.wer for s in scores) / len(scores)
        pn_f, pn_t = sum(s.pn_found for s in scores), sum(s.pn_total for s in scores)
        verdict = "ძალიან კარგი" if cer < 0.02 else "კარგი" if cer < 0.05 else "საშუალო" if cer < 0.10 else "ცუდი"
        total = (f'<div class="sum">საშუალო: <b>CER {cer * 100:.1f}%</b> · WER {wer * 100:.1f}% · '
                 + (f"პირადი ნომრები {pn_f}/{pn_t} · " if pn_t else "") + f"შეფასება: <b>{verdict}</b> ({len(scores)} გვერდი)</div>")
        print(f"საშუალო: CER {cer * 100:.1f}%, WER {wer * 100:.1f}% — {verdict}")
    report = out_dir / "report.html"
    report.write_text(
        '<!doctype html><html lang="ka"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>OCR ტესტი</title><style>body{font:15px/1.55 'Segoe UI','Sylfaen',sans-serif;margin:24px;background:#F8FAFC;color:#0F172A}"
        "section{background:#fff;border:1px solid #E2E8F0;border-radius:14px;padding:16px 20px;margin-bottom:18px}"
        "h2{font-size:17px;margin:0 0 6px}small{color:#64748B;font-weight:400}.pair{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px}"
        "img{width:100%;border:1px solid #E2E8F0;border-radius:8px}pre,.diff{white-space:pre-wrap;word-break:break-word;margin:0;font-size:14px}"
        "del{background:#FEE2E2;color:#B91C1C;text-decoration:line-through}ins{background:#DCFCE7;color:#15803D;text-decoration:none}"
        ".sum{background:#EFF6FF;border-radius:12px;padding:12px 16px;margin-bottom:18px}@media(max-width:800px){.pair{grid-template-columns:1fr}}</style></head>"
        f"<body><h1>OCR ტესტი — {html.escape(pdf.name)}</h1><p>მარცხნივ სკანი, მარჯვნივ ამოკითხული ტექსტი. "
        "<del>წითელი</del> — OCR-მა არასწორად წაიკითხა, <ins>მწვანე</ins> — სწორი ვარიანტი. CER: &lt;2% ძალიან კარგი, 2–5% კარგი, &gt;10% ცუდი.</p>"
        f"{total}{''.join(rows)}</body></html>", encoding="utf-8")
    return report
