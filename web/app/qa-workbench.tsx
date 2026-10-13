'use client';
/* Captured evidence must be served unchanged from the artifact API. */
/* eslint-disable @next/next/no-img-element */

import scenarioCatalog from './scenario-catalog.json';
import PlatformWorkbench from './platform-workbench';
import CurrentFirmwareGates from './current-firmware-gates';
import SpecialScenarios, { type Definition as SpecialDefinition, type Run as SpecialRun } from './special-scenarios';
import { CaseSelection, caseGroup } from './workbench-controls';
import './workbench.css';

import { FormEvent, useCallback, useEffect, useState, useRef } from 'react';

type StreamParameters = Record<string, number | null | Record<string, Record<string, number>>>;
const streamFields = [
  ['duration_seconds','正式测试时长（秒）',18000,86400],
  ['warmup_seconds','最短预热时间（秒）',10,60],
  ['stall_timeout_seconds','无帧超时（秒）',5,300],
  ['fps','目标帧率（fps）',10,60],
  ['rgb_width','RGB 宽度（像素）',640,4096],
  ['rgb_height','RGB 高度（像素）',540,4096],
  ['depth_width','Depth 宽度（像素）',640,4096],
  ['depth_height','Depth 高度（像素）',352,4096],
] as const;
const streamLimits = [
  ['min_fps','最低平均帧率（fps）'],
  ['max_transport_drop_percent','最高传输丢帧率（%）'],
  ['max_latency_mean_ms','最大平均估算延时（ms）'],
  ['max_latency_max_ms','最大单帧估算延时（ms）'],
  ['max_interval_jitter_ms','最大到帧间隔抖动（ms）'],
  ['max_latency_jitter_ms','最大延时抖动（ms）'],
] as const;
function StreamParameterForm({value,onChange}:{value:string;onChange:(value:string)=>void}) {
  const config:StreamParameters=JSON.parse(value || '{}');
  const thresholds=(config.thresholds || {}) as Record<string,Record<string,number>>;
  function updateField(key:string,raw:string) {
    onChange(JSON.stringify({...config,[key]:raw===''?null:Number(raw)}));
  }
  function updateLimit(stream:string,key:string,raw:string) {
    const limits={...thresholds[stream]};
    if(raw==='')delete limits[key];else limits[key]=Number(raw);
    onChange(JSON.stringify({...config,thresholds:{...thresholds,[stream]:limits}}));
  }
  return <div className="stream-parameters">
    <fieldset><legend>采集设置</legend>
      <p>默认正式测试 5 小时。两路完成预热并稳定后开始计时，预热不占用测试时长。</p>
      <div className="stream-parameter-grid">{streamFields.map(([key,label,defaultValue,max])=><label key={key}>{label}<input type="number" required min={1} max={max} step={1} value={key in config ? config[key] as number | null ?? '' : defaultValue} onChange={event=>updateField(key,event.target.value)}/></label>)}</div>
    </fieldset>
    <p>性能阈值可选，留空仅记录实测值；填写后才参与通过／失败判定。抖动按标准差统计，延时为时间同步后的估算值。</p>
    <div className="stream-threshold-grid">{(['rgb','depth'] as const).map(stream=><fieldset key={stream}><legend>{stream==='rgb'?'RGB':'Depth'} 判定阈值</legend><div className="stream-parameter-grid">{streamLimits.map(([key,label])=><label key={key}>{label}<input type="number" min={0} max={key==='max_transport_drop_percent'?100:undefined} step="any" placeholder="不设阈值" value={thresholds[stream]?.[key] ?? ''} onChange={event=>updateLimit(stream,key,event.target.value)}/></label>)}</div></fieldset>)}</div>
  </div>;
}

const caseParameterFields:Record<string, {key:string;label:string;defaultValue?:number;max?:number;hint:string}[]> = {
  'INFO-PING':[{key:'max_rtt_ms',label:'每次 Ping RTT 上限（ms）',defaultValue:50,max:30000,hint:'默认 50 ms，每次测量均需达标。'}],
  'INFO-SENSORS':[
    {key:'window_seconds',label:'观测窗口（秒）',defaultValue:5,max:30,hint:'默认 5 秒，最长 30 秒。'},
    {key:'fps_tolerance_percent',label:'帧率允许偏差（±%）',defaultValue:10,max:50,hint:'默认 ±10%，最大 ±50%。'},
  ],
  'BASIC-FOV-WRITE':[{key:'target_fov',label:'目标 FOV（°）',hint:'留空自动选择设备支持的另一个 FOV；测试后恢复原值。手动值必须在设备支持列表内。'}],
};
function CaseParameterForm({code,value,onChange}:{code:string;value:string;onChange:(value:string)=>void}) {
  const config:Record<string,number|null>=JSON.parse(value || '{}');
  const fields=caseParameterFields[code];
  if(!fields)return null;
  return <div className="stream-parameters"><fieldset><legend>测试参数</legend><div className="stream-parameter-grid">{fields.map(field=><label key={field.key}>{field.label}<input type="number" step="any" min={0.000001} max={field.max} required={field.defaultValue!==undefined} placeholder={field.defaultValue===undefined?'自动选择':undefined} value={field.key in config ? config[field.key] ?? '' : field.defaultValue ?? ''} onChange={event=>{
    const next={...config};const raw=event.target.value;
    if(raw==='' && field.defaultValue===undefined)delete next[field.key];
    else next[field.key]=raw===''?null:Number(raw);
    onChange(JSON.stringify(next));
  }}/><small>{field.hint}</small></label>)}</div></fieldset></div>;
}

type Repository = {
  id: string; name: string; root_path: string; default_branch: string; enabled: boolean;
};
type SdkRepository = {
  id: string; name: string; root_path: string; board_configs: string[]; artifact_root: string;
  artifact_patterns: string[]; enabled: boolean;
};
type ManifestSnapshot = {
  sdk_path: string; manifest_file: string; manifest_branch: string; manifest_commit: string;
  sync_status: string; project_count: number; local_changes: string[]; inspected_at?: string;
};
type Product = {
  id: string; code: string; name: string; sdk_repository_id: string; tested_repository_id: string;
  gerrit_project: string; board_config: string; kernel_baseline: string; uboot_baseline: string;
  rkbin_baseline: string; status: 'draft' | 'verifying' | 'ready' | 'invalid';
  manifest_snapshot: ManifestSnapshot; repository_snapshot: Record<string, unknown>;
  baseline_result: Record<string, unknown> | null; sdk_repository: SdkRepository;
  tested_repository: Repository; created_at: string;
};
type TestPoint = {
  id: string; product_id: string; change_number: number; patchset_number: number; commit_sha: string;
  ref: string; subject: string; branch: string; owner: string; submitted_at: string | null; ready: boolean; created_at: string;
};
type TestCase = {
  id: string; code: string; name: string; description: string; command: string;
  category: 'basic' | 'special';
  expected_exit_code: number; timeout_seconds: number; enabled: boolean;
  risk_level: 'read_only' | 'state_change' | 'firmware_write';
};
type TestBench = {
  id: string; name: string; host: string; port: number; user: string; remote_root: string; enabled: boolean;
};
type RoundEvent = {
  id: string; sequence: number; event_type: string; status: string; message: string;
  details: Record<string, unknown>; created_at: string;
};
type Classification = {automatic_outcome?:string;outcome?:string;manual:boolean;revision:number;executed:boolean;history:{operator:string;reason:string;outcome:string|null;changed_at:string}[]};
type TestRound = {
  origin?: string; candidate_id?: string; validation_plan_id?: string; schedule_id?: string;
  case_classifications: Record<string,Classification>;
  case_classification_verdict: string;
  id: string; display_number: number; product_id: string; test_point_id: string | null; bench_id: string;
  controller_result: Record<string, unknown> | null;
  status: string; product_snapshot: Record<string, unknown>; manifest_commit: string; board_config: string;
  gerrit_project: string; change_number: number; patchset_number: number; commit_sha: string;
  work_branch: string; build_result: Record<string, unknown> | null; image_snapshot: Array<Record<string, unknown>>;
  test_case_snapshot: Array<Record<string, unknown>>; generated_config: Record<string, unknown> | null;
  remote_path: string | null; last_error: string | null; created_at: string; completed_at: string | null;
  bench_snapshot: TestBench; delivery_result: { passed: boolean; verified_at: string; files: Array<{filename: string; sha256: string; size_bytes: number}> } | null;
  product: Product; test_point: TestPoint | null; bench: TestBench; events: RoundEvent[];
};

type View = 'start' | 'special' | 'products' | 'rounds' | 'cases' | 'benches' | 'releases' | 'schedules';
type Screen = View | 'product-detail' | 'product-wizard' | 'round-wizard' | 'round-detail';
type Dialog = 'point' | 'bench';
type TestMode = 'quick' | 'full' | 'delivery';
const modeLabels: Record<TestMode, string> = {quick:'测试当前固件', full:'编译烧录测试', delivery:'仅构建交付'};
const originLabels: Record<string,string> = {quick:'当前固件测试',manual:'手动测试',scheduled:'定时回归',release:'发布验证'};
const activeStatuses = ['created','checking_out','building','packaging','transferring','verifying','bench_running'];
type FirmwareSource = {kind?:string;image_directory?:string;operator?:string;imported_at?:string;validation?:{passed:boolean;checked_at?:string}};
function firmwareSource(round:TestRound) {return (round.product_snapshot.firmware_source||{}) as FirmwareSource;}
function firmwareIdentity(round:TestRound) {return (round.product_snapshot.firmware_identity||{}) as {kind?:string;value?:string};}
function isPrebuiltRound(round:TestRound) {return firmwareSource(round).kind==='prebuilt';}
function roundSource(round: TestRound) {
  if(isPrebuiltRound(round))return '已有镜像 · '+(firmwareIdentity(round).value||String(round.product_snapshot.candidate_name||'175 本地产物'));
  const subject = String((round.product_snapshot.test_point as {subject?:string}|undefined)?.subject || '');
  return subject || (round.change_number ? `${round.change_number} / PS${round.patchset_number}` : round.commit_sha?.slice(0,12) || '当前模组固件');
}

const productStatus: Record<Product['status'], string> = {
  draft: '待验证', verifying: '验证中', ready: '基线可用', invalid: '基线无效',
};
const roundStatus: Record<string, string> = {
  created: '已创建', checking_out: '同步提交', building: '编译中', packaging: '校验镜像',
  test_complete:'测试通过', test_failed:'测试未通过', bench_running: '测试台执行中', flash_complete: '烧录完成', bench_failed: '测试台失败',
  transferring: '传输中', verifying: '远端校验', remote_ready: '交付完成 · 未测试',
  build_failed: '构建失败', blocked: '已阻塞',
};
const riskLabels: Record<TestCase['risk_level'], string> = {
  read_only: '只读', state_change: '状态变更', firmware_write: '固件写入',
};


