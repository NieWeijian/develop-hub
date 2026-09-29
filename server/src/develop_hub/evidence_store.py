"""Archive bench case evidence on the Hub and serve only declared, verified artifacts."""
import asyncio
import hashlib
import json
from pathlib import Path, PurePosixPath
from .basic_cases import ROOT

LOCKS={}
def directory(number):return ROOT.parent/'evidence'/f'TEST-{number:06d}'

def artifact(folder, code, name):
    if '/' in code or '\\' in code:raise ValueError('Invalid testcase')
    p=PurePosixPath(name)
    if p.is_absolute() or '..' in p.parts or '\\' in name:raise ValueError('Invalid artifact path')
    attempt=folder/'cases'/code/'attempt-001'
    result=json.loads((attempt/'result.json').read_text())
    item=next((f for f in result.get('files',[]) if f['path']==name),None)
    if not item or item.get('role') not in ('image','raw_frame','frame_metadata','input','output','measurement'):raise ValueError('Undeclared artifact')
    path=(attempt/name).resolve()
    if attempt.resolve() not in path.parents or path.is_symlink():raise ValueError('Invalid artifact location')
    if path.stat().st_size!=item['size_bytes'] or path.stat().st_size>32*1024*1024:raise ValueError('Artifact size mismatch')
    if hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']:raise ValueError('Artifact digest mismatch')
    return path

async def archive(workflow,bench,remote,number):
    from .test_workbench import run_command
    folder=directory(number)
    async with LOCKS.setdefault(number,asyncio.Lock()):
        if (folder/'archive-complete.json').exists():return
        folder.mkdir(parents=True,exist_ok=True)
        opts=workflow._ssh_base(bench);opts[opts.index('-p')]='-P'
        response=await run_command(['scp',*opts,'-r','--',f'{bench.user}@{bench.host}:{remote}/cases',str(folder)],Path.cwd(),600)
        if response['exit_code']:raise RuntimeError('Evidence archive failed: '+response['stderr_tail'][-500:])
        for report in (folder/'cases').glob('*/attempt-001/result.json'):
            result=json.loads(report.read_text())
            for item in result.get('files',[]):
                if item.get('role') in ('image','raw_frame','frame_metadata','input','output','measurement'):artifact(folder,report.parent.parent.name,item['path'])
        (folder/'archive-complete.json').write_text(json.dumps({'verified':True}))
