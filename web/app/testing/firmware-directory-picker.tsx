'use client';

import { useEffect, useState } from 'react';
import { failureMessage, testingApi } from '../../lib/testing-api';
import { Notice } from './shared';

type ImageFile = { name: string; path: string; size_bytes: number };
type Listing = { roots: { id: string; name: string; path: string }[]; root_id: string; path: string; parent: string | null; directories: { name: string; path: string }[]; files: ImageFile[]; truncated: boolean };
const recommended = new Set(['boot.img', 'system.img', 'oem.img']);

function size(bytes: number): string {
  return bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(1)} GB` : bytes >= 1024 ** 2 ? `${(bytes / 1024 ** 2).toFixed(1)} MB` : `${(bytes / 1024).toFixed(1)} KB`;
}

export default function FirmwareDirectoryPicker({ onSelect, onCancel }: { onSelect: (files: ImageFile[]) => void; onCancel: () => void }) {
  const [location, setLocation] = useState({ root: '', path: '', attempt: 0 });
  const [listing, setListing] = useState<Listing | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [filter, setFilter] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    void testingApi<Listing>(`/firmware-directories?root_id=${encodeURIComponent(location.root)}&path=${encodeURIComponent(location.path)}`, { signal: controller.signal }).then(data => {
      if (controller.signal.aborted) return;
      setListing(data);
      const valid = data.files.filter(file => file.size_bytes > 0);
      const common = valid.filter(file => recommended.has(file.name.toLowerCase()));
      setSelected((common.length ? common : valid.slice(0, 100)).map(file => file.path));
    }).catch(cause => { if (!controller.signal.aborted) setError(failureMessage(cause)); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [location]);
  function navigate(root: string, path: string) {
    setLoading(true); setError(''); setSelected([]); setFilter('');
    setLocation(current => ({ root, path, attempt: current.attempt + 1 }));
  }
  const currentRoot = listing?.roots.find(root => root.id === listing.root_id);
  const segments = listing?.path.split('/').filter(Boolean) || [];
  const files = listing?.files || [];
  const chosen = files.filter(file => selected.includes(file.path));
  return <section className="rt-directory-picker" aria-label="选择平台镜像目录" aria-busy={loading}>
    <div className="rt-section-head"><div><h3>选择镜像文件夹</h3><p className="rt-small rt-muted">浏览平台主机上的目录。进入文件夹后，确认需要导入的镜像。</p></div><button type="button" onClick={onCancel}>取消选择</button></div>
    {!!listing?.roots.length && <label className="rt-field"><span>目录入口</span><select value={listing.root_id} disabled={loading} onChange={event => navigate(event.target.value, '')}>{listing.roots.map(root => <option key={root.id} value={root.id}>{root.name} · {root.path}</option>)}</select></label>}
    {listing && currentRoot && <><nav className="rt-directory-breadcrumb" aria-label="当前文件夹"><button type="button" disabled={loading} onClick={() => navigate(listing.root_id, '')}>{currentRoot.name}</button>{segments.map((segment, index) => <span key={segments.slice(0, index + 1).join('/')}><span aria-hidden="true"> / </span><button type="button" disabled={loading} onClick={() => navigate(listing.root_id, segments.slice(0, index + 1).join('/'))}>{segment}</button></span>)}</nav><p className="rt-small rt-muted rt-directory-path">{currentRoot.path}{listing.path ? `/${listing.path}` : ''}</p></>}
    {error && <Notice tone="bad" action={<button type="button" onClick={() => navigate(location.root, location.path)}>重试</button>}>{error}</Notice>}
    {loading ? <p role="status">正在读取文件夹…</p> : listing && !listing.roots.length ? <Notice>尚未登记可浏览的目录，请先登记本地 SDK 环境，或联系平台维护人员添加镜像目录。</Notice> : listing && <>
      <div className="rt-toolbar"><button type="button" disabled={listing.parent === null} onClick={() => navigate(listing.root_id, listing.parent || '')}>↑ 返回上级</button><input aria-label="筛选当前目录" type="search" placeholder="筛选文件夹或镜像名称" value={filter} onChange={event => setFilter(event.target.value)} /></div>
      {!error && <div className="rt-directory-entries">
        {listing.directories.filter(item => item.name.toLowerCase().includes(filter.toLowerCase())).map(directory => <button className="rt-directory-folder" type="button" key={directory.path} onClick={() => navigate(listing.root_id, directory.path)}><span aria-hidden="true">📁</span><span>{directory.name}</span><span aria-hidden="true">→</span></button>)}
        {files.filter(item => item.name.toLowerCase().includes(filter.toLowerCase())).map(file => <label className="rt-directory-file" key={file.path}><input type="checkbox" checked={selected.includes(file.path)} disabled={!file.size_bytes || (!selected.includes(file.path) && selected.length >= 100)} onChange={event => setSelected(current => event.target.checked ? [...current, file.path] : current.filter(path => path !== file.path))} /><span>{file.name}</span><small>{file.size_bytes ? size(file.size_bytes) : '空文件，不能导入'}</small></label>)}
        {!listing.directories.length && !files.length && <p className="rt-muted">此目录没有子文件夹或镜像文件，请返回上级选择其他目录。</p>}
        {!!(listing.directories.length + files.length) && ![...listing.directories, ...files].some(item => item.name.toLowerCase().includes(filter.toLowerCase())) && <p className="rt-muted">没有匹配的文件夹或镜像。</p>}
      </div>}
      {listing.truncated && <Notice>此目录内容较多，仅显示前 1000 项。可用高级路径输入直接指定文件。</Notice>}
      {!error && <div className="rt-footer"><span className="rt-small" role="status">已选择 {chosen.length} 个镜像 · {size(chosen.reduce((total, file) => total + file.size_bytes, 0))}</span><button className="rt-primary" type="button" disabled={!chosen.length} onClick={() => onSelect(chosen)}>使用所选镜像</button></div>}
    </>}
  </section>;
}
