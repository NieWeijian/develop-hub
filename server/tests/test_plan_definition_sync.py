"""Explicit package refresh preserves requirements and audit history."""
from copy import deepcopy

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_testing_platform import context
from test_release_special import release, launch
from develop_hub import testing_platform as p
from develop_hub.db import get_db
from develop_hub.models import TestRound as Round, TestRoundStatus as Status, ValidationEvidence


def request(revision=1):
    return p.SyncSpecial(revision=revision,operator='tester',reason='同步运行包与已接入用例定义')


def outdated(env):
    original=deepcopy(env.plan.requirements)
    targets=[next(r for r in original if r['mode']==mode) for mode in ('automatic','gated')]
    codes={r['code'] for r in targets}
    reqs=deepcopy(original)
    for req in reqs:
        if req['code'] not in codes:continue
        req['adapter']={'id':req['code'],'old':'frozen-tools'}
        req['name']='旧名称';req['case_id']='old-case'
        req.update(parameters={'existing':17},required=False,environment='保留台架',procedure='保留指令')
    env.plan.requirements=reqs;env.db.commit()
    return original,deepcopy(reqs),codes


def test_audited_refresh_updates_automatic_and_gated_without_erasing_history(release):
    original,before,codes=outdated(release)
    req=next(r for r in before if r['code'] in codes)
    history=ValidationEvidence(plan_id=release.plan.id,requirement_id=req['id'],requirement_hash=p.spec_hash(req),
        payload={'kind':'manual-fixture','outcome':'failed','reason':'original failure evidence'})
    release.db.add(history);release.db.commit();historical_id=history.id
    app=FastAPI();app.include_router(p.router);app.dependency_overrides[get_db]=lambda:release.db
    with TestClient(app) as client:
        response=client.post('/api/qa/platform/plans/'+release.plan.id+'/sync-definitions',json=request().model_dump())
    assert response.status_code==200,response.text
    assert response.json()=={'revision':2,'updated':[r['code'] for r in before if r['code'] in codes]}
    after=release.plan.requirements
    assert [r['id'] for r in after]==[r['id'] for r in before]
    for previous,current,fresh in zip(before,after,original):
        if previous['code'] in codes:
            for key in ('adapter','name','case_id','mode'):assert current[key]==fresh[key]
            for key in ('parameters','required','environment','procedure'):assert current[key]==previous[key]
        else:assert current==previous
    retained=release.db.get(ValidationEvidence,historical_id)
    assert retained.payload['reason']=='original failure evidence'
    assert retained.requirement_hash==p.spec_hash(req)
    audit=list(release.db.scalars(select(ValidationEvidence).where(ValidationEvidence.plan_id==release.plan.id)))
    assert len(audit)==3
    refreshed=[entry for entry in audit if entry.id!=historical_id]
    assert all(entry.payload['kind']=='requirement_revision' and entry.payload['sync_scope']=='all_implemented' for entry in refreshed)
    assert all(entry.payload['operator']=='tester' and entry.payload['before']!=entry.payload['after'] for entry in refreshed)
    assert p.sync_definitions(release.plan.id,request(2),release.db)=={'revision':2,'updated':[]}
    assert len(list(release.db.scalars(select(ValidationEvidence))))==3


def test_sync_special_keeps_automatic_definition_unchanged(release):
    _,before,codes=outdated(release)
    result=p.sync_special(release.plan.id,request(),release.db)
    expected=[r['code'] for r in before if r['mode']=='gated' and r['code'] in codes]
    assert result=={'revision':2,'updated':expected}
    old_auto=next(r for r in before if r['mode']=='automatic' and r['code'] in codes)
    assert next(r for r in release.plan.requirements if r['id']==old_auto['id'])==old_auto


@pytest.mark.parametrize('state',['created',*p.ACTIVE])
def test_sync_refuses_pending_and_active_plan_rounds(release,state):
    _,before,_=outdated(release)
    row=release.db.get(Round,release.candidate.source_round_id)
    row.validation_plan_id=release.plan.id;row.status=Status(state);release.db.commit()
    with pytest.raises(HTTPException) as exc:
        p.sync_definitions(release.plan.id,request(),release.db)
    assert exc.value.status_code==409
    assert release.plan.revision==1 and release.plan.requirements==before
    assert not list(release.db.scalars(select(ValidationEvidence)))


def test_sync_refuses_reserved_manual_gate(release):
    run,_=launch(release)
    prep=release.db.get(Round,run.context['release_binding']['preparation_round_id'])
    prep.status=Status.TEST_COMPLETE;release.db.commit()
    with pytest.raises(HTTPException) as exc:
        p.sync_definitions(release.plan.id,request(),release.db)
    assert exc.value.status_code==409 and '人工门控' in exc.value.detail
    assert release.plan.revision==1 and run.bench_reserved


def test_sync_rejects_stale_revision_and_frozen_report(release):
    _,before,_=outdated(release)
    with pytest.raises(HTTPException):p.sync_definitions(release.plan.id,request(99),release.db)
    release.plan.final_report={'frozen':True};release.db.commit()
    with pytest.raises(HTTPException):p.sync_definitions(release.plan.id,request(),release.db)
    assert release.plan.revision==1 and release.plan.requirements==before
