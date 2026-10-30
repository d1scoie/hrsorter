# -*- mode: python ; coding: utf-8 -*-
# PyInstaller-ის რეცეპტი: ერთი საქაღალდე (onedir), ორი exe საერთო ბიბლიოთეკებით.
#   hrsorter.exe     — კონსოლით (ადმინისტრატორისთვის: status, setup, პრობლემების გარკვევა)
#   hrsorter-bg.exe  — ფანჯრის გარეშე (worker, ui, open — მალსახმობებიდან და ავტოგაშვებიდან)
# აწყობა: .venv\Scripts\python.exe packaging\build.py
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

proj = Path(SPECPATH).parent

datas = [
    (str(proj / "config" / "config.example.json"), "config"),
    (str(proj / "config" / "document_types.example.json"), "config"),
    (str(proj / "config" / "mock_employees.example.json"), "config"),
    (str(proj / "hrsorter" / "schema" / "document_sidecar.schema.json"), "hrsorter/schema"),
]

a = Analysis(
    [str(proj / "packaging" / "entry.py")],
    pathex=[str(proj)],
    datas=datas,
    hiddenimports=collect_submodules("hrsorter") + collect_submodules("uvicorn") + ["keyring.backends.Windows", "pystray._win32"],
    excludes=["tkinter", "reportlab", "anthropic", "pytest", "tools", "pefile", "matplotlib", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe_cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="hrsorter", console=True,
              upx=False, version=str(proj / "packaging" / "version_info.txt"))
exe_bg = EXE(pyz, a.scripts, [], exclude_binaries=True, name="hrsorter-bg", console=False,
             upx=False, version=str(proj / "packaging" / "version_info.txt"))

coll = COLLECT(exe_cli, exe_bg, a.binaries, a.datas, upx=False, name="HRSorter")
