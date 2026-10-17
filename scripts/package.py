#!/usr/bin/env python3
"""Create a minimal Hub install ZIP from the self-contained Apple Silicon app."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import struct
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def arm64_macho(path):
    with path.open('rb') as handle:
        header = handle.read(8)
        if len(header) < 8:
            return False
        magic, cpu = struct.unpack('<II', header)
        if magic == 0xFEEDFACF:
            return cpu == 0x0100000C
        big_magic, architectures = struct.unpack('>II', header)
        if big_magic not in {0xCAFEBABE, 0xCAFEBABF} or not 1 <= architectures <= 16:
            return False
        stride = 32 if big_magic == 0xCAFEBABF else 20
        for _ in range(architectures):
            entry = handle.read(stride)
            if len(entry) != stride:
                return False
            if struct.unpack('>I', entry[:4])[0] == 0x0100000C:
                return True
    return False


def digest(data):
    return hashlib.sha256(data).hexdigest()


def package(binary, output, version=None):
    if binary.is_symlink() or not binary.is_file():
        raise ValueError('Binary must be a regular file, not a symbolic link.')
    if not arm64_macho(binary):
        raise ValueError('Expected a self-contained Apple Silicon macOS executable.')
    manifest = json.loads((ROOT / 'app-manifest.template.json').read_text(encoding='utf-8'))
    if version:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,39}', version):
            raise ValueError('Invalid release version.')
        manifest['version'] = version
    payload = binary.read_bytes()
    if len(payload) > 100 * 1024 * 1024:
        raise ValueError('Binary exceeds the Hub per-file limit of 100 MiB.')
    manifest['files'] = {manifest['entry']: digest(payload)}
    manifest_data = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    output.mkdir(parents=True, exist_ok=True)
    filename = '%s-%s-macos-aarch64.zip' % (manifest['id'], manifest['version'])
    archive = output / filename
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as handle:
        for name, data, mode in [('app.json', manifest_data, 0o600),
                                 (manifest['entry'], payload, 0o700)]:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            handle.writestr(info, data, compresslevel=9)
    if archive.stat().st_size > 100 * 1024 * 1024:
        archive.unlink()
        raise ValueError('Archive exceeds the Hub install limit of 100 MiB.')
    with zipfile.ZipFile(archive) as handle:
        assert set(handle.namelist()) == {'app.json', manifest['entry']}
        actual = json.loads(handle.read('app.json'))
        for name, expected in actual['files'].items():
            if digest(handle.read(name)) != expected:
                raise ValueError('Packaged file checksum mismatch: ' + name)
    archive_hash = digest(archive.read_bytes())
    (output / 'app.json').write_bytes(manifest_data)
    (output / (filename + '.sha256')).write_text(archive_hash + '  ' + filename + '\n', encoding='utf-8')
    metadata = {'id': manifest['id'], 'version': manifest['version'],
                'platform': 'macos-aarch64', 'archive': filename,
                'archive_sha256': archive_hash, 'binary_sha256': manifest['files'][manifest['entry']],
                'archive_bytes': archive.stat().st_size, 'binary_bytes': len(payload)}
    (output / 'release-metadata.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, default=ROOT / 'build/bin/zectrix-nas-gallery')
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    parser.add_argument('--version')
    args = parser.parse_args()
    try:
        result = package(args.binary, args.output, args.version)
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from error
    print(result)


if __name__ == '__main__':
    main()
