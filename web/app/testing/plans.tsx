'use client';

import { useEffect, useState } from 'react';
import { failureMessage, testingApi, testingPost } from '../../lib/testing-api';
import { Badge, dateTime, Empty, Notice, PageHeading, sourceLabels } from './shared';
import type { Catalog, Location, TestPlan } from './types';

export default function Plans({ catalog, navigate }: { catalog: Catalog; navigate: (location: Location) => void }) {
  const [plans, setPlans] = useState<TestPlan[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [query, setQuery] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    void testingApi<TestPlan[]>('/test-plans', { signal: controller.signal }).then(setPlans).catch(cause => { if (!controller.signal.aborted) setError(failureMessage(cause)); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, []);
  async function copy(plan: TestPlan) {
    setBusy(plan.id); setError('');
    try { const next = await testingPost<TestPlan>(`/test-plans/${plan.id}/copy`, { name: `${plan.name} · 副本` }); navigate({ view: 'editor', id: next.id }); }
    catch (cause) { setError(failureMessage(cause)); } finally { setBusy(''); }
  }
  const visible = plans.filter(plan => plan.name.toLowerCase().includes(query.toLowerCase()));
  return <>
    <PageHeading title="测试方案" description="定义一次，按需运行。固件来源、测试范围与判定标准都保存在方案中。" actions={<button className="rt-primary" onClick={() => navigate({ view: 'editor' })}>＋ 新建方案</button>} />
    {error && <Notice tone="bad">{error}</Notice>}
    <div className="rt-section-head"><div><h2>从常用配置开始</h2><p>模板只预选固件来源，后续设置均可修改。</p></div></div>
    <div className="rt-template-grid">{([
      ['current_device', '测试当前固件', '直接验证设备正在运行的程序，无需 SDK。'],
      ['artifact', '使用已有镜像', '选择已保存的镜像，烧录后执行用例。'],
      ['build', '从源码构建', '保存本地修改或拉取指定版本，构建后测试。'],
    ] as const).map(([kind, title, description]) => <button className="rt-template" key={kind} onClick={() => navigate({ view: 'editor', template: kind })}><strong>{title}</strong><small>{description}</small><span>配置方案 →</span></button>)}</div>
    <div className="rt-section-head"><h2>已保存方案</h2><span className="rt-count">共 {plans.length} 个</span></div>
    {loading ? <p className="rt-loading">正在读取方案…</p> : !plans.length ? <Empty title="创建你的第一份测试方案" actions={<button onClick={() => navigate({ view: 'editor' })}>新建空白方案</button>}>先选择产品、固件来源和用例，保存后可随时运行和复制。</Empty> : <>
      <div className="rt-toolbar"><input type="search" aria-label="搜索测试方案" placeholder="搜索方案名称" value={query} onChange={event => setQuery(event.target.value)} /></div>
      <div className="rt-list">{visible.map(plan => { const draft = plan.draft; const product = catalog.products.find(item => item.id === draft.product_id); const published = plan.latest_revision || plan.revisions?.[0]; return <article className="rt-list-card" key={plan.id}><div className="rt-row rt-between"><div className="rt-row"><h2>{plan.name}</h2><Badge value={published ? 'published' : 'draft'}>{published ? `已保存 · 版本 ${published.version}` : '草稿'}</Badge></div><div className="rt-actions"><button disabled={busy === plan.id} onClick={() => void copy(plan)}>{busy === plan.id ? '复制中…' : '复制'}</button><button className="rt-primary" onClick={() => navigate({ view: 'editor', id: plan.id })}>查看与配置</button></div></div><div className="rt-meta"><span>{product?.name || '尚未选择产品'}</span><span>{sourceLabels[draft.firmware?.kind] || '待配置来源'}</span><span>{draft.cases?.length || 0} 项用例</span><span>手动运行</span><span>更新于 {dateTime(plan.updated_at || plan.created_at)}</span></div></article>; })}</div>
      {!visible.length && <p className="rt-muted">没有找到匹配的方案。</p>}
    </>}
  </>;
}
