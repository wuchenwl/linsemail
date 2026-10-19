"""Build allowlisted source archives; never bundle local runtime or research data."""
import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = ['README.md', 'README.zh-CN.md', 'README.en.md', 'LICENSE', 'CONTRIBUTING.md', 'SECURITY.md',
              'CODE_OF_CONDUCT.md', 'CHANGELOG.md', 'THIRD_PARTY_NOTICES.md', 'VERSION',
              '.gitignore', '.gitattributes', '.editorconfig', '.env.example',
              'app.py', 'mail_api.py', 'mailbox_clients.py', 'tasklet_client.py', 'openapi.json']
SCRIPTS = ['console-services.py', 'start-console.ps1', 'stop-console.ps1', 'test-i18n.cjs',
           'screenshot-console.py', 'package-release.py', 'check-release.py',
           'summarize_tasklet_test.py', 'check_tasklet_inboxes.py', 'tasklet_mail_probe.py']
DOCS = ['architecture.md', 'architecture.zh-CN.md', 'api.md', 'api.zh-CN.md',
        'github-description.md', 'social-preview.png', 'social-preview.html', 'releases/v0.3.0.md', 'mailbox-prefix.md', 'screenshots/README.md', 'screenshots/manifest.json']
BLOCKED_PARTS = {'.runtime', '.venv', 'node_modules', '__pycache__', '.git', 'research', '.pytest_cache'}
FIXED_TIME = (2026, 10, 19, 0, 0, 0)


def release_files(root=ROOT):
    files = set(ROOT_FILES)
    files.update('scripts/' + name for name in SCRIPTS)
    files.update('docs/' + name for name in DOCS)
    for directory, patterns in {'backend': ['**/*.py', 'requirements*.txt'], 'tests': ['**/*.py', '**/*.json'],
                                 'frontend': ['src/**/*.ts', 'src/**/*.tsx', 'src/**/*.css', 'index.html', 'package*.json', 'tsconfig.json', 'vite.config.ts'],
                                 '.github': ['**/*.md', '**/*.yml'], 'tools/postgres': ['*.mjs', 'package*.json'],
                                 'docs/screenshots': ['*.png']}.items():
        for pattern in patterns:
            files.update(path.relative_to(root).as_posix() for path in (root / directory).glob(pattern)
                         if path.is_file() and not (set(path.relative_to(root).parts) & BLOCKED_PARTS))
    files.add('data/providers.json')
    for name in files:
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise ValueError('Missing or symbolic-link release input: ' + name)
    return sorted(files)


def public_catalog(raw):
    catalog = json.loads(raw)
    removed = [0]
    def clean(value):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if isinstance(item, str) and (re.search(r'(?<![A-Za-z0-9])[A-Za-z]:[\\/]', item) or item.startswith(('/Users/', '/home/'))):
                    removed[0] += 1
                    continue
                result[key] = clean(item)
            return result
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value
    catalog = clean(catalog)
    catalog['release_note'] = 'Machine-local evidence paths omitted. Historical catalog observations are metadata snapshots, not a current live-delivery guarantee.'
    return (json.dumps(catalog, ensure_ascii=False, indent=2) + '\n').encode('utf-8'), removed[0]


def scan_public(files):
    findings = []
    for name, data in files.items():
        parts = set(Path(name).parts)
        if parts & BLOCKED_PARTS or '.local.' in name or name.endswith(('.cookies', '.log', '.key', '.pem')):
            findings.append(name + ': blocked path')
        if Path(name).suffix in ('.png',):
            continue
        text = data.decode('utf-8')
        if re.search(r'(?:gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|\blm_[A-Za-z0-9_-]{35,})', text):
            findings.append(name + ': credential-shaped content')
        if re.search(r'[A-Za-z]:\\(?:Users|Python)\\', text):
            findings.append(name + ': machine-local absolute path')
    if findings:
        raise ValueError('\n'.join(findings))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'release-artifacts')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    version = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Invalid version')
    name = 'linsemail-' + version + '-source'
    content = {relative: (ROOT / relative).read_bytes() for relative in release_files()}
    content['data/providers.json'], removed = public_catalog(content['data/providers.json'])
    scan_public(content)
    manifest = {'project': 'Linsemail', 'version': version, 'kind': 'source',
                'excludes': sorted(BLOCKED_PARTS) + ['runtime credentials', 'mailbox contents', 'compiled bundles'],
                'sanitized_catalog_path_fields': removed,
                'files': {key: {'bytes': len(value), 'sha256': hashlib.sha256(value).hexdigest()} for key, value in sorted(content.items())}}
    content['release-manifest.json'] = (json.dumps(manifest, indent=2) + '\n').encode('utf-8')
    target = output / name
    with tempfile.TemporaryDirectory(prefix='.linsemail-build-', dir=output) as scratch:
        staging = Path(scratch) / name
        staging.mkdir()
        for relative, data in content.items():
            dest = staging / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        zip_path = Path(scratch) / (name + '.zip')
        with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for relative, data in sorted(content.items()):
                info = zipfile.ZipInfo(name + '/' + relative, FIXED_TIME)
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data)
        tar_path = Path(scratch) / (name + '.tar.gz')
        with tar_path.open('wb') as stream:
            with gzip.GzipFile(filename='', mode='wb', fileobj=stream, mtime=0, compresslevel=9) as compressed:
                with tarfile.open(fileobj=compressed, mode='w') as archive:
                    for relative, data in sorted(content.items()):
                        info = tarfile.TarInfo(name + '/' + relative)
                        info.size, info.mtime, info.mode = len(data), 0, 0o644
                        archive.addfile(info, io.BytesIO(data))
        if target.exists():
            prior = target / 'release-manifest.json'
            if not prior.is_file() or json.loads(prior.read_text())['project'] != 'Linsemail':
                raise FileExistsError('Refusing to replace a non-release directory: ' + str(target))
            assert target.resolve().parent == output and target.name == name
            shutil.rmtree(target)
        shutil.move(str(staging), str(target))
        for archive in (zip_path, tar_path):
            archive.replace(output / archive.name)
    archives = {suffix: output / (name + suffix) for suffix in ('.zip', '.tar.gz')}
    sums = ''.join(hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.name + '\n' for path in archives.values())
    (output / 'SHA256SUMS').write_text(sums, encoding='ascii')
    print(json.dumps({'source': str(target), 'files': len(content), 'sanitized_catalog_path_fields': removed,
                      'archives': {key: {'path': str(path), 'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()} for key, path in archives.items()}}, indent=2))


if __name__ == '__main__':
    main()
