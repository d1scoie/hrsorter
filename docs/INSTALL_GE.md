# ინსტალაცია Windows-ზე

## მარტივი გზა: ინსტალერი (`HRSorter-Setup-<ვერსია>.exe`)
Python, Tesseract და ქართული ენა ინსტალერშივეა ჩაშენებული, სუფთა Windows 10/11 (64-bit) კომპიუტერზე სხვა არაფერია საჭირო.
1. გაუშვი `HRSorter-Setup-0.1.0.exe` (საჭიროა ადმინისტრატორის დადასტურება). ხელმოწერა ჯერ არ აქვს, ამიტომ Windows
   SmartScreen-მა შეიძლება აჩვენოს „Windows protected your PC“ → **More info → Run anyway**.
2. აირჩიე სამუშაო საქაღალდე (ნაგულისხმევი `C:\HRScan`, მხოლოდ ლათინური ასოები).
3. დასრულების შემდეგ ბრაუზერში იხსნება სტატისტიკის გვერდი. დაწყების მენიუში ჩნდება მალსახმობი „HR გადამრჩევი“.
   ფონური დამუშავება ყოველი Windows-ში შესვლისას ავტომატურად ირთვება.
4. **პარამეტრებში** დაამატე შენი კომპანია(ები) და ნაგულისხმევი კომპანია. ცარიელი სიის შემთხვევაში ყველა დოკუმენტი
   `company_unrecognized`-ით გადასამოწმებელში წავა.
5. სკანერი დააყენე `C:\HRScan\Inbox\Batch`-ზე (იხ. ქვემოთ „სკანერის საქაღალდის არჩევა“). საქაღალდის ქსელში
   გაზიარება სკანერისთვის ხელით ხდება (Properties → Sharing). ჩაწერის უფლება მიეცი მხოლოდ სკანერის ანგარიშს.

ადგილები: პროგრამა — `C:\Program Files\HRSorter`; კონფიგურაცია და ჟურნალი — `C:\ProgramData\HRSorter`;
დოკუმენტები — არჩეული სამუშაო საქაღალდე. წაშლა (Uninstall) დოკუმენტებსა და კონფიგურაციას **არ** შლის.
ადმინისტრატორისთვის კონსოლის ვერსია: `"C:\Program Files\HRSorter\hrsorter.exe" status`.
ჩუმი ინსტალაცია (IT-სთვის): `HRSorter-Setup-0.1.0.exe /VERYSILENT /SUPPRESSMSGBOXES`.

**ინსტალერის აწყობა** (დეველოპერისთვის): `.venv\Scripts\python.exe packaging\build.py` →
`build_out\HRSorter-Setup-<ვერსია>.exe`. საჭიროა PyInstaller (venv-ში), pefile, Tesseract (UB Mannheim) და
`C:\ProgramData\hrsorter\tessdata`-ში `kat/eng/osd`, Inno Setup 6. დეტალურად — ქვემოთ, „ახალი ვერსიის გამოშვება“.

## ავტომატური განახლება
დაინსტალირებული პროგრამა ჩატვირთვიდან 10 წუთში და შემდეგ ყოველ 4 საათში ამოწმებს GitHub-ის რეპოზიტორიას
`d1scoie/hrsorter-releases` (Task Scheduler → `HRSorter\Update`, SYSTEM-ის სახელით). თუ იქ უფრო ახალი ვერსიაა
და რიგი ცარიელია, ის ჩამოტვირთავს Setup-ს, ამოწმებს sha256-ს და ჩუმად აყენებს. პარამეტრები და დოკუმენტები ადგილზე
რჩება. დასრულების შემდეგ პროგრამა თავიდან ირთვება შესულ მომხმარებლებთან (`HRSorter\Start`).
ჟურნალი: `C:\Program Files\HRSorter\_update\update.log` (და `setup.log`). ხელით შემოწმება: `hrsorter.exe update check`.

**ახალი ვერსიის გამოშვება:**
1. `hrsorter\__init__.py`-ში გაზარდე `PIPELINE_VERSION` (მაგ. `0.2.0`) და ვერსია `docs\LIMITATIONS_GE.md`-ის სათაურში.
   განახლება ხდება მხოლოდ მაშინ, როცა ვერსია უფრო მაღალია. `packaging\version_info.txt`-ს `build.py` თავად ასწორებს.
2. აწყობის გარემო — **Python 3.13 და `requirements.lock`-ის ზუსტი ვერსიები**. 0.1.10 და 0.1.11 ასე აიწყო; სხვა Python-ით
   ან lock-ის გარეშე აწყობილი ინსტალერი ყველა კომპიუტერზე სხვა ბიბლიოთეკებს დააყენებს, ვიდრე ტესტებში იყო.
   ```powershell
   py -3.13 -m venv .venv
   .venv\Scripts\python.exe -m pip install -r requirements.lock
   .venv\Scripts\python.exe -m pip install -e ".[dev,build]" -c requirements.lock
   .venv\Scripts\python.exe -m pytest -q -m "not slow"
   ```
   Inno Setup ადმინის გარეშეც დგება: `winget install --id JRSoftware.InnoSetup -e --scope user`.
