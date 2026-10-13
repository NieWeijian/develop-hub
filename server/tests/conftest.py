from __future__ import annotations

import os
import tempfile
from pathlib import Path


_test_root = Path(tempfile.mkdtemp(prefix="develop-hub-pytest-"))
os.environ["DEVELOP_HUB_DATABASE_URL"] = f"sqlite:///{_test_root / 'develop-hub.db'}"
os.environ["DEVELOP_HUB_ARTIFACT_ROOT"] = str(_test_root / "artifacts")
os.environ["DEVELOP_HUB_WORKTREE_ROOT"] = str(_test_root / "worktrees")
os.environ["DEVELOP_HUB_QA_PLUGIN_ROOT"] = str(_test_root / "qa-plugins")
os.environ["DEVELOP_HUB_AUTO_CREATE_SCHEMA"] = "true"
