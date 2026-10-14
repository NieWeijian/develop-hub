#!/usr/bin/env python3
"""Build a portable source-only case package; no legacy database or scheduler."""
import argparse
import importlib.util
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / 'bench'
RETIRED = {'INFO-IMU', 'SWITCH-ALGO-ASYNC', 'SWITCH-ALGO-ITER', 'SWITCH-ALGO-DENOISE', 'SWITCH-IR-BTNR'}
LABELS = {'target_fov': '目标视场角（不填则自动选择）', 'max_rtt_ms': '单次响应时间上限',
          'window_seconds': '统计窗口', 'fps_tolerance_percent': '帧率允许偏差', 'duration_seconds': '持续测试时长',
          'rgb_width': 'RGB 宽度', 'rgb_height': 'RGB 高度', 'depth_width': '深度图宽度', 'depth_height': '深度图高度',
          'fps': '目标帧率', 'stall_timeout_seconds': '断流超时', 'warmup_seconds': '预热时长', 'thresholds': '性能阈值（可选）'}


def schema_for(definition):
    properties = definition.get('parameters', {})
    for key, value in properties.items():
        value['title'] = LABELS.get(key, key)
        if key == 'thresholds':
            metrics = {name: {'type': 'number', 'minimum': 0} for name in (
                'min_fps', 'max_transport_drop_percent', 'max_latency_mean_ms', 'max_latency_max_ms', 'max_interval_jitter_ms', 'max_latency_jitter_ms')}
            value.update(additionalProperties=False, properties={stream: {'type': 'object', 'additionalProperties': False, 'properties': metrics} for stream in ('rgb', 'depth')})
        elif value['type'] in ('number', 'integer'):
            value['exclusiveMinimum'] = 0
            value['maximum'] = {'duration_seconds': 84600, 'max_rtt_ms': 30000, 'window_seconds': 30,
                'fps_tolerance_percent': 50, 'target_fov': 180, 'fps': 60, 'warmup_seconds': 60, 'stall_timeout_seconds': 300}.get(key, 4096)
    return {'type': 'object', 'additionalProperties': False, 'properties': properties}


def build(destination):
    codes = ['BASIC-FOV-READ', 'BASIC-FOV-WRITE', 'BASIC-FILE-ROUNDTRIP', 'SWITCH-ROTATE-CONTROL']
    for name in ('switch-codes.json', 'info-codes.json', 'factory-codes.json'):
        codes += json.loads((BENCH / name).read_text())
    codes = [code for code in codes if code not in RETIRED]
    definitions, cases, files = {}, [], {}
    for name in ('test_runtime.py', 'switch_runtime.py', 'telemetry_runtime.py', 'factory_runtime.py', 'stream_quality.py', 'frame_images.py', 'firmware_identity.py', 'testing_rv1126b_driver.py'):
        content = (BENCH / name).read_text(encoding='utf-8')
        if name in ('switch_runtime.py', 'stream_quality.py'):
            content = 'from bridge_support import checked\n' + content
            for helper in ('host-capture', 'uvc_host_stream-metrics'):
                content = content.replace("ROOT/'" + helper + "'", "checked('" + helper + "')")
        files[name] = content
    files['worker.py'] = "from datetime import datetime, timezone\nfrom pathlib import Path\nimport hashlib,json\ndef timestamp(): return datetime.now(timezone.utc).isoformat()\ndef digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()\ndef atomic(path,data):\n temp=path.with_suffix('.tmp');temp.write_text(json.dumps(data,ensure_ascii=False));temp.replace(path)\n"
    files['rv1126b.py'] = 'from bridge_support import discover\n'
    files['bridge_support.py'] = (BENCH / 'rmsl321_bridge_support.py').read_text()
    files['bridge.py'] = (BENCH / 'rmsl321_package_bridge.py').read_text()
    for code in codes:
        definition = json.loads((BENCH / 'test-cases' / code / '1.0.0/case.json').read_text())
        definition.pop('fixture_source', None)
        definitions[code] = definition
        entry = 'entries/' + code + '.py'
        files[entry] = "import sys\nfrom pathlib import Path\nsys.path.insert(0,str(Path(__file__).resolve().parents[1]))\nfrom bridge import main\nraise SystemExit(main(" + repr(code) + "))\n"
        for fixture in definition.get('files', []):
            name = f'test-cases/{code}/1.0.0/' + fixture['path']
            files[name] = (BENCH / name).read_bytes()
        scope = '；'.join(definition.get('assertions', []))
        if code == 'BASIC-FILE-ROUNDTRIP':
            scope += '；独立 /tmp 测试文件保留至重启，不自动删除'
        if code == 'FILE-PERSIST-REBOOT':
            scope += '；保留独立的 4 KB 日志目录测试文件'
        cases.append({'case_id': 'rmsl321.' + code.lower(), 'case_version': '2.0.0', 'name': definition['name'],
            'mode': 'automated', 'test_scope': scope or definition['name'], 'effects': definition['risk_level'],
            'timeout_seconds': 86400 if code == 'STREAM-RGB-DEPTH' else max(180, definition.get('timeout_seconds', 900)) + 120,
            'parameters_schema': schema_for(definition), 'parameter_ui': {key: {'label': LABELS.get(key, key)} for key in definition.get('parameters', {})},
            'requirements': {'capabilities': ['rmsl321.suite.v2', *definition.get('required_capabilities', [])]},
            'implementation': {'runtime_profile': 'python3-case', 'entrypoint': entry, 'input_schema': 'rk-case-input/1', 'result_schema': 'rk-case-result/1'},
            'required_assertions': ['device-identity', 'case-completed'], 'recovery': {'required': definition['risk_level'] != 'read_only'},
            'source_case': code, 'hardware_validation': '请以实际运行报告为准；上线不代表全部用例已经实机通过'})
    files['definitions.json'] = json.dumps(definitions, ensure_ascii=False, indent=2)
    manifest = {'package_schema': 'rk-case-package/1', 'package_id': 'rmsl321.maintained-suite', 'package_version': '2.0.0',
                'name': 'RMSL321 完整测试用例', 'cases': cases}
    files['manifest.json'] = json.dumps(manifest, ensure_ascii=False, indent=2)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return manifest


