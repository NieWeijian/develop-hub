#!/usr/bin/env python3
"""Fail before synchronization when tracked files contain runtime data or credentials."""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{30,}')
FORBIDDEN_PARTS = {'.venv', 'node_modules', '__pycache__', '.ssh', 'var', 'backups', 'evidence', 'runtime-cache', 'releases', 'packages'}


def findings(root=ROOT):
    names = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z']).split(b'\0')
    problems = []
    for raw in names:
        if not raw:
            continue
        name = raw.decode()
        path = root / name
        parts = Path(name).parts
        base = path.name
        template = base.endswith('.example')
        blocked_name = (base == '.env' or base.startswith('.env.') or base.endswith(('.db', '.db-wal', '.db-shm', '.sqlite', '.sqlite3', '.pem', '.key', '.ppk', '.whl', '.bundle', '.plan.json', '.deployment.json'))) and not template
        if any(part in FORBIDDEN_PARTS for part in parts) or blocked_name:
            problems.append((name, 'runtime/private file is tracked'))
        elif path.is_symlink():
            problems.append((name, 'review tracked symlink explicitly'))
        elif path.is_file() and path.stat().st_size <= 4 * 1024 * 1024:
            if PRIVATE.search(path.read_bytes().decode('utf-8', errors='ignore')):
                problems.append((name, 'credential pattern detected (value suppressed)'))
    return problems


if __name__ == '__main__':
    failures = findings()
    for path, reason in failures:
        print(path + ': ' + reason)
    print('Tracked-file synchronization audit:', 'FAILED' if failures else 'passed')
    sys.exit(bool(failures))
