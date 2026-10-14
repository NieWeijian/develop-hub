'use client';

import { Fragment, useEffect, useRef, useState } from 'react';
import { failureMessage, requestKey, testingApi, testingPost } from '../../lib/testing-api';
import { Badge, Notice, PageHeading, sourceLabels, TechnicalDetails } from './shared';
import { ParameterSummary, SchemaFields } from './parameters';
import SdkStatus from './sdk-status';
import FirmwareDirectoryPicker from './firmware-directory-picker';
import { emptyPlan, type Catalog, type FirmwareBundle, type Location, type PlanConfig, type PlanPreview, type PlanRevision, type TestPlan, type TestRun } from './types';

const steps = ['基本信息', '固件准备', '设备与用例', '执行时机', '检查并运行'];

function ArtifactPicker({ bundleId, productId, chip, onChange }: { bundleId?: string; productId: string; chip?: string; onChange: (id: string) => void }) {
  const [bundles, setBundles] = useState<FirmwareBundle[]>([]);
  const [importing, setImporting] = useState(false);
  const [name, setName] = useState('');
  const [paths, setPaths] = useState('');
  const [browsing, setBrowsing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => { const controller = new AbortController(); void testingApi<FirmwareBundle[]>('/assets?kind=firmware_bundle', { signal: controller.signal }).then(setBundles).catch(cause => { if (!controller.signal.aborted) setError(failureMessage(cause)); }); return () => controller.abort(); }, []);
  async function importBundle() {
    if (!name.trim() || !paths.trim()) { setError('请填写镜像名称并选择要导入的镜像文件。'); return; }
    setBusy(true); setError('');
    try { const result = await testingPost<FirmwareBundle>('/firmware-imports', { name: name.trim(), paths: paths.split('\n').map(value => value.trim()).filter(Boolean), ...(productId ? { product_id: productId } : {}), ...(chip ? { chip } : {}) }); setBundles(current => [result, ...current]); onChange(result.id); setImporting(false); }
    catch (cause) { setError(failureMessage(cause)); } finally { setBusy(false); }
  }
  return <div className="rt-stack"><label className="rt-field"><span>选择已保存的镜像</span><select value={bundleId || ''} onChange={event => onChange(event.target.value)}><option value="">请选择镜像</option>{bundles.map(bundle => <option key={bundle.id} value={bundle.id}>{bundle.name}</option>)}</select><small>运行使用导入时保存的独立副本。</small></label>
    <div><button type="button" onClick={() => setImporting(value => !value)}>{importing ? '收起导入表单' : '＋ 从文件导入镜像'}</button></div>
    {importing && <section className="rt-panel"><h3>导入平台主机镜像</h3><p className="rt-muted rt-small">选择镜像所在文件夹，确认文件后保存到平台。</p><label className="rt-field"><span>镜像名称</span><input value={name} disabled={busy} onChange={event => setName(event.target.value)} placeholder="例如 factory-V2.1.1" /></label>
      <div className="rt-directory-selection"><button type="button" disabled={busy} onClick={() => setBrowsing(true)}>📁 {paths.trim() ? '重新选择镜像目录' : '选择镜像目录'}</button>{paths.trim() ? <><p className="rt-small">已选择 {paths.split('\n').filter(value => value.trim()).length} 个镜像</p><ul>{paths.split('\n').filter(value => value.trim()).map((path, index) => <li key={`${index}-${path}`}>{path.split('/').pop()}</li>)}</ul></> : <p className="rt-small rt-muted">无需逐个填写文件路径。</p>}</div>
      {browsing && <FirmwareDirectoryPicker onCancel={() => setBrowsing(false)} onSelect={files => { setPaths(files.map(file => file.path).join('\n')); setBrowsing(false); setError(''); }} />}
      <details><summary>高级：直接输入文件路径</summary><label className="rt-field"><span>平台主机上的镜像文件路径（每行一个）</span><textarea value={paths} disabled={busy} onChange={event => setPaths(event.target.value)} placeholder={'/home/build/output/boot.img\n/home/build/output/system.img\n/home/build/output/oem.img'} spellCheck={false} /></label></details>
      <div className="rt-footer"><span className="rt-small rt-muted">导入只保存文件，此时不会烧录设备。</span><button type="button" disabled={busy || browsing || !name.trim() || !paths.trim()} onClick={() => void importBundle()}>{busy ? '正在导入…' : '校验并保存镜像'}</button></div></section>}
    {error && <Notice tone="bad">{error}</Notice>}
  </div>;
}

