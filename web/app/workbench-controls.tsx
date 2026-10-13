"use client";

import { ReactNode, useState } from 'react';

type SelectableCase = {id:string;code:string;name:string;description:string};
const groups = ['基础读写','模式与算法','信息上报','文件与日志','工厂与持久化','出流质量','其他'];
export function caseGroup(code:string) {
  if(code.startsWith('BASIC-')) return groups[0];
  if(code.startsWith('SWITCH-')) return groups[1];
  if(code.startsWith('INFO-')) return groups[2];
  if(code.startsWith('FILE-')) return groups[3];
  if(code.startsWith('FACTORY-')) return groups[4];
  if(code.startsWith('STREAM-')) return groups[5];
  return groups[6];
}

export function CaseSelection({cases,selected,onChange,renderParameters}:{cases:SelectableCase[];selected:string[];onChange:(ids:string[])=>void;renderParameters:(item:SelectableCase)=>ReactNode}) {
  const [query,setQuery]=useState('');
  const needle=query.trim().toLowerCase();
  const visible=cases.filter(c=>!needle||`${c.code} ${c.name} ${c.description}`.toLowerCase().includes(needle));
  const selectedSet=new Set(selected);
  function toggle(ids:string[],checked:boolean) {
    const updated=new Set(selected);
    for(const id of ids) {if(checked)updated.add(id);else updated.delete(id);}
    onChange(cases.filter(c=>updated.has(c.id)).map(c=>c.id));
  }
  return <div className="wb-case-picker">
    <div className="wb-case-toolbar"><label>查找用例<input type="search" value={query} placeholder="名称或编号" onChange={e=>setQuery(e.target.value)}/></label><button type="button" disabled={!visible.length} onClick={()=>toggle(visible.map(c=>c.id),true)}>{needle?'全选搜索结果':'全选可用用例'}</button><button type="button" disabled={!selected.length} onClick={()=>onChange([])}>清空选择</button></div>
    {!cases.length&&<p>此模式暂无可执行用例，请检查用例库。</p>}
    {cases.length>0&&!visible.length&&<p>没有匹配的用例。已选项目仍然保留。</p>}
    {groups.map(group=>{
      const items=visible.filter(c=>caseGroup(c.code)===group);
      if(!items.length)return null;
      const count=items.filter(c=>selectedSet.has(c.id)).length;
      return <fieldset className="wb-case-group" key={group}><legend>{group} · {count}/{items.length}</legend><div className="wb-case-group-actions"><button type="button" onClick={()=>toggle(items.map(c=>c.id),count!==items.length)}>{count===items.length?'取消本组':'选择本组'}</button></div>{items.map(item=><div className="wb-case-option" key={item.id}><label className="wb-checkbox-row"><input type="checkbox" checked={selectedSet.has(item.id)} onChange={e=>toggle([item.id],e.target.checked)}/><span><strong>{item.name}</strong><small>{item.code}</small></span></label><details className="wb-case-description"><summary>测试说明</summary><p>{item.description}</p></details>{selectedSet.has(item.id)&&renderParameters(item)}</div>)}</fieldset>;
    })}
  </div>;
}
