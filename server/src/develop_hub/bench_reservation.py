"""Cross-process reservation guard shared by ordinary and manual test workflows."""
from contextlib import contextmanager
import fcntl
import hashlib
from .config import settings

@contextmanager
def bench_guard(bench_id):
    root=settings.artifact_root/'bench-reservations'
    root.mkdir(parents=True,exist_ok=True)
    path=root/(hashlib.sha256(bench_id.encode()).hexdigest()+'.lock')
    with path.open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle,fcntl.LOCK_UN)
