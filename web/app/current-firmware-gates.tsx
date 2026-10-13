'use client';

import type { Definition } from './special-scenarios';

type Props = {definitions:Definition[];selectedCode:string;onSelect:(code:string)=>void;operator:string;onOperator:(value:string)=>void;disabled:boolean};

export default function CurrentFirmwareGates({definitions,selectedCode,onSelect,operator,onOperator,disabled}:Props) {
  return <fieldset className="wb-case-group" disabled={disabled}>
    <legend>特殊场景 · 人工门控</legend>
    <p>每次选择一个场景，按页面指引逐步确认。沿用上方选定的产品、测试台和模组，直接测试当前固件。</p>
    {definitions.map(definition=><div className="wb-case-option" key={definition.code}>
      <label className="wb-checkbox-row"><input type="radio" name="current-firmware-scenario" value={definition.code} required checked={selectedCode===definition.code} onChange={()=>onSelect(definition.code)}/><span><strong>{definition.name}</strong><small>{definition.code} · {definition.steps.length} 个门控步骤</small></span></label>
      <p>{definition.description}</p>
      <details className="wb-case-description"><summary>查看操作步骤与记录要求</summary><ol>{definition.steps.map(step=><li key={step.id}><strong>{step.title}</strong><p>{step.instruction}</p><small>记录：{step.records.join('；')}</small></li>)}</ol></details>
    </div>)}
    {!definitions.length&&<p role="status">暂无已启用的人工门控用例，请检查用例库或等待定义加载。</p>}
    <label>本次操作者<input name="current-firmware-operator" value={operator} onChange={event=>onOperator(event.target.value)} required maxLength={120} autoComplete="name" placeholder="姓名或工号"/></label>
    <p className="platform-hint">启动仅建立门控流程并读取、锁定模组身份，不预先烧录固件；写 SN、日志压力、老化和异常 OTA 等操作在后续步骤中逐项确认。</p>
  </fieldset>;
}
