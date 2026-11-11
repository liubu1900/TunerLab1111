"""Archive the complete Windows tree, including files Compress-Archive skips."""
from pathlib import Path
import sys
import zipfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tunerlab_app.version import __version__

root = Path(__file__).resolve().parents[1] / 'dist'
folder = root / f'TunerLab-{__version__}'
archive = root / f'TunerLab-{__version__}-win64.zip'
if not (folder / 'TunerLab.exe').is_file():
    raise SystemExit(f'Missing Windows executable: {folder}')
with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=3) as packed:
    for path in sorted(folder.rglob('*')):
        if path.is_file():
            packed.write(path, path.relative_to(root).as_posix())
print(f'Created {archive.name}: {archive.stat().st_size} bytes')