function apiBase(): string {
  if (process.env.NEXT_PUBLIC_DEVELOP_HUB_API_URL) return process.env.NEXT_PUBLIC_DEVELOP_HUB_API_URL;
  if (typeof window !== 'undefined') return `${window.location.protocol}//${window.location.hostname}:8175`;
  return 'http://127.0.0.1:8175';
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${apiBase()}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText })) as { detail?: unknown };
    const detail = body.detail;
    if (Array.isArray(detail)) {
      const messages = detail.map((item) => {
        if (!item || typeof item !== 'object') return String(item);
        const issue = item as { loc?: unknown[]; msg?: unknown };
        const location = Array.isArray(issue.loc) ? issue.loc.filter((part) => part !== 'body').join('.') : '';
        return `${location ? `${location}：` : ''}${String(issue.msg || '参数无效')}`;
      });
      throw new Error(messages.join('；') || `请求失败 (${response.status})`);
    }
    if (detail && typeof detail === 'object') throw new Error('message' in detail && typeof detail.message === 'string' ? detail.message : JSON.stringify(detail));
    throw new Error(typeof detail === 'string' && detail ? detail : `请求失败 (${response.status})`);
  }
  return response.json() as Promise<T>;
}

function formatDate(value: string | undefined | null): string {
  return value ? new Date(/(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? value : `${value}Z`).toLocaleString('zh-CN', { hour12: false }) : '—';
}

function StatusPill({ status, label }: { status: string; label: string }) {
  return <span className={`tw-pill status-${status}`}>{label}</span>;
}

type BoardChoice = { board_config: string; menu_index: number; display_command: string };
type Application = { id: string; name: string; root_path: string; gerrit_project: string; snapshot: Record<string, unknown> };
type ProductTab = 'history' | 'baseline' | 'points';

function Facts({ items }: { items: Array<[string, unknown]> }) {
  return <dl className="tw-facts">{items.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{String(value ?? '—')}</dd></div>)}</dl>;
}
function Evidence({ value, label = '查看执行证据' }: { value: unknown; label?: string }) {
  return <details className="wb-evidence"><summary>{label}</summary><pre className="tw-terminal tw-json">{JSON.stringify(value, null, 2)}</pre></details>;
}
function Manifest({ manifest }: { manifest: ManifestSnapshot }) {
  return <Facts items={[
    ['SDK 路径', manifest.sdk_path], ['Manifest 文件', manifest.manifest_file],
    ['Manifest 分支', manifest.manifest_branch], ['Manifest Commit', manifest.manifest_commit],
    ['同步状态', manifest.sync_status], ['项目数量', manifest.project_count],
    ['本地修改', manifest.local_changes.join('，') || '无'],
  ]} />;
}

type BaselineProgress = { stage?: string; message?: string; started_at?: string; updated_at?: string; logs?: string; command?: string; last_output_at?: string };
function BaselineBuild({ product }: { product: Product }) {
  const result = product.baseline_result;
  const progress = (result?.progress || {}) as BaselineProgress;
  const running = product.status === 'verifying';
  const [clock, setClock] = useState(0);
  const [follow, setFollow] = useState(true);
  const log = useRef<HTMLPreElement>(null);
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);
  useEffect(() => { if (follow && log.current) log.current.scrollTop = log.current.scrollHeight; }, [progress.logs, follow]);
  if (!result && !running) return <p>尚未验证。开始后会在这里显示执行阶段和实时日志。</p>;
  const phases = [['queued','准备'], ['inspecting','环境检查'], ['configuring','板级配置'], ['building','编译'], ['validating','镜像校验']];
  const active = phases.findIndex(([key]) => key === progress.stage);
  const end = running ? Math.max(clock, Date.parse(progress.updated_at || progress.started_at || '')) : Date.parse(String(result?.finished_at || progress.updated_at || ''));
  const seconds = progress.started_at && Number.isFinite(end) ? Math.max(0, Math.floor((end-Date.parse(progress.started_at))/1000)) : null;
  const rawLogs = progress.logs || [result?.configuration, result?.build].filter(Boolean).map(value => {
    const command = value as Record<string, unknown>;
    return [command.command, command.stdout_tail, command.stderr_tail].filter(Boolean).join('\n');
  }).join('\n');
  const logs = rawLogs.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '').replace(/\r/g, '\n');
  return <div className="wb-build-progress">
    <div className="wb-build-status" role="status"><strong>{running ? progress.message || '正在准备构建…' : result?.passed ? '基线验证通过' : '基线验证未通过'}</strong><span>{seconds !== null ? `耗时 ${Math.floor(seconds/60)}分 ${seconds%60}秒` : ''}</span></div>
    <ol className="wb-build-phases">{phases.slice(1).map(([key,label],index) => <li key={key} className={result?.passed ? 'done' : active===index+1 ? 'current' : active>index+1 ? 'done' : ''}>{label}{running && active===index+1 ? ' · 执行中' : ''}</li>)}</ol>
    {!running && !result?.passed && <p role="alert" className="tw-warning">{String(result?.error || result?.artifact_error || '请查看下方日志中的错误及命令退出码。')}</p>}
    {progress.command && running && <p className="wb-build-command"><code>{progress.command}</code></p>}
    <div className="wb-log-toolbar"><strong>构建日志</strong><label><input type="checkbox" checked={follow} onChange={event=>setFollow(event.target.checked)} />跟随最新输出</label></div>
    <pre ref={log} className="wb-live-log" tabIndex={0} aria-label="构建实时日志">{logs || (running ? '任务已启动，等待输出…' : '暂无日志')}</pre>
    {running && <p className="wb-build-hint">后台任务持续执行，可离开此页后返回查看。最近日志：{formatDate(progress.last_output_at)}。</p>}
    {!running && result?.passed === true && <p>命令执行成功，{Array.isArray(result.artifacts) ? result.artifacts.length : 0} 个镜像通过校验。</p>}
    {result && <Evidence value={result} label="原始 JSON / 详细证据" />}
  </div>;
}

