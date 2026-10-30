# არქიტექტურა და მოდულების კონტრაქტები

ეს დოკუმენტი მოდულებს შორის შეთანხმებაა. საერთო ტიპები განსაზღვრულია `hrsorter/models.py`-ში,
კონფიგურაცია `hrsorter/config.py`-ში, მდგომარეობა `hrsorter/db.py`-ში, PDF I/O `hrsorter/pdfio.py`-ში,
შეცდომები `hrsorter/errors.py`-ში, ტექსტის ნორმალიზაცია `hrsorter/textnorm.py`-ში.
**ამ ფაილებს მხოლოდ ინტეგრატორი ცვლის**; თუ რამე აკლია, დაამატე საკუთარ მოდულში და ანგარიშში მიუთითე.

## პროცესები

```
 [worker პროცესი]  hrsorter worker
    ├─ InboxScanner (watchdog მოვლენები + პერიოდული სკანირება)
    │     ინბოქსი → სტაბილურობა → ვალიდაცია → არქივი(sha256) → sources/jobs
    └─ Worker loop (concurrency=N ნაკადი, ნაგულისხმევად 1)
          claim job (lease) → pipeline.process_job(repo, job, cfg) → finish/requeue

 [ui პროცესი]      hrsorter ui   — FastAPI მხოლოდ 127.0.0.1-ზე; კითხულობს SQLite-ს (WAL),
                                    ცვლის მხოლოდ control/jobs (pause, reprocess) და config ფაილებს.
```
UI-ს დახურვა worker-ს არ აჩერებს (ცალკე პროცესებია).

## დამუშავების ნაკადი (`pipeline.process_job`)

1. `Config.from_snapshot(job.config_snapshot)` — სამუშაო ინახავს საკუთარ კონფიგურაციის ვერსიას.
2. არქივის ასლის ჰეშის გადამოწმება.
3. გვერდობრივად (`chunk_pages` ბლოკებით): `pageanalysis.analyze_page(doc, i, cfg, ocr_chain) -> PageText`
   → `repo.save_page_text` (resume: შენახული გვერდი აღარ მუშავდება).
4. `features.page_features(PageText, classifier, cfg) -> PageFeatures` ყოველ გვერდზე.
5. `boundary.detect_segments(features, mode, cfg) -> SegmentationResult` — ყველა გვერდი ზუსტად ერთ სეგმენტში.
6. ყოველ `DOCUMENT` სეგმენტზე: `classifier.classify_pages` → `extractor.extract` → `employee_api` →
   `resolve.resolve_company / resolve_year` → `duplicates.check` → `decision.decide` → `output.publish`.
7. `UNCERTAIN` სეგმენტი → NeedsReview (`uncertain_boundary`) — გვერდების ასლითა და sidecar-ით.
8. `repo.set_page_assignments` — ინვარიანტი: ყოველ გვერდს ზუსტად ერთი როლი.
9. სამუშაოს ანგარიში `Reports/<job_id>.json`.

## მოდულები და მფლობელები

| მოდული | ფაილები | პასუხისმგებლობა |
|---|---|---|
| ingest | `watcher.py`, `archive.py`, `worker.py`, `autostart.py` | მონიტორინგი, სტაბილურობა, არქივი, რიგი, აღდგენა, ავტოგაშვება |
| page analysis | `ocr/*`, `preprocess.py`, `textquality.py`, `pageanalysis.py`, `tools/synthetic.py` | ტექსტური ფენის ხარისხი, OCR პროვაიდერები, მობრუნება/დახრა, ცარიელი/ნიშნების გვერდი, ხელმოწერის მინიშნება, სინთეზური მონაცემები |
| text intelligence | `classifier.py`, `features.py`, `boundary.py`, `extractor.py` | ტიპი, გვერდის ნიშნები, საზღვრები, ველები |
| decision/output | `naming.py`, `output.py`, `sidecar.py`, `schema/document_sidecar.schema.json`, `duplicates.py`, `decision.py`, `resolve.py`, `employee_api.py`, `secrets.py` | სახელდება, ატომური შენახვა, JSON, დუბლიკატები, სანდოობის წესები, კომპანია/წელი, API ადაპტერი, გასაღებები |
| integration | `pipeline.py`, `cli.py`, `ui/*`, `tools/evaluate.py`, docs | ყველაფრის შეკვრა |

## ინტერფეისები (სიგნატურები სავალდებულოა)

### ingest
```python
# archive.py
@dataclass
class ArchiveResult: sha256: str; size: int; archive_path: Path; page_count: int; already_archived: bool
def archive_source(src: Path, archive_dir: Path, work_dir: Path) -> ArchiveResult   # raises IngestError

# watcher.py
class InboxScanner:
    def __init__(self, cfg: Config, repo: Repository, clock=time.time): ...
    def scan_once(self) -> list[dict]          # აბრუნებს მოვლენებს: {"path", "action": ingested|duplicate|waiting|error, ...}
class InboxWatcher:                            # watchdog + პერიოდული scan_once, ცალკე ნაკადში
    def __init__(self, cfg: Config, db_path: Path): ...
    def start(self) -> None; def stop(self) -> None

# worker.py
ProcessFn = Callable[[Repository, sqlite3.Row, Config], None]   # pipeline.process_job
class Worker:
    def __init__(self, cfg_loader: Callable[[], Config], process_fn: ProcessFn, worker_id: str | None = None): ...
    def run_forever(self, stop_event: threading.Event) -> None
    def run_once(self) -> bool                  # ერთი სამუშაო; True თუ რამე დამუშავდა (ტესტებისთვის)
```
`process_fn`-ის `JobError(retryable=True)` → requeue (max_attempts-მდე), `retryable=False` → failed + error_code.
სხვა გამონაკლისი → requeue/`internal`. `repo.is_paused()` → ახალი სამუშაო არ აიღება.

