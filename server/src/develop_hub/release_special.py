"""Candidate-bound operator gates with an uninterrupted bench reservation.

Every gate owns a dedicated candidate flash/boot round. Reads reconcile its
stored controller evidence; reads never dispatch or repeat hardware actions.
"""
from copy import deepcopy
import hashlib
import json
import re
from fastapi import HTTPException
from sqlalchemy import select
from .models import SpecialRun, TestRound, TestRoundStatus, TestProduct, TestCase, ReleaseCandidate, ReleasePlan, ValidationEvidence, new_id, utcnow
from .special_catalog import definition, matches
from .special_evidence_policy import remote_evidence_complete

VARIANTS=('corrupt','wrong_name','interrupted')

def paired_preparation(rnd,run):
    binding=run.context.get('release_binding',{})
    return (run.status=='preparing_firmware' and run.bench_reserved
        and rnd.product_snapshot.get('special_run_id')==run.id
        and binding.get('preparation_round_id')==rnd.id
        and binding.get('candidate_id')==rnd.candidate_id
        and binding.get('validation_plan_id')==rnd.validation_plan_id
        and run.product_id==rnd.product_id and run.bench_id==rnd.bench_id)

def attach_preparation(db,plan,candidate,rnd,bench,device,gated):
    from .testing_platform import spec_hash
    from .special_scenarios import freeze_package,event,digest
    expected,operator=gated
    req=next((r for r in plan.requirements if r['id']==expected['id']),None)
    case=db.get(TestCase,req['case_id']) if req else None
    if case:db.refresh(case)
    if req!=expected or not case or not case.enabled or req['mode']!='gated' or definition(req['code'])!=req['adapter']:
        raise HTTPException(409,'门控定义已更新，请显式同步清单')
    if not device.get('usb_port') or not device.get('usb_path') or not device.get('version'):
        raise HTTPException(422,'设备身份缺少物理端口或版本')
    for active in db.scalars(select(SpecialRun).where(SpecialRun.bench_reserved.is_(True))):
        owner=active.context.get('release_binding',{})
        if owner.get('validation_plan_id')==plan.id and owner.get('requirement_id')==req['id']:
            raise HTTPException(409,'该清单项已有未结束的人工门控，请继续或终止原运行')
    rid=new_id();frozen=freeze_package(rid)
    if any(frozen.get(k)!=req['adapter'].get(k) for k in ('tools_package','package_sha256','evidence_policy')):
        raise HTTPException(409,'冻结运行包与清单不一致，请同步清单后重试')
    spec={k:deepcopy(req['adapter'][k]) for k in ('code','name','version','description','steps')}
    product=db.get(TestProduct,candidate.product_id)
    binding=dict(validation_plan_id=plan.id,requirement_id=req['id'],requirement_hash=spec_hash(req),
        plan_revision=plan.revision,candidate_id=candidate.id,candidate_fingerprint=candidate.fingerprint,
        product_id=candidate.product_id,case_id=case.id,preparation_round_id=rnd.id,
        original_device=deepcopy(device))
    run=SpecialRun(id=rid,scenario_code=req['code'],product_id=candidate.product_id,bench_id=bench.id,
        status='preparing_firmware',revision=1,current_step_index=0,bench_reserved=True,
        definition_snapshot=spec,product_snapshot={k:getattr(product,k) for k in
            ('id','code','name','board_config','gerrit_project','manifest_snapshot','repository_snapshot')},
        bench_snapshot={k:getattr(bench,k) for k in ('id','name','host','port','user','remote_root')},
        device_snapshot=deepcopy(device),tool_snapshot=frozen,
        steps=[{'step_id':s['id'],'status':'pending'} for s in spec['steps']],events=[],
        context={'parameters':deepcopy(req['parameters']),'results':{},'definition_sha256':digest(spec),'release_binding':binding},
        remote_path=bench.remote_root.rstrip('/')+'/special-runs/'+rid,created_by=operator.strip())
    rnd.product_snapshot={**rnd.product_snapshot,'special_run_id':rid}
    event(run,'candidate_preparation_started','正在烧录固定候选镜像并验证启动；准备完成后进入人工门控',
        operator=operator.strip(),details={'preparation_round_id':rnd.id,'candidate_fingerprint':candidate.fingerprint})
    db.add(run)
    db.add(ValidationEvidence(plan_id=plan.id,requirement_id=req['id'],requirement_hash=spec_hash(req),
        payload={'kind':'special_execution','special_run_id':rid,'candidate_fingerprint':candidate.fingerprint}))
    return run

