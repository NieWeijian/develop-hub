'use client';

import { FormEvent, ReactNode, useCallback, useEffect, useRef, useState } from 'react';
import './platform-workbench.css';
import { caseGroup } from './workbench-controls';
import type { Definition as SpecialDefinition } from './special-scenarios';
import PrebuiltCandidateForm, { type PrebuiltImport } from './prebuilt-candidate';

type Product = {id:string;name:string};
type Bench = {id:string;name:string;enabled:boolean};
type Case = {id:string;code:string;name:string;description?:string};
type Round = {id:string;display_number:number;product_id:string;origin?:string;status:string;commit_sha:string;build_result:Record<string,unknown>|null;image_snapshot:Record<string,unknown>[];product_snapshot:Record<string,unknown>};
type Candidate = {id:string;name:string;product_id:string;subject:string;commit:string;source?:{kind:string;image_directory?:string;operator?:string;imported_at?:string};identity?:{kind:string;value:string};images:{filename:string;sha256:string;size_bytes:number}[]};
function candidateDescription(candidate:Candidate) {return candidate.source?.kind==='prebuilt'?'已有镜像 · '+(candidate.identity?.value||candidate.name):candidate.subject||candidate.commit?.slice(0,12)||candidate.name;}
type Entry = {special_run_id?:string|null;id:string;status:string;message:string;round_id:string|null;round_number:number|null;created_at:string};
type Requirement = {variant_statuses?:Record<string,string>;adapter?:SpecialDefinition;id:string;code:string;name:string;group:string;mode:string;parameters:Record<string,unknown>;status:string;history:Entry[];latest:Entry|null};
type Plan = {id:string;name:string;candidate_id:string;candidate_name:string;revision:number;requirements:Requirement[];counts:Record<string,number>;total:number;ready:boolean;final_report:Record<string,unknown>|null};
type Schedule = {id:string;name:string;enabled:boolean;next_fire_at:string;config:{product_id:string;bench_id?:string;device_serial?:string;source_kind:string;source:string;daily_time:string;timezone:string;test_case_ids?:string[];test_case_parameters?:Record<string,Record<string,unknown>>;skip_unchanged?:boolean};runs:{id:string;state:string;message:string;scheduled_at:string;round_id:string|null;round_number:number|null;commit:string}[]};
type Device = {serial:string;version:string};
type Api = <T>(path:string,init?:RequestInit)=>Promise<T>;
type Props = {initialPrebuiltProductId?:string;initialPlanId?:string;onSelectPlan:(id:string)=>void;openSpecial:(id?:string,code?:string,planId?:string)=>void;specialDefinitions:SpecialDefinition[];mode:'releases'|'schedules';products:Product[];benches:Bench[];cases:Case[];rounds:Round[];executable:string[];api:Api;apiBase:string;openRound:(id:string)=>void;renderParameters:(code:string,value:string,onChange:(value:string)=>void)=>ReactNode};
type CoverageFilter = 'all'|'todo'|'attention'|'active'|'passed';
const labels:Record<string,string>={passed:'通过',failed:'失败',unmeasured:'未测',pending_definition:'待定义',blocked:'阻塞',error:'异常',stale:'待更新',preparing_firmware:'准备候选固件',awaiting_operator:'等待操作',cancelled:'已取消',interrupted:'执行中断',cancelling:'正在停止',queued:'等待执行',running:'运行中',completed:'已完成，待确认',skipped:'已跳过',started:'已启动',created:'待启动',checking_out:'拉取代码',building:'编译中',packaging:'校验镜像',transferring:'传输中',verifying:'校验中',bench_running:'测试中',test_complete:'测试通过',test_failed:'测试未通过',build_failed:'构建失败',bench_failed:'测试台失败',delivered:'已交付'};
const activeStatuses = ['queued','running','completed','preparing_firmware','awaiting_operator','cancelling'];
const sourceActiveStatuses = ['created','checking_out','building','packaging','transferring','verifying','bench_running'];
const date=(value:string)=>new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'});
function requirementGroup(requirement:Requirement) {
  return requirement.mode==='automatic'?caseGroup(requirement.code):requirement.group;
}
function matchesFilter(requirement:Requirement,filter:CoverageFilter) {
  if(filter==='todo') return ['unmeasured','stale','pending_definition'].includes(requirement.status);
  if(filter==='attention') return ['failed','error','blocked','cancelled','interrupted'].includes(requirement.status);
  if(filter==='active') return activeStatuses.includes(requirement.status);
  if(filter==='passed') return requirement.status==='passed';
  return true;
}
function message(cause:unknown) {return cause instanceof Error?cause.message:String(cause);}
function focusLaunchHeading(heading:HTMLHeadingElement|null) {
  if(!heading)return;
  heading.focus({preventScroll:true});
  heading.scrollIntoView({behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'start'});
}
function CaseSelection({cases,selected,onChange,parameters,onParameters,renderParameters,disabled}:{cases:Case[];selected:string[];onChange:(ids:string[])=>void;parameters:Record<string,string>;onParameters:(id:string,value:string)=>void;renderParameters:Props['renderParameters'];disabled:boolean}) {
  const groups=Array.from(new Set(cases.map(c=>caseGroup(c.code))));
  return <div className="platform-case-picker">
    <div className="platform-toolbar"><strong>已选 {selected.length} 项</strong><button type="button" disabled={disabled} onClick={()=>onChange(cases.map(c=>c.id))}>全选可执行用例</button><button type="button" disabled={disabled||!selected.length} onClick={()=>onChange([])}>清空</button></div>
    {groups.map(group=>{
      const items=cases.filter(c=>caseGroup(c.code)===group);
      const allSelected=items.every(c=>selected.includes(c.id));
      return <details className="platform-case-group" key={group} open><summary>{group}<span>{items.filter(c=>selected.includes(c.id)).length} / {items.length} 已选</span></summary>
        <button className="platform-group-toggle" type="button" disabled={disabled} onClick={()=>onChange(allSelected?selected.filter(id=>!items.some(c=>c.id===id)):Array.from(new Set([...selected,...items.map(c=>c.id)])))}>{allSelected?'取消本组':'选择本组'}</button>
        {items.map(c=><div className="platform-case-option" key={c.id}><label><input type="checkbox" disabled={disabled} checked={selected.includes(c.id)} onChange={e=>onChange(e.target.checked?[...selected,c.id]:selected.filter(id=>id!==c.id))}/><span><strong>{c.name}</strong><small>{c.description||c.code}</small></span></label>
          {selected.includes(c.id)&&<details className="platform-case-parameters"><summary>参数设置{c.code==='STREAM-RGB-DEPTH'?' · 默认持续 5 小时':''}</summary>{renderParameters(c.code,parameters[c.id]||'{}',value=>onParameters(c.id,value))}</details>}
        </div>)}
      </details>;
    })}
    {!cases.length&&<p className="platform-empty">暂无可执行用例，请检查用例配置。</p>}
  </div>;
}

