#!/usr/bin/env python3
"""Collect upstream distribution notices for embedding in the onefile release."""
import importlib.metadata
import json
from pathlib import Path
import shutil
import sys
import sysconfig

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'build/licenses'


def main():
    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    OUTPUT.mkdir(parents=True)
    for name in ['LICENSE', 'THIRD_PARTY_NOTICES.md']:
        shutil.copyfile(ROOT / name, OUTPUT / name)
    shutil.copytree(ROOT / 'licenses', OUTPUT / 'additional', dirs_exist_ok=True)
    versions = {'Python': sys.version.split()[0]}
    python_license = Path(sysconfig.get_path('stdlib')) / 'LICENSE.txt'
    if not python_license.is_file():
        raise SystemExit('Python runtime LICENSE.txt was not found; include its license before packaging.')
    shutil.copyfile(python_license, OUTPUT / 'Python-LICENSE.txt')
    for name in ['Pillow', 'pyinstaller', 'pyinstaller-hooks-contrib']:
        distribution = importlib.metadata.distribution(name)
        versions[name] = distribution.version
        copied = 0
        for relative in distribution.files or []:
            parts = [p.lower() for p in relative.parts]
            basename = relative.name.lower()
            if ('licenses' not in parts and not basename.startswith(('license', 'copying', 'notice'))):
                continue
            source = Path(distribution.locate_file(relative))
            if not source.is_file():
                continue
            if 'licenses' in parts:
                offset = parts.index('licenses') + 1
                destination = OUTPUT / name / Path(*relative.parts[offset:])
            else:
                destination = OUTPUT / name / relative.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            copied += 1
        if not copied:
            raise SystemExit('No upstream license file was collected for ' + name)
    (OUTPUT / 'versions.json').write_text(json.dumps(versions, indent=2) + '\n', encoding='utf-8')
    print('Collected runtime and build-tool license notices.')


if __name__ == '__main__':
    main()
