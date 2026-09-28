"""Operator classifications overlay immutable automatic results; events are the audit log."""
def classifications(plans, controller, events):
    latest = {}
    history = {}
    for event in sorted(events, key=lambda e: e.sequence):
        if event.event_type != 'case_classification':
            continue
        d = event.details
        entry = {**d, 'revision': event.sequence, 'changed_at': event.created_at.isoformat()}
        latest[d['code']] = entry
        history.setdefault(d['code'], []).append(entry)
    results = (controller or {}).get('cases') or []
    output = {}
    for plan in plans:
        code = plan.get('code')
        result = next((r for r in results if r.get('id') == plan.get('id') or (code and r.get('code') == code)), {})
        auto = result.get('outcome')
        change = latest.get(code, {})
        output[code] = {
            'automatic_outcome': auto,
            'outcome': change.get('outcome') or auto,
            'manual': bool(change.get('outcome')),
            'revision': change.get('revision', 0),
            'history': history.get(code, []),
            'executed': result.get('state') == 'finished' and auto in ('passed', 'failed', 'error'),
        }
    return output

def validate_change(row, code, revision):
    if row.status.value not in ('test_complete', 'test_failed', 'bench_failed', 'blocked'):
        raise ValueError('轮次尚未结束，不能修改分类')
    values = classifications(row.test_case_snapshot, row.controller_result, row.events)
    if code not in values or not values[code]['executed']:
        raise ValueError('仅可修改本轮已经执行完成的用例')
    current = values[code]
    if current['revision'] != revision:
        raise ValueError('分类已被其他操作更新，请刷新后重试')
    return current
