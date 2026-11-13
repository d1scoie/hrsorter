"""დოკუმენტის ტიპის წესებზე დაფუძნებული კლასიფიკაცია.

ტიპების სიგნალები (`document_types.json`) რეგექსებია, რომლებიც მოწმდება `textnorm.normalize`-ის
ტექსტზე; `title_only=true` სიგნალები — მხოლოდ `textnorm.title_zone`-ში. სანდოობა არ არის
„მოდელის თვითშეფასება": ის ქულის, სათაურის სიგნალის არსებობისა და მეორე კანდიდატთან
სხვაობის კომბინაციაა, რომ ზღვრები რეალურ ნიმუშებზე დაკალიბრდეს.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Config, ConfigError, DocType
from .idcard import document_kind
from .models import Classification, PageText, TypeScore
from .textnorm import normalize, title_zone

# სანდოობის ფორმულის წონები — კალიბრაციისთვის ერთ ადგილას.
W_NORM = 0.5      # ტიპის დადებითი წონის რა წილი დაგროვდა
W_TITLE = 0.3     # სათაურის ზონის სიგნალი არსებობს
W_MARGIN = 0.2    # სხვაობა მეორე კანდიდატთან (ნორმალიზებული ქულებით)
BODY_PAGE_FACTOR = 0.5   # პირველის შემდეგ გვერდების სხეულის სიგნალების წონის კოეფიციენტი
WEAK_BODY_NORM = 0.7     # სათაურის გარეშე ამაზე სუსტი ნორმალიზებული ქულა → unknown
NORM_CAP = 6.0           # ნორმალიზაციის მნიშვნელის ზედა ზღვარი (ბევრსიგნალიანი ტიპი არ დაჯარიმდეს)
BODY_ONLY_RUNNER_UP = 0.5  # margin-ისთვის: სათაურის გარეშე მეორე კანდიდატის ქულა ამ კოეფიციენტით მცირდება
# სათაურის ხაზი: ≤9 სიტყვა, მძიმის გარეშე, წინადადების ნიშნით არ მთავრდება; და დამთხვევა ან ხაზის
# უმეტეს ნაწილს ფარავს, ან ხაზი ძალიან მოკლეა, ან ბრძანების სათაურია („… შესახებ").
TITLE_MAX_WORDS = 9
TITLE_SHORT_WORDS = 4
TITLE_MIN_COVERAGE = 0.5
# პირადობის დოკუმენტი (ბარათი ან პასპორტი) მანქანური ზოლით (MRZ) იცნობა, არა სათაურით: ახალ ბარათზე ქართული სათაური ხშირად არ
# იკითხება, პასპორტს კი სხვა სათაური აქვს. ზოლი საერთაშორისო სტანდარტია და სხვა დოკუმენტზე არ გვხვდება
IDENTITY_CONFIDENCE = 0.95
IDENTITY_MAX_PAGES = 2   # ბარათის ორი მხარე; უფრო გრძელ სკანში ასლი სხვა დოკუმენტს ახლავს და ტიპს არ ცვლის
UNKNOWN = "unknown"
_SENTENCE_END = re.compile(r"[.;:][\"')»]?$")
_WRAPPED_ABOUT = re.compile(r"[^ა-ჰ]*შესახებ[^ა-ჰ]*")   # გადატანილი სათაურის ბოლო სიტყვა


@dataclass(frozen=True)
class Signal:
    type_id: str
    pattern: str
    regex: re.Pattern[str]
    weight: float
    title_only: bool
    negative: bool


@dataclass
class TitleHit:
    """სათაურის ზონის ერთი დამთხვევა (features-ისთვის): რომელი ხაზი, რა წონით."""
    type_id: str
    pattern: str
    weight: float      # უარყოფითი სიგნალისთვის ნიშნით უარყოფითი
    line: str


@dataclass
class _Compiled:
    doc_type: DocType
    signals: list[Signal]
    max_positive: float


def _compile_type(t: DocType) -> _Compiled:
    signals: list[Signal] = []
    for negative, group in ((False, t.positive), (True, t.negative)):
        for s in group:
            try:
                rx = re.compile(s["pattern"])
            except (re.error, KeyError, TypeError) as e:
                raise ConfigError(f"document type {t.id}: bad signal {s!r}: {e}") from e
            signals.append(Signal(t.id, s["pattern"], rx, float(s.get("weight", 1)),
                                  bool(s.get("title_only", False)), negative))
    max_pos = sum(s.weight for s in signals if not s.negative)
    return _Compiled(t, signals, min(max(max_pos, 1.0), NORM_CAP))


def title_lines(text: str) -> list[str]:
    """სათაურის ზონის „სათაურისებური" ხაზები: მოკლე, მძიმის გარეშე, წინადადების ნიშნით დაუსრულებელი.
    ცალკე ხაზზე გადატანილი „შესახებ" წინა ხაზს უერთდება („… ურთიერთობის / შესახებ" → ერთი სათაური)."""
    merged: list[str] = []
    for ln in title_zone(text).split("\n"):
        if merged and _WRAPPED_ABOUT.fullmatch(ln):
            merged[-1] = f"{merged[-1]} შესახებ"
        else:
            merged.append(ln)
    return [ln for ln in merged
            if ln and len(ln.split()) <= TITLE_MAX_WORDS and "," not in ln and not _SENTENCE_END.search(ln)]


def title_match(regex: re.Pattern[str], line: str) -> bool:
    """title_only სიგნალი ხაზზე „სათაურად" ითვლება, თუ დამთხვევა ხაზის ≥50%-ს ფარავს (ჰარების გარეშე),
    ან ხაზი ≤4 სიტყვაა, ან ხაზი „… შესახებ"-ით მთავრდება. გადატანილი აბზაცის ხაზი ამ წესს ვერ აკმაყოფილებს."""
    m = regex.search(line)
    if not m:
        return False
    if len(line.split()) <= TITLE_SHORT_WORDS or line.endswith("შესახებ"):
        return True
    covered = sum(1 for ch in m.group(0) if not ch.isspace())
    total = sum(1 for ch in line if not ch.isspace())
    return total > 0 and covered / total >= TITLE_MIN_COVERAGE