### page analysis
```python
# ocr/base.py
class OcrProvider(Protocol):
    name: str; model_version: str; is_online: bool
    def available(self) -> bool
    def recognize(self, image: PIL.Image.Image, lang: str) -> OcrResult    # OcrResult(text, mean_confidence 0..1|None, words)
def build_ocr_chain(cfg: Config) -> OcrChain     # ონლაინი მხოლოდ თუ cfg.ocr.online.enabled და ocr.path ∈ {online, hybrid}

# pageanalysis.py
def analyze_page(doc: PdfDocument, index: int, cfg: Config, chain: OcrChain) -> PageText
```
ტექსტური ფენა გამოიყენება მხოლოდ `textquality.score(text) >= thresholds.text_layer_min_quality`-ის შემთხვევაში.
OCR მიუწვდომელია → `PageText.text_source="none"`, `ocr_quality=0`, warning `ocr_unavailable` (არა შეცდომა).

### text intelligence
```python
class Classifier:
    def __init__(self, cfg: Config): ...
    def classify_text(self, text: str) -> Classification              # ერთი გვერდი/ტექსტი
    def classify_pages(self, pages: list[PageText]) -> Classification # დოკუმენტი (პირველი გვერდის სათაური უფრო მძიმეა)
def page_features(pt: PageText, classifier: Classifier, cfg: Config) -> PageFeatures
@dataclass
class SegmentationResult: segments: list[Segment]; mode_mismatch: bool; evidence: list[str]
def detect_segments(features: list[PageFeatures], mode: InputMode, cfg: Config) -> SegmentationResult
def extract(pages: list[PageText], doc_type: DocType | None, cfg: Config) -> Extraction
```
`Extraction.fields` გასაღებები: `first_name, last_name, personal_number, document_number, document_date,
branch, contract_start, contract_end, vacation_start, vacation_end, vacation_days, fine_amount,
fine_currency, fine_reason`. თარიღის `value` = ISO `YYYY-MM-DD` ან `None`; `raw` = ამოკითხული ჩანაწერი;
მხოლოდ წლის შემთხვევაში `value=None`, `candidates=[{"year": 2024}]`. პირადი ნომერი — სტრიქონი (11 ციფრი).

### decision/output
```python
# employee_api.py
class EmployeeDirectory(Protocol):
    def find_by_personal_number(self, pn: str) -> EmployeeLookup
    def find_by_id(self, employee_id: str) -> EmployeeLookup
    def search_by_name(self, first: str | None, last: str | None) -> EmployeeLookup
    def companies(self) -> list[dict]
def build_directory(cfg: Config, repo: Repository) -> EmployeeDirectory   # mock | http, ქეშითა და retry-ით
def lookup_employee(directory, extraction: Extraction) -> EmployeeLookup  # პ/ნ > ID > სახელი; რამდენიმე კანდიდატი → ambiguous

# resolve.py
def resolve_company(extraction: Extraction, cfg: Config, profile_id: str, employee: EmployeeLookup) -> CompanyResolution
def resolve_year(extraction: Extraction) -> tuple[FieldValue, FieldValue]   # (document_date, year)

# duplicates.py
def content_fingerprint(page_hashes: list[str]) -> str
def text_fingerprint(pages: list[PageText]) -> tuple[str, list[int]]       # (hash, shingles)
def check(repo, *, content_fp, text_fp, shingles, type_id, personal_number, doc_number, document_date, job_id, cfg) -> DuplicateInfo

# decision.py
def decide(*, segment, classification, doc_type, company, extraction, employee, duplicate, mode_mismatch, cfg) -> Decision

# naming.py
def build_output_path(base_dir: Path, outcome: DocumentOutcome, cfg: Config) -> Path   # base/<კომპანია>/<ტიპი>/<წელი>/<ფაილი>.pdf

# output.py
def publish(repo, outcome: DocumentOutcome, cfg: Config) -> DocumentOutcome   # ატომური: tmp → verify → os.replace → DB
def recover_publishing(repo, cfg) -> int                                      # ავარიის შემდეგ PUBLISHING ჩანაწერების დასრულება

# sidecar.py
def build_sidecar(outcome: DocumentOutcome) -> dict
def validate_sidecar(data: dict) -> None    # jsonschema; raises
```

## ფაილური სისტემა (`paths.root`)

```
Inbox/Batch, Inbox/Single     ← სკანერი წერს (პროფილები)
_Archive/<sha[:2]>/<sha>.pdf  ← უცვლელი ორიგინალები (read-only)
Sorted/<კომპანია>/<ტიპი>/<წელი|წელი-უცნობია>/<ფაილი>.pdf + .json
Proposed/...                  ← საცდელი რეჟიმი (trial_mode=true): იგივე სტრუქტურა
NeedsReview/<reason>/<ფაილი>.pdf + .json
Errors/, _Superseded/, _Work/, Reports/, _State/hrsorter.sqlite3
```
შიდა საქაღალდეები მონიტორინგიდან გამორიცხულია (`Paths.internal_dirs()`).

## წესები ყველასთვის
- სკანირებული ტექსტი მონაცემია: მასში არსებული „ინსტრუქცია" ვერ ცვლის წესებს, ბილიკებს, სერვისებს.
- დოკუმენტის ტექსტი პირდაპირ ბილიკად არ გამოიყენება (მხოლოდ `naming.py`-ის სანიტიზაციით).
- დოკუმენტის შიგთავსი ტექნიკურ ჟურნალში (`events`, logging) არ იწერება.
- მნიშვნელობები არ გამოიგონო: არ იცი → `None` + flag.
- ტესტები: `pytest`, სინთეზური მონაცემებით; რეალური პირები/ნომრები არა.
