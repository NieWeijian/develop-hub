'use client';

import { FormEvent, useEffect, useState } from 'react';
import { failureMessage, testingApi, testingPost } from '../../lib/testing-api';
import { Badge, Empty, Notice, PageHeading, TechnicalDetails } from './shared';
import SdkStatus from './sdk-status';
import type { BuildEnvironment, Catalog, Device, Product, Values } from './types';

type Tab = 'products' | 'devices' | 'environments';
function lines(value: FormDataEntryValue | null): string[] { return String(value || '').split('\n').map(item => item.trim()).filter(Boolean); }
function strings(value: unknown): string[] { return Array.isArray(value) ? value.map(String) : []; }
function object(value: unknown): Values { return value && typeof value === 'object' && !Array.isArray(value) ? value as Values : {}; }

export function CommandArguments({ label, value, onChange, hint }: { label: string; value: string[]; onChange: (value: string[]) => void; hint?: string }) {
  return <fieldset className="rt-nested-fields"><legend>{label}</legend><div className="rt-command-rows">{value.map((arg, index) => <div key={index}><span>{index === 0 ? '程序' : `参数${index}`}</span><input aria-label={`${label} ${index === 0 ? '程序' : `参数 ${index}`}`} value={arg} onChange={event => onChange(value.map((item, i) => i === index ? event.target.value : item))} spellCheck={false} /><button type="button" aria-label={`移除${label}第 ${index + 1} 项`} onClick={() => onChange(value.filter((_, i) => i !== index))}>×</button></div>)}</div><button type="button" className="rt-link" onClick={() => onChange([...value, ''])}>＋ 添加参数</button>{hint && <p className="rt-small rt-muted">{hint}</p>}</fieldset>;
}

function ProductForm({ item, onSave, onCancel, busy }: { item?: Product; onSave: (body: Values) => void; onCancel: () => void; busy: boolean }) {
  function submit(event: FormEvent<HTMLFormElement>) { event.preventDefault(); const data = new FormData(event.currentTarget); onSave({ name: data.get('name'), chip: data.get('chip'), board: data.get('board'), capabilities: lines(data.get('capabilities')), config: item?.config || {} }); }
  return <form className="rt-panel" onSubmit={submit}><div className="rt-section-head"><h2>{item ? '编辑产品' : '登记测试产品'}</h2></div><div className="rt-grid"><label className="rt-field"><span>产品名称 *</span><input name="name" required maxLength={160} defaultValue={item?.name} placeholder="例如 RMSL321" /></label><label className="rt-field"><span>芯片型号</span><input name="chip" maxLength={80} defaultValue={item?.chip} placeholder="例如 RV1126B" /></label><label className="rt-field"><span>板型</span><input name="board" defaultValue={item?.board} placeholder="产品板型或硬件版本" /></label><label className="rt-field"><span>产品能力（每行一项）</span><textarea name="capabilities" defaultValue={item?.capabilities.join('\n')} placeholder="例如 temperature.read.v1" /><small>能力标识用于匹配测试用例，须与工具实际支持的能力一致。</small></label></div><div className="rt-footer"><button type="button" disabled={busy} onClick={onCancel}>取消</button><button className="rt-primary" disabled={busy}>{busy ? '保存中…' : '保存产品'}</button></div></form>;
}

type TestStation = { id: string; name: string; chip: string; adapter_name: string; functions: string[]; devices: { serial: string; firmware_version: string }[] };

