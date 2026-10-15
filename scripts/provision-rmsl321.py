#!/usr/bin/env python3
"""Install the platform adapter on an SSH test station; only read the attached device.

Run on the Linux controller. The registry is private deployment configuration,
not a user-authored device form. No firmware is flashed by this installer.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--user', required=True)
    parser.add_argument('--key', required=True)
    parser.add_argument('--port', type=int, default=22)
    parser.add_argument('--root', required=True, help='Dedicated platform directory on the station')
    parser.add_argument('--ctl-source', required=True, help='Trusted installed UVC control binary on the station')
    parser.add_argument('--dfu-source', default='/usr/bin/dfu-util')
    parser.add_argument('--dfu-sudo', action='store_true')
    parser.add_argument('--registry', required=True)
    parser.add_argument('--station-id', default='rmsl321')
    parser.add_argument('--name', default='RMSL321 测试台')
    parser.add_argument('--suite', action='store_true', help='Install the maintained automated case helpers')
    args = parser.parse_args()
    if not Path(args.root).is_absolute() or args.root in ('/', '/home', '/usr'):
        parser.error('Use a dedicated absolute station directory')
    destination = f'{args.user}@{args.host}'
    options = ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-i', args.key]
    ssh = ['ssh', *options, '-p', str(args.port), destination]

    def remote(source):
        result = subprocess.run([*ssh, 'python3 -'], input=source, capture_output=True, text=True, check=True, timeout=120)
        return json.loads(result.stdout)

    driver = Path(__file__).resolve().parents[1] / 'bench/testing_rv1126b_driver.py'
    content = driver.read_bytes()
    version = hashlib.sha256(content).hexdigest()[:16]
    directory = args.root.rstrip('/') + '/adapters/rmsl321-' + version
    # The helper is passed on stdin; paths are data, never interpolated shell commands.
    config = remote('import json, hashlib, shutil\nfrom pathlib import Path\n' +
        'directory, ctl, dfu = ' + repr((directory, args.ctl_source, args.dfu_source)) + '\n' + '''
root = Path(directory)
root.mkdir(parents=True, exist_ok=True)
result = {}
for key, source, name in [('ctl', ctl, 'uvc_host_ctl'), ('dfu', dfu, 'dfu-util')]:
    src = Path(source).resolve(strict=True)
    target = root / name
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        raise RuntimeError('Installed tool differs; use a new dedicated root for a tool upgrade')
    if not target.exists():
        shutil.copy2(src, target)
    target.chmod(0o755)
    result[key] = {'path': str(target), 'sha256': digest}
print(json.dumps(result))
''')
    remote_driver = directory + '/testing_rv1126b_driver.py'
    # Write exact bytes, verify the installed driver, and inspect all connected RMSL321 modules.
    observed = remote('import hashlib, importlib.util, json\nfrom pathlib import Path\n' +
        'path, content, config = ' + repr((remote_driver, content.hex(), config)) + '\n' + '''
path = Path(path)
data = bytes.fromhex(content)
if path.exists() and path.read_bytes() != data:
    raise RuntimeError('Installed adapter content differs')
if not path.exists():
    with path.open('xb') as stream:
        stream.write(data)
assert hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(data).digest()
spec = importlib.util.spec_from_file_location('adapter', path)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)
tool = adapter.validate_tool(config)
devices = []
for node in sorted(Path('/sys/class/video4linux').glob('video*')):
    if 'RMSL321_RGB' in (node / 'name').read_text() and (node / 'index').read_text().strip() == '0':
        serial = adapter.call(tool, '/dev/' + node.name, 'get-cpu-serial')
        observed = adapter.inspect_device({**config, 'serial': serial})
        if serial not in [item['serial'] for item in devices]:
            devices.append(observed)
print(json.dumps({'devices': devices, 'board': adapter.BOARD}))
''')
    if not observed['devices']:
        raise RuntimeError('Adapter installed, but no RMSL321 device was found; registry not changed')
    config['dfu']['sudo'] = args.dfu_sudo
    if args.dfu_sudo:
        subprocess.run([*ssh, shlex.join(['sudo', '-n', config['dfu']['path'], '--version'])], check=True, capture_output=True, timeout=30)
    config.update(board_config=observed['board'], adapter={
        'kind': 'command',
        'inspect_argv': ['python3', remote_driver, 'inspect', '--input', '{input}'],
        'deploy_argv': ['python3', remote_driver, 'deploy', '--input', '{input}', '--output-dir', '{output}'],
        'deploy_timeout_seconds': 1200,
    })
    capabilities = ['uvc.control.version.v1']
    if args.suite:
        bench = driver.parent
        lock = json.loads((bench / 'tools.lock.json').read_text())
        if config['ctl']['sha256'] != lock['binary_sha256']:
            raise RuntimeError('Suite capability manifest does not match the installed UVC binary')
        helpers = {name: (bench / name).read_bytes().hex() for name in ('host-capture', 'uvc_host_stream-metrics')}
        config['helpers'] = remote('import json, hashlib, platform, shutil\nfrom pathlib import Path\n' +
            'directory, files = ' + repr((directory, helpers)) + '\n' + '''
if platform.machine() not in ('aarch64', 'arm64') or not shutil.which('v4l2-ctl'):
    raise RuntimeError('Suite requires an ARM64 Linux station with v4l2-ctl')
result = {}
for name, content in files.items():
    data = bytes.fromhex(content)
    if data[:4] != b'\\x7fELF' or int.from_bytes(data[18:20], 'little') != 183:
        raise RuntimeError('Helper must be an ARM64 ELF executable')
    digest = hashlib.sha256(data).hexdigest()
    path = Path(directory) / (name + '-' + digest[:16])
    if path.exists() and path.read_bytes() != data:
        raise RuntimeError('Installed helper content differs')
    if not path.exists():
        with path.open('xb') as stream: stream.write(data)
    path.chmod(0o755)
    result[name] = {'path': str(path), 'sha256': digest}
print(json.dumps(result))
''')
        config['tool_capabilities'] = lock['capabilities']
        capabilities += ['rmsl321.suite.v2', *lock['capabilities']]
    registry = Path(args.registry)
    document = json.loads(registry.read_text()) if registry.exists() else {'schema': 'rk-test-stations/1', 'stations': {}}
    if document.get('schema') != 'rk-test-stations/1':
        raise ValueError('Unsupported registry schema')
    document['stations'][args.station_id] = {
        'name': args.name, 'chip': 'RV1126B', 'adapter_name': 'RMSL321 内置适配器',
        'functions': ['设备身份检查', '固件版本读取', '镜像烧录（工具已安装，待实机验证）'],
        'node': {'kind': 'ssh', 'host': args.host, 'user': args.user, 'port': args.port,
                 'key': str(Path(args.key).resolve()), 'root': args.root + '/runs', 'python': 'python3'},
        'config': config, 'capabilities': list(dict.fromkeys(capabilities)),
        'devices': [{'serial': item['serial'], 'firmware_version': item['firmware_version']} for item in observed['devices']],
    }
    registry.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=registry.parent, prefix='.stations-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, registry)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print(json.dumps({'station': args.station_id, 'devices': document['stations'][args.station_id]['devices'],
                      'inspection': 'passed', 'flashed': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()
