"""MHR ინტეგრაციის საერთო მოდელები (კონტრაქტი მოდულებს შორის: mhr_client, validation, mhr_queue, ui).

ნაკადი: დოკუმენტი (Proposed/Sorted) → MHR-ში თანამშრომლის მოძიება პირადი ნომრით → ველების შედარება →
ხელშეკრულებისას — შედარება MHR-ის ორიგინალთან → passed ⇒ „მზადაა ატვირთვისთვის" → ოპერატორი ადასტურებს →
worker ატვირთავს MHR-ზე ამ თანამშრომლის ბარათზე.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class MhrStatus(str, Enum):
    PENDING = "pending"              # ვალიდაციას ელოდება
    UNAVAILABLE = "unavailable"      # MHR მიუწვდომელი იყო — worker მოგვიანებით ცდის
    FAILED = "failed"                # ვალიდაცია ვერ გაიარა (განსხვავებები) — ოპერატორმა უნდა ნახოს
    SKIPPED = "skipped"              # ვალიდაცია არ ეხება (მაგ. პირადი ნომერი არ არის, ტიპი არ იტვირთება)
    READY = "ready"                  # ვალიდაცია გავიდა — ოპერატორის დადასტურებას ელოდება
    APPROVED = "approved"            # ოპერატორმა დაადასტურა — worker ატვირთავს
    UPLOADING = "uploading"          # ატვირთვა დაწყებულია
    UPLOADED = "uploaded"            # ატვირთულია (remote_id)
    UPLOAD_FAILED = "upload_failed"  # ატვირთვა ვერ მოხერხდა (error)
    NEEDS_CHECK = "needs_check"      # ავარია ატვირთვისას — შეიძლება ატვირთულია; ავტომატურად აღარ ვცდით
    REJECTED = "rejected"            # ოპერატორმა უარყო


class MhrUnavailable(Exception):
    """დროებითი პრობლემა (ქსელი, timeout, 5xx, 429) — მოგვიანებით ხელახლა ცდა."""


class MhrError(Exception):
    """მუდმივი შეცდომა (4xx, არასწორი კონფიგურაცია, ცარიელი endpoint)."""


@dataclass
class MhrEmployee:
    employee_id: str
    personal_number: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    company_id: str | None = None
    position: str | None = None
    salary: str | None = None          # სტრიქონად — ფორმატი/ვალუტა API-ზეა დამოკიდებული
    contract_start: str | None = None  # ISO თარიღი, თუ API აბრუნებს
    contract_end: str | None = None
    status: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class MhrDocRef:
    doc_id: str
    category: str | None = None
    title: str | None = None
    created_at: str | None = None
    filename: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Check:
    field: str                  # ლოგიკური სახელი: first_name, last_name, personal_number, company, position, salary, contract_start …
    label_ka: str
    document_value: Any
    mhr_value: Any
    status: str                 # match | mismatch | missing_in_document | missing_in_mhr | skipped
    blocking: bool              # True — mismatch ატვირთვას აჩერებს
    note: str = ""


@dataclass
class TextDiff:
    page: int                   # დასკანერებული დოკუმენტის გვერდი (1-დან); page_count-ისთვის 0
    kind: str                   # changed | added | removed | handwriting | page_count
    original: str               # MHR-ის ორიგინალის ფრაგმენტი
    scanned: str                # დასკანერებულის ფრაგმენტი
    significant: bool           # True — შინაარსობრივი ცვლილება (ციფრები, ახალი/ამოღებული ტექსტი, ხელნაწერი); False — OCR-ის ხმაური
    reason: str = ""


@dataclass
class ValidationResult:
    status: str                 # passed | failed | unavailable | skipped
    employee: MhrEmployee | None = None
    checks: list[Check] = field(default_factory=list)
    diffs: list[TextDiff] = field(default_factory=list)
    original: MhrDocRef | None = None
    summary: list[str] = field(default_factory=list)   # ოპერატორისთვის, ქართულად
    validated_at: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def blocking_failures(self) -> list[Check]:
        return [c for c in self.checks if c.blocking and c.status == "mismatch"]

    @property
    def significant_diffs(self) -> list[TextDiff]:
        return [d for d in self.diffs if d.significant]
