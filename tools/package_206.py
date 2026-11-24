# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0
"""Build source and installable assets using verified 2.0.5 platform dependencies.

Never copy application code or the old Linux executable from a baseline.
The Linux installer uses Python 3.12 + wheels, not a frozen native binary.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import tempfile
import shutil
import tomllib
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
VERSION = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
BASE_VERSION = '2.0.5'
WIN_SHA = '5e583db4defd25118dcd2923697ddb7d7a9374c94eb9b6162466841fde9d4d92'
LINUX_SHA = 'b70ba2a63db2420a6398237c0e4f49a02a4038ae89be4fef847aaa91dff2969a'
BASE_PREFIX = f'PTFree-{BASE_VERSION}/'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def verified_baseline(path, expected, platform):
    if digest(path.read_bytes()) != expected:
        raise ValueError(f'Unexpected {platform} baseline archive')
    with ZipFile(path) as z:
        if z.testzip() is not None:
            raise ValueError('Corrupt baseline ZIP')
        manifest = json.loads(z.read(BASE_PREFIX + 'release-manifest.json'))
        if manifest['version'] != BASE_VERSION or manifest['platform'] != platform:
            raise ValueError('Baseline version/platform mismatch')
        members = {name[len(BASE_PREFIX):]: name for name in z.namelist()
                   if name.startswith(BASE_PREFIX) and not name.endswith('/')}
        if set(members) != set(manifest['files']) | {'release-manifest.json'}:
            raise ValueError('Baseline member list mismatch')
        for name, expected_hash in manifest['files'].items():
            p = PurePosixPath(name)
            if p.is_absolute() or '..' in p.parts or '\\' in name:
                raise ValueError('Unsafe baseline path')
            if digest(z.read(members[name])) != expected_hash:
                raise ValueError(f'Baseline file hash mismatch: {name}')
        return manifest['files']


def copy_members(path, names, stage, prefixes):
    with ZipFile(path) as z:
        for name in names:
            if any(name.startswith(prefix) for prefix in prefixes):
                dest = stage / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(z.read(BASE_PREFIX + name))


def ignored(directory, names):
    return [name for name in names if name == '__pycache__' or name.endswith(('.pyc', '.pyo'))]


def copy_tree(source, dest):
    shutil.copytree(source, dest, ignore=ignored)


def common(stage):
    for folder in ('ptfree', 'tests', 'tools'):
        copy_tree(ROOT / folder, stage / folder)
    for filename in ('requirements.txt', 'pyproject.toml', 'README.md', 'README.en.md', 'README-中文.md',
                     'TEST-REPORT.md', 'LICENSE', 'NOTICE', 'THIRD-PARTY-NOTICES.md', f'UPGRADE-{VERSION}.md'):
        shutil.copy2(ROOT / filename, stage / filename)


def package(stage, platform, output):
    files = {p.relative_to(stage).as_posix(): digest(p.read_bytes())
             for p in sorted(stage.rglob('*')) if p.is_file()}
    manifest = {'application': 'PTFree', 'version': VERSION, 'platform': platform, 'files': files}
    (stage / 'release-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    target = output / f'PTFree-{VERSION}-{platform}.zip'
    with ZipFile(target, 'w', ZIP_DEFLATED, compresslevel=6) as z:
        for path in sorted(stage.rglob('*')):
            relative = path.relative_to(stage.parent).as_posix()
            if path.is_file():
                z.write(path, relative)
            elif not any(path.iterdir()):
                z.mkdir(relative + '/')
    with ZipFile(target) as z:
        assert z.testzip() is None
        for name, h in files.items():
            assert digest(z.read(f'{stage.name}/{name}')) == h, name
        assert set(n for n in z.namelist() if not n.endswith('/')) == {
            f'{stage.name}/{name}' for name in (*files, 'release-manifest.json')}
    print(f'{target.name}: {target.stat().st_size} bytes; {len(files)} files verified; SHA256 {digest(target.read_bytes())}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--windows-baseline', type=Path, required=True)
    parser.add_argument('--linux-baseline', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    win = verified_baseline(args.windows_baseline, WIN_SHA, 'Windows-x64')
    linux = verified_baseline(args.linux_baseline, LINUX_SHA, 'Linux-x86_64-Installer') if args.linux_baseline else None
    for platform in ('Source', 'Windows-x64', 'Linux-x86_64-Installer', 'Synology-Freeseed'):
        if platform.startswith('Linux') and linux is None:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / ('PTFree' if platform == 'Source' else f'PTFree-{VERSION}')
            stage.mkdir()
            if platform == 'Synology-Freeseed':
                syn = ROOT / 'synology'
                copy_tree(syn / 'ptfree', stage / 'ptfree')
                for name in ('Dockerfile', '.dockerignore', 'docker-compose.yml', 'README-群晖.md', 'requirements.txt',
                             'pyproject.toml', 'LICENSE', 'NOTICE', 'THIRD-PARTY-NOTICES.md'):
                    shutil.copy2(syn / name, stage / name)
                (stage / 'data').mkdir(); (stage / 'watch').mkdir()
            else:
                common(stage)
                if platform in ('Source', 'Windows-x64'):
                    for p in (*ROOT.glob('*.ps1'), *ROOT.glob('*.bat')):
                        if platform == 'Windows-x64':
                            (stage / p.name).write_bytes(p.read_text(encoding='utf-8').replace('\n', '\r\n').encode('utf-8'))
                        else:
                            shutil.copy2(p, stage / p.name)
                if platform == 'Source' or platform.startswith('Linux'):
                    for p in ROOT.glob('*linux.sh'):
                        shutil.copy2(p, stage / p.name)
                        (stage / p.name).chmod(0o755)
                if platform == 'Source':
                    copy_tree(ROOT / 'synology', stage / 'synology')
                    for name in ('.gitattributes', '.gitignore', 'PUBLISHING.md'):
                        shutil.copy2(ROOT / name, stage / name)
                elif platform == 'Windows-x64':
                    copy_members(args.windows_baseline, win, stage, ('runtime/', 'third-party-licenses/'))
                    assert (stage / 'runtime/python.exe').read_bytes()[:2] == b'MZ'
                    assert (stage / 'runtime/pythonw.exe').read_bytes()[:2] == b'MZ'
                    assert (stage / 'runtime/python314._pth').read_text().splitlines() == ['python314.zip', '.', '..', 'Lib/site-packages', 'import site']
                    assert not any((stage / 'runtime').rglob('ptfree')) and not any((stage / 'runtime').rglob('mtfree'))
                elif platform.startswith('Linux'):
                    copy_members(args.linux_baseline, linux, stage, ('wheels/', 'third-party-licenses/'))
                    # This is intentionally not a native package. No old executable is shipped.
                    assert not (stage / 'bin').exists()
                    (stage / 'LINUX-INSTALLER.md').write_text('# Linux x86_64 安装版\n\n需要 Ubuntu 24.04 / Python 3.12 与 python3-venv。附带对应依赖 wheel，无需单独下载依赖（匹配平台时）。不含独立原生二进制。\n\n运行 `bash install-linux.sh` 安装，再运行 `bash run-linux.sh`。\n', encoding='utf-8')
            package(stage, platform, args.output)


if __name__ == '__main__':
    main()
