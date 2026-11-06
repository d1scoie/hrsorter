"""Windows ინსტალერის აწყობა ერთი ბრძანებით:  .venv\\Scripts\\python.exe packaging\\build.py

ნაბიჯები: PyInstaller (dist\\HRSorter) → Tesseract-ის საჭირო ფაილები + kat/eng/osd → Inno Setup →
build_out\\HRSorter-Setup-<ვერსია>.exe. საჭიროა: PyInstaller (venv-ში), Tesseract (UB Mannheim) და Inno Setup 6.
გარემო: Python 3.13 + requirements.lock (docs\\INSTALL_GE.md → „ახალი ვერსიის გამოშვება“). Tesseract-ის წყარო
იცვლება HRSORTER_TESSERACT_DIR / HRSORTER_TESSDATA_DIR ცვლადებით (მაგ. დაინსტალირებული HR Sorter-ის `tesseract`).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
OUT = PROJ / "build_out"
DIST = OUT / "dist" / "HRSorter"
TESS_SRC = Path(os.environ.get("HRSORTER_TESSERACT_DIR", r"C:\Program Files\Tesseract-OCR"))
TESSDATA_SRC = Path(os.environ.get("HRSORTER_TESSDATA_DIR", r"C:\ProgramData\hrsorter\tessdata"))
LANGS = ("kat", "eng", "osd")
ISCC_CANDIDATES = [Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
                   Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe")]


def tesseract_files() -> list[Path]:
    """tesseract.exe და მისი DLL-ების ჩაკეტილი სია (დამოკიდებულებების გრაფი PE იმპორტების მიხედვით)."""
    import pefile  # მხოლოდ აწყობისთვის
    local = {p.name.lower(): p for p in TESS_SRC.iterdir() if p.is_file()}
    need, seen = ["tesseract.exe"], {}
    while need:
        name = need.pop()
        if name in seen:
            continue
        path = local[name]
        seen[name] = path
        pe = pefile.PE(str(path), fast_load=True)
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
        for imp in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
            dll = imp.dll.decode().lower()
            if dll in local:
                need.append(dll)
        pe.close()
    return sorted(seen.values())


def run(cmd: list[str]) -> None:
    print(">", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=PROJ)


BUILD_PYTHON = (3, 13)   # 0.1.10-დან ინსტალერი ამ Python-ით იწყობა; სხვაზე გადასვლა ცალკე, განზრახ გადაწყვეტილებაა


def main() -> int:
    from hrsorter import PIPELINE_VERSION
    if sys.version_info[:2] != BUILD_PYTHON:
        print(f"warning: building with Python {sys.version.split()[0]}, releases use {'.'.join(map(str, BUILD_PYTHON))} "
              "(docs\\INSTALL_GE.md)", flush=True)
    # exe-ის თვისებებში (Properties → Details) ვერსია — იგივე, რაც ინსტალერსა და განახლებაში
    vi = PROJ / "packaging" / "version_info.txt"
    nums = ", ".join(PIPELINE_VERSION.split(".") + ["0"])
    text = re.sub(r"(filevers|prodvers)=\([\d, ]+\)", lambda m: f"{m.group(1)}=({nums})", vi.read_text(encoding="utf-8"))
    text = re.sub(r"'(FileVersion|ProductVersion)', '[^']*'", lambda m: f"'{m.group(1)}', '{PIPELINE_VERSION}'", text)
    vi.write_text(text, encoding="utf-8")
    shutil.rmtree(OUT / "dist", ignore_errors=True)
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(OUT / "dist"),
         "--workpath", str(OUT / "work"), str(PROJ / "packaging" / "hrsorter.spec")])

    tess_dir = DIST / "tesseract"
    (tess_dir / "tessdata").mkdir(parents=True, exist_ok=True)
    files = tesseract_files()
    for f in files:
        shutil.copy2(f, tess_dir / f.name)
    for lang in LANGS:
        shutil.copy2(TESSDATA_SRC / f"{lang}.traineddata", tess_dir / "tessdata" / f"{lang}.traineddata")
    print(f"tesseract: {len(files)} files + {', '.join(LANGS)}")

    iscc = next((p for p in ISCC_CANDIDATES if p.exists()), None)
    if iscc is None:
        print("Inno Setup (ISCC.exe) not found: winget install --id JRSoftware.InnoSetup -e")
        return 1
    run([str(iscc), f"/DAppVersion={PIPELINE_VERSION}", f"/DDistDir={DIST}", str(PROJ / "packaging" / "installer.iss")])
    setup = OUT / f"HRSorter-Setup-{PIPELINE_VERSION}.exe"
    print(f"\nready: {setup} ({setup.stat().st_size / 2**20:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
