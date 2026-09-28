import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[3]/'bench'
CODES=('BASIC-VERSION','BASIC-FOV-READ','BASIC-FOV-WRITE','BASIC-FILE-ROUNDTRIP','SWITCH-ROTATE-CONTROL')

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()

def definition(code):
    if code not in CODES: raise ValueError('Case adapter not available')
    folder=ROOT/'test-cases'/code/'1.0.0'
    data=json.loads((folder/'case.json').read_text())
    data['tool_sha256']=sha(ROOT/'test-bins/ctl-trace-1/uvc_host_ctl')
    data['runtime_sha256']=sha(ROOT/'test_runtime.py')
    data['entrypoint_sha256']=sha(folder/'run.py')
    data['definition_sha256']=sha(folder/'case.json')
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
