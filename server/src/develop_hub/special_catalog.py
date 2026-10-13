"""Registered operator-gated cases; never dispatched as automatic worker cases."""
from copy import deepcopy
import hashlib
import json
from sqlalchemy import select
from .basic_cases import ROOT
from .special_evidence_policy import POLICY as EVIDENCE_POLICY
from .models import TestCase, TestCaseRisk
from .special_definitions import definitions, definition as scenario_definition

CODES=tuple(item['code'] for item in definitions())
PACKAGE=('special_runtime.py','rv1126b.py','special_evidence.py','tools.lock.json')

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def package_identity():
    lock=json.loads((ROOT/'tools.lock.json').read_text())
    names=[*PACKAGE,lock['binary_path'],lock['manifest_path']]
    names=[name for name in names if name!='special_evidence.py' or (ROOT/name).exists()]
    hashes={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in names}
    if hashes[lock['binary_path']]!=lock['binary_sha256'] or hashes[lock['manifest_path']]!=lock['manifest_sha256']:
        raise ValueError('Frozen special tool package checksum mismatch')
    return dict(tools_package=lock,package_sha256=hashes,evidence_policy=deepcopy(EVIDENCE_POLICY))

def definition(code):
    spec=scenario_definition(code)
    if spec is None:raise ValueError('Operator-gated case not available')
    return {**spec,'execution_mode':'gated','definition_sha256':digest(spec),**package_identity()}

def matches(adapter,run):
    spec={k:deepcopy(adapter[k]) for k in ('code','name','version','description','steps')}
    return (run.definition_snapshot==spec and run.context.get('definition_sha256')==digest(spec)
        and all(run.tool_snapshot.get(key)==adapter.get(key) for key in ('tools_package','package_sha256','evidence_policy')))

def install_missing(db):
    added=[]
    for item in definitions():
        if db.scalar(select(TestCase).where(TestCase.code==item['code'])) is not None:continue
        db.add(TestCase(code=item['code'],name=item['name'],category='special',
            description='Version '+item['version']+'; 人工门控：'+item['description'],
            command='operator-gated:'+item['code'],timeout_seconds=90000,expected_exit_code=0,
            risk_level=TestCaseRisk.FIRMWARE_WRITE if item['code']=='SPECIAL-OTA' else TestCaseRisk.STATE_CHANGE,enabled=True))
        added.append(item['code'])
    db.flush()
    return added
