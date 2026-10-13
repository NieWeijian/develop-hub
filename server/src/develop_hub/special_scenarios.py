"""Durable operator gates. Hardware actions run once; uncertainty retains the bench."""
from __future__ import annotations
import asyncio
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
from urllib.parse import unquote
from pathlib import Path
import re
import shlex
import shutil
import tarfile

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .basic_cases import ROOT
from .bench_reservation import bench_guard
from .config import settings
from .db import SessionLocal, get_db
from .models import SpecialRun, TestBench, TestProduct, new_id, utcnow
from .special_definitions import definition, definitions
from .special_evidence_policy import POLICY as EVIDENCE_POLICY
from .test_workbench import run_command

router=APIRouter(prefix='/api/qa',tags=['special-scenarios'])
TERMINAL={'passed','failed','blocked','cancelled'}
PACKAGE=['special_runtime.py','rv1126b.py','special_evidence.py','tools.lock.json']

def sha(path):
    checksum=hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda:handle.read(1024*1024),b''):
            checksum.update(chunk)
    return checksum.hexdigest()

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def event(row,kind,message,**details):
    row.events=[*row.events,dict(sequence=len(row.events)+1,type=kind,message=message,
        created_at=utcnow().isoformat(),**details)]

def view(row):
    fields=('id','scenario_code','product_id','bench_id','status','revision','current_step_index',
            'bench_reserved','definition_snapshot','product_snapshot','bench_snapshot','device_snapshot',
            'tool_snapshot','steps','events','context','remote_path','last_error','created_by','created_at','completed_at')
    result={k:getattr(row,k) for k in fields}
    steps=row.definition_snapshot['steps']
    result['current_step_id']=steps[row.current_step_index]['id'] if row.current_step_index<len(steps) else None
    result['hardware_active']=bool(row.context.get('hardware_active'))
    binding=row.context.get('release_binding',{})
    result.update({key:binding.get(key) for key in ('validation_plan_id','requirement_id','candidate_id')})
    return result

def require(db,rid):
    row=db.get(SpecialRun,rid)
    if row is None:raise HTTPException(404,'特殊场景记录不存在')
    return row

class StrictModel(BaseModel):
    model_config=ConfigDict(extra='forbid')
class Create(StrictModel):
    scenario_code:str
    product_id:str
    bench_id:str
    device_serial:str=Field(min_length=1,max_length=128)
    operator:str=Field(min_length=1,max_length=120)
class Advance(StrictModel):
    expected_revision:int=Field(ge=1)
    step_id:str
    operator:str=Field(min_length=1,max_length=120)
    values:dict=Field(default_factory=dict)
class Cancel(StrictModel):
    expected_revision:int=Field(ge=1)
    operator:str=Field(min_length=1,max_length=120)
    reason:str=Field(min_length=1,max_length=2000)
    hardware_stopped:bool=False

def validate_values(row,step,values):
    fields={f['name']:f for f in step['fields']}
    if set(values)-set(fields):raise HTTPException(422,'未定义的步骤字段')
    output={}
    for key,f in fields.items():
        value=values.get(key,f.get('default'))
        if value is None or value=='':
            if f['required']:raise HTTPException(422,'请填写：'+f['label'])
            continue
        kind=f['type']
        if kind=='boolean':
            if not isinstance(value,bool) or f.get('must_be_true') and value is not True:raise HTTPException(422,'请确认：'+f['label'])
        elif kind=='number':
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
                raise HTTPException(422,'必须是有限数字：'+f['label'])
            if value<f.get('min',-math.inf) or value>f.get('max',math.inf):raise HTTPException(422,'数值超出范围：'+f['label'])
        else:
            if not isinstance(value,str) or not value.strip() or len(value)>500000:raise HTTPException(422,'文本为空或过长：'+f['label'])
            value=value.strip()
            if f.get('pattern') and not re.fullmatch(f['pattern'],value):raise HTTPException(422,'格式错误：'+f['label'])
            if kind=='select' and value not in [o['value'] for o in f['options']]:raise HTTPException(422,'选项无效：'+f['label'])
        output[key]=value
    merged={**row.context.get('parameters',{}),**output}
    if row.scenario_code=='FACTORY-AGING' and step['id']=='configure':
        if merged['capture_timeout_seconds']<merged['duration_seconds']+600:raise HTTPException(422,'采集等待应至少比要求老化周期长 600 秒')
        if any(int(merged[k])!=merged[k] for k in ('duration_seconds','capture_timeout_seconds','baud_rate')):raise HTTPException(422,'时长和波特率必须为整数')
        if merged['capture_mode']=='serial' and not re.fullmatch(r'/dev/(?:ttyUSB[0-9]+|ttyACM[0-9]+|serial/by-id/[A-Za-z0-9_.:+-]+)',merged.get('serial_device','')):
            raise HTTPException(422,'请选择检测到的 USB 串口路径')
    if row.scenario_code=='FACTORY-AGING' and step['id']=='aging_verify' and merged.get('capture_mode')=='manual':
        artifact_id=merged.get('manual_log_artifact_id')
        if not (merged.get('manual_log') or artifact_id) or not all(merged.get(k) for k in ('started_at','ended_at')):
            raise HTTPException(422,'人工采集需要原始日志（上传或粘贴）、开始与结束时间')
        if artifact_id and merged.get('manual_log'):raise HTTPException(422,'上传文件与粘贴日志请二选一')
        if artifact_id not in (None,'') and artifact_id not in row.context.get('manual_uploads',{}):
            raise HTTPException(422,'日志附件不属于本轮运行')
        validate_times(merged,('started_at','ended_at'))
    if row.scenario_code=='SPECIAL-OTA':
        if step['id']=='configure' and (not merged['package_path'].startswith('/') or '..' in Path(merged['package_path']).parts or any(c in merged['package_path'] for c in '\r\n\x00')):
            raise HTTPException(422,'源包必须为 3588 上的有效绝对路径')
        if step['id']=='ota_status' and merged.get('variant')=='interrupted':
            if not merged.get('progress_before_cut') or not merged.get('cut_at'):raise HTTPException(422,'中断 OTA 必须记录断电前升级进度和断电时间')
            validate_times(merged,('cut_at',))
    return output