3. Tesseract: თუ ცალკე დაყენებული არ არის, `build.py` მას უკვე დაინსტალირებული HR Sorter-იდან აიღებს — ეს ზუსტად
   ის ფაილებია, რაც წინა ვერსიას მოჰყვა:
   ```powershell
   $env:HRSORTER_TESSERACT_DIR = "C:\Program Files\HRSorter\tesseract"
   $env:HRSORTER_TESSDATA_DIR  = "C:\Program Files\HRSorter\tesseract\tessdata"
   .venv\Scripts\python.exe packaging\build.py
   ```
4. გამოქვეყნებამდე შეამოწმე აწყობილი პროგრამა: `build_out\dist\HRSorter\hrsorter.exe update check` უნდა
   დაწეროს „უახლესი ვერსიაა (<ახალი ვერსია>)“ — ეს ამოწმებს ვერსიას და HTTPS-ს (OpenSSL) ერთად.
5. `gh release create v0.2.0 build_out\HRSorter-Setup-0.2.0.exe -R d1scoie/hrsorter-releases --title "HR Sorter 0.2.0" --notes-file notes.md`
   ჯერ სატესტოდ რომ გამოუშვა, დაამატე `--prerelease`. ასეთ ვერსიას პროგრამა არ აყენებს, სანამ GitHub-ზე
   „Set as latest release“ არ მონიშნავ. **გამოქვეყნება ნიშნავს, რომ ყველა კომპიუტერი მას 4 საათში დააყენებს.**

GitHub-ის ანგარიშზე წვდომის მქონე ადამიანი ყველა კომპიუტერზე დააყენებს იმას, რასაც ატვირთავს. ამიტომ ანგარიშზე
ორფაქტორიანი შესვლა (2FA) აუცილებელია.

## ხელით ინსტალაცია (დეველოპერისთვის)

## მოთხოვნები
- Windows 10/11, ერთი კომპიუტერი. ადმინისტრატორის უფლება საჭირო არ არის (გარდა Tesseract-ის დაყენებისა).
- Python 3.12 ან უფრო ახალი — https://www.python.org/downloads/windows/ (ინსტალაციისას მონიშნე „Add python.exe to PATH" ან გამოიყენე `py` launcher).
- დისკი: პროგრამა ~300 MB (Python გარემო). შედეგებისთვის — სკანების მოცულობის ~2-3-ჯერადი ადგილი
  (არქივი + დაყოფილი PDF-ები). ზუსტი მოთხოვნები საპილოტე გაზომვის შემდეგ დაზუსტდება.
- **სკანირებული (ტექსტური ფენის გარეშე) გვერდებისთვის საჭიროა OCR.** რეკომენდებული ადგილობრივი ძრავა: Tesseract
  (Apache-2.0 ლიცენზია): `winget install --id UB-Mannheim.TesseractOCR -e`.
  ქართული ენა (`kat`) Tesseract-ს თავიდან **არ მოყვება**. მას `scripts\install.ps1` ჩამოტვირთავს ოფიციალური
  `tessdata_best` რეპოზიტორიიდან `C:\ProgramData\hrsorter\tessdata`-ში (config: `ocr.tessdata_dir`).
  **ბილიკში მხოლოდ ლათინური ასოები** უნდა იყოს, რადგან Windows-ზე Tesseract ქართულ ბილიკს ვერ კითხულობს.
  Konica Minolta-ს სკანებს (`SKM_*.pdf`) ტექსტური ფენა არ აქვთ, ამიტომ OCR აუცილებელია.
  ქართული ხელნაწერის ამოცნობა Tesseract-ით **არ არის გარანტირებული**.
  Tesseract-ის გარეშე პროგრამა მუშაობს, მაგრამ ტექსტური ფენის არმქონე გვერდები გადავა `NeedsReview/ocr_unreadable`-ში.

