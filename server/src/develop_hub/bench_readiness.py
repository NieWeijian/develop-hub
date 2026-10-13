"""Read-only checks exposed to operators before a build is queued."""
import base64
import json
import shlex
from datetime import datetime, timezone
from .basic_cases import CODES, ROOT, definition, sha

BOARD = 'BoardConfig-SPI_NAND-RK805-RV1126B_EVB1_V10_V11-DualCamDis-v2.0-arm64.mk'

def artifact_patterns(board):
    return ['oem.img', 'system.img', 'boot.img'] if 'rv1126b' in board.lower() else ['update.img']

def expected_package():
    paths = {'firmware_identity.py','stream_quality.py','uvc_host_stream-metrics','factory_runtime.py','factory-codes.json','worker.py', 'rv1126b.py', 'test_runtime.py', 'tools.lock.json'}
    lock=json.loads((ROOT/'tools.lock.json').read_text())
    paths.update({lock['binary_path'],lock['manifest_path']})
    for code in CODES:
        folder = f'test-cases/{code}/1.0.0'
        paths.update({folder+'/run.py', folder+'/case.json'})
        paths.update(folder+'/'+f['path'] for f in definition(code).get('files', []))
    return {path: sha(ROOT/path) for path in sorted(paths)}

async def inspect_bench(workflow, bench, full=True):
    payload = {'root': bench.remote_root, 'files': expected_package() if full else {}, 'full': full}
    script = """
import hashlib, json, os, subprocess
from pathlib import Path
p = PAYLOAD
root = Path(p['root'])
checks = []
def check(name, passed, detail):
    checks.append({'name':name, 'passed':bool(passed), 'detail':detail})
check('交付目录', root.is_dir() and os.access(root, os.W_OK), str(root))
if p['full']:
    r = subprocess.run(['systemctl','is-active','develop-hub-qa-controller'],capture_output=True,text=True,timeout=10)
    check('Controller 服务', r.returncode == 0 and r.stdout.strip() == 'active', r.stdout.strip() or '未运行')
    bad = []
    for rel, expected in p['files'].items():
        path = root/rel
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            bad.append(rel)
    check('执行器与用例包', not bad, '与当前平台版本一致' if not bad else '缺失或版本不一致：'+', '.join(bad))
    try:
        r = subprocess.run(['sudo','-n','dfu-util','--version'], capture_output=True, text=True, timeout=10)
        check('DFU 工具权限', r.returncode == 0, '可执行' if r.returncode == 0 else 'dfu-util 或 sudo 权限未就绪')
    except OSError:
        check('DFU 工具权限', False, 'dfu-util 或 sudo 未安装')
print(json.dumps({'passed':all(c['passed'] for c in checks), 'checks':checks}))
""".replace('PAYLOAD', repr(payload))
    encoded = base64.b64encode(script.encode()).decode()
    command = 'python3 -c '+shlex.quote("import base64;exec(base64.b64decode("+repr(encoded)+"))")
    try:
        result = await workflow._remote(bench, command, 45)
        if result['exit_code']:
            raise ValueError('测试台检查执行失败，请检查 SSH、Python 和节点环境')
        report = json.loads(result['stdout_tail'])
        if not isinstance(report.get('checks'), list) or not report['checks']:
            raise ValueError('测试台检查未返回有效结果')
        report['passed'] = all(c.get('passed') is True for c in report['checks'])
    except Exception as exc:
        report = {'passed':False, 'checks':[{'name':'测试台连接与检查', 'passed':False, 'detail':str(exc)}]}
    report.update(bench_id=bench.id, checked_at=datetime.now(timezone.utc).isoformat())
    return report