def validate_times(values,keys):
    for key in keys:
        try:
            parsed=datetime.fromisoformat(values[key].replace('Z','+00:00'))
            if parsed.tzinfo is None:raise ValueError()
        except (ValueError,TypeError):raise HTTPException(422,'时间必须为含时区的 ISO 格式：'+key)

def freeze_package(rid):
    lock=json.loads((ROOT/'tools.lock.json').read_text())
    if sha(ROOT/lock['binary_path'])!=lock['binary_sha256'] or sha(ROOT/lock['manifest_path'])!=lock['manifest_sha256']:
        raise HTTPException(409,'工具包校验失败')
    files=[*PACKAGE,lock['binary_path'],lock['manifest_path']]
    # Optional analysis helper is frozen whenever shipped with the runtime.
    files=[name for name in files if name!='special_evidence.py' or (ROOT/name).exists()]
    local=settings.artifact_root/'special-runs'/rid
    package=local/'package'
    hashes={}
    for name in files:
        source=(ROOT/name).resolve()
        if not source.is_relative_to(ROOT.resolve()) or not source.is_file():raise HTTPException(409,'缺少场景运行文件：'+name)
        destination=package/name;destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(source,destination);hashes[name]=sha(destination)
    with tarfile.open(local/'package.tar','w') as archive:
        archive.add(package,arcname='package')
    return dict(tools_package=lock,package_sha256=hashes,archive_sha256=sha(local/'package.tar'),evidence_policy=deepcopy(EVIDENCE_POLICY))

def get_service(request:Request):
    return request.app.state.special_workflow

@router.get('/special-scenarios')
def list_definitions():
    return definitions()

@router.get('/special-runs')
def list_runs(product_id:str|None=None,db:Session=Depends(get_db)):
    query=select(SpecialRun).order_by(SpecialRun.created_at.desc()).limit(100)
    if product_id:query=query.where(SpecialRun.product_id==product_id)
    from .release_special import refresh_preparation
    rows=list(db.scalars(query))
    for row in rows:refresh_preparation(db,row)
    return [view(row) for row in rows]

@router.post('/special-runs',status_code=201)
async def create(payload:Create,db:Session=Depends(get_db),service=Depends(get_service)):
    from .test_workbench_api import bench_devices
    from .testing_platform import bench_busy
    spec=definition(payload.scenario_code)
    if spec is None:raise HTTPException(422,'特殊场景尚未实现')
    product=db.get(TestProduct,payload.product_id);bench=db.get(TestBench,payload.bench_id)
    if not product or not bench or not bench.enabled:raise HTTPException(404,'产品或测试台不存在/已停用')
    if 'rv1126b' not in product.board_config.lower():raise HTTPException(422,'当前特殊场景适配 RV1126B 产品')
    if not payload.operator.strip():raise HTTPException(422,'操作员不能为空')
    # Avoid even identity commands while another hardware workflow owns the bench.
    if bench_busy(db,bench.id):raise HTTPException(409,'测试台已有运行，包含等待人工操作的场景')
    devices=await bench_devices(bench.id,'RV1126B',db,service.workflow)
    matches=[d for d in devices if d.get('serial')==payload.device_serial]
    if len(matches)!=1:raise HTTPException(422,'请选择唯一在线的 CPU serial')
    device=matches[0]
    if not device.get('usb_port') or not device.get('version'):raise HTTPException(422,'设备身份缺少物理端口或版本')
    rid=new_id();frozen=freeze_package(rid)
    with bench_guard(bench.id):
        if bench_busy(db,bench.id):raise HTTPException(409,'测试台已被另一个运行占用')
        row=SpecialRun(id=rid,scenario_code=payload.scenario_code,product_id=product.id,bench_id=bench.id,
            status='awaiting_operator',revision=1,current_step_index=0,bench_reserved=True,
            definition_snapshot=spec,
            product_snapshot={k:getattr(product,k) for k in ('id','code','name','board_config','gerrit_project','manifest_snapshot','repository_snapshot')},
            bench_snapshot={k:getattr(bench,k) for k in ('id','name','host','port','user','remote_root')},
            device_snapshot=deepcopy(device),tool_snapshot=frozen,
            steps=[{'step_id':s['id'],'status':'pending'} for s in spec['steps']],
            events=[],context={'parameters':{},'results':{},'definition_sha256':digest(spec)},
            remote_path=bench.remote_root.rstrip('/')+'/special-runs/'+rid,created_by=payload.operator.strip())
        event(row,'created','已冻结场景、产品、模组身份与工具版本；等待操作员开始',operator=payload.operator.strip())
        db.add(row);db.commit()
    return view(row)

