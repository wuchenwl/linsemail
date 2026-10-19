"""Reopen source archives; verify hashes, public contents and documentation images."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('package_release', ROOT / 'scripts/package-release.py')
packager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packager)


def verify(contents):
    packager.scan_public(contents)
    manifest = json.loads(contents['release-manifest.json'])
    assert set(contents) == set(manifest['files']) | {'release-manifest.json'}
    for name, expected in manifest['files'].items():
        assert len(contents[name]) == expected['bytes'], name
        assert hashlib.sha256(contents[name]).hexdigest() == expected['sha256'], name
    missing = []
    for name, raw in contents.items():
        if not name.endswith('.md'):
            continue
        text = raw.decode('utf-8')
        targets = re.findall(r'!?\[[^\]]*\]\(([^\s)]+)(?:\s+[^)]*)?\)', text)
        targets += re.findall(r'<img\b[^>]*\bsrc="([^"]+)"', text)
        for target in targets:
            if target.startswith(('http:', 'https:', '#', 'mailto:')):
                continue
            target = target.split('#')[0]
            if not target:
                continue
            pieces = list(PurePosixPath(name).parent.parts)
            for piece in PurePosixPath(target).parts:
                if piece == '..': pieces.pop()
                elif piece != '.': pieces.append(piece)
            normalized = '/'.join(pieces)
            if normalized not in contents:
                missing.append((name, target))
    assert not missing, missing
    images = [name for name in contents if name.startswith('docs/screenshots/') and name.endswith('.png')]
    assert len(images) == 16, len(images)
    screenshot_manifest = json.loads(contents['docs/screenshots/manifest.json'])
    for screenshot in screenshot_manifest['screenshots']:
        data = contents['docs/screenshots/' + screenshot['file']]
        assert data[:8] == b'\x89PNG\r\n\x1a\n'
        assert hashlib.sha256(data).hexdigest() == screenshot['sha256']
    assert not screenshot_manifest['errors']
    catalog = json.loads(contents['data/providers.json'])
    assert len(catalog['providers']) == 127
    assert all('homepage' in provider and provider['homepage'].startswith(('https://', 'http://')) for provider in catalog['providers'])
    assert next(provider for provider in catalog['providers'] if provider['id'] == 'mailtm')['homepage'] == 'https://mail.tm/'
    return {'files': len(contents), 'screenshots': len(images), 'links': 'all local Markdown/image targets exist', 'hashes': 'all match'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'release-artifacts')
    args = parser.parse_args()
    version = (ROOT / 'VERSION').read_text().strip()
    name = 'linsemail-' + version + '-source'
    prefix = name + '/'
    zip_path, tar_path = args.output / (name + '.zip'), args.output / (name + '.tar.gz')
    with zipfile.ZipFile(zip_path) as archive:
        assert archive.testzip() is None
        assert all(item.filename.startswith(prefix) and '..' not in PurePosixPath(item.filename).parts for item in archive.infolist())
        zipped = {info.filename[len(prefix):]: archive.read(info.filename) for info in archive.infolist()}
    with tarfile.open(tar_path, 'r:gz') as archive:
        assert all(item.isfile() and item.name.startswith(prefix) and '..' not in PurePosixPath(item.name).parts for item in archive.getmembers())
        tarred = {item.name[len(prefix):]: archive.extractfile(item).read() for item in archive.getmembers()}
    assert zipped == tarred
    for row in (args.output / 'SHA256SUMS').read_text().splitlines():
        digest, filename = row.split('  ', 1)
        assert hashlib.sha256((args.output / filename).read_bytes()).hexdigest() == digest
    print(json.dumps({'zip': str(zip_path.resolve()), 'tar': str(tar_path.resolve()), 'identical_source_contents': True, **verify(zipped)}, indent=2))


if __name__ == '__main__':
    main()
