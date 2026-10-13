'use client';

import { FormEvent, useEffect, useState } from 'react';

type Product = {id:string;name:string};
type Api = <T>(path:string,init?:RequestInit)=>Promise<T>;
export type PrebuiltImport = {product_id:string;name:string;image_directory:string;operator:string;identity:{kind:'version'|'commit';value:string}};
type Options = {allowed_root:string;recommended_image_directory?:string;image_directory?:string;required_filenames:string[];board_config:string};
type Props = {products:Product[];initialProductId?:string;api:Api;busy:boolean;onImport:(value:PrebuiltImport)=>void};

function ImportFields({productId,api,busy,onImport}:{productId:string;api:Api;busy:boolean;onImport:Props['onImport']}) {
  const [options,setOptions]=useState<Options|null>(null);
  const [directory,setDirectory]=useState('');
  const [identityKind,setIdentityKind]=useState<'version'|'commit'>('version');
  const [error,setError]=useState('');
  useEffect(()=>{
    const controller=new AbortController();
    void api<Options>('/api/qa/platform/prebuilt-options?product_id='+encodeURIComponent(productId),{signal:controller.signal})
      .then(value=>{if(!controller.signal.aborted){setOptions(value);setDirectory(value.recommended_image_directory||value.image_directory||'');}})
      .catch(cause=>{if(!controller.signal.aborted)setError(cause instanceof Error?cause.message:String(cause));});
    return()=>controller.abort();
  },[api,productId]);
  function submit(event:FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if(!options||busy)return;
    const data=new FormData(event.currentTarget);
    onImport({product_id:productId,name:String(data.get('name')||'').trim(),image_directory:directory.trim(),operator:String(data.get('operator')||'').trim(),identity:{kind:identityKind,value:String(data.get('identity_value')||'').trim()}});
  }
  if(error)return <p className="error-banner" role="alert">{error}</p>;
  if(!options)return <p role="status">正在读取产品的固件目录…</p>;
  return <form onSubmit={submit} className="platform-prebuilt-form">
    <div className="form-columns"><label>候选版本名称<input name="name" required maxLength={160} placeholder="例如 factory-V2.0.7-1 本地验证" disabled={busy}/></label><label>操作人<input name="operator" required maxLength={120} autoComplete="name" disabled={busy}/></label></div>
    <label>175 上的镜像目录<input value={directory} onChange={event=>setDirectory(event.target.value)} required disabled={busy} spellCheck={false} aria-describedby="prebuilt-directory-help"/></label>
    <p id="prebuilt-directory-help" className="platform-hint">选择 SDK 产物目录，需位于 <code>{options.allowed_root}</code> 内。请先完成本地编译，导入期间保持镜像文件不变。</p>
    <p className="platform-hint">将校验并保存 {options.required_filenames.join(' / ')} 的独立副本，记录大小及 SHA-256。后续测试使用这份固定镜像。</p>
    <div className="form-columns"><label>烧录后身份核验<select value={identityKind} onChange={event=>setIdentityKind(event.target.value as 'version'|'commit')} disabled={busy}><option value="version">版本标识（推荐）</option><option value="commit">提交哈希</option></select></label><label>{identityKind==='version'?'预期版本标识':'预期提交哈希'}<input key={identityKind} name="identity_value" required minLength={identityKind==='version'?3:7} maxLength={identityKind==='version'?512:40} pattern={identityKind==='commit'?'[0-9a-fA-F]{7,40}':undefined} placeholder={identityKind==='version'?'factory-V2.0.7-1-gabcdef0':'7～40 位 Git 提交哈希'} disabled={busy} spellCheck={false} aria-describedby="prebuilt-identity-help"/></label></div>
    <p id="prebuilt-identity-help" className="platform-hint">{identityKind==='version'?'填写本固件预期的 get-version 响应或其中完整的版本标识（如 factory-V2.0.7-1-gabcdef0），用于烧录后核对。':'填写本固件对应的 Git 提交哈希，烧录后核对模组版本信息中的提交。'}</p>
    <p className="platform-hint">导入仅校验并保存镜像，不编译、不烧录。创建清单后，选择用例并启动测试时才会烧录。</p>
    <button type="submit" className="primary-button" disabled={busy||!directory.trim()}>{busy?'正在校验并保存镜像…':'导入镜像并继续'}</button>
  </form>;
}

export default function PrebuiltCandidateForm({products,initialProductId,api,busy,onImport}:Props) {
  const [choice,setChoice]=useState(initialProductId||'');
  const productId=products.some(product=>product.id===choice)?choice:products.length===1?products[0].id:'';
  return <div className="platform-prebuilt-import">
    <label>测试产品<select required value={productId} onChange={event=>setChoice(event.target.value)} disabled={busy}><option value="">选择产品</option>{products.map(product=><option key={product.id} value={product.id}>{product.name}</option>)}</select></label>
    {productId?<ImportFields key={productId} productId={productId} api={api} busy={busy} onImport={onImport}/>:<p className="platform-hint">先选择镜像对应的产品，读取支持的固件目录与镜像清单。</p>}
  </div>;
}
