# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build definition for the TunerLab desktop shell.

Layout assumption::

    <workspace>/
      TunerLab/          original project - imported, never modified
      TunerLabApp/       this application (new code + packaging)
        tunerlab_app/
        packaging/
        docs/

Build::

    .venv\\Scripts\\python -m PyInstaller packaging\\tunerlab.spec --noconfirm --clean

Why *onedir* and not *onefile*: ``zauxdll.dll`` is loaded by ``ctypes`` at run
time from ``vendor/zmotion``.  A onefile build unpacks to a temporary directory
that disappears when the process exits, so the DLL path is not stable across a
restart or a second instance; onedir keeps the exact layout the loader expects
and starts far faster on a workshop PC.
"""
from pathlib import Path

ROOT = Path(globals().get('SPECPATH', Path.cwd())).resolve()
if ROOT.name == 'packaging':
    ROOT = ROOT.parent
PROJECT = ROOT.parent / 'TunerLab'

ICON = ROOT / 'packaging' / 'tunerlab.ico'
VERSION_FILE = ROOT / 'packaging' / 'version_info.txt'

datas = [
    # The vendor SDK is data, not an import: ctypes resolves it by path.
    (str(PROJECT / 'vendor' / 'zmotion'), 'vendor/zmotion'),
    # Original manuals, so Help → 文档 works on an installed machine.
    (str(PROJECT / 'docs'), 'docs'),
    (str(ROOT / 'docs'), 'docs/app'),
]
# The browser console's assets.  They are data, not imports, so PyInstaller
# would not pick them up on its own — and without them the installed program
# cannot serve the console the desktop shell opens.
STATIC = PROJECT / 'tuner' / 'web' / 'static'
if STATIC.is_dir():
    datas.append((str(STATIC), 'tuner/web/static'))
if ICON.is_file():
    datas.append((str(ICON), 'packaging'))

a = Analysis(
    [str(ROOT / 'packaging' / 'launcher.py')],
    pathex=[str(ROOT), str(PROJECT)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        'h5py',
        'scipy.spatial',
        'scipy.interpolate',
        'scipy.optimize',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Never ship the test/analysis stack with a machine-control application.
    excludes=[
        'tkinter', 'matplotlib', 'pandas', 'pytest', 'skrf',
        'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
        'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.Qt3DCore',
        'PySide6.QtMultimedia', 'PySide6.QtCharts', 'PySide6.QtDataVisualization',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TunerLab',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ICON) if ICON.is_file() else None,
    version=str(VERSION_FILE) if VERSION_FILE.is_file() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='TunerLab',
)
