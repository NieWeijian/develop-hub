#!/usr/bin/env python3
"""Restore copies of a baseline and exercise production cutover inside network=none."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import time

import hub


def extract_checked(archive_path, destination):
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path) as archive:
        for member in archive:
            path = destination / member.name
            if not path.resolve().is_relative_to(destination.resolve()):
                raise ValueError('Unsafe backup path')
            if member.issym() and Path(member.linkname).is_absolute():
                # Interpreter aliases are recreated with the isolated runtime.
                continue
            if member.issym() and not (path.parent / member.linkname).resolve().is_relative_to(destination.resolve()):
                raise ValueError('Unsafe backup symlink')
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise ValueError('Unsupported backup entry')
            archive.extract(member, destination)


def repair_bin_links(modules):
    bins = modules / '.bin'
    bins.mkdir(exist_ok=True)
    packages = []
    for path in modules.iterdir():
        if path.name.startswith('@') and path.is_dir():
            packages.extend(path.iterdir())
        else:
            packages.append(path)
    for package in packages:
        manifest = package / 'package.json'
        if not manifest.is_file():
            continue
        data = json.loads(manifest.read_text())
        values = data.get('bin', {})
        if isinstance(values, str):
            values = {data['name'].split('/')[-1]: values}
        for name, script in values.items():
            target = bins / name
            if target.exists() or target.is_symlink():
                target.unlink()
            target.symlink_to(os.path.relpath(package / script, bins))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', required=True, type=Path)
    parser.add_argument('--backup-directory', required=True, type=Path)
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    root = hub.validate_root(args.root)
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    baseline = root / 'baseline'
    backup = args.backup_directory
    extract_checked(backup / 'source.tar.gz', baseline)
    extract_checked(backup / 'web-dist.tar.gz', baseline / 'web')
    extract_checked(backup / 'python-runtime.tar.gz', baseline)
    extract_checked(backup / 'node-dependencies.tar.gz', baseline / 'web')
    extract_checked(backup / 'node-runtime.tar.gz', baseline / 'runtime/node')
    extract_checked(backup / 'artifacts.tar.gz', baseline / 'var/preview')
    shutil.copy2(backup / 'testing.db', baseline / 'var/preview/testing.db')
    repair_bin_links(baseline / 'web/node_modules')
    # Relocate only the backup COPY's persisted local file references.
    import sqlite3
    with sqlite3.connect(baseline / 'var/preview/testing.db') as db:
        for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            if not name.startswith('testing_'):
                continue
            for row in db.execute('PRAGMA table_info("' + name + '")').fetchall():
                column, kind = row[1], row[2].upper()
                if kind in ['TEXT', 'JSON', 'VARCHAR'] or 'CHAR' in kind:
                    db.execute('UPDATE "' + name + '" SET "' + column + '"=replace("' + column + '", ?, ?) WHERE typeof("' + column + '")=\'text\'', ('/home/nwj/develop-hub-redesign', '/drill/baseline'))
    lock = json.loads((hub.HERE / 'production-runtime.lock.json').read_text())
    manifest = json.loads((args.package / 'package.json').read_text())
    image = 'develop-hub-production-probe:' + manifest['dependency_key'][:20]
    with (root / 'image-build.log').open('w') as output:
        subprocess.run(['docker', 'build', '--memory=4g', '--cpu-period=100000', '--cpu-quota=200000', '-t', image, '-f', str(hub.HERE / 'Dockerfile.production-test'), '--build-arg', 'TOOLS_IMAGE=' + manifest['image'], str(hub.REPO)], env=dict(os.environ, DOCKER_BUILDKIT='0'), stdout=output, stderr=subprocess.STDOUT, check=True)
    command = hub.common() + ['--rm', '--network=none', '--mount', f'type=bind,src={args.package.absolute()},dst=/candidate', '--mount', f'type=bind,src={root},dst=/drill', '-e', 'PYTHONPATH=/candidate/server/src', '-e', 'PYTHONDONTWRITEBYTECODE=1', '-e', 'HOME=/tmp', '-e', 'DEVELOP_HUB_ENV=test', image, 'sh', '-c', 'python -m pytest /candidate/server/tests && python /candidate/deploy/isolated-development/drill-in-container.py']
    with (root / 'drill.log').open('w') as output:
        subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, check=True)
    print((root / 'result.json').read_text())


if __name__ == '__main__':
    main()
