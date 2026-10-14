'use client';

import { useId } from 'react';
import type { ParameterSchema, ParameterUi, Values } from './types';
import { readable } from './shared';

export function schemaDefaults(schema?: ParameterSchema): Values {
  return Object.fromEntries(Object.entries(schema?.properties || {}).flatMap(([key, field]) => {
    if (field.default !== undefined) return [[key, field.default]];
    if (field.type === 'object') { const nested = schemaDefaults(field); return Object.keys(nested).length ? [[key, nested]] : []; }
    return [];
  }));
}

export function SchemaFields({ schema, ui = {}, values, onChange, disabled = false, showDefaults = true, prefix = '' }: {
  schema?: ParameterSchema; ui?: Record<string, ParameterUi>; values: Values; onChange: (values: Values) => void; disabled?: boolean; showDefaults?: boolean; prefix?: string;
}) {
  const groupId = useId();
  const fields = Object.entries(schema?.properties || {}).sort(([a], [b]) => (ui[a]?.order ?? 100) - (ui[b]?.order ?? 100));
  if (!fields.length) return <p className="rt-muted rt-small">此用例没有可配置参数。</p>;
  function change(key: string, value: unknown) { onChange({ ...values, [key]: value }); }
  function reset(key: string) { const next = { ...values }; delete next[key]; onChange(next); }
  return <div className="rt-schema-grid">{fields.map(([key, field]) => {
    const meta = ui[prefix + key] || ui[key] || {};
    const id = `${groupId}-${key}`;
    const required = schema?.required?.includes(key) || false;
    const overridden = Object.hasOwn(values, key);
    const value = overridden ? values[key] : field.default;
    const label = meta.label || field.title || key;
    const description = meta.help || field.description;
    if (field.type === 'object') return <fieldset className="rt-nested-fields" key={key}><legend>{label}{required && ' *'}</legend>{description && <p className="rt-small rt-muted">{description}</p>}{value === undefined ? <button type="button" disabled={disabled} onClick={() => change(key, schemaDefaults(field))}>{required ? '填写参数组' : '启用此参数组'}</button> : <><SchemaFields schema={field} ui={ui} values={value && typeof value === 'object' && !Array.isArray(value) ? value as Values : {}} onChange={next => change(key, next)} disabled={disabled} showDefaults={showDefaults} prefix={`${prefix}${key}.`} />{!required && field.default === undefined && <button type="button" disabled={disabled} onClick={() => reset(key)}>取消此参数组</button>}</>}</fieldset>;
    if (field.type === 'array' && field.items && (!['string', 'number', 'integer'].includes(field.items.type || '') || field.items.enum)) {
      const items = Array.isArray(value) ? value : [];
      return <fieldset className="rt-nested-fields" key={key}><legend>{label}{required && ' *'}</legend>{items.map((item, index) => <div key={index}><SchemaFields schema={{ type: 'object', properties: { value: { ...field.items, title: `第 ${index + 1} 项` } }, required: ['value'] }} values={{ value: item }} onChange={next => change(key, items.map((old, i) => i === index ? next.value : old))} disabled={disabled} showDefaults={false} /><button type="button" disabled={disabled} onClick={() => change(key, items.filter((_, i) => i !== index))}>移除此项</button></div>)}<button type="button" disabled={disabled} onClick={() => change(key, [...items, field.items?.default ?? (field.items?.type === 'object' ? {} : field.items?.type === 'array' ? [] : field.items?.type === 'boolean' ? false : '')])}>添加一项</button></fieldset>;
    }
    return <div className="rt-field" key={key}>
      <label htmlFor={id}>{label}{meta.unit && <span className="rt-unit">（{meta.unit}）</span>}{required && <span aria-label="必填"> *</span>}</label>
      {field.enum ? <select id={id} value={value === undefined || value === null ? '' : String(field.enum.findIndex(option => option === value))} disabled={disabled} required={required} onChange={event => change(key, event.target.value === '' ? null : field.enum![Number(event.target.value)])}><option value="">请选择</option>{field.enum.map((option, index) => <option key={index} value={index}>{readable(option)}</option>)}</select>
        : field.type === 'boolean' ? <select id={id} value={value === undefined || value === null ? '' : value ? 'true' : 'false'} disabled={disabled} required={required} onChange={event => change(key, event.target.value === '' ? null : event.target.value === 'true')}><option value="">请选择</option><option value="true">是</option><option value="false">否</option></select>
        : field.type === 'array' ? <textarea id={id} value={Array.isArray(value) ? value.map(String).join('\n') : ''} placeholder="每行填写一项" disabled={disabled} required={required} onChange={event => change(key, event.target.value.split('\n').filter(Boolean).map(item => ['number', 'integer'].includes(field.items?.type || '') ? Number(item) : item))} />
        : <input id={id} type={field.type === 'number' || field.type === 'integer' ? 'number' : 'text'} value={value === undefined || value === null ? '' : String(value)} min={field.minimum} max={field.maximum} step={field.type === 'integer' ? 1 : 'any'} minLength={field.minLength} maxLength={field.maxLength} pattern={field.pattern} disabled={disabled} required={required} onChange={event => change(key, ['number', 'integer'].includes(field.type || '') ? event.target.value === '' ? null : Number(event.target.value) : event.target.value)} />}
      {description && <small id={`${id}-help`}>{description}</small>}
      {showDefaults && <small className="rt-parameter-origin">{overridden ? <><span>本方案设置</span>{field.default !== undefined && <button type="button" className="rt-link" onClick={() => reset(key)} disabled={disabled}>恢复默认 {readable(field.default)}{meta.unit || ''}</button>}</> : field.default !== undefined ? `使用默认值 · ${readable(field.default)}${meta.unit || ''}` : required ? '此项没有默认值，请填写' : '可选 · 未设置'}</small>}
    </div>;
  })}</div>;
}

export function ParameterSummary({ values, ui }: { values: Values; ui?: Record<string, ParameterUi> }) {
  return <dl className="rt-facts">{Object.entries(values).map(([key, value]) => <div key={key}><dt>{ui?.[key]?.label || key}</dt><dd>{readable(value)}{ui?.[key]?.unit && ` ${ui[key].unit}`}</dd></div>)}</dl>;
}
