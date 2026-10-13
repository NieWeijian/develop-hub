"""175 administrator-only SDK registration; never invoked from the operator API."""
import argparse
from sqlalchemy import select
from .db import SessionLocal
from .models import SdkRepository
from .test_workbench import discover_sdk_registration, validate_sdk_paths, validate_sdk_manifest_checkout

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', required=True)
    parser.add_argument('--root', required=True)
    parser.add_argument('--board-directory', default='project/cfg')
    parser.add_argument('--board-pattern', default='BoardConfig*.mk')
    parser.add_argument('--artifact-root', required=True)
    parser.add_argument('--artifact-pattern', action='append', required=True)
    args = parser.parse_args()
    root, artifacts = validate_sdk_paths(args.root, args.artifact_root)
    validate_sdk_manifest_checkout(root)
    discovered = discover_sdk_registration(str(root), args.board_directory, args.board_pattern)
    with SessionLocal() as db:
        if db.scalar(select(SdkRepository).where(SdkRepository.root_path == str(root))):
            parser.error('SDK already registered; existing product baselines must not be silently changed')
        db.add(SdkRepository(name=args.name, root_path=str(root), board_configs=discovered['board_configs'], artifact_root=str(artifacts), artifact_patterns=args.artifact_pattern))
        db.commit()
    print(f'Registered {args.name}: {len(discovered["board_configs"])} Board Configs')

if __name__ == '__main__':
    main()