## დაყენება (ავტომატური სკრიპტით)
PowerShell-ში, პროექტის საქაღალდიდან:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Root "D:\HRScan" -Autostart
```
სკრიპტი: ქმნის `.venv`-ს, აყენებს `requirements.lock`-ის ზუსტ ვერსიებს, ქმნის `config\config.json`-ს და
`config\document_types.json`-ს მაგალითებიდან, ქმნის სამუშაო საქაღალდეებს და (`-Autostart`-ით) ავტოგაშვებას.

## დაყენება (ხელით, განმეორებადი აწყობა)
```powershell
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock
.venv\Scripts\python.exe -m pip install --no-deps -e .
$env:PYTHONUTF8 = "1"
.venv\Scripts\python.exe -m hrsorter.cli init --root "D:\HRScan"
```
ონლაინ ამოცნობის შედარებისთვის (არასავალდებულო): `.venv\Scripts\python.exe -m pip install anthropic==2.3.0`.
API-გასაღების ნაცვლად საკუთარი Claude-გამოწერით (ნაგულისხმევი, `ocr.online.provider = "claude_code"`): დააყენე Claude Code, მერე პარამეტრებში „Claude-ის ანგარიში → შესვლა“ დააჭირე — გაიხსნება Claude-ის ფანჯარა და ბრაუზერში ავტორიზაციას გაივლი (გასაღები არ სჭირდება; ინდივიდუალური გამოყენებისთვის).

## სკანერის საქაღალდის არჩევა
`init` ქმნის ორ ინბოქსს (თითოეულს თავისი რეჟიმით):

| საქაღალდე | რეჟიმი | როდის |
|---|---|---|
| `<Root>\Inbox\Batch` | `batch_pdf` | ერთ PDF-ში ბევრი დოკუმენტი ზედიზედ არის დასკანერებული |
| `<Root>\Inbox\Single` | `single_document_pdf` | თითო PDF = ერთი სრული დოკუმენტი (არ დაიყოფა) |
| ნებისმიერი (მაგ. `Z:\`) | `auto` | სკანერის ერთი საქაღალდე: 1 გვერდი → ცალკე დოკუმენტი, მეტი → ნაკრები (დაიყოფა) |

სკანერის/MFP-ის პარამეტრებში (Scan to Folder / SMB / ქსელური საქაღალდე) მიუთითე შესაბამისი საქაღალდე.
შეგიძლია სკანერზე ორი პროფილი შექმნა („HR — ნაკრები" → Batch, „HR — ცალკე" → Single).
სკანერის ფორმატი: PDF, 300 DPI, შავ-თეთრი ან ნაცრისფერი. საქაღალდეს გაუზიარე წვდომა მხოლოდ სკანერის ანგარიშსა და ოპერატორს.

**სკანერის პარამეტრები:** პირველმა რეალურმა ტესტმა (Konica Minolta bizhub C458) აჩვენა, რომ ეს აუცილებელია.
- **„ფაილის გვერდებად დაყოფა“ (Page Separation / File per page) გამორთე.** სხვაგვარად 14-გვერდიანი ხელშეკრულება 14 ცალკე
  ფაილად მოვა და `Single` საქაღალდეში ზოგი გვერდი სრულ დოკუმენტად ჩაითვლება. დასტა ერთ PDF-ად გაგზავნე `Batch`-ში.
  `Single`-ში გაგზავნე მხოლოდ მაშინ, როცა ერთ სკანში ერთი სრული დოკუმენტია.
- **„კომპაქტური PDF“ / მაღალი შეკუმშვა გამორთე.** ასეთი შეკუმშვისას ტექსტი 150 DPI ფონის სურათში ხვდება და
  მკრთალი ასოები იკარგება. სჯობს ჩვეულებრივი PDF, 300 DPI, შავ-თეთრი ან ნაცრისფერი.
- **ორიგინალის ხარისხი:** მკრთლად დაბეჭდილ (დაბალი ტონერით) დოკუმენტს OCR ვერ კითხულობს. პროგრამა მას გადასამოწმებლად
  გადადებს, მაგრამ ამოცნობა ვერ მოხდება.

რეჟიმის შეცვლა: ვებინტერფეისი → „პარამეტრები" → „საქაღალდეები და რეჟიმები", ან `config\config.json` → `profiles`.
პირველ ვერსიაში ორივე ფორმის ერთ საქაღალდეში შერევა არ არის მხარდაჭერილი: ასეთი ეჭვი გამოიყოფა `mode_mismatch`-ით.

## გაშვება
- ორივე პროცესი ერთად: `scripts\start.cmd` (ფონური დამმუშავებელი + ინტერფეისი, ბრაუზერში იხსნება http://127.0.0.1:8765).
- ცალ-ცალკე: `.venv\Scripts\python.exe -m hrsorter.cli worker` და `.venv\Scripts\python.exe -m hrsorter.cli ui`.
- ავტოგაშვება Windows-ში შესვლისას: `.venv\Scripts\python.exe -m hrsorter.cli autostart install` (გამორთვა: `uninstall`).
  ინტერფეისის (ბრაუზერის) დახურვა ფონურ დამუშავებას არ აჩერებს.

## წვდომის უფლებები (მნიშვნელოვანია)
ფაილის სახელებში პირადი ნომრებია. `<Root>` საქაღალდეზე დატოვე წვდომა მხოლოდ HR ოპერატორს:
```powershell
icacls "D:\HRScan" /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F"
```
(სკანერის ანგარიშს მიეცი ჩაწერის უფლება მხოლოდ `Inbox`-ზე.) ინტერფეისი იხსნება მხოლოდ `127.0.0.1`-ზე და გარედან მიუწვდომელია.

## გასაღებები
გასაღებები/პაროლები config-ში **არ** იწერება — მხოლოდ Windows Credential Manager-ში:
```powershell
.venv\Scripts\python.exe -m hrsorter.cli secret set hrsorter/employee_api
.venv\Scripts\python.exe -m hrsorter.cli secret set hrsorter/online_ocr
```
