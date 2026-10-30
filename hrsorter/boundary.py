"""დოკუმენტების საზღვრების დადგენა გვერდების ნიშნებით (ერთი გავლა, O(1) მდგომარეობა გვერდზე).

ყოველ გვერდზე i>0 ითვლება „ახალი დოკუმენტის დაწყების" ალბათობა p ყველა მტკიცებულების ჯამით
(სათაური, ტიპის ცვლილება, გვერდის ნომერი, პ/ნ, დოკ. ნომერი, ტექსტის გაგრძელება, დასასრულის ბლოკი).
p >= thresholds.boundary_start → სანდო საზღვარი; p <= boundary_continue → სანდო გაგრძელება;
შუაში → გაურკვეველი: წინა სანდო საზღვრიდან მომდევნო სანდო საზღვრამდე ერთი UNCERTAIN სეგმენტი.
ყოველი გვერდი ზუსტად ერთ სეგმენტშია (ინვარიანტი მოწმდება).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .config import Config
from .models import InputMode, PageFeatures, Segment, SegmentKind

# მტკიცებულებების წონები — კალიბრაციისთვის ერთ ადგილას. p = PRIOR + Σ წონები, შემოჭრილი [0, 1]-ში.
PRIOR = 0.20                 # ნაგულისხმევად გაგრძელება (მტკიცებულების გარეშე)
W_TITLE = 0.40               # ძლიერი სათაურის სიგნალი
W_TITLE_TYPE_DIFF = 0.20     # სათაურის ტიპი განსხვავდება მიმდინარე დოკუმენტისგან
W_TITLE_TYPE_SAME = 0.10     # იგივე ტიპის სათაური თავიდან (ახალი ეგზემპლარი)
W_PAGE_RESET = 0.35          # „გვერდი 1"
W_PAGE_CONTINUES = -0.35     # „გვერდი k>1" იმავე ჯამით / მომდევნო ნომერი
W_PAGE_TOTAL_DIFF = 0.10     # k>1, მაგრამ ჯამი განსხვავდება (სხვა დოკუმენტის შუა გვერდი)
W_PN_DIFF = 0.30             # თანამშრომლის პ/ნ განსხვავდება (იგივე პ/ნ არაა გაგრძელების მტკიცებულება)
W_NAME_DIFF = 0.10           # თანამშრომლის სახელი განსხვავდება
W_DOCNUM_DIFF = 0.15         # დოკუმენტის ნომერი შეიცვალა
W_DOCNUM_SAME = -0.10        # იგივე დოკუმენტის ნომერი
W_FLOW_BOTH = -0.30          # წინა წინადადების შუაში დასრულდა და ეს გაგრძელებით იწყება
W_FLOW_ONE = -0.10           # მხოლოდ ერთი მათგანი
W_CLOSING = 0.10             # წინა გვერდზე ხელმოწერის/დასასრულის ბლოკი (სუსტი)
W_POLICY_SPLIT = 0.25        # attachment_policy=split_independent
W_HEADER = 0.25              # გვერდი თავფურცლით იწყება (ორგანიზაცია/ს/კ/№/ქალაქი+თარიღი/მიმართვა)
W_HEADER_AFTER_CLOSING = 0.20  # თავფურცელი წინა გვერდის ხელმოწერის ბლოკის შემდეგ — ძლიერი დასაწყისი
W_COMPANY_DIFF = 0.30        # თავფურცლის კომპანია განსხვავდება მიმდინარე დოკუმენტისგან
W_RUNNING_HEADER = -0.30     # სათაური(+თარიღი) მიმდინარე დოკუმენტისას ემთხვევა — გვერდის განმეორებადი თავი
W_OPENING = 0.30             # დოკუმენტის გახსნა: „პრეამბულა" / „ერთი მხრივ … მეორე მხრივ"
W_HEADER_DATE_DIFF = 0.15    # გვერდის თავში სხვა თარიღი, ვიდრე მიმდინარე დოკუმენტის თავში
TITLE_SIMILARITY = 0.8       # OCR-ის შეცდომების მიუხედავად „იგივე სათაური"


@dataclass
class SegmentationResult:
    segments: list[Segment]
    mode_mismatch: bool
    evidence: list[str]


@dataclass
class _Decision:
    p: float
    kind: str              # start | continue | uncertain
    confidence: float      # max(p, 1-p)
    reasons: list[str]
    independent: bool      # ცალსახად დამოუკიდებელი დოკუმენტი (სხვა ტიპი/სხვა თანამშრომელი)


@dataclass
class _Open:
    """მიმდინარე ღია სეგმენტის შეჯამება — ზომა არ იზრდება გვერდების რაოდენობასთან ერთად."""
    start: int
    end: int
    kind: SegmentKind
    confidence: float
    evidence: list[str] = field(default_factory=list)
    blank_pages: list[int] = field(default_factory=list)
    title_type: str | None = None
    pns: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    doc_number: str | None = None
    company: str | None = None
    page_total: int | None = None
    last_page_number: int | None = None
    prev_ends_mid: bool = False
    prev_closing: bool = False
    title_key: str | None = None
    header_date: str | None = None

    def absorb(self, f: PageFeatures) -> None:
        self.end = f.page_index
        self.pns.update(f.personal_numbers)
        self.names.update(f.employee_names)
        if self.doc_number is None:
            self.doc_number = f.doc_number
        if self.title_type is None:
            self.title_type = f.title_type_id
        if self.company is None:
            self.company = f.header_company
        if self.title_key is None and f.title_text:
            self.title_key = _title_key(f.title_text)
        if self.header_date is None:
            self.header_date = f.header_date
        if f.page_total is not None:
            self.page_total = f.page_total
        self.last_page_number = f.page_number
        self.prev_ends_mid = f.ends_mid_sentence
        self.prev_closing = f.has_closing_block


class _Stream:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.start_th = float(cfg.thresholds["boundary_start"])
        self.cont_th = float(cfg.thresholds["boundary_continue"])
        self.mid = round((self.start_th + self.cont_th) / 2, 3)
        self.ocr_min = float(cfg.thresholds.get("ocr_min_quality", 0.0))
        self.segments: list[Segment] = []
        self.cur: _Open | None = None
        self.pending_blanks: list[int] = []
        self.independent_starts: list[str] = []
        self.unreadable_pages: list[int] = []

    def is_unreadable(self, f: PageFeatures) -> bool:
        """არაცარიელი, არა-marks გვერდი წაკითხვადი ტექსტის გარეშე (OCR არ არის / ხარისხი დაბალია)."""
        return not f.is_blank and not f.marks_only and (f.text_len == 0 or f.ocr_quality < self.ocr_min)

    # --- გვერდობრივი შეყვანა -----------------------------------------------------
    def feed(self, f: PageFeatures) -> None:
        if f.is_blank:
            self.pending_blanks.append(f.page_index)
            return
        unreadable = self.is_unreadable(f)
        if unreadable:
            self.unreadable_pages.append(f.page_index)
        if self.cur is None:
            self._flush_blanks()
            self._open(f, 1.0, ["first text page"])
            if unreadable:
                self.cur.kind = SegmentKind.UNCERTAIN
                self.cur.confidence = round(max(self.mid, 1 - self.mid), 3)
                self.cur.evidence.append(f"p{f.page_index}: unreadable page (no text / low OCR quality) → uncertain")
            return
        if f.marks_only:
            self._attach_blanks()
            self.cur.end = f.page_index
            self.cur.prev_closing = True
            self.cur.evidence.append(f"p{f.page_index}: marks-only page attached (signature/stamp)")
            return
        if unreadable:
            # წაუკითხავი გვერდი არასდროს არის „სანდო გაგრძელება" — რეგიონი გადასამოწმებელი ხდება
            d = _Decision(self.mid, "uncertain", round(max(self.mid, 1 - self.mid), 3),
                          ["unreadable page (no text / low OCR quality): cannot confirm continuation"], False)
        else:
            d = self._decide(f)
        if d.kind == "start":
            self._close(d.confidence)
            self._flush_blanks()
            self._open(f, d.confidence, d.reasons)
            if d.independent:
                self.independent_starts.append(f"p{f.page_index}: " + "; ".join(d.reasons))
            return
        self._attach_blanks()
        if d.kind == "uncertain":
            self.cur.kind = SegmentKind.UNCERTAIN
        if d.kind == "uncertain" or self.cur.kind == SegmentKind.UNCERTAIN:
            self.cur.confidence = min(self.cur.confidence, d.confidence)
        self.cur.evidence.append(f"p{f.page_index}: {d.kind} p={d.p:.2f} [" + "; ".join(d.reasons) + "]")
        self.cur.absorb(f)

    def finish(self) -> list[Segment]:
        self._close(1.0)
        self._flush_blanks()
        return self.segments

    # --- სეგმენტების მართვა ------------------------------------------------------
    def _open(self, f: PageFeatures, confidence: float, reasons: list[str]) -> None:
        self.cur = _Open(f.page_index, f.page_index, SegmentKind.DOCUMENT, confidence,
                         [f"p{f.page_index}: start [" + "; ".join(reasons) + "]"])
        self.cur.absorb(f)

    def _close(self, end_confidence: float) -> None:
        if self.cur is None:
            return
        c = self.cur
        self.segments.append(Segment(c.start, c.end, c.kind, round(min(c.confidence, end_confidence), 3),
                                     c.evidence, sorted(c.blank_pages)))
        self.cur = None

    def _attach_blanks(self) -> None:
        if self.cur is not None and self.pending_blanks:
            self.cur.blank_pages.extend(self.pending_blanks)
            self.cur.end = max(self.cur.end, self.pending_blanks[-1])
            self.pending_blanks.clear()

    def _flush_blanks(self) -> None:
        if self.pending_blanks:
            b = self.pending_blanks
            self.segments.append(Segment(b[0], b[-1], SegmentKind.BLANK, 1.0,
                                         [f"blank pages {b[0]}..{b[-1]} between documents"]))
            self.pending_blanks = []

    # --- ქულა ----------------------------------------------------------------------
    def _decide(self, f: PageFeatures) -> _Decision:
        cur = self.cur
        assert cur is not None
        p = PRIOR
        reasons: list[str] = []
        independent = False   # სხვა ტიპის სათაური ან სხვა თანამშრომელი (რეჟიმის შეუსაბამობისთვის)
        other_employee = False

        def add(w: float, why: str) -> None:
            nonlocal p
            p += w
            reasons.append(f"{why} ({w:+.2f})")

        same_title = bool(f.title_text and cur.title_key and _similar(_title_key(f.title_text), cur.title_key))
        date_diff = bool(f.header_date and cur.header_date and f.header_date != cur.header_date)
        # იგივე სათაური (და თარიღი), გახსნის გარეშე → გვერდის განმეორებადი თავი, არა ახალი დოკუმენტი
        same_type = not (f.title_type_id and cur.title_type and f.title_type_id != cur.title_type)
        other_pn = bool(f.personal_numbers and cur.pns and cur.pns.isdisjoint(f.personal_numbers))
        running = same_title and same_type and not date_diff and not f.has_opening and not other_pn
        if running:
            add(W_RUNNING_HEADER, "same title/header as current document (running page header)")
        elif f.title_text:
            add(W_TITLE, f"title {f.title_text[:40]!r}")
            if f.title_type_id and cur.title_type:
                if f.title_type_id != cur.title_type:
                    add(W_TITLE_TYPE_DIFF, f"title type {f.title_type_id} != {cur.title_type}")
                    independent = True
                else:
                    add(W_TITLE_TYPE_SAME, f"same title type {f.title_type_id} again")
        if date_diff:
            add(W_HEADER_DATE_DIFF, f"header date {f.header_date} != {cur.header_date}")
        if f.has_opening:
            add(W_OPENING, "document opening (preamble / 'ერთი მხრივ … მეორე მხრივ')")
        if f.has_header_block and not running:
            add(W_HEADER, "page starts with a document header block")
            if cur.prev_closing:
                add(W_HEADER_AFTER_CLOSING, "header block right after a closing/signature block")
            if f.header_company and cur.company and f.header_company != cur.company:
                add(W_COMPANY_DIFF, f"header company {f.header_company} != {cur.company}")
                independent = True
        if f.page_number is not None:
            if f.page_number == 1:
                add(W_PAGE_RESET, "page number reset to 1")
            elif (f.page_total is not None and cur.page_total is not None and f.page_total != cur.page_total):
                add(W_PAGE_TOTAL_DIFF, f"page {f.page_number}/{f.page_total} but current total {cur.page_total}")
            else:
                add(W_PAGE_CONTINUES, f"page number {f.page_number} continues")
        if f.personal_numbers and cur.pns:
            if cur.pns.isdisjoint(f.personal_numbers):
                add(W_PN_DIFF, "employee personal number differs")
                independent = other_employee = True
            else:
                reasons.append("same personal number (not evidence by itself)")
        if f.employee_names and cur.names and cur.names.isdisjoint(f.employee_names):
            add(W_NAME_DIFF, "employee name differs")
        if f.doc_number and cur.doc_number:
            add(W_DOCNUM_DIFF if f.doc_number != cur.doc_number else W_DOCNUM_SAME,
                f"doc number {f.doc_number} vs {cur.doc_number}")
        flow = int(cur.prev_ends_mid and not cur.prev_closing) + int(
            f.body_starts_mid if running else f.starts_with_continuation)
        if flow == 2:
            add(W_FLOW_BOTH, "sentence continues across pages")
        elif flow == 1:
            add(W_FLOW_ONE, "previous ends mid-sentence" if cur.prev_ends_mid and not cur.prev_closing
                else "starts mid-sentence")
        if cur.prev_closing:
            add(W_CLOSING, "previous page had closing/signature block")

        # დანართების პოლიტიკა — ახალი გვერდის სათაურის ტიპის მიხედვით (სპეც. §8)
        policy = None
        if f.title_type_id:
            dt = self.cfg.doc_type(f.title_type_id)
            policy = dt.attachment_policy if dt else None
        if policy == "split_independent" and f.title_text:
            add(W_POLICY_SPLIT, "attachment policy split_independent")
        p = round(min(1.0, max(0.0, p)), 3)
        # review: ურთიერთკავშირი გაურკვეველია, თუ იგივე თანამშრომელია და გვერდს საკუთარი თავფურცელი არ აქვს
        # (თავფურცლიანი დანართი „ცალსახად დამოუკიდებელი" დოკუმენტია — სპეც. §8)
        if policy == "review" and f.title_text and p >= self.start_th and not other_employee and not f.has_header_block:
            p = self.mid
            reasons.append(f"attachment policy review: relationship to previous document ambiguous → p={p}")

        kind = "start" if p >= self.start_th else "continue" if p <= self.cont_th else "uncertain"
        return _Decision(p, kind, round(max(p, 1 - p), 3), reasons, independent and kind == "start")


def _title_key(title: str) -> str:
    return "".join(ch for ch in title if ch.isalpha())


def _similar(a: str, b: str) -> bool:
    return bool(a and b) and SequenceMatcher(None, a, b).ratio() >= TITLE_SIMILARITY


# --- საჯარო API ----------------------------------------------------------------------------
def detect_segments(features: list[PageFeatures], mode: InputMode, cfg: Config) -> SegmentationResult:
    n = len(features)
    if n == 0:
        return SegmentationResult([], False, ["no pages"])
    stream = _Stream(cfg)
    for f in features:
        stream.feed(f)
    batch_segments = stream.finish()
    mismatch = bool(stream.independent_starts)

    if InputMode(mode) == InputMode.SINGLE_DOCUMENT_PDF:
        blanks = [f.page_index for f in features if f.is_blank]
        if len(blanks) == n:
            segments = [Segment(features[0].page_index, features[-1].page_index, SegmentKind.BLANK, 1.0,
                                ["all pages blank"])]
        else:
            ev = ["single_document_pdf: one document over all pages"]
            conf = 1.0
            if mismatch:
                ev.append("mode mismatch suspected: " + " | ".join(stream.independent_starts))
            first = next((f for f in features if not f.is_blank and not f.marks_only), None)
            if first is not None and not first.has_opening and (
                    (first.page_number or 0) > 1 or first.body_starts_mid or first.starts_with_continuation):
                # ფაილი დოკუმენტის შუიდან იწყება (მაგ. სკანერმა გვერდებად დაჭრა) — სრულ დოკუმენტად არ ჩაითვალოს
                conf = round(max(stream.mid, 1 - stream.mid), 3)
                ev.append(f"file seems to start mid-document (page number {first.page_number}, "
                          f"continuation={first.body_starts_mid or first.starts_with_continuation})")
            if stream.unreadable_pages:
                # რეჟიმისგან დამოუკიდებელი სიგნალი: წაუკითხავი გვერდები → სანდოობა boundary_auto-ზე დაბლა
                conf = round(max(stream.mid, 1 - stream.mid), 3)
                ev.append(f"unreadable pages (no text / low OCR quality): {stream.unreadable_pages}")
            segments = [Segment(features[0].page_index, features[-1].page_index, SegmentKind.DOCUMENT, conf, ev, blanks)]
        evidence = list(stream.independent_starts) + (
            [f"unreadable pages: {stream.unreadable_pages}"] if stream.unreadable_pages else [])
    else:
        segments = batch_segments
        mismatch = False
        evidence = [f"{s.kind.value} {s.start}-{s.end} conf={s.boundary_confidence}" for s in segments]

    check_coverage(segments, features[0].page_index, features[-1].page_index)
    return SegmentationResult(segments, mismatch, evidence)


def check_coverage(segments: list[Segment], first: int, last: int) -> None:
    """ინვარიანტი: სეგმენტები მიმდევრობითია და ყოველი გვერდი ზუსტად ერთხელ არის დაფარული."""
    expect = first
    for s in segments:
        if s.start != expect or s.end < s.start:
            raise RuntimeError(f"segment coverage broken at {s.start}-{s.end}, expected start {expect}")
        if any(b < s.start or b > s.end for b in s.blank_pages):
            raise RuntimeError(f"blank page outside its segment {s.start}-{s.end}: {s.blank_pages}")
        expect = s.end + 1
    if expect != last + 1:
        raise RuntimeError(f"segments end at {expect - 1}, expected {last}")
