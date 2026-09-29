import json
from pathlib import Path
import pytest
from develop_hub import basic_cases


def test_imported_package_is_arm64_and_supports_temperature():
    definition = basic_cases.definition('INFO-TEMPERATURE')
    lock = definition['tools_package']
    assert lock['upstream_commit'] == 'cc3f2345245b3d8347d4ef604bb80b8a9e330546'
    assert lock['target'] == 'linux-arm64'
    assert 'temperature.read.v1' in lock['capabilities']
    assert definition['missing_capabilities'] == []
    binary = (basic_cases.ROOT / lock['binary_path']).read_bytes()
    assert int.from_bytes(binary[18:20], 'little') == 183
    assert b'get-temperature' in binary
    for filename, expected in lock['files'].items():
        assert basic_cases.sha(basic_cases.ROOT / 'tool-packages' / lock['package_id'] / filename) == expected


def test_historical_tool_kept_and_unchanged():
    manifest = json.loads((basic_cases.ROOT / 'tool-packages/ctl-trace-1/manifest.json').read_text())
    assert basic_cases.sha(basic_cases.ROOT / manifest['binary_path']) == manifest['binary_sha256']
    assert manifest['binary_sha256'] == '8d4003b77f131b8f3bd26c8507332501a5e69161d1b9658aabe4db9720add24b'


def test_definition_rejects_tampered_binary(monkeypatch):
    original = basic_cases.sha
    monkeypatch.setattr(basic_cases, 'sha', lambda p: 'tampered' if p.name == 'uvc_host_ctl' else original(p))
    with pytest.raises(ValueError, match='checksum mismatch'):
        basic_cases.definition('INFO-TEMPERATURE')