@router.get('/special-runs/{rid}')
def get_run(rid:str,db:Session=Depends(get_db)):
    from .release_special import refresh_preparation
    row=require(db,rid);refresh_preparation(db,row)
    return view(row)

@router.post('/special-runs/{rid}/advance')
async def advance(rid:str,payload:Advance,db:Session=Depends(get_db),service=Depends(get_service)):
    from .release_special import refresh_preparation
    request_hash=digest(payload.model_dump())
    row=require(db,rid);refresh_preparation(db,row)
    with bench_guard(row.bench_id):
        db.refresh(row)
        # Exact retries acknowledge the original action, never issue a second command.
        if any(s.get('request_revision')==payload.expected_revision and s.get('request_hash')==request_hash for s in row.steps):
            return view(row)
        if row.revision!=payload.expected_revision:raise HTTPException(409,'步骤状态已更新，请刷新后操作')
        if row.status!='awaiting_operator':raise HTTPException(409,'当前运行不能推进')
        spec=row.definition_snapshot['steps'][row.current_step_index]
        if spec['id']!=payload.step_id:raise HTTPException(409,'只能操作当前步骤')
        if not payload.operator.strip():raise HTTPException(422,'操作员不能为空')
        values=validate_values(row,spec,payload.values)
        binding=row.context.get('release_binding')
        if binding:
            from .models import ReleasePlan,ReleaseCandidate
            from .release_special import preparation_proof
            from .testing_platform import spec_hash,ensure_open
            plan=db.get(ReleasePlan,binding['validation_plan_id']);ensure_open(plan)
            req=next((r for r in plan.requirements if r['id']==binding['requirement_id']),None)
            if not req or spec_hash(req)!=binding['requirement_hash']:raise HTTPException(409,'门控清单已更新，请重新创建运行')
            if any(key in values and values[key]!=value for key,value in req['parameters'].items()):
                raise HTTPException(422,'步骤输入必须与发布清单参数一致')
        steps=deepcopy(row.steps)
        steps[row.current_step_index].update(status='running',operator=payload.operator.strip(),values=values,
            request_revision=payload.expected_revision,request_hash=request_hash,started_at=utcnow().isoformat())
        row.steps=steps;row.status='running';row.revision+=1
        row.context={**row.context,'parameters':{**row.context.get('parameters',{}),**values}}
        event(row,'step_started',spec['title'],step_id=spec['id'],operator=payload.operator.strip(),details={'values':values})
        db.commit()
    if spec['operation']:
        service.start(rid)
    else:
        service.finish(rid,dict(outcome='passed',message='操作员已确认本步骤',
            data={'values':values},assertions=[{'name':'operator_gate','passed':True}],commands=[],files=[]))
        db.refresh(row)
    return view(row)

@router.post('/special-runs/{rid}/manual-log',status_code=201)
async def upload_manual_log(rid:str,request:Request,expected_revision:int,operator:str,db:Session=Depends(get_db)):
    row=require(db,rid)
    def validate():
        if row.revision!=expected_revision:raise HTTPException(409,'运行状态已更新，请刷新')
        if (row.status!='awaiting_operator' or row.scenario_code!='FACTORY-AGING'
            or row.definition_snapshot['steps'][row.current_step_index]['id']!='aging_verify'
            or row.context.get('parameters',{}).get('capture_mode')!='manual'):
            raise HTTPException(409,'仅在人工老化证据步骤等待操作时上传')
        if not operator.strip() or len(operator)>120:raise HTTPException(422,'请填写操作员')
    validate()
    ident=new_id();directory=settings.artifact_root/'special-runs'/rid/'manual-uploads'
    directory.mkdir(parents=True,exist_ok=True)
    partial=directory/(ident+'.partial');destination=directory/(ident+'.log')
    total=0;checksum=hashlib.sha256()
    try:
        with partial.open('xb') as handle:
            async for chunk in request.stream():
                total+=len(chunk)
                if total>64*1024*1024:raise HTTPException(413,'人工日志文件不能超过 64 MiB')
                checksum.update(chunk);handle.write(chunk)
        if not total:raise HTTPException(422,'不能上传空日志')
        raw_name=unquote(request.headers.get('x-filename','aging-manual.log'))
        filename=Path(raw_name.replace('\\','/')).name[:160] or 'aging-manual.log'
        metadata=dict(artifact_id=ident,filename=filename,size_bytes=total,sha256=checksum.hexdigest(),
            created_at=utcnow().isoformat(),operator=operator.strip(),
            download_url='/api/qa/special-runs/'+rid+'/manual-log/'+ident)
        with bench_guard(row.bench_id):
            db.refresh(row);validate()
            partial.replace(destination)
            row.context={**row.context,'manual_uploads':{**row.context.get('manual_uploads',{}),ident:metadata}}
            event(row,'manual_log_uploaded','已上传本轮人工采集原始日志',operator=operator.strip(),details=metadata)
            db.commit()
        return metadata
    finally:
        if partial.exists():partial.unlink()

