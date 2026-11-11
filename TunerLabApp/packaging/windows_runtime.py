"""Bundle the x64 VC++ 2010 runtime required by the shipped Zmotion SDK.

The actual zmotion.dll imports MSVCR100.dll. Copying unrelated VC++ 2015/MFC
DLLs from an older release does not satisfy that dependency. Obtain the runtime
from an installed Microsoft VC++ 2010 SP1 x64 redistributable, never a DLL site.
This check loads the library and resolves an export; it opens no controller.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
import shutil
import struct
import sys
from contextlib import ExitStack
from pathlib import Path

RUNTIME_DLLS = ('msvcr100.dll',)
VENDOR_DLLS = ('zauxdll.dll', 'zmotion.dll')
AMD64 = 0x8664


def require_x64(path: Path) -> None:
    """Reject absent, corrupt and 32-bit DLLs before copying any file."""
    with path.open('rb') as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b'MZ':
            raise ValueError(f'Not a Windows PE file: {path}')
        stream.seek(struct.unpack_from('<I', header, 60)[0])
        pe = stream.read(6)
    if len(pe) != 6 or pe[:4] != b'PE\0\0' or struct.unpack_from('<H', pe, 4)[0] != AMD64:
        raise ValueError(f'Expected an x64 Windows DLL: {path}')


def vendor_dir(app_dir: Path) -> Path:
    return app_dir / '_internal' / 'vendor' / 'zmotion'


def prepare_runtime(app_dir: Path, source: Path) -> None:
    vendor = vendor_dir(app_dir)
    for name in VENDOR_DLLS:
        require_x64(vendor / name)
    for name in RUNTIME_DLLS:
        require_x64(source / name)
    for destination in (app_dir / '_internal', vendor):
        destination.mkdir(parents=True, exist_ok=True)
        for name in RUNTIME_DLLS:
            src, target = source / name, destination / name
            if src.resolve() != target.resolve():
                shutil.copy2(src, target)
    verify_runtime(app_dir)


def verify_runtime(app_dir: Path) -> None:
    vendor = vendor_dir(app_dir)
    for name in VENDOR_DLLS:
        require_x64(vendor / name)
    for name in RUNTIME_DLLS:
        paths = [app_dir / '_internal' / name, vendor / name]
        for path in paths:
            require_x64(path)
        digests = {hashlib.sha256(path.read_bytes()).digest() for path in paths}
        if len(digests) != 1:
            raise ValueError(f'Runtime copies disagree: {name}')


def load_vendor(app_dir: Path) -> None:
    if sys.platform != 'win32':
        raise RuntimeError('Loading the vendor DLL requires Windows x64')
    with ExitStack() as stack:
        for directory in (app_dir / '_internal', vendor_dir(app_dir)):
            stack.enter_context(os.add_dll_directory(str(directory.resolve())))
        library = ctypes.WinDLL(str((vendor_dir(app_dir) / 'zauxdll.dll').resolve()))
        getattr(library, 'ZAux_OpenEth')
        getattr(library, 'ZAux_Close')
    print('PASS: bundled x64 zauxdll.dll loads and exports are present; no hardware connection')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-dir', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'dist' / 'TunerLab')
    parser.add_argument('--runtime-dir', type=Path,
                        default=Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32')
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('Run this check on Windows x64')
    if args.check_only:
        verify_runtime(args.app_dir)
    else:
        prepare_runtime(args.app_dir, args.runtime_dir)
    load_vendor(args.app_dir)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
