from copy import deepcopy
from uuid import uuid4
from fastapi.testclient import TestClient
from develop_hub.main import app
from develop_hub.db import SessionLocal
from develop_hub import models as m
from sqlalchemy import select,func
import pytest

created_rounds=[]

@pytest.fixture(autouse=True)
def cleanup_rounds():
    yield
    with SessionLocal() as db:
        for rid in created_rounds:
            row=db.get(m.TestRound,rid)
            if row: db.delete(row)
        db.commit()
    created_rounds.clear()

def create_round():
    uid=uuid4().hex
    with SessionLocal() as db:
        sdk=m.SdkRepository(name=uid,root_path='/tmp/'+uid,artifact_root='/tmp/'+uid+'/images',board_configs=['test'],artifact_patterns=['*.img'])
        repo=m.Repository(name=uid,root_path='/tmp/'+uid+'/app')
        gerrit=m.GerritRepository(name=uid,project_path='test/'+uid)
        bench=m.TestBench(name=uid,host='127.0.0.1',user='test',remote_root='/tmp/test')
        db.add_all([sdk,repo,gerrit,bench]);db.flush()
        product=m.TestProduct(code=uid,name=uid,sdk_repository_id=sdk.id,tested_repository_id=repo.id,gerrit_repository_id=gerrit.id,gerrit_project=gerrit.project_path,board_config='test')
        db.add(product);db.flush()
        point=m.TestPoint(product_id=product.id,change_number=1,patchset_number=1,commit_sha='a'*40,ref='refs/changes/01/1/1',subject='test',branch='main')
        db.add(point);db.flush()
        row=m.TestRound(display_number=(db.scalar(select(func.max(m.TestRound.display_number))) or 0)+1,
            product_id=product.id,test_point_id=point.id,bench_id=bench.id,status=m.TestRoundStatus.TEST_COMPLETE,
            product_snapshot={'execute_test_cases':True},manifest_commit='a'*40,board_config='test',gerrit_project=gerrit.project_path,
            change_number=1,patchset_number=1,commit_sha='a'*40,work_branch='test',
            test_case_snapshot=[{'id':'case1','code':'CASE1'},{'id':'case2','code':'CASE2'}],
            controller_result={'state':'finished','cases':[{'id':'case1','code':'CASE1','state':'finished','outcome':'passed'}]})
        db.add(row);db.commit();created_rounds.append(row.id);return row.id

def test_classification_persists_audit_restores_and_preserves_automatic_result():
    with TestClient(app) as client:
        rid=create_round();url=f'/api/qa/test-rounds/{rid}'
        before=client.get(url).json();auto=deepcopy(before['controller_result'])
        endpoint=url+'/cases/CASE1/classification'
        payload={'outcome':'failed','reason':'Image did not change','operator':'QA','revision':0}
        response=client.post(endpoint,json=payload)
        assert response.status_code==200,response.text
        value=response.json();assert value['manual'] and value['outcome']=='failed'
        assert value['automatic_outcome']=='passed'
        assert client.post(endpoint,json=payload).status_code==409
        after=client.get(url).json()
        assert after['controller_result']==auto and after['status']==before['status']
        assert after['case_classifications']['CASE1']['history'][0]['reason']==payload['reason']
        report=client.get(url+'/report').json()
        assert report['verdict']=='PASS' and report['case_classifications']['CASE1']['outcome']=='failed'
        assert report['case_classification_verdict']=='failed'
        payload.update(outcome=None,revision=value['revision'],reason='Return to automatic result')
        reset=client.post(endpoint,json=payload).json()
        assert reset['outcome']=='passed' and not reset['manual'] and len(reset['history'])==2
        assert client.post(url+'/cases/CASE2/classification',json={**payload,'revision':0}).status_code==409
        assert client.post(endpoint,json={**payload,'reason':'   '}).status_code==422
        with SessionLocal() as db:
            row=db.get(m.TestRound,rid);row.status=m.TestRoundStatus.BENCH_RUNNING;db.commit()
        assert client.post(endpoint,json={**payload,'revision':reset['revision']}).status_code==409