export default function PlatformWorkbench({initialPrebuiltProductId,initialPlanId,onSelectPlan,openSpecial,specialDefinitions,mode,products,benches,cases,rounds,executable,api,apiBase,openRound,renderParameters}:Props) {
  const [candidates,setCandidates]=useState<Candidate[]>([]);
  const [plans,setPlans]=useState<Plan[]>([]);
  const [schedules,setSchedules]=useState<Schedule[]>([]);
  const [loaded,setLoaded]=useState(false);
  const [planId,setPlanId]=useState(initialPlanId||'');
  const [gatedRequirementId,setGatedRequirementId]=useState('');
  const [selected,setSelected]=useState<string[]>([]);
  const [filter,setFilter]=useState<CoverageFilter>('all');
  const [benchChoice,setBenchChoice]=useState('');
  const [deviceChoice,setDeviceChoice]=useState({benchId:'',serial:''});
  const [discovery,setDiscovery]=useState<{key:string;rows:Device[];error:string;pending:boolean}>({key:'',rows:[],error:'',pending:false});
  const [deviceRefresh,setDeviceRefresh]=useState(0);
  const [productChoice,setProductChoice]=useState('');
  const [sourceKind,setSourceKind]=useState('branch');
  const [caseIds,setCaseIds]=useState<string[]>([]);
  const [parameters,setParameters]=useState<Record<string,string>>({});
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [loadError,setLoadError]=useState('');
  const [notice,setNotice]=useState('');
  const [editing,setEditing]=useState<string|null>(null);
  const [editValue,setEditValue]=useState('{}');
  const [editRevision,setEditRevision]=useState(0);
  const [showCreate,setShowCreate]=useState(!!initialPrebuiltProductId);
  const [candidateSource,setCandidateSource]=useState<'existing'|'new'|'prebuilt'>(initialPrebuiltProductId?'prebuilt':'existing');
  const [candidateChoice,setCandidateChoice]=useState('');
  const [sourceRoundChoice,setSourceRoundChoice]=useState('');
  const refreshSequence=useRef(0);
  const actionInFlight=useRef(false);
  const gatedHeadingRef=useRef<HTMLHeadingElement>(null);
  const mounted=useRef(true);
  const plan=plans.find(p=>p.id===planId);
  const enabledBenches=benches.filter(b=>b.enabled);
  const benchId=enabledBenches.some(b=>b.id===benchChoice)?benchChoice:enabledBenches.length===1?enabledBenches[0].id:'';
  const productId=products.some(p=>p.id===productChoice)?productChoice:products.length===1?products[0].id:'';
  const selectedIds=plan?.requirements.filter(r=>r.mode==='automatic'&&selected.includes(r.id)).map(r=>r.id)||[];
  const gatedRequirement=plan?.requirements.find(r=>r.id===gatedRequirementId&&r.mode==='gated');
  const supportedRequirements=plan?.requirements.filter(r=>executable.includes(r.code)||specialDefinitions.some(d=>d.code===r.code))||[];
  const pendingDefinitions=supportedRequirements.filter(r=>r.mode==='pending');
  const deviceNeeded=(mode==='schedules'&&showCreate)||(mode==='releases'&&!!plan&&!plan.final_report&&!showCreate&&(selectedIds.length>0||!!gatedRequirement));
  const discoveryKey=deviceNeeded&&benchId?mode+':'+planId+':'+benchId+':'+deviceRefresh:'';
  // Clear previous devices before committing a newly opened execution context.
  if(discovery.key!==discoveryKey)setDiscovery({key:discoveryKey,rows:[],error:'',pending:!!discoveryKey});
  const devices=discovery.key===discoveryKey&&!discovery.pending?discovery.rows:[];
  const detecting=deviceNeeded&&!!benchId&&(discovery.key!==discoveryKey||discovery.pending);
  const device=devices.some(d=>d.serial===deviceChoice.serial)&&deviceChoice.benchId===benchId?deviceChoice.serial:devices.length===1?devices[0].serial:'';
  const candidateAwaitingRefresh=!!candidateChoice&&!candidates.some(c=>c.id===candidateChoice);
  const candidateId=candidateChoice?(candidateAwaitingRefresh?'':candidateChoice):candidates.length===1?candidates[0].id:'';
  const candidate=candidates.find(c=>c.id===candidateId);
  const creatingCandidate=candidateSource==='new';
  const sourceRounds=rounds.filter(r=>r.build_result?.passed&&r.image_snapshot.length&&r.commit_sha&&!sourceActiveStatuses.includes(r.status));
  const sourceRoundId=sourceRounds.some(r=>r.id===sourceRoundChoice)?sourceRoundChoice:sourceRounds.length===1?sourceRounds[0].id:'';
  const executableCases=cases.filter(c=>executable.includes(c.code));
  const validCaseIds=caseIds.filter(id=>executableCases.some(c=>c.id===id));

  const refresh=useCallback(async()=>{
    const sequence=++refreshSequence.current;
    try {
      if(mode==='releases') {
        const [nextCandidates,nextPlans]=await Promise.all([api<Candidate[]>('/api/qa/platform/candidates'),api<Plan[]>('/api/qa/platform/plans')]);
        if(sequence!==refreshSequence.current||!mounted.current)return;
        setCandidates(nextCandidates);setPlans(nextPlans);
      } else {
        const nextSchedules=await api<Schedule[]>('/api/qa/platform/schedules');
        if(sequence!==refreshSequence.current||!mounted.current)return;
        setSchedules(nextSchedules);
      }
      setLoaded(true);setLoadError('');
    } catch(cause) {if(sequence===refreshSequence.current&&mounted.current)throw cause;}
  },[api,mode]);
  useEffect(()=>{
    mounted.current=true;
    let stopped=false;
    let timer:ReturnType<typeof setTimeout>;
    const poll=async()=>{
      try {await refresh();}catch(cause){if(!stopped)setLoadError(message(cause));}
      if(!stopped)timer=setTimeout(poll,10000);
    };
    void poll();
    return()=>{stopped=true;mounted.current=false;clearTimeout(timer);};
  },[refresh]);
  useEffect(()=>{
    if(gatedRequirementId)focusLaunchHeading(gatedHeadingRef.current);
  },[gatedRequirementId]);
  useEffect(()=>{
    if(!deviceNeeded||!benchId)return;
    let active=true;
    const controller=new AbortController();
    void api<Device[]>('/api/qa/benches/'+benchId+'/devices?chip=RV1126B',{signal:controller.signal})
      .then(rows=>{if(active)setDiscovery({key:discoveryKey,rows,error:'',pending:false});})
      .catch(cause=>{if(active)setDiscovery({key:discoveryKey,rows:[],error:message(cause),pending:false});});
    return()=>{active=false;controller.abort();};
  },[api,benchId,deviceNeeded,discoveryKey]);

  async function perform(action:()=>Promise<void>) {
    if(actionInFlight.current)return;
    actionInFlight.current=true;setBusy(true);setError('');setNotice('');
    try {
      await action();
      if(mounted.current) {
        try {await refresh();}catch(cause){setLoadError('操作已完成，列表刷新失败：'+message(cause));}
      }
    } catch(cause) {if(mounted.current)setError(message(cause));}
    finally {actionInFlight.current=false;if(mounted.current)setBusy(false);}
  }
  function post<T>(path:string,body:unknown,method='POST') {return api<T>('/api/qa/platform'+path,{method,body:JSON.stringify(body)});}
  function choosePlan(id:string) {setPlanId(id);onSelectPlan(id);setGatedRequirementId('');setSelected([]);setEditing(null);setFilter('all');setError('');setNotice('');}
  function toggleCreate() {setShowCreate(value=>!value);setError('');setNotice('');}
  function submitCandidate(event:FormEvent<HTMLFormElement>) {
    event.preventDefault();const data=new FormData(event.currentTarget);
    void perform(async()=>{
      const result=await post<{id:string}>('/candidates',{name:String(data.get('name')||'').trim(),source_round_id:sourceRoundId});
      setCandidateChoice(result.id);setCandidateSource('existing');
      setNotice('候选固件已保存。确认计划名称后创建验证清单。');
    });
  }
  function importCandidate(value:PrebuiltImport) {
    void perform(async()=>{
      const result=await post<{id:string}>('/candidates/prebuilt',value);
      setCandidateChoice(result.id);setCandidateSource('existing');
      setNotice('已有镜像已校验并保存，未执行烧录。继续创建验证清单后选择用例。');
    });
  }
  function submitPlan(event:FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if(!candidateId){setError('请等待所选候选固件加载完成后再创建计划。');return;}
    const data=new FormData(event.currentTarget);
    void perform(async()=>{
      const result=await post<{id:string}>('/plans',{candidate_id:candidateId,name:String(data.get('name')||'').trim()});
      choosePlan(result.id);setShowCreate(false);setNotice('验证计划已创建，选择需要执行的项目即可开始。');
    });
  }
  function submitSchedule(event:FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if(detecting||!device||!benchId){setError('请等待在线模组检测完成并选择模组。');return;}
    const data=new FormData(event.currentTarget);
    void perform(async()=>{
      await post('/schedules',{name:String(data.get('name')||'').trim(),product_id:productId,bench_id:benchId,device_serial:device,source_kind:sourceKind,source:String(data.get('source')||'').trim(),timezone:data.get('timezone'),daily_time:data.get('daily_time'),skip_unchanged:data.get('skip')==='on',test_case_ids:validCaseIds,test_case_parameters:Object.fromEntries(validCaseIds.map(id=>[id,JSON.parse(parameters[id]||'{}')])),enabled:data.get('enabled')==='on'});
      setShowCreate(false);setCaseIds([]);setParameters({});setNotice('定时计划已保存。');
    });
  }
  const benchSelector=<div className="platform-device-picker">
    <div className="form-columns">
      <label>测试台{enabledBenches.length===1?<span className="platform-fixed-value">{enabledBenches[0].name}<small>已自动选择</small></span>:<select required disabled={busy} value={benchId} onChange={e=>setBenchChoice(e.target.value)}><option value="">选择测试台</option>{enabledBenches.map(b=><option key={b.id} value={b.id}>{b.name}</option>)}</select>}</label>
      <label>在线模组{devices.length===1?<span className="platform-fixed-value">{devices[0].serial}<small>{devices[0].version||'版本待确认'} · 已自动选择</small></span>:<select required disabled={busy||detecting||!devices.length} value={device} onChange={e=>setDeviceChoice({benchId,serial:e.target.value})}><option value="">{detecting?'正在检测…':devices.length?'选择模组':'等待在线模组'}</option>{devices.map(d=><option key={d.serial} value={d.serial}>{d.serial} · {d.version}</option>)}</select>}</label>
    </div>
    <div className="platform-device-status"><span role="status">{!enabledBenches.length?'暂无可用测试台，请先完成测试台配置。':!benchId?'选择测试台后自动检测在线模组。':detecting?'正在检测在线模组…':discovery.error&&discovery.key===discoveryKey?'检测失败：'+discovery.error:!devices.length?'未发现在线模组，请检查连接后重新检测。':'已检测到 '+devices.length+' 台模组'}</span><button type="button" disabled={busy||!benchId||detecting} onClick={()=>setDeviceRefresh(value=>value+1)}>重新检测</button></div>
  </div>;

  return <div className="platform-workbench platform-clean">
    <div className="tw-heading"><div><span className="panel-label">{mode==='releases'?'RELEASE VALIDATION':'SCHEDULED REGRESSION'}</span><h2>{mode==='releases'?'发布验证':'定时回归'}</h2><p>{mode==='releases'?'固定候选固件，分批完成验证，持续追踪覆盖缺口。':'按时编译、烧录并执行固定用例，结果统一保存在测试记录中。'}</p></div><button disabled={busy||!loaded} onClick={toggleCreate}>{showCreate?'取消创建':mode==='releases'?'新建验证计划':'新建定时计划'}</button></div>
    {error&&<p className="error-banner" role="alert">{error}</p>}
    {loadError&&<div className="error-banner" role="alert">{loadError}<button type="button" disabled={busy} onClick={()=>void refresh().catch(cause=>setLoadError(message(cause)))}>重新加载</button></div>}
    {notice&&<p className="platform-notice" role="status">{notice}</p>}
    {!loaded&&!loadError&&<p className="panel" role="status">正在加载…</p>}

    {mode==='releases'&&loaded&&<>
      {showCreate?<section className="panel platform-create-panel">
        <div className="platform-step-heading"><span>1</span><div><h3>选择验证固件</h3><p>复用已有候选、导入 175 上的镜像，或从成功构建中保存新版本。</p></div></div>
        <div className="platform-segmented" aria-label="候选固件来源">
          <button type="button" disabled={busy||!candidates.length} aria-pressed={candidateSource==='existing'} onClick={()=>setCandidateSource('existing')}>已有候选（{candidates.length}）</button>
          <button type="button" disabled={busy} aria-pressed={candidateSource==='prebuilt'} onClick={()=>setCandidateSource('prebuilt')}>导入已有镜像</button>
          <button type="button" disabled={busy} aria-pressed={creatingCandidate} onClick={()=>setCandidateSource('new')}>从构建创建</button>
        </div>
        {candidateSource==='prebuilt'?<PrebuiltCandidateForm products={products} initialProductId={initialPrebuiltProductId} api={api} busy={busy} onImport={importCandidate}/>:creatingCandidate?<form onSubmit={submitCandidate}>
          <div className="form-columns"><label>候选版本名称<input name="name" required maxLength={160} placeholder="例如 V2.1.0-RC1" disabled={busy}/></label><label>源构建轮次<select required value={sourceRoundId} onChange={e=>setSourceRoundChoice(e.target.value)} disabled={busy}><option value="">选择构建产物</option>{sourceRounds.map(r=><option key={r.id} value={r.id}>TEST-{r.display_number} · {products.find(p=>p.id===r.product_id)?.name} · {String((r.product_snapshot.test_point as {subject?:string}|undefined)?.subject||r.commit_sha.slice(0,12))}</option>)}</select></label></div>
          {!sourceRounds.length?<p className="platform-empty">暂无可用构建产物，请先完成一次编译构建。</p>:<p className="platform-hint">保存固定镜像后，后续各轮复用同一份固件。</p>}
          <button disabled={busy||!sourceRoundId}>{busy?'保存中…':'保存候选并继续'}</button>
        </form>:<form onSubmit={submitPlan}>
          {!candidates.length&&<p className="platform-empty">暂无候选固件。可选择“导入已有镜像”，或从成功构建创建。</p>}
          <label>候选固件<select required value={candidateId} onChange={e=>setCandidateChoice(e.target.value)} disabled={busy||candidateAwaitingRefresh}><option value="">选择候选固件</option>{candidates.map(c=><option key={c.id} value={c.id}>{c.name} · {products.find(p=>p.id===c.product_id)?.name||c.subject}</option>)}</select></label>
          {candidateAwaitingRefresh&&<p className="platform-hint" role="status">所选候选固件尚未加载，请刷新列表后继续。<button type="button" disabled={busy} onClick={()=>void refresh().catch(cause=>setLoadError(message(cause)))}>重新加载候选</button></p>}
          {candidate&&<p className="platform-hint">{candidateDescription(candidate)} · {candidate.images.length} 个固定镜像</p>}
          <div className="platform-step-heading"><span>2</span><div><h3>建立验证清单</h3><p>纳入自动执行、人工门控与待定义场景，按项目分批完成。</p></div></div>
          <label>计划名称<input key={candidateId} name="name" required maxLength={160} defaultValue={candidate?candidate.name+' 发布验证':''} placeholder="填写验证计划名称" disabled={busy}/></label>
          <button disabled={busy||!candidateId}>{busy?'创建中…':'创建验证计划'}</button>
        </form>}
      </section>:plan?<section className="panel platform-plan-detail">
        <button className="platform-back" type="button" disabled={busy} onClick={()=>choosePlan('')}>← 全部验证计划</button>
        <header className="platform-detail-heading"><div><span className="platform-eyebrow">{plan.candidate_name} · 清单 v{plan.revision}</span><h3>{plan.name}</h3><p>{plan.final_report?'验证报告已冻结':plan.ready?'全部要求已通过，可以完成验证。':'选择本次执行项目，完成后自动回填结果。'}</p></div><a href={apiBase+'/api/qa/platform/plans/'+plan.id+'/report'} download>下载{plan.final_report?'正式':'草稿'}报告</a></header>
        <div className="platform-coverage"><strong>{plan.counts.passed||0}<span> / {plan.total} 项通过</span></strong><progress aria-label="验证通过进度" value={plan.counts.passed||0} max={plan.total||1}/><small>{Object.entries(plan.counts).filter(([key,value])=>key!=='passed'&&value>0).map(([key,value])=>(labels[key]||key)+' '+value).join(' · ')||'所有项目均已通过'}</small></div>
        <details className="platform-secondary"><summary>固件信息与固定镜像</summary>{candidates.filter(c=>c.id===plan.candidate_id).map(c=><div key={c.id}><p>{candidateDescription(c)}</p>{c.source?.kind==='prebuilt'&&<><p>175 镜像目录：<code>{c.source.image_directory}</code></p><p>导入人：{c.source.operator} · {c.source.imported_at?date(c.source.imported_at):''}</p><p>镜像已校验并保存；未通过 Hub 编译。</p></>}</div>)}{candidates.find(c=>c.id===plan.candidate_id)?.images.map(i=><p key={i.filename}>{i.filename} · {(i.size_bytes/1024/1024).toFixed(1)} MB<br/><code>{i.sha256}</code></p>)}</details>
        {!plan.final_report&&supportedRequirements.length>0&&<details className="platform-secondary" open={pendingDefinitions.length>0}><summary>同步用例定义{pendingDefinitions.length?' · '+pendingDefinitions.length+' 项已接入':''}</summary>
          <p className="platform-hint">同步当前用例库已接入的自动执行与人工门控定义。清单项目、参数与历史证据保留；定义变化的项目将标记为待更新，需要重新测试。同步需由操作人确认，已冻结报告不能修改。</p>
          <form className="platform-sync-special" onSubmit={e=>{e.preventDefault();const data=new FormData(e.currentTarget);void perform(async()=>{const result=await post<{revision:number;updated:string[]}>('/plans/'+plan.id+'/sync-definitions',{revision:plan.revision,operator:data.get('operator'),reason:data.get('reason')});setNotice(result.updated.length?'已同步 '+result.updated.length+' 项用例定义，清单修订为 v'+result.revision+'。':'用例定义已是最新版本。');});}}>
            <div className="form-columns"><label>操作人<input name="operator" required disabled={busy}/></label><label>同步原因<input name="reason" required defaultValue="同步当前已接入的用例与执行工具定义" disabled={busy}/></label></div><button disabled={busy}>同步用例定义</button>
          </form>
        </details>}
        {!plan.final_report&&<div className="platform-toolbar"><strong>已选 {selectedIds.length} 项自动用例</strong><button disabled={busy} onClick={()=>setSelected(plan.requirements.filter(r=>r.mode==='automatic'&&['unmeasured','stale'].includes(r.status)).map(r=>r.id))}>选择未测 / 待更新</button><button disabled={busy} onClick={()=>setSelected(plan.requirements.filter(r=>r.mode==='automatic'&&['failed','error','blocked'].includes(r.status)).map(r=>r.id))}>选择失败 / 阻塞</button><button disabled={busy||!selectedIds.length} onClick={()=>setSelected([])}>清空</button></div>}
        {!plan.final_report&&selectedIds.length>0&&<form className="platform-execution-card" onSubmit={e=>{e.preventDefault();if(detecting||!device||!benchId){setError('请等待在线模组检测完成并选择模组。');return;}void perform(async()=>{const result=await post<{round_id:string}>('/plans/'+plan.id+'/execute',{revision:plan.revision,requirement_ids:selectedIds,bench_id:benchId,device_serial:device});openRound(result.round_id);});}}>
          <div className="platform-execution-heading"><div><h4>自动执行已选 {selectedIds.length} 项</h4><p>烧录 {plan.candidate_name} 的固定镜像后运行用例。</p></div></div>
          {benchSelector}<button className="primary-button" disabled={busy||detecting||!device||!benchId}>{busy?'正在启动…':'烧录候选固件并测试'}</button>
        </form>}
        {!plan.final_report&&gatedRequirement&&<form className="platform-execution-card platform-gated-launch" aria-labelledby="platform-gated-title" onSubmit={e=>{
          e.preventDefault();if(detecting||!device||!benchId){setError('请等待在线模组检测完成并选择模组。');return;}
          const data=new FormData(e.currentTarget);
          void perform(async()=>{const result=await post<{id:string}>('/plans/'+plan.id+'/requirements/'+gatedRequirement.id+'/special-runs',{revision:plan.revision,bench_id:benchId,device_serial:device,operator:data.get('operator')});openSpecial(result.id,gatedRequirement.code,plan.id);});
        }}>
          <div className="platform-execution-heading"><div><h4 id="platform-gated-title" ref={gatedHeadingRef} tabIndex={-1}>人工门控 · {gatedRequirement.name}</h4><p>关联本计划与候选固件，按步骤确认操作，结果自动回填清单。</p></div><button type="button" disabled={busy} onClick={()=>setGatedRequirementId('')}>收起</button></div>
          {benchSelector}
          <p className="platform-hint">确认后自动烧录本计划的候选固件并核验版本，再进入门控步骤。准备与门控期间持续占用测试台，并锁定同一模组。</p>
          <label>操作者<input name="operator" required maxLength={120} disabled={busy} placeholder="姓名或工号"/></label>
          <div className="platform-toolbar"><button className="primary-button" disabled={busy||detecting||!device||!benchId}>{busy?'正在创建…':'烧录候选并开始门控'}</button>

          </div>
        </form>}
        <div className="platform-segmented platform-filters" aria-label="筛选覆盖状态">{([['all','全部'],['todo','待完成'],['attention','失败 / 阻塞'],['active','执行中'],['passed','已通过']] as [CoverageFilter,string][]).map(([value,label])=><button key={value} type="button" aria-pressed={filter===value} onClick={()=>setFilter(value)}>{label}<span>{plan.requirements.filter(r=>matchesFilter(r,value)).length}</span></button>)}</div>
        <div className="platform-requirements">
          {Array.from(new Set(plan.requirements.filter(r=>matchesFilter(r,filter)).map(requirementGroup))).map(group=>{
            const requirements=plan.requirements.filter(r=>requirementGroup(r)===group&&matchesFilter(r,filter));
            const automatic=requirements.some(r=>r.mode==='automatic');
            const gatedCount=requirements.filter(r=>r.mode==='gated').length;
            const pendingCount=requirements.filter(r=>r.mode==='pending').length;
            return <details className="platform-coverage-group" key={group+filter} open={automatic||gatedCount>0||filter!=='all'}><summary>{group}<span>{requirements.length} 项{gatedCount?' · 人工门控 '+gatedCount:''}{pendingCount?' · 待定义 '+pendingCount:''}</span></summary>
              {requirements.map(r=><article className="platform-requirement" key={r.id}>
                <div className="platform-requirement-row">
                  {r.mode==='automatic'?<label><input type="checkbox" disabled={busy||!!plan.final_report} checked={selectedIds.includes(r.id)} onChange={e=>setSelected(ids=>e.target.checked?[...ids,r.id]:ids.filter(id=>id!==r.id))}/><span><strong>{r.name}</strong><small>{r.code} · 自动执行</small></span></label>:<div className="platform-requirement-title"><strong>{r.name}</strong><small>{r.code} · {r.mode==='gated'?'人工门控 · 已接入':'待定义测试方式'}</small></div>}
                  <span className={'wb-outcome result-'+r.status}>{labels[r.status]||r.status}</span>
                  {r.latest?.round_id&&<button type="button" onClick={()=>openRound(r.latest!.round_id!)}>TEST-{r.latest.round_number} →</button>}
                  {r.latest?.special_run_id&&<button type="button" onClick={()=>openSpecial(r.latest!.special_run_id!,r.code,plan.id)}>{activeStatuses.includes(r.status)?'继续门控流程':'查看门控记录'} →</button>}
                  {r.mode==='gated'&&!plan.final_report&&!activeStatuses.includes(r.status)&&<button type="button" disabled={busy} onClick={()=>{setSelected([]);if(gatedRequirementId===r.id)focusLaunchHeading(gatedHeadingRef.current);else setGatedRequirementId(r.id);setError('');setNotice('');}}>{r.latest?.special_run_id?'重新执行':'准备执行'}</button>}
                </div>
                {r.latest?.message&&<p className="platform-evidence-message">{r.latest.message}</p>}
                {r.mode==='gated'&&r.code==='SPECIAL-OTA'&&<div className="platform-ota-variants"><p>分别执行 3 种异常，全部通过后此项通过。</p>{([['corrupt','损坏包'],['wrong_name','错误包名'],['interrupted','升级中断电']] as [string,string][]).map(([variant,label])=><span key={variant}>{label}<span className={'wb-outcome result-'+(r.variant_statuses?.[variant]||'unmeasured')}>{labels[r.variant_statuses?.[variant]||'unmeasured']||r.variant_statuses?.[variant]}</span></span>)}</div>}

                {(r.mode!=='pending'||r.history.length>0)&&<details className="platform-requirement-more"><summary>{r.mode==='gated'?'门控定义与历史记录':'参数与历史记录'}{r.history.length?'（'+r.history.length+'）':''}</summary>
                  {r.mode==='gated'&&r.adapter&&<><p className="platform-hint">定义版本 {r.adapter.version} · {r.adapter.steps.length} 个步骤。启动后先烧录并核验候选固件，再由操作员逐步确认。</p><ol>{r.adapter.steps.map(step=><li key={step.id}><strong>{step.title}</strong><p>{step.instruction}</p><small>记录：{step.records.join('；')}</small></li>)}</ol></>}
                  {r.mode==='automatic'&&<><p className="platform-hint">{Object.keys(r.parameters).length?'当前参数：'+JSON.stringify(r.parameters):'当前使用默认参数'}{r.code==='STREAM-RGB-DEPTH'&&!Object.keys(r.parameters).length?' · 默认持续 5 小时':''}</p>{!plan.final_report&&editing!==r.id&&<button type="button" disabled={busy} onClick={()=>{setEditing(r.id);setEditValue(JSON.stringify(r.parameters));setEditRevision(plan.revision);}}>修改参数 / 更新用例定义</button>}</>}
                  {editing===r.id&&<form onSubmit={e=>{e.preventDefault();const data=new FormData(e.currentTarget);void perform(async()=>{await post('/plans/'+plan.id+'/requirements/'+r.id,{revision:editRevision,parameters:JSON.parse(editValue),operator:data.get('operator'),reason:data.get('reason')},'PATCH');setEditing(null);setNotice('清单已修订，变更的项目需要重新测试。');});}}>
                    {renderParameters(r.code,editValue,setEditValue)}<p className="platform-hint">保存当前参数及最新用例定义，变更的项目需要重新测试。</p><div className="form-columns"><label>操作人<input name="operator" required disabled={busy}/></label><label>修改原因<input name="reason" required disabled={busy}/></label></div><button disabled={busy}>保存修订</button><button type="button" disabled={busy} onClick={()=>setEditing(null)}>取消</button>
                  </form>}
                  {r.history.map(h=><p className="platform-history-row" key={h.id}>{date(h.created_at)} · {labels[h.status]||h.status} · {h.message} {h.round_id&&<button type="button" onClick={()=>openRound(h.round_id!)}>查看 TEST-{h.round_number}</button>}{h.special_run_id&&<button type="button" onClick={()=>openSpecial(h.special_run_id!,r.code,plan.id)}>查看门控证据</button>}</p>)}
                </details>}
              </article>)}
            </details>;
          })}
          {!plan.requirements.some(r=>matchesFilter(r,filter))&&<p className="platform-empty">此状态下没有清单项。</p>}
        </div>
        {!plan.final_report&&<details className="platform-secondary platform-advanced" open={plan.ready}><summary>关联历史结果与完成验证</summary>
          <form onSubmit={e=>{e.preventDefault();const data=new FormData(e.currentTarget);void perform(async()=>{await post('/plans/'+plan.id+'/link',{round_id:data.get('round_id'),requirement_ids:selectedIds});setNotice('匹配的结果已关联。');});}}><h4>引用历史测试结果</h4><p className="platform-hint">为已选 {selectedIds.length} 项校验并引用相同固件、用例、参数与工具的测试证据。</p><label>历史轮次<select required name="round_id" disabled={busy}><option value="">选择已完成的轮次</option>{rounds.filter(r=>r.product_id===candidates.find(c=>c.id===plan.candidate_id)?.product_id&&!sourceActiveStatuses.includes(r.status)).map(r=><option key={r.id} value={r.id}>TEST-{r.display_number} · {labels[r.status]||r.status}</option>)}</select></label><button disabled={busy||!selectedIds.length}>校验并关联已选项目</button></form>
          <form className="platform-execute" onSubmit={e=>{e.preventDefault();const data=new FormData(e.currentTarget);void perform(async()=>{await post('/plans/'+plan.id+'/finalize',{revision:plan.revision,operator:data.get('operator')});setNotice('验证报告已冻结。');});}}><h4>冻结验证报告</h4><p className="platform-hint">{plan.ready?'全部要求已通过，确认后生成正式报告。':'仍有未通过、未测或待定义项，全部通过后可冻结报告。'}此操作仅完成验证。</p><label>确认人<input name="operator" required disabled={busy||!plan.ready}/></label><button disabled={busy||!plan.ready}>冻结验证报告</button></form>
        </details>}
      </section>:<>
        <div className="platform-list-summary"><strong>{plans.length} 个验证计划</strong><span>{plans.filter(p=>!p.final_report).length} 个进行中 · {plans.filter(p=>!!p.final_report).length} 个已完成</span></div>
        <div className="platform-plan-list">{plans.map(p=><button className="panel platform-plan-card" key={p.id} onClick={()=>choosePlan(p.id)}><span className="platform-plan-card-top"><strong>{p.candidate_name}</strong><span className={'wb-outcome result-'+(p.final_report?'passed':p.ready?'passed':'unmeasured')}>{p.final_report?'已完成':p.ready?'待冻结报告':'验证中'}</span></span><span>{p.name}</span><progress aria-label={p.name+' 测试通过进度'} value={p.counts.passed||0} max={p.total||1}/><span>{p.counts.passed||0} / {p.total} 项通过</span><small>{p.final_report?'查看验证结果与正式报告':Object.entries(p.counts).filter(([key,value])=>key!=='passed'&&value>0).map(([key,value])=>(labels[key]||key)+' '+value).join(' · ')}</small><span className="platform-card-link">查看验证清单 →</span></button>)}</div>
        {!plans.length&&<div className="panel platform-empty"><h3>尚无发布验证计划</h3><p>选择已有候选、导入现有镜像或复用成功构建，建立第一份验证清单。</p><button className="primary-button" onClick={toggleCreate}>新建验证计划</button></div>}
        {candidates.length>0&&<details className="panel platform-candidate-library"><summary>候选固件库 · {candidates.length} 个版本</summary>{candidates.map(c=><article key={c.id}><div><strong>{c.name}</strong><small>{products.find(p=>p.id===c.product_id)?.name} · {candidateDescription(c)}</small></div><button onClick={()=>{setCandidateChoice(c.id);setCandidateSource('existing');setShowCreate(true);}}>基于此版本建计划</button></article>)}</details>}
      </>}
    </>}

    {mode==='schedules'&&loaded&&<>
      {showCreate?<form className="panel platform-schedule-form" onSubmit={submitSchedule}>
        <section><div className="platform-step-heading"><span>1</span><div><h3>版本与执行时间</h3><p>选择固定测试来源和每天执行的时间。</p></div></div>
          <div className="form-columns"><label>计划名称<input name="name" required maxLength={160} placeholder="例如 RMSL321 每日回归" disabled={busy}/></label><label>产品{products.length===1?<span className="platform-fixed-value">{products[0].name}<small>已自动选择</small></span>:<select required value={productId} disabled={busy} onChange={e=>setProductChoice(e.target.value)}><option value="">选择产品</option>{products.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}</select>}</label></div>
          <div className="form-columns"><label>代码来源<select value={sourceKind} disabled={busy} onChange={e=>setSourceKind(e.target.value)}><option value="branch">分支最新版本</option><option value="change">Gerrit Change / Patch Set</option></select></label><label>{sourceKind==='branch'?'分支名称':'Gerrit 链接'}<input key={sourceKind} name="source" required disabled={busy} placeholder={sourceKind==='branch'?'master':'http://gerrit/c/project/+/123'}/></label></div>
          {sourceKind==='change'&&<p className="platform-hint">链接带 Patch Set 编号则测试固定版本；未带编号时测试最新 Patch Set。</p>}
          <div className="form-columns"><label>每天执行时间<input name="daily_time" type="time" required defaultValue="02:00" disabled={busy}/></label><label>时区<select name="timezone" defaultValue="Asia/Shanghai" disabled={busy}><option value="Asia/Shanghai">北京时间（Asia/Shanghai）</option><option value="UTC">协调世界时（UTC）</option></select></label></div>
        </section>
        <section><div className="platform-step-heading"><span>2</span><div><h3>测试台与模组</h3><p>计划固定使用所选模组。</p></div></div>{benchSelector}</section>
        <section><div className="platform-step-heading"><span>3</span><div><h3>测试范围</h3><p>按功能选择用例，展开单项调整参数。</p></div></div><CaseSelection cases={executableCases} selected={validCaseIds} onChange={setCaseIds} parameters={parameters} onParameters={(id,value)=>setParameters(current=>({...current,[id]:value}))} renderParameters={renderParameters} disabled={busy}/></section>
        <section className="platform-schedule-confirm"><h3>保存计划</h3><p className="platform-hint">每天定时编译、烧录并执行 {validCaseIds.length} 项。测试台忙时排队，上一轮未结束时跳过本次触发。</p>
          <label className="platform-checkbox"><input name="skip" type="checkbox" defaultChecked disabled={busy}/>版本未变化且上一轮已通过时跳过</label>
          <label className="platform-checkbox"><input name="enabled" type="checkbox" disabled={busy}/>保存后启用定时执行</label>
          <details className="platform-secondary"><summary>用例版本说明</summary><p>计划保存当前用例和工具定义；定义更新后需建立新计划。</p></details>
          <button className="primary-button" disabled={busy||!validCaseIds.length||!device||!benchId||!productId||detecting}>{busy?'保存中…':'保存定时计划'}</button>
        </section>
      </form>:<>
        <div className="platform-list-summary"><strong>{schedules.length} 个定时计划</strong><span>{schedules.filter(s=>s.enabled).length} 个已启用</span></div>
        {!schedules.length&&<div className="panel platform-empty"><h3>尚无定时计划</h3><p>创建后默认暂停，可检查配置后再启用。</p><button className="primary-button" onClick={toggleCreate}>新建定时计划</button></div>}
        <div className="platform-schedule-list">{schedules.map(s=><section className="panel platform-schedule-card" key={s.id}>
          <header><div><span className="platform-eyebrow">{products.find(p=>p.id===s.config.product_id)?.name} · {s.enabled?'已启用':'已暂停'}</span><h3>{s.name}</h3></div><button disabled={busy} onClick={()=>void perform(async()=>{await post('/schedules/'+s.id,{enabled:!s.enabled},'PATCH');setNotice(s.name+(s.enabled?'已暂停。':'已启用。'));})}>{s.enabled?'暂停计划':'启用计划'}</button></header>
          <div className="platform-schedule-meta"><strong>每天 {s.config.daily_time}</strong><span>{s.config.timezone==='Asia/Shanghai'?'北京时间':s.config.timezone}</span><span>{s.config.test_case_ids?.length||0} 项用例</span></div>
          <p>{s.config.source_kind==='branch'?'分支':'Change'} · {s.config.source}</p>
          <p className="platform-hint">{s.enabled?'下次执行：'+date(s.next_fire_at)+'（北京时间）':'当前暂停，启用后从下次计划时间开始。'}</p>
          {s.runs[0]&&<div className="platform-latest-run"><span>最近一次 · {date(s.runs[0].scheduled_at)}</span><strong>{labels[s.runs[0].state]||s.runs[0].state}</strong>{s.runs[0].round_id&&<button onClick={()=>openRound(s.runs[0].round_id!)}>TEST-{s.runs[0].round_number} →</button>}</div>}
          <details className="platform-secondary"><summary>计划配置与执行记录</summary>
            <p>测试台：{benches.find(b=>b.id===s.config.bench_id)?.name||s.config.bench_id||'未记录'} · 模组：{s.config.device_serial||'未记录'}</p><p>{s.config.skip_unchanged?'版本未变且上一轮通过时跳过':'每次按计划执行'}</p>
            <details><summary>固定用例与参数（{s.config.test_case_ids?.length||0}）</summary>{s.config.test_case_ids?.map(id=><p key={id}>{cases.find(c=>c.id===id)?.name||id}<small className="platform-saved-parameters">{Object.keys(s.config.test_case_parameters?.[id]||{}).length?JSON.stringify(s.config.test_case_parameters?.[id]):'默认参数'}</small></p>)}</details>
            {s.runs.map(r=><p className="platform-history-row" key={r.id}>{date(r.scheduled_at)} · {labels[r.state]||r.state} · {r.message} {r.round_id&&<button onClick={()=>openRound(r.round_id!)}>查看 TEST-{r.round_number}</button>}</p>)}
            {!s.runs.length&&<p>尚未触发执行。</p>}
          </details>
        </section>)}</div>
      </>}
    </>}
  </div>;
}
