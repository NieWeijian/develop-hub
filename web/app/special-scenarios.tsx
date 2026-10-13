'use client';

import { FormEvent, useEffect, useRef, useState } from 'react';
import './special-scenarios.css';

type Product = { id: string; name: string; board_config: string };
type Bench = { id: string; name: string; host: string; enabled: boolean };
type Device = { serial: string; port?: string; mode?: string; version?: string };
type Field = { name: string; label: string; type: 'text' | 'number' | 'boolean' | 'select' | 'textarea' | 'file'; required?: boolean; must_be_true?: boolean; default?: string | number | boolean; min?: number; max?: number; pattern?: string; options?: { value: string; label: string }[]; help?: string };
type Step = { id: string; title: string; instruction: string; kind: 'manual' | 'automatic'; operation?: string; fields: Field[]; records: string[]; risk?: string };
export type Definition = { code: string; name: string; version: string; description: string; steps: Step[] };
type StepRecord = { step_id: string; status: string; operator?: string; values?: Record<string, unknown>; started_at?: string; finished_at?: string; progress?: Record<string, unknown>; result?: Record<string, unknown> };
export type Run = { validation_plan_id?:string|null;requirement_id?:string|null;candidate_id?:string|null; id: string; created_by?: string; scenario_code: string; status: string; revision: number; current_step_index: number; current_step_id: string | null; definition_snapshot: Definition; product_snapshot: Record<string, unknown>; bench_snapshot: Record<string, unknown>; device_snapshot: Record<string, unknown>; tool_snapshot?: Record<string, unknown>; steps: StepRecord[]; events: Record<string, unknown>[]; context?: Record<string, unknown>; bench_reserved: boolean; hardware_active?: boolean; created_at: string; completed_at?: string; last_error?: string | null };
type Api = <T>(path: string, init?: RequestInit) => Promise<T>;
type Values = Record<string, string | number | boolean>;
type UploadedLog = { artifact_id: string; filename: string; size_bytes: number; sha256: string; download_url: string };
const labels: Record<string, string> = { preparing_firmware: '准备候选固件', awaiting_operator: '等待操作', waiting: '等待操作', pending: '尚未执行', running: '执行中', passed: '通过', failed: '失败', blocked: '已阻塞', interrupted: '执行中断', cancelled: '已取消', cancelling: '正在停止' };
const terminal = ['passed', 'failed', 'blocked', 'interrupted', 'cancelled'];
function date(value?: string) { return value ? new Date(/(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? value : value + 'Z').toLocaleString('zh-CN', { hour12: false }) : '—'; }
function errorMessage(error: unknown) { return error instanceof Error ? error.message : String(error); }
function Status({ value }: { value: string }) { return <span className={'special-status special-status-' + value}>{labels[value] || value}</span>; }
function Evidence({ value, label }: { value: unknown; label: string }) { return <details className="special-evidence"><summary>{label}</summary><pre>{JSON.stringify(value, null, 2)}</pre></details>; }
function PreparedEvidence({ record }: { record?: StepRecord }) {
  const [copyNotice, setCopyNotice] = useState('');
  const data = record?.result?.data as Record<string, unknown> | undefined;
  const commands = Array.isArray(data?.instructions) ? data.instructions.filter((item): item is string => typeof item === 'string') : [];
  const ports = Array.isArray(data?.available) ? data.available as { path: string; resolved?: string; readable?: boolean; writable?: boolean }[] : null;
  if (!commands.length && !ports) return null;
  return <div className="special-prepared">
    {commands.length ? <><h4>本轮 OTA 执行指令</h4><p>在 3588 上按顺序执行。先核实目标 ADB 设备并替换占位符，完整保留日志；测试包先传，MD5 触发文件最后传。</p><pre tabIndex={0}>{commands.join('\n')}</pre><button type="button" onClick={async () => { try { await navigator.clipboard.writeText(commands.join('\n')); setCopyNotice('指令已复制。'); } catch { setCopyNotice('浏览器不支持复制，请选中上方指令手动复制。'); } }}>复制本轮指令</button><small role="status">{copyNotice}</small></> : null}
    {ports ? <><h4>3588 串口检测结果</h4>{ports.length ? <ul>{ports.map(port => <li key={port.path}><code>{port.path}</code><small>{port.readable && port.writable ? '可读写' : '请检查设备权限'}{port.resolved && port.resolved !== port.path ? ' · ' + port.resolved : ''}</small></li>)}</ul> : <p>当前未检测到 USB 串口。可停止后接入适配器重新创建测试，或在本步选择人工采集。</p>}</> : null}
  </div>;
}
type EvidenceFile = { path?: string; role?: string; kind?: string; size_bytes?: number; sha256?: string; artifact_id?: string; download_url?: string; archived?: boolean; remote_only?: boolean; remote_path?: string };
type PressureData = { target_bytes?: number; successful_bytes?: number; files_written?: string[]; errors?: { name?: string; response?: { exit_code?: number; stderr?: string; stdout?: string } }[] };
function bytes(value?: number) { return typeof value === 'number' && Number.isFinite(value) ? (value / (1024 * 1024)).toFixed(2) + ' MiB（' + value.toLocaleString('zh-CN') + ' 字节）' : '—'; }
function LogPressureSummary({ record }: { record?: StepRecord }) {
  const data = record?.result?.data as PressureData | undefined;
  if (!data || typeof data.target_bytes !== 'number' || typeof data.successful_bytes !== 'number') return null;
  const errors = Array.isArray(data.errors) ? data.errors : [];
  return <div className="special-pressure-result">
    <h4>日志写入结果</h4>
    <dl><div><dt>成功写入</dt><dd>{bytes(data.successful_bytes)}</dd></div><div><dt>本轮目标</dt><dd>{bytes(data.target_bytes)}</dd></div><div><dt>成功文件数</dt><dd>{Array.isArray(data.files_written) ? data.files_written.length : '—'}</dd></div></dl>
    {errors.length ? <div className="special-write-error"><strong>写入失败 · {errors.length} 次</strong>{errors.map((error, index) => <div key={index}><p>{error.name || '文件写入'}{typeof error.response?.exit_code === 'number' ? ' · 退出码 ' + error.response.exit_code : ''}</p><pre>{(error.response?.stderr?.trim() || error.response?.stdout?.trim() || '工具未返回错误详情').slice(0, 1600)}</pre></div>)}</div> : null}
  </div>;
}
function ResultSummary({ record, apiBase }: { record: StepRecord; apiBase: string }) {
  const assertions = Array.isArray(record.result?.assertions) ? record.result.assertions as { name?: string; passed?: boolean }[] : [];
  const files = Array.isArray(record.result?.files) ? record.result.files as EvidenceFile[] : [];
  return <>
    <LogPressureSummary record={record} />
    {assertions.length ? <ul className="special-assertions">{assertions.map((item, index) => <li key={index}><Status value={item.passed === true ? 'passed' : 'failed'} /><span>{item.name || '断言 ' + (index + 1)}</span></li>)}</ul> : null}
    {files.length ? <div className="special-files"><h4>证据文件</h4>{files.map((file, index) => <div key={file.artifact_id || index}><strong>{file.path || file.role || file.kind || '文件'}</strong><small>{typeof file.size_bytes === 'number' ? file.size_bytes.toLocaleString('zh-CN') : '—'} 字节 · {file.role || file.kind || '执行证据'}</small>{file.archived && file.download_url?.startsWith('/api/qa/special-runs/') ? <a href={apiBase + file.download_url} download>下载原始文件</a> : file.remote_only ? <span>原始文件保留在 3588，未回传 Hub</span> : <span>尚未归档</span>}{file.remote_only && file.remote_path ? <small className="special-file-location">3588 路径：<code>{file.remote_path}</code></small> : null}{file.sha256 ? <small className="special-file-location">SHA-256：<code>{file.sha256}</code></small> : null}</div>)}</div> : null}
  </>;
}
function readDraft(key: string): { operator?: string; values?: Values } {
  try { if (typeof window !== 'undefined') return JSON.parse(sessionStorage.getItem(key) || '{}'); } catch { /* The server remains authoritative when storage is unavailable. */ }
  return {};
}
function defaults(step: Step): Values { return Object.fromEntries(step.fields.filter(field => field.default !== undefined || field.type === 'boolean').map(field => [field.name, field.default ?? false])) as Values; }

function ManualLogUpload({ run, field, operator, value, apiBase, onUploaded, onUpload }: { run: Run; field: Field; operator: string; value: string; apiBase: string; onUploaded: (id: string) => void; onUpload: (file: File, operator: string) => Promise<UploadedLog> }) {
  const [uploaded, setUploaded] = useState<UploadedLog | null>(null);
  const [error, setError] = useState('');
  const [uploading, setUploading] = useState(false);
  const restored = (run.context?.manual_uploads as Record<string, UploadedLog> | undefined)?.[value];
  const saved = uploaded?.artifact_id === value ? uploaded : restored;
  async function upload(file?: File) {
    if (!file || uploading) return;
    if (!operator.trim()) { setError('请先填写本步操作者，再选择日志文件。'); return; }
    if (file.size === 0 || file.size > 64 * 1024 * 1024) { setError('请选择非空的完整原始日志，文件上限为 64 MiB。'); return; }
    setError(''); setUploading(true);
    try { const result = await onUpload(file, operator); setUploaded(result); onUploaded(result.artifact_id); }
    catch (cause) { setError(errorMessage(cause)); }
    finally { setUploading(false); }
  }
  return <div className="special-upload">
    <label>{field.label}<input type="file" disabled={uploading || !operator.trim()} accept=".log,.txt,text/plain,application/octet-stream" onChange={event => { const file = event.currentTarget.files?.[0]; event.currentTarget.value = ''; void upload(file); }} /></label>
    <small>{field.help || '选择本轮完整原始日志，最大 64 MiB。上传文件和下方粘贴日志二选一；开始/结束时间仍需填写。'}</small>
    {!operator.trim() ? <small>填写操作者后可上传。</small> : null}
    {uploading ? <p role="status">正在上传完整日志，请等待完成…</p> : null}
    {error ? <p role="alert" className="special-alert">{error}</p> : null}
    {value ? <div className="special-upload-saved"><strong>{saved?.filename || '已关联上传日志'}</strong>{saved ? <><small>{saved.size_bytes.toLocaleString('zh-CN')} 字节</small><small>SHA-256：<code>{saved.sha256}</code></small>{saved.download_url?.startsWith('/api/qa/special-runs/') ? <a href={apiBase + saved.download_url} download>下载已上传日志</a> : null}</> : <small>已保存文件引用，将在本步提交时使用。</small>}<button type="button" onClick={() => { onUploaded(''); setUploaded(null); }}>改用粘贴日志</button></div> : null}
  </div>;
}

function StepGate({ run, step, busy, apiBase, onAdvance, onUpload }: { run: Run; step: Step; busy: boolean; apiBase: string; onAdvance: (operator: string, values: Values) => Promise<boolean>; onUpload: (file: File, operator: string) => Promise<UploadedLog> }) {
  const draftKey = 'develop-hub:special-draft:v1:' + run.id + ':' + step.id;
  const [operator, setOperator] = useState(() => readDraft(draftKey).operator || run.steps.find(record => record.operator)?.operator || run.created_by || '');
  const [values, setValues] = useState<Values>(() => ({ ...defaults(step), ...readDraft(draftKey).values }));
  const [confirmed, setConfirmed] = useState(false);
  const [saveNotice, setSaveNotice] = useState('');
  const enabled = run.status === 'awaiting_operator' || run.status === 'waiting';
  const confirmationField = step.fields.find(field => field.name === 'confirmed' && field.type === 'boolean');
  const currentRecord = run.steps.find(record => record.step_id === step.id);
  const captureReady = currentRecord?.progress?.capture_ready === true;
  const manualCapture = run.steps.find(record => record.step_id === 'configure')?.values?.capture_mode === 'manual';
  useEffect(() => {
    try { sessionStorage.setItem(draftKey, JSON.stringify({ operator, values })); } catch { /* Local drafts are optional. */ }
  }, [draftKey, operator, values]);
  function change(name: string, value: string | number | boolean) {
    setValues(previous => {
      const next = { ...previous, [name]: value };
      if (name === 'manual_log_artifact_id' && value) next.manual_log = '';
      if (name === 'manual_log' && value) next.manual_log_artifact_id = '';
      return next;
    });
    setConfirmed(false); setSaveNotice('');
  }
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!enabled || busy || !confirmed) return;
    if (await onAdvance(operator, values)) { try { sessionStorage.removeItem(draftKey); } catch { /* Optional browser cache. */ } }
  }
  const immediatePower = run.scenario_code === 'SPECIAL-SN-POWER-CYCLE' && /拔|断电/.test(step.instruction);
  return <section className={'panel special-gate' + (immediatePower ? ' special-gate-urgent' : '')} aria-labelledby="special-current-title">
    <div className="special-kicker">当前第 {run.current_step_index + 1} 步 · {step.kind === 'automatic' ? '确认后由测试台执行' : '人工操作并留档'}</div>
    <h3 id="special-current-title">{step.title}</h3>
    <p className="special-instruction">{step.instruction}</p>
    {immediatePower && step.id === 'set_sn' ? <p className="special-alert"><strong>先填写 SN 并确认写入；等页面提示写入完成后立即拔下电源，不要提前拔线。</strong></p> : immediatePower && step.id === 'verify_disconnected' ? <p className="special-alert"><strong>SN 写入已完成，请立即拔下模组电源。</strong>保持断电，确认已拔下后系统验证掉线；直至界面要求重新插入。</p> : null}
    {run.scenario_code === 'FACTORY-AGING' && step.operation === 'aging_capture' && !manualCapture ? <p className="special-alert" role="status"><strong>{captureReady ? '串口采集已就绪，现在将模组接入老化座上电。' : run.status === 'running' ? '正在准备串口采集，请勿给老化座上电。' : '确认后先等待“串口采集已就绪”，再给老化座上电。'}</strong>{captureReady ? '保持串口连接。采集开始：' + date(String(currentRecord?.progress?.capture_started_at || '')) : '界面会在串口实际打开后提示上电。'}</p> : null}
    {run.scenario_code === 'FACTORY-AGING' && step.id === 'aging_verify' && manualCapture ? <p className="special-alert"><strong>请先在自己的串口工具启动完整原始日志保存，再给老化座上电。</strong>本轮老化结束后，将完整日志和起止时间填写到下面表单。</p> : null}
    {run.scenario_code === 'FACTORY-AGING' && step.id === 'configure' ? <PreparedEvidence record={run.steps.find(record => record.step_id === 'serial_probe')} /> : null}
    {run.scenario_code === 'SPECIAL-OTA' && ['ota_start', 'ota_status'].includes(step.id) ? <PreparedEvidence record={run.steps.find(record => record.step_id === 'ota_prepare')} /> : null}
    {run.scenario_code === 'FACTORY-AGING' && ['serial_probe', 'configure'].includes(step.id) ? <p className="special-hint">串口适配器接到 3588 的 USB 口，建议使用 FTDI 或 CP210x；模组 TX 接适配器 RX，并共地。确认串口电平匹配后连接，先不启动老化。</p> : null}
    {step.risk ? <p className="special-alert">{step.risk}</p> : null}
    <div className="special-records"><strong>本步需要记录</strong><ul>{step.records.map(record => <li key={record}>{record}</li>)}</ul><small>{(run.tool_snapshot?.evidence_policy as { mode?: string } | undefined)?.mode === 'remote_retained_v1' ? 'Hub 保存判定结果与摘要；原始协议保留在 3588，可在步骤证据中查看路径。' : '工具输出保留原始证据，人工填写按人工证据保存。'}</small></div>
    <form onSubmit={submit}>
      <fieldset disabled={busy || !enabled} className="special-fields">
        <label>本步操作者<input name="operator" autoComplete="name" value={operator} required maxLength={120} onChange={event => setOperator(event.target.value)} placeholder="姓名或工号" /></label>
        {step.fields.filter(field => field !== confirmationField && (field.type !== 'file' || manualCapture)).map(field => field.type === 'file' ? <ManualLogUpload key={field.name} run={run} field={field} operator={operator} value={String(values[field.name] || '')} apiBase={apiBase} onUploaded={id => change(field.name, id)} onUpload={onUpload} /> : field.type === 'boolean' ? <label className="special-check" key={field.name}><input type="checkbox" required={field.must_be_true ?? field.required} checked={values[field.name] === true} onChange={event => change(field.name, event.target.checked)} /><span>{field.label}{field.help ? <small>{field.help}</small> : null}</span></label> : <label key={field.name}>{field.label}{field.required ? <span className="special-required">必填</span> : null}
          {field.type === 'select' ? <select value={String(values[field.name] ?? '')} required={field.required} onChange={event => change(field.name, event.target.value)}><option value="">请选择</option>{field.options?.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}</select>
            : field.type === 'textarea' ? <textarea rows={4} value={String(values[field.name] ?? '')} required={field.required} onChange={event => change(field.name, event.target.value)} />
              : <input type={field.type} value={String(values[field.name] ?? '')} required={field.required} min={field.min} max={field.max} step={field.type === 'number' ? 'any' : undefined} onChange={event => change(field.name, field.type === 'number' && event.target.value !== '' ? Number(event.target.value) : event.target.value)} />}
          {field.help ? <small>{field.help}</small> : null}</label>)}
        <label className="special-check special-confirm"><input type="checkbox" required checked={confirmed} onChange={event => { setConfirmed(event.target.checked); if (confirmationField) setValues(previous => ({ ...previous, confirmed: event.target.checked })); }} /><span>{confirmationField ? confirmationField.label : step.kind === 'automatic' ? '已完成本步准备，确认启动上述操作。' : '已按指引完成本步操作，以上记录属实。'}</span></label>
      </fieldset>
      <div className="special-actions"><button className="primary-button" type="submit" disabled={busy || !enabled || !confirmed}>{busy ? '正在提交…' : run.status === 'running' ? '测试台正在执行…' : step.kind === 'automatic' ? '确认并执行本步' : '确认记录，进入下一步'}</button><button type="button" disabled={busy || !enabled} onClick={() => { try { sessionStorage.setItem(draftKey, JSON.stringify({ operator, values })); setSaveNotice('草稿已保存到当前浏览器。'); } catch { setSaveNotice('浏览器存储不可用，请提交本步后再离开。'); } }}>保存填写草稿</button><small role="status">{saveNotice || '已提交步骤保存在 Hub；未提交填写在当前浏览器暂存。'}</small></div>
    </form>
  </section>;
}

