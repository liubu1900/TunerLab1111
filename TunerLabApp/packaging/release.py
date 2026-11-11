"""Cut a release: bump the version, build the desktop app, archive it.

The release is the *only* artefact an operator receives, so this script owns
the whole chain and refuses to publish something that does not work:

1. **Version bump.**  ``tunerlab_app/version.py`` and
   ``packaging/version_info.txt`` and ``packaging/installer.iss`` carry the version;
   all are rewritten from the same value, so the About page, the window
   title and the Windows file properties can never disagree.

2. **Build.**  PyInstaller produces ``dist/TunerLab/TunerLab.exe``.  The
   vendor DLLs (``zauxdll.dll``) are loaded by ``ctypes`` at run time, so
   they are data, not imports -- the spec ships them under ``vendor/zmotion``.

3. **Runtime fix-up.** Bundle the VC++ 2010 x64 runtime required by the
   shipped ``zmotion.dll`` from the installed Microsoft redistributable.
   No previous release directory is required.

4. **Verification.**  The frozen EXE is started twice: once for
   ``--version`` and once for ``--smoke-test``, which walks every page.  A
   release that cannot open its own pages is not a release.

5. **Archive.**  The tree is zipped into ``releases/`` and every file in the
   archive is compared against the disk by SHA-256, so a truncated or
   corrupted download is detectable.

Usage::

    python packaging/release.py --bump patch       # 0.5.10 -> 0.5.11
    python packaging/release.py --bump minor       # 0.5.10 -> 0.6.0
    python packaging/release.py --bump patch --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import date
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]        # TunerLabApp
WORKSPACE = APP_ROOT.parent                           # repo root
RELEASES = WORKSPACE / 'releases'
VERSION_FILE = APP_ROOT / 'tunerlab_app' / 'version.py'
RESOURCE_FILE = APP_ROOT / 'packaging' / 'version_info.txt'
INSTALLER_FILE = APP_ROOT / 'packaging' / 'installer.iss'
SPEC = APP_ROOT / 'packaging' / 'tunerlab.spec'
DIST = APP_ROOT / 'dist'
APP_DIR = DIST / 'TunerLab'
INTERNAL = APP_DIR / '_internal'
VENDOR = INTERNAL / 'vendor' / 'zmotion'

FAILS: list[str] = []


def check(label: str, ok: bool, detail: str = '') -> None:
    print(('PASS  ' if ok else 'FAIL  ') + label
          + (('  -> ' + detail) if detail else ''), flush=True)
    if not ok:
        FAILS.append(label)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


# ------------------------------------------------------------------ version
def current_version() -> str:
    text = VERSION_FILE.read_text(encoding='utf-8')
    match = re.search(r"__version__\s*=\s*'([^']+)'", text)
    if not match:
        raise SystemExit(f'cannot read __version__ from {VERSION_FILE}')
    return match.group(1)


def bump(version: str, level: str) -> str:
    """Return the next version number for the requested level."""
    parts = version.split('.')
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise SystemExit(f'cannot bump non-numeric version {version!r}')
    major, minor, patch = (int(part) for part in parts)
    if level == 'major':
        return f'{major + 1}.0.0'
    if level == 'minor':
        return f'{major}.{minor + 1}.0'
    return f'{major}.{minor}.{patch + 1}'


def write_version(version: str, release_date: str) -> None:
    """Write the version into every place that carries it.

    Three files must agree: the Python module (About page, window title), the
    Windows file-version resource and the Inno Setup script (the installer's
    file name and its *Version Information* tab).  A release whose installer
    still says the old version is worse than no installer at all, because the
    operator would install a package under the wrong name.
    """
    text = VERSION_FILE.read_text(encoding='utf-8')
    text = re.sub(r"__version__\s*=\s*'[^']+'",
                  f"__version__ = '{version}'", text)
    text = re.sub(r"__release_date__\s*=\s*'[^']+'",
                  f"__release_date__ = '{release_date}'", text)
    VERSION_FILE.write_text(text, encoding='utf-8')

    numbers = tuple(int(part) for part in version.split('.'))
    quadruplet = numbers + (0,)
    text = RESOURCE_FILE.read_text(encoding='utf-8')
    text = re.sub(r'filevers=\([^)]*\)', f'filevers={quadruplet}', text)
    text = re.sub(r'prodvers=\([^)]*\)', f'prodvers={quadruplet}', text)
    text = re.sub(r"StringStruct\('FileVersion', '[^']+'\)",
                  f"StringStruct('FileVersion', '{version}.0')", text)
    text = re.sub(r"StringStruct\('ProductVersion', '[^']+'\)",
                  f"StringStruct('ProductVersion', '{version}.0')", text)
    RESOURCE_FILE.write_text(text, encoding='utf-8')

    text = INSTALLER_FILE.read_text(encoding='utf-8')
    text = re.sub(r'#define MyAppVersion "[^"]+"',
                  f'#define MyAppVersion "{version}"', text)
    INSTALLER_FILE.write_text(text, encoding='utf-8')


# -------------------------------------------------------------------- build
def run(label: str, args: list[str], cwd: Path | None = None,
        env: dict | None = None, timeout: int = 900) -> str:
    run_env = os.environ.copy()
    run_env.setdefault('QT_QPA_PLATFORM', 'offscreen')
    if env:
        run_env.update(env)
    proc = subprocess.run(args, cwd=str(cwd or WORKSPACE), env=run_env,
                          capture_output=True, timeout=timeout)
    out = ((proc.stdout or b'') + (proc.stderr or b'')).decode(
        'utf-8', errors='replace').strip()
    check(label, proc.returncode == 0,
          out.splitlines()[-1][:160] if out else f'exit={proc.returncode}')
    return out


def build(version: str) -> None:
    """PyInstaller build + runtime fix-up + frozen-binary smoke test."""
    python = sys.executable
    run('PyInstaller 构建', [python, '-m', 'PyInstaller', str(SPEC),
                             '--noconfirm', '--clean'],
        cwd=APP_ROOT, timeout=1800)
    if FAILS:
        return

    exe = APP_DIR / 'TunerLab.exe'
    check('生成 TunerLab.exe', exe.is_file(), str(exe))
    if not exe.is_file():
        return

    run('运行时及运动 DLL 加载核对',
        [python, str(APP_ROOT / 'packaging' / 'windows_runtime.py'), '--app-dir', str(APP_DIR)],
        cwd=APP_ROOT, timeout=180)
    if FAILS:
        return
    check('zauxdll/zmotion 由 spec 带入',
          (VENDOR / 'zauxdll.dll').is_file() and (VENDOR / 'zmotion.dll').is_file())

    run('冻结包 --version', [str(exe), '--version'], cwd=APP_DIR, timeout=180)
    # Windowed PyInstaller executables may have no stdout; verify the actual
    # Windows file resource rather than requiring a console output stream.
    out = run('冻结包版本资源', ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
              '(Get-Item -LiteralPath $env:TUNERLAB_RELEASE_EXE).VersionInfo.FileVersion'],
              cwd=APP_DIR, env={'TUNERLAB_RELEASE_EXE': str(exe)}, timeout=180)
    check(f'版本号 = {version}', out.strip() == f'{version}.0', out[:120])

    run('冻结包 --smoke-test', [str(exe), '--smoke-test'], cwd=APP_DIR, timeout=600)

# ------------------------------------------------------------------ archive
def archive(version: str) -> Path | None:
    """Zip the built tree into ``releases/`` and verify every entry."""
    if not APP_DIR.is_dir():
        check('发布目录存在', False, str(APP_DIR))
        return None
    tag = f'TunerLab-{version}-dist'
    target = RELEASES / tag
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(APP_DIR, target / 'TunerLab')

    archive_path = RELEASES / f'{tag}.zip'
    prefix = 'TunerLab/'
    with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED,
                         compresslevel=3) as archive_file:
        for path in sorted((target / 'TunerLab').rglob('*')):
            if path.is_file():
                archive_file.write(path,
                                  prefix + path.relative_to(target / 'TunerLab').as_posix())

    names: list[str] = []
    with zipfile.ZipFile(archive_path) as archive_file:
        bad = archive_file.testzip()
        names = [item.filename for item in archive_file.infolist() if not item.is_dir()]
        check('zip CRC 全通过', bad is None, str(bad))
        check('zip 无重复条目', len(names) == len(set(names)))
        required = {prefix + 'TunerLab.exe',
                    prefix + '_internal/tuner/web/static/index.html',
                    prefix + '_internal/tuner/web/static/app.js',
                    prefix + '_internal/tuner/web/static/style.css'}
        check('zip 含 EXE + 控制台资源', required.issubset(set(names)))

    mismatch = []
    with zipfile.ZipFile(archive_path) as archive_file:
        for item in archive_file.infolist():
            if item.is_dir():
                continue
            local = target / item.filename
            with archive_file.open(item) as packed, local.open('rb') as handle:
                if sha256(local) != hashlib.sha256(packed.read()).hexdigest():
                    mismatch.append(item.filename)
    check('zip 每文件 SHA-256 与磁盘一致', not mismatch, str(mismatch[:3]))

    manifest = RELEASES / f'{tag}.sha256.txt'
    manifest.write_text(
        f'{sha256(archive_path)}  {archive_path.name}\n', encoding='utf-8')
    print(f'\nzip: {archive_path}  ({archive_path.stat().st_size} B, {len(names)} 文件)')
    print(f'SHA-256: {sha256(archive_path)}')
    print(f'清单: {manifest}')
    return archive_path


def main() -> int:
    parser = argparse.ArgumentParser(description='Cut a TunerLab release.')
    parser.add_argument('--bump', choices=('major', 'minor', 'patch'),
                        help='which part of the version to increase')
    parser.add_argument('--version', help='use this exact version instead')
    parser.add_argument('--dry-run', action='store_true',
                        help='show what would happen, change nothing')
    args = parser.parse_args()

    old = current_version()
    version = args.version or (bump(old, args.bump) if args.bump else old)
    release_date = date.today().isoformat()
    print(f'当前版本 {old} -> 发布版本 {version} ({release_date})')

    if args.dry_run:
        print('dry-run：未改动任何文件')
        return 0
    if sys.platform != 'win32':
        parser.error('Windows releases must be built on Windows x64')

    write_version(version, release_date)
    check('版本号写入 version.py',
          current_version() == version, current_version())
    check('版本号写入 version_info.txt',
          f"'{version}.0'" in RESOURCE_FILE.read_text(encoding='utf-8'))
    check('版本号写入 installer.iss',
          f'#define MyAppVersion "{version}"'
          in INSTALLER_FILE.read_text(encoding='utf-8'))

    build(version)
    if not FAILS:
        archive(version)

    if FAILS:
        print(f'\n{len(FAILS)} 项失败: {FAILS}')
        return 1
    print('\n全部检查通过')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
