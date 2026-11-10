"""კონფიგურაციის ჩატვირთვა, ვალიდაცია და ვერსიონირება.

ორი ფაილი:
  * config.json          — საქაღალდეები, პროფილები, ზღვრები, API და სხვ.
  * document_types.json  — დოკუმენტების ტიპები (კოდის ცვლილების გარეშე გაფართოებადი)

config_version = ორივე ფაილის კანონიკური JSON-ის sha256 (12 სიმბოლო). სამუშაო (job)
ინახავს გამოყენებულ snapshot-ს, ამიტომ პარამეტრების ცვლილება მხოლოდ ახალ სამუშაოებზე მოქმედებს.
საიდუმლოებები (API გასაღებები) აქ არ ინახება — მხოლოდ მათი სახელი Windows Credential Manager-ში.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from .models import InputMode

DEFAULT_CONFIG_ENV = "HRSORTER_CONFIG"
# შეფუთულ (PyInstaller) ვერსიაში PROJECT_DIR = ჩაშენებული რესურსები (_internal), კონფიგურაცია კი —
# მანქანის საერთო საქაღალდეში, რომ ყველა მომხმარებელმა და ფონურმა პროცესმა ერთი ფაილი გამოიყენოს.
FROZEN = bool(getattr(sys, "frozen", False))
PROJECT_DIR = Path(__file__).resolve().parent.parent
MACHINE_DATA_DIR = Path(os.environ.get("PROGRAMDATA") or r"C:\ProgramData") / "HRSorter"
DEFAULT_CONFIG_PATH = MACHINE_DATA_DIR / "config.json" if FROZEN else PROJECT_DIR / "config" / "config.json"
EXAMPLE_CONFIG_PATH = PROJECT_DIR / "config" / "config.example.json"
EXAMPLE_TYPES_PATH = PROJECT_DIR / "config" / "document_types.example.json"

# ყველა ნაგულისხმევი მნიშვნელობა ერთ ადგილას; config.json-ში მხოლოდ შეცვლილის მითითებაც საკმარისია.
DEFAULTS: dict[str, Any] = {
    "config_schema": 1,
    "paths": {
        "root": "C:/HRScan",
        "archive": "_Archive",
        "sorted": "Sorted",
        "proposed": "Proposed",
        "needs_review": "NeedsReview",
        "errors": "Errors",
        "superseded": "_Superseded",
        "work": "_Work",
        "reports": "Reports",
        "state": "_State",
        "processed": "Processed",       # დამუშავებული ინბოქსის ფაილები (შეცდომიანი — Processed/_Failed)
        "types_file": "document_types.json",
    },
    "profiles": [
        {"id": "batch", "inbox": "Inbox/Batch", "mode": "batch_pdf"},
        {"id": "single", "inbox": "Inbox/Single", "mode": "single_document_pdf"},
    ],
    "default_company": {"id": "default", "name": "ნაგულისხმევი კომპანია", "folder": "ნაგულისხმევი-კომპანია"},
    "companies": [],
    "watcher": {
        "stability_seconds": 10,
        "poll_interval_seconds": 30,
        "recheck_interval_seconds": 5,
        "max_wait_minutes": 120,
    },
    "processing": {
        "concurrency": 1,
        "dpi": 300,
        "analysis_dpi": 100,
        "max_attempts": 5,
        "retry_backoff_seconds": 60,
        "lease_seconds": 900,
        "trial_mode": True,
        "deskew": True,
        "auto_rotate": True,
    },
    "ocr": {
        "path": "local",
        "local_provider": "tesseract",
        "tesseract_cmd": "",
        "tesseract_lang": "kat+eng",
        "tessdata_dir": "",
        "online": {
            "enabled": False,
            "provider": "claude_code",      # გამოწერით შესული Claude Code; "anthropic" = API-გასაღები
            "model": "",
            "secret_name": "hrsorter/online_ocr",
            "send": "uncertain_pages_only",
            "timeout_seconds": 180,         # claude_code: გაზომილი ~35–60 წმ/გვერდი
            "max_retries": 2,
            "max_pages_per_day": 0,
        },
    },
    "thresholds": {
        "text_layer_min_quality": 0.6,
        "ocr_min_quality": 0.35,
        "type_auto": 0.80,
        "type_min_margin": 0.25,
        "type_unknown_below": 0.35,
        "boundary_start": 0.70,
        "boundary_continue": 0.30,
        "boundary_auto": 0.80,
        "blank_ink_ratio": 0.002,
        "duplicate_possible": 0.85,
    },
    "employee_api": {
        "enabled": False,
        "mode": "mock",
        "mock_file": "config/mock_employees.example.json",
        "base_url": "",
        "auth": {"method": "none", "secret_name": "hrsorter/employee_api", "header": "Authorization"},
        "endpoints": {
            "by_personal_number": "",
            "by_id": "",
            "search_by_name": "",
            "companies": "",
            "branches": "",
            "dismissed": "",
        },
        "field_map": {
            "employee_id": "id",
            "first_name": "first_name",
            "last_name": "last_name",
            "personal_number": "personal_number",
            "company_id": "company_id",
            "branch": "branch",
            "status": "status",
            "history": "",
        },
        "pagination": {"style": "none", "page_param": "page", "size_param": "page_size", "page_size": 100, "items_key": "items", "next_key": "next"},
        "timeout_seconds": 10,
        "max_retries": 3,
        "retry_backoff_seconds": 2,
        "cache_ttl_hours": 24,
    },
    "mhr": {
        "enabled": False,
        "mode": "mock",                     # mock | http
        "mock_dir": "mock_mhr",             # ფარდობითი config.json-ის საქაღალდიდან (ან პროექტიდან)
        "base_url": "",                     # მაგ. https://d.mhr.ge/api/v1/hr-sorter (ტესტი) — მხოლოდ deployment config-ში
        "auth": {"method": "bearer", "secret_name": "hrsorter/mhr", "header": "Authorization"},
        "endpoints": {                      # MHR HR Sorter API (docs/api/hr-sorter-openapi.yaml). ცარიელი = არ არის მხარდაჭერილი
            "employee_lookup": "/employees/lookup",            # POST {"personal_number"} — პ/ნ URL-ში არასდროს
            "employee_by_pn": "",                              # ძველი GET შაბლონი ({pn}) — მხოლოდ თუ employee_lookup ცარიელია
            "list_documents": "/employees/{employee_id}/documents",   # კატეგორია — query `category`
            "download_document": "/documents/{doc_id}/file",
            "upload_document": "/employees/{employee_id}/documents",
            "by_external_id": "/documents/by-external-id/{external_id}",
            "companies": "/companies",
            "contract_template": "/employees/{employee_id}/contract-template",   # ორიგინალის არქონისას
            "template_file": "/contract-templates/{template_id}/file",
        },
        "employee_map": {"employee_id": "id", "personal_number": "personal_number", "first_name": "first_name",
                         "last_name": "last_name", "company_id": "company.id", "company_tax_id": "company.tax_id",
                         "company_name": "company.name", "position": "position", "salary": "salary.amount",
                         "contract_start": "contract_start", "contract_end": "contract_end", "status": "status",
                         "employments": "employments"},
        "document_map": {"doc_id": "id", "category": "category", "title": "title", "created_at": "created_at",
                         "filename": "filename", "items_key": "items"},
        "upload": {"file_field": "file", "category_field": "category", "extra_fields": {}, "remote_id_key": "id"},
        "categories": {"contract_annex": "annex"},   # type_id → MHR-ის კატეგორია (არ არის = type_id)
        "upload_type_ids": [],              # რომელი ტიპები იტვირთება (ცარიელი = ყველა, unknown-ის გარდა)
        "contract_type_ids": ["contract"],  # რომელ ტიპებზე ხდება შედარება MHR-ის ორიგინალთან
        "template_fallback": False,         # ორიგინალი (დაგენერირებული ხელშეკრულება) არ არის → true: თანამდებობის შაბლონთან
                                            # შედარება; false (ნაგულისხმევი): „ვერ გაიარა" — ჯერ MHR-ში დააგენერირე
        "require_approval": True,           # ოპერატორის დადასტურება ატვირთვამდე
        "identity_auto_upload": True,       # პირადობა (ტიპი identity) MHR-ს ზუსტად ემთხვევა → დადასტურების გარეშე იტვირთება
        "employee_form_auto_upload": True,  # ანკეტა (ტიპი employee_form): პ/ნ, დაწყების და დაბადების თარიღი ემთხვევა → ასევე
        "recheck": {                        # წესებმა ტექსტური სხვაობა იპოვა Tesseract-ის გვერდზე → გვერდი ონლაინ OCR-ით
            "enabled": True,                # (Claude) ხელახლა იკითხება და შედარება მეორდება (validation._recheck_diffs);
            "max_pages": 6,                 # მოქმედებს მხოლოდ ocr.online.enabled-ზე. ≤ ამდენი გვერდი ერთ დოკუმენტზე
        },
        "ai_review": {                      # ვალიდაციის შემდეგ Opus-ის „მეორე აზრი“ (ai_review.py); მოქმედებს მხოლოდ
            "enabled": True,                # ocr.online.enabled + provider=claude_code რეჟიმში. მხოლოდ სთავაზობს, არ ტვირთავს
            "model": "opus",
            "timeout_seconds": 300,
            "max_chars": 60000,             # ტექსტის მაქსიმუმი პრომპტში
            "max_noise": 200,               # ამდენი „ხმაურის" სხვაობა გადაეცემა ანალიზს (ყველა, თუ ნაკლებია)
            "per_sweep": 3,                 # ერთ გავლაზე ამდენი დოკუმენტი (თითო ~0.5–2 წთ)
        },
        "signature_every_page": True,       # ხელშეკრულება: ხელმოწერა ყველა (არაცარიელ) გვერდზე, თორემ — სხვაობა
        "name_similarity": 0.85,
        "timeout_seconds": 20,
        "max_retries": 3,
        "sweep_seconds": 60,
        "retry_minutes": 10,
        "missing_recheck_minutes": 15,      # „MHR-ში აკლია“ (ფაილი/თანამშრომელი): ამ სიხშირით ეკითხება MHR-ს, გაჩნდა თუ არა;
                                            # გაჩნდა → შედარება თავიდან, დაჭერის გარეშე. 0 = გამორთულია
        # გვერდი „თანამშრომლები“: რომელი საბუთი უნდა ჰქონდეს ყველას MHR-ის ბარათზე (ტიპების id-ები, ამ რიგით ჩანს)
        "required_type_ids": ["id_card", "employee_form", "contract", "job_description"],
        "card_refresh_minutes": 60,         # MHR-ის ბარათი (ფაილების სია) ამ სიხშირით ახლდება ფონზე. 0 = გვერდი გამორთულია
    },
    "naming": {
        "template": "{date}_{type}_{name}_{personal_number}_{doc_number}_{short_id}",
        "unknown_date": "თარიღი-უცნობია",
        "unknown_year": "წელი-უცნობია",
        "unknown_name": "სახელი-უცნობია",
        "unknown_personal_number": "პნ-უცნობია",
        "max_component_len": 40,
        "max_filename_len": 120,
        "max_path_len": 240,
    },
    "ui": {"host": "127.0.0.1", "port": 8765},
    "autostart": {"enabled": True},
    "cleanup": {
        "move_processed": True,     # დასრულებული ფაილი ინბოქსიდან → Processed (შეცდომიანი → Processed/_Failed)
        "processed_days": 30,       # Processed-ის ფაილის წაშლა N დღის შემდეგ (ორიგინალი არქივში რჩება); 0 = არასდროს
        "uploaded_days": 0,         # არქივის ორიგინალისა და შედეგის PDF-ის წაშლა MHR-ში ატვირთვიდან N დღის შემდეგ; 0 = არასდროს
    },
    "tray": {"notifications": True},   # Windows-ის შეტყობინებები: ახალი გადასამოწმებელი, შეცდომა, worker გაჩერდა
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class ConfigError(ValueError):
    pass


def on_local_disk(path: Path) -> bool:
    """ფიქსირებული ადგილობრივი დისკია? ქსელური (Z:, \\\\server\\share), მოსახსნელი ან ჯერ მიუერთებელი — არა."""
    if sys.platform != "win32" or not path.anchor:
        return True
    import ctypes
    return ctypes.windll.kernel32.GetDriveTypeW(path.anchor) == 3   # DRIVE_FIXED


@dataclass
class Profile:
    id: str
    inbox: Path
    mode: InputMode
    default_company: dict[str, Any] | None = None


@dataclass
class Company:
    id: str
    name: str
    folder: str
    aliases: list[str] = field(default_factory=list)
    tax_id: str | None = None


@dataclass
class DocType:
    id: str
    name_ka: str
    folder: str
    description: str = ""
    positive: list[dict[str, Any]] = field(default_factory=list)  # [{pattern, weight, title_only}]
    negative: list[dict[str, Any]] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)
    active: bool = True
    auto_save_enabled: bool = False   # ჩაირთოს მხოლოდ გამოცდის შემდეგ
    attachment_policy: str = "keep_continuation"  # keep_continuation | split_independent | review
    signature_expected: bool = True
    # MHR-ში თანამშრომლის დაგენერირებული ფაილი სავალდებულოა (ხელშეკრულების მსგავსად): არ არის → „დააგენერირე“
    mhr_original: bool = False
    # პირადობის დამადასტურებელი დოკუმენტი: სახელი/გვარი/პ/ნ ზუსტად უდარდება MHR-ს; ზუსტი დამთხვევა → ავტომატური ატვირთვა
    # (mhr.identity_auto_upload) და ამ თანამშრომლის სხვა დოკუმენტებში MHR-ის სახელი დადასტურებულად ითვლება
    identity: bool = False
    # ხელით შევსებული ანკეტა („ინფორმაცია თანამშრომელზე“): პ/ნ და მუშაობის დაწყების თარიღი MHR-ს ედარება, დაბადების თარიღი —
    # დადასტურებულ პირადობას; ყველაფერი ემთხვევა და ხელმოწერილია → ავტომატური ატვირთვა (mhr.employee_form_auto_upload)
    employee_form: bool = False


@dataclass
class Paths:
    root: Path
    archive: Path
    sorted: Path
    proposed: Path
    needs_review: Path
    errors: Path
    superseded: Path
    work: Path
    reports: Path
    state: Path
    processed: Path

    @property
    def db(self) -> Path:
        return self.state / "hrsorter.sqlite3"

    @property
    def failed(self) -> Path:
        return self.processed / "_Failed"

    def internal_dirs(self) -> list[Path]:
        """მონიტორინგიდან გამოსარიცხი შიდა საქაღალდეები."""
        return [self.archive, self.sorted, self.proposed, self.needs_review, self.errors,
                self.superseded, self.work, self.reports, self.state, self.processed]


@dataclass
class Config:
    data: dict[str, Any]
    types_data: dict[str, Any]
    path: Path | None = None
    types_path: Path | None = None

    def __post_init__(self) -> None:
        self.validate()

    # --- ვერსია / snapshot ---------------------------------------------------
    @property
    def version(self) -> str:
        blob = json.dumps({"c": self.data, "t": self.types_data}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]

    def snapshot(self) -> str:
        return json.dumps({"config": self.data, "types": self.types_data}, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_snapshot(cls, snap: str) -> "Config":
        d = json.loads(snap)
        return cls(d["config"], d["types"])

    # --- სექციები --------------------------------------------------------------
    def section(self, name: str) -> dict[str, Any]:
        return self.data[name]

    @property
    def thresholds(self) -> dict[str, Any]:
        return self.data["thresholds"]

    @property
    def paths(self) -> Paths:
        p = self.data["paths"]
        root = Path(p["root"])

        def r(key: str) -> Path:
            v = Path(p[key])
            return v if v.is_absolute() else root / v

        state = r("state")
        if not Path(p["state"]).is_absolute() and not on_local_disk(root):
            # ქსელურ დისკზე SQLite ბაზა შეიძლება დაზიანდეს — ჟურნალი ამ კომპიუტერზე, თითო root-ს თავისი
            state = MACHINE_DATA_DIR / "state" / (re.sub(r"[^A-Za-z0-9]+", "_", str(root)).strip("_") or "root")
        return Paths(root=root, archive=r("archive"), sorted=r("sorted"), proposed=r("proposed"),
                     needs_review=r("needs_review"), errors=r("errors"), superseded=r("superseded"),
                     work=r("work"), reports=r("reports"), state=state, processed=r("processed"))

    @property
    def profiles(self) -> list[Profile]:
        root = Path(self.data["paths"]["root"])
        out = []
        for pr in self.data["profiles"]:
            inbox = Path(pr["inbox"])
            out.append(Profile(id=pr["id"], inbox=inbox if inbox.is_absolute() else root / inbox,
                               mode=InputMode(pr["mode"]), default_company=pr.get("default_company")))
        return out

    def profile(self, profile_id: str) -> Profile:
        for p in self.profiles:
            if p.id == profile_id:
                return p
        raise ConfigError(f"unknown profile: {profile_id}")

    @property
    def default_company(self) -> Company:
        d = self.data["default_company"]
        return Company(id=d["id"], name=d["name"], folder=d.get("folder") or d["id"], aliases=d.get("aliases", []))

    def default_company_for(self, profile_id: str | None) -> Company:
        if profile_id:
            for pr in self.data["profiles"]:
                if pr["id"] == profile_id and pr.get("default_company"):
                    d = pr["default_company"]
                    return Company(id=d["id"], name=d["name"], folder=d.get("folder") or d["id"],
                                   aliases=d.get("aliases", []))
        return self.default_company

    @property
    def companies(self) -> list[Company]:
        out = [Company(id=c["id"], name=c["name"], folder=c.get("folder") or c["id"],
                       aliases=c.get("aliases", []), tax_id=c.get("tax_id")) for c in self.data["companies"]]
        dc = self.default_company
        if dc.id not in {c.id for c in out}:
            out.append(dc)
        return out

    def company(self, company_id: str) -> Company | None:
        return next((c for c in self.companies if c.id == company_id), None)

    @property
    def types(self) -> list[DocType]:
        """ყველა ტიპი (არააქტიურების ჩათვლით). „unknown" ყოველთვის არსებობს."""
        known = {f.name for f in fields(DocType)}  # დამატებითი გასაღებები JSON-ში არ აჩერებს პროგრამას
        out = [DocType(**{k: v for k, v in t.items() if k in known}) for t in self.types_data.get("types", [])]
        if "unknown" not in {t.id for t in out}:
            out.append(DocType(id="unknown", name_ka="უცნობი-გადასამოწმებელი", folder="უცნობი", active=True))
        return out

    @property
    def active_types(self) -> list[DocType]:
        return [t for t in self.types if t.active and t.id != "unknown"]

    def doc_type(self, type_id: str) -> DocType | None:
        return next((t for t in self.types if t.id == type_id), None)

    # --- ვალიდაცია -------------------------------------------------------------
    def validate(self) -> None:
        d = self.data
        if not d.get("profiles"):
            raise ConfigError("at least one inbox profile is required")
        ids = [p["id"] for p in d["profiles"]]
        if len(ids) != len(set(ids)):
            raise ConfigError("duplicate profile id")
        for p in d["profiles"]:
            if p.get("mode") not in {m.value for m in InputMode}:
                raise ConfigError(f"profile {p.get('id')}: invalid mode {p.get('mode')!r}")
        if d["ui"]["host"] not in ("127.0.0.1", "localhost"):
            raise ConfigError("ui.host must be 127.0.0.1 (local only)")
        if int(d["processing"]["concurrency"]) < 1:
            raise ConfigError("processing.concurrency must be >= 1")
        for key in ("processed_days", "uploaded_days"):
            if int(d["cleanup"][key]) < 0:
                raise ConfigError(f"cleanup.{key} must be >= 0 (0 = never)")
        processed = self.paths.processed.resolve()
        for pr in self.profiles:
            if pr.inbox.resolve() == processed or pr.inbox.resolve().is_relative_to(processed):
                raise ConfigError(f"profile {pr.id}: inbox must not be inside paths.processed")
        type_ids = [t.get("id") for t in self.types_data.get("types", [])]
        if len(type_ids) != len(set(type_ids)):
            raise ConfigError("duplicate document type id")
        for t in self.types_data.get("types", []):
            for key in ("id", "name_ka", "folder"):
                if not t.get(key):
                    raise ConfigError(f"document type missing {key}: {t}")
        if "{short_id}" not in d["naming"]["template"]:
            raise ConfigError("naming.template must contain {short_id} (protects against name collisions)")
        th = d["thresholds"]
        if not th["boundary_continue"] < th["boundary_start"]:
            raise ConfigError("thresholds.boundary_continue must be < boundary_start")
        # საიდუმლოება არასდროს უნდა ეწეროს config.json-ში
        _reject_inline_secrets(d)

    # --- შენახვა (UI-დან ცვლილება) --------------------------------------------
    def save(self) -> None:
        if not self.path:
            raise ConfigError("config has no file path")
        _atomic_write_json(self.path, self.data)
        if self.types_path:
            _atomic_write_json(self.types_path, self.types_data)


