# Basic Case execution, v1

Four versioned cases: BASIC-VERSION, BASIC-FOV-READ, BASIC-FOV-WRITE, BASIC-FILE-ROUNDTRIP. Definitions and fixtures live under bench/test-cases/<code>/1.0.0. Seed the registry with PYTHONPATH=server/src .venv/bin/python -m develop_hub.seed_basic_cases.

The round freezes definition, executable, runtime, entrypoint and fixture SHA-256 hashes. Controller accepts registered adapter IDs only; JSON cannot supply arbitrary shell commands. Executable builds independently from bench/host-tools-source via build-host-tools.sh with CXX set to an AArch64 compiler. No tracked SDK changes are required.

Round execution is optional and requires flashing. Following chip/serial/image validation, the controller remembers the physical USB port, flashes update.img, then verifies the application commit on the same port before running cases sequentially. UVC serial currently returns zeros; it is not used as an identity check.

Each job contains cases/<code>/attempt-001/{case-input.json,result.json,protocol.jsonl,stdout.log,stderr.log,inputs/,artifacts/}. result.json is atomically replaced. Job result.json includes heartbeat and individual outcomes. Develop Hub polls status and retrieves paginated evidence over the existing SSH channel. Finished attempts are not overwritten or automatically rerun.

Every XU ioctl records actual TX/RX bytes, unit, selector, query, lengths, return code, errno, timestamp and elapsed time. Failed GETs contain no fabricated response. This is host ioctl evidence, not a hardware USB bus capture. Payload presentation preserves bytes; runtime assertions record decoded functional values separately.

PASS requires tool exit zero, all assertions true and actual protocol evidence. FOV write always attempts original-value restoration, including after assertion failure. Recovery failure causes ERROR and blocks subsequent cases. File roundtrip compares length and SHA-256 of a fixed 1024-byte binary fixture. Current CLI has no file-delete command: an isolated /tmp/qa-<id>.bin remains on the device, explicitly reported as retained_temporary_file, and is removed by device reboot.

Execution definitions and results reuse existing JSON snapshot columns. No database structure changes, therefore no Alembic DDL migration is needed. Added terminal enum values test_complete/test_failed use the existing unconstrained VARCHAR status column.

Scope: four samples only. OTA, IMU, ISP, streaming, remaining control reads/writes and stress scenarios remain unadapted. Historical case registrations must not imply coverage or PASS.

## Application freshness
Real verification found SDK repacking could retain an old Buildroot application. For linux/app/struct_light, each round now runs ./build.sh bmake:struct_light-reconfigure after checkout/lunch, checks generated git_build_id.txt against the locked commit, then builds the SDK image. The post-flash XU version assertion independently checks the running firmware. Buildroot's copied source tree may append -dirty; the original application repository still requires tracked-clean status.

## Verified 2026-09-12

- Server pytest: 51 passed; web lint and production build passed; host-tools native CMake build passed.
- TEST-000004 correctly rejected old application b5d89f0 after successful flashing (expected b31ba8a).
- TEST-000005 (d9d1b407-32e2-4c3e-a501-285e3cbcb56b), Change 316200 / PS6: checkout, forced application rebuild, SDK image, transfer, flash, boot identity and all four cases passed.
- Actual XU trace records: VERSION 5, FOV READ 6, FOV WRITE 20, FILE ROUNDTRIP 90.
- FOV restored to 101.64. Input and downloaded output both 1024 bytes, SHA-256 785b0751fc2c53dc14a4ce3d800e69ef9ce1009eb327ccf458afe09c242c26c9.
- Evidence API pagination and binary downloads verified end-to-end over SSH. Web HTTP returned 200. Browser automation provider unavailable; visual browser verification was not completed.
