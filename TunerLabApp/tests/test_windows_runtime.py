"""Release checks that catch absent/incorrect architecture runtime dependencies."""
import importlib.util
import struct
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    'windows_runtime', Path(__file__).resolve().parents[1] / 'packaging' / 'windows_runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def pe_file(path, machine=runtime.AMD64, payload=b''):
    path.parent.mkdir(parents=True, exist_ok=True)
    header = bytearray(64)
    header[:2] = b'MZ'
    struct.pack_into('<I', header, 60, 64)
    path.write_bytes(header + b'PE\0\0' + struct.pack('<H', machine) + payload)


def application(tmp_path):
    app = tmp_path / 'TunerLab'
    for name in runtime.VENDOR_DLLS:
        pe_file(runtime.vendor_dir(app) / name)
    return app


def test_runtime_is_bundled_in_both_loader_directories_and_corruption_is_rejected(tmp_path):
    app, source = application(tmp_path), tmp_path / 'system32'
    pe_file(source / 'msvcr100.dll', payload=b'correct redistributable')
    runtime.prepare_runtime(app, source)
    runtime.verify_runtime(app)
    pe_file(runtime.vendor_dir(app) / 'msvcr100.dll', payload=b'corrupted replacement')
    with pytest.raises(ValueError, match='copies disagree'):
        runtime.verify_runtime(app)


@pytest.mark.parametrize('content', [None, b'not a DLL', 'x86'])
def test_missing_or_wrong_runtime_fails_before_any_copy(tmp_path, content):
    app, source = application(tmp_path), tmp_path / 'system32'
    source.mkdir()
    if content == 'x86':
        pe_file(source / 'msvcr100.dll', machine=0x14c)
    elif content is not None:
        (source / 'msvcr100.dll').write_bytes(content)
    with pytest.raises((FileNotFoundError, ValueError)):
        runtime.prepare_runtime(app, source)
    assert not (app / '_internal' / 'msvcr100.dll').exists()
    assert not (runtime.vendor_dir(app) / 'msvcr100.dll').exists()


def test_shipped_vendor_dlls_have_the_release_architecture():
    vendor = Path(__file__).resolve().parents[2] / 'TunerLab' / 'vendor' / 'zmotion'
    for name in runtime.VENDOR_DLLS:
        runtime.require_x64(vendor / name)
