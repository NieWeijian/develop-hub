"""Pure deployment checks, shared by plan validation and the execution boundary."""
import re


def metadata_error(device: dict, bundle: dict, expected_version: str | None) -> str | None:
    if device.get('node', {}).get('kind') == 'simulation':
        return None
    if not isinstance(expected_version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,127}', expected_version):
        return '请填写完整的预期固件版本，例如 factory-V2.0.7。'
    return None