function CancelRun({ run, busy, onCancel }: { run: Run; busy: boolean; onCancel: (operator: string, reason: string, stopped: boolean) => Promise<void> }) {
  const [operator, setOperator] = useState('');
  const [reason, setReason] = useState('');
  const [stopped, setStopped] = useState(false);
  if (!run.bench_reserved && terminal.includes(run.status)) return null;
  return <details className="panel special-cancel"><summary>停止本次测试</summary><p>停止后保留已采集记录，未完成步骤不计为通过。硬件任务停止完成后才释放测试台。</p><form onSubmit={event => { event.preventDefault(); void onCancel(operator, reason, stopped); }}><fieldset className="special-fields" disabled={busy || run.status === 'cancelling'}><label>操作者<input required maxLength={120} value={operator} onChange={event => setOperator(event.target.value)} /></label><label>停止原因<textarea rows={2} required maxLength={2000} value={reason} onChange={event => setReason(event.target.value)} /></label>{(run.status === 'interrupted' || run.hardware_active) ? <label className="special-check"><input type="checkbox" required checked={stopped} onChange={event => setStopped(event.target.checked)} /><span>已现场核实测试台工具和硬件操作均已停止，可以释放测试台。</span></label> : null}<button type="submit">{run.status === 'cancelling' ? '等待测试台停止…' : '停止并保留记录'}</button></fieldset></form></details>;
}

