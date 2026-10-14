from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _path_env(name: str, default: str) -> Path:
    return Path(os.getenv(name, default)).expanduser().resolve()


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_name: str
    environment: str
    host: str
    port: int
    database_url: str
    auto_create_schema: bool
    artifact_root: Path
    worktree_root: Path
    schema_root: Path
    codex_bin: str
    codex_model: str | None
    max_concurrent_jobs: int
    max_repair_attempts: int
    validation_timeout_seconds: int
    cancel_grace_seconds: int
    qa_host: str
    qa_port: int
    qa_user: str
    qa_ssh_key: Path
    qa_work_root: str
    qa_codex_bin: str
    qa_ota_tool: str
    qa_plugin_root: Path
    cors_origins: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "Settings":
        root = Path(os.getenv("DEVELOP_HUB_ROOT", Path.cwd())).resolve()
        database_url = os.getenv(
            "DEVELOP_HUB_DATABASE_URL",
            f"sqlite:///{root / 'var' / 'rk-testing.db'}",
        )
        origins = tuple(
            item.strip()
            for item in os.getenv(
                "DEVELOP_HUB_CORS_ORIGINS",
                "http://localhost:3000,http://127.0.0.1:3000",
            ).split(",")
            if item.strip()
        )
        return cls(
            app_name="Develop Hub",
            environment=os.getenv("DEVELOP_HUB_ENV", "development"),
            host=os.getenv("DEVELOP_HUB_HOST", "127.0.0.1"),
            port=int(os.getenv("DEVELOP_HUB_PORT", "8175")),
            database_url=database_url,
            auto_create_schema=_bool_env("DEVELOP_HUB_AUTO_CREATE_SCHEMA", True),
            artifact_root=_path_env("DEVELOP_HUB_ARTIFACT_ROOT", str(root / "var" / "artifacts")),
            worktree_root=_path_env("DEVELOP_HUB_WORKTREE_ROOT", str(root / "var" / "worktrees")),
            schema_root=_path_env("DEVELOP_HUB_SCHEMA_ROOT", str(root / "server" / "schemas")),
            codex_bin=os.getenv("DEVELOP_HUB_CODEX_BIN", "codex"),
            codex_model=os.getenv("DEVELOP_HUB_CODEX_MODEL") or None,
            max_concurrent_jobs=int(os.getenv("DEVELOP_HUB_MAX_CONCURRENT_JOBS", "1")),
            max_repair_attempts=int(os.getenv("DEVELOP_HUB_MAX_REPAIR_ATTEMPTS", "2")),
            validation_timeout_seconds=int(os.getenv("DEVELOP_HUB_VALIDATION_TIMEOUT_SECONDS", "1800")),
            cancel_grace_seconds=int(os.getenv("DEVELOP_HUB_CANCEL_GRACE_SECONDS", "10")),
            qa_host=os.getenv("DEVELOP_HUB_QA_HOST", "172.16.38.137"),
            qa_port=int(os.getenv("DEVELOP_HUB_QA_PORT", "22")),
            qa_user=os.getenv("DEVELOP_HUB_QA_USER", "linaro"),
            qa_ssh_key=_path_env("DEVELOP_HUB_QA_SSH_KEY", str(root / "var" / "ssh" / "qa-3588-ed25519")),
            qa_work_root=os.getenv("DEVELOP_HUB_QA_WORK_ROOT", "/home/linaro/develop-hub-qa"),
            qa_codex_bin=os.getenv("DEVELOP_HUB_QA_CODEX_BIN", "/home/linaro/.local/bin/codex"),
            qa_ota_tool=os.getenv(
                "DEVELOP_HUB_QA_OTA_TOOL",
                "/home/linaro/Desktop/DEV-structlight/tools/bin/uvc_host_ota",
            ),
            qa_plugin_root=_path_env("DEVELOP_HUB_QA_PLUGIN_ROOT", str(root / "var" / "qa-plugins")),
            cors_origins=origins,
        )

    def ensure_directories(self) -> None:
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        self.qa_plugin_root.mkdir(parents=True, exist_ok=True)
        if self.database_url.startswith("sqlite:///"):
            Path(self.database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)


settings = Settings.from_env()
