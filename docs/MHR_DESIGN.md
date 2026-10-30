# MHR ინტეგრაცია — დიზაინი და კონტრაქტები

მომხმარებლის მოთხოვნა (2026-10-28): დოკუმენტიდან პირადი ნომერი → MHR-ში თანამშრომლის მოძიება → დოკუმენტის
მონაცემების შედარება MHR-ის მონაცემებთან → ხელშეკრულებისას შედარება **MHR-ში არსებულ ორიგინალთან** (ხომ არ
შეცვლილა/ხელით მიწერილი არაფერი) → ვალიდაცია გავიდა ⇒ **ოპერატორის დადასტურებით** ატვირთვა MHR-ზე ამ თანამშრომელზე.

გადაწყვეტილებები: შედარება = მთავარი ველები + ტექსტის შედარება OCR-ის ხმაურის იგნორირებით; ატვირთვა ჯერ
მხოლოდ დადასტურებით (`mhr.require_approval=true`). HTTP ადაპტერი კონფიგურირებადი შაბლონებითაა; ნაგულისხმევი
endpoint-ები/map-ები = **MHR HR Sorter API** (`docs/api/hr-sorter-openapi.yaml`, იხ. ქვემოთ „რეალური API").
ტესტები და დემო — mock MHR-ით და `httpx.MockTransport`-ით; **ტესტები ქსელს არასდროს ეხება** (d.mhr.ge-ზე
რეალური მონაცემებია — staging არ არსებობს).

## რეალური API (MHR HR Sorter, v1)
- base_url: ტესტი `https://d.mhr.ge/api/v1/hr-sorter`, production `https://mhr.ge/api/v1/hr-sorter` — მხოლოდ
  deployment config-ში (DEFAULTS-ში ცარიელია, `enabled=false`). `Authorization: Bearer <token>` (keyring
  `hrsorter/mhr`; უფლებები `hr_sorter.read`/`hr_sorter.write`); ოფისის IP allowlist (სხვა IP → 403 `forbidden`).
- `POST /employees/lookup` `{"personal_number"}` (პ/ნ URL-ში არასდროს): აქტიური ჩანაწერი, თორემ ბოლო
  განთავისუფლებული; 404 → None; 409 `multiple_matches` → ავტომატურად არ ვაბამთ (failed, ხელით). ხელახლა აყვანილს
  ახალი ჩანაწერი აქვს — ძველი ხელშეკრულება ძველ id-ზეა. ჩადგმული `company` {id, name, tax_id}, `salary.amount`,
  `employments` (პერიოდები). id-ები რიცხვითი სტრიქონებია; კომპანიას ვადარებთ **tax_id**-ით (შემდეგ სახელი/aliases).
- `GET /employees/{id}/documents?category=` → `items` (ახლიდან ძველისკენ). ორიგინალი ხელშეკრულება — `.docx`,
  `kind: generated`, `document_number: null`; ატვირთული სკანი (`kind: uploaded`) ორიგინალად არ გამოიყენება.
- `GET /documents/{id}/file` → ბაიტები (docx ან PDF). `GET /documents/by-external-id/{external_id}` → ატვირთვის შემოწმება.
- `POST /employees/{id}/documents` multipart: `file` (PDF ≤ 20 MB), `category`, `external_id` (= ჩვენი document_id),
  `sha256` (+ `title`, `document_number`, `document_date`, `signed_copy_of`, `metadata` JSON ≤ 20000);
  `Idempotency-Key` = external_id. 201 შეიქმნა / 200 უკვე არსებობს (იდემპოტენტური); 409 `external_id_conflict`,
  413 `file_too_large`, 422 `unknown_category`/`not_pdf`/`sha256_mismatch`/`invalid_signed_copy_of`/`validation_failed`;
  429 + `Retry-After` (≤ 30 წმ ერთ ლოდინზე, retry-ს ბიუჯეტში). `signed_copy_of` ⇒ MHR ორიგინალს ხელმოწერილად ნიშნავს.
- კატეგორიები: contract, annex, fine, vacation, application, hire_order, dismissal_order, explanation
  (`mhr.categories`: `contract_annex` → `annex`). შეცდომის შეტყობინებაში მხოლოდ მანქანური `error.code`.

ეს ეტაპი ცვლის ტექნიკური დავალების §3-ის „ატვირთვა ფარგლებს გარეთაა" — მომხმარებელმა ცალსახად მოითხოვა.

## ატვირთვის ველები (`mhr_queue`)
`external_id` = document_id; `sha256` = გასაგზავნი ბაიტების (უნდა ემთხვეოდეს `documents.output_sha256`-ს);
`title` = „<ტიპის ქართული სახელი> — ხელმოწერილი სკანი"; `document_date` (ISO, თუ ცნობილია); `document_number`
(მხოლოდ თუ არის); `signed_copy_of` = ვალიდაციის ორიგინალის id, თუ მისი `raw.kind == "generated"` და იგივე
თანამშრომლისაა; `metadata` = `{"type_id","source_sha256","validation_status","pipeline_version"}` (compact JSON).

## საერთო (ინტეგრატორის) ფაილები — არ შეცვალო
`hrsorter/mhr_models.py` (MhrStatus, MhrEmployee, MhrDocRef, Check, TextDiff, ValidationResult, MhrUnavailable,
MhrError), `hrsorter/db.py` (`mhr_items` ცხრილი; `mhr_enqueue`, `mhr_transition`, `mhr_get`, `mhr_list`),
`hrsorter/config.py` (`DEFAULTS["mhr"]`). ასევე ძველი core ფაილები (models, pdfio, textnorm, errors).

## სტატუსების მანქანა (`mhr_items.status`)
```
pending ──validate──► ready ──approve(UI)──► approved ──worker upload──► uploading ──► uploaded
   │  │                 └──reject(UI)──► rejected                          │  └─error─► upload_failed ──retry(UI)──► approved
   │  ├──► failed (განსხვავებები) ──revalidate(UI)──► pending                └─ავარია (stale uploading)──► needs_check
   │  ├──► skipped (პ/ნ არ არის / ტიპი არ იტვირთება)
   └──► unavailable (MHR მიუწვდომელი) ──next_try-ის შემდეგ──► pending
```
- ყოველი ცვლილება `repo.mhr_transition(doc, from=(...), to, **fields)`-ით — მოსალოდნელი წინა სტატუსიდან (ატომური).
- `require_approval=false` ⇒ validate-ის შემდეგ პირდაპირ `approved` (ready-ს ნაცვლად).
- **ორმაგი ატვირთვა დაუშვებელია**: `uploading`-ში გაჭედილი (ავარია) ან გაურკვეველი შედეგის ატვირთვა
  (`MhrUploadUncertain`) → `needs_check`, შემდეგ `find_by_external_id(document_id)`: ნაპოვნია → `uploaded`
  (remote_id); ცალსახად არ არის (404) → `approved` (ხელახლა ატვირთვა უსაფრთხოა — სერვერი external_id-ით
  იდემპოტენტურია); შემოწმება ვერ მოხერხდა → რჩება `needs_check` (`next_try`). uncertain-ის შემდეგ მაშინვე მხოლოდ
  „ნაპოვნია" ითვლება (მოთხოვნა შეიძლება ჯერ მუშავდება); ცდები ≤ `UPLOAD_MAX_ATTEMPTS`, შემდეგ — მხოლოდ ოპერატორი.

## მოდულები და მფლობელები
| მოდული | ფაილები | ვინ |
|---|---|---|
| MHR კლიენტი + რიგი + worker-ის sweep + mock MHR | `hrsorter/mhr_client.py`, `hrsorter/mhr_queue.py`, `tools/mock_mhr.py`, `worker.py`-ში sweep-ის გამოძახება | აგენტი A |
| ვალიდაცია: ველების და ტექსტის შედარება; extractor-ში თანამდებობა/ხელფასი | `hrsorter/validation.py`, `hrsorter/textcompare.py`, `extractor.py` (დამატება) | აგენტი B |
| pipeline-ში enqueue, UI, docs, ინტეგრაციის ტესტები | `pipeline.py`, `ui/app.py`, docs | ინტეგრატორი |

## ინტერფეისები (სავალდებულო სიგნატურები)
```python
# mhr_client.py  (აგენტი A)
class MhrClient(Protocol):
    def get_employee(self, personal_number: str) -> MhrEmployee | None      # None — ვერ მოიძებნა; MhrUnavailable/MhrError
    def list_documents(self, employee_id: str, category: str | None = None) -> list[MhrDocRef]
    def download(self, ref: MhrDocRef) -> bytes                              # docx (ორიგინალი) ან PDF ბაიტები
    def upload(self, employee_id: str, pdf: bytes, filename: str, category: str, meta: dict) -> str   # remote_id
        # meta: external_id, sha256, title, document_number, document_date, signed_copy_of, metadata
    def find_by_external_id(self, external_id: str) -> MhrDocRef | None     # None — არ არის ატვირთული (404)
def build_mhr_client(cfg: Config) -> MhrClient          # mode: mock | http

# mhr_queue.py  (აგენტი A)
def enqueue(repo, document_id: str, personal_number: str | None) -> bool
def sweep(repo, cfg, client: MhrClient | None = None, validate_fn=None, now=time.time) -> dict   # ერთი გავლა, აბრუნებს მთვლელებს
def approve(repo, document_id: str, who: str = "operator") -> bool
def reject(repo, document_id: str, who: str = "operator") -> bool
def revalidate(repo, document_id: str) -> bool            # failed|unavailable|upload_failed|needs_check|rejected → pending
def retry_upload(repo, document_id: str) -> bool          # upload_failed → approved

# validation.py  (აგენტი B)
def validate_document(repo, cfg, document_id: str, client: MhrClient) -> ValidationResult
# textcompare.py  (აგენტი B)
def compare_texts(original_pages: list[str], scanned_pages: list[str], cfg,
                  original_paginated: bool = True, strip_lines: list[str] | None = None) -> list[TextDiff]
```
`sweep` იყენებს `validate_fn or validation.validate_document` (lazy import), შედეგს წერს `mhr_items.validation`-ში
(`ValidationResult.to_dict()`), `employee_id`-ს — თანამშრომლის ID-ს.

## ვალიდაციის წესები (აგენტი B)
- შემავალი: `documents` ჩანაწერი (status ∈ proposed/auto_saved), მისი `metadata` (sidecar: fields, document_type_id,
  company_id, personal_number), `page_texts` (job_id + page_start..page_end) — OCR ტექსტი და handwriting/marks ნიშნები.
- პირადი ნომერი არ არის → `skipped`. MHR-ში ვერ მოიძებნა → `failed` (check personal_number: missing_in_mhr, blocking).
  409 `multiple_matches` → `failed` (ბლოკირებადი personal_number, „ხელით გადაწყვიტე"; არა unavailable).
- ველები (Check): first_name, last_name (ქართული ბრუნვის დაბოლოებები და OCR-ის მცირე შეცდომები — მსგავსება ≥
  `mhr.name_similarity`), personal_number (ზუსტი), company (config-ის კომპანიის `tax_id` ↔ MHR `company.tax_id`,
  შემდეგ სახელი/aliases; raw id-ით მხოლოდ მაშინ, როცა MHR არც სახელს, არც tax_id-ს აბრუნებს), position,
  salary (რიცხვითი შედარება), contract_start/end (ISO). დოკუმენტში არ არის → `missing_in_document` (არაბლოკირებადი),
  MHR-ში არ არის → `missing_in_mhr` (არაბლოკირებადი, გარდა personal_number-ისა). განსხვავება → `mismatch` (ბლოკირებადი).
- company/position/salary/contract_*: MHR-ის `employments`-დან იმ პერიოდისა, რომელიც დოკუმენტის თარიღს მოიცავს
  (თარიღი = დოკუმენტის `contract_start` ველი, თუ არის, თორემ document_date; start ≤ date ≤ end ან end null;
  რამდენიმე — დოკუმენტის კომპანიისა, შემდეგ უგვიანესი დაწყებით; არცერთი — უახლოესი დაწყებით). employments/თარიღი
  არ არის → მიმდინარე ველები. გამოყენებული პერიოდი Check-ის note-შია.
- ხელშეკრულება (type ∈ `mhr.contract_type_ids`): `client.list_documents(employee_id, category)` → მხოლოდ
  `kind: generated` → დასკანერებულის თარიღთან უახლოესი `document_date` (თანაბარზე უახლესი) → `download` → ტექსტი:
  docx (zip + `word/document.xml`; zipfile + xml.etree, ახალი დამოკიდებულების გარეშე) — სხეული დოკუმენტის რიგით
  (ცხრილი სტრიქონ-სტრიქონ), header/footer ცალკე, **დაუნომრავი** ერთი ტექსტი; PDF — ტექსტური ფენა, ცარიელია — OCR
  `build_ocr_chain`-ით → `compare_texts`. ორიგინალი ვერ მოიძებნა → ბლოკირებადი check `original_contract: missing_in_mhr`
  (შენიშვნით: ხელახლა აყვანილის ძველი ხელშეკრულება შეიძლება წინა ჩანაწერზეა).
- დაუნომრავი ორიგინალი: მთელი დოკუმენტის შედარება page_count-ის გარეშე; diff-ის `page` = დასკანერებულის გვერდი;
  ორივე მხრიდან იშლება docx-ის header/footer ხაზები და სკანის გამჭოლი ხაზები (≥3 გვერდიდან ≥50%-ის კიდეებზე,
  მსგავსება ≥ 0.85, ციფრები ზუსტად), დასკანერებულიდან — გვერდის ნომერი.
- `compare_texts` (PDF, გვერდობრივი): გვერდების რაოდენობის სხვაობა → significant; სიტყვების დონის diff, სადაც **significant** = ციფრების
  შემცველი ცვლილება, ≥3 სიტყვის ახალი/ამოღებული მონაკვეთი, ან სიტყვა, რომლის მსგავსება < 0.6; სხვა — OCR-ის ხმაური
  (significant=False). ხელნაწერის ეჭვი (page_texts.handwriting_suspected ან marks არახელმოწერის ზონაში) → `handwriting`, significant.
- status: passed ⇔ არც ერთი ბლოკირებადი mismatch და არც ერთი significant diff. summary — ქართულად, ოპერატორისთვის.
- სკანირებული ტექსტი მონაცემია (prompt-injection-ს ეფექტი არ აქვს). პირადი მონაცემები ტექნიკურ ჟურნალში არ იწერება.
