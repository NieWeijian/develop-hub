from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from .config import settings
from .db import SessionLocal, initialize_database
from .models import Repository, SdkRepository, TestBench
from .test_workbench import validate_sdk_paths


def board_configs(root: Path) -> list[str]:
    return sorted({
        path.name
        for path in (root / "project" / "cfg").rglob("BoardConfig*.mk")
        if path.is_file()
    })


def seed() -> tuple[int, int]:
    initialize_database()
    sdk_count = 0
    with SessionLocal() as db:
        repositories = list(
            db.scalars(
                select(Repository).where(
                    Repository.enabled.is_(True),
                    Repository.build_root.is_not(None),
                    Repository.firmware_image_root.is_not(None),
                )
            )
        )
        sdk_roots: set[str] = set()
        for repository in repositories:
            assert repository.build_root and repository.firmware_image_root
            root, artifacts = validate_sdk_paths(repository.build_root, repository.firmware_image_root)
            if str(root) in sdk_roots:
                continue
            sdk_roots.add(str(root))
            configs = board_configs(root)
            if not configs:
                continue
            sdk = db.scalar(select(SdkRepository).where(SdkRepository.root_path == str(root)))
            if sdk:
                sdk.board_configs = configs
                sdk.artifact_root = str(artifacts)
                sdk.artifact_patterns = ["*.img"]
                sdk.enabled = True
            else:
                db.add(SdkRepository(
                    name=root.name,
                    root_path=str(root),
                    board_configs=configs,
                    artifact_root=str(artifacts),
                    artifact_patterns=["*.img"],
                ))
            sdk_count += 1

        bench = db.scalar(select(TestBench).where(TestBench.name == "3588-qa-01"))
        values = {
            "host": settings.qa_host,
            "port": settings.qa_port,
            "user": settings.qa_user,
            "remote_root": settings.qa_work_root,
            "enabled": True,
        }
        if bench:
            for key, value in values.items():
                setattr(bench, key, value)
        else:
            db.add(TestBench(name="3588-qa-01", **values))
        db.commit()
    return sdk_count, 1


def main() -> None:
    sdk_count, bench_count = seed()
    print(f"Test workbench registry ready: {sdk_count} SDK(s), {bench_count} bench(es)")


if __name__ == "__main__":
    main()
