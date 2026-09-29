"""Developer rounds use observed module firmware, never a fabricated Gerrit point."""
import base64
import hashlib
import json
from pathlib import Path
import shlex
from fastapi import HTTPException
from sqlalchemy import select
from .basic_cases import ROOT, definition, sha
from .models import TestRound, TestRoundEvent, TestRoundStatus, TestCase, TestBench, new_id
from .db import SessionLocal

from .basic_cases import ACTIVE_CODES, validate_parameters
QUICK_CODES=tuple(code for code in json.loads((ROOT/'switch-codes.json').read_text())+json.loads((ROOT/'info-codes.json').read_text()) if code in ACTIVE_CODES)
PACKAGE=['worker.py','quick_tests.py','switch_runtime.py','telemetry_runtime.py','frame_images.py','test_runtime.py','rv1126b.py','host-capture','host-imu','info-codes.json','tools.lock.json']

async def create(payload,db,workflow):
    from .test_workbench_api import product_query, round_query, next_round_number, bench_devices
    from .models import TestProduct
    product=db.scalar(product_query().where(TestProduct.id==payload.product_id))
    bench=db.get(TestBench,payload.bench_id)
    if not product or not bench or not bench.enabled:raise HTTPException(404,'Product or bench unavailable')
    if payload.flash_firmware or not payload.execute_test_cases:raise HTTPException(422,'Developer mode requires tests enabled and flash disabled')
    if len(payload.test_case_ids)!=len(set(payload.test_case_ids)):raise HTTPException(422,'Duplicate cases')
    cases=[]
    for cid in payload.test_case_ids:
        case=db.get(TestCase,cid)
        if not case or not case.enabled or case.code not in QUICK_CODES:raise HTTPException(422,'Developer mode supports switching and information cases')
        adapter=definition(case.code)
        if adapter.get('missing_capabilities'):raise HTTPException(422,'Tool unsupported: '+', '.join(adapter['missing_capabilities']))
        parameters=payload.test_case_parameters.get(cid, {})
        validate_parameters(case.code, parameters)
        cases.append(dict(id=cid,code=case.code,name=case.name,category=case.category,adapter=adapter,parameters=parameters))
    if set(payload.test_case_parameters)-set(payload.test_case_ids):raise HTTPException(422,'Unselected case parameters')
    devices=await bench_devices(bench.id,'RV1126B',db,workflow)
    matches=[d for d in devices if d['serial']==payload.device_serial]
    if len(matches)!=1:raise HTTPException(422,'Select one connected module')
    device=matches[0];number=next_round_number(db)
    row=TestRound(id=new_id(),display_number=number,product_id=product.id,test_point_id=None,bench_id=bench.id,
        product_snapshot={'id':product.id,'name':product.name,'developer_mode':True,'execute_test_cases':True,'device':device,
                          'test_point':{'subject':'当前模组固件 · '+device['version']}},
        bench_snapshot={k:getattr(bench,k) for k in ('id','name','host','port','user','remote_root')},
        manifest_commit='',board_config=product.board_config,gerrit_project='',change_number=0,patchset_number=0,commit_sha='',work_branch='',test_case_snapshot=cases)
    row.events.append(TestRoundEvent(sequence=1,event_type='created',status='succeeded',message='开发者模式：锁定当前固件、设备和用例；跳过编译烧录',details={'device':device}))
    db.add(row);db.commit();rid=row.id
    if payload.auto_start:await workflow.start(rid)
    db.expire_all();return db.scalar(round_query().where(TestRound.id==rid))

async def dispatch(workflow,rid):
    try:
        with SessionLocal() as db:
            row=db.get(TestRound,rid);bench=TestBench(**row.bench_snapshot)
            for c in row.test_case_snapshot:
                frozen=c['adapter']['tools_package']
                if sha(ROOT/frozen['binary_path'])!=frozen['binary_sha256'] or sha(ROOT/frozen['manifest_path'])!=frozen['manifest_sha256']:
                    raise RuntimeError('Frozen tools package changed since round creation')
            tool_files=sorted({path for c in row.test_case_snapshot for path in (c['adapter']['tools_package']['binary_path'], c['adapter']['tools_package']['manifest_path'])})
            files=PACKAGE+tool_files+['test-cases/'+c['code']+'/1.0.0/'+name for c in row.test_case_snapshot for name in ('run.py','case.json')]
            config={'schema_version':2,'round_id':f'TEST-{row.display_number:06d}','execution_boundary':'current_firmware','execute_test_cases':True,
                    'tools_package':row.test_case_snapshot[0]['adapter']['tools_package'],
                    'device':row.product_snapshot['device'],'test_cases':row.test_case_snapshot,'package_sha256':{name:sha(ROOT/name) for name in files}}
            data=json.dumps(config,ensure_ascii=False,indent=2).encode();checksum=hashlib.sha256(data).hexdigest()
            remote=bench.remote_root+'/jobs/'+config['round_id']
            row.generated_config=config;row.remote_path=remote;row.status=TestRoundStatus.TRANSFERRING;db.commit()
        # mkdir fails if a prior task directory exists. Never replace a previous run.
        code="import base64,pathlib,hashlib;p=pathlib.Path("+repr(remote)+");p.mkdir();b=base64.b64decode("+repr(base64.b64encode(data).decode())+");(p/'test-run.json').write_bytes(b);print(hashlib.sha256(b).hexdigest())"
        response=await workflow._remote(bench,'python3 -c '+shlex.quote(code))
        if response['exit_code'] or response['stdout_tail'].strip()!=checksum:raise RuntimeError('Quick test configuration delivery failed')
        local=ROOT.parent/'evidence'/config['round_id'];local.mkdir(parents=True,exist_ok=True);(local/'test-run.json').write_bytes(data)
        import shutil
        for name,expected in config['package_sha256'].items():
            destination=local/'test-package'/name;destination.parent.mkdir(parents=True,exist_ok=True)
            if sha(ROOT/name)!=expected:raise RuntimeError('Test package changed during dispatch')
            shutil.copy2(ROOT/name,destination)
        with SessionLocal() as db:
            row=db.get(TestRound,rid);row.delivery_result={'passed':True,'files':[{'filename':'test-run.json','sha256':checksum,'size_bytes':len(data)}]};db.commit()
        await workflow._dispatch_bench(rid)
    except Exception as exc:
        with SessionLocal() as db:
            row=db.get(TestRound,rid);row.status=TestRoundStatus.BLOCKED;row.last_error=str(exc);db.commit()

def passed(report,ids):
    cases=report.get('cases') or []
    return (report.get('execution_boundary')=='current_firmware'
        and report.get('identity_verified') is True and 'flash_exit_code' not in report
        and report.get('state')=='finished' and report.get('phase')=='tests_complete' and report.get('outcome')=='passed'
        and len(cases)==len(ids) and {c.get('id') for c in cases}==set(ids)
        and all(c.get('state')=='finished' and c.get('outcome')=='passed'
                and c.get('assertions_total',0)>0 and c.get('assertions_passed')==c.get('assertions_total')
                and (c.get('recovery') or {}).get('state')=='passed' for c in cases))