@router.get('/special-runs/{rid}/manual-log/{artifact_id}')
def download_manual_log(rid:str,artifact_id:str,db:Session=Depends(get_db)):
    row=require(db,rid);metadata=row.context.get('manual_uploads',{}).get(artifact_id)
    if metadata is None:raise HTTPException(404,'本轮日志附件不存在')
    path=settings.artifact_root/'special-runs'/rid/'manual-uploads'/(artifact_id+'.log')
    if not path.is_file() or sha(path)!=metadata['sha256']:raise HTTPException(409,'原始日志附件校验失败')
    return FileResponse(path,filename=metadata['filename'],media_type='application/octet-stream')

@router.post('/special-runs/{rid}/cancel')
async def cancel(rid:str,payload:Cancel,db:Session=Depends(get_db),service=Depends(get_service)):
    row=require(db,rid)
    with bench_guard(row.bench_id):
        db.refresh(row)
        if row.revision!=payload.expected_revision:raise HTTPException(409,'运行状态已更新，请刷新')
        binding=row.context.get('release_binding',{})
        if binding:
            from .models import TestRound,TestRoundStatus
            from .testing_platform import ACTIVE
            prep=db.get(TestRound,binding.get('preparation_round_id',''))
            if prep and prep.status==TestRoundStatus.CREATED:
                prep.status=TestRoundStatus.BLOCKED;prep.last_error='操作员终止尚未派发的候选准备';prep.completed_at=utcnow()
                event(row,'preparation_cancelled_before_dispatch',prep.last_error)
            if prep and prep.status.value in ACTIVE:
                raise HTTPException(409,'候选烧录准备尚未结束，不能释放测试台；请等待结果或先确认准备轮次停止')
            if row.status=='preparing_firmware':
                row.status='interrupted'
        if row.status=='cancelled':return view(row)
        if row.status in TERMINAL and not row.bench_reserved:raise HTTPException(409,'运行已结束')
        if not payload.operator.strip() or not payload.reason.strip():raise HTTPException(422,'必须记录操作员和终止原因')
        if row.status=='running':
            row.status='cancelling'
            message='已请求终止；等待当前命令返回，测试台仍被占用'
        elif row.status=='cancelling':
            message='再次请求停止当前命令，测试台仍被占用'
        else:
            if (row.status=='interrupted' or row.context.get('hardware_active')) and not payload.hardware_stopped:
                raise HTTPException(422,'请先人工停止远端采集/升级进程并确认硬件安全，再释放测试台')
            row.status='cancelled';row.bench_reserved=False;row.completed_at=utcnow()
            message='操作员终止运行并确认释放测试台'
        row.revision+=1
        event(row,'cancel_requested',message,operator=payload.operator.strip(),details={'reason':payload.reason,'hardware_stopped':payload.hardware_stopped})
        db.commit()
    if row.status=='cancelling':
        await service.request_cancel(rid)
        db.refresh(row)
    return view(row)

class CorrectLogWriteFailure(StrictModel):
    expected_revision:int=Field(ge=1)
    operator:str=Field(min_length=1,max_length=120)
    reason:str=Field(min_length=1,max_length=2000)

