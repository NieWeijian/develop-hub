"""Install tool packages from an immutable Debian archive snapshot."""
import hashlib
import json
from pathlib import Path
import subprocess

lock = json.loads(Path('/toolchain/toolchain.lock.json').read_text())
assert hashlib.sha256(Path('/usr/local/bin/repo').read_bytes()).hexdigest() == lock['repo_launcher_sha256']
sources = Path('/etc/apt/sources.list.d/debian.sources')
if sources.exists():
    sources.unlink()
Path('/etc/apt/sources.list').write_text('deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/' + lock['debian_snapshot'] + '/ trixie main\n')
subprocess.run(['apt-get', '-o', 'Acquire::Check-Valid-Until=false', '-o', 'Acquire::Retries=3', 'update'], check=True)
subprocess.run(['apt-get', 'install', '-y', '--no-install-recommends', 'git=' + lock['git_package'], 'g++=' + lock['gxx_package']], check=True)
