# PyInstaller build spec -- produces a single executable that needs no Python.
#
#   pip install pyinstaller
#   pyinstaller repohealth.spec
#
# The result lands in dist/. On Windows that is dist\RepoHealth.exe: one file a
# user can download and double-click, with no install step and no Python on the
# machine.
#
# The data files matter. schema.sql and the web assets are read at runtime, and
# PyInstaller cannot see that from the imports alone -- without the `datas`
# entries below the executable builds cleanly and then fails on first run.

import sys
from PyInstaller.utils.hooks import collect_submodules

datas = [
    ("repohealth/db/schema.sql", "repohealth/db"),
    ("repohealth/web/index.html", "repohealth/web"),
    ("repohealth/web/d3.min.js", "repohealth/web"),
]

# uvicorn and fastapi load parts of themselves dynamically, so static analysis
# misses them.
hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("fastapi")
    + ["radon.complexity", "radon.visitors", "radon.metrics", "git"]
)

a = Analysis(
    ["repohealth/__main__.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="RepoHealth",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    # console=True keeps a terminal visible: it is where the gatekeeper's
    # output appears, and where a crash would otherwise vanish silently.
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