export default function PlanEditor({ id, template, catalog, navigate, onCatalogChanged }: {
  id?: string; template?: PlanConfig['firmware']['kind']; catalog: Catalog; navigate: (location: Location) => void; onCatalogChanged: () => void;
}) {
  const [record, setRecord] = useState<TestPlan | null>(null);
  const [name, setName] = useState('');
  const [config, setConfig] = useState<PlanConfig>(() => emptyPlan(template));
  const [savedValue, setSavedValue] = useState('');
  const [step, setStep] = useState(0);
  const [loading, setLoading] = useState(!!id);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [query, setQuery] = useState('');
  const [preview, setPreview] = useState<PlanPreview | null>(null);
  const [previewValue, setPreviewValue] = useState('');
  const [remoteSource, setRemoteSource] = useState(false);
  const inFlight = useRef(false);
  const runKey = useRef({ value: '', key: '' });
  const currentValue = JSON.stringify({ name, config });
  const dirty = currentValue !== savedValue;
  const previewCurrent = previewValue === currentValue;
  const environment = catalog.environments.find(item => item.id === config.firmware.environment_id);
  const product = catalog.products.find(item => item.id === config.product_id);
  const device = catalog.devices.find(item => item.id === config.device_id);
  const buildOnly = config.firmware.kind === 'build' && config.firmware.build_only;
  const selected = config.cases;

  useEffect(() => {
    if (!id) return;
    const controller = new AbortController();
    void testingApi<TestPlan>(`/test-plans/${id}`, { signal: controller.signal }).then(plan => { setRecord(plan); setName(plan.name); setConfig({ ...emptyPlan(), ...plan.draft }); setSavedValue(JSON.stringify({ name: plan.name, config: { ...emptyPlan(), ...plan.draft } })); setRemoteSource(!!plan.draft.firmware?.source?.repository_overrides?.length); }).catch(cause => { if (!controller.signal.aborted) setError(failureMessage(cause)); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [id]);
  useEffect(() => {
    if (!dirty || (!name && !config.product_id && !config.cases.length)) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn); return () => window.removeEventListener('beforeunload', warn);
  }, [dirty, name, config.product_id, config.cases.length]);

  function change(next: PlanConfig) { setConfig(next); setNotice(''); }
  function firmware(values: Partial<PlanConfig['firmware']>) { change({ ...config, firmware: { ...config.firmware, ...values } }); }
  async function savedDraft(): Promise<TestPlan> {
    if (!name.trim()) throw new Error('请先填写方案名称。');
    if (record && !dirty) return record;
    const plan = record
      ? await testingPost<TestPlan>(`/test-plans/${record.id}/draft`, { revision: record.revision, name: name.trim(), draft: config }, 'PATCH')
      : await testingPost<TestPlan>('/test-plans', { name: name.trim(), draft: config });
    setRecord(plan); setName(plan.name); setSavedValue(JSON.stringify({ name: plan.name, config }));
    const url = new URL(window.location.href); url.searchParams.set('test_view', 'editor'); url.searchParams.set('test_id', plan.id); window.history.replaceState({}, '', url);
    return plan;
  }
  async function perform(action: () => Promise<void>) {
    if (inFlight.current) return;
    inFlight.current = true; setBusy(true); setError(''); setNotice('');
    try { await action(); } catch (cause) { setError(failureMessage(cause)); } finally { inFlight.current = false; setBusy(false); }
  }
  async function inspectPlan() {
    await perform(async () => { const plan = await savedDraft(); const result = await testingPost<PlanPreview>(`/test-plans/${plan.id}/validate`, { draft: config }); setPreview(result); setPreviewValue(JSON.stringify({ name: plan.name, config })); setStep(4); });
  }
  async function publish() {
    await perform(async () => { const plan = await savedDraft(); const revision = await testingPost<PlanRevision>(`/test-plans/${plan.id}/revisions`, { revision: plan.revision }); setRecord({ ...plan, revision: revision.draft_revision || plan.revision + 1, latest_revision: revision }); setNotice(`方案版本 ${revision.version} 已保存，可以按此版本运行。`); });
  }
  async function run() {
    await perform(async () => {
      const plan = await savedDraft(); const value = `${plan.id}:${plan.revision}`;
      if (runKey.current.value !== value) runKey.current = { value, key: requestKey() };
      const result = await testingPost<TestRun>('/test-runs', { plan_id: plan.id, draft_revision: plan.revision, parameter_overrides: {}, idempotency_key: runKey.current.key });
      navigate({ view: 'run', id: result.id });
    });
  }
  if (loading) return <p className="rt-loading">正在加载测试方案…</p>;
  if (id && !record) return <Notice tone="bad">{error || '方案暂时无法读取。'}</Notice>;
  const visibleCases = catalog.cases.filter(item => `${item.name} ${item.case_id}`.toLowerCase().includes(query.toLowerCase()));
  const overrides = config.firmware.source?.repository_overrides || [];
  return <>
    <PageHeading title={record ? '配置测试方案' : '新建测试方案'} description="按测试需求配置流程，保存后可重复执行。" actions={<><span className="rt-small rt-muted">{dirty ? '有尚未保存的修改' : record ? '草稿已保存' : '新方案'}</span><button disabled={busy} onClick={() => void perform(async () => { await savedDraft(); setNotice('草稿已保存。可以稍后继续配置。'); })}>{busy ? '处理中…' : '保存草稿'}</button></>} />
    <ol className="rt-stepper">{steps.map((label, index) => <li key={label}><button type="button" aria-current={step === index ? 'step' : undefined} onClick={() => setStep(index)} disabled={busy}><span className="rt-step-number">{index + 1}</span>{label}</button></li>)}</ol>
    {error && <Notice tone="bad">{error}</Notice>}{notice && <Notice tone="good">{notice}</Notice>}
    {step === 0 && <section className="rt-panel"><div className="rt-section-head"><div><h2>这份方案要验证什么</h2><p>产品仅定义测试对象；SDK 和代码来源在需要构建时选择。</p></div></div><div className="rt-grid"><label className="rt-field"><span>方案名称 *</span><input required maxLength={160} value={name} onChange={event => setName(event.target.value)} placeholder="例如 RMSL321 基础功能回归" autoComplete="off" /></label><label className="rt-field"><span>测试产品 *</span><select value={config.product_id} onChange={event => change({ ...config, product_id: event.target.value, device_id: undefined })}><option value="">请选择产品</option>{catalog.products.map(item => <option value={item.id} key={item.id}>{item.name}{item.chip ? ` · ${item.chip}` : ''}</option>)}</select></label></div>{!catalog.products.length && <Notice>还没有产品。请先到“设备与环境”登记产品，再创建方案。</Notice>}{product && <dl className="rt-facts"><div><dt>芯片</dt><dd>{product.chip || '未填写'}</dd></div><div><dt>板型</dt><dd>{product.board || '未填写'}</dd></div></dl>}</section>}
    {step === 1 && <>
      <section className="rt-panel"><div className="rt-section-head"><div><h2>固件从哪里来</h2><p>选择来源后，平台只执行必要阶段。</p></div></div><div className="rt-choice-grid">{([['current_device', '设备当前固件', '不编译、不烧录，读取设备身份后测试。'], ['artifact', '已有镜像', '导入镜像，保存独立副本后烧录测试。'], ['build', '从源码构建', '保存 SDK 输入，独立构建并使用产物。']] as const).map(([kind, label, hint]) => <label className="rt-choice" data-selected={config.firmware.kind === kind} key={kind}><input type="radio" name="firmware-kind" checked={config.firmware.kind === kind} onChange={() => change({ ...config, firmware: { kind, ...(kind === 'build' ? { source: { kind: 'local_workspace', repository_overrides: [] } } : {}) } })} /><span><strong>{label}</strong><small>{hint}</small></span></label>)}</div>
      {config.firmware.kind === 'current_device' && <Notice tone="good">运行前会读取设备报告的固件身份。此方式无需构建环境，也不会烧录。</Notice>}
      {config.firmware.kind === 'artifact' && <ArtifactPicker bundleId={config.firmware.bundle_id} productId={config.product_id} chip={product?.chip} onChange={bundle_id => firmware({ bundle_id })} />}
      {config.firmware.kind === 'build' && <div className="rt-stack"><label className="rt-field"><span>构建环境 *</span><select value={config.firmware.environment_id || ''} onChange={event => firmware({ environment_id: event.target.value })}><option value="">请选择 SDK 构建环境</option>{catalog.environments.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select><small>环境中保存 SDK 路径、构建命令与镜像位置。</small></label>
        {environment && <div className="rt-readonly"><strong>{environment.name}</strong><div className="rt-small rt-muted">控制端 SDK · {String(environment.config.sdk_path || '尚未配置目录')}</div></div>}
        <div className="rt-grid"><label className="rt-choice" data-selected={!remoteSource}><input type="radio" name="source" checked={!remoteSource} onChange={() => { setRemoteSource(false); firmware({ source: { kind: 'local_workspace', repository_overrides: [] } }); }} /><span><strong>使用本地 SDK 工作区</strong><small>包含当前已跟踪的修改及输入规则允许的未跟踪文件。</small></span></label><label className="rt-choice" data-selected={remoteSource}><input type="radio" name="source" checked={remoteSource} onChange={() => { setRemoteSource(true); if (!overrides.length) firmware({ source: { kind: 'local_workspace', repository_overrides: [{ path: '', url: '', ref: '' }] } }); }} /><span><strong>指定部分仓库的远端版本</strong><small>基于完整 SDK，为指定子仓库设置远端地址和版本。</small></span></label></div>
        {remoteSource && <div><div className="rt-section-head"><h3>仓库版本覆盖</h3><button type="button" onClick={() => firmware({ source: { kind: 'local_workspace', repository_overrides: [...overrides, { path: '', url: '', ref: '' }] } })}>＋ 添加仓库</button></div>{overrides.map((item, index) => <div className="rt-panel" key={index}><div className="rt-grid three">{([['path', 'SDK 内相对路径', 'project/app/example'], ['url', '远端仓库地址', 'ssh://git@host/project'], ['ref', '分支、提交或 Gerrit Ref', 'refs/changes/…']] as const).map(([key, label, placeholder]) => <label className="rt-field" key={key}><span>{label}</span><input value={item[key]} placeholder={placeholder} onChange={event => firmware({ source: { kind: 'local_workspace', repository_overrides: overrides.map((entry, i) => i === index ? { ...entry, [key]: event.target.value } : entry) } })} spellCheck={false} /></label>)}</div><button type="button" className="rt-link rt-danger" onClick={() => firmware({ source: { kind: 'local_workspace', repository_overrides: overrides.filter((_, i) => i !== index) } })}>移除此仓库</button></div>)}</div>}
        <label className="rt-check"><input type="checkbox" checked={!!config.firmware.build_only} onChange={event => change({ ...config, firmware: { ...config.firmware, build_only: event.target.checked }, ...(event.target.checked ? { device_id: undefined, cases: [] } : {}) })} /><span>只构建固件，完成后结束<small className="rt-muted"> · 不选择设备和用例，结果记录为“未测试”</small></span></label>
        <Notice>实际构建使用运行时保存的独立 SDK 输入。当前环境中的修改会保留，不在原 SDK 目录直接编译。</Notice>
      </div>}
      </section>
      {config.firmware.kind !== 'current_device' && !buildOnly && <section className="rt-panel"><label className="rt-field"><span>烧录后预期固件版本</span><input value={config.firmware.expected_version || ''} onChange={event => change({ ...config, firmware: { ...config.firmware, expected_version: event.target.value } })} placeholder="例如 factory-V2.1.0" /><small>用于部署后的启动身份确认。填写本次固件的实际完整版本。</small></label></section>}
      {config.firmware.kind === 'build' && environment && <section className="rt-panel"><SdkStatus key={environment.id} environment={environment} onChanged={onCatalogChanged} /></section>}
    </>}
    {step === 2 && (buildOnly ? <section className="rt-panel"><h2>仅构建方案</h2><Notice>这份方案不选择设备或执行用例。构建结束后保存固件产物，结果为“未测试”。</Notice></section> : <>
      <section className="rt-panel"><div className="rt-section-head"><div><h2>选择测试设备</h2><p>只显示当前产品下已登记的设备，运行前会再次检查设备状态。</p></div></div><label className="rt-field"><span>测试设备 *</span><select value={config.device_id || ''} onChange={event => change({ ...config, device_id: event.target.value })}><option value="">请选择设备</option>{catalog.devices.filter(item => item.product_id === config.product_id).map(item => <option value={item.id} key={item.id}>{item.name}{item.node.kind === 'simulation' ? ' · 模拟设备' : ''}</option>)}</select></label>{!config.product_id && <Notice>请先在基本信息中选择产品。</Notice>}{device && <div className="rt-row"><Badge value={device.health} />{device.node.kind === 'simulation' && <Badge value="simulation">模拟设备 · 不代表硬件验证</Badge>}</div>}</section>
      <section className="rt-panel"><div className="rt-section-head"><div><h2>选择用例与判定标准</h2><p>必需项全部通过才满足方案要求；补充观察项的结果也会完整保留。</p></div><span className="rt-count">已选 {selected.length} 项 · 必需 {selected.filter(item => item.required).length} 项</span></div><div className="rt-toolbar"><input type="search" aria-label="搜索用例" value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索用例名称或编号" /></div>
        {!catalog.cases.length && <Notice>用例库为空。请在“用例库”新建人工用例或导入代码包。</Notice>}
        {visibleCases.map(item => { const bindings = selected.filter(binding => binding.case_revision_id === item.id); return <article className="rt-case-row" key={item.id}><div className="rt-case-top"><label><input type="checkbox" checked={bindings.length > 0} onChange={event => change({ ...config, cases: event.target.checked ? [...selected, { binding_id: requestKey(), case_revision_id: item.id, required: true, parameter_overrides: {} }] : selected.filter(binding => binding.case_revision_id !== item.id) })} /><span><strong>{item.name}</strong><small>{item.definition.test_scope || item.definition.description || item.case_id}</small></span></label><div className="rt-row"><Badge>{item.mode === 'manual' ? '人工记录' : '自动执行'}</Badge><span className="rt-small rt-muted">版本 {item.version}</span></div></div>
          {bindings.map((binding, index) => <div className="rt-case-parameters" key={binding.binding_id}><div className="rt-row rt-between"><label className="rt-check"><input type="checkbox" checked={binding.required} onChange={event => change({ ...config, cases: selected.map(entry => entry.binding_id === binding.binding_id ? { ...entry, required: event.target.checked } : entry) })} />{binding.required ? '必需项' : '补充观察项'}{bindings.length > 1 && ` · 配置 ${index + 1}`}</label><button className="rt-link" type="button" onClick={() => change({ ...config, cases: selected.map(entry => entry.binding_id === binding.binding_id ? { ...entry, parameter_overrides: {} } : entry) })}>全部恢复默认</button></div><SchemaFields schema={item.definition.parameters_schema} ui={item.definition.parameter_ui} values={binding.parameter_overrides} onChange={parameter_overrides => change({ ...config, cases: selected.map(entry => entry.binding_id === binding.binding_id ? { ...entry, parameter_overrides } : entry) })} /><div className="rt-actions"><button type="button" className="rt-link" onClick={() => change({ ...config, cases: [...selected, { ...binding, binding_id: requestKey(), parameter_overrides: { ...binding.parameter_overrides } }] })}>＋ 增加一组参数测试</button>{bindings.length > 1 && <button type="button" className="rt-link rt-danger" onClick={() => change({ ...config, cases: selected.filter(entry => entry.binding_id !== binding.binding_id) })}>移除此组</button>}</div></div>)}
        </article>; })}
      </section>
    </>)}
    {step === 3 && <section className="rt-panel"><div className="rt-section-head"><div><h2>何时执行</h2><p>本次交付支持手动运行，同一方案可以重复执行。</p></div></div><label className="rt-choice" data-selected="true"><input type="radio" checked readOnly /><span><strong>手动执行</strong><small>检查配置后点击运行，后端创建独立运行记录。</small></span></label><Notice>定时触发与 Gerrit 事件触发将在后续接入；当前保存方案不会创建自动任务。</Notice><hr className="rt-divider" /><div className="rt-grid"><label className="rt-field"><span>独立用例失败后</span><select value={String(config.policy.independent_case_failure || 'continue')} onChange={event => change({ ...config, policy: { ...config.policy, independent_case_failure: event.target.value } })}><option value="continue">继续执行剩余独立用例</option><option value="stop">停止后续用例</option></select></label><label className="rt-field"><span>人工记录等待时间（秒）</span><input type="number" min={60} max={86400} value={Number(config.policy.manual_wait_timeout_seconds || 3600)} onChange={event => change({ ...config, policy: { ...config.policy, manual_wait_timeout_seconds: Number(event.target.value) } })} /></label></div><p className="rt-small rt-muted">设备恢复失败时停止继续操作，并标记设备待恢复。</p></section>}
    {step === 4 && <>
      <section className="rt-panel"><div className="rt-section-head"><div><h2>检查本次配置</h2><p>运行前预览会校验用例参数和资源要求。真正执行前仍会再次检查。</p></div><button onClick={() => void inspectPlan()} disabled={busy}>{busy ? '检查中…' : '检查配置'}</button></div><dl className="rt-facts"><div><dt>方案</dt><dd>{name || '尚未命名'}</dd></div><div><dt>产品</dt><dd>{product?.name || '尚未选择'}</dd></div><div><dt>固件来源</dt><dd>{sourceLabels[config.firmware.kind]}{remoteSource && config.firmware.kind === 'build' ? ' · 远端仓库覆盖' : ''}</dd></div><div><dt>设备</dt><dd>{buildOnly ? '仅构建，无需设备' : device?.name || '尚未选择'}</dd></div><div><dt>测试范围</dt><dd>{buildOnly ? '不执行测试' : `${selected.length} 项用例，其中 ${selected.filter(item => item.required).length} 项必需`}</dd></div><div><dt>执行时机</dt><dd>手动运行</dd></div></dl>
        {!previewCurrent && <Notice>配置尚未检查，或检查后已发生变化。请点击“检查配置”。</Notice>}
        {preview && previewCurrent && <><Notice tone={preview.valid ? 'good' : 'bad'}>{preview.valid ? '配置检查通过，可以保存方案版本或开始运行。' : '配置尚不完整，请处理以下问题。'}</Notice>{preview.errors.length > 0 && <ul className="rt-errors">{preview.errors.map((issue, index) => <li key={index}>{issue.message}</li>)}</ul>}{preview.warnings.map((warning, index) => <Notice tone="warn" key={index}>{typeof warning === 'string' ? warning : warning.message}</Notice>)}<div className="rt-flow" aria-label="预计执行阶段">{preview.stages.map((stage, index) => <Fragment key={stage}>{index > 0 && <b aria-hidden="true">→</b>}<span>{stage}</span></Fragment>)}</div>{selected.map(binding => { const item = catalog.cases.find(entry => entry.id === binding.case_revision_id); return <details key={binding.binding_id}><summary>{item?.name || '所选用例'} · {binding.required ? '必需项' : '补充观察项'} · 实际参数</summary><ParameterSummary values={preview.parameters[binding.binding_id] || {}} ui={item?.definition.parameter_ui} /></details>; })}</>}
        <TechnicalDetails value={config} label="查看方案技术配置" />
      </section>
    </>}
    <footer className="rt-footer"><div className="rt-actions">{step > 0 && <button onClick={() => setStep(current => current - 1)} disabled={busy}>上一步</button>}<span className="rt-small rt-muted">第 {step + 1} 步，共 {steps.length} 步</span></div><div className="rt-footer-main">{step < 4 ? <button className="rt-primary" disabled={busy} onClick={() => setStep(current => current + 1)}>下一步 →</button> : <><button disabled={busy || !previewCurrent || !preview?.valid} onClick={() => void publish()}>保存方案版本</button><button className="rt-primary" disabled={busy || !previewCurrent || !preview?.valid} onClick={() => void run()}>{busy ? '正在提交…' : '保存并运行'}</button></>}</div></footer>
  </>;
}