def preparation_proof(rnd,run,candidate):
    from .testing_platform import digest,image_identity
    binding=run.context.get('release_binding',{})
    report=rnd.controller_result or {};delivery=rnd.delivery_result or {};config=rnd.generated_config or {}
    if (rnd.product_snapshot.get('special_run_id')!=run.id or binding.get('preparation_round_id')!=rnd.id
        or rnd.candidate_id!=candidate.id or rnd.validation_plan_id!=binding.get('validation_plan_id')
        or rnd.product_id!=candidate.product_id or run.product_id!=candidate.product_id or rnd.bench_id!=run.bench_id
        or digest(image_identity(rnd.image_snapshot))!=candidate.fingerprint
        or rnd.product_snapshot.get('release_fingerprint')!=candidate.fingerprint):
        raise ValueError('候选镜像或专属准备轮次身份不匹配')
    if (rnd.status!=TestRoundStatus.TEST_COMPLETE or not delivery.get('passed')
        or report.get('state')!='finished' or report.get('boot_verified') is not True or report.get('flash_exit_code')!=0):
        raise ValueError('候选镜像烧录、启动或交付未通过')
    expected_config=hashlib.sha256(json.dumps(config,ensure_ascii=False,indent=2).encode()).hexdigest()
    delivered=next((f for f in delivery.get('files',[]) if f.get('filename')=='test-run.json'),{})
    if (report.get('round_id')!=f'TEST-{rnd.display_number:06d}' or not config
        or report.get('config_sha256')!=expected_config or delivered.get('sha256')!=expected_config):
        raise ValueError('Controller 结果与交付配置摘要不匹配')
    if image_identity(config.get('artifacts',[]))!=image_identity(rnd.image_snapshot):
        raise ValueError('交付配置镜像与候选包不匹配')
    for item in rnd.image_snapshot:
        if not any(f.get('filename')=='firmware/'+item['filename'] and all(f.get(k)==item[k] for k in ('sha256','size_bytes')) for f in delivery.get('files',[])):
            raise ValueError('候选镜像交付证明不完整')
    before=binding['original_device'];flash=config.get('flash',{});actual=report.get('device',{})
    if (not flash.get('enabled') or flash.get('target_chip')!='RV1126B'
        or flash.get('device_serial')!=before.get('serial') or flash.get('usb_path')!=before.get('usb_path')
        or any(actual.get(k)!=before.get(k) for k in ('serial','usb_path','usb_port'))
        or not report.get('usb_location')):
        raise ValueError('实际 CPU serial 或 USB 物理端口与锁定模组不匹配')
    slot=report.get('target_slot');version=report.get('boot_version','')
    original_slot=re.search(r'(?:^|\s)_?([abAB])\s*$',before.get('version',''))
    if not original_slot or report.get('source_slot')!=original_slot[1].lower() or slot==original_slot[1].lower():
        raise ValueError('候选未写入已锁定模组的另一槽位')
    from .firmware_identity import source_identity, matches_firmware_identity
    identity = source_identity({'identity': candidate.snapshot['identity']} if 'identity' in candidate.snapshot
        else {'commit': candidate.snapshot.get('commit', '')})
    if candidate.snapshot.get('source', {}).get('kind') == 'prebuilt':
        if config.get('source', {}).get('type') != 'prebuilt' or source_identity(config['source']) != identity:
            raise ValueError('预编译候选的交付身份规则不匹配')
    if slot not in ('a','b') or not matches_firmware_identity(identity, version) or not re.search(r'(?:^|\s)_?'+slot+r'\s*$',version,re.I):
        raise ValueError('候选启动版本或目标槽位未确认')
    partitions=report.get('flash_partitions',[])
    for part in ('boot','system','oem'):
        item=next((i for i in rnd.image_snapshot if i['filename']==part+'.img'),None)
        record=next((r for r in partitions if r.get('partition')==part+'_'+slot),{})
        if (not item or record.get('sha256')!=item['sha256'] or record.get('download_confirmed') is not True
            or record.get('exit_code') not in ((0,251) if part=='boot' else (0,))):
            raise ValueError('候选分区写入证明不完整')
    basic=next((c for c in rnd.test_case_snapshot if c.get('code')=='BASIC-VERSION'),None)
    result=next((c for c in report.get('cases',[]) if basic and c.get('id')==basic['id']),{})
    if (not basic or result.get('state')!='finished' or result.get('outcome')!='passed'
        or not result.get('assertions_total') or result.get('assertions_total')!=result.get('assertions_passed')
        or (result.get('recovery') or {}).get('state')=='failed'):
        raise ValueError('候选版本验证用例未通过')
    return dict(round_id=rnd.id,config_sha256=expected_config,candidate_fingerprint=candidate.fingerprint,
        serial=before['serial'],usb_path=before['usb_path'],usb_port=before['usb_port'],usb_location=report['usb_location'],
        version=version,video=report.get('video'),target_slot=slot,flash_partitions=deepcopy(partitions))

def refresh_preparation(db,run):
    from .bench_reservation import bench_guard
    with bench_guard(run.bench_id):
        db.refresh(run)
        return _refresh_preparation(db,run)

