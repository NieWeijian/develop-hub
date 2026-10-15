"""Development device restrictions, backed by container isolation."""
import os
from pathlib import Path
from fastapi import HTTPException

def simulation_only():
    return os.environ.get('DEVELOP_HUB_ENV') == 'isolated-development' or os.environ.get('DEVELOP_HUB_DEVICE_MODE') == 'simulation-only'

def require_simulation(node):
    if simulation_only() and node.get('kind') != 'simulation':
        raise HTTPException(403, detail={'code': 'development_device_blocked', 'message': '开发环境仅允许模拟设备；真实设备检查、执行和烧录已禁用'})

def validate_isolated_paths(settings):
    if os.environ.get('DEVELOP_HUB_ENV') != 'isolated-development':
        return
    prefix = 'sqlite:///'
    allowed = Path('/var/dev')
    if not settings.database_url.startswith(prefix):
        raise RuntimeError('Isolated development requires its own SQLite database')
    paths = [Path(settings.database_url[len(prefix):]), settings.artifact_root, settings.worktree_root, settings.qa_plugin_root]
    if any(not path.resolve().is_relative_to(allowed) for path in paths):
        raise RuntimeError('Development runtime data must stay inside /var/dev')
    if os.environ.get('DEVELOP_HUB_LEGACY_ENABLED', 'false').lower() != 'false':
        raise RuntimeError('Legacy scheduling is disabled in isolated development')
