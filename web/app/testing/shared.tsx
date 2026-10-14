'use client';

import { ReactNode, useCallback, useEffect, useState } from 'react';
import { failureMessage, testingApi } from '../../lib/testing-api';
import type { BuildEnvironment, CaseRevision, Catalog, Device, Product } from './types';

export const stateLabels: Record<string, string> = { draft: '草稿', published: '已保存版本', queued: '排队中', pending: '待执行', running: '执行中', preparing: '准备中', preparing_inputs: '准备输入', building: '构建中', flashing: '烧录中', testing: '测试中', awaiting_operator: '等待人工记录', waiting_operator: '等待人工记录', cancelling: '正在取消', cancelled: '已取消', passed: '通过', failed: '未通过', error: '执行异常', blocked: '暂不可执行', interrupted: '执行中断', finished: '已完成', completed: '已完成', not_tested: '未测试', inconclusive: '无法判定', ready: '可用', busy: '使用中', offline: '离线', quarantined: '待恢复', needs_attention: '需检查设备', unavailable: '不可用', unknown: '尚未检查', not_required: '无需恢复', partial: '部分通过', simulation: '模拟环境' };
export const sourceLabels = { current_device: '设备当前固件', artifact: '已有固件镜像', build: '从源码构建' };
export const terminalStates = new Set(['passed', 'failed', 'error', 'blocked', 'cancelled', 'completed', 'finished', 'interrupted']);

export function Badge({ value, children }: { value?: string; children?: ReactNode }) {
  const tone = ['passed', 'ready', 'published'].includes(value || '') ? 'good' : ['failed', 'error', 'quarantined', 'needs_attention'].includes(value || '') ? 'bad' : ['blocked', 'awaiting_operator', 'waiting_operator', 'interrupted', 'simulation'].includes(value || '') ? 'warn' : '';
  return <span className={`rt-badge ${tone}`}>{children || stateLabels[value || ''] || value || '待定'}</span>;
}
export function Notice({ children, tone = '', action }: { children: ReactNode; tone?: string; action?: ReactNode }) {
  return <div className={`rt-notice ${tone}`} role={tone === 'bad' ? 'alert' : 'status'}><span>{children}</span>{action}</div>;
}
export function PageHeading({ title, description, actions }: { title: string; description?: string; actions?: ReactNode }) {
  return <header className="rt-page-heading"><div><h1>{title}</h1>{description && <p>{description}</p>}</div>{actions && <div className="rt-actions">{actions}</div>}</header>;
}
export function Empty({ title, children, actions }: { title: string; children?: ReactNode; actions?: ReactNode }) {
  return <section className="rt-empty rt-panel"><span className="rt-empty-symbol" aria-hidden="true">＋</span><h2>{title}</h2><p>{children}</p>{actions && <div className="rt-actions">{actions}</div>}</section>;
}
export function TechnicalDetails({ value, label = '技术详情' }: { value: unknown; label?: string }) {
  return <details className="rt-technical"><summary>{label}</summary><pre>{JSON.stringify(value, null, 2)}</pre></details>;
}
export function dateTime(value?: string): string {
  if (!value) return '—';
  const date = new Date(/(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? value : `${value}Z`);
  return Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}
export function readable(value: unknown): string {
  if (value === null || value === undefined || value === '') return '未提供';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (Array.isArray(value)) return value.map(readable).join('、');
  if (typeof value === 'object') return Object.entries(value).map(([key, item]) => `${key}：${readable(item)}`).join('；');
  return String(value);
}
export function useCatalog() {
  const [catalog, setCatalog] = useState<Catalog>({ products: [], devices: [], environments: [], cases: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const [products, environments, devices, cases] = await Promise.all([
        testingApi<Product[]>('/products', { signal }), testingApi<BuildEnvironment[]>('/build-environments', { signal }),
        testingApi<Device[]>('/devices', { signal }), testingApi<CaseRevision[]>('/case-revisions', { signal }),
      ]);
      if (!signal?.aborted) { setCatalog({ products, environments, devices, cases: cases.filter(item => item.enabled !== false) }); setError(''); }
    } catch (cause) { if (!signal?.aborted) setError(failureMessage(cause)); }
    finally { if (!signal?.aborted) setLoading(false); }
  }, []);
  // Data fetching updates state only after requests settle; there is no synchronous derived-state update.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { const controller = new AbortController(); void refresh(controller.signal); return () => controller.abort(); }, [refresh]);
  return { catalog, loading, error, refresh };
}
