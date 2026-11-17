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
- კატეგორიები: contract, annex, fine, vacation, application, hire_order, dismissal_order, explanation, id_card
  (id_card — პირადობის მოწმობა), employee_form (თანამშრომლის ანკეტა; MHR-ის მხარეს დასამატებელია, იხ. MHR_API_PROMPT.md)
  (`mhr.categories`: `contract_annex` → `annex`). შეცდომის შეტყობინებაში მხოლოდ მანქანური `error.code`.

ეს ეტაპი ცვლის ტექნიკური დავალების §3-ის „ატვირთვა ფარგლებს გარეთაა" — მომხმარებელმა ცალსახად მოითხოვა.

## ატვირთვის ველები (`mhr_queue`)
`external_id` = document_id; `sha256` = გასაგზავნი ბაიტების (უნდა ემთხვეოდეს `documents.output_sha256`-ს);
`title` = „<ტიპის ქართული სახელი> — ხელმოწერილი სკანი"; `document_date` (ISO, თუ ცნობილია); `document_number`
(მხოლოდ თუ არის); `signed_copy_of` = ვალიდაციის ორიგინალის id, თუ მისი `raw.kind == "generated"` და იგივე
თანამშრომლისაა; `metadata` = `{"type_id","source_sha256","validation_status","pipeline_version"}` (compact JSON).