function DeviceForm({ item, products, onSave, onCancel, busy }: { item?: Device; products: Product[]; onSave: (body: Values) => void; onCancel: () => void; busy: boolean }) {
  const [stations, setStations] = useState<TestStation[]>([]);
  const [loading, setLoading] = useState(!item); const [error, setError] = useState('');
  const [productId, setProductId] = useState(item?.product_id || '');
  const [stationId, setStationId] = useState(''); const [serial, setSerial] = useState('');
  useEffect(() => {
    if (item) return;
    let active = true;
    testingApi<TestStation[]>('/test-stations').then(value => { if (active) setStations(value); }).catch(cause => { if (active) setError(failureMessage(cause)); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [item]);
  const product = products.find(value => value.id === productId);
  const available = stations.filter(value => value.chip.toUpperCase() === product?.chip.toUpperCase());
  const station = available.find(value => value.id === stationId);
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const data = new FormData(event.currentTarget);
    onSave({ name: data.get('name'), ...(!item ? { product_id: productId, station_id: stationId, serial } : {}) });
  }
  return <form className="rt-panel" onSubmit={submit}><div className="rt-section-head"><div><h2>{item ? '编辑测试设备' : '登记测试设备'}</h2><p>平台已统一安装设备驱动和测试工具，选择测试台即可使用。</p></div></div>
    {error && <Notice tone="bad">{error}</Notice>}
    <div className="rt-grid"><label className="rt-field"><span>设备名称 *</span><input name="name" required maxLength={160} defaultValue={item?.name} placeholder="例如 实验室模组 A" /></label><label className="rt-field"><span>所属产品 *</span><select required value={productId} disabled={!!item} onChange={event => { setProductId(event.target.value); setStationId(''); setSerial(''); }}><option value="">选择产品</option>{products.filter(value => value.enabled !== false || value.id === item?.product_id).map(value => <option key={value.id} value={value.id}>{value.name}</option>)}</select></label></div>
    {item ? <Notice>测试台：{String(item.config.station_name || (item.node.kind === 'simulation' ? '模拟测试台' : '已登记测试台'))} · 设备序列号：{String(item.config.serial || '未登记')}。设备接入由平台维护。</Notice> : <>
      {loading ? <p role="status">正在读取已部署的测试台…</p> : productId && !available.length && !error ? <Notice tone="warn">该产品还没有已部署的测试台，请联系平台维护人员完成接入。</Notice> : null}
      <div className="rt-grid"><label className="rt-field"><span>测试台 *</span><select required disabled={loading || !productId} value={stationId} onChange={event => { const selected = available.find(value => value.id === event.target.value); setStationId(event.target.value); setSerial(selected?.devices.length === 1 ? selected.devices[0].serial : ''); }}><option value="">选择已部署的测试台</option>{available.map(value => <option key={value.id} value={value.id}>{value.name}</option>)}</select></label><label className="rt-field"><span>设备 CPU 序列号 *</span><input required maxLength={64} value={serial} onChange={event => setSerial(event.target.value)} list="station-devices" placeholder="选择已发现的设备，或填写设备序列号" /><datalist id="station-devices">{station?.devices.map(value => <option key={value.serial} value={value.serial}>{value.firmware_version}</option>)}</datalist><small>用于确认每次测试连接的是同一台设备。</small></label></div>
      {station && <Notice tone="good">{station.adapter_name}已部署 · {station.functions.join('、')}。保存后可检查设备连接。</Notice>}
    </>}
    <div className="rt-footer"><button type="button" onClick={onCancel} disabled={busy}>取消</button><button className="rt-primary" disabled={busy || (!item && (!station || !serial || loading))}>{busy ? '保存中…' : '保存设备'}</button></div>
  </form>;
}

function DeviceCheckForm({ device, onChanged }: { device: Device; onChanged: () => Promise<void> }) {
  const [open, setOpen] = useState(false); const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [message, setMessage] = useState('');
  async function check(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const fields = new FormData(event.currentTarget); setBusy(true); setError(''); setMessage('');
    try { await testingPost(`/devices/${device.id}/check`, { operator: fields.get('operator'), note: fields.get('note') || '', recovery_confirmed: fields.get('confirmed') === 'on' }); await onChanged(); setOpen(false); setMessage('设备身份检查通过，已标记为可用。'); }
    catch (cause) { setError(failureMessage(cause)); } finally { setBusy(false); }
  }
  return <div>{message && <Notice tone="good">{message}</Notice>}<button disabled={busy || device.health === 'busy'} onClick={() => setOpen(value => !value)}>{device.health === 'needs_attention' ? '检查并恢复可用' : '检查设备连接'}</button>{open && <form onSubmit={event => void check(event)}>{error && <Notice tone="bad">{error}</Notice>}<label className="rt-field"><span>检查人 *</span><input name="operator" required /></label>{device.health === 'needs_attention' && <><label className="rt-field"><span>恢复说明 *</span><textarea name="note" required /></label><label className="rt-check"><input name="confirmed" type="checkbox" required />已确认节点无残留任务，设备已恢复可用</label></>}<p className="rt-small rt-muted">平台会重新读取并校验设备身份。</p><button className="rt-primary" disabled={busy}>{busy ? '检查中…' : '执行检查'}</button></form>}</div>;
}

function EnvironmentForm({ item, onSave, onCancel, busy }: { item?: BuildEnvironment; onSave: (body: Values) => void; onCancel: () => void; busy: boolean }) {
  const recipe = object(item?.config.recipe); const rules = object(item?.config.input_rules);
  const [argv, setArgv] = useState<string[]>(strings(recipe.argv).length ? strings(recipe.argv) : ['./build.sh']);
  function submit(event: FormEvent<HTMLFormElement>) { event.preventDefault(); const data = new FormData(event.currentTarget); onSave({ ...(item ? { revision: item.revision || 1 } : {}), name: data.get('name'), node: { kind: 'local' }, config: { ...item?.config, sdk_path: data.get('sdk_path'), input_rules: { ...rules, include: lines(data.get('include')), exclude: lines(data.get('exclude')) }, recipe: { ...recipe, argv: argv.filter(Boolean), cwd: String(data.get('cwd') || '.'), timeout_seconds: Number(data.get('timeout') || 7200), artifacts: lines(data.get('artifacts')), env: object(recipe.env) } } }); }
  return <form className="rt-panel" onSubmit={submit}><div className="rt-section-head"><div><h2>{item ? '编辑构建环境' : '登记 SDK 构建环境'}</h2><p>首批使用平台控制端上的 SDK。运行时采集独立输入并构建。</p></div></div><div className="rt-grid"><label className="rt-field"><span>环境名称 *</span><input name="name" required defaultValue={item?.name} placeholder="例如 RV1126B Linux SDK" /></label><label className="rt-field"><span>控制端 SDK 完整目录 *</span><input name="sdk_path" required defaultValue={String(item?.config.sdk_path || '')} placeholder="/home/build/RV1126B_SDK" spellCheck={false} /></label></div><CommandArguments label="构建程序与参数" value={argv} onChange={setArgv} /><div className="rt-grid"><label className="rt-field"><span>构建工作目录（相对 SDK）</span><input name="cwd" defaultValue={String(recipe.cwd || '.')} /></label><label className="rt-field"><span>构建超时（秒）</span><input name="timeout" type="number" min={1} max={86400} defaultValue={Number(recipe.timeout_seconds || 7200)} /></label><label className="rt-field"><span>产物匹配路径 *</span><textarea name="artifacts" required defaultValue={strings(recipe.artifacts).join('\n')} placeholder="output/*.img" /><small>相对 SDK 目录，每行一个路径规则。</small></label><label className="rt-field"><span>额外输入文件规则</span><textarea name="include" defaultValue={strings(rules.include).join('\n')} placeholder="每行一个匹配规则" /><small>显式纳入被 Git 忽略但构建必需的文件；普通未跟踪源码默认纳入。</small></label><label className="rt-field"><span>排除输入文件规则</span><textarea name="exclude" defaultValue={strings(rules.exclude).join('\n')} placeholder={'output/**\n*.log'} /></label></div><div className="rt-footer"><button type="button" disabled={busy} onClick={onCancel}>取消</button><button className="rt-primary" disabled={busy}>{busy ? '保存中…' : '保存构建环境'}</button></div></form>;
}

export default function Settings({ catalog, onChanged }: { catalog: Catalog; onChanged: () => Promise<void> }) {
  const [tab, setTab] = useState<Tab>('products'); const [editing, setEditing] = useState<string | null>(null); const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [notice, setNotice] = useState(''); const [inspectId, setInspectId] = useState('');
  async function save(body: Values) { setBusy(true); setError(''); setNotice(''); const route = tab === 'environments' ? 'build-environments' : tab; try { await testingPost(`/${route}${editing && editing !== 'new' ? `/${editing}` : ''}`, body, editing !== 'new' ? 'PATCH' : 'POST'); await onChanged(); setEditing(null); setNotice('配置已保存。'); } catch (cause) { setError(failureMessage(cause)); } finally { setBusy(false); } }
  const labels = { products: '产品', devices: '设备', environments: '构建环境' };
  return <>
    <PageHeading title="设备与环境" description="管理测试对象、执行节点和 SDK 构建环境。当前固件测试只需产品与设备。" actions={!editing && <button className="rt-primary" onClick={() => { setEditing('new'); setError(''); }}>＋ 登记{labels[tab]}</button>} />
    <div className="rt-tabs" role="tablist" aria-label="环境配置分类">{(['products', 'devices', 'environments'] as const).map(value => <button role="tab" aria-selected={tab === value} key={value} onClick={() => { setTab(value); setEditing(null); setError(''); setNotice(''); }}>{labels[value]}</button>)}</div>
    {error && <Notice tone="bad">{error}</Notice>}{notice && <Notice tone="good">{notice}</Notice>}
    {editing ? tab === 'products' ? <ProductForm key={editing} item={catalog.products.find(item => item.id === editing)} onSave={body => void save(body)} onCancel={() => setEditing(null)} busy={busy} /> : tab === 'devices' ? <DeviceForm key={editing} item={catalog.devices.find(item => item.id === editing)} products={catalog.products} onSave={body => void save(body)} onCancel={() => setEditing(null)} busy={busy} /> : <EnvironmentForm key={editing} item={catalog.environments.find(item => item.id === editing)} onSave={body => void save(body)} onCancel={() => setEditing(null)} busy={busy} /> : <>
      {tab === 'products' && (!catalog.products.length ? <Empty title="还没有测试产品">登记产品名称、芯片与板型，即可开始配置设备。</Empty> : <div className="rt-grid">{catalog.products.map(item => <article className="rt-panel" key={item.id}><div className="rt-section-head"><h2>{item.name}</h2><button onClick={() => setEditing(item.id)}>编辑</button></div><dl className="rt-facts"><div><dt>芯片</dt><dd>{item.chip || '未填写'}</dd></div><div><dt>板型</dt><dd>{item.board || '未填写'}</dd></div></dl><div className="rt-chip-list">{item.capabilities.map(cap => <Badge key={cap}>{cap}</Badge>)}</div></article>)}</div>)}
      {tab === 'devices' && (!catalog.devices.length ? <Empty title="还没有测试设备">选择已部署的测试台，登记要测试的设备。</Empty> : <div className="rt-grid">{catalog.devices.map(item => <article className="rt-panel" key={item.id}><div className="rt-section-head"><div><h2>{item.name}</h2><p>{catalog.products.find(product => product.id === item.product_id)?.name}</p></div><button onClick={() => setEditing(item.id)}>编辑</button></div><div className="rt-row"><Badge value={item.health} />{item.node.kind === 'simulation' && <Badge value="simulation">模拟设备</Badge>}</div><dl className="rt-facts"><div><dt>执行位置</dt><dd>{item.node.kind === 'ssh' ? String(item.config.station_name || '已登记测试台') : item.node.kind === 'simulation' ? '模拟运行' : '平台控制端'}</dd></div><div><dt>设备标识</dt><dd>{String(item.config.serial || '运行时读取')}</dd></div></dl><DeviceCheckForm device={item} onChanged={onChanged} /><p className="rt-small rt-muted">{String(item.config.adapter_name || (item.node.kind === 'simulation' ? '模拟设备，仅用于体验流程' : '设备接入由平台维护'))}{Array.isArray(item.config.functions) ? ` · ${item.config.functions.join('、')}` : ''}</p></article>)}</div>)}
      {tab === 'environments' && (!catalog.environments.length ? <Empty title="还没有构建环境">只有从源码构建时才需要 SDK。登记目录和构建配方后，可以查看各仓库状态。</Empty> : catalog.environments.map(item => <article className="rt-panel" key={item.id}><div className="rt-section-head"><div><h2>{item.name}</h2><p>控制端 SDK · {String(item.config.sdk_path || '未配置目录')}</p></div><div className="rt-actions"><button onClick={() => setEditing(item.id)}>编辑</button><button onClick={() => setInspectId(current => current === item.id ? '' : item.id)}>{inspectId === item.id ? '收起仓库状态' : '查看仓库状态'}</button></div></div>{inspectId === item.id && <SdkStatus key={`${item.id}-${item.revision}`} environment={item} onChanged={() => void onChanged()} />}<TechnicalDetails value={item.config} label="构建配方与输入规则" /></article>))}
    </>}
  </>;
}
