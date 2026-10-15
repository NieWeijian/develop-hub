"""Presentation-only explanations from saved assertions and verified evidence.

Never change a stored verdict or infer a passing result from auxiliary checks.
"""
import ast
import hashlib
import json
from pathlib import Path
import re


def assertion_passed(assertion):
    return assertion.get('passed', assertion.get('status') == 'passed') is True


def assertion_role(assertion):
    if assertion.get('id') == 'case-completed':
        return 'summary'
    if assertion.get('role') in ('auxiliary', 'business'):
        return assertion['role']
    name = str(assertion.get('name') or assertion.get('label') or '')
    if assertion.get('id') == 'device-identity' or re.search(
            r'^Tool SHA-?256$|^测试工具完整性$|设备身份一致|stable frame|^Actual XU evidence$', name, re.I):
        return 'auxiliary'
    return 'business'


def protocol_evidence(case, evidence, root):
    """Read only a registered, bounded, digest-verified file for this case."""
    wanted = next((e for e in case.get('evidence', [])
                   if e.get('path') == 'output/execution/protocol.jsonl'), None)
    if not wanted or not root:
        return [], None
    for entry in evidence:
        if entry.get('sha256') != wanted.get('sha256') or not entry.get('sha256'):
            continue
        try:
            path = Path(entry['path']).resolve(strict=True)
            if not path.is_relative_to(Path(root).resolve()) or not path.is_file():
                continue
            with path.open('rb') as stream:
                raw = stream.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != wanted['sha256']:
                continue
            rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
            return [row for row in rows if isinstance(row, dict)], entry.get('evidence_id')
        except (OSError, ValueError, KeyError):
            continue
    return [], None


def unsupported_temperature(rows):
    for row in rows:
        if (not str(row.get('step', '')).startswith('temperature-')
                or row.get('command_id') != 0x1135 or row.get('direction') != 'RX'
                or row.get('return_code') != 0):
            continue
        try:
            raw = bytes.fromhex(row.get('hex', ''))
        except (ValueError, TypeError):
            continue
        if len(raw) < 2:
            continue
        size = int.from_bytes(raw[:2], 'little')
        if size & 0x8000 and raw[2:2 + (size & 0x7fff)] == b'unsupported command':
            return True
    return False


def failure_reasons(case, rows=(), evidence_id=None):
    if case.get('verdict') not in ('failed', 'error', 'blocked', 'interrupted'):
        return []
    if unsupported_temperature(rows):
        return [{'title': '固件不支持六路温度读取命令',
                 'detail': '第一次温度读取即被设备拒绝，未完成六路温度采样与统计。出流准备通过不代表温度测试通过。',
                 'expected': '支持 get-temperature（0x1135），返回六路有效温度',
                 'actual': '设备返回 unsupported command',
                 'suggestion': '核对固件是否包含温度命令处理，以及固件与控制工具的协议版本。',
                 'evidence_id': evidence_id}]
    reasons = []
    for assertion in case.get('assertions', []):
        if assertion_passed(assertion) or assertion_role(assertion) == 'summary':
            continue
        name = assertion.get('label') or assertion.get('name') or assertion.get('id') or '检查未通过'
        actual = assertion.get('actual')
        reason = {'title': name, 'detail': assertion.get('message') or '',
                  'expected': assertion.get('expected'), 'actual': actual}
        prefix = 'Missing, duplicate or unexpected proc nodes: '
        if isinstance(actual, str) and actual.startswith(prefix):
            try:
                nodes = ast.literal_eval(actual[len(prefix):])
                expected = assertion.get('expected')
                if isinstance(nodes, list) and isinstance(expected, list) and all(isinstance(n, str) for n in nodes + expected):
                    missing = [n for n in expected if n not in nodes]
                    extra = [n for n in nodes if n not in expected]
                    duplicate = sorted({n for n in nodes if nodes.count(n) > 1})
                    reason.update(title=('VICAP' if str(name).startswith('vicap') else 'ISP') + ' 状态返回的节点名称不符合预期',
                                  detail='；'.join(text for text in [
                                      '缺少节点：' + '、'.join(missing) if missing else '',
                                      '异常节点：' + '、'.join(extra) if extra else '',
                                      '重复节点：' + '、'.join(duplicate) if duplicate else ''] if text),
                                  actual=nodes,
                                  suggestion='本项在状态文本校验时中止，尚未完成后续计数比较；需检查设备返回及分段传输，不能据此判断传感器损坏。')
            except (ValueError, SyntaxError):
                pass
        reasons.append(reason)
    if not reasons:
        message = str(case.get('reason') or case.get('message') or '未取得完整的检查结果，请展开执行详情。')
        match = re.fullmatch(r'temperature-(\d+) tool exit (\d+)', message)
        reasons.append({'title': '温度读取工具执行失败' if match else '用例未完成' if case.get('verdict') != 'failed' else '用例未通过',
                        'detail': f'第 {int(match[1]) + 1} 次温度读取失败（工具退出码 {match[2]}），未完成温度统计。' if match else message})
    return reasons


def annotate_report(report, evidence_root=None):
    """Annotate the already detached API view; leave saved evidence untouched."""
    result = report.get('result') or {}
    for case in result.get('cases', []):
        for assertion in case.get('assertions', []):
            assertion['role'] = assertion_role(assertion)
        rows, evidence_id = ([], None)
        if case.get('verdict') in ('failed', 'error') and str(case.get('message', '')).startswith('temperature-'):
            rows, evidence_id = protocol_evidence(case, result.get('evidence', []), evidence_root)
        case['failure_reasons'] = failure_reasons(case, rows, evidence_id)
    return report
