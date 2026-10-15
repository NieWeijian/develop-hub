"""Platform-installed test stations. Their execution configuration stays server-owned."""
from copy import deepcopy
import json
import os
from pathlib import Path
import re

from .service import fail, validate_node


def stations() -> dict:
    filename = os.environ.get('DEVELOP_HUB_TEST_STATIONS')
    if not filename:
        return {}
    try:
        data = json.loads(Path(filename).read_text(encoding='utf-8'))
        if data.get('schema') != 'rk-test-stations/1' or not isinstance(data.get('stations'), dict):
            raise ValueError('Invalid registry')
        for value in data['stations'].values():
            validate_node(value['node'])
            if not all(key in value for key in ('name', 'chip', 'config', 'capabilities')):
                raise ValueError('Incomplete station')
        return data['stations']
    except (OSError, ValueError, KeyError, TypeError):
        fail(503, 'stations_unavailable', '测试台部署信息不可用，请联系平台维护人员')


def catalog() -> list[dict]:
    # Never return commands, credentials, installation paths or tool digests here.
    return [{'id': key, 'name': value['name'], 'chip': value['chip'],
             'adapter_name': value.get('adapter_name', '平台内置适配器'),
             'functions': value.get('functions', []),
             'devices': value.get('devices', [])}
            for key, value in stations().items()]


def bind(station_id: str, serial: str, product) -> dict:
    from .safety import require_simulation
    require_simulation({'kind': 'ssh'})
    station = stations().get(station_id)
    if station is None:
        fail(422, 'station_unavailable', '请选择已部署的测试台')
    if product.chip.upper() != station['chip'].upper():
        fail(422, 'station_incompatible', '所选测试台不支持该产品的芯片型号')
    if not re.fullmatch(r'[0-9a-fA-F]{8,64}', serial) or not int(serial, 16):
        fail(422, 'invalid_serial', '请填写有效的设备 CPU 序列号')
    config = deepcopy(station['config'])
    config.update(serial=serial, managed_station_id=station_id,
                  station_name=station['name'], adapter_name=station.get('adapter_name', '平台内置适配器'),
                  functions=station.get('functions', []))
    return {'node': deepcopy(station['node']), 'config': config,
            'capabilities': deepcopy(station['capabilities'])}