type BasicDefinition = {id:string; name:string; version:string; command:string; entrypoint:string; assertions:string[]; files?:{path:string;size_bytes:number;sha256:string}[]};
type BasicResult = {scope?:string;image_evidence?:{state:string;reason:string};state:string; outcome:string; message?:string; assertions:{name:string;expected:unknown;actual:unknown;passed:boolean}[]; steps:Record<string,unknown>[]; recovery:Record<string,unknown>; files:Record<string,unknown>[]};
function BasicEvidence({round,code}:{round:TestRound;code:string}) {
  const [kind,setKind]=useState('result'); const [content,setContent]=useState(''); const [error,setError]=useState('');
  useEffect(()=>{
    let alive=true;
    const read=async()=>{try {let offset=0;let text='';let done=false;while(!done && offset<131072){const page=await request<{content:string;next_offset:number;eof:boolean}>(`/api/qa/test-rounds/${round.id}/cases/${code}/evidence?kind=${kind}&offset=${offset}`);text+=page.content;offset=page.next_offset;done=page.eof;}if(alive){setContent(text);setError(done?'':'显示前 128KB；完整文件保留在测试台。');}}catch(cause){if(alive){setContent('');setError(String(cause));}}};
    void read();return()=>{alive=false;};
  },[round.id,code,kind,round.controller_result?.revision]);
  async function downloadFile(kind:string) {
    try {let offset=0;let done=false;const chunks:Uint8Array[]=[];while(!done && offset<131072){const page=await request<{data:string;next_offset:number;eof:boolean}>(`/api/qa/test-rounds/${round.id}/cases/${code}/evidence?kind=${kind}&offset=${offset}`);chunks.push(Uint8Array.from(atob(page.data),c=>c.charCodeAt(0)));offset=page.next_offset;done=page.eof;}if(!done)throw new Error('文件超过下载上限');const url=URL.createObjectURL(new Blob(chunks as BlobPart[]));const link=document.createElement('a');link.href=url;link.download=kind==='input-file'?'payload.bin':'download.bin';link.click();URL.revokeObjectURL(url);}catch(cause){setError(String(cause));}
  }
  let result:BasicResult|null=null;
  if(kind==='result' && content){try {result=JSON.parse(content) as BasicResult;}catch{}}
  return <section className="panel wb-case-evidence" aria-label="用例执行证据"><h3>{round.test_case_snapshot.find(item=>item.code===code)?.name as string || code} · 执行证据</h3><nav className="tw-nav">{[['result','断言与恢复'],['input','执行配置'],['protocol','实际 XU 收发'],['stdout','标准输出'],['stderr','错误日志']].map(([value,label])=><button type="button" key={value} className={kind===value?'active':''} onClick={()=>{setKind(value);setContent('');}}>{label}</button>)}</nav>{error && <p role="status">{error}</p>}{result ? <><p>结果：{outcomeLabels[result.outcome] || result.outcome}</p>{result.message && <p role="alert">{result.message}</p>}{result.scope==='configuration_readback_and_host_stream_capture' && <p>自动结论覆盖配置、有效取流及恢复；图片用于展示实际效果，可在用例列表人工修改通过/失败分类。</p>}{result.scope==='configuration_readback_only' && <p>本用例仅验证配置读写与恢复，不代表图像效果通过。</p>}{result.image_evidence?.state==='unavailable' && <p>这条历史记录没有图片附件；新版切换用例在测试台抓图。</p>}<table><thead><tr><th>断言</th><th>期望</th><th>实际</th><th>结果</th></tr></thead><tbody>{result.assertions.map((item,i)=><tr key={i}><td>{item.name}</td><td>{typeof item.expected==='string'?item.expected:JSON.stringify(item.expected)}</td><td>{typeof item.actual==='string'?item.actual:JSON.stringify(item.actual)}</td><td><span className={`wb-outcome result-${item.passed?'passed':'failed'}`}>{item.passed?'✓ 通过':'× 不通过'}</span></td></tr>)}</tbody></table><h4>实际执行指令</h4><table><thead><tr><th>步骤</th><th>指令</th><th>退出码</th></tr></thead><tbody>{result.steps.map((step,i)=><tr key={i}><td>{String(step.id)}</td><td style={{overflowWrap:'anywhere'}}><code>{Array.isArray(step.command)?step.command.join(' '):String(step.command)}</code></td><td>{String(step.exit_code)}</td></tr>)}</tbody></table><h4>恢复结果</h4><p>{({not_required:'无需恢复',passed:'已恢复',failed:'恢复失败',retained_temporary_file:'临时文件保留至重启'} as Record<string,string>)[String(result.recovery.state)] || String(result.recovery.state)} · {String(result.recovery.reason || result.recovery.error || '')}</p><Evidence value={result.recovery} label="恢复详情" /><h4>结果图片</h4>{round.controller_result?.state!=='finished' && <p>本轮完成后，图片和原始帧将自动同步到 Hub。</p>}<div style={{display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(240px,1fr))',gap:16,alignItems:'start'}}>{result.files.filter(f=>f.role==='image' && round.controller_result?.state==='finished').map((file,i)=>{const url=`${apiBase()}/api/qa/test-rounds/${round.id}/cases/${code}/artifact?name=${encodeURIComponent(String(file.path))}`;return <figure key={i} style={{margin:0}}><a href={url} target="_blank" rel="noreferrer"><img src={url} alt={`${String(file.label)} ${String(file.stream)}`} loading="lazy" style={{width:'100%',maxHeight:320,objectFit:'contain',background:'#15251f'}} /></a><figcaption>{String(file.label)} · {String(file.stream)} · {String(file.path).split("/").pop()} · <a href={url} download>下载图片</a></figcaption></figure>;})}</div><h4>测试文件</h4>{result.files.length ? result.files.map((file,i)=><div key={i}><Facts items={[["文件",file.path],["用途",file.role],["字节数",file.size_bytes],["SHA-256",file.sha256]]} />{round.controller_result?.state==='finished' && <a href={`${apiBase()}/api/qa/test-rounds/${round.id}/cases/${code}/artifact?name=${encodeURIComponent(String(file.path))}`} download>下载文件</a>}</div>):<p>此用例无需测试文件。</p>}{result.files.some(f=>f.role==='input') && <><button onClick={()=>void downloadFile('input-file')}>下载测试输入</button><button onClick={()=>void downloadFile('output-file')}>下载回读文件</button></>}</> : kind==='protocol' ? <><p>记录真实 ioctl 收发。SET_CUR 的前 4 字节为 little-endian command；其余为实际 payload（含填充）。GET 保留实际原始响应，不将未识别字节伪装为解码结果。</p>{content.split('\n').filter(Boolean).map((line,i)=>{try{const entry=JSON.parse(line) as Record<string,unknown>;return <details key={i}><summary>{String(entry.step)} · {String(entry.direction)} · Unit {String(entry.unit)} / Selector {String(entry.selector)} · Query {String(entry.query)} · rc={String(entry.return_code)}</summary><p>Command：0x{Number(entry.command_u32_le).toString(16)} · {String(entry.elapsed_us)} μs</p><pre className="wb-build-log">{String(entry.hex)}</pre>{entry.direction==='TX' && <p style={{overflowWrap:'anywhere'}}>Payload：{String(entry.hex).slice(8)}</p>}<Evidence value={entry} label="完整传输记录" /></details>;}catch{return <pre key={i}>{line}</pre>;}})}</> : <pre className="wb-build-log">{content || '等待证据…'}</pre>}</section>;
}


type CaseOutcome = {id:string; code?:string; state?:string; outcome?:string; assertions_passed?:number; assertions_total?:number; recovery?:{state?:string}};
const outcomeLabels: Record<string,string> = {passed:'通过',failed:'不通过',error:'执行异常',blocked:'阻塞',skipped:'跳过',running:'执行中',pending:'未测',interrupted:'已中断',sync_error:'状态待同步'};
function roundSyncError(round:TestRound) {
  return round.status==='bench_running' && Boolean(round.controller_result?.sync_status==='error' || round.controller_result?.transport_error);
}
function roundLabel(round:TestRound) {
  return roundSyncError(round) ? '结果同步异常' : roundStatus[round.status] || round.status;
}
function resultState(result?:CaseOutcome) {
  return result?.outcome || (result?.state === 'finished' ? 'error' : result?.state === 'running' ? 'running' : ['blocked','skipped','interrupted'].includes(result?.state || '') ? result!.state! : 'pending');
}
function commitComment(round:TestRound) {
  if(isPrebuiltRound(round))return firmwareIdentity(round).value||'已保存镜像指纹';
  const frozen = round.product_snapshot.test_point as {subject?:string} | undefined;
  return frozen?.subject || (round.test_point?.commit_sha === round.commit_sha ? round.test_point.subject : '') || '历史记录未保存提交说明';
}
function RoundResults({round,onEvidence,onChanged}:{round:TestRound;onEvidence:(code:string)=>void;onChanged:()=>Promise<void>}) {
  const [filter,setFilter] = useState('all');
  const [editing,setEditing] = useState<string|null>(null);
  const syncError = roundSyncError(round);
  const stateOf = (result:CaseOutcome|undefined, plan:Record<string,unknown>) => {
    const state = round.case_classifications?.[String(plan.code)]?.outcome || resultState(result);
    return syncError && ['running','pending'].includes(state) ? 'sync_error' : state;
  };
  const results = (round.controller_result?.cases || []) as CaseOutcome[];
  const rows = round.test_case_snapshot.map(plan => ({plan, result:results.find(r=>r.id===plan.id || (!!r.code && r.code===plan.code))}));
  const count = (state:string) => rows.filter(row=>stateOf(row.result,row.plan)===state).length;
  const passed=count('passed'), failed=count('failed'), errors=count('error');
  const pending=rows.length-passed-failed-errors;
  const visible=rows.filter(row=>filter==='all' || (filter==='attention' ? ['failed','error','blocked','interrupted','sync_error'].includes(stateOf(row.result,row.plan)) : filter==='unfinished' ? !['passed','failed','error'].includes(stateOf(row.result,row.plan)) : stateOf(row.result,row.plan)===filter));
  const hasManual = Object.values(round.case_classifications || {}).some(item=>item.manual);
  const isRelease = round.origin === 'release';
  const milestones = [
    [round.product_snapshot.developer_mode ? '使用当前固件' : isPrebuiltRound(round) ? '导入镜像校验' : isRelease ? '候选镜像' : '编译与镜像',round.product_snapshot.developer_mode ? 'skipped' : isPrebuiltRound(round) ? firmwareSource(round).validation?.passed===true?'passed':'pending' : round.build_result?.passed === true ? 'passed' : round.build_result?.passed === false ? 'failed' : round.status==='building' ? 'running' : 'pending'],
    ['测试台交付',round.delivery_result?.passed === true ? 'passed' : round.delivery_result?.passed === false ? 'failed' : round.status==='transferring' ? 'running' : 'pending'],
    [round.product_snapshot.developer_mode ? '当前固件身份确认' : '烧录后启动确认',(round.product_snapshot.developer_mode ? round.controller_result?.identity_verified : round.controller_result?.boot_verified) === true ? 'passed' : 'pending'],
    ['用例执行',syncError ? 'sync_error' : round.status==='test_complete' ? 'passed' : round.status==='test_failed' ? 'failed' : round.status==='blocked' ? 'blocked' : results.some(r=>r.state==='running') ? 'running' : 'pending'],
  ];
  if (!round.product_snapshot.execute_test_cases) return null;
  return <section className="panel wb-results">
    <header><div><span className="panel-label">TEST RESULTS</span><h3>用例结果</h3></div><span className={`wb-outcome result-${round.status==='test_complete'?'passed':round.status==='test_failed'?'failed':'pending'}`}>{roundLabel(round)}</span></header>
    {hasManual && <p className="tw-warning">复核后分类：{outcomeLabels[round.case_classification_verdict] || '未测'}。以下统计包含人工修正，原始执行结论保留。</p>}<p className="wb-result-subtitle">{roundSource(round)}</p>
    <div className="wb-result-counts">{[['✓','通过',passed,'passed'],['×','不通过',failed,'failed'],['!','执行异常',errors,'error'],['○','未完成 / 跳过',pending,'pending']].map(([icon,label,value,state])=><div key={String(state)} className={`result-${state}`}><span><b aria-hidden="true">{icon}</b> {label}</span><strong>{value}</strong></div>)}</div>
    <p>本轮计划 {rows.length} 项 · 有效判定 {passed+failed}/{rows.length} · 未测 {count('pending')} · 运行中 {count('running')}{syncError && ` · 状态待同步 ${count('sync_error')}`} · 阻塞 {count('blocked')} · 跳过 {count('skipped')} · 中断 {count('interrupted')}</p>
    {!round.product_snapshot.execute_test_cases && <p className="tw-warning">本轮未启用用例执行；交付或烧录成功不能作为测试通过。</p>}
    <nav className="wb-result-filters" aria-label="筛选用例结果">{[['all','全部'],['attention','失败 / 异常'],['unfinished','未完成 / 跳过'],['passed','通过']].map(([value,label])=><button type="button" key={value} aria-pressed={filter===value} onClick={()=>setFilter(value)}>{label}</button>)}</nav>
    <div className="wb-result-rows">{visible.map(({plan,result})=>{const state=stateOf(result,plan);const classification=round.case_classifications?.[String(plan.code)];return <article key={String(plan.id)}><span className={`wb-outcome result-${state}`}>{outcomeLabels[state] || state}</span><div><strong>{String(plan.name || plan.code)}</strong><small>{String(plan.code)}{result?.assertions_total!==undefined ? ` · 断言 ${result.assertions_passed ?? 0}/${result.assertions_total}` : ' · 尚无断言结果'}</small>{classification?.manual && <small>人工修正 · 自动结果：{outcomeLabels[classification.automatic_outcome || ''] || classification.automatic_outcome}</small>}{classification?.history.length ? <details><summary>修改记录 · {classification.history.length}</summary>{classification.history.map((h,i)=><p key={i}>{formatDate(h.changed_at)} · {h.operator} · {h.outcome ? outcomeLabels[h.outcome] : '恢复自动分类'}：{h.reason}</p>)}</details>:null}{result?.recovery?.state==='failed' && <small>恢复失败，请先处理设备状态</small>}</div><div className="wb-case-actions"><button type="button" disabled={!result?.code} onClick={()=>result?.code && onEvidence(result.code)}>查看证据 →</button>{classification?.executed && ['test_complete','test_failed','bench_failed','blocked'].includes(round.status) && <button type="button" onClick={()=>setEditing(String(plan.code))}>修改分类</button>}</div></article>;})}{!visible.length && <p>此筛选下没有用例。</p>}</div>
    {editing && <ClassificationEditor key={editing} round={round} code={editing} onClose={()=>setEditing(null)} onChanged={onChanged} />}
    <h4>执行进度</h4><ol className="wb-milestones">{milestones.map(([name,state])=><li key={name} className={`result-${state}`}><span aria-hidden="true">{state==='passed'?'✓':state==='failed'?'×':state==='running'?'◷':'○'}</span><div><strong>{name}</strong><small>{state==='pending'?'待确认':outcomeLabels[state]}</small></div></li>)}</ol>
    <small>里程碑来自服务端镜像校验、交付和 Controller 证据；逐项测试结论以上方用例为准。</small>
  </section>;
}


function ClassificationEditor({round,code,onClose,onChanged}:{round:TestRound;code:string;onClose:()=>void;onChanged:()=>Promise<void>}) {
  const [busy,setBusy]=useState(false); const [error,setError]=useState('');
  const current=round.case_classifications[code];
  const [revision]=useState(current.revision);
  async function save(event:FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form=new FormData(event.currentTarget); setBusy(true);setError('');
    try {await request(`/api/qa/test-rounds/${round.id}/cases/${code}/classification`,{method:'POST',body:JSON.stringify({outcome:form.get('outcome') || null,operator:form.get('operator'),reason:form.get('reason'),revision})});await onChanged();onClose();}
    catch(cause){setError(String(cause));}finally{setBusy(false);}
  }
  return <form className="wb-classification-editor" onSubmit={save}><h4>{code} · 修改结果分类</h4><p>修改影响本轮用例的分类、统计和筛选；原始断言与自动执行记录保留。</p><label>分类<select name="outcome" defaultValue={current.outcome==='failed'?'failed':'passed'} disabled={busy}><option value="passed">通过</option><option value="failed">不通过</option><option value="">恢复自动结果</option></select></label><label>修改人（手工填写）<input name="operator" required maxLength={100} disabled={busy} /></label><label>修改原因<textarea name="reason" required maxLength={1000} disabled={busy} /></label>{error && <p role="alert">{error}</p>}<div className="wb-case-actions"><button disabled={busy} type="submit">{busy?'保存中…':'保存分类'}</button><button disabled={busy} type="button" onClick={onClose}>取消</button></div></form>;
}

function PendingScenarios({implementedCodes}:{implementedCodes:Set<string>}) {
  const pending=scenarioCatalog.scenarios.flatMap(group=>group.cases.filter(item=>!implementedCodes.has(item.code)));
  if(!pending.length)return null;
  return <details className="panel wb-pending-scenarios">
    <summary>待接入场景 · {pending.length} 项</summary>
    <p>这里只列出尚未接入执行的场景。</p>
    <div className="wb-pending-grid">{pending.map(item=><article key={item.code}>
      <h4>{item.name}</h4><small>{item.code}</small><p>{item.assertion}</p>
    </article>)}</div>
  </details>;
}

type PatchsetLookup = {
  change_number: number; project: string; branch: string; subject: string; owner: string; status: string;
  selected_patchset: number; patchsets: {patchset_number: number; commit_sha: string; ref: string; submitted_at: string | null}[];
};

function GerritPatchsetFields({product, onReady, busy}: {product: Product; onReady: (ready: boolean) => void; busy: boolean}) {
  const [url, setUrl] = useState('');
  const [result, setResult] = useState<PatchsetLookup | null>(null);
  const [selected, setSelected] = useState(0);
  const [lookupError, setLookupError] = useState('');
  const [loading, setLoading] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    if (!url.trim()) return () => controller.abort();
    const timer = window.setTimeout(async () => {
      setLoading(true);
      try {
        const data = await request<PatchsetLookup>(`/api/qa/products/${product.id}/patchset-lookup`, {method:'POST', body:JSON.stringify({url:url.trim()}), signal:controller.signal});
        if (!controller.signal.aborted) { setResult(data); setSelected(data.selected_patchset); onReady(true); }
      } catch (cause) {
        if (!controller.signal.aborted) setLookupError(cause instanceof Error ? cause.message : String(cause));
      } finally { if (!controller.signal.aborted) setLoading(false); }
    }, 500);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [url, product.id, retry, onReady]);
  const patch = result?.patchsets.find(item => item.patchset_number === selected);
  return <>
    <p>锁定 Project：{product.gerrit_project}</p>
    <label>Gerrit Change 链接<input type="url" required value={url} disabled={busy} placeholder="https://10.10.10.29/c/linux/app/struct_light/+/316200" onChange={event => {setUrl(event.target.value); setResult(null); setLookupError(''); setLoading(false); onReady(false);}} /></label>
    <p role="status" aria-live="polite">{loading ? '正在查询 Gerrit，请稍候…' : result ? '查询完成，请确认 Patchset。' : '粘贴链接后自动查询，默认选择最新 Patchset；也支持带 Patchset 序号的链接。'}</p>
    {lookupError && <div className="tw-warning" role="alert">{lookupError}<button type="button" onClick={() => {setLookupError(''); setRetry(value => value + 1);}}>重试查询</button></div>}
    {result && patch && <>
      <Facts items={[["Change",result.change_number],["状态",result.status],["分支",result.branch],["提交者",result.owner]]} />
      <label>Patchset<select name="patchset_number" value={selected} disabled={busy} onChange={event => setSelected(Number(event.target.value))}>{result.patchsets.map((item,index) => <option key={item.patchset_number} value={item.patchset_number}>PS{item.patchset_number}{index===0?'（最新）':''} · {item.commit_sha.slice(0,12)}</option>)}</select></label>
      <label>完整 Commit SHA<input name="commit_sha" value={patch.commit_sha} readOnly /></label>
      <label>测试点说明<input key={result.change_number} name="subject" defaultValue={result.subject} required disabled={busy} /></label>
      <input type="hidden" name="change_number" value={result.change_number} />
      <input type="hidden" name="branch" value={result.branch} />
      <input type="hidden" name="owner" value={result.owner} />
      <input type="hidden" name="submitted_at" value={patch.submitted_at || ''} />
      <p>提交时间（Patchset 上传）：{formatDate(patch.submitted_at)}</p>
      <p>保存时服务端会再次核验所选 Ref 与 Commit。</p>
    </>}
  </>;
}

type Readiness = {bench_id:string; passed:boolean; checked_at:string; checks:{name:string;passed:boolean;detail:string}[]};
function BenchReadiness({benchId, full=true, onResult}:{benchId:string;full?:boolean;onResult?:(result:Readiness|null)=>void}) {
  const [result,setResult]=useState<Readiness|null>(null);
  const [error,setError]=useState('');
  const [revision,setRevision]=useState(0);
  useEffect(()=>{
    const controller=new AbortController();
    request<Readiness>(`/api/qa/benches/${benchId}/readiness?execution=${full?'full':'delivery'}`,{signal:controller.signal})
      .then(value=>{if(!controller.signal.aborted){setResult(value);onResult?.(value);}})
      .catch(cause=>{if(!controller.signal.aborted)setError(String(cause));});
    return ()=>controller.abort();
  },[benchId,full,revision,onResult]);
  return <div className="wb-readiness">
    <p role="status">{error || (!result?'正在检查测试台…':result.passed?'测试台检查通过':'测试台未就绪，请处理以下问题后重新检查。')}</p>
    {result && <><details open={!result.passed}><summary>检查详情</summary><ul>{result.checks.map(check=><li key={check.name}>{check.passed?'✓':'✕'} {check.name}：{check.detail}</li>)}</ul><small>检查时间：{formatDate(result.checked_at)}</small></details></>}

    <button type="button" disabled={!result && !error} onClick={()=>{setResult(null);setError('');onResult?.(null);setRevision(value=>value+1);}}>重新检查测试台</button>
  </div>;
}

export default function QaWorkbench({ onManageRepositories }: { onManageRepositories: () => void }) {
  const [screen, setScreen] = useState<Screen>('start');
  const [locationReady,setLocationReady]=useState(false);
  const [benchReady,setBenchReady]=useState<Readiness|null>(null);
  const [notice,setNotice]=useState('');
  const [tab, setTab] = useState<ProductTab>('history');
  const [products, setProducts] = useState<Product[]>([]);
  const [sdks, setSdks] = useState<SdkRepository[]>([]);
  const [cases, setCases] = useState<TestCase[]>([]);
  const [benches, setBenches] = useState<TestBench[]>([]);
  const [rounds, setRounds] = useState<TestRound[]>([]);
  const [points, setPoints] = useState<TestPoint[]>([]);
  const [productId, setProductId] = useState('');
  const [roundId, setRoundId] = useState('');
  const [sdkId, setSdkId] = useState('');
  const [applications, setApplications] = useState<Application[]>([]);
  const [applicationId, setApplicationId] = useState('');
  const [manifest, setManifest] = useState<ManifestSnapshot | null>(null);
  const [board, setBoard] = useState('');
  const [boardChoices, setBoardChoices] = useState<BoardChoice[]>([]);
  const [pointId, setPointId] = useState('');
  const [benchId, setBenchId] = useState('');
  const [mode, setMode] = useState<TestMode>('quick');
  const developerMode = mode === 'quick';
  const flashFirmware = mode === 'full';
  const executeBasic = mode !== 'delivery';
  const [historyOrigin, setHistoryOrigin] = useState('all');
  const [historyState, setHistoryState] = useState('all');
  const [historyProduct, setHistoryProduct] = useState('all');
  const [basicDefinitions,setBasicDefinitions]=useState<BasicDefinition[]>([]);
  const [specialDefinitions,setSpecialDefinitions]=useState<SpecialDefinition[]>([]);
  const [definitionsLoaded,setDefinitionsLoaded]=useState(false);
  const [specialRuns,setSpecialRuns]=useState<SpecialRun[]>([]);
  const [releasePlanId,setReleasePlanId]=useState('');
  const [releaseImportProductId,setReleaseImportProductId]=useState('');
  const [evidenceCase,setEvidenceCase]=useState('');
  const [deviceSerial, setDeviceSerial] = useState('');
  const [devices, setDevices] = useState<{serial:string; mode:string;version?:string}[]>([]);
  const [deviceScan, setDeviceScan] = useState<'idle'|'loading'|'done'|'error'>('idle');
  const [deviceError, setDeviceError] = useState('');
  const [deviceScanRevision, setDeviceScanRevision] = useState(0);
  const [selectedCases, setSelectedCases] = useState<string[]>([]);
  const [quickExecution,setQuickExecution]=useState<'automatic'|'gated'>('automatic');
  const [quickScenarioCode,setQuickScenarioCode]=useState('');
  const [quickOperator,setQuickOperator]=useState('');
  const launchInFlight=useRef(false);
  const [parameters, setParameters] = useState<Record<string, string>>({});
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [busy, setBusy] = useState(false);
  const [pointReady, setPointReady] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [updated, setUpdated] = useState<string | null>(null);
  const orderedCases = [...cases].sort((a,b)=>{
    const order=(code:string)=>{const i=basicDefinitions.findIndex(d=>d.id===code);return i<0?999:i;};
    return order(a.code)-order(b.code) || a.code.localeCompare(b.code);
  });
  const manualGate=developerMode&&quickExecution==='gated';
  const availableAutomaticCases=orderedCases.filter(c=>basicDefinitions.some(d=>d.id===c.code) && (!developerMode || ((c.code.startsWith('SWITCH-')||c.code.startsWith('INFO-')||c.code.startsWith('FILE-')||c.code.startsWith('FACTORY-')||c.code==='STREAM-RGB-DEPTH')&&c.code!=='SWITCH-ROTATE-CONTROL')));
  const quickGateDefinitions=specialDefinitions.filter(definition=>cases.some(c=>c.code===definition.code));
  const selectedGate=quickGateDefinitions.find(definition=>definition.code===quickScenarioCode);
  const selectedCount=manualGate?(selectedGate?1:0):selectedCases.length;
  useEffect(()=>{
    const timer=window.setTimeout(()=>{
    const params=new URLSearchParams(window.location.search);
    const target=params.get('qa');
    if(target && ['start','special','products','rounds','cases','benches','releases','schedules','product-detail','round-detail'].includes(target)) setScreen(target as Screen);
    setProductId(params.get('product') || '');setRoundId(params.get('round') || '');setReleasePlanId(params.get('validation_plan') || '');
    if(params.get('tab')==='baseline')setTab('baseline');
    setLocationReady(true);
    },0);
    return ()=>window.clearTimeout(timer);
  },[]);
  useEffect(()=>{
    if(!locationReady)return;
    const url=new URL(window.location.href);
    url.searchParams.set('qa',screen==='round-wizard'?'start':screen==='product-wizard'?'products':screen);
    for(const key of ['product','round','tab'])url.searchParams.delete(key);
    if(screen.startsWith('product') || screen==='round-wizard' || screen==='start'){
      if(productId)url.searchParams.set('product',productId);
      if(tab==='baseline')url.searchParams.set('tab',tab);
    }
    if(screen==='round-detail')url.searchParams.set('round',roundId);
    if(screen==='releases') { if(releasePlanId)url.searchParams.set('validation_plan',releasePlanId); else url.searchParams.delete('validation_plan'); }
    window.history.replaceState(null,'',url);
  },[locationReady,screen,productId,roundId,tab,releasePlanId]);
  const product = products.find(item => item.id === productId) || (products.length === 1 ? products[0] : undefined);
  const isRv1126b = Boolean(product?.board_config.toLowerCase().includes('rv1126b'));
  const round = rounds.find(item => item.id === roundId);
  const sdk = sdks.find(item => item.id === sdkId);
  const application = applications.find(item => item.id === applicationId);
  const sdkBusy = Boolean(product && (rounds.some(r=>r.product.sdk_repository_id===product.sdk_repository_id && ['created','checking_out','building','packaging','transferring','verifying'].includes(r.status)) || products.some(p=>p.sdk_repository_id===product.sdk_repository_id && p.status==='verifying')));
  const productRounds = rounds.filter(item => item.product_id === product?.id);
  const activeView: View = screen==='round-wizard' ? 'start' : screen.startsWith('round') ? 'rounds' : screen.startsWith('product') ? 'products' : screen as View;

  const refresh = useCallback(async () => {
    const [p, s, c, b, r] = await Promise.all([
      request<Product[]>('/api/qa/products'), request<SdkRepository[]>('/api/qa/sdk-repositories'),
      request<TestCase[]>('/api/qa/test-cases'), request<TestBench[]>('/api/qa/benches'), request<TestRound[]>('/api/qa/test-rounds'),
    ]);
    setProducts(p); setSdks(s); setCases(c.filter(item => item.enabled)); setBenches(b); setRounds(r);
    setUpdated(new Date().toISOString()); setLoading(false);
  }, []);
  useEffect(() => {
    let alive = true;
    const poll = async () => { try { await refresh(); } catch (cause) { if (alive) { setError(String(cause)); setLoading(false); } } };
    void poll(); const timer = window.setInterval(() => void poll(), 2000);
    return () => { alive = false; window.clearInterval(timer); };
  }, [refresh]);
  useEffect(()=>{
    let alive=true;
    const refreshDefinitions=async()=>{
      try {
        const [definitions,specials,history]=await Promise.all([
          request<BasicDefinition[]>('/api/qa/basic-case-definitions'),
          request<SpecialDefinition[]>('/api/qa/special-scenarios'),
          request<SpecialRun[]>('/api/qa/special-runs'),
        ]);
        if(alive){setBasicDefinitions(definitions);setSpecialDefinitions(specials);setSpecialRuns(history);setDefinitionsLoaded(true);}
      } catch(cause) { if(alive)setError(String(cause)); }
    };
    void refreshDefinitions();
    const timer=window.setInterval(()=>void refreshDefinitions(),10000);
    window.addEventListener('focus',refreshDefinitions);
    return ()=>{alive=false;window.clearInterval(timer);window.removeEventListener('focus',refreshDefinitions);};
  },[]);
  useEffect(() => {
    if (!sdkId) return;
    let alive = true;
    Promise.all([request<ManifestSnapshot>(`/api/qa/sdk-repositories/${sdkId}/manifest`), request<Application[]>(`/api/qa/sdk-repositories/${sdkId}/applications`), request<BoardChoice[]>(`/api/qa/sdk-repositories/${sdkId}/board-configs`)])
      .then(([m, a, b]) => { if (alive) { setManifest(m); setApplications(a); setBoardChoices(b); } })
      .catch(cause => { if (alive) setError(String(cause)); });
    return () => { alive = false; };
  }, [sdkId]);
  useEffect(() => {
    if (!product?.id) return;
    let alive = true;
    request<TestPoint[]>(`/api/qa/products/${product.id}/test-points`).then(result => { if (alive) setPoints(result); }).catch(cause => { if (alive) setError(String(cause)); });
    return () => { alive = false; };
  }, [product?.id]);

  useEffect(() => {
    if (screen !== 'round-wizard' || !benchId || (!flashFirmware && !developerMode)) return;
    const controller = new AbortController();
    const detect = async () => {
      setDeviceScan('loading'); setDeviceError(''); setDevices([]); setDeviceSerial('');
      try {
        const found = await request<{serial:string; mode:string;version?:string}[]>(`/api/qa/benches/${benchId}/devices?chip=${isRv1126b ? "RV1126B" : "RK3572"}`, {signal:controller.signal});
        if (controller.signal.aborted) return;
        setDevices(found); setDeviceSerial(found.length===1 ? found[0].serial : '');
        setDeviceScan('done');
      } catch (cause) {
        if (controller.signal.aborted) return;
        setDeviceScan('error'); setDeviceError(cause instanceof Error ? cause.message : String(cause));
      }
    };
    void detect();
    return () => controller.abort();
  }, [screen, benchId, flashFirmware, developerMode, deviceScanRevision, isRv1126b]);

  async function perform(work: () => Promise<void>) {
    setBusy(true); setError(null);
    try { await work(); } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  }
  function openProduct(item: Product) { if (item.id !== productId) setPoints([]); setProductId(item.id); setScreen('product-detail'); setTab('history'); }
  function openRound(item: TestRound) { setNotice('');setError(null);setEvidenceCase(''); setRoundId(item.id); setScreen('round-detail'); }
  function newRound(nextMode: TestMode) {
    if (!product) return;
    setProductId(product.id); setMode(nextMode); setNotice(''); setError(null); setBenchReady(null);
    setPointId('');
    const available = benches.filter(item=>item.enabled);
    setBenchId(available.length===1 ? available[0].id : '');
    setDeviceSerial(''); setDevices([]); setDeviceScan('idle'); setDeviceError('');
    setSelectedCases([]);setQuickExecution('automatic');setQuickScenarioCode('');setQuickOperator('');setParameters({});setScreen('round-wizard');
  }
  function newProduct() { setSdkId(''); setManifest(null); setApplications([]); setApplicationId(''); setBoard('');setBoardChoices([]); setScreen('product-wizard'); }
  async function verifyBaseline() {
    if (!product) return;
    await perform(async () => { try { await request(`/api/qa/products/${product.id}/verify-baseline`, {method:'POST'}); } finally { await refresh(); } });
  }
  function saveProduct(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const data = new FormData(event.currentTarget);
    void perform(async () => {
      const created = await request<Product>('/api/qa/products', {method:'POST', body:JSON.stringify({
        name:data.get('name'), code:data.get('code'), sdk_repository_id:sdkId, tested_repository_id:applicationId,
        board_config:board,
      })});
      await refresh(); openProduct(created); setTab('baseline');
    });
  }
  function createRound(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!product||launchInFlight.current) return;
    launchInFlight.current=true;
    void perform(async () => {
      if(manualGate){
        if(!selectedGate||!quickOperator.trim()||!benchId||!deviceSerial)throw new Error('请选择人工门控场景、在线模组并填写操作者');
        const created=await request<SpecialRun>('/api/qa/special-runs',{method:'POST',body:JSON.stringify({scenario_code:selectedGate.code,product_id:product.id,bench_id:benchId,device_serial:deviceSerial,operator:quickOperator.trim()})});
        setSpecialRuns(current=>[created,...current.filter(run=>run.id!==created.id)]);
        openSpecial(created.id);
        return;
      }
      const parsed: Record<string, Record<string, unknown>> = {};
      for (const id of executeBasic ? selectedCases : []) {
        const value = JSON.parse(parameters[id]?.trim() || '{}');
        if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error('Test Case 参数必须为 JSON 对象');
        parsed[id] = value;
      }
      const created = await request<TestRound>('/api/qa/test-rounds', {method:'POST', body:JSON.stringify({product_id:product.id, test_point_id:developerMode ? null : pointId, developer_mode:developerMode, bench_id:benchId, test_case_ids:[...(executeBasic ? selectedCases : [])].sort((a,b)=>basicDefinitions.findIndex(d=>d.id===cases.find(c=>c.id===a)?.code)-basicDefinitions.findIndex(d=>d.id===cases.find(c=>c.id===b)?.code)), test_case_parameters:parsed, execute_test_cases:executeBasic, flash_firmware:flashFirmware, device_serial:deviceSerial, auto_start:true})});
      await refresh(); openRound(created);
    }).finally(()=>{launchInFlight.current=false;});
  }
  function saveDialog(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const data = new FormData(event.currentTarget);
    void perform(async () => {
      if (dialog === 'point' && product) {
        if (!pointReady) throw new Error('请先查询 Gerrit 并选择 Patchset');
        const change = Number(data.get('change_number')); const patchset = Number(data.get('patchset_number'));
        const saved = await request<TestPoint>(`/api/qa/products/${product.id}/test-points`, {method:'POST', body:JSON.stringify({change_number:change, patchset_number:patchset, ref:`refs/changes/${String(change % 100).padStart(2,'0')}/${change}/${patchset}`, commit_sha:data.get('commit_sha'), subject:data.get('subject'), branch:data.get('branch'), owner:data.get('owner'), submitted_at:data.get('submitted_at') || null, ready:true})});
        setPoints(await request<TestPoint[]>(`/api/qa/products/${product.id}/test-points`));
        setPointId(saved.id);setNotice('Patch Set 已核验并选中；重复登记会复用已有测试点。');
      } else if (dialog === 'bench') {
        await request('/api/qa/benches', {method:'POST', body:JSON.stringify({name:data.get('name'), host:data.get('host'), port:Number(data.get('port')), user:data.get('user'), remote_root:data.get('remote_root')})});
      }
      setDialog(null); await refresh();
    });
  }
  async function downloadReport(item: TestRound) {
    await perform(async()=>{
      const report=await request<Record<string,unknown>>(`/api/qa/test-rounds/${item.id}/report`);
      const url=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:'application/json'}));
      const link=document.createElement('a');link.href=url;link.download=`TEST-${String(item.display_number).padStart(6,'0')}.json`;
      document.body.appendChild(link);link.click();link.remove();
      window.setTimeout(()=>URL.revokeObjectURL(url),30000);
    });
  }
  function openSpecial(runId?:string,code?:string,planId?:string) {
    const url=new URL(window.location.href);
    for(const key of ['special_run','special_case','validation_plan'])url.searchParams.delete(key);
    if(runId)url.searchParams.set('special_run',runId);
    if(code)url.searchParams.set('special_case',code);
    if(planId)url.searchParams.set('validation_plan',planId);
    window.history.replaceState(null,'',url);
    setScreen('special');setError(null);
  }
  function openPlan(id:string) {setReleaseImportProductId('');setReleasePlanId(id);setScreen('releases');setError(null);}
  function openPrebuiltImport() {if(!product)return;setReleasePlanId('');setReleaseImportProductId(product.id);setScreen('releases');setError(null);setNotice('');}
  function roundList(items: TestRound[]) {
    return items.length ? <div className="wb-history">{items.map(item => <button key={item.id} onClick={() => openRound(item)}><span><strong>TEST-{String(item.display_number).padStart(6,'0')}</strong><small>{formatDate(item.created_at)}</small></span><span>{item.product.name}<small>{originLabels[item.origin||'manual']} · {roundSource(item)}</small></span><StatusPill status={roundSyncError(item)?'blocked':item.status} label={roundLabel(item)} /></button>)}</div> : <p className="quiet-row">暂无符合条件的测试记录。</p>;
  }

  return <section className="test-workbench wb-workspace" onInvalidCapture={event=>{
    let ancestor=(event.target as HTMLElement).parentElement;
    while(ancestor && ancestor!==event.currentTarget){
      if(ancestor instanceof HTMLDetailsElement)ancestor.open=true;
      ancestor=ancestor.parentElement;
    }
  }}>
    <nav className="tw-nav wb-main-nav" aria-label="测试平台导航">
      {([['start','开始测试'],['special','特殊场景'],['rounds','测试记录'],['releases','发布验证'],['schedules','定时回归'],['products','配置管理']] as Array<[View,string]>).map(([key,label]) => <button key={key} aria-current={(key==='products' ? ['products','cases','benches'].includes(activeView) : activeView===key) ? 'page' : undefined} className={(key==='products' ? ['products','cases','benches'].includes(activeView) : activeView===key) ? 'active' : ''} onClick={()=>{setReleaseImportProductId('');setScreen(key);setError(null);setNotice('');}}>{label}</button>)}
      <span role="status">{loading ? '正在连接…' : `更新于 ${formatDate(updated)}`}</span>
    </nav>
    {['products','cases','benches'].includes(activeView) && <nav className="wb-settings-nav" aria-label="配置管理">{([['products','产品与基线'],['cases','用例库'],['benches','测试台节点']] as Array<[View,string]>).map(([key,label])=><button key={key} aria-current={activeView===key?'page':undefined} onClick={()=>setScreen(key)}>{label}</button>)}</nav>}
    {notice && <p role="status" className="wb-receipt">{notice}</p>}
    {error && <div className="error-banner" role="alert"><span>{error}</span><button aria-label="关闭错误" onClick={() => setError(null)}>×</button></div>}
    {screen==='special' && <SpecialScenarios products={products} benches={benches} api={request} apiBase={apiBase()} initialProductId={product?.id} onOpenPlan={openPlan} onOpenRound={id=>{setRoundId(id);setScreen('round-detail');void refresh();}}/>}
    {(screen==='releases'||screen==='schedules')&&<PlatformWorkbench key={screen+releaseImportProductId} mode={screen} initialPrebuiltProductId={releaseImportProductId||undefined} initialPlanId={releasePlanId} onSelectPlan={setReleasePlanId} openSpecial={openSpecial} specialDefinitions={specialDefinitions} products={products} benches={benches} cases={orderedCases} rounds={rounds} executable={basicDefinitions.map(d=>d.id)} api={request} apiBase={apiBase()} openRound={id=>{const found=rounds.find(r=>r.id===id);if(found)openRound(found);else{setRoundId(id);setScreen('round-detail');void refresh();}}} renderParameters={(code,value,onChange)=>code==='STREAM-RGB-DEPTH'?<StreamParameterForm value={value} onChange={onChange}/>:caseParameterFields[code]?<CaseParameterForm code={code} value={value} onChange={onChange}/>:<p>此用例使用固定配置，无可调整参数。</p>}/>}
    {screen === 'start' && <>
      <div className="tw-heading"><div><span className="panel-label">TEST WORKBENCH</span><h2>开始测试</h2><p>选择固件来源，配置本次需要验证的项目。</p></div></div>
      <section className="panel wb-start-product"><label>测试产品<select value={product?.id || ''} onChange={event=>{setProductId(event.target.value);setPoints([]);setPointId('');}}><option value="">选择产品</option>{products.map(item=><option key={item.id} value={item.id}>{item.name}</option>)}</select></label>{product && <div><StatusPill status={product.status} label={productStatus[product.status]}/><button type="button" onClick={()=>openProduct(product)}>产品配置 →</button></div>}</section>
      {!products.length && !loading && <p className="panel">尚未配置产品。<button onClick={()=>setScreen('products')}>前往产品与基线</button></p>}
      <div className="wb-start-modes">
        <button className="panel wb-mode-card" disabled={!product || busy} onClick={()=>newRound('quick')}><span className="panel-label">当前模组</span><h3>测试当前固件</h3><p>直接验证在线模组，省去编译和烧录。</p><small>选择设备 → 选择用例 → 执行测试</small><strong>配置测试 →</strong></button>
        <button className="panel wb-mode-card" disabled={!product || !isRv1126b || busy} onClick={openPrebuiltImport}><span className="panel-label">175 本地镜像</span><h3>已有镜像烧录测试</h3><p>使用 SDK 中已编译好的镜像，保存固件后选择测试项目。</p><small>{product&&!isRv1126b?'当前支持 RV1126B 产品':'导入镜像 → 创建清单 → 烧录并测试'}</small><strong>选择已有镜像 →</strong></button>
        <button className="panel wb-mode-card" disabled={!product || product.status!=='ready' || busy} onClick={()=>newRound('full')}><span className="panel-label">指定提交</span><h3>编译烧录测试</h3><p>验证 Gerrit 提交，构建并烧录后执行用例。</p><small>{product && product.status!=='ready' ? '请先在产品配置中完成基线验证' : '选择版本 → 选择设备与用例 → 完整验证'}</small><strong>配置测试 →</strong></button>
      </div>
      <section className="panel wb-delivery-shortcut"><div><strong>特殊场景 · 人工门控</strong><p>SN 断电持久化、日志压力、老化串口、异常 OTA；按步骤接线、确认并记录证据。</p></div><button onClick={()=>setScreen('special')}>进入特殊场景 →</button></section>
      <section className="panel wb-delivery-shortcut"><div><strong>仅构建交付</strong><p>从指定提交生成镜像并交付到测试台，供后续使用。</p></div><button disabled={!product || product.status!=='ready' || busy} onClick={()=>newRound('delivery')}>配置构建 →</button></section>
      <section className="panel"><header><h3>最近测试</h3><button onClick={()=>setScreen('rounds')}>全部记录 →</button></header>{roundList(rounds.filter(item=>!product || item.product_id===product.id).slice(0,5))}</section>
    </>}
    {screen === 'products' && <>
      <div className="tw-heading"><div><span className="panel-label">QA / PRODUCTS</span><h2>产品与基线</h2><p>维护 SDK、应用仓库与板级配置；构建前验证产品基线。</p></div><button className="primary-button" disabled={!sdks.length || loading} onClick={newProduct}>＋ 新建测试产品</button></div>
      {!sdks.length && !loading && <p className="tw-warning">175 尚未注册 SDK。请由管理员预先登记本地 SDK；测试产品只从登记列表选择。</p>}
      <div className="wb-products">{products.map(item => { const history = rounds.filter(r => r.product_id === item.id); const last = history[0]; return <button className="panel wb-product" key={item.id} onClick={() => openProduct(item)}><header><span className="panel-label">{item.code}</span><StatusPill status={item.status} label={productStatus[item.status]} /></header><h3>{item.name}</h3><p>{item.gerrit_project}</p><Facts items={[["Board Config",item.board_config],["Manifest",item.manifest_snapshot.manifest_commit?.slice(0,12)],["测试轮次",history.length]]} /><footer><span>{last ? `最近 · ${last.product_snapshot.developer_mode ? '当前固件' : `${last.change_number}/PS${last.patchset_number}`}` : '尚未创建 Test Round'}</span><span>进入产品 →</span></footer></button>; })}</div>
      {!products.length && !loading && <div className="empty-state panel"><h2>从一个明确的产品基线开始</h2><p>一个 SDK、一份 Manifest、一个被测应用 Repo、一个 Board Config。</p></div>}
    </>}
    {screen === 'product-detail' && product && <>
      <div className="tw-heading"><div><button className="tw-back" onClick={() => setScreen('products')}>← 测试产品</button><span className="panel-label">{product.code}</span><h2>{product.name}</h2><p>{product.gerrit_project} · {product.board_config}</p></div><div className="tw-heading-actions"><StatusPill status={product.status} label={productStatus[product.status]} /><button className="primary-button" disabled={busy || product.status !== 'ready'} onClick={()=>newRound('full')}>编译烧录测试</button><button className="primary-button" disabled={busy} onClick={()=>newRound('quick')}>测试当前固件</button><button disabled={busy||!isRv1126b} onClick={openPrebuiltImport}>已有镜像烧录测试</button></div></div>
      <div className="wb-product-tabs" role="tablist" aria-label="产品详情">{([['history','测试时间线'],['points',`测试点 · ${points.length}`],['baseline','SDK / Manifest 基线']] as Array<[ProductTab,string]>).map(([key,label]) => <button key={key} role="tab" aria-selected={tab === key} onClick={() => setTab(key)}>{label}</button>)}</div>
      {tab === 'history' && <section className="panel"><header><h3>测试历史</h3><span>{productRounds.length} 轮</span></header>{roundList(productRounds)}</section>}
      {tab === 'points' && <section className="panel"><header><div><h3>Gerrit 测试点</h3><p>{product.gerrit_project}</p></div><button className="secondary-button" onClick={() => {setPointReady(false); setError(null); setDialog('point');}}>＋ 登记 Patchset</button></header>{points.map(point => <div className="wb-point" key={point.id}><strong>{point.change_number} / PS{point.patchset_number}</strong><span>{point.subject}<small>{point.branch} · {point.commit_sha}</small><small>Owner：{point.owner || '—'} · 提交时间：{formatDate(point.submitted_at)}</small></span><StatusPill status={point.ready?'ready':'blocked'} label={point.ready?'可选择':'等待依赖'} /></div>)}{!points.length && <p className="quiet-row">粘贴 Gerrit 链接即可查询并登记 Patchset，服务端会核验 Ref。</p>}</section>}
      {tab === 'baseline' && <><div className="tw-detail-grid"><section className="panel"><h3>Manifest 快照</h3><Manifest manifest={product.manifest_snapshot} /></section><section className="panel"><h3>唯一被测应用 Repo</h3><Facts items={[["Gerrit Project",product.gerrit_project],["本地路径",product.tested_repository.root_path],["分支",product.repository_snapshot.branch],["HEAD",product.repository_snapshot.head],["Git 状态",product.repository_snapshot.git_status],["Board Config",product.board_config]]} /></section></div><section className="panel"><header><div><h3>基线构建</h3><p>编译烧录与构建交付需要有效基线；当前固件测试可直接执行。</p></div><button className="primary-button" disabled={busy || sdkBusy} onClick={() => void verifyBaseline()}>{busy || product.status === 'verifying' ? '基线验证中…' : sdkBusy ? 'SDK 正被测试轮次使用' : '验证并编译基线'}</button></header><BaselineBuild product={product} /></section></>}
    </>}
    {screen === 'product-wizard' && <form onSubmit={saveProduct}><div className="tw-heading"><div><span className="panel-label">NEW PRODUCT</span><h2>建立产品基线</h2></div><button type="button" className="tw-back" onClick={() => setScreen('products')}>取消</button></div>
      <section className="panel"><h3>01 / 选择已注册 SDK</h3><div className="tw-choice-grid">{sdks.map(item => <label key={item.id} className={sdkId===item.id?'selected':''}><input type="radio" name="sdk" required checked={sdkId===item.id} onChange={() => {setSdkId(item.id);setManifest(null);setApplications([]);setApplicationId('');setBoard('');setBoardChoices([]);}} /><span><strong>{item.name}</strong><code>{item.root_path}</code></span></label>)}</div></section>
      {sdk && <><section className="panel"><header><h3>02 / Manifest 与唯一被测应用</h3><button type="button" className="secondary-button" onClick={onManageRepositories}>管理本地 Repo</button></header>{manifest ? <Manifest manifest={manifest} /> : <p>正在读取 Manifest 与 Git 状态…</p>}<label>被测应用 Repo<select required value={applicationId} onChange={event => setApplicationId(event.target.value)}><option value="">选择服务端已核验的应用仓库</option>{applications.map(item => <option value={item.id} key={item.id}>{item.name} · {item.gerrit_project}</option>)}</select></label>{application && <Facts items={[["Gerrit Project",application.gerrit_project],["本地目录",application.root_path],["Git 状态",application.snapshot.git_status],["分支",application.snapshot.branch],["HEAD",application.snapshot.head]]} />}{manifest && !applications.length && <p className="tw-warning">没有符合条件的应用 Repo。请核对本地仓库登记及 Gerrit remote；Kernel、U-Boot、RKBin 不可作为测试对象。</p>}</section>
      <section className="panel"><h3>03 / 选择 Board Config</h3><div className="tw-form-grid"><label>Board Config<select required value={board} onChange={event => setBoard(event.target.value)}><option value="">请选择板级配置</option>{boardChoices.map(item => <option key={item.board_config} value={item.board_config}>{item.menu_index}. {item.board_config}</option>)}</select></label><label>配置命令<input readOnly value={boardChoices.find(item => item.board_config === board)?.display_command || ''} /></label><label>固件目录<input readOnly value={sdk.artifact_root} /></label></div></section>
      <section className="panel"><h3>04 / 保存产品</h3><div className="tw-form-grid"><label>产品名称<input name="name" required /></label><label>产品标识<input name="code" pattern="[A-Za-z0-9][A-Za-z0-9._-]{1,79}" required placeholder="1126B-struct" /></label></div><p>保存后进入产品基线页，执行基线验证。编译退出码与产物校验共同决定结果。</p><div className="tw-actions"><button type="submit" className="primary-button" disabled={busy || !manifest || !application || !board}>保存产品基线</button></div></section></>}
    </form>}
    {screen === 'rounds' && <><div className="tw-heading"><div><h2>测试记录</h2><p>每次执行独立留档，可按来源和状态查找。</p></div><button className="primary-button" onClick={()=>setScreen('start')}>开始新测试</button></div><section className="panel"><div className="wb-history-filters"><label>产品<select value={historyProduct} onChange={e=>setHistoryProduct(e.target.value)}><option value="all">全部产品</option>{products.map(p=><option value={p.id} key={p.id}>{p.name}</option>)}</select></label><label>来源<select value={historyOrigin} onChange={e=>setHistoryOrigin(e.target.value)}><option value="all">全部来源</option>{Object.entries(originLabels).map(([key,label])=><option key={key} value={key}>{label}</option>)}</select></label><label>状态<select value={historyState} onChange={e=>setHistoryState(e.target.value)}><option value="all">全部状态</option><option value="active">进行中</option><option value="test_complete">测试通过</option><option value="attention">失败 / 阻塞</option><option value="remote_ready">已交付，未测试</option></select></label></div>{roundList(rounds.filter(r=>(historyProduct==='all'||r.product_id===historyProduct)&&(historyOrigin==='all'||(r.origin||'manual')===historyOrigin)&&(historyState==='all'||(historyState==='active'?activeStatuses.includes(r.status):historyState==='attention'?(roundSyncError(r)||['test_failed','bench_failed','build_failed','blocked'].includes(r.status)):r.status===historyState))))}</section></>}
    {screen === 'round-wizard' && product && <form onSubmit={createRound}>
      <div className="tw-heading"><div><button type="button" className="tw-back" onClick={()=>setScreen('start')}>← 开始测试</button><h2>{modeLabels[mode]}</h2><p>{product.name} · {developerMode?'使用模组当前运行的固件':mode==='delivery'?'构建镜像并交付，不执行设备测试':'构建所选提交并烧录，确认版本后执行用例'}</p></div></div>
      {!developerMode && <section className="panel"><header><h3>1. 选择固件版本</h3><button type="button" onClick={()=>{setPointReady(false);setError(null);setDialog('point');}}>从 Gerrit 链接选择</button></header><label>已登记版本<select required value={pointId} onChange={e=>setPointId(e.target.value)}><option value="">选择 Patch Set</option>{points.filter(p=>p.ready).map(p=><option key={p.id} value={p.id}>{p.change_number} / PS{p.patchset_number} · {p.subject}</option>)}</select></label>{pointId && <small>{points.find(p=>p.id===pointId)?.commit_sha}</small>}<details className="wb-evidence"><summary>构建配置</summary><Facts items={[["Board Config",product.board_config],["命令",isRv1126b?'./build.sh all':'./build.sh'],["镜像",isRv1126b?'oem.img / system.img / boot.img':'update.img']]}/></details></section>}
      <section className="panel"><header><h3>{developerMode?'1':'2'}. {mode==='delivery'?'选择交付测试台':'选择测试设备'}</h3></header>
        <label>测试台<select required value={benchId} onChange={e=>{setBenchId(e.target.value);setBenchReady(null);setDeviceSerial('');setDevices([]);setDeviceScan('idle');}}><option value="">选择测试台</option>{benches.filter(b=>b.enabled).map(b=><option key={b.id} value={b.id}>{b.name}</option>)}</select></label>
        {benchId && !developerMode && isRv1126b && <BenchReadiness key={`${benchId}-${mode}`} benchId={benchId} full={executeBasic} onResult={setBenchReady}/>}
        {executeBasic && <><div className="wb-device-toolbar"><p role="status">{!benchId?'选择测试台后自动检测设备':deviceScan==='loading'?'正在检测在线设备…':deviceScan==='error'?deviceError:deviceScan==='done'?`发现 ${devices.length} 台设备${devices.length===1?'，已自动选中':''}`:'等待设备检测'}</p><button type="button" disabled={!benchId||deviceScan==='loading'||busy} onClick={()=>setDeviceScanRevision(v=>v+1)}>重新检测</button></div><label>被测模组<select required disabled={deviceScan!=='done'||!devices.length} value={deviceSerial} onChange={e=>setDeviceSerial(e.target.value)}><option value="">选择模组</option>{devices.map(d=><option key={d.serial} value={d.serial}>{d.serial} · {d.mode}</option>)}</select></label>{deviceSerial && <small>当前固件：{devices.find(d=>d.serial===deviceSerial)?.version || '已记录设备身份'}</small>}{deviceScan==='done'&&!devices.length && <p className="tw-warning">{isRv1126b?'请连接 RMSL321，等待正常 UVC 启动后重新检测。':'请连接处于 Loader / Maskrom 模式的设备。'}</p>}</>}
        {mode==='full' && isRv1126b && <details className="wb-evidence"><summary>烧录范围</summary><p>写入另一槽的 oem / system / boot，启动后确认设备、槽位和本轮版本。</p></details>}
      </section>
      {executeBasic && <section className="panel"><header><h3>{developerMode?'2':'3'}. 选择用例</h3><span>已选 {selectedCount} 项{manualGate?' · 人工门控':''}</span></header>
        {developerMode&&<><nav className="tw-nav" aria-label="当前固件用例执行方式"><button type="button" aria-pressed={!manualGate} className={!manualGate?'active':''} disabled={busy} onClick={()=>setQuickExecution('automatic')}>自动执行 · {availableAutomaticCases.length} 项</button><button type="button" aria-pressed={manualGate} className={manualGate?'active':''} disabled={busy||!isRv1126b} onClick={()=>setQuickExecution('gated')}>特殊场景 · 人工门控 · {quickGateDefinitions.length} 项</button></nav><p>用例库包括自动用例与人工门控场景。自动用例可多选；人工门控每次执行一个场景，完成后可继续选择下一项。</p></>}
        {manualGate?<CurrentFirmwareGates definitions={quickGateDefinitions} selectedCode={quickScenarioCode} onSelect={setQuickScenarioCode} operator={quickOperator} onOperator={setQuickOperator} disabled={busy}/>:<CaseSelection cases={availableAutomaticCases} selected={selectedCases} onChange={setSelectedCases} renderParameters={c=>c.code==='STREAM-RGB-DEPTH'?<StreamParameterForm value={parameters[c.id]||'{}'} onChange={v=>setParameters(p=>({...p,[c.id]:v}))}/>:caseParameterFields[c.code]?<CaseParameterForm code={c.code} value={parameters[c.id]||'{}'} onChange={v=>setParameters(p=>({...p,[c.id]:v}))}/>:null}/>}</section>}
      <section className="panel wb-launch"><div><h3>确认本次范围</h3><p>{product.name} · {modeLabels[mode]} · {mode==='delivery'?'交付后结束，结果为未测试':manualGate?`人工门控 · ${selectedGate?.name||'尚未选择场景'}`:`${selectedCount} 项用例`}</p>{!manualGate&&selectedCases.some(id=>cases.find(c=>c.id===id)?.code==='STREAM-RGB-DEPTH')&&<p>包含出流质量测试，请确认测试时长；默认 5 小时。</p>}{executeBasic&&!manualGate&&selectedCases.length>0&&<details><summary>查看执行顺序</summary><ol>{orderedCases.filter(c=>selectedCases.includes(c.id)).map(c=><li key={c.id}>{c.name}</li>)}</ol></details>}</div><button type="submit" className="primary-button" disabled={busy || (!developerMode&&!pointId)||!benchId||(executeBasic&&(!selectedCount||!deviceSerial||deviceScan!=='done'))||(manualGate&&(!quickOperator.trim()||!isRv1126b))||(!developerMode&&product.status!=='ready')||(!developerMode&&isRv1126b&&(benchReady?.bench_id!==benchId||!benchReady.passed))}>{busy?'正在创建…':mode==='delivery'?'开始构建并交付':mode==='full'?'开始编译、烧录并测试':manualGate?'开始当前固件人工门控':'开始测试当前固件'}</button></section>
    </form>}
    {screen === 'round-detail' && round && <>
      {round.validation_plan_id&&<button type="button" onClick={()=>openPlan(round.validation_plan_id!)}>← 返回发布验证清单</button>}
      <div className="tw-heading"><div><button className="tw-back" onClick={() => setScreen('rounds')}>← 测试记录</button><span className="panel-label">TEST-{String(round.display_number).padStart(6,'0')}</span><h2>{String(round.product_snapshot.name)} · {originLabels[round.origin||'manual']} </h2></div><StatusPill status={roundSyncError(round)?'blocked':round.status} label={roundLabel(round)} /></div>
      {round.status==='created' && <button className="primary-button" disabled={busy} onClick={() => void perform(async()=>{await request(`/api/qa/test-rounds/${round.id}/start`,{method:'POST'});await refresh();})}>启动本轮任务</button>}
      {roundSyncError(round) && <div className="run-error" role="alert"><strong>结果同步异常</strong><p>无法获取最新测试结果，正在自动重试。下方保留最近一次成功同步的快照；未完成用例的当前状态待确认。</p><p>最近成功同步：{formatDate(round.controller_result?.received_at as string)} · 最近尝试：{formatDate(round.controller_result?.checked_at as string)}</p><p>{String(round.controller_result?.transport_error || '')}</p></div>}
      {round.last_error && <div className="run-error" role="alert"><strong>流程未完成</strong><p>{round.last_error}</p></div>}
      {round.status==='remote_ready' && <div className="wb-receipt"><strong>远端文件校验通过</strong><span>JSON 与 {round.image_snapshot.length} 个镜像已交付 · {formatDate(round.delivery_result?.verified_at)}</span></div>}
      {round.product_snapshot.execute_test_cases !== true && <section className="panel wb-receipt"><strong>{round.status==='remote_ready'?'交付完成 · 未测试':'构建交付任务'}</strong><p>本轮仅构建和交付镜像，不执行设备用例。</p></section>}<RoundResults key={round.id} round={round} onEvidence={setEvidenceCase} onChanged={refresh} />{evidenceCase && <BasicEvidence key={`${round.id}-${evidenceCase}`} round={round} code={evidenceCase} />}<div className="wb-round-layout"><aside className="panel"><h3>本轮固定快照</h3><Facts items={[["产品",round.product_snapshot.name],[isPrebuiltRound(round)?"175 镜像目录":"Gerrit Project",isPrebuiltRound(round)?firmwareSource(round).image_directory:round.gerrit_project],[isPrebuiltRound(round)?"预期固件身份":"提交说明",commitComment(round)],["Board Config",round.board_config],["测试台",round.bench_snapshot.name || '历史记录未保存快照'],["SSH",round.bench_snapshot.host ? `${round.bench_snapshot.user}@${round.bench_snapshot.host}:${round.bench_snapshot.port}` : '—'],["测试台任务目录",round.remote_path],["Test Cases",round.test_case_snapshot.length]]} /><Evidence value={{commit:round.commit_sha,product:round.product_snapshot}} label="完整版本与产品基线" /><Evidence value={round.test_case_snapshot} label="用例与参数快照" /></aside><div><details className="panel wb-process-details"><summary>执行过程记录 · {round.events.length} 条事件</summary><p>服务端在阶段开始、结束及收到 Controller 回报时保存事件，按序号展示。此记录用于定位流程问题。</p><div className="wb-events">{[...round.events].sort((a,b)=>a.sequence-b.sequence).map(event=><article key={event.id}><span className={`wb-event-mark ${event.status}`} /><div><header><strong>{({created:'已保存本轮配置与版本快照',checking_out:'同步并检出 Patchset',building:'执行 SDK 构建命令',packaging:'校验镜像并生成 JSON',transferring:'传输 JSON 与镜像',verifying:'远端文件校验'} as Record<string,string>)[event.event_type] || event.message}</strong><time>{formatDate(event.created_at)}</time></header><small>{({running:'进行中',succeeded:'已完成',failed:'失败',blocked:'已阻塞'} as Record<string,string>)[event.status] || event.status}{typeof event.details.finished_at==='string' && ` · 结束于 ${formatDate(event.details.finished_at)}`}</small>{Object.keys(event.details).length>0 && <Evidence value={event.details} />}</div></article>)}</div></details>
      <section className="panel"><h3>本轮报告</h3><p>固件来源：{roundSource(round)}</p><p>自动执行结论：{roundLabel(round)}</p><button className="secondary-button" disabled={busy} onClick={()=>void downloadReport(round)}>{busy?'正在生成报告…':'下载完整 JSON 报告'}</button></section>{round.controller_result && <details className="panel wb-process-details"><summary>测试台 Controller · 连接与原始结果</summary><Facts items={[["阶段",round.controller_result.phase],["状态",round.controller_result.state],["说明",round.controller_result.message],["心跳",formatDate(round.controller_result.heartbeat_at as string)],["连接",round.controller_result.transport_error || '最近一次接收正常']]} /><p>{round.product_snapshot.developer_mode ? '当前固件身份验证' : '烧录后启动验证'}：{(round.product_snapshot.developer_mode ? round.controller_result.identity_verified : round.controller_result.boot_verified)===true?'已确认':'尚未确认'}</p><pre className="wb-build-log">{String(round.controller_result.log_tail || '')}</pre><Evidence value={round.controller_result} label="Controller result.json" /></details>}<details className="panel wb-process-details" open={round.status==='building'}><summary>{isPrebuiltRound(round)?'导入校验与镜像':'构建证据与镜像'}</summary>{Boolean(round.build_result?.progress) && <><p><code>{String(round.build_result?.command || (round.build_result?.progress as Record<string,unknown>)?.command || '')}</code></p><pre className="wb-live-log" aria-label="轮次构建实时日志">{String(round.build_result?.stdout_tail || (round.build_result?.progress as Record<string,unknown>)?.logs || '')}</pre></>}{isPrebuiltRound(round)?<><p>本轮使用 175 上导入并保存的镜像，未执行编译。</p><Facts items={[["来源目录",firmwareSource(round).image_directory],["导入人",firmwareSource(round).operator],["导入时间",formatDate(firmwareSource(round).imported_at)],["导入镜像校验",firmwareSource(round).validation?.passed===true?'通过':'待确认'],["预期身份",firmwareIdentity(round).value]]}/><Evidence value={firmwareSource(round)} label="导入来源与镜像校验证据"/></>:round.build_result ? <><Facts items={[["命令退出码",round.build_result.exit_code],["产物校验",round.build_result.artifact_validation_passed===true?'通过':round.build_result.artifact_validation_passed===false?'未通过':'待校验'],["构建结果",round.build_result.passed===true?'通过':round.build_result.passed===false?'未通过':'进行中']]} /><Evidence value={round.build_result} label="命令、输出和退出码" /></> : <p>{round.product_snapshot.developer_mode ? '开发者模式已跳过构建与烧录。' : '等待服务端执行构建。'}</p>}{round.image_snapshot.map((item,index)=><Facts key={index} items={[["镜像",item.filename],["字节数",item.size_bytes],["SHA-256",item.sha256]]} />)}</details>
      {round.generated_config && <section className="panel"><h3>交付文件</h3><Evidence value={round.generated_config} label="test-run.json" />{round.delivery_result && <Evidence value={round.delivery_result} label="远端文件摘要校验结果" />}<p>{round.product_snapshot.execute_test_cases ? "本轮执行结果和协议证据见上方。" : "本轮仅交付文件，未执行测试。"}</p></section>}</div></div>
    </>}
    {screen === 'cases' && <>
      <div className="tw-heading"><div><h2>用例库</h2><p>管理已接入的自动执行与人工门控用例；具体执行结果见测试记录或发布验证。</p></div><button disabled={busy} onClick={()=>void perform(async()=>{const result=await request<{added:string[]}>('/api/qa/basic-case-definitions/install',{method:'POST'});await refresh();setNotice(result.added.length?`已加入 ${result.added.length} 个内置用例。`:'内置用例已齐全，未重复添加。');})}>同步内置用例</button></div>
      <h3>已接入用例 · {orderedCases.length} 项</h3>
      <p className="wb-case-catalog-counts">自动执行 {orderedCases.filter(item=>basicDefinitions.some(d=>d.id===item.code)).length} 项 · 人工门控 {orderedCases.filter(item=>specialDefinitions.some(d=>d.code===item.code)).length} 项</p>
      <div className="tw-case-grid">{orderedCases.map(item=>{
        const special=specialDefinitions.find(d=>d.code===item.code);
        const basic=basicDefinitions.find(d=>d.id===item.code);
        const latestSpecial=specialRuns.find(run=>run.scenario_code===item.code);
        const latestRound=rounds.find(r=>Array.isArray(r.controller_result?.cases)&&(r.controller_result!.cases as {id:string;outcome?:string}[]).some(c=>c.id===item.id&&c.outcome));
        const outcome=latestRound&&(latestRound.controller_result!.cases as {id:string;outcome?:string}[]).find(c=>c.id===item.id)?.outcome;
        return <article className="panel" key={item.id}>
          <header><strong>{item.code}</strong><StatusPill status={item.risk_level} label={riskLabels[item.risk_level]} /></header>
          <h3>{item.name}</h3><p>{item.description}</p>
          <p>{special?'特殊场景 · 人工门控 · 已接入':caseGroup(item.code)+' · '+(basic?'自动执行 · 已接入':'尚未接入执行')}</p>
          {special?<><p>{special.steps.length} 个门控步骤 · 定义版本 {special.version}</p><button type="button" onClick={()=>openSpecial(undefined,item.code)}>准备执行此场景 →</button>
            {latestSpecial?<p>最近测试：{({preparing_firmware:'准备候选固件',awaiting_operator:'等待操作',running:'执行中',passed:'通过',failed:'失败',blocked:'阻塞',interrupted:'中断',cancelled:'已取消',cancelling:'正在停止'} as Record<string,string>)[latestSpecial.status]||latestSpecial.status} · {formatDate(latestSpecial.created_at)}<button type="button" onClick={()=>openSpecial(latestSpecial.id)}>查看流程与证据</button></p>:<p>尚无实测结果</p>}
            <details><summary>流程定义与记录要求</summary><ol>{special.steps.map(step=><li key={step.id}><strong>{step.title}</strong><p>{step.instruction}</p><small>记录：{step.records.join('；')}</small></li>)}</ol><Evidence value={special} label="查看完整门控定义" /></details>
          </>:<>{latestRound?<p>最近实测：{outcome==='passed'?'PASS':outcome==='failed'?'FAIL':'ERROR'} · TEST-{String(latestRound.display_number).padStart(6,'0')}<button onClick={()=>openRound(latestRound)}>查看该轮证据</button></p>:<p>尚无实测结果</p>}
            <details><summary>定义与管理</summary><code>{item.command}</code>{basic?<Evidence value={basic} label="用例定义、协议入口与测试文件" />:<p>执行适配器未接入</p>}
              <label>所属 set<select value={item.category} disabled={busy} onChange={event=>{const category=event.target.value;void perform(async()=>{await request(`/api/qa/test-cases/${item.id}/category`,{method:'PATCH',body:JSON.stringify({category})});await refresh();});}}><option value="basic">基本功能 set</option><option value="special">特殊场景 set</option></select></label><small>预期 exit {item.expected_exit_code} · {item.timeout_seconds}s</small>
            </details>
          </>}
        </article>;
      })}</div>{!cases.length&&<p className="quiet-row">暂无 Test Case。</p>}
      {definitionsLoaded&&<PendingScenarios implementedCodes={new Set([...basicDefinitions.map(item=>item.id),...specialDefinitions.map(item=>item.code)])} />}
    </>}
    {screen === 'benches' && <><div className="tw-heading"><div><h2>测试台节点</h2><p>175 服务端使用已配置的 SSH 密钥连接节点。</p></div><button className="primary-button" onClick={()=>setDialog('bench')}>＋ 登记节点</button></div><div className="tw-bench-grid">{benches.map(item=><article className="panel" key={item.id}><header><h3>{item.name}</h3><StatusPill status={item.enabled?'ready':'blocked'} label={item.enabled?'已启用':'已停用'} /></header><Facts items={[["SSH",`${item.user}@${item.host}:${item.port}`],["测试台根目录",item.remote_root]]} /><BenchReadiness benchId={item.id} /></article>)}</div>{!benches.length && <p className="quiet-row">暂无测试台节点。</p>}</>}
    {dialog && <div className="dialog-backdrop"><section className="dialog qa-dialog" role="dialog" aria-modal="true" aria-labelledby="wb-dialog-title"><div className="dialog-heading"><h2 id="wb-dialog-title">{dialog==='point'?'登记 Patchset':dialog==='bench'?'登记测试台':'新建 Test Case'}</h2><button aria-label="关闭" disabled={busy} onClick={()=>setDialog(null)}>×</button></div>{error && <p role="alert" className="tw-warning">{error}</p>}<form onSubmit={saveDialog}>
      {dialog==='point' ? product && <GerritPatchsetFields product={product} onReady={setPointReady} busy={busy} /> : dialog==='bench' ? <><label>节点名称<input name="name" required /></label><label>主机<input name="host" required /></label><label>SSH 端口<input name="port" type="number" min="1" max="65535" defaultValue="22" required /></label><label>用户名<input name="user" required /></label><label>测试台根目录<input name="remote_root" required placeholder="/home/linaro/develop-hub-qa" /></label></> : null}
      <div className="form-actions"><button type="button" disabled={busy} onClick={()=>setDialog(null)}>取消</button><button type="submit" className="approve" disabled={busy || (dialog==='point' && !pointReady)}>{busy?'正在保存…':'保存'}</button></div></form></section></div>}
  </section>;
}
