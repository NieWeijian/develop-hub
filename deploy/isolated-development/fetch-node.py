"""Fetch a checksum-pinned official Node archive; no host Node installation needed."""
import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
import time
import urllib.request

lock = json.loads(Path('/toolchain/toolchain.lock.json').read_text())
archive = Path('/tmp/node.tar.xz')
for attempt in range(4):
    try:
        with urllib.request.urlopen(lock['node_url'], timeout=90) as source, archive.open('wb') as output:
            while chunk := source.read(1024 * 1024):
                output.write(chunk)
        break
    except OSError:
        if attempt == 3:
            raise
        time.sleep(2)
assert hashlib.sha256(archive.read_bytes()).hexdigest() == lock['node_sha256'], 'Node checksum mismatch'
with tarfile.open(archive) as source:
    source.extractall('/opt', filter='data')
Path('/opt/node-v' + lock['node_version'] + '-linux-x64').rename('/opt/node')
archive.unlink()
