"""დოკუმენტის დონის დუბლიკატები.

  * ზუსტი (`exact`): იგივე გვერდების შიგთავსის ჰეში ან იგივე ნორმალიზებული ტექსტის ჰეში აქტიურ დოკუმენტებში.
  * სავარაუდო (`possible`): ტექსტის მსგავსება (სიტყვების 5-გრამების Jaccard) + ტიპის, პ/ნ-ის, ნომრისა
    და თარიღის თანხვედრა. მხოლოდ თანამშრომლისა და ტიპის დამთხვევა დუბლიკატად არ ითვლება.
დუბლიკატი არასდროს იშლება და არ ანაცვლებს არსებულ ფაილს — decision მას გადასამოწმებლად აგზავნის.
"""
from __future__ import annotations

import hashlib
import json
import re

from .config import Config
from .models import DuplicateInfo, PageText
from .textnorm import one_line

SHINGLE_SIZE = 5
MAX_SHINGLES = 400
TEXT_WEIGHT = 0.7          # ტექსტის წილი საერთო ქულაში; ატრიბუტებით მაქს. 0.3 — ტექსტის გარეშე ზღვარს ვერ აღწევს
MIN_TEXT_SIMILARITY = 0.5  # ამაზე ნაკლები ტექსტური მსგავსება — კანდიდატიც არ არის
_WORD = re.compile(r"\w+")


def content_fingerprint(page_hashes: list[str]) -> str:
    return hashlib.sha256("\n".join(page_hashes).encode("ascii")).hexdigest()


def _words(pages: list[PageText]) -> list[str]:
    return _WORD.findall(one_line(" ".join(p.text for p in pages if not p.is_blank)))


def _hash64(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big") >> 1  # SQLite/JSON-ში ეტევა


def text_fingerprint(pages: list[PageText]) -> tuple[str, list[int]]:
    """(ნორმალიზებული ტექსტის sha256, shingles). ტექსტი არ არის → ("", []) — ცარიელი ტექსტი დუბლიკატს არ ქმნის.

    shingles = სიტყვების 5-გრამების ჰეშებიდან MAX_SHINGLES ყველაზე მცირე (bottom-k ესკიზი — ორ დოკუმენტზე
    თანმიმდევრულად შერჩეული, ამიტომ Jaccard-ის შეფასება სანდოა გრძელ ტექსტზეც).
    """
    words = _words(pages)
    if not words:
        return "", []
    sha = hashlib.sha256(" ".join(words).encode("utf-8")).hexdigest()
    grams = [" ".join(words[i:i + SHINGLE_SIZE]) for i in range(max(1, len(words) - SHINGLE_SIZE + 1))]
    return sha, sorted({_hash64(g) for g in grams})[:MAX_SHINGLES]


def jaccard(a: list[int], b: list[int]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def check(repo, *, content_fp: str, text_fp: str, shingles: list[int], type_id: str | None,
          personal_number: str | None, doc_number: str | None, document_date: str | None,
          job_id: str, cfg: Config, document_id: str | None = None, source_id: str | None = None) -> DuplicateInfo:
    """გამორიცხულია: თავად ეს დოკუმენტი (ხელახალი გაშვება) და იმავე წყაროს სხვა job-ის შედეგები
    (ხელახალი დამუშავების ძველი ვერსიები). document_id-ის გარეშე — მთელი job გამოირიცხება."""
    def skip(r) -> bool:
        if document_id is None:
            return r["job_id"] == job_id
        return r["id"] == document_id or (source_id is not None and r["source_id"] == source_id
                                          and r["job_id"] != job_id)

    matches: dict[str, dict] = {}
    if content_fp:
        for r in repo.documents_by_fingerprint(content_fp):
            if not skip(r):
                matches[r["id"]] = {"document_id": r["id"], "score": 1.0, "reasons": ["content_fingerprint"]}
    if text_fp:
        for r in repo.q("SELECT id, job_id, source_id FROM documents WHERE text_fingerprint=? AND active=1",
                        (text_fp,)):
            if skip(r):
                continue
            m = matches.setdefault(r["id"], {"document_id": r["id"], "score": 1.0, "reasons": []})
            m["reasons"].append("text_fingerprint")
    if matches:
        return DuplicateInfo("exact", sorted(matches.values(), key=lambda m: m["document_id"]))

    threshold = float(cfg.thresholds["duplicate_possible"])
    mine = {"type_id": type_id, "personal_number": personal_number, "doc_number": doc_number,
            "document_date": document_date}
    possible = []
    for r in repo.candidate_duplicates(type_id=type_id, personal_number=personal_number,
                                       exclude_job=None if document_id else job_id):
        if skip(r):
            continue
        theirs = json.loads(r["text_shingles"]) if r["text_shingles"] else []
        sim = jaccard(shingles, theirs)
        if sim < MIN_TEXT_SIMILARITY:
            continue
        reasons = [f"text_similarity={sim:.2f}"]
        compared = agreed = 0
        for key, mine_v in mine.items():
            their_v = r[key]
            if mine_v and their_v:
                compared += 1
                if str(mine_v) == str(their_v):
                    agreed += 1
                    reasons.append(f"same_{key}")
                else:
                    reasons.append(f"different_{key}")
        score = TEXT_WEIGHT * sim + (1 - TEXT_WEIGHT) * (agreed / compared) if compared else sim
        if score >= threshold:
            possible.append({"document_id": r["id"], "score": round(score, 3), "reasons": reasons})
    if possible:
        return DuplicateInfo("possible", sorted(possible, key=lambda m: -m["score"]))
    return DuplicateInfo()
