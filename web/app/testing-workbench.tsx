'use client';

import { useEffect, useRef, useState } from 'react';
import { failureMessage, testingPost } from '../lib/testing-api';
import { Notice, useCatalog } from './testing/shared';
import Plans from './testing/plans';
import PlanEditor from './testing/plan-editor';
import CaseLibrary from './testing/case-library';
import Settings from './testing/settings';
import { RunDetail, Runs } from './testing/runs';
import type { Location, View } from './testing/types';
import './testing.css';

const navigation = [{ view: 'plans', label: '测试方案', icon: '▤' }, { view: 'runs', label: '运行记录', icon: '◷' }, { view: 'cases', label: '用例库', icon: '▦' }, { view: 'settings', label: '设备与环境', icon: '⚙' }] as const;
function readLocation(): Location {
  const params = new URLSearchParams(window.location.search);
  const view = params.get('test_view');
  if (['plans', 'editor', 'cases', 'settings', 'runs', 'run'].includes(view || '')) return { view: view as View, id: params.get('test_id') || undefined };
  return { view: 'plans' };
}

export default function TestingWorkbench() {
  const [location, setLocation] = useState<Location>({ view: 'plans' });
  const { catalog, loading, error, refresh } = useCatalog();
  const [demoBusy, setDemoBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [actionError, setActionError] = useState('');
  const mainRef = useRef<HTMLElement>(null);
  useEffect(() => { const sync = () => setLocation(readLocation()); sync(); window.addEventListener('popstate', sync); return () => window.removeEventListener('popstate', sync); }, []);
  function navigate(next: Location, replace = false) {
    const url = new URL(window.location.href); url.searchParams.set('test_view', next.view);
    if (next.id) url.searchParams.set('test_id', next.id); else url.searchParams.delete('test_id');
    window.history[replace ? 'replaceState' : 'pushState']({}, '', url);
    setLocation(next); setNotice(''); mainRef.current?.focus({ preventScroll: true }); window.scrollTo({ top: 0, behavior: 'instant' });
  }
  async function seedDemo() {
    setDemoBusy(true); setActionError('');
    try { await testingPost('/demo'); await refresh(); setNotice('演示产品、模拟设备和示例用例已保存。演示运行会明确标记为模拟结果。'); }
    catch (cause) { setActionError(failureMessage(cause)); } finally { setDemoBusy(false); }
  }
  const activeView = location.view === 'editor' ? 'plans' : location.view === 'run' ? 'runs' : location.view;
  const empty = !loading && !error && !catalog.products.length && !catalog.devices.length && !catalog.cases.length;
  return <div className="rk-testing">
    <aside className="rt-sidebar"><div className="rt-brand"><span aria-hidden="true">RK</span><div><strong>Test Workbench</strong><small>RK AUTOMATED TESTING</small></div></div><div><p className="rt-nav-title">测试工作空间</p><nav className="rt-navigation" aria-label="测试平台主导航">{navigation.map(item => <button key={item.view} aria-current={activeView === item.view ? 'page' : undefined} onClick={() => navigate({ view: item.view })}><span className="rt-nav-icon" aria-hidden="true">{item.icon}</span>{item.label}</button>)}</nav></div><div className="rt-sidebar-footer"><strong>面向设备的测试平台</strong><p>可配置方案 · 版本化用例<br />运行过程与证据保存在服务端</p></div></aside>
    <main className="rt-main" ref={mainRef} tabIndex={-1}>
      <div className="rt-topline"><span>测试工作空间 / {navigation.find(item => item.view === activeView)?.label}</span><span>{loading ? '连接服务中…' : error ? '服务连接异常' : `${catalog.devices.length} 台已登记设备`} · 北京时间</span></div>
      {(error || actionError) && <Notice tone="bad" action={<button onClick={() => void refresh()}>重试</button>}>{error || actionError}</Notice>}
      {notice && <Notice tone="good">{notice}</Notice>}
      {empty && <Notice action={<div className="rt-actions"><button onClick={() => navigate({ view: 'settings' })}>登记真实环境</button><button disabled={demoBusy} onClick={() => void seedDemo()}>{demoBusy ? '正在创建…' : '创建模拟演示数据'}</button></div>}>欢迎使用。先登记产品和设备，或用模拟数据了解完整测试流程。</Notice>}
      {loading ? <div className="rt-loading">正在加载产品、设备与用例…</div> : error ? null : <>
        {location.view === 'plans' && <Plans key={`${catalog.products.length}-${catalog.cases.length}`} catalog={catalog} navigate={navigate} />}
        {location.view === 'editor' && <PlanEditor key={location.id || `new-${location.template || 'current_device'}`} id={location.id} template={location.template} catalog={catalog} navigate={navigate} onCatalogChanged={() => void refresh()} />}
        {location.view === 'cases' && <CaseLibrary cases={catalog.cases} onChanged={() => refresh()} />}
        {location.view === 'settings' && <Settings catalog={catalog} onChanged={() => refresh()} />}
        {location.view === 'runs' && <Runs navigate={navigate} />}
        {location.view === 'run' && location.id && <RunDetail key={location.id} id={location.id} catalog={catalog} navigate={navigate} />}
      </>}
    </main>
  </div>;
}
