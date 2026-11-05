"""საერთო მონაცემთა მოდელები — ყველა მოდული ამ ტიპებით ცვლის ინფორმაციას.

ეს ფაილი არის მოდულებს შორის კონტრაქტი. გვერდის ინდექსები ყოველთვის 0-დან
იწყება და მიუთითებს **წყარო PDF-ის** გვერდზე.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class InputMode(str, Enum):
    BATCH_PDF = "batch_pdf"
    SINGLE_DOCUMENT_PDF = "single_document_pdf"
    AUTO = "auto"   # პროფილის რეჯიმი: ფაილზე წყდება გვერდების რაოდენობით (job-ში არ ინახება)

    def resolve(self, page_count: int) -> "InputMode":
        """სამუშაოს რეალური რეჯიმი: auto → 1 გვერდი = ცალკე დოკუმენტი, მეტი = ნაკრები."""
        if self is not InputMode.AUTO:
            return self
        return InputMode.SINGLE_DOCUMENT_PDF if page_count == 1 else InputMode.BATCH_PDF


class SignatureStatus(str, Enum):
    PRESENT = "present"
    NOT_DETECTED = "not_detected"  # არ ნიშნავს დადასტურებულად ხელმოუწერელს
    UNCERTAIN = "uncertain"
    NOT_APPLICABLE = "not_applicable"


class FieldSource(str, Enum):
    DOCUMENT = "document"  # დოკუმენტიდან ამოკითხული
    API = "api"            # თანამშრომლების API-დან დამატებული
    DEFAULT = "default"    # კონფიგურაციის ნაგულისხმევი მნიშვნელობა
    DERIVED = "derived"    # სხვა ველიდან გამოთვლილი (მაგ. წელი თარიღიდან)


class ReviewReason(str, Enum):
    """გადასამოწმებლად გაგზავნის მიზეზის კოდები (ბლოკავს საბოლოო შენახვას)."""
    UNKNOWN_TYPE = "unknown_type"
    LOW_TYPE_CONFIDENCE = "low_type_confidence"
    UNCERTAIN_BOUNDARY = "uncertain_boundary"
    COMPANY_CONFLICT = "company_conflict"
    COMPANY_UNRECOGNIZED = "company_unrecognized"
    MULTIPLE_EMPLOYEES = "multiple_employees"
    POSSIBLE_DUPLICATE = "possible_duplicate"
    MODE_MISMATCH = "mode_mismatch"
    TYPE_NOT_AUTO_ENABLED = "type_not_auto_enabled"
    OCR_UNREADABLE = "ocr_unreadable"
    OTHER = "other"


class Flag(str, Enum):
    """არაბლოკირებადი შენიშვნები — მეტამონაცემებში ჩანს, შენახვას არ აფერხებს."""
    YEAR_UNKNOWN = "year_unknown"
    DATE_UNKNOWN = "date_unknown"
    SIGNATURE_UNCERTAIN = "signature_uncertain"
    SIGNATURE_NOT_DETECTED = "signature_not_detected"
    EMPLOYEE_UNVERIFIED = "employee_unverified"
    EMPLOYEE_API_UNAVAILABLE = "employee_api_unavailable"
    EMPLOYEE_NAME_CANDIDATES = "employee_name_candidates"
    PERSONAL_NUMBER_MISSING = "personal_number_missing"
    PERSONAL_NUMBER_HANDWRITTEN = "personal_number_handwritten"
    COMPANY_DEFAULT = "company_default"
    AMOUNT_MISSING = "amount_missing"
    LOW_OCR_QUALITY = "low_ocr_quality"
    API_COMPANY_DIFFERS = "api_company_differs"


class DocumentStatus(str, Enum):
    PUBLISHING = "publishing"      # ჩაწერა დაწყებულია (ავარიის შემდეგ აღსადგენი)
    AUTO_SAVED = "auto_saved"      # Sorted/-ში საბოლოოდ შენახული
    PROPOSED = "proposed"          # საცდელი რეჟიმი: Proposed/-ში შემოთავაზებული
    NEEDS_REVIEW = "needs_review"  # NeedsReview/-ში, მიზეზის კოდით
    SPLIT = "split"                # სკანში რამდენიმე MHR-ის ფაილია — მუშავდება ნაწილებად (documents.part_of)
    SUPERSEDED = "superseded"      # ხელახალი დამუშავებით ჩანაცვლებული


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"   # მართვადი შეცდომა error_code-ით
    CANCELLED = "cancelled"


class ErrorCode(str, Enum):
    PDF_CORRUPT = "pdf_corrupt"
    PDF_ENCRYPTED = "pdf_encrypted"
    PDF_EMPTY = "pdf_empty"
    FILE_LOCKED = "file_locked"
    FILE_UNSTABLE = "file_unstable"
    FILE_VANISHED = "file_vanished"
    SOURCE_CHANGED_DURING_COPY = "source_changed_during_copy"
    ARCHIVE_HASH_MISMATCH = "archive_hash_mismatch"
    DISK_FULL = "disk_full"
    OUTPUT_VERIFY_FAILED = "output_verify_failed"
    OCR_FAILED = "ocr_failed"
    MAX_ATTEMPTS = "max_attempts"
    INTERNAL = "internal"


class SegmentKind(str, Enum):
    DOCUMENT = "document"    # სანდოდ განსაზღვრული დოკუმენტი
    UNCERTAIN = "uncertain"  # გაურკვეველი საზღვრის მქონე მონაკვეთი
    BLANK = "blank"          # დადასტურებულად ცარიელი გვერდ(ებ)ი


class PageRole(str, Enum):
    """გვერდის აღრიცხვის დანიშნულება (ყოველ გვერდს ზუსტად ერთი)."""
    DOCUMENT = "document"
    UNCERTAIN = "uncertain"
    BLANK = "blank"


@dataclass
class PageText:
    """ერთი გვერდის ტექსტი და მისი ხარისხი (page analysis-ის შედეგი)."""
    page_index: int
    text: str
    text_source: str              # "text_layer" | "ocr:tesseract" | "ocr:<provider>" | "none"
    ocr_quality: float            # 0..1 — ტექსტის წაკითხვადობა (არა ტიპის სანდოობა)
    is_blank: bool                # დადასტურებულად ცარიელი (არც ტექსტი, არც მელანი)
    has_marks: bool               # არატექსტური მელანი: ხელმოწერა/ბეჭედი/ხელნაწერი
    marks_only: bool              # გვერდზე მხოლოდ ნიშნებია (ხელმოწერა/ბეჭედი/თარიღი) — ცარიელი არაა
    ink_ratio: float              # მუქი პიქსელების წილი (0..1), -1 თუ არ გაზომილა
    rotation_applied: int = 0     # 0/90/180/270
    deskew_angle: float = 0.0
    provider: str = ""
    model_version: str = ""
    signature_hint: str = SignatureStatus.UNCERTAIN.value  # გვერდის დონის ხელმოწერის შეფასება
    # ხელმოწერის ხაზის წარწერებთან („დამსაქმებელი"/„დასაქმებული") კვალი — მხარე → მოაწერა თუ არა ({} — წარწერა ვერ მოიძებნა)
    signature_parties: dict[str, bool] = field(default_factory=dict)
    handwriting_suspected: bool = False
    warnings: list[str] = field(default_factory=list)
    # ვებ-გვერდზე მოსანიშნად, გვერდის ზომის მეათასედებში: OCR სიტყვები [ტექსტი, x0, y0, x1, y1], არატექსტური ნიშნები [x0, y0, x1, y1],
    # მხარე → სად იპოვა ხელმოწერა ან სად უნდა ყოფილიყო
    words: list[list[Any]] = field(default_factory=list)
    marks: list[list[int]] = field(default_factory=list)
    signature_boxes: dict[str, list[int]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PageText":
        return cls(**d)


@dataclass
class TypeScore:
    type_id: str
    score: float
    matched_positive: list[str] = field(default_factory=list)
    matched_negative: list[str] = field(default_factory=list)


@dataclass
class Classification:
    type_id: str                  # "unknown" თუ ვერ განისაზღვრა
    confidence: float             # 0..1 — წესებზე დაფუძნებული, არა მოდელის თვითშეფასება
    margin: float                 # საუკეთესოსა და მეორეს შორის სხვაობა
    scores: list[TypeScore] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


@dataclass
class PageFeatures:
    """boundary detector-ის შესასვლელი: ერთი გვერდის ნიშნები."""
    page_index: int
    is_blank: bool
    marks_only: bool
    title_type_id: str | None          # გვერდის ზედა ნაწილში ნაპოვნი სათაურის ტიპი
    title_text: str | None
    page_type: Classification | None   # გვერდის დონის კლასიფიკაცია
    personal_numbers: list[str]        # თანამშრომლის როლში ნაპოვნი პ/ნ-ები
    employee_names: list[str]
    doc_number: str | None
    page_number: int | None            # „გვერდი 2" / „2/3" → 2
    page_total: int | None             # „2/3" → 3
    starts_with_continuation: bool     # გვერდი წინადადების შუაში იწყება
    ends_mid_sentence: bool            # გვერდი წინადადების შუაში მთავრდება
    has_closing_block: bool            # ხელმოწერის/დასასრულის ბლოკი („ხელმოწერა:", „დირექტორი")
    text_len: int
    ocr_quality: float
    has_header_block: bool = False     # ზედა ხაზები დოკუმენტის თავფურცელს ჰგავს (ორგანიზაცია, ს/კ, №, ქალაქი+თარიღი)
    header_company: str | None = None  # თავფურცელში ამოცნობილი კონფიგურირებული კომპანიის id
    header_date: str | None = None     # პირველი თარიღი გვერდის ზედა ხაზებში (განმეორებადი თავის შესადარებლად)
    has_opening: bool = False          # დოკუმენტის გახსნა: „პრეამბულა" / „ერთი მხრივ … მეორე მხრივ"
    body_starts_mid: bool = False      # სათაურის/თავის შემდეგ ტექსტი წინადადების შუაში იწყება


@dataclass
class Segment:
    """boundary detector-ის გამოსავალი: მიმდევრობითი გვერდების მონაკვეთი."""
    start: int                    # ჩათვლით
    end: int                      # ჩათვლით
    kind: SegmentKind
    boundary_confidence: float    # 0..1 — საზღვრის სანდოობა (ტიპის სანდოობისგან განცალკევებით)
    evidence: list[str] = field(default_factory=list)
    blank_pages: list[int] = field(default_factory=list)  # დოკუმენტის შიგნით ცარიელი გვერდები

    @property
    def pages(self) -> list[int]:
        return list(range(self.start, self.end + 1))


@dataclass
class FieldValue:
    value: Any = None
    raw: str | None = None              # ამოკითხული საწყისი ჩანაწერი
    source: str | None = None           # FieldSource.value
    page_index: int | None = None
    confidence: float = 0.0
    evidence: str | None = None         # დამადასტურებელი ტექსტური მონაკვეთი
    handwritten: bool = False
    candidates: list[Any] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def empty_field() -> FieldValue:
    return FieldValue()


@dataclass
class Extraction:
    """field extractor-ის გამოსავალი ერთ სეგმენტზე."""
    fields: dict[str, FieldValue] = field(default_factory=dict)
    employee_personal_numbers: list[str] = field(default_factory=list)  # ყველა განსხვავებული თანამშრომლის პ/ნ
    other_personal_numbers: list[str] = field(default_factory=list)     # დირექტორი/ხელმომწერი და სხვ.
    companies_found: list[dict[str, Any]] = field(default_factory=list)  # [{company_id, role, page_index, evidence}]
    unrecognized_companies: list[str] = field(default_factory=list)
    signature_status: str = SignatureStatus.UNCERTAIN.value
    warnings: list[str] = field(default_factory=list)


@dataclass
class EmployeeRecord:
    employee_id: str
    first_name: str | None = None
    last_name: str | None = None
    personal_number: str | None = None
    company_id: str | None = None
    branch: str | None = None
    status: str | None = None       # active / dismissed / ...
    history: list[dict[str, Any]] = field(default_factory=list)  # მხოლოდ თუ API აბრუნებს
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class EmployeeLookup:
    status: str          # verified | not_found | ambiguous | api_unavailable | not_checked
    record: EmployeeRecord | None = None
    candidates: list[EmployeeRecord] = field(default_factory=list)
    matched_by: str | None = None    # personal_number | employee_id | name
    from_cache: bool = False
    synced_at: str | None = None
    detail: str | None = None


@dataclass
class DuplicateInfo:
    status: str = "none"             # none | exact | possible
    matches: list[dict[str, Any]] = field(default_factory=list)  # [{document_id, score, reasons}]


@dataclass
class Decision:
    outcome: str                     # auto_saved | proposed | needs_review
    review_reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    explanation: list[str] = field(default_factory=list)


@dataclass
class CompanyResolution:
    company_id: str | None
    company_name: str | None
    folder: str | None
    source: str                      # document | api | default
    conflict: bool = False           # ორი წინააღმდეგობრივი კომპანია, როლები ვერ გაირჩა
    unrecognized: bool = False       # დოკუმენტში კომპანიაა, მაგრამ კონფიგურაციაში არ არის
    candidates: list[dict[str, Any]] = field(default_factory=list)
    evidence: str | None = None


@dataclass
class DocumentOutcome:
    """ერთი გამოყოფილი დოკუმენტის (ან გაურკვეველი მონაკვეთის) სრული შედეგი — sidecar-ის შესავალი."""
    document_id: str
    job_id: str
    source_id: str
    source_sha256: str
    source_path: str                 # არქივის ბილიკი (არა ინბოქსის)
    profile_id: str
    mode: str
    segment: Segment
    pages: list[int]                 # 0-დან, წყაროს გვერდები
    classification: Classification
    extraction: Extraction
    employee: EmployeeLookup
    company: CompanyResolution
    document_date: FieldValue
    year: FieldValue
    duplicate: DuplicateInfo
    decision: Decision
    signature_status: str
    content_fingerprint: str
    text_fingerprint: str
    provider: str
    model_version: str
    pipeline_version: str
    config_version: str
    attempt: int
    output_path: str | None = None
    output_sha256: str | None = None
    sidecar_path: str | None = None
    error_codes: list[str] = field(default_factory=list)
    attempt_history: list[dict[str, Any]] = field(default_factory=list)