_SECRET_KEYS = {"api_key", "apikey", "token", "password", "secret", "client_secret", "bearer"}


def _reject_inline_secrets(obj: Any, path: str = "") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in _SECRET_KEYS and v:
                raise ConfigError(f"secret value must not be stored in config ({path}{k}); use `hrsorter secret set`")
            _reject_inline_secrets(v, f"{path}{k}.")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _reject_inline_secrets(v, f"{path}{i}.")


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


OFFERED_TYPES_KEY = "_offered_type_ids"


def merge_new_types(types_path: Path, example_path: Path = EXAMPLE_TYPES_PATH) -> list[str]:
    """განახლებისას (`setup`): ახალ ვერსიაში დამატებული ტიპები (მაგ. თანამდებობრივი ინსტრუქცია) არსებულ document_types.json-ს
    ემატება. არსებული ტიპები და მომხმარებლის ცვლილებები ხელუხლებელია; ერთხელ დამატებული ტიპი, მერე წაშლილიც, აღარ ბრუნდება
    (`_offered_type_ids`). აბრუნებს დამატებული ტიპების id-ებს."""
    live = json.loads(types_path.read_text(encoding="utf-8"))
    example = json.loads(example_path.read_text(encoding="utf-8"))
    types = live.setdefault("types", [])
    have = {t.get("id") for t in types}
    offered = set(live.get(OFFERED_TYPES_KEY) or have)
    added: list[str] = []
    for t in example.get("types", []):
        if t["id"] in have or t["id"] in offered:
            continue
        at = next((i for i, x in enumerate(types) if x.get("id") == "unknown"), len(types))   # „უცნობი“ ბოლოში რჩება
        types.insert(at, t)
        added.append(t["id"])
    ids = sorted(offered | have | {t["id"] for t in example.get("types", [])})
    if added or live.get(OFFERED_TYPES_KEY) != ids:
        live[OFFERED_TYPES_KEY] = ids
        _atomic_write_json(types_path, live)
    return added


