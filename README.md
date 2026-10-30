# HR დოკუმენტების ადგილობრივი გადამრჩევი (hrsorter)

Windows-ის ერთ კომპიუტერზე მომუშავე პროგრამა, რომელიც სკანერის საქაღალდეში შენახულ ქართულენოვან PDF-ებს:
აღმოაჩენს → უცვლელად აარქივებს → საჭიროებისას დოკუმენტებად ყოფს → ტიპს ამოიცნობს → მონაცემებს ამოიღებს →
ასათაურებს და ანაწილებს `კომპანია / ტიპი / წელი` სტრუქტურაში. გაურკვეველი შემთხვევები გამოიყოფა მიზეზით.

> **სტატუსი: ეტაპი 1–4 განხორციელებულია; ეტაპი 5 დაწყებულია — პირველი რეალური ნიმუში (4 ხელშეკრულება, 68 გვ.) გაზომილია, იხ. PILOT_REPORT_GE.md §3ა. სტატისტიკურად სანდო შეფასებისთვის საჭიროა ≥200 დოკუმენტი სხვადასხვა ტიპით.**
> სიზუსტე რეალურ დოკუმენტებზე **არ არის გაზომილი**. ციფრები `docs/PILOT_REPORT_GE.md`-ში მხოლოდ სინთეზურ
> ნაკრებს ეხება. ნაგულისხმევად ჩართულია საცდელი რეჟიმი (საბოლოო ავტომატური განაწილება გამორთულია).

## დოკუმენტაცია
| ფაილი | შინაარსი |
|---|---|
| `docs/INSTALL_GE.md` | ინსტალაცია, სკანერის საქაღალდე, batch/single რეჟიმი, Tesseract, წვდომის უფლებები |
| `docs/USAGE_GE.md` | ყოველდღიური მუშაობა, ინტერფეისი, მიზეზის კოდები, ახალი ტიპის დამატება |
| `docs/BACKUP_RESTORE_GE.md` | სარეზერვო ასლი და აღდგენა |
| `docs/PILOT_REPORT_GE.md` | შეფასების მეთოდი, ადგილობრივი/ონლაინ შედარება, სინთეზური შედეგები, შეზღუდვები |
| `docs/LIMITATIONS_GE.md` | ცნობილი შეზღუდვები |
| `docs/ARCHITECTURE.md` | მოდულები და მათი კონტრაქტები |
| `hrsorter/schema/document_sidecar.schema.json`, `docs/sidecar_example.json` | JSON მეტამონაცემების სქემა და ნიმუში |

## სწრაფი დაწყება
```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Root "D:\HRScan" -Autostart
scripts\start.cmd          # worker + UI → http://127.0.0.1:8765
```

## ტესტები
```powershell
$env:PYTHONUTF8 = "1"
.venv\Scripts\python.exe -m pytest -q -m "not slow"   # სწრაფი ნაკრები
.venv\Scripts\python.exe -m pytest -q -m slow         # 500 გვერდი / 500 ფაილი
```

## შეფასება (სიზუსტის გაზომვა)
```powershell
.venv\Scripts\python.exe -m tools.synthetic --out D:\eval\synth --batch-docs 40 --singles 30 --seed 2
.venv\Scripts\python.exe -m tools.evaluate --gt D:\eval\synth\ground_truth.json --work D:\eval\work --out D:\eval\report.json
```
რეალური ნაკრებისთვის `ground_truth.json` ხელით მოინიშნება (ფორმატი — `tools/evaluate.py`-ის თავში).

## სტრუქტურა
```
hrsorter/        პროგრამა (watcher, worker, pipeline, ocr/, classifier, boundary, extractor, decision, output, ui/ ...)
config/          კონფიგურაციისა და ტიპების მაგალითები (საიდუმლოებების გარეშე), სინთეზური თანამშრომლები
tools/           synthetic.py — სატესტო PDF-ების გენერატორი; evaluate.py — შეფასება
tests/           ავტომატური ტესტები
scripts/         install.ps1, start.cmd
docs/            ინსტრუქციები და ანგარიშები
requirements.lock  დამოკიდებულებების ზუსტი ვერსიები
```
