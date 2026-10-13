"""Use the same pure firmware identity rules as the bench executor."""
from importlib.util import module_from_spec, spec_from_file_location
from .basic_cases import ROOT

_spec = spec_from_file_location('_develop_hub_firmware_identity', ROOT / 'firmware_identity.py')
_module = module_from_spec(_spec)
_spec.loader.exec_module(_module)
validate_identity = _module.validate_identity
source_identity = _module.source_identity
matches_firmware_identity = _module.matches_firmware_identity
