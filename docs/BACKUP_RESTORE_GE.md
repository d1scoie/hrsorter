# სარეზერვო ასლი და აღდგენა

ორიგინალების არქივი (`_Archive`) **არ არის** სარეზერვო ასლის შემცვლელი — ის იმავე დისკზეა.

## რა უნდა დაარეზერვო
| რა | სად | რატომ |
|---|---|---|
| მდგომარეობის ბაზა | `<Root>\_State\hrsorter.sqlite3` (+ `-wal`, `-shm`). თუ `<Root>` ქსელურ დისკზეა (მაგ. `Z:`) — `C:\ProgramData\HRSorter\state\<root>\` (ზუსტი ბილიკი: პარამეტრები → მთავარი საქაღალდე) | რიგი, დოკუმენტები, გვერდების აღრიცხვა, ისტორია |
| კონფიგურაცია | `config\config.json`, `config\document_types.json` | პარამეტრები და ტიპები |
| ორიგინალები | `<Root>\_Archive` | უცვლელი წყაროები (sha256 სახელით) |
| შედეგები | `<Root>\Sorted`, `Proposed`, `NeedsReview`, `_Superseded`, `Reports` | PDF + JSON გვერდითი ფაილები |

გასაღებები Windows Credential Manager-შია და ფაილურ ასლში არ ხვდება — შეინახე ცალკე, დაცულ ადგილას.

## ბაზის თანმიმდევრული ასლი (პროცესის გაჩერების გარეშე)
```powershell
.venv\Scripts\python.exe -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close()" "D:\HRScan\_State\hrsorter.sqlite3" "E:\Backup\hrsorter-$(Get-Date -Format yyyyMMdd).sqlite3"
```
SQLite-ის `backup` API თანმიმდევრულ ასლს იღებს მაშინაც კი, როცა worker მუშაობს.

## ფაილების ასლი
```powershell
robocopy "D:\HRScan" "E:\Backup\HRScan" /MIR /XD "_Work" "_State" /R:2 /W:5 /LOG:"E:\Backup\robocopy.log"
copy config\config.json, config\document_types.json E:\Backup\config\
```
რეკომენდაცია: ყოველდღიური ასლი სხვა დისკზე/სერვერზე და კვირაში ერთხელ — კომპიუტერის გარეთ.
ასლის საქაღალდეზეც დააწესე იგივე წვდომის შეზღუდვები (იხ. INSTALL_GE.md).

## აღდგენა
1. გააჩერე პროცესები: დახურე „HR Sorter worker"/„UI" ფანჯრები ან `Stop-Process -Name python* ` (ფრთხილად — სხვა Python პროცესებიც გაჩერდება).
2. დააბრუნე ბაზა: `copy E:\Backup\hrsorter-YYYYMMDD.sqlite3 D:\HRScan\_State\hrsorter.sqlite3` (წაშალე ძველი `-wal`/`-shm` ფაილები).
3. დააბრუნე `config\*.json` და `_Archive`, `Sorted`, … საქაღალდეები (`robocopy E:\Backup\HRScan D:\HRScan /E`).
4. გაუშვი `scripts\start.cmd`. გაშვებისას worker:
   - `running` სამუშაოებს ვადაგასული lease-ით აბრუნებს რიგში (ავარიის აღდგენა);
   - `publishing` მდგომარეობაში გაჭედილ დოკუმენტებს ასრულებს ან თავიდან წერს (დუბლიკატის გარეშე);
   - ინბოქსში დარჩენილ ფაილებს შიგთავსის ჰეშით ადარებს — უკვე დამუშავებული ხელახლა არ დამუშავდება.
5. შეამოწმე: `.venv\Scripts\python.exe -m hrsorter.cli status`.

## მთლიანობის შემოწმება
არქივის ფაილის სახელი თავადაა მისი sha256. შემოწმება:
```powershell
Get-ChildItem D:\HRScan\_Archive -Recurse -Filter *.pdf | ForEach-Object { if ((Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower() -ne $_.BaseName) { "BAD: $($_.FullName)" } }
```
ყოველი შედეგის JSON-ში ჩაწერილია `source_sha256`, `output_sha256` და `source_page_ranges` — შედეგი ყოველთვის უკავშირდება წყაროს.
