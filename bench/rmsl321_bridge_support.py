"""Per-process access to platform-installed tools, with content verification."""
import hashlib
from pathlib import Path

from testing_rv1126b_driver import inspect_device

_config = None


def configure(config):
    global _config
    _config = config
    for name in config.get('helpers', {}):
        checked(name)


def checked(name):
    value = _config['helpers'][name]
    path = Path(value['path'])
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != value['sha256']:
        raise RuntimeError('平台采集工具校验失败：' + name)
    return path


def inspect():
    return inspect_device(_config)


def discover():
    try:
        result = inspect()
        return [{**result, 'version': result['firmware_version']}]
    except RuntimeError as error:
        # The maintained runtimes poll for disappearance/reappearance on reboot.
        if 'Expected exactly one module' in str(error):
            return []
        raise
