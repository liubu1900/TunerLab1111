"""Validate every release file against its ZIP, including non-executable assets."""
from __future__ import annotations
import hashlib
import sys
import zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tunerlab_app.version import __version__ as VERSION


def _digest(stream):
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        digest.update(block)
    return digest.hexdigest()


def verify(folder: Path, archive: Path) -> bool:
    if not folder.is_dir() or not archive.is_file():
        print(f'发布目录或压缩包不存在: {folder} / {archive}')
        return False
    prefix = folder.name + '/'
    disk = {prefix + p.relative_to(folder).as_posix(): p
            for p in folder.rglob('*') if p.is_file()}
    required = {prefix + 'TunerLab.exe'} | {
        prefix + '_internal/tuner/web/static/' + name
        for name in ('index.html', 'app.js', 'style.css')}
    try:
        with zipfile.ZipFile(archive) as z:
            entries = [info for info in z.infolist() if not info.is_dir()]
            names = [info.filename for info in entries]
            if len(names) != len(set(names)):
                print('压缩包包含重复文件条目')
                return False
            if set(names) != set(disk) or not required.issubset(disk):
                print(f'文件清单不一致: ZIP缺少={sorted(set(disk)-set(names))[:20]}, ZIP多出={sorted(set(names)-set(disk))[:20]}, 必需文件缺少={sorted(required-set(disk))}')
                return False
            mismatched = []
            for info in entries:
                with disk[info.filename].open('rb') as local, z.open(info) as packed:
                    if _digest(local) != _digest(packed):
                        mismatched.append(info.filename)
            if mismatched:
                print(f'文件内容不一致: {mismatched}')
                return False
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, EOFError) as exc:
        print(f'发布包校验失败: {exc}')
        return False
    print(f'校验通过：{len(disk)} 个文件全部 SHA-256 一致，必需资源齐全')
    return True


def main():
    root = Path(__file__).resolve().parents[1] / 'dist'
    return 0 if verify(root / f'TunerLab-{VERSION}', root / f'TunerLab-{VERSION}-win64.zip') else 1


if __name__ == '__main__':
    sys.exit(main())