@router.post('/special-runs/{rid}/correct-log-write-failure')
def correct_log_write_failure(rid:str,payload:CorrectLogWriteFailure,db:Session=Depends(get_db)):
    """Narrow reclassification from immutable captured evidence; no hardware work."""
    row=require(db,rid)
    with bench_guard(row.bench_id):
        db.refresh(row)
        rule='log-write-failure-v1'
        if any(e.get('type')=='result_reclassified' and e.get('details',{}).get('rule')==rule for e in row.events):
            return view(row)
        if row.revision!=payload.expected_revision:raise HTTPException(409,'运行状态已更新，请刷新')
        if row.scenario_code!='SPECIAL-LOG-FULL' or row.status not in TERMINAL or row.bench_reserved or row.context.get('hardware_active'):
            raise HTTPException(409,'仅纠正已结束且已释放测试台的日志压力测试')
        if not payload.operator.strip() or not payload.reason.strip():raise HTTPException(422,'需要记录操作员与纠正原因')
        records={step['step_id']:step for step in row.steps}
        pressure=records.get('log_pressure',{}).get('result',{})
        # Re-read the saved runtime JSON; do not change it or its original outcome.
        source=settings.artifact_root/'special-runs'/rid/'steps'/'log_pressure'/'runtime-result.json'
        try:original=json.loads(source.read_text())
        except (OSError,ValueError):raise HTTPException(409,'缺少原始日志写入结果，不能自动纠正')
        data=original.get('data',{})
        if not data or data!=pressure.get('data'):raise HTTPException(409,'保存的原始写入数据与当前证据不一致')
        failures=[]
        for error in data.get('errors',[]):
            response=error.get('response',{});command=response.get('command',[])
            if (isinstance(command,list) and 'file-put' in command and response.get('exit_code')==1
                and 'file-put chunk failed: write failed' in str(response.get('stderr',''))
                and response in original.get('commands',[])):
                failures.append(error)
        if not failures:raise HTTPException(409,'没有可核验的模组 file-put 写入失败证据')
        source_sha=sha(source)
        before={'status':row.status,'last_error':row.last_error,'revision':row.revision,
            'steps':[deepcopy(x) for x in row.steps if x['step_id'] in ('log_pressure','log_verify')]}
        message='日志写入失败：模组返回 write failed，未达到目标写入量；日志区压力保护测试失败'
        steps=deepcopy(row.steps)
        for record in steps:
            if record['step_id'] not in ('log_pressure','log_verify') or not record.get('result'):continue
            result=record['result']
            result.update(outcome='failed',message=message)
            result['assertions']=[*result.get('assertions',[]),{'name':'Log writes accepted until target','passed':False,
                'details':'file-put exited 1: write failed'}]
            record['status']='failed'
        row.steps=steps;row.status='failed';row.last_error=message;row.revision+=1
        # Preserve completion timestamps, frozen definitions, parameters and every raw file.
        event(row,'result_reclassified',message,operator=payload.operator.strip(),details={
            'rule':rule,'reason':payload.reason.strip(),'source_path':str(source),'source_sha256':source_sha,
            'write_failures':failures,'before':before,
            'after':{'status':row.status,'revision':row.revision,'step_outcomes':{x['step_id']:x['status'] for x in row.steps}}})
        db.commit()
        return view(row)

@router.get('/special-runs/{rid}/report')
def report(rid:str,db:Session=Depends(get_db)):
    return JSONResponse(jsonable_encoder({'schema_version':1,'report_kind':'operator_gated_scenario',**view(require(db,rid))}),
        headers={'Content-Disposition':f'attachment; filename="special-{rid}.json"'})

@router.get('/special-runs/{rid}/artifacts/{artifact_id}')
def artifact(rid:str,artifact_id:str,db:Session=Depends(get_db)):
    row=require(db,rid)
    for step_record in row.steps:
        for item in (step_record.get('result') or {}).get('files',[]):
            if item.get('artifact_id')!=artifact_id:continue
            root=(settings.artifact_root/'special-runs'/rid).resolve()
            path=(root/'steps'/step_record['step_id']/'files'/item['path']).resolve()
            if not path.is_relative_to(root) or not path.is_file() or sha(path)!=item['sha256']:
                raise HTTPException(409,'归档证据文件缺失或校验失败')
            return FileResponse(path,filename=path.name,media_type='application/octet-stream')
    raise HTTPException(404,'证据文件不存在')


