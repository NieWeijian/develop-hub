'use client';
/* Captured evidence must be served unchanged from the artifact API. */
/* eslint-disable @next/next/no-img-element */

import scenarioCatalog from './scenario-catalog.json';

import { FormEvent, useCallback, useEffect, useState, useRef } from 'react';

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

type View = 'products' | 'rounds' | 'cases' | 'benches';
type Screen = View | 'product-detail' | 'product-wizard' | 'round-wizard' | 'round-detail';
type Dialog = 'case' | 'point' | 'bench';

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
    if (detail && typeof detail === 'object') throw new Error(JSON.stringify(detail));
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
const outcomeLabels: Record<string,string> = {passed:'通过',failed:'不通过',error:'执行异常',blocked:'阻塞',skipped:'跳过',running:'执行中',pending:'未测',interrupted:'已中断'};
function resultState(result?:CaseOutcome) {
  return result?.outcome || (result?.state === 'finished' ? 'error' : result?.state === 'running' ? 'running' : ['blocked','skipped','interrupted'].includes(result?.state || '') ? result!.state! : 'pending');
}
function commitComment(round:TestRound) {
  const frozen = round.product_snapshot.test_point as {subject?:string} | undefined;
  return frozen?.subject || (round.test_point?.commit_sha === round.commit_sha ? round.test_point.subject : '') || '历史记录未保存提交说明';
}
function RoundResults({round,onEvidence,onChanged}:{round:TestRound;onEvidence:(code:string)=>void;onChanged:()=>Promise<void>}) {
  const [filter,setFilter] = useState('all');
  const [editing,setEditing] = useState<string|null>(null);
  const stateOf = (result:CaseOutcome|undefined, plan:Record<string,unknown>) => round.case_classifications?.[String(plan.code)]?.outcome || resultState(result);
  const results = (round.controller_result?.cases || []) as CaseOutcome[];
  const rows = round.test_case_snapshot.map(plan => ({plan, result:results.find(r=>r.id===plan.id || (!!r.code && r.code===plan.code))}));
  const count = (state:string) => rows.filter(row=>stateOf(row.result,row.plan)===state).length;
  const passed=count('passed'), failed=count('failed'), errors=count('error');
  const pending=rows.length-passed-failed-errors;
  const visible=rows.filter(row=>filter==='all' || (filter==='attention' ? ['failed','error','blocked','interrupted'].includes(stateOf(row.result,row.plan)) : filter==='unfinished' ? !['passed','failed','error'].includes(stateOf(row.result,row.plan)) : stateOf(row.result,row.plan)===filter));
  const milestones = [
    [round.product_snapshot.developer_mode ? '编译与烧录（已跳过）' : '编译与镜像',round.product_snapshot.developer_mode ? 'skipped' : round.build_result?.passed === true ? 'passed' : round.build_result?.passed === false ? 'failed' : round.status==='building' ? 'running' : 'pending'],
    ['测试台交付',round.delivery_result?.passed === true ? 'passed' : round.delivery_result?.passed === false ? 'failed' : round.status==='transferring' ? 'running' : 'pending'],
    [round.product_snapshot.developer_mode ? '当前固件身份确认' : '烧录后启动确认',(round.product_snapshot.developer_mode ? round.controller_result?.identity_verified : round.controller_result?.boot_verified) === true ? 'passed' : 'pending'],
    ['用例执行',round.status==='test_complete' ? 'passed' : round.status==='test_failed' ? 'failed' : results.some(r=>r.state==='running') ? 'running' : 'pending'],
  ];
  return <section className="panel wb-results">
    <header><div><span className="panel-label">TEST RESULTS</span><h3>用例结果</h3></div><span className={`wb-outcome result-${round.case_classification_verdict || 'pending'}`}>当前分类：{outcomeLabels[round.case_classification_verdict] || '未测'}</span></header>
    <p>自动执行：{roundStatus[round.status] || round.status} · 当前分类包含人工修正</p><p className="wb-result-subtitle">{commitComment(round)} · {round.product_snapshot.developer_mode ? '开发者快速测试' : `${round.change_number}/PS${round.patchset_number}`} </p>
    <div className="wb-result-counts">{[['✓','通过',passed,'passed'],['×','不通过',failed,'failed'],['!','执行异常',errors,'error'],['○','未完成 / 跳过',pending,'pending']].map(([icon,label,value,state])=><div key={String(state)} className={`result-${state}`}><span><b aria-hidden="true">{icon}</b> {label}</span><strong>{value}</strong></div>)}</div>
    <p>本轮计划 {rows.length} 项 · 有效判定 {passed+failed}/{rows.length} · 未测 {count('pending')} · 运行中 {count('running')} · 阻塞 {count('blocked')} · 跳过 {count('skipped')} · 中断 {count('interrupted')}</p>
    {!round.product_snapshot.execute_test_cases && <p className="tw-warning">本轮未启用用例执行；交付或烧录成功不能作为测试通过。</p>}
    <nav className="wb-result-filters" aria-label="筛选用例结果">{[['all','全部'],['attention','失败 / 异常'],['unfinished','未完成 / 跳过'],['passed','通过']].map(([value,label])=><button type="button" key={value} aria-pressed={filter===value} onClick={()=>setFilter(value)}>{label}</button>)}</nav>
    <div className="wb-result-rows">{visible.map(({plan,result})=>{const state=stateOf(result,plan);const classification=round.case_classifications?.[String(plan.code)];return <article key={String(plan.id)}><span className={`wb-outcome result-${state}`}>{outcomeLabels[state] || state}</span><div><strong>{String(plan.name || plan.code)}</strong><small>{String(plan.code)}{result?.assertions_total!==undefined ? ` · 断言 ${result.assertions_passed ?? 0}/${result.assertions_total}` : ' · 尚无断言结果'}</small>{classification?.manual && <small>人工修正 · 自动结果：{outcomeLabels[classification.automatic_outcome || ''] || classification.automatic_outcome}</small>}{classification?.history.length ? <details><summary>修改记录 · {classification.history.length}</summary>{classification.history.map((h,i)=><p key={i}>{formatDate(h.changed_at)} · {h.operator} · {h.outcome ? outcomeLabels[h.outcome] : '恢复自动分类'}：{h.reason}</p>)}</details>:null}{result?.recovery?.state==='failed' && <small>恢复失败，请先处理设备状态</small>}</div><div className="wb-case-actions"><button type="button" disabled={!result?.code} onClick={()=>result?.code && onEvidence(result.code)}>查看证据 →</button>{classification?.executed && ['test_complete','test_failed','bench_failed','blocked'].includes(round.status) && <button type="button" onClick={()=>setEditing(String(plan.code))}>修改分类</button>}</div></article>;})}{!visible.length && <p>此筛选下没有用例。</p>}</div>
    {editing && <ClassificationEditor key={editing} round={round} code={editing} onClose={()=>setEditing(null)} onChanged={onChanged} />}
    <h4>执行里程碑（自动执行记录）</h4><ol className="wb-milestones">{milestones.map(([name,state])=><li key={name} className={`result-${state}`}><span aria-hidden="true">{state==='passed'?'✓':state==='failed'?'×':state==='running'?'◷':'○'}</span><div><strong>{name}</strong><small>{state==='pending'?'待确认':outcomeLabels[state]}</small></div></li>)}</ol>
    <small>里程碑来自服务端构建、交付和 Controller 证据；逐项测试结论以上方用例为准。</small>
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

function ScenarioCatalog() {
  return <section className="panel wb-scenario-catalog"><header><div><span className="panel-label">COVERAGE PLAN</span><h3>场景覆盖目录</h3></div><span>{scenarioCatalog.scenarios.reduce((n,g)=>n+g.cases.length,0)} 项规划用例</span></header>
    <p>按场景组织独立用例，再由分辨率、设备模式、参数与持续时间生成执行实例。此目录是扩充计划，不计入已执行结果；当前可执行用例见下方。</p>
    <div className="wb-scenario-grid">{scenarioCatalog.scenarios.map(group=><details key={group.id}><summary><span aria-hidden="true">{group.icon}</span> {group.name}<small>{group.cases.length} 项</small></summary>{group.cases.map(item=><article key={item.code}><strong>{item.name}</strong><small>{item.code} · {item.capability}</small><p>{item.assertion}</p><small>遍历：{item.matrix}</small><small>工具：{item.tool}</small></article>)}</details>)}</div>
  </section>;
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
  return <div className="wb-readiness"><h4>测试台就绪检查</h4>
    <p role="status">{error || (!result?'正在检查测试台…':result.passed?'测试台检查通过':'测试台未就绪，请处理以下问题后重新检查。')}</p>
    {result && <><ul>{result.checks.map(check=><li key={check.name}>{check.passed?'✓':'✕'} {check.name}：{check.detail}</li>)}</ul><small>检查时间：{formatDate(result.checked_at)}</small></>}
    <p>此检查只读取环境；RMSL321 完整验证提交时会再次检查，未通过不会开始编译。</p>
    <button type="button" disabled={!result && !error} onClick={()=>{setResult(null);setError('');onResult?.(null);setRevision(value=>value+1);}}>重新检查测试台</button>
  </div>;
}

export default function QaWorkbench({ onManageRepositories }: { onManageRepositories: () => void }) {
  const [screen, setScreen] = useState<Screen>('products');
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
  const [flashFirmware, setFlashFirmware] = useState(true);
  const [developerMode,setDeveloperMode] = useState(false);
  const [executeBasic, setExecuteBasic] = useState(true);
  const [wearOverride, setWearOverride] = useState(false);
  const [basicDefinitions,setBasicDefinitions]=useState<BasicDefinition[]>([]);
  const [evidenceCase,setEvidenceCase]=useState('');
  const [deviceSerial, setDeviceSerial] = useState('');
  const [devices, setDevices] = useState<{serial:string; mode:string;version?:string}[]>([]);
  const [deviceScan, setDeviceScan] = useState<'idle'|'loading'|'done'|'error'>('idle');
  const [deviceError, setDeviceError] = useState('');
  const [deviceScanTime, setDeviceScanTime] = useState<string | null>(null);
  const [deviceScanRevision, setDeviceScanRevision] = useState(0);
  const [selectedCases, setSelectedCases] = useState<string[]>([]);
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
  useEffect(()=>{
    const timer=window.setTimeout(()=>{
    const params=new URLSearchParams(window.location.search);
    const target=params.get('qa');
    if(target && ['products','rounds','cases','benches','product-detail','round-detail'].includes(target)) setScreen(target as Screen);
    setProductId(params.get('product') || '');setRoundId(params.get('round') || '');
    if(params.get('tab')==='baseline')setTab('baseline');
    setLocationReady(true);
    },0);
    return ()=>window.clearTimeout(timer);
  },[]);
  useEffect(()=>{
    if(!locationReady)return;
    const url=new URL(window.location.href);
    url.searchParams.set('qa',screen==='round-wizard'?'product-detail':screen==='product-wizard'?'products':screen);
    for(const key of ['product','round','tab'])url.searchParams.delete(key);
    if(screen.startsWith('product') || screen==='round-wizard'){
      if(productId)url.searchParams.set('product',productId);
      if(tab==='baseline')url.searchParams.set('tab',tab);
    }
    if(screen==='round-detail')url.searchParams.set('round',roundId);
    window.history.replaceState(null,'',url);
  },[locationReady,screen,productId,roundId,tab]);
  const product = products.find(item => item.id === productId);
  const isRv1126b = Boolean(product?.board_config.toLowerCase().includes('rv1126b'));
  const round = rounds.find(item => item.id === roundId);
  const sdk = sdks.find(item => item.id === sdkId);
  const application = applications.find(item => item.id === applicationId);
  const sdkBusy = Boolean(product && (rounds.some(r=>r.product.sdk_repository_id===product.sdk_repository_id && ['created','checking_out','building','packaging','transferring','verifying'].includes(r.status)) || products.some(p=>p.sdk_repository_id===product.sdk_repository_id && p.status==='verifying')));
  const productRounds = rounds.filter(item => item.product_id === productId);
  const activeView: View = screen.startsWith('round') ? 'rounds' : screen.startsWith('product') ? 'products' : screen as View;

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
        const definitions=await request<BasicDefinition[]>('/api/qa/basic-case-definitions');
        if(alive)setBasicDefinitions(definitions);
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
    if (!productId) return;
    let alive = true;
    request<TestPoint[]>(`/api/qa/products/${productId}/test-points`).then(result => { if (alive) setPoints(result); }).catch(cause => { if (alive) setError(String(cause)); });
    return () => { alive = false; };
  }, [productId]);

  useEffect(() => {
    if (!benchId || (!flashFirmware && !developerMode)) return;
    const controller = new AbortController();
    const detect = async () => {
      setDeviceScan('loading'); setDeviceError(''); setDevices([]); setDeviceSerial('');
      try {
        const found = await request<{serial:string; mode:string;version?:string}[]>(`/api/qa/benches/${benchId}/devices?chip=${isRv1126b ? "RV1126B" : "RK3572"}`, {signal:controller.signal});
        if (controller.signal.aborted) return;
        setDevices(found); setDeviceSerial(found.length===1 ? found[0].serial : '');
        setDeviceScan('done'); setDeviceScanTime(new Date().toISOString());
      } catch (cause) {
        if (controller.signal.aborted) return;
        setDeviceScan('error'); setDeviceError(cause instanceof Error ? cause.message : String(cause));
      }
    };
    void detect();
    return () => controller.abort();
  }, [benchId, flashFirmware, developerMode, deviceScanRevision, isRv1126b]);

  async function perform(work: () => Promise<void>) {
    setBusy(true); setError(null);
    try { await work(); } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  }
  function openProduct(item: Product) { if (item.id !== productId) setPoints([]); setProductId(item.id); setScreen('product-detail'); setTab('history'); }
  function openRound(item: TestRound) { setNotice('');setError(null);setEvidenceCase(''); setRoundId(item.id); setScreen('round-detail'); }
  function newRound() { setNotice('');setBenchReady(null);setPointId(''); setBenchId(''); setDeviceSerial(''); setDevices([]); setSelectedCases(cases.filter(item=>basicDefinitions.some(d=>d.id===item.code)).map(item=>item.id)); setParameters({}); setFlashFirmware(true); setExecuteBasic(true); setWearOverride(false); setScreen('round-wizard'); }
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
    event.preventDefault(); if (!product) return;
    void perform(async () => {
      const parsed: Record<string, Record<string, unknown>> = {};
      for (const id of selectedCases) {
        const value = JSON.parse(parameters[id]?.trim() || '{}');
        if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error('Test Case 参数必须为 JSON 对象');
        parsed[id] = value;
      }
      const created = await request<TestRound>('/api/qa/test-rounds', {method:'POST', body:JSON.stringify({product_id:product.id, test_point_id:developerMode ? null : pointId, developer_mode:developerMode, bench_id:benchId, test_case_ids:[...selectedCases].sort((a,b)=>basicDefinitions.findIndex(d=>d.id===cases.find(c=>c.id===a)?.code)-basicDefinitions.findIndex(d=>d.id===cases.find(c=>c.id===b)?.code)), test_case_parameters:parsed, execute_test_cases:executeBasic, flash_firmware:flashFirmware, device_serial:deviceSerial, allow_wear_limit_override:wearOverride, auto_start:true})});
      await refresh(); openRound(created);
    });
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
      } else {
        await request('/api/qa/test-cases', {method:'POST', body:JSON.stringify({code:data.get('code'), name:data.get('name'), description:data.get('description'), command:data.get('command'), expected_exit_code:Number(data.get('expected_exit_code')), timeout_seconds:Number(data.get('timeout_seconds')), risk_level:data.get('risk_level'), category:data.get('category'), enabled:true})});
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
  function roundList(items: TestRound[]) {
    return items.length ? <div className="wb-history">{items.map(item => <button key={item.id} onClick={() => openRound(item)}><span><strong>TEST-{String(item.display_number).padStart(6,'0')}</strong><small>{formatDate(item.created_at)}</small></span><span>{item.product.name}<small>Change {item.change_number} · PS{item.patchset_number} · {item.commit_sha.slice(0,12)}</small></span><StatusPill status={item.status} label={roundStatus[item.status] || item.status} /></button>)}</div> : <p className="quiet-row">暂无测试历史。从基线可用的产品创建第一轮测试。</p>;
  }

  return <section className="test-workbench wb-workspace">
    <nav className="tw-nav" aria-label="测试平台导航">{([['products','测试产品'],['rounds','测试轮次'],['cases','用例库'],['benches','测试台节点']] as Array<[View,string]>).map(([key,label]) => <button key={key} className={activeView === key ? 'active' : ''} onClick={() => {setScreen(key);setError(null);setNotice('');}}>{label}</button>)}<span>{loading ? '正在读取服务端状态…' : `更新于 ${formatDate(updated)}`}</span></nav>
    {notice && <p role="status" className="wb-receipt">{notice}</p>}
    {error && <div className="error-banner" role="alert"><span>{error}</span><button aria-label="关闭错误" onClick={() => setError(null)}>×</button></div>}
    {screen === 'products' && <>
      <div className="tw-heading"><div><span className="panel-label">QA / PRODUCTS</span><h2>测试产品</h2><p>从产品进入，选择 Gerrit 版本和用例，启动一次完整测试。</p></div><button className="primary-button" disabled={!sdks.length || loading} onClick={newProduct}>＋ 新建测试产品</button></div>
      {!sdks.length && !loading && <p className="tw-warning">175 尚未注册 SDK。请由管理员预先登记本地 SDK；测试产品只从登记列表选择。</p>}
      <ol className="wb-process-guide"><li>选择产品基线</li><li>锁定 Gerrit Patch Set</li><li>选择测试台与用例</li><li>编译 → 烧录 → 版本确认</li><li>检查断言与下载报告</li></ol><p className="wb-build-hint">每轮记录精确版本、镜像摘要、设备身份和实际结果。编译或烧录成功不代表测试通过；失败后创建新轮次，历史结果不会覆盖。</p><div className="wb-products">{products.map(item => { const history = rounds.filter(r => r.product_id === item.id); const last = history[0]; return <button className="panel wb-product" key={item.id} onClick={() => openProduct(item)}><header><span className="panel-label">{item.code}</span><StatusPill status={item.status} label={productStatus[item.status]} /></header><h3>{item.name}</h3><p>{item.gerrit_project}</p><Facts items={[["Board Config",item.board_config],["Manifest",item.manifest_snapshot.manifest_commit?.slice(0,12)],["测试轮次",history.length]]} /><footer><span>{last ? `最近 · ${last.product_snapshot.developer_mode ? '当前固件' : `${last.change_number}/PS${last.patchset_number}`}` : '尚未创建 Test Round'}</span><span>进入产品 →</span></footer></button>; })}</div>
      {!products.length && !loading && <div className="empty-state panel"><h2>从一个明确的产品基线开始</h2><p>一个 SDK、一份 Manifest、一个被测应用 Repo、一个 Board Config。</p></div>}
    </>}
    {screen === 'product-detail' && product && <>
      <div className="tw-heading"><div><button className="tw-back" onClick={() => setScreen('products')}>← 测试产品</button><span className="panel-label">{product.code}</span><h2>{product.name}</h2><p>{product.gerrit_project} · {product.board_config}</p></div><div className="tw-heading-actions"><StatusPill status={product.status} label={productStatus[product.status]} /><button className="primary-button" disabled={busy || product.status !== 'ready'} onClick={newRound}>＋ 新建测试轮次</button></div></div>
      <div className="wb-product-tabs" role="tablist" aria-label="产品详情">{([['history','测试时间线'],['points',`测试点 · ${points.length}`],['baseline','SDK / Manifest 基线']] as Array<[ProductTab,string]>).map(([key,label]) => <button key={key} role="tab" aria-selected={tab === key} onClick={() => setTab(key)}>{label}</button>)}</div>
      {tab === 'history' && <section className="panel"><header><h3>测试历史</h3><span>{productRounds.length} 轮</span></header>{roundList(productRounds)}</section>}
      {tab === 'points' && <section className="panel"><header><div><h3>Gerrit 测试点</h3><p>{product.gerrit_project}</p></div><button className="secondary-button" onClick={() => {setPointReady(false); setError(null); setDialog('point');}}>＋ 登记 Patchset</button></header>{points.map(point => <div className="wb-point" key={point.id}><strong>{point.change_number} / PS{point.patchset_number}</strong><span>{point.subject}<small>{point.branch} · {point.commit_sha}</small><small>Owner：{point.owner || '—'} · 提交时间：{formatDate(point.submitted_at)}</small></span><StatusPill status={point.ready?'ready':'blocked'} label={point.ready?'可选择':'等待依赖'} /></div>)}{!points.length && <p className="quiet-row">粘贴 Gerrit 链接即可查询并登记 Patchset，服务端会核验 Ref。</p>}</section>}
      {tab === 'baseline' && <><div className="tw-detail-grid"><section className="panel"><h3>Manifest 快照</h3><Manifest manifest={product.manifest_snapshot} /></section><section className="panel"><h3>唯一被测应用 Repo</h3><Facts items={[["Gerrit Project",product.gerrit_project],["本地路径",product.tested_repository.root_path],["分支",product.repository_snapshot.branch],["HEAD",product.repository_snapshot.head],["Git 状态",product.repository_snapshot.git_status],["Board Config",product.board_config]]} /></section></div><section className="panel"><header><div><h3>基线构建</h3><p>验证当前 SDK；命令成功且镜像校验通过后才能创建 Test Round。</p></div><button className="primary-button" disabled={busy || sdkBusy} onClick={() => void verifyBaseline()}>{busy || product.status === 'verifying' ? '基线验证中…' : sdkBusy ? 'SDK 正被测试轮次使用' : '验证并编译基线'}</button></header><BaselineBuild product={product} /></section></>}
    </>}
    {screen === 'product-wizard' && <form onSubmit={saveProduct}><div className="tw-heading"><div><span className="panel-label">NEW PRODUCT</span><h2>建立产品基线</h2></div><button type="button" className="tw-back" onClick={() => setScreen('products')}>取消</button></div>
      <section className="panel"><h3>01 / 选择已注册 SDK</h3><div className="tw-choice-grid">{sdks.map(item => <label key={item.id} className={sdkId===item.id?'selected':''}><input type="radio" name="sdk" required checked={sdkId===item.id} onChange={() => {setSdkId(item.id);setManifest(null);setApplications([]);setApplicationId('');setBoard('');setBoardChoices([]);}} /><span><strong>{item.name}</strong><code>{item.root_path}</code></span></label>)}</div></section>
      {sdk && <><section className="panel"><header><h3>02 / Manifest 与唯一被测应用</h3><button type="button" className="secondary-button" onClick={onManageRepositories}>管理本地 Repo</button></header>{manifest ? <Manifest manifest={manifest} /> : <p>正在读取 Manifest 与 Git 状态…</p>}<label>被测应用 Repo<select required value={applicationId} onChange={event => setApplicationId(event.target.value)}><option value="">选择服务端已核验的应用仓库</option>{applications.map(item => <option value={item.id} key={item.id}>{item.name} · {item.gerrit_project}</option>)}</select></label>{application && <Facts items={[["Gerrit Project",application.gerrit_project],["本地目录",application.root_path],["Git 状态",application.snapshot.git_status],["分支",application.snapshot.branch],["HEAD",application.snapshot.head]]} />}{manifest && !applications.length && <p className="tw-warning">没有符合条件的应用 Repo。请核对本地仓库登记及 Gerrit remote；Kernel、U-Boot、RKBin 不可作为测试对象。</p>}</section>
      <section className="panel"><h3>03 / 选择 Board Config</h3><div className="tw-form-grid"><label>Board Config<select required value={board} onChange={event => setBoard(event.target.value)}><option value="">请选择板级配置</option>{boardChoices.map(item => <option key={item.board_config} value={item.board_config}>{item.menu_index}. {item.board_config}</option>)}</select></label><label>配置命令<input readOnly value={boardChoices.find(item => item.board_config === board)?.display_command || ''} /></label><label>固件目录<input readOnly value={sdk.artifact_root} /></label></div></section>
      <section className="panel"><h3>04 / 保存产品</h3><div className="tw-form-grid"><label>产品名称<input name="name" required /></label><label>产品标识<input name="code" pattern="[A-Za-z0-9][A-Za-z0-9._-]{1,79}" required placeholder="1126B-struct" /></label></div><p>保存后进入产品基线页，执行基线验证。编译退出码与产物校验共同决定结果。</p><div className="tw-actions"><button type="submit" className="primary-button" disabled={busy || !manifest || !application || !board}>保存产品基线</button></div></section></>}
    </form>}
    {screen === 'rounds' && <><div className="tw-heading"><div><span className="panel-label">QA / TEST ROUNDS</span><h2>测试轮次</h2><p>每轮都保留独立的源码、构建与交付记录。</p></div><button className="secondary-button" onClick={() => setScreen('products')}>从产品创建 →</button></div><section className="panel">{roundList(rounds)}</section></>}
    {screen === 'round-wizard' && product && <form onSubmit={createRound}><div className="tw-heading"><div><span className="panel-label">{product.code} / NEW ROUND</span><h2>配置测试轮次</h2></div><button type="button" className="tw-back" onClick={() => setScreen('product-detail')}>取消</button></div><div className="wb-round-layout"><aside className="panel wb-locked"><span className="panel-label">本轮锁定</span><h3>{product.name}</h3><Facts items={[["Repo",product.gerrit_project],["Board Config",product.board_config]]} /><StatusPill status={product.status} label={productStatus[product.status]} /><p>本轮执行范围在下方确认。</p><details><summary>产品历史 · {productRounds.length} 轮</summary>{productRounds.map(item => <p key={item.id}>{item.product_snapshot.developer_mode ? '当前固件' : `${item.change_number}/PS${item.patchset_number}`} · {roundStatus[item.status]}</p>)}</details></aside><div>
      <section className="panel"><h3>执行模式</h3><label><input type="checkbox" checked={developerMode} onChange={event=>{const quick=event.target.checked;setDeveloperMode(quick);setExecuteBasic(true);setFlashFirmware(!quick);setSelectedCases([]);}} />开发者快速测试：使用当前模组固件，跳过编译与烧录</label>{developerMode && <p>保存实际固件版本、设备身份和用例版本。模式或分辨率切换可能重启模组。</p>}</section>{!developerMode && <section className="panel"><header><h3>01 / 选择测试点</h3><button type="button" onClick={() => {setPointReady(false); setError(null); setDialog('point');}}>登记 / 选用 Patch Set</button></header><div className="tw-point-list">{points.map(item => <label key={item.id}><input type="radio" name="point" required disabled={!item.ready} checked={pointId===item.id} onChange={() => setPointId(item.id)} /><span><strong>{item.change_number} / PS{item.patchset_number} · {item.subject}</strong><small>{item.branch} · {item.commit_sha}</small><small>Owner：{item.owner || '—'} · 提交时间（Patchset 上传）：{formatDate(item.submitted_at)}</small></span></label>)}</div>{!points.length && <p>此产品尚无测试点。</p>}</section>}
      {!developerMode && <section className="panel"><h3>02 / 构建与镜像</h3><Facts items={[["Board Config",product.board_config],["配置方式","./build.sh lunch，由服务端输入对应菜单序号"],["构建命令",isRv1126b ? "./build.sh all" : "./build.sh"],["镜像目录",product.sdk_repository.artifact_root],["镜像规则",isRv1126b ? "oem.img / system.img / boot.img" : "update.img"]]} /><p>提交后由服务端核验源码、编译并检查本轮新产物。</p></section>}
      <section className="panel"><h3>03 / 选择并检查测试台</h3><label>接收节点<select required value={benchId} onChange={event => {setBenchId(event.target.value);setBenchReady(null); setDeviceSerial(''); setDevices([]); setDeviceScan('idle'); setDeviceScanTime(null);}}><option value="">请选择已启用的测试台</option>{benches.filter(item => item.enabled).map(item => <option key={item.id} value={item.id}>{item.name} · {item.user}@{item.host}</option>)}</select></label>{benchId && <p><code>{benches.find(item => item.id===benchId)?.remote_root}</code></p>}<p>启用状态不代表在线；请以就绪检查和设备检测结果为准。</p>{benchId && isRv1126b && !developerMode && <BenchReadiness key={`${benchId}-${executeBasic}`} benchId={benchId} full={executeBasic} onResult={setBenchReady} />}{!developerMode && <label>执行范围<select value={executeBasic ? 'full' : 'build'} onChange={event=>{const full=event.target.value==='full';setBenchReady(null);setExecuteBasic(full);setFlashFirmware(full);}}><option value="full">完整验证：编译、烧录、启动确认、执行用例</option><option value="build">仅构建并交付：不烧录、不测试</option></select></label>}{(flashFirmware || developerMode) && <><p>检测位置：{benchId ? `${benches.find(item=>item.id===benchId)?.host} 的 USB 接口（175 通过 SSH 查询）` : '请先选择测试台'}</p><button type="button" className="secondary-button" disabled={busy || !benchId || deviceScan==='loading'} onClick={()=>setDeviceScanRevision(value=>value+1)}>{deviceScan==='loading'?'正在检测远端设备…':isRv1126b ? '重新检测 RMSL321' : '重新检测 Loader / Maskrom 设备'}</button><p role="status" aria-live="polite">{!benchId?'请先选择测试台，选中后自动检测。':deviceScan==='loading'?'正在通过 SSH 查询测试台上的 USB 设备…':deviceScan==='done'?`已检测到 ${devices.length} 台设备 · ${formatDate(deviceScanTime)}${devices.length===1?' · 已自动选中':''}`:deviceScan==='error'?'检测失败':''}</p>{deviceScan==='done' && devices.length===0 && <p className="tw-warning">{isRv1126b ? '测试台连接正常，未发现可读取 CPU 序列号的 RMSL321。请连接模组，等待正常 UVC 启动后重新检测。' : '测试台连接正常，未发现 Loader / Maskrom 设备。请进入烧录模式后重新检测。'}</p>}{deviceScan==='error' && <p className="tw-warning" role="alert">{deviceError}</p>}<label>被测设备<select required disabled={!benchId || deviceScan!=='done' || devices.length===0} value={deviceSerial} onChange={event=>setDeviceSerial(event.target.value)}><option value="">{deviceScan==='loading'?'正在检测…':deviceScan==='done' && !devices.length?'未发现烧录设备':'请选择被测设备'}</option>{devices.map(device=><option key={device.serial} value={device.serial}>{device.serial} · {device.mode}</option>)}</select></label>{developerMode && <p>当前固件：{devices.find(d=>d.serial===deviceSerial)?.version || '待检测'}</p>}<p>{developerMode ? '测试当前模组固件，记录完整版本字符串和 CPU 序列号。' : isRv1126b ? 'RMSL321 V2.0：锁定 CPU 序列号和物理 USB 端口，DFU 写入另一槽的 oem/system/boot，保留标定与用户数据。烧录后必须验证本轮 commit 和启动槽。' : 'RK3572：Controller 将核对 Loader 序列号、芯片和 update.img。'}</p>{isRv1126b && !developerMode && <label className="wb-flash-toggle"><input type="checkbox" checked={wearOverride} onChange={event=>setWearOverride(event.target.checked)} />本轮允许超过开发烧写次数上限（计数及豁免仍记录）</label>}</>}</section>
      <section className="panel"><h3>04 / 选择用例与参数</h3><p>{developerMode ? '确认当前固件后执行选中的用例；工厂与持久化用例可能重启模组并写入后恢复测试数据。' : executeBasic ? '烧录并验证版本后，按确认区列出的固定顺序执行所选用例。' : '本轮仅保存用例计划，结果保持未测。'}</p>{executeBasic && <p>切换与信息上报用例记录断言和原始证据；需开流的项目保存帧与图片。</p>}{(['basic','special'] as const).map(group => <div key={group}><h4>{group==='basic'?'基本功能 set':'特殊场景 set'}</h4>{orderedCases.filter(item => item.category === group).map(item => <div className="wb-case-select" key={item.id}><label><input type="checkbox" disabled={(executeBasic && !basicDefinitions.some(d=>d.id===item.code)) || (developerMode && (!(item.code.startsWith("SWITCH-") || item.code.startsWith("INFO-") || item.code.startsWith("FILE-") || item.code.startsWith("FACTORY-") || item.code==='STREAM-RGB-DEPTH') || item.code==="SWITCH-ROTATE-CONTROL"))} checked={selectedCases.includes(item.id)} onChange={event => setSelectedCases(current => event.target.checked ? [...current,item.id] : current.filter(id=>id!==item.id))} /><span><strong>{item.code} · {item.name}</strong><small>{item.description}</small>{executeBasic && !basicDefinitions.some(d=>d.id===item.code) && <small>暂不可选：尚未加载到此用例的执行定义，页面每 10 秒自动同步。</small>}</span></label>{selectedCases.includes(item.id) && item.code==='STREAM-RGB-DEPTH' && <label>数据流参数 JSON（默认正式测试 18000 秒；预热不计入）<textarea rows={5} placeholder={'{"duration_seconds":18000,"warmup_seconds":10,"rgb_width":640,"rgb_height":540,"depth_width":640,"depth_height":352,"fps":10,"thresholds":{}}'} value={parameters[item.id] || ''} onChange={event=>setParameters(current=>({...current,[item.id]:event.target.value}))}/><small>thresholds 可分别设置 rgb、depth：min_fps、max_transport_drop_percent、max_latency_mean_ms、max_latency_max_ms、max_interval_jitter_ms、max_latency_jitter_ms；延时为同步后估算值，单位 ms。</small></label>}{selectedCases.includes(item.id) && ['INFO-PING','INFO-SENSORS'].includes(item.code) && <label>{item.code==='INFO-PING' ? 'RTT 阈值参数（默认 50 ms，每次均需达标）' : '观测窗口与 FPS 容差（默认 5 秒、±10%）'}<textarea rows={2} placeholder={item.code==='INFO-PING' ? '{"max_rtt_ms":50}' : '{"window_seconds":5,"fps_tolerance_percent":10}'} value={parameters[item.id] || ''} onChange={event=>setParameters(current=>({...current,[item.id]:event.target.value}))} /></label>}{selectedCases.includes(item.id) && item.code==='BASIC-FOV-WRITE' && <label>FOV 参数 JSON（留空自动选择支持值并恢复）<textarea rows={2} placeholder='{"target_fov":63.06}' value={parameters[item.id] || ''} onChange={event=>setParameters(current=>({...current,[item.id]:event.target.value}))} /></label>}</div>)}{!cases.some(item => item.category===group) && <p className="quiet-row">此分类暂无已接入用例。新增能力须先实现执行器、断言与恢复逻辑。</p>}</div>)}</section>
      <section className="panel"><h3>05 / 确认并启动</h3><p>本轮用例顺序：{orderedCases.filter(c=>selectedCases.includes(c.id)).map(c=>c.name).join(" → ") || "尚未选择用例"}</p><p>{developerMode ? "服务端保存当前固件、设备和用例版本，测试台校验测试包后执行；完成后将图片和原始帧同步到 Hub。" : "服务端保存配置，交付 JSON 和镜像并校验 SHA-256。"}</p><p className="tw-warning">{developerMode?'本轮不编译、不烧录；直接执行所选用例并上传图片。':executeBasic?'本轮执行烧录、启动身份确认和选中的基础用例。':flashFirmware?'本轮交付后执行固件烧录；已选 Test Case 保存为计划，不执行用例。':'本轮仅交付文件，不烧录、不执行 Test Case。'}</p><button type="submit" className="primary-button" disabled={busy || (!developerMode && !pointId) || !benchId || !selectedCases.length || ((flashFirmware || developerMode) && !deviceSerial) || (executeBasic && !flashFirmware && !developerMode) || (!developerMode && product.status!=='ready') || (!developerMode && isRv1126b && (benchReady?.bench_id!==benchId || !benchReady.passed))}>{busy ? '正在创建…' : (developerMode?'测试当前固件':flashFirmware?'启动完整测试':'开始构建并交付')}</button></section>
    </div></div></form>}
    {screen === 'round-detail' && round && <>
      <div className="tw-heading"><div><button className="tw-back" onClick={() => setScreen('rounds')}>← Test Rounds</button><span className="panel-label">TEST-{String(round.display_number).padStart(6,'0')}</span><h2>{String(round.product_snapshot.name)} · {round.product_snapshot.developer_mode ? '开发者快速测试' : `${round.change_number}/PS${round.patchset_number}`} </h2></div><StatusPill status={round.status} label={roundStatus[round.status] || round.status} /></div>
      {round.status==='created' && <button className="primary-button" disabled={busy} onClick={() => void perform(async()=>{await request(`/api/qa/test-rounds/${round.id}/start`,{method:'POST'});await refresh();})}>开始交付</button>}
      {round.last_error && <div className="run-error" role="alert"><strong>流程未完成</strong><p>{round.last_error}</p></div>}
      {round.status==='remote_ready' && <div className="wb-receipt"><strong>远端文件校验通过</strong><span>JSON 与 {round.image_snapshot.length} 个镜像已交付 · {formatDate(round.delivery_result?.verified_at)}</span></div>}
      <RoundResults key={round.id} round={round} onEvidence={setEvidenceCase} onChanged={refresh} />{evidenceCase && <BasicEvidence key={`${round.id}-${evidenceCase}`} round={round} code={evidenceCase} />}<div className="wb-round-layout"><aside className="panel"><h3>本轮固定快照</h3><Facts items={[["产品",round.product_snapshot.name],["Gerrit Project",round.gerrit_project],["提交说明",commitComment(round)],["Board Config",round.board_config],["测试台",round.bench_snapshot.name || '历史记录未保存快照'],["SSH",round.bench_snapshot.host ? `${round.bench_snapshot.user}@${round.bench_snapshot.host}:${round.bench_snapshot.port}` : '—'],["测试台任务目录",round.remote_path],["Test Cases",round.test_case_snapshot.length]]} /><Evidence value={{commit:round.commit_sha,product:round.product_snapshot}} label="完整版本与产品基线" /><Evidence value={round.test_case_snapshot} label="用例与参数快照" /></aside><div><details className="panel wb-process-details"><summary>执行过程记录 · {round.events.length} 条事件</summary><p>服务端在阶段开始、结束及收到 Controller 回报时保存事件，按序号展示。此记录用于定位流程问题。</p><div className="wb-events">{[...round.events].sort((a,b)=>a.sequence-b.sequence).map(event=><article key={event.id}><span className={`wb-event-mark ${event.status}`} /><div><header><strong>{({created:'已保存本轮配置与版本快照',checking_out:'同步并检出 Patchset',building:'执行 SDK 构建命令',packaging:'校验镜像并生成 JSON',transferring:'传输 JSON 与镜像',verifying:'远端文件校验'} as Record<string,string>)[event.event_type] || event.message}</strong><time>{formatDate(event.created_at)}</time></header><small>{({running:'进行中',succeeded:'已完成',failed:'失败',blocked:'已阻塞'} as Record<string,string>)[event.status] || event.status}{typeof event.details.finished_at==='string' && ` · 结束于 ${formatDate(event.details.finished_at)}`}</small>{Object.keys(event.details).length>0 && <Evidence value={event.details} />}</div></article>)}</div></details>
      <section className="panel"><h3>本轮报告</h3><p>源码：{commitComment(round)} · {round.product_snapshot.developer_mode ? '开发者快速测试' : `${round.change_number}/PS${round.patchset_number}`} </p><p>自动执行结论：{roundStatus[round.status] || round.status}</p><button className="secondary-button" disabled={busy} onClick={()=>void downloadReport(round)}>{busy?'正在生成报告…':'下载完整 JSON 报告'}</button></section>{round.controller_result && <details className="panel wb-process-details"><summary>测试台 Controller · 连接与原始结果</summary><Facts items={[["阶段",round.controller_result.phase],["状态",round.controller_result.state],["说明",round.controller_result.message],["心跳",formatDate(round.controller_result.heartbeat_at as string)],["连接",round.controller_result.transport_error || '最近一次接收正常']]} /><p>{round.product_snapshot.developer_mode ? '当前固件身份验证' : '烧录后启动验证'}：{(round.product_snapshot.developer_mode ? round.controller_result.identity_verified : round.controller_result.boot_verified)===true?'已确认':'尚未确认'}</p><pre className="wb-build-log">{String(round.controller_result.log_tail || '')}</pre><Evidence value={round.controller_result} label="Controller result.json" /></details>}<details className="panel wb-process-details" open={round.status==='building'}><summary>构建证据与镜像</summary>{Boolean(round.build_result?.progress) && <><p><code>{String(round.build_result?.command || (round.build_result?.progress as Record<string,unknown>)?.command || '')}</code></p><pre className="wb-live-log" aria-label="轮次构建实时日志">{String(round.build_result?.stdout_tail || (round.build_result?.progress as Record<string,unknown>)?.logs || '')}</pre></>}{round.build_result ? <><Facts items={[["命令退出码",round.build_result.exit_code],["产物校验",round.build_result.artifact_validation_passed===true?'通过':round.build_result.artifact_validation_passed===false?'未通过':'待校验'],["构建结果",round.build_result.passed===true?'通过':round.build_result.passed===false?'未通过':'进行中']]} /><Evidence value={round.build_result} label="命令、输出和退出码" /></> : <p>{round.product_snapshot.developer_mode ? '开发者模式已跳过构建与烧录。' : '等待服务端执行构建。'}</p>}{round.image_snapshot.map((item,index)=><Facts key={index} items={[["镜像",item.filename],["字节数",item.size_bytes],["SHA-256",item.sha256]]} />)}</details>
      {round.generated_config && <section className="panel"><h3>交付文件</h3><Evidence value={round.generated_config} label="test-run.json" />{round.delivery_result && <Evidence value={round.delivery_result} label="远端文件摘要校验结果" />}<p>{round.product_snapshot.execute_test_cases ? "本轮包含基础测试；执行结果和协议证据见上方。" : "本轮用例仅保存为计划，未执行测试。"}</p></section>}</div></div>
    </>}
    {screen === 'cases' && <><div className="tw-heading"><div><h2>用例库</h2><p>场景覆盖计划与已接入执行用例分开显示。每轮固定定义、工具版本、参数和判定阈值。</p></div><button disabled={busy} onClick={()=>void perform(async()=>{const result=await request<{added:string[]}>('/api/qa/basic-case-definitions/install',{method:'POST'});await refresh();setNotice(result.added.length?`已加入 ${result.added.length} 个内置用例。`:'内置用例已齐全，未重复添加。');})}>同步内置用例</button></div><ScenarioCatalog /><h3>已登记执行用例</h3><div className="tw-case-grid">{orderedCases.map(item=><article className="panel" key={item.id}><header><strong>{item.code}</strong><StatusPill status={item.risk_level} label={riskLabels[item.risk_level]} /></header><h3>{item.name}</h3><p>{item.description}</p><p>{basicDefinitions.some(d=>d.id===item.code)?'可执行 · v1.0.0':'尚未接入执行'}</p>{(()=>{const latest=rounds.find(r=>Array.isArray(r.controller_result?.cases) && (r.controller_result!.cases as {id:string;outcome?:string}[]).some(c=>c.id===item.id && c.outcome));const outcome=latest && (latest.controller_result!.cases as {id:string;outcome?:string}[]).find(c=>c.id===item.id)?.outcome;return latest?<p>最近实测：{outcome==='passed'?'PASS':outcome==='failed'?'FAIL':'ERROR'} · TEST-{String(latest.display_number).padStart(6,'0')}<button onClick={()=>openRound(latest)}>查看该轮证据</button></p>:<p>尚无实测结果</p>;})()}<code>{item.command}</code>{basicDefinitions.find(d=>d.id===item.code) ? <Evidence value={basicDefinitions.find(d=>d.id===item.code)} label="用例定义、协议入口与测试文件" /> : <p>执行适配器未接入</p>}<label>所属 set<select value={item.category} disabled={busy} onChange={event => {const category=event.target.value; void perform(async () => {await request(`/api/qa/test-cases/${item.id}/category`, {method:'PATCH', body:JSON.stringify({category})}); await refresh();});}}><option value="basic">基本功能 set</option><option value="special">特殊场景 set</option></select></label><small>预期 exit {item.expected_exit_code} · {item.timeout_seconds}s</small></article>)}</div>{!cases.length && <p className="quiet-row">暂无 Test Case。</p>}</>}
    {screen === 'benches' && <><div className="tw-heading"><div><h2>测试台节点</h2><p>175 服务端使用已配置的 SSH 密钥连接节点。</p></div><button className="primary-button" onClick={()=>setDialog('bench')}>＋ 登记节点</button></div><div className="tw-bench-grid">{benches.map(item=><article className="panel" key={item.id}><header><h3>{item.name}</h3><StatusPill status={item.enabled?'ready':'blocked'} label={item.enabled?'已启用':'已停用'} /></header><Facts items={[["SSH",`${item.user}@${item.host}:${item.port}`],["测试台根目录",item.remote_root]]} /><BenchReadiness benchId={item.id} /></article>)}</div>{!benches.length && <p className="quiet-row">暂无测试台节点。</p>}</>}
    {dialog && <div className="dialog-backdrop"><section className="dialog qa-dialog" role="dialog" aria-modal="true" aria-labelledby="wb-dialog-title"><div className="dialog-heading"><h2 id="wb-dialog-title">{dialog==='point'?'登记 Patchset':dialog==='bench'?'登记测试台':'新建 Test Case'}</h2><button aria-label="关闭" disabled={busy} onClick={()=>setDialog(null)}>×</button></div>{error && <p role="alert" className="tw-warning">{error}</p>}<form onSubmit={saveDialog}>
      {dialog==='point' ? product && <GerritPatchsetFields product={product} onReady={setPointReady} busy={busy} /> : dialog==='bench' ? <><label>节点名称<input name="name" required /></label><label>主机<input name="host" required /></label><label>SSH 端口<input name="port" type="number" min="1" max="65535" defaultValue="22" required /></label><label>用户名<input name="user" required /></label><label>测试台根目录<input name="remote_root" required placeholder="/home/linaro/develop-hub-qa" /></label></> : <><label>用例编号<input name="code" required /></label><label>名称<input name="name" required /></label><label>说明<textarea name="description" required /></label><label>命令<textarea name="command" required /></label><label>所属 set<select name="category"><option value="basic">基本功能 set</option><option value="special">特殊场景 set</option></select></label><label>风险级别<select name="risk_level"><option value="read_only">只读</option><option value="state_change">状态变更</option><option value="firmware_write">固件写入</option></select></label><div className="form-columns"><label>预期退出码<input name="expected_exit_code" type="number" defaultValue="0" required /></label><label>超时（秒）<input name="timeout_seconds" type="number" min="1" defaultValue="300" required /></label></div></>}
      <div className="form-actions"><button type="button" disabled={busy} onClick={()=>setDialog(null)}>取消</button><button type="submit" className="approve" disabled={busy || (dialog==='point' && !pointReady)}>{busy?'正在保存…':'保存'}</button></div></form></section></div>}
  </section>;
}
