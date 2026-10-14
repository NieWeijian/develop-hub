'use client';

import { useState } from 'react';
import { failureMessage, testingPost } from '../../lib/testing-api';
import { Badge, dateTime, Notice, TechnicalDetails } from './shared';
import type { BuildEnvironment } from './types';

type RepositoryStatus = { path: string; branch: string; head: string; subject: string; status: string; tracked_changes: unknown; untracked_files: unknown };
type Inspection = { sdk_path: string; inspected_at: string; repositories: RepositoryStatus[]; manifest: unknown };
function count(value: unknown): string { return Array.isArray(value) ? String(value.length) : typeof value === 'number' ? String(value) : value ? '有' : '0'; }

export default function SdkStatus({ environment, onChanged }: { environment: BuildEnvironment; onChanged?: () => void }) {
  const [inspection, setInspection] = useState<Inspection | null>(null);
  const [query, setQuery] = useState('');
  const [watch, setWatch] = useState<string[]>(Array.isArray(environment.config.watched_repositories) ? environment.config.watched_repositories as string[] : []);
  const [onlyWatched, setOnlyWatched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  async function inspect() {
    setBusy(true); setError('');
    try { setInspection(await testingPost<Inspection>(`/build-environments/${environment.id}/inspect`)); }
    catch (cause) { setError(failureMessage(cause)); } finally { setBusy(false); }
  }
  async function saveWatch() {
    setBusy(true); setError('');
    try { await testingPost(`/build-environments/${environment.id}`, { revision: environment.revision || 1, config: { ...environment.config, watched_repositories: watch } }, 'PATCH'); setSaved(true); onChanged?.(); }
    catch (cause) { setError(failureMessage(cause)); } finally { setBusy(false); }
  }
  const repositories = (inspection?.repositories || []).filter(repo => (!onlyWatched || watch.includes(repo.path)) && `${repo.path} ${repo.branch} ${repo.subject}`.toLowerCase().includes(query.toLowerCase()));
  return <div>
    <div className="rt-section-head"><div><h3>SDK 当前工作区</h3><p>查看构建前的仓库状态。运行时会另存输入快照，当前状态不会改变历史构建记录。</p></div><button type="button" onClick={() => void inspect()} disabled={busy}>{busy ? '正在读取…' : inspection ? '刷新状态' : '读取仓库状态'}</button></div>
    {error && <Notice tone="bad">{error}</Notice>}
    {inspection && <>
      <div className="rt-row rt-between"><span className="rt-small rt-muted">检查于 {dateTime(inspection.inspected_at)} · {inspection.repositories.length} 个仓库</span><Badge value="ready">已读取当前状态</Badge></div>
      <div className="rt-toolbar"><input type="search" aria-label="搜索 SDK 仓库" placeholder="搜索仓库、分支或提交说明" value={query} onChange={event => setQuery(event.target.value)} /><label className="rt-check"><input type="checkbox" checked={onlyWatched} onChange={event => setOnlyWatched(event.target.checked)} />只看关注的仓库</label></div>
      <div className="rt-table-wrap"><table className="rt-table"><thead><tr><th>关注</th><th>仓库 / 分支</th><th>最近提交</th><th>本地状态</th></tr></thead><tbody>{repositories.map(repo => <tr key={repo.path}><td><input type="checkbox" aria-label={`关注 ${repo.path}`} checked={watch.includes(repo.path)} onChange={event => { setWatch(current => event.target.checked ? [...current, repo.path] : current.filter(path => path !== repo.path)); setSaved(false); }} /></td><td>{repo.path}<small>{repo.branch || '未指定分支'}</small></td><td>{repo.subject || '未提供提交说明'}<details><summary>提交标识</summary><span className="rt-code">{repo.head}</span></details></td><td>修改 {count(repo.tracked_changes)} 项<small>未跟踪 {count(repo.untracked_files)} 项</small><TechnicalDetails value={repo} label="查看仓库详情" /></td></tr>)}</tbody></table></div>
      {!repositories.length && <p className="rt-muted rt-small">没有符合筛选的仓库。</p>}
      <div className="rt-footer"><p className="rt-small rt-muted">本地修改可以用于构建；实际输入以运行时保存的快照为准。</p><button type="button" disabled={busy || saved} onClick={() => void saveWatch()}>{saved ? '关注列表已保存' : '保存关注列表'}</button></div>
      <TechnicalDetails value={inspection.manifest} label="Manifest 技术详情" />
    </>}
  </div>;
}
