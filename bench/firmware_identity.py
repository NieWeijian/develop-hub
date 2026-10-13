"""Firmware identity shared by import validation, boot verification and cases.

Image digests establish the delivered bytes. This identity separately verifies
what the device reports after boot; it never replaces the USB/CPU/slot checks.
"""
import re

_COMMIT = re.compile(r'[0-9a-fA-F]{7,40}')
_SLOT = re.compile(r'(?:\s+_?[abAB]|_[abAB])$')
_VERSION_OUTPUT = re.compile(r'^.*?\bversion:\s*(\S+)(?:\s+\(compile\s+[^)]*\))?$')


def _without_slot(value):
    return _SLOT.sub('', value.strip()).rstrip()


def normalize_identity(identity):
    """Return a canonical identity or raise ValueError for invalid metadata."""
    if not isinstance(identity, dict) or set(identity) != {'kind', 'value'}:
        raise ValueError('Firmware identity requires kind and value')
    kind, value = identity['kind'], identity['value']
    if kind not in ('commit', 'version') or not isinstance(value, str):
        raise ValueError('Firmware identity kind must be commit or version')
    value = value.strip()
    if kind == 'commit':
        if not _COMMIT.fullmatch(value):
            raise ValueError('Firmware commit must contain 7 to 40 hexadecimal characters')
        value = value.lower()
    else:
        if len(value) > 1024 or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError('Firmware version must be a single line of at most 1024 characters')
        value = _without_slot(value)
        if not value:
            raise ValueError('Firmware version must not be empty')
    return {'kind': kind, 'value': value}


validate_identity = normalize_identity


def source_identity(source):
    """Legacy sources may omit identity, but cannot supply an empty commit."""
    if not isinstance(source, dict):
        raise ValueError('Firmware source metadata is required')
    if 'identity' in source:
        return normalize_identity(source['identity'])
    return normalize_identity({'kind': 'commit', 'value': source.get('commit')})


def matches_firmware_identity(identity, observed):
    """Match a validated identity without accepting version substrings.

Legacy Git builds retain the seven-character commit comparison, with token
boundaries and an optional git-describe g prefix. Full version output retains
its compile timestamp; a bare version token matches only the entire token.
"""
    identity = normalize_identity(identity)
    if not isinstance(observed, str) or not observed.strip():
        return False
    expected = identity['value']
    if identity['kind'] == 'commit':
        expression = r'(?<![0-9a-z])g?' + re.escape(expected[:7]) + r'[0-9a-f]{0,33}(?![0-9a-z])'
        return re.search(expression, observed, re.IGNORECASE) is not None
    actual = _without_slot(observed)
    if actual == expected:
        return True
    # Full output expectations must also match the compile timestamp exactly.
    if re.search(r'\s', expected):
        return False
    match = _VERSION_OUTPUT.fullmatch(actual)
    return match is not None and match[1] == expected