class SpecialWorkflow:
    def __init__(self,workflow):
        self.workflow=workflow
        self.tasks={}
    async def recover(self):
        with SessionLocal() as db:
            from .models import TestRound,TestRoundStatus
            from .release_special import refresh_preparation
            for pending in db.scalars(select(SpecialRun).where(SpecialRun.status=='preparing_firmware')):
                with bench_guard(pending.bench_id):
                    prep=db.get(TestRound,pending.context.get('release_binding',{}).get('preparation_round_id',''))
                    if prep and prep.status==TestRoundStatus.CREATED:
                        prep.status=TestRoundStatus.BLOCKED;prep.last_error='服务重启前候选准备尚未派发；不会自动重试';prep.completed_at=utcnow()
                        db.commit()
                refresh_preparation(db,pending)
            for row in db.scalars(select(SpecialRun).where(SpecialRun.status.in_(('running','cancelling')))):
                row.status='interrupted';row.revision+=1
                steps=deepcopy(row.steps)
                steps[row.current_step_index].update(status='interrupted',interrupted_at=utcnow().isoformat())
                row.steps=steps
                row.last_error='服务重启或进程中断；硬件动作不会自动重试。请检查远端进程和模组后终止此轮。'
                event(row,'interrupted',row.last_error)
            db.commit()
    async def stop(self):
        for task in list(self.tasks.values()):task.cancel()
        if self.tasks:await asyncio.gather(*list(self.tasks.values()),return_exceptions=True)
    def start(self,rid):
        if rid in self.tasks and not self.tasks[rid].done():return
        task=asyncio.create_task(self.execute(rid),name='special-'+rid);self.tasks[rid]=task
        task.add_done_callback(lambda done:self.tasks.pop(rid,None))
    def finish(self,rid,result):
        with SessionLocal() as db:
            row=require(db,rid)
            if row.status not in ('running','cancelling'):return
            result=deepcopy(result)
            assertions=result.get('assertions')
            if result.get('outcome')=='passed' and (not isinstance(assertions,list) or not assertions or
                any(not isinstance(a,dict) or a.get('passed') is not True for a in assertions)):
                result.update(outcome='blocked',message='运行器未提供完整通过断言，不能判定通过')
            if result.get('outcome') not in ('passed','failed','blocked'):
                result.update(outcome='blocked',message='运行器返回未知结果，不能判定通过')
            index=row.current_step_index;spec=row.definition_snapshot['steps'][index]
            steps=deepcopy(row.steps);steps[index].update(status=result['outcome'],result=result,finished_at=utcnow().isoformat())
            row.steps=steps
            data=result.get('data',{})
            context=deepcopy(row.context)
            context.setdefault('results',{})[spec['id']]=data
            if spec['operation']:context[spec['operation']]=data
            if isinstance(data,dict) and 'hardware_active' in data:context['hardware_active']=bool(data['hardware_active'])
            if result['outcome']=='passed' and (spec['id']=='ota_prepare' or spec['id']=='aging_capture' and context.get('parameters',{}).get('capture_mode')=='manual'):
                context['hardware_active']=True
            row.context=context
            event(row,'step_finished',result.get('message',''),step_id=spec['id'],details={'outcome':result['outcome']})
            row.revision+=1
            if row.status=='cancelling':
                if context.get('hardware_active'):
                    row.status='interrupted';row.last_error='当前操作已返回，但硬件任务仍在运行；请人工停止后释放测试台'
                else:
                    row.status='cancelled';row.bench_reserved=False;row.completed_at=utcnow()
            elif result['outcome']!='passed':
                row.status=result['outcome'];row.last_error=result.get('message')
                row.bench_reserved=bool(context.get('hardware_active'));row.completed_at=utcnow()
            elif index+1==len(row.definition_snapshot['steps']):
                if context.get('hardware_active'):
                    row.status='blocked';row.last_error='硬件任务仍在运行，不能结束并释放测试台'
                else:
                    row.status='passed';row.current_step_index+=1;row.bench_reserved=False
                row.completed_at=utcnow()
            else:
                row.current_step_index+=1;row.status='awaiting_operator'
            db.commit()
    def interrupt(self,rid,message):
        with SessionLocal() as db:
            row=db.get(SpecialRun,rid)
            if row and row.status in ('running','cancelling'):
                row.status='interrupted';row.last_error=message;row.revision+=1
                steps=deepcopy(row.steps)
                steps[row.current_step_index].update(status='interrupted',interrupted_at=utcnow().isoformat())
                row.steps=steps
                event(row,'interrupted',message);db.commit()
    async def execute(self,rid):
        try:
            with SessionLocal() as db:
                row=require(db,rid)
                bench=TestBench(**row.bench_snapshot)
                index=row.current_step_index;spec=row.definition_snapshot['steps'][index]
                snapshot=deepcopy(view(row))
            result=await self.run_remote(bench,snapshot,spec)
            self.finish(rid,result)
        except asyncio.CancelledError:
            self.interrupt(rid,'控制进程停止；硬件状态未确认，不自动重试并保留测试台占用')
            raise
        except Exception as exc:
            self.interrupt(rid,'远端执行状态未确认：'+str(exc)[:2000])
    async def request_cancel(self,rid):
        with SessionLocal() as db:
            row=require(db,rid)
            if row.status!='cancelling':return
            bench=TestBench(**row.bench_snapshot)
            step=row.definition_snapshot['steps'][row.current_step_index]
            directory=row.remote_path+'/steps/'+str(row.current_step_index).zfill(2)+'-'+step['id']
        # The marker path is generated entirely from a frozen run and step.
        code='import pathlib;p=pathlib.Path('+repr(directory)+');p.mkdir(parents=True,exist_ok=True);(p/"CANCEL_REQUESTED").write_text("operator cancellation")'
        try:
            response=await self.workflow._remote(bench,'python3 -c '+shlex.quote(code),20)
            if response['exit_code']:raise RuntimeError('远端拒绝停止标记')
        except Exception as exc:
            self.interrupt(rid,'无法确认停止请求已送达：'+str(exc)+'；请人工停止远端任务后再释放测试台')
            task=self.tasks.get(rid)
            if task and not task.done():task.cancel()

    async def run_remote(self,bench,snapshot,spec):
        remote=snapshot['remote_path'];local=settings.artifact_root/'special-runs'/snapshot['id']
        archive=local/'package.tar'
        if not archive.is_file() or sha(archive)!=snapshot['tool_snapshot']['archive_sha256']:
            return dict(outcome='blocked',message='冻结的本地运行包不存在或已变更',assertions=[],data={})
        prepared=await self.workflow._remote(bench,'mkdir -p -- '+shlex.quote(remote),30)
        if prepared['exit_code']:raise RuntimeError('无法建立远端场景目录')
        options=self.workflow._ssh_base(bench);options[options.index('-p')]='-P'
        # The archive contains only files frozen by this server, never operator filenames.
        check=await self.workflow._remote(bench,'test -f '+shlex.quote(remote+'/package.ready'),20)
        if check['exit_code']:
            upload=await run_command(['scp',*options,'--',str(archive),f'{bench.user}@{bench.host}:{remote}/package.tar'],Path.cwd(),300)
            if upload['exit_code']:raise RuntimeError('场景运行包上传失败')
            unpack='import hashlib,pathlib,tarfile;p=pathlib.Path('+repr(remote)+');a=p/"package.tar";assert hashlib.sha256(a.read_bytes()).hexdigest()=='+repr(snapshot['tool_snapshot']['archive_sha256'])+';tarfile.open(a).extractall(p);(p/"package.ready").write_text("ready")'
            response=await self.workflow._remote(bench,'python3 -c '+shlex.quote(unpack),60)
            if response['exit_code']:raise RuntimeError('运行包解包校验失败')
        stepdir=remote+'/steps/'+str(snapshot['current_step_index']).zfill(2)+'-'+spec['id']
        lock=snapshot['tool_snapshot']['tools_package']
        parameters=deepcopy(snapshot['context']['parameters'])
        manual_id=parameters.get('manual_log_artifact_id')
        manual_source=None
        if manual_id and spec['operation']=='aging_verify':
            metadata=snapshot['context'].get('manual_uploads',{}).get(manual_id)
            if not metadata:raise RuntimeError('人工日志附件未绑定本轮')
            manual_source=local/'manual-uploads'/(manual_id+'.log')
            if not manual_source.is_file() or sha(manual_source)!=metadata['sha256']:
                raise RuntimeError('人工日志附件在执行前校验失败')
            parameters.update(manual_log_path=stepdir+'/input.log',manual_log_sha256=metadata['sha256'])
        request=dict(operation=spec['operation'],device=snapshot['device_snapshot'],parameters=parameters,
            context=snapshot['context'],tool=remote+'/package/'+lock['binary_path'],tool_sha256=lock['binary_sha256'],run_id=snapshot['id'])
        data=json.dumps(request,ensure_ascii=False).encode()
        local_step=local/'steps'/spec['id'];local_step.mkdir(parents=True,exist_ok=True)
        (local_step/'request.json').write_bytes(data)
        hashes=snapshot['tool_snapshot']['package_sha256']
        script='import pathlib,hashlib,json;p=pathlib.Path('+repr(remote+'/package')+');h=json.loads('+repr(json.dumps(hashes))+');assert all(hashlib.sha256((p/n).read_bytes()).hexdigest()==v for n,v in h.items());s=pathlib.Path('+repr(stepdir)+');s.mkdir(parents=True,exist_ok=True);assert all(f.name=="CANCEL_REQUESTED" for f in s.iterdir())'
        response=await self.workflow._remote(bench,'python3 -c '+shlex.quote(script),30)
        if response['exit_code']:raise RuntimeError('步骤目录已存在或冻结运行包校验失败；拒绝重试硬件动作')
        delivered=await run_command(['scp',*options,'--',str(local_step/'request.json'),f'{bench.user}@{bench.host}:{stepdir}/request.json'],Path.cwd(),300)
        if delivered['exit_code']:raise RuntimeError('步骤请求上传失败；未启动硬件动作')
        if manual_source:
            delivered=await run_command(['scp',*options,'--',str(manual_source),f'{bench.user}@{bench.host}:{stepdir}/input.log'],Path.cwd(),300)
            if delivered['exit_code']:raise RuntimeError('人工原始日志交付失败；未启动判定')
        with SessionLocal() as db:
            current=db.get(SpecialRun,snapshot['id'])
            if current and current.status=='cancelling':
                return dict(outcome='blocked',message='已在硬件动作开始前停止',assertions=[],commands=[],files=[],
                    data={'hardware_active':bool(snapshot['context'].get('hardware_active'))})
        timeout=int(request['parameters'].get('capture_timeout_seconds',15000))+120 if spec['operation']=='aging_capture' else 3600
        command='python3 '+shlex.quote(remote+'/package/special_runtime.py')+' --request '+shlex.quote(stepdir+'/request.json')+' --output '+shlex.quote(stepdir+'/result.json')
        invocation=await self.invoke_with_progress(bench,snapshot,spec,command,timeout,stepdir)
        local_step=local/'steps'/spec['id'];local_step.mkdir(parents=True,exist_ok=True)
        (local_step/'request.json').write_bytes(data)
        # Fetch the complete structured result, independent of SSH stdout-tail limits.
        # Raw-file retention follows the policy frozen at run creation.
        metadata='import pathlib,json;p=pathlib.Path('+repr(stepdir+'/result.json')+');print(json.dumps(dict(size=p.stat().st_size)))'
        response=await self.workflow._remote(bench,'python3 -c '+shlex.quote(metadata),30,capture_stdout=True)
        if response['exit_code']:raise RuntimeError('运行器未生成结果；远端硬件状态未知')
        if json.loads(response.get('stdout',response.get('stdout_tail','')))['size']>64*1024*1024:
            raise RuntimeError('步骤结果超过归档上限，远端原文件保留')
        target=local_step/'runtime-result.json'
        copied=await run_command(['scp',*options,'--',f'{bench.user}@{bench.host}:{stepdir}/result.json',str(target)],Path.cwd(),300)
        if copied['exit_code']:raise RuntimeError('完整步骤结果归档失败，远端原文件保留')
        result=json.loads(target.read_text())
        if not isinstance(result,dict):raise RuntimeError('运行器结果格式错误')
        result.setdefault('commands',[]).append({'operation':'runtime','command':command,**invocation})
        if invocation['exit_code'] and result.get('outcome')=='passed':
            result.update(outcome='blocked',message='运行器退出码非零，不接受通过结果')
        try:
            await self.archive_files(bench,snapshot,spec,result,stepdir,local_step,options)
        except Exception as exc:
            if result.get('outcome')!='failed':
                result.update(outcome='blocked',message='硬件步骤已返回，但原始证据校验或归档未完成：'+str(exc))
            else:
                result['message']=result.get('message','测试失败')+'；原始证据校验或归档未完成：'+str(exc)
            result.setdefault('data',{})['archive_error']=str(exc)
        (local_step/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        return result

    async def invoke_with_progress(self,bench,snapshot,spec,command,timeout,stepdir):
        task=asyncio.create_task(self.workflow._remote(bench,command,timeout))
        try:
            if spec['operation']!='aging_capture':
                return await task
            while not task.done():
                await asyncio.wait({task},timeout=2)
                if task.done():break
                code='import pathlib,json;p=pathlib.Path('+repr(stepdir+'/result.json')+');r=json.loads(p.read_text()) if p.exists() else {};d=r.get("data",{});print(json.dumps({k:d[k] for k in ("capture_ready","capture_started_at","capture_ended_at","hardware_active","serial_device","mode") if k in d}))'
                try:
                    response=await self.workflow._remote(bench,'python3 -c '+shlex.quote(code),15,capture_stdout=True)
                    if response['exit_code']:continue
                    progress=json.loads(response.get('stdout',response.get('stdout_tail','')))
                except Exception:
                    # Losing a read-only progress poll must not abort a long capture.
                    continue
                if not progress:continue
                with SessionLocal() as db:
                    row=db.get(SpecialRun,snapshot['id'])
                    if row is None or row.status not in ('running','cancelling'):continue
                    steps=deepcopy(row.steps)
                    steps[row.current_step_index]['progress']=progress
                    row.steps=steps
                    if progress.get('hardware_active'):
                        row.context={**row.context,'hardware_active':True}
                    db.commit()
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)

    async def archive_files(self,bench,snapshot,spec,result,stepdir,local_step,options):
        for item in result.get('files',[]):
            name=item.get('path','')
            path=Path(name)
            if not name or path.is_absolute() or '..' in path.parts or '\\' in name:
                raise RuntimeError('运行器证据路径越界')
            if not re.fullmatch(r'[0-9a-f]{64}',str(item.get('sha256',''))):
                raise RuntimeError('运行器证据缺少有效摘要')
            if type(item.get('size_bytes')) is not int or not 0<=item['size_bytes']<=1024*1024*1024:
                raise RuntimeError('运行器证据大小超出归档范围')
            # Resolve symlinks remotely before reading; evidence must belong to this step.
            code='import pathlib,hashlib;p=pathlib.Path('+repr(stepdir)+');f=(p/'+repr(name)+').resolve();assert p.resolve() in f.parents and f.is_file();assert f.stat().st_size=='+str(item['size_bytes'])+';h=hashlib.sha256();s=f.open("rb");[h.update(b) for b in iter(lambda:s.read(1048576),b"")];assert h.hexdigest()=='+repr(item['sha256'])
            response=await self.workflow._remote(bench,'python3 -c '+shlex.quote(code),120)
            if response['exit_code']:raise RuntimeError('远端原始证据校验失败：'+name)
            if snapshot['tool_snapshot'].get('evidence_policy')==EVIDENCE_POLICY:
                item.update(archived=False,remote_only=True,remote_path=stepdir+'/'+name,
                    remote_verified_at=utcnow().isoformat(),remote_verified_sha256=item['sha256'],
                    remote_verified_size_bytes=item['size_bytes'])
                item.pop('download_url',None);item.pop('artifact_id',None)
                continue
            destination=local_step/'files'/path
            destination.parent.mkdir(parents=True,exist_ok=True)
            response=await run_command(['scp',*options,'--',f'{bench.user}@{bench.host}:{stepdir}/{name}',str(destination)],Path.cwd(),1800)
            if response['exit_code'] or not destination.is_file() or sha(destination)!=item['sha256']:
                raise RuntimeError('原始证据归档失败，远端文件保留：'+name)
            item['artifact_id']=digest({'step':spec['id'],'path':name})
            item['download_url']='/api/qa/special-runs/'+snapshot['id']+'/artifacts/'+item['artifact_id']
            item['archived']=True
