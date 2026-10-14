"""Single-control-instance coordinator for immutable test requests."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from ..db import SessionLocal
from .models import Device, InputAsset, OperatorRecord, TestRun
from .execution import execute_step, validate_case_result, checked_child, digest

ACTIVE = {'preparing', 'running', 'awaiting_operator'}
TERMINAL = {'completed', 'cancelled', 'interrupted'}


def now():
    return datetime.now(timezone.utc)


def resource_key(device):
    node = device.get('node', {})
    if node.get('kind') == 'ssh':
        return ('ssh', node.get('host', '').lower(), int(node.get('port', 22)))
    return (node.get('kind', 'local'),)


class CancelRequested(Exception):
    pass


class TestingRunner:
    def __init__(self, settings):
        self.settings = settings
        self.root = settings.artifact_root / 'testing'
        self.root.mkdir(parents=True, exist_ok=True)
        self.instance_lock = None
        self.tasks: dict[str, asyncio.Task] = {}
        self.claim_lock = asyncio.Lock()
        self.environment_locks: dict[str, asyncio.Lock] = {}

    async def start(self, run_id: str):
        if run_id in self.tasks and not self.tasks[run_id].done():
            return
        task = asyncio.create_task(self._execute(run_id), name='testing-' + run_id)
        self.tasks[run_id] = task
        task.add_done_callback(lambda done: self.tasks.pop(run_id, None) if self.tasks.get(run_id) is done else None)

    async def resume(self, run_id: str):
        await self.start(run_id)

    async def check_device(self, identity, operator, note, recovery_confirmed):
        from .service import view, require, fail
        async with self.claim_lock:
            with SessionLocal() as db:
                row = require(db, Device, identity)
                device = view(row)
                active = list(db.scalars(select(TestRun).where(TestRun.state.in_(ACTIVE))))
                if any((r.result or {}).get('device_claimed') and resource_key(r.request_snapshot.get('device', {})) == resource_key(device) for r in active):
                    fail(409, 'device_busy', '执行节点正在被测试占用，请等待运行结束')
                if row.health == 'needs_attention' and (not recovery_confirmed or not note.strip()):
                    fail(422, 'recovery_confirmation_required', '请先核对节点上没有残留操作，并填写设备恢复说明')
            try:
                if device['node']['kind'] == 'simulation':
                    observed = {'serial': device['config'].get('serial'), 'simulation': True}
                else:
                    token = str(uuid4())
                    result = await execute_step(device['node'], self.root / 'device-checks' / token / 'inspect', {
                        'action': 'inspect', 'argv': device['config'].get('adapter', {}).get('inspect_argv'),
                        'input': {'run_id': token, 'device': device}, 'timeout_seconds': 60})
                    observed = result.get('data', {})
                    if result.get('exit_code') or not device['config'].get('serial') or observed.get('serial') != device['config']['serial']:
                        raise ValueError('设备身份读取未通过')
            except Exception as exc:
                with SessionLocal() as db:
                    row = db.get(Device, identity)
                    row.health = 'needs_attention'
                    db.commit()
                fail(422, 'device_check_failed', str(exc))
            with SessionLocal() as db:
                row = db.get(Device, identity)
                row.health = 'ready'
                row.config = {**row.config, 'last_check': {'at': now().isoformat(), 'operator': operator,
                    'note': note, 'recovery_confirmed': recovery_confirmed, 'observation': observed}}
                db.commit()
                return view(row)

    async def cancel(self, run_id: str):
        with SessionLocal() as db:
            run = db.get(TestRun, run_id)
            if run and run.state not in TERMINAL:
                run.cancel_requested = True
                db.commit()
        # Active commands finish their controlled stage before cancellation.
        await self.start(run_id)

    async def recover(self):
        import fcntl
        self.instance_lock = (self.root / 'control.lock').open('a')
        try:
            fcntl.flock(self.instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.instance_lock.close()
            self.instance_lock = None
            raise RuntimeError('同一产物目录只允许启动一个测试控制实例')
        with SessionLocal() as db:
            for run in db.scalars(select(TestRun).where(TestRun.state.in_(ACTIVE | {'queued'}))):
                if run.state == 'awaiting_operator':
                    continue
                if run.state == 'queued':
                    continue
                touched = bool((run.result or {}).get('device_claimed'))
                run.state, run.verdict, run.completed_at = 'interrupted', 'error', now()
                run.result = {**(run.result or {}), 'message': '控制端重启，未重复下发设备操作；请检查执行记录和设备状态'}
                if touched:
                    run.recovery = 'unknown'
                    device = db.get(Device, run.request_snapshot['device']['id'])
                    if device:
                        device.health = 'needs_attention'
            db.commit()
            queued = list(db.scalars(select(TestRun.id).where(TestRun.state.in_({'queued', 'awaiting_operator'}))))
        for run_id in queued:
            await self.start(run_id)

    async def stop(self):
        # Transport cancellation is never interpreted as successful recovery.
        for task in list(self.tasks.values()):
            task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
        if self.instance_lock:
            self.instance_lock.close()
            self.instance_lock = None

    def _update(self, run_id, *, message=None, result=None, **values):
        with SessionLocal() as db:
            run = db.get(TestRun, run_id)
            if not run:
                return
            for key, value in values.items():
                setattr(run, key, value)
            if result is not None:
                run.result = deepcopy(result)
            if message:
                run.events = [*(run.events or []), {'at': now().isoformat(), 'phase': run.phase, 'message': message}]
                run.result = {**(run.result or {}), 'message': message}
            db.commit()

    def _archive_evidence(self, run_id):
        # Only declared step outputs and build logs, never the captured SDK tree.
        folder = self.root / 'runs' / run_id
        with SessionLocal() as db:
            run = db.get(TestRun, run_id)
            if not run:
                return
            result = deepcopy(run.result or {})
            records = result.get('evidence', [])
            seen = {r['path'] for r in records}
            candidates = []
            for step in folder.glob('*/node-result.json'):
                try:
                    node = json.loads(step.read_text())
                    if node.get('run_id') == run_id:
                        for item in node.get('evidence', []):
                            path = checked_child(step.parent, item['path'])
                            candidates.append((path, step.parent.name + ' / ' + item['path'], item['sha256']))
                except (ValueError, OSError, KeyError):
                    continue
            for key in ('stdout_path', 'stderr_path'):
                value = result.get('build', {}).get(key)
                if value:
                    candidates.append((Path(value), '构建 / ' + Path(value).name, None))
            for path, name, expected in candidates:
                try:
                    path = path.resolve()
                    if folder.resolve() not in path.parents or str(path) in seen or not path.is_file():
                        continue
                    actual = digest(path)
                    if expected and actual != expected:
                        continue
                    records.append({'evidence_id': str(uuid4()), 'name': name, 'path': str(path), 'size_bytes': path.stat().st_size, 'sha256': actual})
                    seen.add(str(path))
                except OSError:
                    continue
            run.result = {**result, 'evidence': records}
            db.commit()

    def _check_cancel(self, run_id):
        with SessionLocal() as db:
            if db.get(TestRun, run_id).cancel_requested:
                raise CancelRequested()

    def _asset(self, identity, kind):
        with SessionLocal() as db:
            asset = db.get(InputAsset, identity)
            if not asset or asset.kind != kind:
                raise ValueError('固定输入不存在或类型不匹配')
            return deepcopy(asset.manifest)

    def _save_asset(self, kind, name, manifest):
        with SessionLocal() as db:
            asset = InputAsset(kind=kind, name=name, digest=manifest['digest'], manifest=manifest)
            db.add(asset)
            db.commit()
            return asset.id

    async def _claim(self, run_id, device):
        while True:
            self._check_cancel(run_id)
            async with self.claim_lock:
                with SessionLocal() as db:
                    current = db.get(Device, device['id'])
                    if not current or current.health == 'needs_attention' or not current.enabled:
                        raise ValueError('设备需要检查或已停用')
                    active = list(db.scalars(select(TestRun).where(TestRun.state.in_(ACTIVE), TestRun.id != run_id)))
                    node_key = resource_key(device)
                    busy = any((r.result or {}).get('device_claimed') and (r.request_snapshot.get('device', {}).get('id') == device['id'] or resource_key(r.request_snapshot.get('device', {})) == node_key) for r in active)
                    if not busy:
                        current.health = 'busy'
                        run = db.get(TestRun, run_id)
                        run.result = {**(run.result or {}), 'device_claimed': True}
                        db.commit()
                        return
            self._update(run_id, phase='waiting_device')
            await asyncio.sleep(1)

    def _release(self, run_id, recovery):
        with SessionLocal() as db:
            run = db.get(TestRun, run_id)
            if not run or not (run.result or {}).get('device_claimed'):
                return
            device = db.get(Device, run.request_snapshot['device']['id'])
            if device:
                device.health = 'needs_attention' if recovery in {'failed', 'unknown'} else 'ready'
            run.result = {**run.result, 'device_claimed': False}
            db.commit()

    async def _prepare(self, run_id, snapshot, folder):
        from .sources import capture_source, build_snapshot
        firmware = snapshot['plan']['firmware']
        resolved = deepcopy(snapshot)
        if firmware['kind'] == 'artifact':
            resolved['firmware_bundle'] = self._asset(firmware['bundle_id'], 'firmware_bundle')
        if firmware['kind'] == 'build':
            environment = snapshot['environment']
            if environment['node'].get('kind', 'local') != 'local':
                raise ValueError('构建环境应登记在控制端；远端节点暂不支持 SDK 构建')
            source = firmware.get('source', {})
            if source.get('kind') == 'snapshot':
                captured = self._asset(source['snapshot_id'], 'source_snapshot')
            else:
                self._update(run_id, phase='source_capture', message='采集 SDK 实际内容，保留本地未提交修改')
                captured = await asyncio.to_thread(capture_source, environment['config']['sdk_path'], folder / 'source', environment['config'].get('input_rules'), source.get('repository_overrides', []))
                resolved['source_snapshot_id'] = self._save_asset('source_snapshot', '本次源码快照', captured)
            resolved['source_snapshot'] = captured
            # Source and case inputs are sealed before starting the build.
            self._update(run_id, resolved_input=resolved, phase='building', message='在独立 SDK 副本中构建')
            self._check_cancel(run_id)
            built = await asyncio.to_thread(build_snapshot, captured, environment['config']['recipe'], folder / 'build')
            with SessionLocal() as db:
                run = db.get(TestRun, run_id)
                run.result = {**run.result, 'build': built['build']}
                db.commit()
            self._check_cancel(run_id)
            if built['build']['exit_code'] != 0:
                raise RuntimeError('固件构建失败，请查看构建日志')
            bundle = built['firmware_bundle']
            bundle.update(product_id=snapshot['product']['id'], chip=snapshot['product'].get('chip'), board_config=snapshot['product'].get('board'))
            bundle_id = self._save_asset('firmware_bundle', '构建产物', bundle)
            with SessionLocal() as db:
                run = db.get(TestRun, run_id)
                run.result = {**run.result, 'build': built['build'], 'firmware_bundle': bundle, 'firmware_bundle_id': bundle_id}
                db.commit()
            # Keep the sealed input unchanged; output is a distinct asset.
            return resolved, bundle
        self._update(run_id, resolved_input=resolved)
        return resolved, resolved.get('firmware_bundle')

    async def _manual(self, run_id, case, result):
        assertions = []
        requires_recovery = case['definition'].get('recovery', {}).get('required', False)
        for step in case['definition'].get('steps', []):
            if requires_recovery and any(not a['passed'] for a in assertions) and step['id'] != 'restore-confirmation':
                continue  # Stop further risky operations; still offer the recovery gate.
            step_id = case['binding_id'] + ':' + step['id']
            with SessionLocal() as db:
                record = db.scalar(select(OperatorRecord).where(OperatorRecord.run_id == run_id, OperatorRecord.step_id == step_id))
                if record:
                    assertions.append({'id': step['id'], 'name': step['title'], 'expected': step.get('instruction', '符合人工检查标准'), 'actual': record.values, 'passed': record.decision == 'passed' and all(record.values.get(key) is True for key in step.get('required_confirmations', [])), 'operator': record.operator, 'at': record.created_at.isoformat()})
                    continue
            prior = result.get('awaiting_operator', {})
            started = prior.get('started_at') if prior.get('step_id') == step_id else now().isoformat()
            result['awaiting_operator'] = {**step, 'step_id': step_id, 'case_name': case['name'], 'binding_id': case['binding_id'], 'started_at': started}
            self._update(run_id, state='awaiting_operator', phase='operator', result=result, message='等待人工确认：' + step['title'])
            with SessionLocal() as db:
                limit = case['definition'].get('operator_wait_timeout_seconds', db.get(TestRun, run_id).request_snapshot['plan'].get('policy', {}).get('manual_wait_timeout_seconds', 3600))
            while True:
                self._check_cancel(run_id)
                with SessionLocal() as db:
                    record = db.scalar(select(OperatorRecord).where(OperatorRecord.run_id == run_id, OperatorRecord.step_id == step_id))
                    latest = deepcopy(db.get(TestRun, run_id).result or {})
                    if record:
                        result.update(latest)
                        assertions.append({'id': step['id'], 'name': step['title'], 'expected': step.get('instruction', '符合人工检查标准'), 'actual': record.values, 'passed': record.decision == 'passed' and all(record.values.get(key) is True for key in step.get('required_confirmations', [])), 'operator': record.operator, 'at': record.created_at.isoformat()})
                        break
                if (now() - datetime.fromisoformat(started)).total_seconds() > limit:
                    raise RuntimeError('人工步骤等待超时')
                await asyncio.sleep(0.25)
        result.pop('awaiting_operator', None)
        recovery = {'state': 'not_required'}
        if requires_recovery:
            restored = assertions and assertions[-1]['id'] == 'restore-confirmation' and assertions[-1]['passed'] and assertions[-1]['actual'].get('recovery_confirmed') is True
            recovery = {'state': 'restored' if restored else 'failed', 'source': 'operator_confirmation'}
        return {'binding_id': case['binding_id'], 'case_revision_id': case['case_revision_id'], 'name': case['name'], 'required': case['required'], 'state': 'completed', 'verdict': 'error' if recovery['state'] == 'failed' else 'passed' if assertions and all(a['passed'] for a in assertions) else 'failed', 'assertions': assertions, 'recovery': recovery}

    async def _execute(self, run_id):
        recovery = 'not_required'
        claimed = False
        result = {}
        try:
            with SessionLocal() as db:
                run = db.get(TestRun, run_id)
                if not run or run.state in TERMINAL:
                    return
                snapshot = deepcopy(run.request_snapshot)
                resolved = deepcopy(run.resolved_input)
                result = deepcopy(run.result or {})
                recovery = run.recovery
            self._check_cancel(run_id)
            self._update(run_id, state='preparing', message='正在准备固定输入')
            folder = self.root / 'runs' / run_id
            folder.mkdir(parents=True, exist_ok=True)
            if resolved is None:
                environment_id = (snapshot.get('environment') or {}).get('id')
                lock = self.environment_locks.setdefault(environment_id or 'imports', asyncio.Lock())
                async with lock:
                    self._check_cancel(run_id)
                    resolved, bundle = await self._prepare(run_id, snapshot, folder)
            else:
                bundle = result.get('firmware_bundle') or resolved.get('firmware_bundle')
            with SessionLocal() as db:
                result = deepcopy(db.get(TestRun, run_id).result or {})
            firmware = snapshot['plan']['firmware']
            if firmware.get('build_only'):
                self._archive_evidence(run_id)
                self._update(run_id, state='completed', phase='report', verdict='not_tested', completed_at=now(), message='固件产物已准备，本次未执行测试')
                return
            device = snapshot['device']
            simulated = device['node'].get('kind') == 'simulation'
            result['simulation'] = simulated
            if not result.get('device_claimed'):
                await self._claim(run_id, device)
                result['device_claimed'] = True
            claimed = True
            self._check_cancel(run_id)
            self._update(run_id, state='running', phase='device_check', result=result)
            adapter = device.get('config', {}).get('adapter', {})
            if not result.get('device_observation'):
                if simulated:
                    observation = {'serial': device['config'].get('serial', 'SIMULATED'), 'firmware_version': '示例固件 1.0', 'simulation': True}
                else:
                    step = await execute_step(device['node'], folder / 'inspect', {'action': 'inspect', 'argv': adapter.get('inspect_argv'), 'input': {'run_id': run_id, 'device': device}, 'timeout_seconds': 60})
                    observation = step.get('data', {})
                    if step.get('exit_code') or not device['config'].get('serial') or observation.get('serial') != device['config']['serial']:
                        raise ValueError('无法核实目标设备身份，测试已停止')
                result['device_observation'] = observation
                self._update(run_id, result=result, message='已读取设备身份与固件信息')
            if firmware['kind'] != 'current_device' and not result.get('deployment'):
                self._check_cancel(run_id)
                if not simulated:
                    from .deployment import metadata_error
                    problem = metadata_error(device, bundle, firmware.get('expected_version') or bundle.get('expected_version'))
                    if problem:
                        raise ValueError(problem)
                self._update(run_id, phase='deploying', recovery='pending', message='正在部署已固定的固件，取消请求将在本阶段结束后处理')
                recovery = 'unknown'
                if simulated:
                    deployment = {'boot_verified': True, 'simulation': True, 'firmware_digest': bundle['digest']}
                else:
                    deployment_bundle = {**bundle, 'expected_version': firmware.get('expected_version') or bundle.get('expected_version')}
                    deployed = await execute_step(device['node'], folder / 'deploy', {'action': 'deploy', 'argv': adapter.get('deploy_argv'), 'input': {'run_id': run_id, 'device': device}, 'timeout_seconds': adapter.get('deploy_timeout_seconds', 1200)}, firmware=deployment_bundle)
                    deployment = deployed.get('data', {})
                    if deployed.get('exit_code') or deployment.get('deployment_schema') != 'rk-deployment-result/1' or deployment.get('run_id') != run_id or deployment.get('serial') != device['config']['serial'] or deployment.get('firmware_digest') != bundle['digest'] or deployment.get('boot_verified') is not True:
                        raise ValueError('烧录或启动身份没有得到确认')
                recovery = 'restored'
                result['deployment'] = deployment
                if deployment.get('observation'):
                    result['device_observation'] = deployment['observation']
                self._update(run_id, result=result, recovery=recovery, message='部署与启动确认完成')
            self._check_cancel(run_id)
            done = {c['binding_id']: c for c in result.get('cases', [])}
            for index, case in enumerate(snapshot['cases']):
                if case['binding_id'] in done:
                    continue
                self._check_cancel(run_id)
                result['current_case'] = case['binding_id']
                self._update(run_id, state='running', phase='cases', result=result, message='正在执行：' + case['name'])
                if case['mode'] == 'manual':
                    if case['definition'].get('recovery', {}).get('required'):
                        recovery = 'unknown'
                        self._update(run_id, recovery=recovery)
                    data = await self._manual(run_id, case, result)
                    if data is None:
                        return  # The device reservation remains held in the database.
                elif case['definition'].get('implementation', {}).get('runtime_profile') == 'simulation':
                    if not simulated:
                        raise ValueError('模拟用例不能在真实设备上形成测试结论')
                    await asyncio.sleep(0.2)
                    limit = case['parameters'].get('limit_c', 75)
                    observed = 42
                    data = {'state': 'completed', 'verdict': 'passed' if observed <= limit else 'failed', 'assertions': [{'id': 'demo-measurement', 'name': '模拟采样值', 'expected': limit, 'actual': observed, 'unit': '°C', 'passed': observed <= limit}], 'recovery': {'state': 'not_required'}, 'simulation': True}
                else:
                    implementation = case['definition']['implementation']
                    package = self._asset(implementation['package_asset_id'], 'case_package')
                    if not package.get('trusted'):
                        raise ValueError('用例包尚未获准执行')
                    if case['definition'].get('effects', 'read_only') != 'read_only':
                        recovery = 'unknown'
                        self._update(run_id, recovery=recovery)
                    step = await execute_step({**device['node'], 'kind': 'local'} if simulated else device['node'], folder / f'case-{index:03d}', {'action': 'case', 'entrypoint': 'package/' + implementation['entrypoint'], 'cwd': 'package', 'input': {'input_schema': 'rk-case-input/1', 'run_id': run_id, 'binding_id': case['binding_id'], 'case_revision_id': case['case_revision_id'], 'parameters': case['parameters'], 'device': {**device, 'observation': result['device_observation']}, 'firmware': bundle or result['device_observation']}, 'timeout_seconds': case['definition'].get('timeout_seconds', 120)}, package=package)
                    data = validate_case_result(step, case, run_id)
                    evidence = result.get('evidence', [])
                    for item in step.get('evidence', []):
                        evidence.append({**item, 'evidence_id': str(uuid4()), 'name': case['name'] + ' / ' + item['path'], 'path': str(folder / f'case-{index:03d}' / item['path'])})
                    result['evidence'] = evidence
                data.update(binding_id=case['binding_id'], case_revision_id=case['case_revision_id'], name=case['name'], required=case['required'])
                if data.get('recovery', {}).get('state') == 'failed':
                    recovery = 'failed'
                elif data.get('recovery', {}).get('state') == 'restored':
                    recovery = 'restored'
                done[case['binding_id']] = data
                result['cases'] = list(done.values())
                self._update(run_id, result=result, recovery=recovery)
                result.pop('current_case', None)
                if recovery == 'failed':
                    raise RuntimeError('设备恢复失败，后续测试停止')
                if data['verdict'] != 'passed' and snapshot['plan'].get('policy', {}).get('independent_case_failure') == 'stop':
                    break
            expected = [c for c in snapshot['cases'] if c['required']]
            outcomes = [done.get(c['binding_id'], {}).get('verdict', 'blocked') for c in expected]
            verdict = 'error' if 'error' in outcomes else 'failed' if 'failed' in outcomes else 'blocked' if 'blocked' in outcomes else 'passed' if outcomes and all(v == 'passed' for v in outcomes) else 'inconclusive'
            result['cases'] += [{'binding_id': c['binding_id'], 'name': c['name'], 'required': c['required'], 'state': 'not_run', 'verdict': 'blocked', 'reason': '前序用例停止了执行'} for c in snapshot['cases'] if c['binding_id'] not in done]
            self._check_cancel(run_id)
            self._update(run_id, state='completed', phase='report', verdict=verdict, recovery=recovery, result=result, completed_at=now(), message=('模拟测试完成' if simulated else '测试完成'))
            self._archive_evidence(run_id)
            self._release(run_id, recovery)
        except CancelRequested:
            self._update(run_id, state='cancelled', verdict='not_tested', completed_at=now(), recovery=recovery, message='已停止后续执行；已完成的结果保留')
            self._archive_evidence(run_id)
            self._release(run_id, recovery)
        except asyncio.CancelledError:
            with SessionLocal() as db:
                waiting = db.get(TestRun, run_id).state == 'awaiting_operator'
            if waiting:
                # Waiting contains no active process; persist the step and lease.
                raise
            self._update(run_id, state='interrupted', verdict='error', completed_at=now(), recovery='unknown' if claimed else 'not_required', message='服务停止，执行状态需要核对；不会自动重试')
            self._release(run_id, 'unknown' if claimed else 'not_required')
            raise
        except Exception as exc:
            with SessionLocal() as db:
                current = db.get(TestRun, run_id)
                report = deepcopy(current.result or {})
                if getattr(exc, 'details', None):
                    report.update(exc.details)
                present = {c['binding_id'] for c in report.get('cases', [])}
                for case in current.request_snapshot.get('cases', []):
                    if case['binding_id'] not in present:
                        failed = case['binding_id'] == report.get('current_case')
                        report.setdefault('cases', []).append({'binding_id': case['binding_id'], 'name': case['name'], 'required': case['required'], 'state': 'completed' if failed else 'not_run', 'verdict': 'error' if failed else 'blocked', 'reason': str(exc) if failed else '前序阶段未完成'})
                current.result = report
                db.commit()
            self._update(run_id, state='completed', verdict='error', recovery=recovery, completed_at=now(), message=str(exc))
            self._archive_evidence(run_id)
            self._release(run_id, recovery)
