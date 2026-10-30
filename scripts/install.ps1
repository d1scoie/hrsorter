# HR Sorter - Windows install script (PowerShell 5.1+). ASCII-only on purpose (PS 5.1 encoding).
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Root "D:\HRScan" [-Autostart]
param(
    [string]$Root = "C:\HRScan",
    [switch]$Autostart
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$proj = Split-Path -Parent $here
Set-Location $proj

# 1) Python 3.12+ : prefer the py launcher, then python on PATH
$py = $null
foreach ($cand in @("py -3.13", "py -3.12", "python")) {
    try {
        $v = & cmd /c "$cand -c ""import sys;print(sys.version_info>=(3,12))""" 2>$null
        if ($v -eq "True") { $py = $cand; break }
    } catch {}
}
if (-not $py) { throw "Python 3.12+ not found. Install from https://www.python.org/downloads/windows/ and re-run." }
Write-Host "Using: $py"

# 2) virtual environment + pinned dependencies
if (-not (Test-Path ".venv\Scripts\python.exe")) { & cmd /c "$py -m venv .venv"; if ($LASTEXITCODE -ne 0) { throw "venv failed" } }
& .venv\Scripts\python.exe -m pip install --upgrade pip | Out-Null
& .venv\Scripts\python.exe -m pip install -r requirements.lock
if ($LASTEXITCODE -ne 0) { throw "dependency install failed" }
& .venv\Scripts\python.exe -m pip install --no-deps -e .
if ($LASTEXITCODE -ne 0) { throw "package install failed" }

# 3) config + folders
$env:PYTHONUTF8 = "1"
& .venv\Scripts\python.exe -m hrsorter.cli init --root $Root
if ($LASTEXITCODE -ne 0) { throw "init failed" }

# 4) Tesseract (OCR for scanned pages). Install once: winget install --id UB-Mannheim.TesseractOCR -e
#    Language data goes to an ASCII-only folder: Tesseract on Windows cannot read non-ASCII paths.
$tess = "C:\Program Files\Tesseract-OCR\tesseract.exe"
$tessdata = "C:\ProgramData\hrsorter\tessdata"
if (Test-Path $tess) {
    New-Item -ItemType Directory -Force $tessdata | Out-Null
    foreach ($l in @("kat", "eng", "osd")) {
        $dst = Join-Path $tessdata "$l.traineddata"
        if (-not (Test-Path $dst)) {
            $src = "C:\Program Files\Tesseract-OCR\tessdata\$l.traineddata"
            if (($l -ne "kat") -and (Test-Path $src)) { Copy-Item $src $dst }
            else { Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/tesseract-ocr/tessdata_best/raw/main/$l.traineddata" -OutFile $dst }
        }
    }
    $langs = & $tess --tessdata-dir $tessdata --list-langs 2>$null
    if ($langs -match "^kat$") { Write-Host "Tesseract ready with Georgian (kat): $tessdata" } else { Write-Warning "Georgian (kat) language data missing in $tessdata" }
} else {
    Write-Warning "Tesseract not found: scanned pages will go to NeedsReview (ocr_unreadable). Install: winget install --id UB-Mannheim.TesseractOCR -e"
}

# 5) optional: autostart at Windows logon (no admin needed)
if ($Autostart) { & .venv\Scripts\python.exe -m hrsorter.cli autostart install }

Write-Host "Done. Start: scripts\start.cmd   UI: http://127.0.0.1:8765"