**განთავისუფლების განცხადება (2026-11-12):** MHR-ში მენეჯერი განთავისუფლების დაფიქსირებისას თანამშრომლის ხელნაწერი განცხადების
ფოტოს ტვირთავს — ბარათზე `kind: uploaded`, კატეგორია `application`, სათაური `STATEMENT_TITLE` („თანამშრომლის განცხადება
(ხელნაწერი)", MHR-თან კონტრაქტი — `App\Support\DismissalStatements::TITLE`). ორიგინალის (`kind: generated`) არქონისას
`validation._link_statement` ასეთ ფაილს პოულობს იმავე კატეგორიაში და წერს `ValidationResult.statement`-ში (`original` ცარიელი რჩება —
ხელნაწერი არ შედარდება, UI „ემთხვევა"-ს არ ამბობს). რამდენიმეა → უახლესი, რომელსაც სკანი (`signed_copy_of`) ჯერ არ აქვს; ყველას აქვს →
უახლესი + `already_uploaded` (ბლოკირებადი). ატვირთვისას `signed_copy_of` = განცხადების id (იგივე თანამშრომელი), `title` =
„<ტიპი> — ქაღალდის ორიგინალი". MHR ასეთ მიბმას იღებს (`HrSorterController::isOriginal`) და რეესტრში „ორიგინალი მოვიდა"-ს აჩენს;
`MockMhr`-იც იგივეს ამოწმებს.

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
- `pending ──validate──► split`: სკანში რამდენიმე MHR-ის ფაილია — საბოლოო სტატუსი; ნაწილები საკუთარ ჩანაწერებად განაგრძობენ.
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
                  original_paginated: bool = True, strip_lines: list[str] | None = None, labels: set[str] | None = None,
                  known: list[str] | None = None, trusted_pages: set[int] | None = None) -> list[TextDiff]
```
`sweep` იყენებს `validate_fn or validation.validate_document` (lazy import), შედეგს წერს `mhr_items.validation`-ში
(`ValidationResult.to_dict()`), `employee_id`-ს — თანამშრომლის ID-ს.

**ერთ სკანში რამდენიმე ფაილი** (`plan_pages`, `_split_scan`, `output.split_document`, 2026-11-04): თანამშრომლის ყველა
კატეგორიის `kind: generated` ფაილი (`original_candidates`-ის თარიღის წესით, ≤12) ჩამოიტვირთება და ყოველი არაცარიელი გვერდი იმ ფაილს
მიეკუთვნება, რომლის ტოკენების ≥`PAGE_OWNER_MIN_OVERLAP` (0.7) შეიცავს; ≥`PAGE_OWNER_WEAK` (0.6) მეზობელთან რჩება, ≤`PAGE_OWNER_GAP`
(2) გვერდიანი წყვეტა ერთი ფაილის შიგნით ივსება, ტექსტის გარეშე გვერდი მეზობელს მიჰყვება. ფაილის ტოკენებში მისი header/footer-იც
ითვლება (ყოველ გვერდზე იბეჭდება). გვერდი წინა გვერდის ფაილთან რჩება, თუ სხვა ფაილი ≤`PAGE_OWNER_TIE` (0.05) წილით იგებს, ან — როცა
წინა ფაილიც ≥0.7-ს ხსნის — ≤`PAGE_OWNER_TIE_WORDS` (5) სიტყვით: ხელმოწერებისა და რეკვიზიტების მოკლე გვერდი თითქმის ყველა ფაილს
ჰგავს და დიდი ხელშეკრულება ერთი-ორი სიტყვით იგებდა (2026-11-11). ≥2 ფაილი ან ზედმეტი გვერდები → დაყოფა:
`documents` ჩანაწერი თითო ფაილზე (`part_of`, `metadata.part` {of, pages, original_id, category, title}, საკუთარი PDF, დეტერმინისტული id),
`mhr_items` — `pending`; მთლიანი სკანი რჩება აქტიურად (`status=split`, MHR `split`, ValidationResult.parts) — დუბლიკატის შემოწმება
მასზე მუშაობს და `/manual`-ში არ ჩანს. არცერთს არ ემთხვევა → ნაწილი `needs_review` (`pages_not_in_mhr`), რიგის გარეშე. ერთი ფაილი
ყველა გვერდით → დაყოფის გარეშე, `adopt_match` ტიპს ფაილიდან აყენებს (`_matched_type`: სახელში „ხელშეკრულება“ → contract, სხვა → contract_annex)
და ხსნის `MATCH_RESOLVED_REASONS`-ს; ამიტომ `pipeline._goes_to_mhr` ასეთ გადასამოწმებელსაც რიგში აყენებს, თუ პ/ნ იკითხება. ნაწილი ცალკე
ფასდება: contract — ველები + ხელმოწერა ყველა გვერდზე; სხვა — პ/ნ + `last_page_signature_diffs`, ხელნაწერი/ნიშანი შენიშვნაა. ატვირთვა: თითო
PDF, `signed_copy_of` = თავისი ორიგინალი, კატეგორია და სათაური ორიგინალიდან. ხელმოწერილი ასლი უკვე არის → `already_uploaded` ბლოკავს.
`housekeeping`: მთლიანი სკანის PDF იშლება მხოლოდ ყველა ნაწილის შემდეგ.

**ორიგინალის არჩევა** (`original_candidates`, 2026-11-04): მხოლოდ `kind: generated`; დოკ. ნომერი სათაურში > სკანის
თარიღის ±`ORIGINAL_DATE_TOLERANCE_DAYS` (7) და უახლოესი > **უახლესი დაგენერირებული** (±7-ის გარეთაც — `original_date_note`
info-check ბლოკირების გარეშე). დაგენერირებული საერთოდ არ არის → ბლოკირებადი `original_contract: missing_in_mhr`
`NO_ORIGINAL_NOTE`-ით („ჯერ MHR-ში დააგენერირე…"); თანამდებობის შაბლონთან შედარება (`_compare_with_template`) მხოლოდ
`mhr.template_fallback=true`-ზე (ნაგულისხმევად false). მომხმარებლის მოთხოვნა: ყოველთვის დაგენერირებულს შეადაროს.
UI-ში ეს კედელი არ არის: `_no_compare(validation)` (check `original_contract: missing_in_mhr`) → MHR-ის დეტალებში ჩანს
„ხელით ატვირთვა — შედარების გარეშე“ ბარათი (ჩვეულებრივ failed+employee_id-ზე ის იმალება) და „ატვირთვა მაინც“ ფორმა სხვა
ტექსტით, დოკუმენტის გვერდზე კი ბარათი „MHR-ში ხელშეკრულება ვერ მოიძებნა“ ხელით ატვირთვის ღილაკით.

**„MHR-ში აკლია“** (`mhr_missing`, 2026-11-10): failed ჩანაწერი, რომელსაც ბლოკირებადი `missing_in_mhr` check აქვს
(`missing_kind`: `employee` — თანამშრომელი ვერ მოიძებნა; `original` — `original_contract`; `data` — ბარათის ველი ცარიელია),
სკანის პრობლემა არ არის. UI: `/mhr?tab=missing` — სია თანამშრომლების მიხედვით (`mhr_missing.groups`), „ვერ გაიარა“ ჩანართი
და მთავარი გვერდის ამოცანა ასეთებს აღარ ითვლის. **თავისით გადამოწმება** (`mhr_queue._recheck_missing`, sweep-ის ნაბიჯი a2):
ვალიდაცია ინახავს `ValidationResult.mhr_snapshot`-ს — ანაბეჭდს იმისა, რა ნახა MHR-ში (`validation.mhr_snapshot`:
თანამშრომლის ძირითადი ველები + დაგენერირებული ფაილების სია; ატვირთული სკანები არ შედის). ყოველ
`mhr.missing_recheck_minutes` (15) წუთში თითო თანამშრომელზე ორი GET (თანამშრომელი, ფაილების სია): ანაბეჭდი შეიცვალა →
failed → pending (`attempts=1`), არ შეიცვალა → მხოლოდ `next_try` (`updated_at` არ იცვლება). ძველი ვერსიის ჩანაწერი
ანაბეჭდის გარეშე ერთხელ მოწმდება თავიდან (`attempts == 0`). AI ანალიზი ამ გზაზე არ იშლება: `_validate_one` ინახავს მას,
თუ ახალი შედეგი იგივეა (`_same_outcome`) — MHR-ში უცხო ცვლილება Opus-ის ხარჯს არ იმეორებს. „ახლავე შეამოწმე“
(`recheck_missing_now`) მხოლოდ `next_try`-ს ასუფთავებს.

**საბუთების სისრულე** (`people`, გვერდი `/people`, 2026-11-10): თანამშრომლები = პ/ნ-ები, რომელთა ერთ მაინც mhr_items
ჩანაწერს `employee_id` აქვს (MHR-ის API სიას არ იძლევა). `people.refresh` (worker, `_mhr_sweep`-ის ბოლოს; ≤10 თანამშრომელი
გავლაზე, თითოზე ორი GET) ბარათს ინახავს `employee_cache`-ში გასაღებით `card:<პ/ნ>`: ფაილების მოკლე სია, დაგენერირებულზე
ტიპი (`type_from_filename`) და ეტაპი (`file_stage`) უკვე გამოთვლილი. `people.checklist` რენდერისას ითვლება ბარათიდან +
ადგილობრივი mhr_items-იდან, ამიტომ პროგრამით ატვირთული მაშინვე ჩანს. წესი: `mhr_original`/ხელშეკრულების ტიპზე ბარათის
**უახლესი** დაგენერირებული ფაილი უნდა იყოს ხელმოწერილი (`signed_at`, ან uploaded `signed_copy_of`, ან ადგილობრივი uploaded
იმავე `original`-ით); სკანი, რომლის ფაილიც MHR-ში აკლია, ცალკე სტრიქონია და ძველი ეტაპის მოთხოვნას ანაცვლებს. სხვა ტიპზე
(პირადობა, ანკეტა) საკმარისია ერთი ატვირთული ფაილი ტიპის კატეგორიაში.

**გაფრთხილებები თარიღებზე** (`hrdates`, 2026-11-10; ზღვრები `mhr.warnings`: `adult_age` 18, `id_expiry_days` 30). არაფერს
ბლოკავს — `info` check `blocking=False`, ამიტომ `_summary`-ის თავში ხვდება და ავტომატურ ატვირთვას არ აჩერებს.
(1) პირადობა: `ValidationResult.facts = {birth_date, id_expiry}` ინახება ვალიდაციისას; ვადა ≤ N დღეში → `id_expiry` info
(გასული ვადა ძველებურად ბლოკავს). (2) ანკეტა: დაბადების და დაწყების თარიღი ორივე დადასტურდა და ასაკი < 18 → `age` info.
(3) ხელშეკრულება (`_contract_date_warnings`): დოკუმენტის თარიღი > ქაღალდზევე დაწერილი `contract_start` → `signed_late`;
დადასტურებული პირადობით დაწყებისას < 18 → `age`. (4) გვერდი „თანამშრომლები“: `people.overview(...)["warnings"]` =
`hrdates.person_warnings` დადასტურებული პირადობის ფაქტებით — რაც **დღეს** აქტუალურია (ვადა, არასრულწლოვანი); ძველი ვერსიით
შემოწმებულ პირადობაზე ფაქტები მისივე ტექსტიდან იკითხება (`people.identity_facts`). მთავარ გვერდზე — ამოცანა
„N თანამშრომელზე გაფრთხილებაა“ → `/people?only=warn`.

**ვინ მოაწერა ხელი ონლაინ წაკითხვისას** (`ocr/signatures.py`, 2026-11-10). ნამდვილ მონაცემებზე (`ocr.path = online`,
210 გვერდი) `signature_parties` არცერთ გვერდს არ ჰქონდა: `pageanalysis.page_parties` სიტყვების კოორდინატებს ითხოვს, Claude კი
მხოლოდ ტექსტს აბრუნებს — ამიტომ ყველა გვერდზე ეწერა „ხელმოწერა ჩანს, მხარეები ვერ გაირჩა“ და შემოწმება ფაქტობრივად სუსტი
იყო. ახლა წაკითხვის ინსტრუქცია (`claude_code_vision.PROMPT`, `anthropic_vision.SYSTEM`) ბოლოს ითხოვს ერთ სტრიქონს
`[SIGNATURES employer=yes|no|none employee=yes|no|none]`; `signatures.split` მას ტექსტიდან ჭრის (შედარებაში/ტიპში/ველებში
არ ხვდება) და `OcrResult.signature_parties`-ად აბრუნებს; `analyze_page` მას იყენებს, როცა კოორდინატები არ არის. `none` —
გასაღები აკლია ({} = ზოგადი შეფასება, როგორც ადრე). დანარჩენი უცვლელია: `signature_diffs` / `last_page_signature_diffs`
მხარეებს უკვე ამოწმებდა. ანკეტაზე: თუ მკითხველმა თქვა `employee=no` და ხელნაწერის ნიშანიც არ არის — ხელმოუწერელია.
შემოწმებულია ნამდვილ გვერდებზე (ხელშეკრულების შიდა და ბოლო გვერდი, ინსტრუქცია, პირადობა) და ხელოვნურად წაშლილი
ხელმოწერით ორივე მხარეს — ექვსივე პასუხი სწორია. ხელახალი წაკითხვა (`RECHECK_PROMPT`) ამ კითხვას არ სვამს.

**საეჭვო გვერდის ხელახალი წაკითხვა** (`validation._recheck_diffs`): ორიგინალთან/შაბლონთან შედარებამ ტექსტური
სხვაობა (`changed|added|removed`, significant) იპოვა Tesseract-ით წაკითხულ გვერდზე → ეს გვერდი (≤ `mhr.recheck.max_pages`)
ონლაინ OCR-ით (`build_ocr_chain(cfg).online`, Claude) ხელახლა იკითხება, ტექსტი `page_texts`-ში ინახება
(`text_source="ocr:<provider>:recheck"`) და შედარება იმავე `compare`-ით მეორდება. გაქრა → სიაში აღარ ჩანს; დარჩა → `_weigh_readers`
ადარებს, რა ნახა Tesseract-მა (მისი სიტყვები გვერდთან რჩება, `pt["words"]`, ამიტომ „ხელახლა შედარებაზეც“ მუშაობს). ორივე განსხვავდება
ორიგინალისგან → `RECHECK_STILL`. Tesseract-მა ორიგინალი წაიკითხა → `RECHECK_DISAGREE`, ხოლო თუ სიტყვაა (`RECHECK_DOUBT_REASONS`) და არა ციფრი ან „არ“,
significant → suspect. მიზეზი: ნამდვილ გვერდებზე Claude-მა „თანხმობას“ წაიკითხა „თანხმდება“-დ და ბარათი ამბობდა „წაკითხვის შეცდომა არ არის“. მოქმედებს მხოლოდ ონლაინ პროვაიდერის ხელმისაწვდომობისას;
`mhr.recheck.enabled=false` თიშავს. მიზეზი: რეალურ სკანზე Tesseract-მა სუფთა გვერდზე ციფრი დაკარგა („2 კვირით" →
„კვირით") და „მე-5" → „მე-3" წაიკითხა — ცრუ გაჩერებები.

**გვერდი თავიდანვე Claude-მა წაიკითხა (`ocr.path = online`, 2026-11-11):** ონლაინ მკითხველი სიტყვების კოორდინატებს არ აბრუნებს
(`words=[]`), ამიტომ `analyze_page`-ს ტექსტის ნიღაბი არ აქვს და ნაბეჭდი სტრიქონებიც „ლაქებია“: ნამდვილ ბაზაში 686 გვერდიდან 657-ს
`handwriting_suspected=True` ეწერა და ხელშეკრულება ყოველ გვერდზე ჩერდებოდა (არცერთი „მზადაა“-მდე არ მისულა). სამი წესი
(`readermarks.py`, `reader_only(pt)` = `text_source` იწყება `ocr:online`-ით, `:recheck` არ არის, `words` ცარიელია):
1. **ხელნაწერი მკითხველის ნიშნებით.** ასეთ გვერდზე `handwriting_diffs` შენახულ ნიშანს არ უყურებს — ხელნაწერი ის არის, რაც მკითხველმა
   `[ხელნაწერი: …]`-ით მონიშნა, გარდა ხელმოწერისა (აღწერით: „ხელმოწერა დამსაქმებლის მხარეს“), ბეჭდის აღწერისა, ინიციალისა და
   ხელმომწერის სახელისა და გვარისა (`_name_tokens`; მხოლოდ სახელი — MHR-ის სხვა მონაცემი, მაგ. ხელფასი, ხელით მიწერილი რომ იყოს,
   უნდა გამოჩნდეს). რიცხვი და უარყოფა ხელმოწერის აღწერაშიც ჩანს; წაუკითხავი ხელნაწერი („[ხელნაწერი: ?]“) — ჩანს. diff:
   `handwriting`, reason `HANDWRITTEN_NOTE`, `scanned` = რა წერია. ძველი გვერდები ხელახალი წაკითხვის გარეშე სწორდება („ხელახლა
   შედარება“). `analyze_page` ახალ გვერდებზე ნიშანს იმავე წესით წერს (სახელების გარეშე); `ai_review` — `readermarks.handwriting_suspected`.
2. **ბეჭედი წინადადების შუაში.** მკითხველი ბეჭედს ჩვეულებრივ `[ბეჭედი: …]` / `[შტამპი: …]` ნიშნით წერს, ზოგ გვერდზე კი ტექსტში
   (ნამდვილი: 56 გვერდიდან 2). `readermarks.stamp_words` ამ სკანის ყველა ბეჭდის ნიშნის სიტყვებს აგროვებს და `compare_texts(stamp=…)`
   მხოლოდ მათგან (ან `known`-ისგან) შემდგარ სუფთა ჩამატებას ბეჭდად თვლის (`_stamp_word`: ≥8-ნიშნა ნომერში ერთი ციფრის სხვაობა
   დასაშვებია — კოდის ბოლო ციფრს მკითხველი გვერდიდან გვერდზე სხვანაირად კითხულობს; მოკლე რიცხვი ზუსტად). უარყოფა ამით არ იმალება.
3. **მეორე მკითხველი** (`_local_second_reading`): Claude 14-გვერდიან ხელშეკრულებაში დაახლოებით ერთ სიტყვას მსგავსი მნიშვნელობის
   სხვა სიტყვით კითხულობს („დაეკისრება“ → „დაექვემდებარება“, „ყოველი“ → „ყველა“; ქაღალდზე ორიგინალია — შემოწმდა სურათზე).
   `RECHECK_DOUBT_REASONS`-ის სხვაობაზე იმავე გვერდს ადგილობრივი Tesseract კითხულობს (≤ `mhr.recheck.max_pages`, ~10–25 წმ, გარეთ
   არაფერი იგზავნება), ტექსტი `page_texts.local_text`-ში ინახება და `_weigh_readers` იმავე წესით წყვეტს: Tesseract-მა ეს ადგილი
   ორიგინალივით წაიკითხა → `RECHECK_DISAGREE`, significant → suspect; მანაც სხვაობა ნახა → უცვლელი. რიცხვი და „არ“ ყოველთვის
   აჩერებს (მათთვის Tesseract არც ეშვება). `_only_claude_differs` მხოლოდ იმავე და მეზობელ გვერდს უყურებს (იგივე სიტყვა შორ
   გვერდზე სხვა შეცდომით — „ყველი“ — ამ ადგილზე არაფერს ამბობს).
`signature_hint` ასეთ გვერდებზე ისევ ნიღბის გარეშეა გამოთვლილი (თითქმის ყოველთვის `present`) — ხელმოწერას მკითხველის პასუხი
(`signature_parties`) ამოწმებს; როცა მან `none` თქვა, ბოლო გვერდის შემოწმება ამ სუსტ ნიშანს ეყრდნობა (იხ. შეზღუდვები).

**პატარა ცვლილებები (2026-11-05, შეცვლილი ნამდვილი სკანების ტესტიდან):** `_negation_change` — „არ"… და „არა-" ყოველთვის
significant. `_word_change` — 1-2 ნამდვილი სიტყვის ჩამატება/ამოღება და `_far_word`: Tesseract-ის გვერდზე `TextDiff.suspect=True`
(`_recheck_diffs` კითხულობს, significant-ის შემდეგ, ბიუჯეტის ფარგლებში), `trusted_pages`-ზე (`validation.trusted_pages`: ტექსტი
Tesseract-ის არ არის) უკვე significant. დარჩენილი suspect → `_summary`-ში „ვერ გადამოწმდა", არა ბლოკი. მიზეზი: 44 სუფთა ნამდვილ გვერდზე
Claude მთელ სიტყვას არ კარგავს, ერთ ასოს კი ურევს, ამიტომ ერთი ასო ხმაურად დარჩა. ბოლო გვერდი: `_table_noise`-ს ემატება
`_table_counts_kept` (`_kept`: სვეტების გადანაცვლება რაოდენობას არ ცვლის; ძველი რიცხვის სხვაგან არსებობა აღარ მალავს ცვლილებას).
`_digits_match`-ს ≥10-ნიშნა ნომრის ერთი ციფრის ტოლერანტობა მოეხსნა. `_strip`: Claude-ის ორ ხაზად დაწერილი სათაური და კიდეზე სათაურის
ნაწილი იშლება. `_reading_broke`: Claude-ის ტექსტი ცარიელია ან 40 სიტყვა ზედიზედ მეორდება → ხელახლა, მერე უარი.

**შეცდომების მდგომარეობები (2026-11-05, ნამდვილი ფონური გაშვებიდან):** `status="unavailable"` ან `error="MhrError"` (მაგ. ტოკენი) ნიშნავს,
რომ არაფერი შემოწმებულა (`ui._mhr_did_not_answer`): გვერდი „ვერ მოიძებნა“/„ადგილზეა“ ნაბიჯებს და შედარების ასაცილებელ ღილაკს არ აჩვენებს. ტოკენის შენახვა
(`/settings/secret`, MHR-ის გასაღები) `mhr_queue.revalidate_refused`-ით ასეთ ფაილებს თავიდან აყენებს რიგში. ველის პრობლემა (ხელფასი, თარიღი)
`fields[...].page`/`raw`-ით ჩანს თავის გვერდზე (`ui._field_place`; გვერდი სკანის ნომრითაა, ფაილის გვერდად გადადის).

**ხელმოწერის ბლოკი** (`pageanalysis.signature_block`, `block_parties`, `page_parties`): ქვედა სტრიქონის წარწერები არ არის → გვერდის
შუის ორი სვეტი. Tesseract წარწერების სტრიქონს ხანდახან ტოვებს, ამიტომ დასაყრდენები მეტია: მარცხნივ „დამსაქმებელი", „დირექტორი", „შპს",
„ს/ნ"; მარჯვნივ „სტაჟიორი"/„დასაქმებული", „პ/ნ", 11-ნიშნა ნომერი, წინ ≥0.4in ცარიელი ადგილით. ნიშანი = ხელმოწერის ზომის ლაქა სვეტში, ბლოკიდან გვერდის 90%-მდე.

**სად, რა, რატომ ვებ-გვერდზე** (`ui/app.py`, 2026-11-05): `PageText.words/marks/signature_boxes` — OCR სიტყვები, ნიშნები და ხელმოწერის ადგილები გვერდის მეათასედებში, `/page/{id}/{i}.png?dpi=&crop=x0,y0,x1,y1` სურათს იმავე მიმართულებით აბრუნებს (rotation + deskew), ამიტომ მონიშვნა ზუსტად ჯდება. `TextDiff.orig_parts/scan_parts/clause/boxes`: ზუსტი სიტყვები, პუნქტი და ადგილი (`textcompare.locate`, `validation._place_diffs`). გვერდები: `_problem_card`, `_check_card`, `_checked_block`, `_page_map`, `/mhr/{id}/page/{n}` (`_page_view`), `_mhr_parts_block` + `page_match`. სიის მიზეზი `_main_reason` (ადრე summary-ის პირველი ხაზი, ხშირად შენიშვნა). `_scan_hints`: ფურცელი ორჯერ გავიდა / რიგი აირია — შენიშვნა. `MATCH_RESOLVED_REASONS`-ს დაემატა `uncertain_boundary`: საზღვარს MHR-ის ფაილებთან შედარება ხსნის. `MHR_ERROR_KA`: MHR-ის კოდები ადამიანის ენაზე.

**ტესტირება ნამდვილ MHR-ის კოდზე სერვერის გარეშე:** `origin/main`-ის კოდი ცალკე საქაღალდეში, ცარიელი MariaDB Docker-ში, `httpx.MockTransport` ყოველ მოთხოვნას ერთჯერად `php replay.php`-ით ატარებს Laravel-ის kernel-ში (მარშრუტი, ტოკენი, კონტროლერი, ბაზა). ასე შემოწმდა ატვირთვა, `signed_copy_of`/`signed_at`, დაკარგული პასუხი, გათიშვა და ხელით ატვირთვა.

**ფაილი უკვე ატვირთულ PDF-შია** (`validation._inside_uploaded`): მხოლოდ „მზადა" ფაილზე. `kind: uploaded` (გარდა საკუთარი და ამ პროგრამის
ნაწილისა, `metadata.part`) → `_pdf_pages(dpi=150, local_only=True)`, sha256-ით მეხსიერებაში → `plan_pages` → ამ ორიგინალის გვერდები არის →
`already_uploaded` ბლოკირებადი. `signed_copy_of` მხოლოდ ერთ ორიგინალს უთითებს, ამიტომ მთელი პაკეტის ატვირთვა მის მიხედვით არ ჩანდა.

**რეალური სკანებიდან (2026-11-03) დამატებული ხმაურის წესები:** ბეჭდის/ხელმოწერის ლაქაში Tesseract-ის ≥0.6 სანდოობის
ნაგავი („2099/3)", „/7", „LI") `drop_stamp_words`-ში იშლება — რჩება ნამდვილი ქართული სიტყვა, თარიღი/პ.ნ. და სანდო სუფთა
ტოკენი (≥4 ციფრი, „ს/ნ", ელ-ფოსტა — ბეჭედი რეკვიზიტებს ფარავს); სუფთა ჩამატება მხოლოდ MHR-ის ცნობილი ტოკენებით („2009",
ს/კ — `known_tokens(employee)` ახლა ორიგინალთან შედარებასაც გადაეცემა) ბეჭედია; რეკვიზიტების ცხრილში ≤1 უცნობი მოკლე (≤4 ასო)
ტოკენი („ალლი" — ხელმოწერის ნაკაწრი) იტანება; ბოლო გვერდის გრძელი სტრიქონი შაბლონში კომპაქტურ ფანჯარაში ეძებება
(`_best_match` — „ქ. თბილისი, ვარდების ქ." პრეამბულაშიც არის); პუნქტის ნომერთან შეწებებული გამჭოლი სათაური („10.6 ქ. თბილისი
2026-11-01") `_strip`-ში იშლება; `find_doc_number` მისამართის ხაზს („ქუჩა #17") გამოტოვებს. `claude -p` ყველგან
`--safe-mode`-ით ეშვება — მომხმარებლის CLAUDE.md-ის პირადი წესები („ბოლოს შეჯამება") ტრანსკრიფციას/JSON-ს არ ემატება.

**AI ანალიზი** (`ai_review.py`, ნაბიჯი b2): `ready|failed` ჩანაწერებზე, სადაც `mhr_items.ai_review IS NULL`, `sweep`
`review_fn or ai_review.review_document`-ს უშვებს (≤ `mhr.ai_review.per_sweep` ერთ გავლაზე) და შედეგს
(`AiReview.to_dict()`: verdict upload|check|reject|error, confidence, summary, items, risks) `ai_review` სვეტში წერს.
სტატუსს არ ცვლის — მხოლოდ UI-ში სთავაზობს. მოქმედებს მხოლოდ `ai_review.enabled(cfg)`-ზე (ocr.online.enabled +
provider=claude_code + mhr.ai_review.enabled). Claude Code-ს `claude -p --tools "" --model opus` ეძახის, პრომპტი stdin-ით;
შეცდომაც იწერება (verdict=error), რომ უსასრულოდ არ გამეორდეს. `revalidate` და ხელახალი ვალიდაცია `ai_review`-ს ასუფთავებს;
`rerun_ai_review` — ოპერატორის „ხელახალი AI ანალიზი“.

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
  მსგავსება ≥ 0.85, ციფრები ზუსტად), დასკანერებულიდან — გვერდის ნომერი. თუ docx-ის header-ში სურათია (`docx_header_picture` →
  `OriginalText.header_picture`, ბლანკის ლოგო), დასკანერებული გვერდის თავში სათაურის პირველი ხაზის ზემოთ მდგომი ≤2 ხაზი (სულ ≤3
  სიტყვა, ციფრების გარეშე) სიტყვებად წაკითხული ლოგოა და იშლება (`_logo_rows`, 2026-11-11): Claude ლოგოს ყოველ გვერდზე სხვანაირად
  კითხულობს, ამიტომ გამჭოლ ხაზად ვერ იპოვება.
- `compare_texts` (PDF, გვერდობრივი): გვერდების რაოდენობის სხვაობა → significant; სიტყვების დონის diff, სადაც **significant** = ციფრების
  შემცველი ცვლილება, ≥3 სიტყვის ახალი/ამოღებული მონაკვეთი, ან სიტყვა, რომლის მსგავსება < 0.6; სხვა — OCR-ის ხმაური
  (significant=False). ხელნაწერის ეჭვი (page_texts.handwriting_suspected ან marks არახელმოწერის ზონაში; Claude-ით წაკითხულ გვერდზე —
  მკითხველის `[ხელნაწერი: …]` ნიშნები, იხ. ზემოთ) → `handwriting`, significant.
- status: passed ⇔ არც ერთი ბლოკირებადი mismatch და არც ერთი significant diff. summary — ქართულად, ოპერატორისთვის.
- სკანირებული ტექსტი მონაცემია (prompt-injection-ს ეფექტი არ აქვს). პირადი მონაცემები ტექნიკურ ჟურნალში არ იწერება.