class Classifier:
    """აქტიური ტიპების რეგექსები კომპილირდება ერთხელ; არააქტიური ტიპი არასდროს ბრუნდება."""

    def __init__(self, cfg: Config):
        self._types = [_compile_type(t) for t in cfg.active_types]
        self._unknown_below = float(cfg.thresholds.get("type_unknown_below", 0.35))
        self._identity_type = next((t.id for t in cfg.active_types if t.identity), None)

    @property
    def type_ids(self) -> list[str]:
        return [c.doc_type.id for c in self._types]

    # --- საჯარო API --------------------------------------------------------------
    def classify_text(self, text: str) -> Classification:
        """ერთი გვერდი/ტექსტი: სათაურის ზონა + სრული სხეული სრული წონით."""
        return self._classify(title_lines(text), [(normalize(text), 1.0)])

    def classify_pages(self, pages: list[PageText]) -> Classification:
        """დოკუმენტი: პირველი ტექსტური გვერდის სათაურის ზონა დომინირებს; მომდევნო გვერდები
        სხეულის სიგნალებს დაბალი წონით ამატებენ. ცარიელი/მხოლოდ-ნიშნების გვერდები იგნორირდება."""
        texts = [p.text for p in pages if not p.is_blank and not p.marks_only and (p.text or "").strip()]
        if not texts:
            return Classification(UNKNOWN, 0.0, 0.0, [], ["no text pages"])
        bodies = [(normalize(t), 1.0 if i == 0 else BODY_PAGE_FACTOR) for i, t in enumerate(texts)]
        return self._identity_document(self._classify(title_lines(texts[0]), bodies), texts)

    def _identity_document(self, c: Classification, texts: list[str]) -> Classification:
        """პირველ გვერდზე პირადობის დოკუმენტის მანქანური ზოლია (ბარათი ან პასპორტი) და წესებმა სხვა ტიპი ვერ დაადგინა →
        ტიპი პირადობაა, მაღალი სანდოობით. მხოლოდ მოკლე სკანზე: ხელშეკრულებას მიკრული ასლი ხელშეკრულებად რჩება."""
        if self._identity_type is None or c.type_id not in (UNKNOWN, self._identity_type) or len(texts) > IDENTITY_MAX_PAGES:
            return c
        kind = document_kind(texts[0])
        if kind is None:
            return c
        return Classification(self._identity_type, max(c.confidence, IDENTITY_CONFIDENCE), 1.0, c.scores,
                              [*c.evidence, f"identity document: {kind} (machine-readable zone or passport title)"])

    def title_hits(self, text: str) -> list[TitleHit]:
        """სათაურის ზონის ხაზობრივი დამთხვევები (`title_only` სიგნალები) — boundary-ის ნიშნებისთვის."""
        hits: list[TitleHit] = []
        for line in title_lines(text):
            for c in self._types:
                for s in c.signals:
                    if s.title_only and title_match(s.regex, line):
                        hits.append(TitleHit(s.type_id, s.pattern, -s.weight if s.negative else s.weight, line))
        return hits

    # --- ქულები ------------------------------------------------------------------
    def _classify(self, title: list[str], bodies: list[tuple[str, float]]) -> Classification:
        scores: list[TypeScore] = []
        detail: dict[str, tuple[float, float, float, bool]] = {}  # type_id -> (pos, neg, norm, title_hit)
        for c in self._types:
            pos = neg = 0.0
            title_hit = False
            ts = TypeScore(c.doc_type.id, 0.0)
            for s in c.signals:
                w = self._signal_weight(s, title, bodies)
                if w <= 0:
                    continue
                tag = f"{'-' if s.negative else '+'}{w:g} {s.pattern!r}{' (title)' if s.title_only else ''}"
                if s.negative:
                    neg += w
                    ts.matched_negative.append(tag)
                else:
                    pos += w
                    ts.matched_positive.append(tag)
                    title_hit = title_hit or s.title_only
            raw = pos - neg
            norm = min(1.0, max(0.0, raw / c.max_positive))
            ts.score = round(raw, 3)
            scores.append(ts)
            detail[c.doc_type.id] = (pos, neg, norm, title_hit and raw > 0)

        scores.sort(key=lambda s: (s.score, detail[s.type_id][2]), reverse=True)
        if not scores or scores[0].score <= 0:
            return Classification(UNKNOWN, 0.0, 0.0, scores, ["no positive type signals"])

        best = scores[0]
        _, neg, norm, title_hit = detail[best.type_id]
        # margin ნედლ ქულებზე: სათაურიანი გამარჯვებული მხოლოდ-სხეულის კონკურენტთან არ „თანაბრდება"
        margin = 1.0
        if len(scores) > 1 and scores[1].score > 0:
            second = scores[1].score * (BODY_ONLY_RUNNER_UP if title_hit and not detail[scores[1].type_id][3] else 1.0)
            margin = max(0.0, (best.score - second) / best.score)
        confidence = min(1.0, W_NORM * norm + W_TITLE * (1.0 if title_hit else 0.0) + W_MARGIN * margin)

        evidence = [f"{best.type_id}: {m}" for m in best.matched_positive + best.matched_negative]
        evidence.append(f"norm={norm:.2f} title={'yes' if title_hit else 'no'} margin={margin:.2f}")
        if len(scores) > 1 and scores[1].score > 0:
            evidence.append(f"runner-up {scores[1].type_id} score={scores[1].score:g}")
        type_id = best.type_id
        if confidence < self._unknown_below:
            type_id = UNKNOWN
            evidence.append(f"unknown: confidence {confidence:.2f} < {self._unknown_below}")
        elif not title_hit and norm < WEAK_BODY_NORM:
            type_id = UNKNOWN
            evidence.append("unknown: no title signal and weak body evidence")
        return Classification(type_id, round(confidence, 3), round(margin, 3), scores, evidence)

    @staticmethod
    def _signal_weight(s: Signal, title: list[str], bodies: list[tuple[str, float]]) -> float:
        """სიგნალის ეფექტური წონა: title_only — სათაურისებურ ხაზებზე; სხვა — გვერდებზე მაქსიმუმი."""
        if s.title_only:
            return s.weight if any(title_match(s.regex, ln) for ln in title) else 0.0
        return max((s.weight * f for t, f in bodies if s.regex.search(t)), default=0.0)
