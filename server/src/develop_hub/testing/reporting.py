"""Human-readable, self-contained report export from the saved run record."""
from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo

from .report_analysis import assertion_passed, failure_reasons


LABELS = {'passed': '通过', 'failed': '未通过', 'error': '执行异常', 'blocked': '未执行',
          'cancelled': '已取消', 'interrupted': '执行中断', 'not_tested': '未测试',
          'inconclusive': '无法判定', 'pending': '待执行', 'not_required': '无需恢复',
          'restored': '已恢复', 'unknown': '需检查设备'}


def readable(value):
    if value is None:
        return '未提供'
    if isinstance(value, bool):
        return '是' if value else '否'
    if isinstance(value, dict):
        return '；'.join(str(k) + '：' + readable(v) for k, v in value.items())
    if isinstance(value, list):
        return '、'.join(map(readable, value))
    return str(value)


def text(value):
    return escape(readable(value), quote=True)


def timestamp(value):
    if not value:
        return '—'
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not parsed.tzinfo:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')


def report_html(report):
    summary = report['summary']
    snapshot = report['request_snapshot']
    result = report.get('result', {})
    sections = []
    for case in snapshot.get('cases', []):
        outcome = next((c for c in result.get('cases', []) if c['binding_id'] == case['binding_id']), {})
        definition = case.get('definition', {})
        ui = definition.get('parameter_ui', {})
        parameters = '；'.join(text(ui.get(k, {}).get('label', k)) + '：' + text(v) + text(ui.get(k, {}).get('unit', '')) for k, v in case.get('parameters', {}).items()) or '无'
        rows = []
        for assertion in outcome.get('assertions', []):
            label = assertion.get('name') or assertion.get('label') or assertion.get('id')
            expected, actual = assertion.get('expected'), assertion.get('actual')
            if any(word in str(label).lower() for word in ('sha256', 'sha-256', 'checksum', 'hash')):
                label, expected, actual = '文件完整性', '与登记文件一致', '校验通过' if assertion.get('passed') else '校验失败'
            unit = text(assertion.get('unit', ''))
            auxiliary = '（辅助检查）' if assertion.get('role') == 'auxiliary' else ''
            rows.append(f'<tr><td>{text(label)}{auxiliary}</td><td>{text(expected)} {unit}</td><td>{text(actual)} {unit}</td><td>{"通过" if assertion_passed(assertion) else "未通过"}</td></tr>')
        recovery = outcome.get('recovery', {})
        recovery = recovery.get('state', 'unknown') if isinstance(recovery, dict) else recovery
        verdict = LABELS.get(outcome.get('verdict', 'blocked'), outcome.get('verdict', '未执行'))
        reasons = outcome.get('failure_reasons', failure_reasons(outcome))
        failures = []
        for reason in reasons:
            comparison = (f'<p>预期：{text(reason.get("expected"))}<br>实际：{text(reason.get("actual"))}</p>'
                          if 'expected' in reason or 'actual' in reason else '')
            suggestion = f'<p>排查建议：{text(reason["suggestion"])}</p>' if reason.get('suggestion') else ''
            failures.append(f'<div class="failure"><strong>{text(reason["title"])}</strong><p>{text(reason.get("detail", ""))}</p>{comparison}{suggestion}</div>')
        checks = (f'<details><summary>检查详情（{len(rows)} 项，含辅助检查）</summary><p>辅助检查通过不代表整个用例通过；表格并非执行时间顺序。</p><table><thead><tr><th>检查项</th><th>判断标准</th><th>实际结果</th><th>结论</th></tr></thead><tbody>{"".join(rows)}</tbody></table></details>' if rows else '')
        scope = f'<details><summary>测试内容与判定标准</summary><p>{text(definition.get("test_scope", ""))}</p></details>' if definition.get('test_scope') else ''
        sections.append(f'<section><h2>{text(case["name"])} · {text(verdict)}</h2><p>{"必需项" if case["required"] else "补充观察项"} · {text(LABELS.get(recovery, recovery))}</p>{"".join(failures)}{checks}{scope}<details><summary>本次实际参数</summary><p>{parameters}</p></details></section>')
    records = ''.join(f'<p><strong>{text(r["operator"])}</strong> · {timestamp(r["created_at"])} · {text(LABELS[r["decision"]])}</p><p>{text(r["values"])}</p>' for r in report.get('operator_records', [])) or '<p>本次没有人工记录。</p>'
    events = ''.join(f'<li>{timestamp(e.get("at"))} · {text(e.get("message", ""))}</li>' for e in report.get('events', []))
    evidence = ''.join(f'<li>{text(e["name"])} · {e.get("size_bytes", 0)} 字节</li>' for e in result.get('evidence', []))
    title = text(summary.get('plan_name') or '测试报告')
    notice = '<p class="note">模拟运行：本报告用于验证平台流程，不代表真实设备测试结论。</p>' if summary.get('simulated') else ''
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>{title} · 测试报告</title>
<style>body{{font:15px/1.7 system-ui,sans-serif;max-width:1050px;margin:40px auto;padding:20px;color:#17372b}}h1{{font-size:28px}}table{{width:100%;border-collapse:collapse}}td,th{{text-align:left;padding:12px;border:1px solid #dce5de;overflow-wrap:anywhere}}th{{background:#edf3ef}}section{{margin:30px 0}}.note{{background:#fff4df;padding:16px}}small{{color:#607469}}.failure{{border-left:4px solid #b34335;background:#fff4f2;padding:16px;margin:12px 0;overflow-wrap:anywhere}}details{{margin:14px 0}}summary{{cursor:pointer}}@media print{{section{{break-inside:avoid}}}}</style>
<h1>{title}</h1>{notice}<p>测试结论：<strong>{text(LABELS.get(report['verdict'], report['verdict']))}</strong> · {summary['passed']} / {summary['total']} 项通过</p>
<p>产品：{text(summary.get('product'))} · 设备：{text(summary.get('device'))}</p>
<p>开始：{timestamp(report['created_at'])} · 完成：{timestamp(report.get('completed_at'))}（北京时间）</p>
<p>设备身份：{text(result.get('device_observation', {}).get('serial'))} · 固件版本：{text(result.get('device_observation', {}).get('firmware_version'))}</p>
{''.join(sections)}<h2>人工记录</h2>{records}<h2>执行过程</h2><ol>{events}</ol><h2>证据附件</h2><ul>{evidence}</ul><p>原始附件在平台运行报告中下载，未嵌入本文件。</p><small>运行标识：{text(report['id'])} · 根据服务端保存的运行记录生成</small></html>'''
