import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[3]/'bench'
CODES=('BASIC-VERSION','BASIC-FOV-READ','BASIC-FOV-WRITE','BASIC-FILE-ROUNDTRIP','SWITCH-ROTATE-CONTROL')+tuple(json.loads((ROOT/'switch-codes.json').read_text()))+tuple(json.loads((ROOT/'info-codes.json').read_text()))

RETIRED_CODES=('INFO-IMU', 'SWITCH-ALGO-ASYNC', 'SWITCH-ALGO-DENOISE', 'SWITCH-ALGO-ITER', 'SWITCH-IR-BTNR')
ACTIVE_CODES=tuple(code for code in CODES if code not in RETIRED_CODES)

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()

def definition(code):
    if code not in CODES: raise ValueError('Case adapter not available')
    folder=ROOT/'test-cases'/code/'1.0.0'
    data=json.loads((folder/'case.json').read_text())
    data['tool_sha256']=sha(ROOT/'test-bins/ctl-trace-1/uvc_host_ctl')
    data['runtime_sha256']=sha(ROOT/'test_runtime.py')
    data['entrypoint_sha256']=sha(folder/'run.py')
    data['definition_sha256']=sha(folder/'case.json')
    if code in json.loads((ROOT/'switch-codes.json').read_text()):
        data['support_sha256']={name:sha(ROOT/name) for name in ('switch_runtime.py','frame_images.py','host-capture','rv1126b.py')}
        data['runtime_sha256']=sha(ROOT/'test_runtime.py')
    if code in json.loads((ROOT/'info-codes.json').read_text()):
        data['support_sha256']={name:sha(ROOT/name) for name in ('telemetry_runtime.py','switch_runtime.py','frame_images.py','host-capture','host-imu','rv1126b.py')}
    return data


def report_passed(report, case_ids):
    cases = report.get('cases') or []
    return (report.get('state') == 'finished'
            and report.get('phase') == 'tests_complete'
            and report.get('outcome') == 'passed'
            and report.get('flash_exit_code') == 0
            and report.get('boot_verified') is True
            and len(cases) == len(case_ids)
            and {case.get('id') for case in cases} == set(case_ids)
            and all(case.get('state') == 'finished'
                    and case.get('outcome') == 'passed'
                    and case.get('assertions_total', 0) > 0
                    and case.get('assertions_passed') == case.get('assertions_total')
                    and (case.get('recovery') or {}).get('state') != 'failed'
                    for case in cases))


def validate_parameters(code, parameters):
    import math
    from fastapi import HTTPException
    spec = definition(code).get('parameters', {})
    if set(parameters) - set(spec):
        raise HTTPException(422, 'Unsupported case parameters')
    if not code.startswith('INFO-'):
        return
    for key, value in parameters.items():
        maximum = {'max_rtt_ms': 30000, 'window_seconds': 30, 'fps_tolerance_percent': 50}[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= maximum:
            raise HTTPException(422, 'Invalid parameter: ' + key)