export default function SpecialScenarios({ products, benches, api, apiBase, initialProductId, onOpenPlan, onOpenRound }: { products: Product[]; benches: Bench[]; api: Api; apiBase: string; initialProductId?: string; onOpenPlan?: (id:string)=>void; onOpenRound?: (id:string)=>void }) {
  const [definitions, setDefinitions] = useState<Definition[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [run, setRun] = useState<Run | null>(null);
  const [scenarioCode, setScenarioCode] = useState('');
  const [productId, setProductId] = useState(initialProductId || '');
  const [benchId, setBenchId] = useState('');
  const [operator, setOperator] = useState('');
  const [devices, setDevices] = useState<Device[]>([]);
  const [deviceSerial, setDeviceSerial] = useState('');
  const [scan, setScan] = useState<'idle' | 'loading' | 'done' | 'error'>('idle');
  const [scanRevision, setScanRevision] = useState(0);
  const [scanError, setScanError] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [historyFilter, setHistoryFilter] = useState('all');
  const busyRef = useRef(false);
  const activeRunRef = useRef('');
  const rvProducts = products.filter(product => product.board_config.toLowerCase().includes('rv1126b'));
  const selectedProduct = rvProducts.find(product => product.id === productId) || (rvProducts.length === 1 ? rvProducts[0] : undefined);
  const definition = definitions.find(item => item.code === scenarioCode) || definitions[0];
  const currentStep = run?.definition_snapshot.steps.find(step => step.id === run.current_step_id);
  const runId = run?.id;
  const runStatus = run?.status;
  const benchReserved = run?.bench_reserved;
  const releaseBinding=run?.context?.release_binding as {preparation_round_id?:string;candidate_name?:string}|undefined;
  const preparationRoundId=releaseBinding?.preparation_round_id;
  function selectRun(next: Run | null) {
    activeRunRef.current = next?.id || ''; setRun(next);
    const url = new URL(window.location.href);
    if (next) url.searchParams.set('special_run', next.id); else url.searchParams.delete('special_run');
    window.history.replaceState(null, '', url);
  }
  function updateRun(next: Run) {
    if (activeRunRef.current === next.id) setRun(previous => previous?.id === next.id && previous.revision > next.revision ? previous : next);
    setRuns(previous => [next, ...previous.filter(item => item.id !== next.id)].sort((a, b) => b.created_at.localeCompare(a.created_at)));
  }
  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const [catalog, history] = await Promise.all([api<Definition[]>('/api/qa/special-scenarios', { signal: controller.signal }), api<Run[]>('/api/qa/special-runs', { signal: controller.signal })]);
        if (controller.signal.aborted) return;
        setDefinitions(catalog); setRuns(history);
        const locationParams=new URLSearchParams(window.location.search);
        setScenarioCode(locationParams.get('special_case') || '');
        const savedId = locationParams.get('special_run');
        if (savedId) { const saved = await api<Run>('/api/qa/special-runs/' + encodeURIComponent(savedId), { signal: controller.signal }); if (!controller.signal.aborted) selectRun(saved); }
      } catch (cause) { if (!controller.signal.aborted) setError(errorMessage(cause)); }
      finally { if (!controller.signal.aborted) setLoading(false); }
    }
    void load(); return () => controller.abort();
  }, [api]);
  useEffect(() => {
    if (!benchId || runId) return;
    const controller = new AbortController();
    async function detect() {
      setScan('loading'); setScanError(''); setDevices([]); setDeviceSerial('');
      try { const found = await api<Device[]>('/api/qa/benches/' + benchId + '/devices?chip=RV1126B', { signal: controller.signal }); if (!controller.signal.aborted) { setDevices(found); setDeviceSerial(found.length === 1 ? found[0].serial : ''); setScan('done'); } }
      catch (cause) { if (!controller.signal.aborted) { setScan('error'); setScanError(errorMessage(cause)); } }
    }
    void detect(); return () => controller.abort();
  }, [api, benchId, scanRevision, runId]);
  useEffect(() => {
    if (!runId || (terminal.includes(runStatus || '') && !benchReserved)) return;
    const controller = new AbortController();
    let timer: number | undefined;
    async function poll() {
      try { const fresh = await api<Run>('/api/qa/special-runs/' + runId, { signal: controller.signal }); if (!controller.signal.aborted) updateRun(fresh); }
      catch (cause) { if (!controller.signal.aborted) setError(errorMessage(cause)); }
      finally { if (!controller.signal.aborted) timer = window.setTimeout(() => void poll(), 2000); }
    }
    timer = window.setTimeout(() => void poll(), 1500);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [api, runId, runStatus, benchReserved]);
  async function perform(action: () => Promise<void>) {
    if (busyRef.current) return false;
    busyRef.current = true; setBusy(true); setError('');
    try { await action(); return true; }
    catch (cause) {
      setError(errorMessage(cause));
      const id = activeRunRef.current;
      if (id) { try { updateRun(await api<Run>('/api/qa/special-runs/' + id)); } catch { /* Preserve the first actionable error. */ } }
      return false;
    } finally { busyRef.current = false; setBusy(false); }
  }
  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!definition || !selectedProduct) return;
    await perform(async () => { const created = await api<Run>('/api/qa/special-runs', { method: 'POST', body: JSON.stringify({ scenario_code: definition.code, product_id: selectedProduct.id, bench_id: benchId, device_serial: deviceSerial, operator }) }); selectRun(created); updateRun(created); });
  }
  async function advance(stepOperator: string, values: Values) {
    if (!run || !currentStep) return false;
    return perform(async () => updateRun(await api<Run>('/api/qa/special-runs/' + run.id + '/advance', { method: 'POST', body: JSON.stringify({ expected_revision: run.revision, step_id: currentStep.id, operator: stepOperator, values }) })));
  }
  async function uploadManualLog(file: File, stepOperator: string): Promise<UploadedLog> {
    if (!run) throw new Error('请先选择测试记录。');
    let uploaded: UploadedLog | undefined;
    const saved = await perform(async () => {
      uploaded = await api<UploadedLog>('/api/qa/special-runs/' + run.id + '/manual-log?expected_revision=' + run.revision + '&operator=' + encodeURIComponent(stepOperator), { method: 'POST', headers: { 'Content-Type': 'application/octet-stream', 'X-Filename': encodeURIComponent(file.name) }, body: file });
    });
    if (!saved || !uploaded) throw new Error('日志上传未完成，请检查上方错误后重试。');
    return uploaded;
  }
  async function cancel(stepOperator: string, reason: string, stopped: boolean) {
    if (!run) return;
    await perform(async () => updateRun(await api<Run>('/api/qa/special-runs/' + run.id + '/cancel', { method: 'POST', body: JSON.stringify({ expected_revision: run.revision, operator: stepOperator, reason, hardware_stopped: stopped }) })));
  }
  return <div className="special-workbench">
    <div className="tw-heading"><div><span className="panel-label">OPERATOR GATED TESTS</span><h2>特殊场景</h2><p>每步确认后推进，锁定同一模组，保存操作记录与测试证据。</p></div>{run ? <button disabled={busy} onClick={() => { selectRun(null); setError(''); }}>返回场景选择</button> : null}</div>
    {error ? <div className="error-banner" role="alert"><span>{error}</span><button type="button" aria-label="关闭特殊场景错误" onClick={() => setError('')}>×</button></div> : null}
    {loading ? <p role="status" className="panel">正在读取特殊场景与测试记录…</p> : null}
    {!run && !loading ? <>
      <div className="special-scenario-grid" aria-label="选择特殊场景">{definitions.map(item => <button key={item.code} type="button" aria-pressed={definition?.code === item.code} className={'panel special-scenario-card' + (definition?.code === item.code ? ' selected' : '')} onClick={() => setScenarioCode(item.code)}><span className="special-kicker">{item.code}</span><h3>{item.name}</h3><p>{item.description}</p><small>{item.steps.length} 个门控步骤 · 版本 {item.version}</small></button>)}</div>
      {definition ? <form onSubmit={create} className="panel special-create"><h3>准备执行 · {definition.name}</h3>
        <div className="special-form-grid">
          <label>测试产品<select value={selectedProduct?.id || ''} required onChange={event => setProductId(event.target.value)}><option value="">选择 RV1126B 产品</option>{rvProducts.map(product => <option key={product.id} value={product.id}>{product.name}</option>)}</select></label>
          <label>测试台<select value={benchId} required onChange={event => { setBenchId(event.target.value); setScan('idle'); }}><option value="">选择测试台</option>{benches.filter(bench => bench.enabled).map(bench => <option key={bench.id} value={bench.id}>{bench.name} · {bench.host}</option>)}</select></label>
          <label>操作者<input required maxLength={120} autoComplete="name" value={operator} onChange={event => setOperator(event.target.value)} placeholder="姓名或工号" /></label>
        </div>
        <div className="special-actions"><p role="status">{scan === 'loading' ? '正在读取在线模组…' : scan === 'error' ? scanError : scan === 'done' ? '检测到 ' + devices.length + ' 个模组' : '选择测试台后读取模组身份。'}</p><button type="button" disabled={!benchId || busy || scan === 'loading'} onClick={() => setScanRevision(value => value + 1)}>重新检测</button></div>
        <label>被测模组 · CPU 序列号<select required disabled={scan !== 'done' || busy} value={deviceSerial} onChange={event => setDeviceSerial(event.target.value)}><option value="">选择模组</option>{devices.map(device => <option key={device.serial} value={device.serial}>{device.serial} · {device.mode || '在线'}{device.port ? ' · ' + device.port : ''}</option>)}</select></label>
        {deviceSerial ? <p className="special-identity">固件：{devices.find(device => device.serial === deviceSerial)?.version || '由第一步读取并保存'}。本次流程锁定 CPU 序列号；重连后核验同一模组。</p> : null}
        {scan === 'done' && !devices.length ? <p className="special-alert">请连接模组，等待正常 UVC 启动后重新检测。</p> : null}
        <details className="special-preview"><summary>查看完整步骤与记录要求</summary><ol>{definition.steps.map(step => <li key={step.id}><strong>{step.title}</strong><p>{step.instruction}</p><small>记录：{step.records.join('；')}</small></li>)}</ol></details>
        <button className="primary-button" type="submit" disabled={busy || !selectedProduct || !benchId || !deviceSerial || scan !== 'done'}>{busy ? '正在创建…' : '创建测试，进入第一步'}</button><small className="special-create-note">创建后固定场景定义、产品、测试台与设备身份；硬件操作均由后续步骤单独确认。</small>
      </form> : <p className="panel">尚无可用的特殊场景定义。</p>}
    </> : null}
    {run ? <>
      <section className="panel special-run-summary">
        {run.validation_plan_id?<div className="special-plan-binding"><div><strong>发布验证 · 候选固件门控</strong><p>本次证据关联验证清单，步骤完成后由服务器核验并回填结果。{run.scenario_code==='SPECIAL-OTA'?' OTA 需损坏包、错误包名和中断电 3 种异常全部通过。':''}</p></div><button type="button" disabled={busy} onClick={()=>onOpenPlan?.(run.validation_plan_id!)}>返回验证清单 →</button></div>:null}
        {preparationRoundId&&onOpenRound?<p><button type="button" onClick={()=>onOpenRound(preparationRoundId)}>查看候选烧录与版本核验记录 →</button></p>:null}
        <header><div><h3>{run.definition_snapshot.name}</h3><small>{run.id} · 创建于 {date(run.created_at)}</small></div><Status value={run.status} /></header>
        <dl><div><dt>产品</dt><dd>{String(run.product_snapshot.name || '—')}</dd></div><div><dt>测试台</dt><dd>{String(run.bench_snapshot.name || '—')}</dd></div><div><dt>锁定模组</dt><dd>{String(run.device_snapshot.serial || run.device_snapshot.cpu_serial || '已记录身份')}</dd></div><div><dt>测试台占用</dt><dd>{run.bench_reserved ? '本流程占用中' : '已释放'}</dd></div></dl>
        <div className="special-actions"><a className="special-report" href={apiBase + '/api/qa/special-runs/' + encodeURIComponent(run.id) + '/report'} download>下载 JSON 报告</a><button type="button" disabled={busy} onClick={() => void perform(async () => updateRun(await api<Run>('/api/qa/special-runs/' + run.id)))}>刷新执行状态</button><small>修订 {run.revision} · 已确认记录由 Hub 保存，刷新后可继续。</small></div>
        {run.last_error ? <p className={run.status === 'failed' ? 'special-alert special-alert-failed' : 'special-alert'} role="alert">{run.last_error}</p> : null}
      </section>
      <div className="special-pipeline">
        <aside className="panel special-steps" aria-label="测试步骤"><h3>门控流水线</h3><ol>{run.definition_snapshot.steps.map((step, index) => {
          const record = run.steps.find(item => item.step_id === step.id);
          const current = step.id === run.current_step_id;
          return <li key={step.id} aria-current={current ? 'step' : undefined} className={current ? 'current' : ''}><span className="special-step-number">{index + 1}</span><div><strong>{step.title}</strong><small>{step.kind === 'manual' ? '人工确认' : '测试台执行'} · {record?.status && record.status !== 'pending' ? labels[record.status] || record.status : current ? '当前步骤' : index > run.current_step_index ? '等待前一步完成' : '未完成'}</small></div></li>;
        })}</ol><p>后续步骤在当前步骤完成后解锁。</p></aside>
        <div>
          {run.status==='preparing_firmware'?<section className="panel special-conclusion" role="status"><Status value={run.status}/><h3>正在烧录并核验候选固件</h3><p>保持模组连接。准备完成后自动开放第一步，开始人工门控；本流程持续占用测试台。</p></section>:currentStep && !terminal.includes(run.status) ? <StepGate key={run.id + ':' + currentStep.id} run={run} step={currentStep} busy={busy} apiBase={apiBase} onAdvance={advance} onUpload={uploadManualLog} /> : <section className="panel special-conclusion"><Status value={run.status} /><h3>{run.status === 'passed' ? '本场景所有判定步骤已通过' : run.status === 'failed' ? '测试已执行，本场景失败' : run.status === 'blocked' ? '执行条件不足，测试已阻塞' : run.status === 'interrupted' ? '执行中断，需要核实现场状态' : '本次执行已结束'}</h3><p>{run.status === 'passed' ? '结论以各步骤的证据和判定范围为准；人工结论在记录中单独标记。' : run.status === 'failed' ? '已发现不符合预期的测试结果，失败原因和写入数据保存在下方。未执行的后续步骤不计为通过。' : '已有证据已保存。未完成步骤不会计为通过，可检查记录后重新创建测试。'}</p>{run.scenario_code === 'SPECIAL-LOG-FULL' ? <LogPressureSummary record={run.steps.find(record => record.step_id === 'log_pressure')} /> : null}</section>}
          {run.status === 'running' || run.status === 'cancelling' ? <p className="special-running" role="status">{run.status === 'cancelling' ? '正在等待硬件任务停止。' : '测试台正在执行当前步骤，请按本步指引操作。完成后会显示下一步。'}</p> : null}
          <CancelRun key={run.id} run={run} busy={busy} onCancel={cancel} />
          <section className="panel special-log"><h3>已保存的步骤证据</h3>
            {run.steps.filter(record => record.status !== 'pending').map((record, index) => { const step = run.definition_snapshot.steps.find(item => item.id === record.step_id); return <details key={record.step_id + ':' + index}><summary>{step?.title || record.step_id} · {labels[record.status] || record.status}</summary><p><strong>{step?.kind === 'manual' ? '人工记录' : '工具执行记录'}</strong> · 操作者：{record.operator || '—'}</p><p>开始：{date(record.started_at)} · 完成：{date(record.finished_at)}</p>{typeof record.result?.message === 'string' ? <p>{record.result.message}</p> : null}<ResultSummary record={record} apiBase={apiBase} /><Evidence value={record.values || {}} label="操作者填写与确认" /><Evidence value={record.result || {}} label="执行结果与采集证据" /></details>; })}
            {!run.steps.some(record => record.status !== 'pending') ? <p>完成第一步后在这里查看记录。</p> : null}
            <Evidence value={run.events} label="查看完整时间线" /><Evidence value={{ product: run.product_snapshot, bench: run.bench_snapshot, device: run.device_snapshot, tools: run.tool_snapshot, definition: run.definition_snapshot }} label="查看固定快照" />
          </section>
        </div>
      </div>
    </> : null}
    <section className="panel special-history">
      <header><div><h3>特殊场景测试记录</h3><p>选择记录可恢复流程或检查历史证据。</p></div><button type="button" disabled={busy} onClick={() => void perform(async () => setRuns(await api<Run[]>('/api/qa/special-runs')))}>刷新记录</button></header>
      <label>场景筛选<select value={historyFilter} onChange={event => setHistoryFilter(event.target.value)}><option value="all">全部场景</option>{definitions.map(item => <option key={item.code} value={item.code}>{item.name}</option>)}</select></label>
      <div className="special-history-list">{runs.filter(item => historyFilter === 'all' || item.scenario_code === historyFilter).map(item => <button type="button" key={item.id} disabled={busy} aria-current={run?.id === item.id ? 'true' : undefined} onClick={() => void perform(async () => selectRun(await api<Run>('/api/qa/special-runs/' + item.id)))}><span><strong>{item.definition_snapshot.name}</strong><small>{String(item.product_snapshot.name || '—')} · {date(item.created_at)}</small></span><span><Status value={item.status} /><small>{item.steps.filter(step => step.status === 'passed').length} / {item.definition_snapshot.steps.length} 步通过</small></span></button>)}</div>
      {!runs.length && !loading ? <p>尚无特殊场景记录。</p> : null}
    </section>
  </div>;
}
