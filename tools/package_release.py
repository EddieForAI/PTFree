# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

"""Package reviewed source plus optional platform runtimes, without local user data."""
import argparse
import hashlib
import importlib.metadata
import json
import shutil
import tempfile
import tomllib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

parser=argparse.ArgumentParser()
windows=parser.add_mutually_exclusive_group(required=True)
windows.add_argument('--windows-python',type=Path)
windows.add_argument('--windows-baseline',type=Path)
parser.add_argument('--windows-wheels',type=Path)
parser.add_argument('--linux-wheels',type=Path)
parser.add_argument('--linux-binary',type=Path,required=True)
parser.add_argument('--linux-python-license',type=Path,required=True,
                    help='CPython license from the interpreter bundled into the Linux binary')
parser.add_argument('--openssl-license',type=Path,required=True,
                    help='Applicable upstream OpenSSL license for the bundled runtimes')
parser.add_argument('--include-synology',action='store_true')
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()
root=Path(__file__).resolve().parents[1]
version=tomllib.loads((root/'pyproject.toml').read_text())['project']['version']
args.output.mkdir(parents=True,exist_ok=True)
official='90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159'
if args.windows_python:
    assert args.windows_wheels
    assert hashlib.sha256(args.windows_python.read_bytes()).hexdigest()==official
else:
    known_baselines = {
        '3203488563f4c5e93a7a1c4b1921c759b80ce684b5dd606cec920550eb263ef9': 'PTFree-2.0.3',
        'a98c0c7c2dd4eaead06eaf8d3382093b7012f6ec1c91cb0bedb519131d137192': 'MTFree-1.3.8',
        '018efd1f5d2a1247dca79d7cb4504df128a71013f452f14d5c656a27faa536d8': 'MTFree-1.3.9',
    }
    baseline_hash = hashlib.sha256(args.windows_baseline.read_bytes()).hexdigest()
    if baseline_hash not in known_baselines:
        parser.error('Unrecognized Windows baseline archive')
    runtime_prefix = f'{known_baselines[baseline_hash]}/runtime/'


def unpack(zip_path,dest):
    with ZipFile(zip_path) as z:
        assert all(not Path(n).is_absolute() and '..' not in Path(n).parts for n in z.namelist())
        z.extractall(dest)


def write_release(stage, platform):
    files = sorted(path for path in stage.rglob('*') if path.is_file())
    manifest = {
        'application': 'PTFree', 'version': version, 'platform': platform,
        'files': {path.relative_to(stage).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in files},
    }
    manifest_file = stage / 'release-manifest.json'
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    target = args.output / f'PTFree-{version}-{platform}.zip'
    with ZipFile(target, 'w', ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(stage.rglob('*')):
            relative = path.relative_to(stage.parent).as_posix()
            if path.is_file():
                archive.write(path, relative)
            elif not any(path.iterdir()):
                archive.mkdir(relative + '/')
    print(json.dumps({'file': str(target), 'bytes': target.stat().st_size,
                      'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}))


for platform in ('Windows-x64','Linux-x86_64'):
    with tempfile.TemporaryDirectory() as tmp:
        stage=Path(tmp)/f'PTFree-{version}';stage.mkdir()
        for folder in ('ptfree','tests','tools'):
            shutil.copytree(root/folder,stage/folder,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        for filename in ('requirements.txt','pyproject.toml','README-中文.md','TEST-REPORT.md','LICENSE','NOTICE','THIRD-PARTY-NOTICES.md'):
            shutil.copy2(root/filename,stage/filename)
        notices = stage / 'third-party-licenses'
        notices.mkdir()
        shutil.copy2(args.openssl_license, notices / 'OpenSSL-LICENSE.txt')
        if platform.startswith('Windows'):
            for source in [*root.glob('*[Ww]indows*.ps1'),*root.glob('*.bat')]:
                (stage/source.name).write_bytes(source.read_text().replace('\r\n','\n').replace('\n','\r\n').encode())
            runtime=stage/'runtime';runtime.mkdir()
            if args.windows_baseline:
                with ZipFile(args.windows_baseline) as archive:
                    prefix=runtime_prefix
                    for entry in archive.namelist():
                        if entry.startswith(prefix) and not entry.endswith('/'):
                            target=runtime/entry[len(prefix):]
                            target.parent.mkdir(parents=True,exist_ok=True)
                            target.write_bytes(archive.read(entry))
                if not all((runtime / name).is_file() for name in ('python.exe', 'pythonw.exe', 'python314._pth')):
                    parser.error('Windows baseline does not contain the expected embedded runtime')
            else:
                unpack(args.windows_python,runtime)
                packages=runtime/'Lib'/'site-packages';packages.mkdir(parents=True)
                for wheel in sorted(args.windows_wheels.glob('*.whl')):unpack(wheel,packages)
                (runtime/'python314._pth').write_text('python314.zip\n.\n..\nLib/site-packages\nimport site\n')
        else:
            for source in root.glob('*linux.sh'):
                shutil.copy2(source,stage/source.name);(stage/source.name).chmod(0o755)
            (stage/'bin').mkdir();shutil.copy2(args.linux_binary,stage/'bin'/'ptfree');(stage/'bin'/'ptfree').chmod(0o755)
            if args.linux_wheels:
                (stage/'wheels').mkdir()
                for source in sorted(args.linux_wheels.glob('*.whl')):
                    if source.name.startswith(('cryptography-', 'cffi-', 'pycparser-')):
                        shutil.copy2(source,stage/'wheels'/source.name)
            shutil.copy2(args.linux_python_license, notices / 'CPython-LICENSE.txt')
            for package in ('cryptography','cffi','pycparser','pyinstaller'):
                dist=importlib.metadata.distribution(package)
                for path in dist.files or []:
                    if '.dist-info' in str(path) and any(x in path.name.lower() for x in ('license','copying','notice')):
                        src=dist.locate_file(path)
                        if src.is_file():
                            dst=notices/package/str(path);dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src,dst)
        write_release(stage, platform)

if args.include_synology:
    platform = 'Synology-Freeseed'
    synology = root / 'synology'
    synology_version = tomllib.loads((synology / 'pyproject.toml').read_text())['project']['version']
    if synology_version != version:
        parser.error('Desktop and Synology versions differ')
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / f'PTFree-{version}-{platform}'
        stage.mkdir()
        shutil.copytree(synology / 'ptfree', stage / 'ptfree',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for filename in ('Dockerfile', '.dockerignore', 'docker-compose.yml', 'README-群晖.md',
                         'requirements.txt', 'pyproject.toml', 'LICENSE', 'NOTICE', 'THIRD-PARTY-NOTICES.md'):
            shutil.copy2(synology / filename, stage / filename)
        for directory in ('data', 'watch'):
            (stage / directory).mkdir()
        write_release(stage, platform)
