"""New-platform entry point for the maintained RMSL321 test implementations."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

import bridge_support as support


def normalized_result(data, legacy, before, after, required):
    assertions = [
        {'id': 'device-identity', 'name': '测试前后设备身份一致', 'expected': before['serial'],
         'actual': after.get('serial'), 'passed': bool(after) and before['serial'] == after.get('serial')
         and before['usb_path'] == after.get('usb_path') and before['firmware_version'] == after.get('firmware_version')},
        {'id': 'case-completed', 'name': '用例完整执行', 'expected': '执行完成且检查项齐全',
         'actual': legacy.get('message') or legacy.get('outcome'),
         'passed': legacy.get('state') == 'finished' and legacy.get('outcome') == 'passed' and bool(legacy.get('assertions'))},
    ]
    for index, item in enumerate(legacy.get('assertions', [])):
        item = deepcopy(item)
        item['id'] = f'check-{index + 1}'
        if item.get('name') == 'Tool SHA-256':
            item.update(name='测试工具完整性', expected='与平台安装的工具一致', actual='校验通过' if item.get('passed') else '校验失败')
        assertions.append(item)
    original = deepcopy(legacy.get('recovery') or {'state': 'not_required'})
    state = original.get('state')
    if state == 'passed':
        original['state'] = 'restored'
    elif state == 'retained_temporary_file':
        original.update(state='restored', detail='测试文件保留在独立 /tmp 路径，重启后清除；设备控制状态未改变')
    elif state not in ('not_required', 'restored') or (required and state == 'not_required'):
        original['state'] = 'failed'
    if not after:
        original['state'] = 'failed'
    verdict = 'passed' if all(a['passed'] for a in assertions) else 'failed'
    if legacy.get('outcome') not in ('passed', 'failed') or original['state'] == 'failed':
        verdict = 'error'
    return {'result_schema': 'rk-case-result/1', 'run_id': data['run_id'], 'binding_id': data['binding_id'],
            'case_revision_id': data['case_revision_id'], 'state': 'completed', 'verdict': verdict,
            'assertions': assertions, 'recovery': original, 'simulation': False,
            'message': legacy.get('message', ''), 'metrics': legacy.get('metrics', {}),
            'retained_test_file': legacy.get('retained_test_file'), 'attachments': []}


def main(code):
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    data = json.loads(Path(args.input).read_text())
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    config = data['device']['config']
    support.configure(config)
    before = support.inspect()
    definition = json.loads((Path(__file__).parent / 'definitions.json').read_text())[code]
    definition['support_sha256'] = {key: value['sha256'] for key, value in config['helpers'].items()}
    old_input = {'tool': config['ctl']['path'], 'tool_sha256': config['ctl']['sha256'],
                 'tools_package': {'capabilities': config['tool_capabilities']},
                 'definition': definition, 'parameters': data['parameters'], 'video': before['video'],
                 'device': {**before, 'version': before['firmware_version']}}
    if code == 'STREAM-RGB-DEPTH':
        from stream_quality import StreamCase as Case
    elif code.startswith('INFO-'):
        from telemetry_runtime import InfoCase as Case
    elif code.startswith(('FACTORY-', 'FILE-')):
        from factory_runtime import FactoryCase as Case
    elif code.startswith('SWITCH-') and code != 'SWITCH-ROTATE-CONTROL':
        from switch_runtime import SwitchCase as Case
    else:
        from test_runtime import Case
    legacy_dir = output / 'execution'
    case = Case(code, old_input, legacy_dir)
    case.run()
    try:
        after = support.inspect()
    except Exception:
        after = {}
    result = normalized_result(data, case.result, before, after, definition['risk_level'] != 'read_only')
    result['attachments'] = [{'path': p.relative_to(output).as_posix()} for p in sorted(legacy_dir.rglob('*')) if p.is_file()]
    (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    return 1 if result['verdict'] == 'error' else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv.pop(1)))
