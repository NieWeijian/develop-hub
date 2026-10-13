import asyncio
import json
from types import SimpleNamespace
from fastapi.testclient import TestClient
from develop_hub.main import app
from develop_hub.bench_readiness import BOARD, artifact_patterns, inspect_bench


def test_product_delivery_uses_same_image_contract_as_full_execution():
    assert artifact_patterns(BOARD) == ['oem.img','system.img','boot.img']
    assert artifact_patterns('rk3572_defconfig') == ['update.img']


def test_readiness_rejects_unreachable_or_inconsistent_node():
    bench=SimpleNamespace(id='test',remote_root='/srv/bench')
    class Workflow:
        async def _remote(self,*args):
            return {'exit_code':0,'stdout_tail':json.dumps({'passed':True,'checks':[{'name':'Controller','passed':False,'detail':'inactive'}]})}
    assert asyncio.run(inspect_bench(Workflow(),bench))['passed'] is False
    class Offline:
        async def _remote(self,*args):
            raise TimeoutError('offline')
    report=asyncio.run(inspect_bench(Offline(),bench))
    assert report['passed'] is False
    assert report['checks'][0]['detail']=='offline'


def test_shipped_case_install_is_idempotent_and_preserves_operator_group():
    with TestClient(app) as client:
        first=client.post('/api/qa/basic-case-definitions/install')
        assert first.status_code==200
        cases=client.get('/api/qa/test-cases').json()
        case=next(c for c in cases if c['code']=='BASIC-VERSION')
        assert client.patch('/api/qa/test-cases/'+case['id']+'/category',json={'category':'special'}).status_code==200
        second=client.post('/api/qa/basic-case-definitions/install')
        assert second.json()['added']==[]
        after=client.get('/api/qa/test-cases').json()
        assert len(after)==len(cases)
        assert next(c for c in after if c['id']==case['id'])['category']=='special'