def _refresh_preparation(db,run):
    if run.status!='preparing_firmware':return
    from .testing_platform import ACTIVE
    from .special_scenarios import event
    binding=run.context.get('release_binding',{})
    rnd=db.get(TestRound,binding.get('preparation_round_id',''))
    if rnd:db.refresh(rnd)
    if rnd and rnd.status.value in (*ACTIVE,'created'):return
    candidate=db.get(ReleaseCandidate,binding.get('candidate_id',''))
    try:
        if not rnd or not candidate:raise ValueError('候选准备记录缺失')
        proof=preparation_proof(rnd,run,candidate)
    except (ValueError,KeyError,TypeError) as exc:
        run.status='interrupted';run.last_error='候选准备未通过，检查硬件后终止此轮：'+str(exc)
        event(run,'candidate_preparation_blocked',run.last_error)
    else:
        run.context={**run.context,'release_binding':{**binding,'preparation_proof':proof}}
        run.device_snapshot={**run.device_snapshot,'version':proof['version'],'video':proof['video'] or run.device_snapshot.get('video')}
        run.status='awaiting_operator'
        event(run,'candidate_preparation_verified','固定候选镜像及 CPU / USB / 启动版本已验证，等待人工门控',details=proof)
    run.revision+=1
    db.commit()

def special_evidence(db,plan,req,candidate,entry):
    from .testing_platform import spec_hash
    rid=entry.payload.get('special_run_id')
    if not rid:
        return ('unmeasured',entry.payload.get('reason','等待人工门控')) if entry.payload.get('kind')=='requirement_revision' else ('blocked','缺少本计划专属人工门控证据')
    run=db.get(SpecialRun,rid)
    if run is None:return 'blocked','人工门控记录不存在'
    refresh_preparation(db,run)
    binding=run.context.get('release_binding',{})
    if (binding.get('validation_plan_id')!=plan.id or binding.get('requirement_id')!=req['id']
        or binding.get('requirement_hash')!=spec_hash(req) or binding.get('case_id')!=req['case_id']):
        return 'stale','人工门控绑定的清单要求不一致'
    if (binding.get('candidate_id')!=candidate.id or binding.get('candidate_fingerprint')!=candidate.fingerprint
        or run.product_id!=candidate.product_id or binding.get('product_id')!=candidate.product_id):
        return 'blocked','人工门控候选包或产品不匹配'
    if run.scenario_code!=req['code'] or not matches(req['adapter'],run):return 'stale','场景定义或冻结工具与清单不一致'
    if run.status=='preparing_firmware':return 'preparing_firmware','正在烧录并验证固定候选镜像'
    rnd=db.get(TestRound,binding.get('preparation_round_id',''))
    try:
        if not rnd:raise ValueError('候选准备轮次缺失')
        proof=preparation_proof(rnd,run,candidate)
        if proof!=binding.get('preparation_proof'):raise ValueError('候选准备证明发生变化或未通过')
        if any(run.device_snapshot.get(k)!=proof[k] for k in ('serial','usb_path','usb_port','version')):
            raise ValueError('门控模组身份与候选准备不一致')
    except (ValueError,KeyError,TypeError) as exc:return 'blocked',str(exc)
    if any(run.context.get('parameters',{}).get(k)!=v for k,v in req['parameters'].items()):
        return 'stale','本轮门控参数与清单要求不匹配'
    if run.status=='passed':
        if run.bench_reserved or run.context.get('hardware_active') or run.current_step_index!=len(run.definition_snapshot['steps']):
            return 'blocked','门控尚未完成或硬件状态未确认'
        if len(run.steps)!=len(run.definition_snapshot['steps']):return 'blocked','步骤证据不完整'
        for spec,record in zip(run.definition_snapshot['steps'],run.steps):
            result=record.get('result',{});assertions=result.get('assertions',[])
            if (record.get('step_id')!=spec['id'] or record.get('status')!='passed' or result.get('outcome')!='passed'
                or not assertions or any(a.get('passed') is not True for a in assertions)
                or any(not item.get('archived') and not remote_evidence_complete(run,spec,item) for item in result.get('files',[]))):
                return 'blocked','门控步骤断言或已校验的证据记录不完整'
        return 'passed','人工门控步骤全部通过，候选及设备身份已绑定'
    return (run.status if run.status in ('awaiting_operator','running','failed','blocked','cancelled','interrupted','cancelling') else 'blocked',run.last_error or '等待人工操作')

def aggregate_ota(db,history,fallback):
    variants={name:'unmeasured' for name in VARIANTS}
    attempts=[entry for entry in history if entry.get('special_run_id')]
    for entry in attempts:
        run=db.get(SpecialRun,entry['special_run_id'])
        variant=run.context.get('parameters',{}).get('variant') if run else None
        if variant in variants:variants[variant]=entry['status']
    if not attempts:return fallback,variants
    latest=attempts[-1];run=db.get(SpecialRun,latest['special_run_id'])
    variant=run.context.get('parameters',{}).get('variant') if run else None
    if variant not in variants or latest['status'] in ('preparing_firmware','awaiting_operator','running','cancelling'):
        return latest['status'],variants
    for state in ('failed','blocked','error','interrupted','cancelled','stale'):
        if state in variants.values():return state,variants
    return ('passed' if all(value=='passed' for value in variants.values()) else 'unmeasured'),variants
