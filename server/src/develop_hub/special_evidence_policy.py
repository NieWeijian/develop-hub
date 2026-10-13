"""Frozen evidence retention policy; raw captures stay on the test bench."""
from pathlib import PurePosixPath
import re
from datetime import datetime

POLICY={'mode':'remote_retained_v1','result_transport':'complete_json','raw_files':'verified_remote_only'}

def remote_evidence_complete(run,spec,item):
    if run.tool_snapshot.get('evidence_policy')!=POLICY:return False
    if not isinstance(item,dict):return False
    name=item.get('path','')
    if not isinstance(name,str):return False
    path=PurePosixPath(name)
    if not name or path.is_absolute() or '..' in path.parts or '\\' in name:return False
    if not re.fullmatch(r'[0-9a-f]{64}',str(item.get('sha256',''))):return False
    size=item.get('size_bytes')
    if type(size) is not int or not 0<=size<=1024*1024*1024:return False
    index=next((i for i,s in enumerate(run.definition_snapshot['steps']) if s['id']==spec['id']),None)
    if index is None:return False
    expected=run.remote_path+'/steps/'+str(index).zfill(2)+'-'+spec['id']+'/'+name
    try:
        verified=datetime.fromisoformat(item['remote_verified_at'])
        if verified.tzinfo is None:return False
    except (KeyError,TypeError,ValueError):return False
    return (item.get('archived') is False and item.get('remote_only') is True
        and item.get('remote_path')==expected
        and item.get('remote_verified_sha256')==item['sha256']
        and item.get('remote_verified_size_bytes')==size
        and not item.get('download_url'))