def load_config(path: str | Path | None = None) -> Config:
    """config.json-ის ჩატვირთვა DEFAULTS-თან შერწყმით; types ფაილი paths.types_file-დან."""
    cfg_path = Path(path or os.environ.get(DEFAULT_CONFIG_ENV) or DEFAULT_CONFIG_PATH)
    if not cfg_path.exists():
        raise ConfigError(f"config file not found: {cfg_path} (copy config/config.example.json)")
    user = json.loads(cfg_path.read_text(encoding="utf-8"))
    data = deep_merge(DEFAULTS, user)
    if "profiles" in user:  # სიები არ ერწყმის — მომხმარებლის სია ანაცვლებს
        data["profiles"] = user["profiles"]
    types_path = Path(data["paths"]["types_file"])
    if not types_path.is_absolute():
        types_path = cfg_path.parent / types_path
    if not types_path.exists():
        raise ConfigError(f"document types file not found: {types_path}")
    types_data = json.loads(types_path.read_text(encoding="utf-8"))
    return Config(data, types_data, path=cfg_path, types_path=types_path)


def make_config(overrides: dict[str, Any] | None = None, types_data: dict[str, Any] | None = None) -> Config:
    """ტესტებისთვის: DEFAULTS + overrides, ტიპები — მაგალითის ფაილიდან, თუ არ მიეწოდა."""
    data = deep_merge(DEFAULTS, overrides or {})
    if overrides and "profiles" in overrides:
        data["profiles"] = overrides["profiles"]
    if types_data is None:
        types_data = json.loads(EXAMPLE_TYPES_PATH.read_text(encoding="utf-8"))
    return Config(data, types_data)