def manual_cases():
    spec = importlib.util.spec_from_file_location('definitions', ROOT / 'server/src/develop_hub/special_definitions.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    result = []
    for item in module.definitions():
        steps = []
        for step in item['steps']:
            instruction = {
                'inspect': '保持目标模组连接，使用测试工具读取 CPU 序列号、SN、固件版本和健康状态，保存原始输出及 USB 端口信息。',
                'serial_probe': '把串口适配器连接到测试节点或采集电脑，检查设备路径、权限和电平。此时不要启动老化，使用外部串口工具准备日志采集。',
                'aging_capture': '使用外部串口工具启动原始日志保存，确认采集已就绪后再给老化座上电。记录开始、结束时间并上传本轮完整日志；平台不会启动串口采集。',
                'aging_verify': '上传本轮完整串口日志，记录带时区的开始、结束时间。人工核对持续时间、温度峰值、错误或重启记录、最终老化结果，并与设置的阈值比较。',
                'ota_prepare': '由操作者校验源包摘要并创建独立测试副本，按本轮异常类型准备测试包。保存原包、测试包摘要、目标文件名及实际传输与触发命令。保留原包，此步不要启动 OTA。',
            }.get(step['id'], step['instruction'])
            instruction = instruction.replace('系统', '操作者').replace('服务器', '操作者').replace('Hub 判定时长', '本方案记录的判定时长').replace('确认后下一步启动采集', '确认后使用外部串口工具启动采集')
            # These are explicitly operator procedures, not silent calls to a retired scheduler.
            if step.get('operation'):
                instruction = '由操作者使用测试工具执行并保存原始证据。' + instruction
                instruction = instruction.replace('系统', '操作者').replace('服务器', '操作者').replace('点击开始后', '操作时')
            properties = {'note': {'type': 'string', 'title': '操作、测量结果与判定依据', 'minLength': 1}}
            required = ['note']
            for field in step.get('fields', []):
                name = field['name']; kind = field['type']
                if kind == 'file':
                    continue  # New platform uses step attachments rather than legacy artifact IDs.
                value = {'type': 'boolean' if kind == 'boolean' else 'number' if kind == 'number' else 'string', 'title': field['label']}
                if field.get('options'):
                    value['enum'] = [v['value'] for v in field['options']]
                for src, dst in [('default', 'default'), ('min', 'minimum'), ('max', 'maximum')]:
                    if src in field: value[dst] = field[src]
                if field.get('required'):
                    required.append(name)
                    if value['type'] == 'string': value['minLength'] = 1
                properties[name] = value
            steps.append({'id': step['id'], 'title': step['title'], 'instruction': instruction,
                'required_confirmations': [f['name'] for f in step.get('fields', []) if f.get('must_be_true')],
                'pass_criteria': '；'.join(step.get('records', [])) or '完成操作并记录可核对的原始证据',
                'record_schema': {'type': 'object', 'additionalProperties': False, 'properties': properties, 'required': required},
                'attachments_required': bool(step.get('operation'))})
        steps.append({'id': 'restore-confirmation', 'title': '确认设备恢复与最终状态',
            'instruction': '核对同一模组已重新连接、固件可运行，记录测试后保留的 SN、文件或其他状态；无法恢复时选择未通过。',
            'pass_criteria': '设备恢复可用，保留状态和原始证据均已记录。', 'attachments_required': True,
            'record_schema': {'type': 'object', 'additionalProperties': False, 'properties': {
                'note': {'type': 'string', 'title': '恢复证据与保留状态', 'minLength': 1},
                'recovery_confirmed': {'type': 'boolean', 'title': '已确认设备恢复可用'}}, 'required': ['note', 'recovery_confirmed']}})
        description = {
            'FACTORY-AGING': '使用外部串口工具准备采集，再接入老化座供电；上传本轮原始日志，人工核对时长、温度和最终老化结果。',
            'SPECIAL-OTA': '每轮选择损坏包、错误包名或升级中断中的一种；操作者准备独立测试包、执行注入、核对原始日志并确认设备恢复。',
        }.get(item['code'], item['description'])
        result.append({'case_id': 'rmsl321.' + item['code'].lower(), 'version': '2.0.1' if item['code'] in ('FACTORY-AGING', 'SPECIAL-OTA') else '2.0.0', 'name': item['name'] + '（人工执行）',
            'mode': 'manual', 'definition': {'test_scope': '人工执行与证据记录版；平台保存步骤、附件和人工判定，不自动执行或自动判读这些特殊操作。' + description,
                'effects': 'firmware_write' if item['code'] == 'SPECIAL-OTA' else 'state_change',
                'parameters_schema': {'type': 'object', 'additionalProperties': False, 'properties': {}},
                'operator_wait_timeout_seconds': 90000 if item['code'] == 'FACTORY-AGING' else 7200,
                'steps': steps, 'recovery': {'required': True, 'mode': 'operator_confirmation'}}})
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    manifest = build(args.output)
    args.output.with_suffix('.manual.json').write_text(json.dumps(manual_cases(), ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'automated_cases': len(manifest['cases']), 'manual_cases': 4, 'path': str(args.output)}))
