"""Post-build for dist_066 (0.5.10): vendor DLLs, smoke test, zip + verify.

Runs from the TunerLab-0.5.6 root with the project venv.  PyInstaller does not
bundle the VC++/MFC runtime that zauxdll.dll needs, so we copy the same six
DLLs that shipped in dist_064 into both the _internal root and vendor/zmotion.

0.5.10 changes vs 0.5.9: the top title/breadcrumb now follows navigation-tree
clicks (shell._apply_nav_meta), and version_info.txt numeric tuples are aligned.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # TunerLab-0.5.6
WORKSPACE = ROOT.parent                             # repo root
DIST = ROOT / 'dist_066'
APP = DIST / 'TunerLab'
INTERNAL = APP / '_internal'
VENDOR = INTERNAL / 'vendor' / 'zmotion'
REF = WORKSPACE / 'dist_064' / 'TunerLab' / '_internal'
PY = WORKSPACE / 'TunerLabApp' / '.venv' / 'Scripts' / 'python.exe'
VERSION = '0.5.10'
DLLS = ('mfc140u.dll', 'mfcm140.dll', 'mfcm140u.dll',
        'msvcp140.dll', 'msvcp140_1.dll', 'concrt140.dll')

FAILS: list[str] = []


def check(label: str, ok: bool, detail: str = '') -> None:
    print(('PASS  ' if ok else 'FAIL  ') + label
          + (('  -> ' + detail) if detail else ''), flush=True)
    if not ok:
        FAILS.append(label)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def run(label: str, args: list[str], env: dict | None = None) -> str:
    import os
    run_env = os.environ.copy()
    run_env.setdefault('QT_QPA_PLATFORM', 'offscreen')
    if env:
        run_env.update(env)
    # The frozen EXE prints some Windows-localised lines in GBK (0xbf etc.);
    # force a lossy decode so a stray non-UTF8 byte cannot crash the packager.
    proc = subprocess.run(args, cwd=str(APP), env=run_env,
                          capture_output=True, timeout=180)
    out = ((proc.stdout or b'') + (proc.stderr or b'')).decode(
        'utf-8', errors='replace').strip()
    check(label, proc.returncode == 0,
          f'exit={proc.returncode} ' + out.splitlines()[-1][:120] if out else f'exit={proc.returncode}')
    return out


def main() -> int:
    if not APP.is_dir():
        print(f'dist_066 尚未构建: {APP}')
        return 2

    # 1) VC++/MFC runtime into both locations (mirror dist_064).
    for target in (INTERNAL, VENDOR):
        target.mkdir(parents=True, exist_ok=True)
        for name in DLLS:
            src = REF / name
            if src.is_file():
                shutil.copy2(src, target / name)
            else:
                check(f'DLL 源存在 {name}', False, str(src))
    present = all((INTERNAL / n).is_file() and (VENDOR / n).is_file() for n in DLLS)
    check('6 个 VC++/MFC DLL 已补齐(两处)', present)
    check('zauxdll/zmotion 由 spec 带入',
          (VENDOR / 'zauxdll.dll').is_file() and (VENDOR / 'zmotion.dll').is_file())

    # 2) Version + smoke test on the frozen EXE.
    exe = APP / 'TunerLab.exe'
    ver = run('冻结包 --version', [str(exe), '--version'])
    check('版本号 = 0.5.10', VERSION in ver, ver[:80])
    run('冻结包 --smoke-test (16 页)', [str(exe), '--smoke-test'])

    # 3) DLL self-containment: load the frozen zauxdll directly.
    probe = (
        "import ctypes,sys;"
        f"p=r'{VENDOR / 'zauxdll.dll'}';"
        "d=ctypes.WinDLL(p);"
        "assert hasattr(d,'ZAux_OpenEth');"
        "print('zauxdll loaded + ZAux_OpenEth bound')"
    )
    run('冻结包 DLL 自包含加载', [str(PY), '-c', probe])

    # 4) Live hardware reachability (read-only; same bench as dist_064).
    hw = WORKSPACE / 'verify_hardware_056.py'
    if hw.is_file():
        out = run('真机只读探测(控制器+矢网)',
                  [str(PY), str(hw)], env={'QT_QPA_PLATFORM': 'offscreen'})
        check('控制器/矢网可达', 'FAIL' not in out.upper(), '')

    # 5) Zip + per-file SHA-256 verification.
    archive = WORKSPACE / f'TunerLab-{VERSION}-dist_066.zip'
    prefix = APP.name + '/'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=3) as z:
        for path in sorted(APP.rglob('*')):
            if path.is_file():
                z.write(path, prefix + path.relative_to(APP).as_posix())
    names: list[str] = []
    with zipfile.ZipFile(archive) as z:
        bad = z.testzip()
        names = [i.filename for i in z.infolist() if not i.is_dir()]
        check('zip CRC 全通过', bad is None, str(bad))
        check('zip 无重复条目', len(names) == len(set(names)))
        required = {prefix + 'TunerLab.exe',
                    prefix + '_internal/tuner/web/static/index.html',
                    prefix + '_internal/tuner/web/static/app.js',
                    prefix + '_internal/tuner/web/static/style.css'}
        check('zip 含 EXE + 控制台资源', required.issubset(set(names)))
    mismatch = []
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            local = DIST / info.filename
            with z.open(info) as packed, local.open('rb') as fh:
                if sha256(local) != hashlib.sha256(packed.read()).hexdigest():
                    mismatch.append(info.filename)
    check('zip 每文件 SHA-256 与磁盘一致', not mismatch, str(mismatch[:3]))
    print(f'\nzip: {archive}  ({archive.stat().st_size} B, {len(names)} 文件)')
    print(f'SHA-256: {sha256(archive)}')

    print(f'\n{len(FAILS)} 项失败' if FAILS else '\n全部检查通过')
    return 1 if FAILS else 0


if __name__ == '__main__':
    raise SystemExit(main())
